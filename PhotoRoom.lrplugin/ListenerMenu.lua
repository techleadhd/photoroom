-- User-invoked feedback only. Automatic lifecycle callbacks stay silent.
local LrDialogs = import 'LrDialogs'
local LrPathUtils = import 'LrPathUtils'
local M = {}
local function describe()
    local listener = _G.photoRoomListener
    if not listener or listener.finished then
        return 'Stopped. Choose PhotoRoom: start listener before running Python, or disable and re-enable the plug-in.'
    elseif listener.stopping then
        return 'Stopping. Waiting for the current Lightroom operation and cleanup to finish. Completed matches are kept.'
    elseif listener.status == 'matching' then
        return 'Matching. PhotoRoom is connected to Python. Progress closes automatically when the run finishes.'
    elseif listener.status == 'cleaning up' then
        return 'Cleaning up the previous run. Please wait before editing photos in Lightroom.'
    elseif listener.status == 'starting' then
        return 'Starting. The listener will become ready after initialization and any previous cleanup finish.'
    end
    return 'Ready. Run your PhotoRoom Python command to begin matching. The listener waits quietly between runs.'
end
function M.status()
    LrDialogs.message('PhotoRoom listener', describe(), 'info')
end
function M.start()
    local listener = _G.photoRoomListener
    if not listener or listener.finished or listener.stopping then
        dofile(LrPathUtils.child(_PLUGIN.path, 'Bridge.lua'))
    end
    M.status()
end
function M.stop()
    local listener = _G.photoRoomListener
    if listener and not listener.finished then listener.stopping = true end
    M.status()
end
return M
