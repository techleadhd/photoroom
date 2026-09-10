-- Also called by Lightroom when the plug-in is disabled or unloaded.
local listener = _G.photoRoomListener
if listener then listener.stopping = true end
