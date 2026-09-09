local LrApplication = import 'LrApplication'
local LrDialogs = import 'LrDialogs'
local LrTasks = import 'LrTasks'
local LrPathUtils = import 'LrPathUtils'
local LrFileUtils = import 'LrFileUtils'
local LrExportSession = import 'LrExportSession'
local LrFunctionContext = import 'LrFunctionContext'
local LrProgressScope = import 'LrProgressScope'
local Json = dofile(LrPathUtils.child(_PLUGIN.path, 'Json.lua'))
local Develop = dofile(LrPathUtils.child(_PLUGIN.path, 'Develop.lua'))
local VERSION = '0.2.0'

local function read(path)
    local f = io.open(path, 'rb'); if not f then return nil end
    local s = f:read('*a'); f:close(); return s
end
local function write(path, data)
    local tmp = path .. ".tmp"
    local f = assert(io.open(tmp, "wb"))
    f:write(data)
    f:close()

    -- move() will not overwrite an existing file
    if LrFileUtils.exists(path) then
        LrFileUtils.delete(path)
    end

    local ok, err = LrFileUtils.move(tmp, path)
    assert(ok, err or ("move failed: " .. tmp .. " -> " .. path))
end
local function exported(photo, directory, edge, stage)
    stage('createExportDirectory')
    LrFileUtils.createAllDirectories(directory)
    local settings = {
        LR_export_destinationType = 'specificFolder',
        LR_export_destinationPathPrefix = directory,
        LR_export_useSubfolder = false,
        LR_collisionHandling = 'rename',
        LR_renamingTokensOn = false,
        LR_format = 'TIFF',
        LR_tiff_bitDepth = 16,
        LR_tiff_compressionMethod = 'compressionMethod_None',
        LR_export_colorSpace = 'ProPhotoRGB',
        LR_size_doConstrain = true,
        LR_size_resizeType = 'longEdge',
        LR_size_maxHeight = edge or 768,
        LR_size_maxWidth = edge or 768,
        LR_size_doNotEnlarge = true,
        LR_size_resolution = 240,
        LR_outputSharpeningOn = false,
        LR_useWatermark = false,
        LR_embeddedMetadataOption = 'all',
        LR_minimizeEmbeddedMetadata = false,
        LR_removeLocationMetadata = false,
        LR_removeFaceMetadata = false,
        LR_includeVideoFiles = false,
        LR_export_postProcessing = 'do_nothing',
    }
    stage('createExportSession')
    local session = LrExportSession { photosToExport = { photo }, exportSettings = settings }
    stage('iterateRenditions')
    for _, rendition in session:renditions() do
        stage('waitForRender')
        local ok, path = rendition:waitForRender()
        if not ok then error('Lightroom export failed: ' .. tostring(path)) end
        return path
    end
    error('Lightroom did not produce an export')
end

