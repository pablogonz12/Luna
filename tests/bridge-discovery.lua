-- Item 14: discovery must not depend on shell quoting.
--
-- Run with a HOME whose path contains a space and a single quote, and with no
-- `fs` shim, so the bridge falls back to its own directory listing. The old
-- io.popen("ls -1 '" .. base .. "'") broke here and silently discovered nothing,
-- which makes Luna disappear with no error.
local home = assert(os.getenv('HOME'), 'HOME must be set')
assert(home:find("'", 1, true), 'this test must run in a HOME containing a single quote')
local repo = assert(os.getenv('LUNA_REPO'), 'LUNA_REPO must be set')
local base = home .. '/.local/share/Lumen/sls-addons/'

local function write(relative, body)
  local target = base .. relative
  local directory = target:match('^(.*)/[^/]+$')
  os.execute('mkdir -p ' .. string.format('%q', directory))
  local handle = assert(io.open(target, 'wb'), 'cannot write ' .. target)
  handle:write(body)
  handle:close()
end

local addon = 'local M = {}\nfunction M.register(registry) registry["%s"] = function() return "{}" end end\nreturn M\n'
write('zeta/backend/main.lua', addon:format('Zeta'))
write('alpha/backend/main.lua', addon:format('Alpha'))
write('luna/backend/main.lua', addon:format('Luna'))
write('.hidden/backend/main.lua', addon:format('Hidden'))
write('stray-file.txt', 'not an add-on')

local bridge = assert(loadfile(repo .. '/bridge/bridge.lua'))()
local names = bridge.discover()

assert(#names == 3, 'expected three add-ons, got ' .. #names .. ' (shell quoting broke discovery)')
assert(names[1] == 'luna', 'the shell entry point must sort first, got ' .. tostring(names[1]))
assert(names[2] == 'alpha' and names[3] == 'zeta', 'discovery is not sorted: ' .. table.concat(names, ','))
for _, name in ipairs(names) do
  assert(name ~= '.hidden' and name ~= 'stray-file.txt', name .. ' is not an add-on')
end

-- Heartbeat: written next to Luna's state dir (which exists on an installed machine), never fatal.
os.execute('mkdir -p ' .. string.format('%q', (os.getenv('LUNA_HOME') or (home .. '/.local/share/Luna')) .. '/state'))
local function fresh_bridge()
  return assert(loadfile(repo .. '/bridge/bridge.lua'))()
end
local registry = {}
bridge.register(registry)
assert(type(registry.Alpha) == 'function' and type(registry.Zeta) == 'function',
  'an add-on under a quoted HOME did not register')

local expected_path = (os.getenv('LUNA_HOME') or (home .. '/.local/share/Luna')) .. '/state/bridge-heartbeat.json'
assert(bridge.heartbeat_path == expected_path, 'heartbeat path: ' .. tostring(bridge.heartbeat_path))
local beat = io.open(bridge.heartbeat_path, 'rb')
assert(beat, 'no heartbeat written at ' .. bridge.heartbeat_path)
local text = beat:read('*a'); beat:close()
assert(text:find('"addons":["luna","alpha","zeta"]', 1, true), 'heartbeat add-ons: ' .. text)
assert(text:find('"failed":[]', 1, true), 'heartbeat failed: ' .. text)
assert(text:match('"time":%d+') and text:match('"pid":%d+'), 'heartbeat time/pid: ' .. text)
assert(text:find('"format":2', 1, true), 'the additive format marker is missing: ' .. text)
assert(not text:find('"attachedAt"', 1, true), 'a boot stamp must not claim an attach: ' .. text)

-- Attach: the first add-on call is the only proof Steam is driving the bridge. It
-- must add attachedAt/lastSeen and leave the boot stamp and the add-on list alone.
local boot_time = tonumber(text:match('"time":(%d+)'))
assert(type(registry.Alpha) == 'function', 'the registered entry point is missing')
assert(registry.Alpha() == '{}', 'the wrapped entry point must return the add-on value')
local attach_beat = assert(io.open(bridge.heartbeat_path, 'rb'))
local attached = attach_beat:read('*a'); attach_beat:close()
assert(attached:match('"attachedAt":%d+'), 'no attachedAt after the first RPC: ' .. attached)
assert(attached:match('"lastSeen":%d+'), 'no lastSeen after the first RPC: ' .. attached)
assert(tonumber(attached:match('"time":(%d+)')) == boot_time, 'the boot stamp moved: ' .. attached)
assert(attached:find('"addons":["luna","alpha","zeta"]', 1, true), 'add-ons lost: ' .. attached)

-- A broken add-on is reported with its error, the others still load.
write('broken/backend/main.lua', 'this is not lua (\n')
local again = fresh_bridge()
local registry2 = {}
again.register(registry2)
assert(type(registry2.Alpha) == 'function', 'a broken add-on stopped the others from loading')
local beat2 = assert(io.open(again.heartbeat_path, 'rb')); local text2 = beat2:read('*a'); beat2:close()
assert(text2:find('"name":"broken"', 1, true) and text2:find('failed to load', 1, true), 'error text: ' .. text2)
os.remove(base .. 'broken/backend/main.lua')

-- An unwritable heartbeat location never breaks loading.
again.heartbeat_path = '/nonexistent-luna-dir/state/bridge-heartbeat.json'
local registry3 = {}
assert(pcall(again.register, registry3), 'heartbeat failure must not raise')
assert(type(registry3.Zeta) == 'function', 'add-ons must still load without a heartbeat')

print('Luna bridge discovery OK under an awkward HOME: 3 add-ons, sorted, no shell quoting')
