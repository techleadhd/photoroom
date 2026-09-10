return {
    LrSdkVersion = 12.0,
    LrSdkMinimumVersion = 12.0,
    LrToolkitIdentifier = 'local.photoroom.classic',
    LrPluginName = 'PhotoRoom',
    VERSION = { major = 0, minor = 3, revision = 0 },
    LrInitPlugin = 'Bridge.lua',
    LrForceInitPlugin = true,
    LrEnablePlugin = 'Bridge.lua',
    LrDisablePlugin = 'StopBridge.lua',
    LrShutdownPlugin = 'StopBridge.lua',
    LrLibraryMenuItems = {
        { title = 'PhotoRoom: show listener status', file = 'ListenerStatus.lua' },
        { title = 'PhotoRoom: start listener', file = 'StartListener.lua' },
        { title = 'PhotoRoom: stop listener', file = 'StopListener.lua' },
    },
}