LrTasks.startAsyncTask(function()
    LrFunctionContext.callWithContext('PhotoRoom', function(context)
        -- The plug-in and Python launcher resolve the same project-relative folder.
        local root = LrPathUtils.child(LrPathUtils.parent(_PLUGIN.path), 'match-session')
        LrFileUtils.createAllDirectories(root)
        local config = nil
        local progress = LrProgressScope { title = 'PhotoRoom: ready for Python', functionContext = context }
        local catalog = LrApplication.activeCatalog()
        local copies, lastId = {}, nil
        local activeStage = 'idle'
        local activeJob = nil
        local function stage(name)
            activeStage = name
            local f = assert(io.open(LrPathUtils.child(root,'bridge.log'),'ab'))
            f:write(Json.encode({version=VERSION,id=activeJob and activeJob.id,
                action=activeJob and activeJob.action,run_id=config and config.run_id,stage=name}) .. '\n')
            f:close()
        end
        local originalTarget = catalog:getTargetPhoto()
        local originalSelection = catalog:getTargetPhotos()
        local lock = LrPathUtils.child(root, 'bridge-running')
        if read(lock) then
            LrDialogs.message('PhotoRoom', 'PhotoRoom bridge is already running. If Lightroom crashed, remove match-session/bridge-running and start the plug-in again.', 'critical')
            return
        end
        local stopRequest = LrPathUtils.child(root, 'stop-bridge')
        LrFileUtils.delete(stopRequest)
        write(lock, 'running')
        local orientationTurns = {AB=0,DA=1,CD=2,BC=3}
        local orientationNames = {[0]='AB',[1]='DA',[2]='CD',[3]='BC'}
        local function orientTo(state, desired)
            local current = state.photo:getDevelopSettings().orientation
            if current == desired then return end
            assert(orientationTurns[current] and orientationTurns[desired],
                'Cannot automatically rotate an unknown or mirrored orientation')
            local turns = (orientationTurns[desired]-orientationTurns[current]) % 4
            if turns > 2 then turns = turns-4 end
            for _=1,math.abs(turns) do
                -- Classic 12+ acts on this photo only, regardless of selection.
                if turns > 0 then state.photo:rotateLeft() else state.photo:rotateRight() end
            end
            assert(state.photo:getDevelopSettings().orientation == desired,
                'Lightroom did not apply the requested orientation')
        end
        local function restore(state)
            if state.committed or state.virtual then return end
            if state.initialOrientation then orientTo(state,state.initialOrientation) end
            local patch, target = Develop.patch(state.baseline,state.previous,state.baseline)
            if next(patch) then
                local applied=false
                catalog:withWriteAccessDo('PhotoRoom restore original',function()
                    state.photo:applyDevelopSettings(patch,'PhotoRoom restore original')
                    applied=true
                end,{timeout=30})
                assert(applied,'Could not restore original: catalog write access timed out')
            end
            state.previous=target
        end
        context:addCleanupHandler(function()
            for _, state in pairs(copies) do
                local ok, err=LrTasks.pcall(function() restore(state) end)
                if not ok then stage('restoreFailed:'..tostring(err)) end
            end
            LrFileUtils.delete(stopRequest)
            LrFileUtils.delete(lock)
            if originalTarget then LrTasks.pcall(function() catalog:setSelectedPhotos(originalTarget, originalSelection) end) end
        end)
        local function handle(job)
            if job.action == 'stop' then
                for _, state in pairs(copies) do restore(state) end
                copies = {}
                if originalTarget then catalog:setSelectedPhotos(originalTarget, originalSelection) end
                return { stopped = true }
            end
            if job.action == 'hello' then
                return {ready=true,bridge_version=VERSION}
            end
            assert(type(job.source) == 'string', 'Missing source path')
            local state = copies[job.source]
            local photo = state and state.photo
            if job.action == 'abort' then
                if state then stage('restoreOriginal');state.committed=false;restore(state) end
                return {restored=true}
            end
            if job.action == 'commit' then
                assert(state,'No active photo to commit')
                state.committed=true
                return {committed=true}
            end
            if job.action == 'begin' then
                stage('findPhotoByPath')
                local master = catalog:findPhotoByPath(job.source)
                assert(master, 'Original is not imported into this catalog: ' .. job.source)
                assert(type(master)=='table' or type(master)=='userdata', 'Expected photo object; got '..type(master))
                stage('checkMaster')
                assert(not master:getRawMetadata('isVirtualCopy'), 'Expected a master photo')
                photo=master
                if config.virtual_copies == true then
                    stage('setSelectedPhotos')
                    catalog:setSelectedPhotos(master, { master })
                    stage('createVirtualCopies')
                    local created = catalog:createVirtualCopies('PhotoRoom ' .. config.run_id)
                    assert(type(created)=='table', 'Expected virtual-copy array; got '..type(created))
                    assert(#created == 1, 'Could not create exactly one virtual copy')
                    photo = created[1]
                    assert(type(photo)=='table' or type(photo)=='userdata', 'Expected virtual-copy object; got '..type(photo))
                end
                stage('getInitialDevelopSettings')
                local initial = photo:getDevelopSettings()
                local controls = Develop.controls(initial)
                copies[job.source] = {photo=photo,baseline=controls,previous=controls,
                    initialOrientation=initial.orientation,virtual=config.virtual_copies==true}
                stage('getFileFormat')
                return { settings = initial, file_format = photo:getRawMetadata('fileFormat'), bridge_version=VERSION }
            end
            assert(photo, 'Send begin before render')
            if job.action == 'orient' then
                local turns = job.settings and job.settings.quarter_turns_ccw
                assert(type(turns)=='number' and turns==math.floor(turns) and math.abs(turns)<=2 and turns~=0,
                    'Expected a quarter-turn count of -2, -1, 1, or 2')
                local current = photo:getDevelopSettings().orientation
                assert(orientationTurns[current], 'Automatic rotation requires an unmirrored photo')
                stage('rotatePhoto')
                orientTo(state,orientationNames[(orientationTurns[current]+turns)%4])
                local actual = photo:getDevelopSettings()
                state.previous = Develop.controls(actual)
                return {settings=actual}
            end
            assert(job.action == 'render', 'Unknown action')
            assert(type(job.settings) == 'table', 'Missing settings')
            stage('prepareDevelopPatch')
            local patch, target = Develop.patch(job.settings,state.previous,state.baseline)
            state.previous = target
            if next(patch) then
                stage('catalogWriteAccess')
                local applied = false
                catalog:withWriteAccessDo('PhotoRoom candidate', function()
                    stage('applyDevelopSettings')
                    photo:applyDevelopSettings(patch, 'PhotoRoom candidate')
                    applied = true
                end, { timeout = 30 })
                assert(applied, 'Catalog write access timed out')
            end
            state.previous = target
            -- Export waits for Lightroom's rendering, not a guessed UI delay.
            local directory = LrPathUtils.child(root, 'renders/' .. job.id)
            local path = exported(photo, directory, job.edge, stage)
            stage('getRenderedDevelopSettings')
            return { path = path, settings = photo:getDevelopSettings() }
        end
        while not progress:isCanceled() and not read(stopRequest) do
            local txt = read(LrPathUtils.child(root, 'request.json'))
            if txt then
                local decoded, job = pcall(Json.decode, txt)
                if decoded and type(job)=='table' and type(job.run_id)=='string' and
                   job.run_id:match('^[a-zA-Z0-9_-]+$') and job.action=='hello' and
                   (not config or config.run_id ~= job.run_id) then
                    local text = read(LrPathUtils.child(root,'session.json'))
                    local valid, nextConfig = pcall(Json.decode,text or '')
                    if valid and type(nextConfig)=='table' and nextConfig.protocol==2 and nextConfig.run_id==job.run_id then
                        -- A new run also restores an interrupted previous run.
                        for _, state in pairs(copies) do restore(state) end
                        copies, lastId, config = {}, nil, nextConfig
                    end
                end
                if decoded and type(job)=='table' and config and job.run_id==config.run_id and job.id ~= lastId then
                    assert(type(job.id) == 'string' and job.id:match('^[a-zA-Z0-9_-]+$'), 'Invalid request ID')
                    lastId = job.id
                    activeJob = job
                    stage('start')
                    progress:setCaption(job.label or job.action or 'Matching')
                    local ok, result = LrTasks.pcall(function() return handle(job) end)
                    if not ok then
                        result = { error = 'PhotoRoom '..VERSION..' ['..tostring(job.action)..'/'..activeStage..']: '..tostring(result),
                            action=job.action, stage=activeStage, bridge_version=VERSION }
                        stage('failed:'..activeStage)
                    end
                    result.id = job.id
                    result.run_id = job.run_id
                    write(LrPathUtils.child(root, 'response-' .. job.id .. '.json'), Json.encode(result))
                    if job.action == 'stop' then progress:setCaption('Ready for the next PhotoRoom run') end
                end
            end
            LrTasks.sleep(0.2)
        end
        if progress:isCanceled() or read(stopRequest) then write(LrPathUtils.child(root, 'cancelled'), config and config.run_id or '') end
        progress:done()
    end)
end)
