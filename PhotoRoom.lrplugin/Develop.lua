-- Only send controls owned by the matcher back into applyDevelopSettings.
-- getDevelopSettings also contains opaque, version-dependent structures.
local M = {}
local numeric = {}
for _, name in ipairs({ 'Exposure2012','Contrast2012','Highlights2012','Shadows2012',
    'Whites2012','Blacks2012','Temperature','Tint','IncrementalTemperature',
    'IncrementalTint',
    'CropLeft','CropRight','CropTop','CropBottom','CropAngle','CropConstrainToWarp' }) do
    numeric[name] = true
end
function M.controls(settings)
    assert(type(settings)=='table', 'Expected Develop settings table; got '..type(settings))
    local result = {}
    for name in pairs(numeric) do
        local value = settings[name]
        if value ~= nil then
            assert(type(value)=='number', 'Expected numeric '..name..'; got '..type(value))
            result[name] = value
        end
    end
    for name, expected in pairs({HasCrop='boolean',WhiteBalance='string'}) do
        local value = settings[name]
        if value ~= nil then
            assert(type(value)==expected, 'Unexpected type for '..name..': '..type(value))
            result[name] = value
        end
    end
    return result
end
function M.patch(desired, previous, baseline)
    local target = M.controls(desired)
    local defaults = {HasCrop=false,CropLeft=0,CropTop=0,CropRight=1,CropBottom=1,
        CropAngle=0,CropConstrainToWarp=0}
    for name in pairs(M.controls(previous)) do
        if target[name] == nil then
            if baseline[name] ~= nil then target[name] = baseline[name]
            elseif defaults[name] ~= nil then target[name] = defaults[name]
            elseif numeric[name] and not name:match('^Crop') and name ~= 'Temperature' then target[name] = 0
            else error('Cannot safely restore omitted control '..name) end
        end
    end
    local patch = {}
    for name, value in pairs(target) do
        if previous[name] ~= value then patch[name] = value end
    end
    return patch, target
end
return M
