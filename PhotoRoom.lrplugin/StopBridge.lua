local LrPathUtils = import 'LrPathUtils'
local LrFileUtils = import 'LrFileUtils'
local LrDialogs = import 'LrDialogs'
local root = LrPathUtils.child(LrPathUtils.parent(_PLUGIN.path), 'match-session')
local lock = LrPathUtils.child(root, 'bridge-running')
if not LrFileUtils.exists(lock) then
    LrDialogs.message('PhotoRoom', 'The matching bridge is not running.', 'info')
    return
end
local f = assert(io.open(LrPathUtils.child(root, 'stop-bridge'), 'wb'))
f:write('stop')
f:close()
LrDialogs.message('PhotoRoom', 'Stop requested. The bridge will exit after its current operation finishes. Completed matches are kept.', 'info')
