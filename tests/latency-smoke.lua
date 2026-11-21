-- SLS Luna page-load budget test.
--
-- The plugin host answers one RPC at a time on a single injector thread, so a
-- handler that decodes the whole HLTB store, re-encodes it, or spawns a
-- process on a plain status read stalls every page behind it. This test builds
-- a realistic HOME (a 150-app library, a store of the real size and shape, a
-- month of playtime) and asserts:
--   * no page-load handler blocks for hundreds of milliseconds once warm;
--   * the HLTB reply is served from its cached wire text, not re-encoded;
--   * Health counts the store from its sidecar, without decoding it;
--   * a warm status pass spawns no process at all, and the Lumen channels read
--     spawns once per TTL, not once per page load;
--   * SlsPlaytimeGet's daily30/prev7d are derived correctly.
assert(os.getenv('LUNA_TEST_HOME') == os.getenv('HOME'), 'Test must run in isolated HOME')

local home = os.getenv('HOME')
local repo = os.getenv('LUNA_REPO') or '.'
local data = home .. '/.local/share/slssteam-integrations'
local steam = home .. '/.local/share/Steam'
local lumen = home .. '/.local/share/Lumen'

local function mkdir(path) os.execute('mkdir -p ' .. string.format('%q', path)) end
local function write(path, text)
  mkdir(path:match('^(.*)/[^/]+$') or '.')
  local handle = assert(io.open(path, 'wb'))
  handle:write(text); handle:close()
end
local function now_ms()
  -- os.time is one-second; os.clock is CPU seconds. Child waits are covered by
  -- the spawn assertions instead, which is where a blocking call would show.
  return os.clock() * 1000
end

