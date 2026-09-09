-- Small data-only JSON codec. No loadstring or execution of request contents.
local M = {}
M.null = {}
local escapes = { ['"']='\\"', ['\\']='\\\\', ['\b']='\\b',
    ['\f']='\\f', ['\n']='\\n', ['\r']='\\r', ['\t']='\\t' }
local function quote(s)
    return '"' .. s:gsub('[%z\1-\31\\"]', function(c)
        return escapes[c] or string.format('\\u%04x', c:byte())
    end) .. '"'
end
function M.encode(v)
    local t = type(v)
    if v == M.null or v == nil then return 'null' end
    if t == 'string' then return quote(v) end
    if t == 'boolean' then return tostring(v) end
    if t == 'number' then
        assert(v == v and v ~= math.huge and v ~= -math.huge, 'Nonfinite JSON number')
        return string.format('%.17g', v)
    end
    assert(t == 'table', 'Unsupported JSON value: ' .. t)
    local count, array = 0, true
    for k in pairs(v) do
        count = count + 1
        if type(k) ~= 'number' or k < 1 or k % 1 ~= 0 then array = false end
    end
    local out = {}
    if array and count > 0 and count == #v then
        for i = 1, #v do out[i] = M.encode(v[i]) end
        return '[' .. table.concat(out, ',') .. ']'
    end
    for k, item in pairs(v) do out[#out+1] = quote(tostring(k)) .. ':' .. M.encode(item) end
    table.sort(out)
    return '{' .. table.concat(out, ',') .. '}'
end
local function utf8(cp)
    if cp < 128 then return string.char(cp) end
    if cp < 2048 then return string.char(192+math.floor(cp/64), 128+cp%64) end
    if cp < 65536 then
        return string.char(224+math.floor(cp/4096), 128+math.floor(cp/64)%64, 128+cp%64)
    end
    return string.char(240+math.floor(cp/262144), 128+math.floor(cp/4096)%64,
        128+math.floor(cp/64)%64, 128+cp%64)
end
function M.decode(s)
    local i, parse = 1, nil
    local function ws() local _, e = s:find('^[ \t\r\n]*', i); i = (e or i-1)+1 end
    local function hex()
        local h = s:sub(i, i+3)
        assert(h:match('^%x%x%x%x$'), 'Invalid Unicode escape')
        i = i+4; return tonumber(h,16)
    end
    local function str()
        assert(s:sub(i,i) == '"'); i = i+1
        local out = {}
        while i <= #s do
            local c = s:sub(i,i); i = i+1
            if c == '"' then return table.concat(out) end
            if c == '\\' then
                c = s:sub(i,i); i = i+1
                local simple = { ['"']='"', ['\\']='\\', ['/']='/', b='\b', f='\f', n='\n', r='\r', t='\t' }
                if c == 'u' then
                    local cp = hex()
                    if cp >= 0xD800 and cp <= 0xDBFF then
                        assert(s:sub(i,i+1) == '\\u', 'Missing low surrogate'); i=i+2
                        local low = hex(); assert(low >= 0xDC00 and low <= 0xDFFF)
                        cp = 0x10000+(cp-0xD800)*1024+low-0xDC00
                    else assert(cp < 0xDC00 or cp > 0xDFFF, 'Unexpected low surrogate') end
                    out[#out+1] = utf8(cp)
                else assert(simple[c], 'Invalid escape'); out[#out+1] = simple[c] end
            else assert(c:byte() >= 32, 'Unescaped control'); out[#out+1] = c end
        end
        error('Unterminated JSON string')
    end
    parse = function(depth)
        assert(depth < 100, 'JSON nesting too deep'); ws()
        local c = s:sub(i,i)
        if c == '"' then return str() end
        if c == '{' or c == '[' then
            local object, out = c == '{', {}; i = i+1; ws()
            local close = object and '}' or ']'
            if s:sub(i,i) == close then i=i+1; return out end
            while true do
                ws(); local key
                if object then key = str(); ws(); assert(s:sub(i,i)==':'); i=i+1 end
                local item = parse(depth+1)
                if object then out[key] = item else out[#out+1] = item end
                ws(); c = s:sub(i,i); i=i+1
                if c == close then return out end
                assert(c == ',', 'Expected comma')
            end
        end
        for literal, v in pairs({ ['true']=true, ['false']=false, ['null']=M.null }) do
            if s:sub(i,i+#literal-1) == literal then i=i+#literal; return v end
        end
        local token = s:match('^-?%d+%.?%d*[eE]?[+-]?%d*', i)
        assert(token and tonumber(token), 'Invalid JSON value at ' .. i)
        i=i+#token; return tonumber(token)
    end
    local result = parse(0); ws(); assert(i > #s, 'Trailing JSON data'); return result
end
return M