-- ------------------------------------------------------------------ fixtures
local NSTORE, NLIB, NAPPS = 3000, 1500, 150
local apps, store, library = {}, {}, {}
for index = 1, NAPPS do
  local appid = 100000 + index
  apps[#apps + 1] = appid
  store[#store + 1] = string.format(
    '"%d":{"name":"Game %d","match":"exact","id":%d,"main":%d,"extra":%d,"all":%d,'
      .. '"completionist":%d,"fetchedAt":%d,"source":"hltb-search"}',
    appid, index, 1000 + index, 30000 + index, 40000 + index, 50000 + index,
    60000 + index, 1792000000 + index)
end
for index = NAPPS, NSTORE - 1 do
  store[#store + 1] = string.format(
    '"%d":{"name":"Extra %d","match":"none","id":0,"source":"hltb-search","fetchedAt":%d}',
    300000 + index, index, 1792000000 + index)
end
for index = 1, NLIB do
  library[#library + 1] = string.format('{"appid":%d,"name":"Lib %d","shortcut":false}',
    200000 + index, index)
end
write(data .. '/hltb.json', '{"games":{' .. table.concat(store, ',') .. '},"version":1}')
write(data .. '/hltb-library.json',
  '{"updatedAt":1795226687,"version":1,"games":[' .. table.concat(library, ',') .. ']}')
write(data .. '/luna.json', '{"version":1,"hltb_order":"short"}')

for _, appid in ipairs(apps) do
  write(steam .. '/steamapps/appmanifest_' .. appid .. '.acf', string.format([[
"AppState"
{
	"appid"		"%d"
	"name"		"Game %d"
	"StateFlags"		"4"
	"installdir"		"Game %d"
	"SizeOnDisk"		"8000000000"
}
]], appid, appid, appid))
end
write(steam .. '/config/loginusers.vdf', [[
"users"
{
	"76561198000012345"
	{
		"AccountName"		"tester"
		"MostRecent"		"1"
	}
}
]])
-- A month of daily play: today 1000 s, yesterday 900 s, ..., so prev7d is the
-- sum of days 7..13 ago (offsets 7..13) and last7d covers offsets 0..6.
local daily = {}
for offset = 0, 44 do
  local day = os.date('%Y-%m-%d', os.time() - offset * 86400)
  daily[#daily + 1] = string.format('"%s":%d', day, 1000 - offset)
end
write(data .. '/playtime/76561198000012345.json',
  '{"version":2,"account":"76561198000012345","games":{},"daily":{' .. table.concat(daily, ',')
    .. '},"events":[],"titles":{}}')

write(lumen .. '/lua/boot.lua', '-- SLS-ADDONS-INTEGRATION\n')
write(lumen .. '/lumen', 'binary')
mkdir(lumen .. '/sls-addons')
for _, addon in ipairs({ 'compressor', 'graphics', 'luna', 'playtime', 'presence', 'sgdb' }) do
  write(lumen .. '/sls-addons/' .. addon .. '/plugin.json', '{"name":"sls-' .. addon .. '"}')
end
-- The updater exists, so the channels read would really spawn Python.
write(lumen .. '/sls-luna/luna.py', '-- updater')

-- ------------------------------------------------------------------- stubs
local json = {}
local escapes = { ['"'] = '\\"', ['\\'] = '\\\\', ['\n'] = '\\n', ['\r'] = '\\r', ['\t'] = '\\t' }
local encode_value
function encode_value(value, out)
  local kind = type(value)
  if value == nil then out[#out + 1] = 'null'
  elseif kind == 'boolean' then out[#out + 1] = value and 'true' or 'false'
  elseif kind == 'number' then
    if value == math.floor(value) and math.abs(value) < 2 ^ 53 then out[#out + 1] = string.format('%d', value)
    else out[#out + 1] = string.format('%.14g', value) end
  elseif kind == 'string' then
    out[#out + 1] = '"' .. value:gsub('[%z\1-\31\\"]', function(c)
      return escapes[c] or string.format('\\u%04x', c:byte())
    end) .. '"'
  elseif kind == 'table' then
    local array = getmetatable(value) == json.__array
    out[#out + 1] = array and '[' or '{'
    local first = true
    if array then
      for index = 1, #value do
        if not first then out[#out + 1] = ',' end
        first = false; encode_value(value[index], out)
      end
    else
      for key, item in pairs(value) do
        if not first then out[#out + 1] = ',' end
        first = false
        out[#out + 1] = '"' .. tostring(key) .. '":'
        encode_value(item, out)
      end
    end
    out[#out + 1] = array and ']' or '}'
  else out[#out + 1] = 'null' end
end
function json.encode(value)
  local out = {}
  encode_value(value, out)
  return table.concat(out)
end
json.__array = { __jsonarray = true }
function json.array(value) return setmetatable(value or {}, json.__array) end
local decode_value
local function skip(text, pos) return (text:find('[^ \t\r\n]', pos) or #text + 1) end
function decode_value(text, pos)
  pos = skip(text, pos)
  local c = text:sub(pos, pos)
  if c == '{' then
    local out = {}
    pos = skip(text, pos + 1)
    if text:sub(pos, pos) == '}' then return out, pos + 1 end
    while true do
      local key; key, pos = decode_value(text, pos)
      pos = skip(text, pos) + 1
      local value; value, pos = decode_value(text, pos)
      out[key] = value
      pos = skip(text, pos)
      if text:sub(pos, pos) == '}' then return out, pos + 1 end
      pos = pos + 1
    end
  elseif c == '[' then
    local out = {}
    pos = skip(text, pos + 1)
    if text:sub(pos, pos) == ']' then return json.array(out), pos + 1 end
    while true do
      local value; value, pos = decode_value(text, pos)
      out[#out + 1] = value
      pos = skip(text, pos)
      if text:sub(pos, pos) == ']' then return json.array(out), pos + 1 end
      pos = pos + 1
    end
  elseif c == '"' then
    local out, escaped = {}, false
    pos = pos + 1
    while pos <= #text do
      local ch = text:sub(pos, pos)
      if escaped then
        if ch == 'n' then out[#out + 1] = '\n'
        elseif ch == 'r' then out[#out + 1] = '\r'
        elseif ch == 't' then out[#out + 1] = '\t'
        else out[#out + 1] = ch end
        escaped = false
      elseif ch == '\\' then escaped = true
      elseif ch == '"' then return table.concat(out), pos + 1
      else out[#out + 1] = ch end
      pos = pos + 1
    end
    error('unterminated string')
  end
  local number = text:match('^-?%d+%.?%d*[eE]?[-+]?%d*', pos)
  if number then return tonumber(number), pos + #number end
  if text:sub(pos, pos + 3) == 'true' then return true, pos + 4 end
  if text:sub(pos, pos + 4) == 'false' then return false, pos + 5 end
  if text:sub(pos, pos + 3) == 'null' then return nil, pos + 4 end
  error('unexpected byte at ' .. pos)
end
function json.decode(text)
  local value, pos = decode_value(text, 1)
  pos = skip(text, pos)
  if pos <= #text then error('trailing text at ' .. pos) end
  return value
end
package.preload['json'] = function() return json end

-- The host's fs.list is a native binding (Lumen) or a real listing (luna-host):
-- it is not an add-on's process spawn, so it uses the unwrapped popen and the
-- spawn counters below only see what the backends themselves start.
local real_popen = io.popen
local listings = {
  [lumen .. '/sls-addons'] = { 'compressor', 'graphics', 'luna', 'playtime', 'presence', 'sgdb' },
  [steam .. '/steamapps'] = (function()
    local out = {}
    for _, appid in ipairs(apps) do out[#out + 1] = 'appmanifest_' .. appid .. '.acf' end
    out[#out + 1] = 'libraryfolders.vdf'
    return out
  end)(),
  [data] = { 'hltb.json', 'hltb-library.json', 'luna.json', 'playtime' },
}
package.preload['fs'] = function()
  return {
    join = function(...) return table.concat({ ... }, '/') end,
    parent_path = function(path) return (tostring(path):gsub('/+$', '')):match('^(.*)/[^/]+$') or '.' end,
    create_directories = function(path) mkdir(path); return true end,
    exists = function(path)
      local handle = io.open(path, 'rb')
      if handle then handle:close(); return true end
      return os.rename(path, path) == true
    end,
    size = function(path)
      local handle = io.open(path, 'rb')
      if not handle then return nil end
      local size = handle:seek('end'); handle:close()
      return size
    end,
    list = function(path)
      local out = {}
      for _, name in ipairs(listings[path] or {}) do out[#out + 1] = { name = name } end
      if #out > 0 then return out end
      local pipe = real_popen("ls -1 -a '" .. path .. "' 2>/dev/null")
      if not pipe then return out end
      for line in pipe:lines() do
        if line ~= '.' and line ~= '..' then out[#out + 1] = { name = line } end
      end
      pipe:close()
      return out
    end,
  }
end
package.preload['millennium'] = function()
  return { steam_path = function() return steam end }
end

-- The updater's CLI read is faked: a counted pipe that prints the three
-- components, so the test never starts Python.
local spawns = { popen = 0, execute = 0 }
io.popen = function()
  spawns.popen = spawns.popen + 1
  local output = 'lumen        stable\nplugin       stable\nslsteam_moon beta\nfile         : '
    .. lumen .. '/update-channels.json\n'
  local read_all = false
  return {
    read = function(_, what)
      if read_all then return nil end
      read_all = true
      return output
    end,
    lines = function()
      local index = 0
      local rows = {}
      for line in output:gmatch('[^\r\n]+') do rows[#rows + 1] = line end
      return function() index = index + 1; return rows[index] end
    end,
    close = function() return true end,
  }
end
os.execute = function(...)
  spawns.execute = spawns.execute + 1
  return true
end

-- ------------------------------------------------------------------ load
local registry = {}
for _, addon in ipairs({ 'luna', 'playtime', 'presence', 'compressor', 'sgdb', 'graphics' }) do
  local plugin = assert(loadfile(repo .. '/addons/' .. addon .. '/plugin/backend/main.lua'))()
  local ok, err = pcall(plugin.register, registry)
  assert(ok, addon .. ' register failed: ' .. tostring(err))
end
local function call(name, ...)
  local handler = assert(registry[name], name .. ' is not registered')
  local before = spawns.popen + spawns.execute
  local started = now_ms()
  local ok, out = pcall(handler, ...)
  local elapsed = now_ms() - started
  assert(ok, name .. ' failed: ' .. tostring(out))
  return out, elapsed, spawns.popen + spawns.execute - before
end
local function budget(name, limit, ...)
  local out, elapsed = call(name, ...)
  assert(elapsed < limit, string.format('%s took %.1f ms, budget %d ms', name, elapsed, limit))
  return out, elapsed
end

-- ------------------------------------------------------- HLTB: cold and warm
local cold, cold_ms = budget('SlsHltbGet', 900)
assert(cold:find('"count":' .. NSTORE, 1, true), 'the cold reply must carry every stored game')
assert(#cold > 200000, 'the cold reply must be the full payload, got ' .. #cold .. ' bytes')
for _, path in ipairs({ data .. '/hltb-wire.txt', data .. '/hltb-wire.rev', data .. '/hltb-count.json' }) do
  local handle = io.open(path, 'rb')
  assert(handle, 'missing cache file ' .. path)
  handle:close()
end
local wire = assert(io.open(data .. '/hltb-wire.txt', 'rb'))
local wire_text = wire:read('*a'); wire:close()
assert(wire_text == cold, 'the wire cache must hold the exact reply text')
local warm, warm_ms = budget('SlsHltbGet', 120)
assert(warm == cold, 'the cached reply must be byte-for-byte the same payload')
-- A new VM reads the wire text instead of decoding and re-encoding the store:
-- prove it by timing the decode path away (no encode on this call at all).
assert(warm_ms * 2 < cold_ms + 1,
  string.format('the cached read (%.1f ms) must be far cheaper than the first encode (%.1f ms)',
    warm_ms, cold_ms))

local health, health_ms = budget('SlsLunaHealth', 400)
assert(health_ms < cold_ms, 'Health must not decode the store just to count it')
assert(health:find('"cached":' .. NSTORE, 1, true),
  'Health must report the sidecar count (' .. NSTORE .. ')')

-- ------------------------------------------------------- warm status pass
local page = {
  { 'SlsPlaytimeGet' }, { 'SlsPresenceGet' }, { 'SlsLunaInfo' }, { 'SlsLunaHost' },
  { 'SlsLunaUpdate', '{"action":"status"}' }, { 'SlsLunaSteam', '{"action":"status"}' },
  { 'SlsLunaLumen', '{"action":"status"}' }, { 'SlsLunaLuaTools' },
  { 'SlsLunaRefresh', '{"action":"status"}' }, { 'SlsSgdbGet' }, { 'SlsGraphicsGet' },
  { 'SlsCompressorGet', '{"light":true}' }, { 'SlsHltbGet' },
}
for _, case in ipairs(page) do call(case[1], case[2]) end
local total, spawned = 0, 0
for _, case in ipairs(page) do
  local _, elapsed, started = call(case[1], case[2])
  total = total + elapsed
  spawned = spawned + started
end
assert(total < 250, string.format('a warm page-load pass took %.1f ms, budget 250 ms', total))
assert(spawned == 0, spawned .. ' process spawn(s) on a warm status pass')

-- The channels read is a synchronous CLI call: it may spawn once, then the TTL
-- serves the page load without one.
call('SlsLunaLumen', '{"action":"status"}')
local first, _, first_spawns = call('SlsLunaLumen', '{"action":"channels"}')
assert(first_spawns == 1, 'the first channels read must run the CLI once')
local _, _, again_spawns = call('SlsLunaLumen', '{"action":"channels"}')
assert(again_spawns == 0, 'a cached channels read must not spawn again')
assert(first:find('"slsteam_moon":"beta"', 1, true), 'the parsed channels must survive the cache')

-- ------------------------------------------------- playtime daily series
local snapshot, playtime_ms = budget('SlsPlaytimeGet', 400)
local _, entry = pcall(json.decode, snapshot)
assert(type(entry) == 'table' and type(entry.daily30) == 'table', 'SlsPlaytimeGet needs a daily30 array')
assert(#entry.daily30 == 30, 'daily30 must hold 30 days, got ' .. #entry.daily30)
local expected_prev, expected_7d = 0, 0
for offset = 0, 29 do
  local day = os.date('%Y-%m-%d', os.time() - offset * 86400)
  local row = entry.daily30[30 - offset]
  assert(row.day == day, 'daily30 must be oldest first: expected ' .. day .. ', got ' .. tostring(row.day))
  assert(row.seconds == 1000 - offset, 'daily30 must carry the ledger value for ' .. day)
  if offset <= 6 then expected_7d = expected_7d + (1000 - offset) end
  if offset >= 7 and offset <= 13 then expected_prev = expected_prev + (1000 - offset) end
end
assert(entry.stats.last7d == expected_7d, 'last7d changed: ' .. tostring(entry.stats.last7d))
assert(entry.stats.prev7d == expected_prev,
  'prev7d must sum days 7..13 ago: expected ' .. expected_prev .. ', got ' .. tostring(entry.stats.prev7d))
assert(entry.stats.prev7d ~= entry.stats.last7d, 'prev7d and last7d must be different windows')
-- A day with no play is zero-filled, not missing.
assert(entry.daily30[1].seconds ~= nil, 'every daily30 row needs a seconds field')
print(string.format('Luna page-load budget OK: HLTB %d B cold %.1f ms / warm %.1f ms, Health %.1f ms, '
  .. 'warm pass %.1f ms, playtime %.1f ms', #cold, cold_ms, warm_ms, health_ms, total, playtime_ms))
