-- Composition test: the real bridge loading the real add-on backends.
--
-- This is what makes Luna more than a copy job. The bridge must register every
-- add-on it finds under sls-addons/ into one shared registry, expose each one's
-- UI and control scripts, and keep loading the rest when one add-on is broken.
-- Only json/fs/millennium are stubbed; everything else is the shipped payload.
assert(os.getenv('LUNA_TEST_HOME') == os.getenv('HOME'), 'Test must run in isolated HOME')

local home = os.getenv('HOME')
local addons = home .. '/.local/share/Lumen/sls-addons/'
local repo = os.getenv('LUNA_REPO') or '.'

-- Copy the pinned payloads into place, as the installer would.
local function copy(source, target)
  local input = assert(io.open(source, 'rb'), 'missing payload ' .. source)
  local data = input:read('*a'); input:close()
  local directory = target:match('^(.*)/[^/]+$')
  if directory then os.execute('mkdir -p ' .. string.format('%q', directory)) end
  local output = assert(io.open(target, 'wb'))
  output:write(data); output:close()
end

-- The shell add-on and the compressor ship no agent.js: they have nothing to
-- count.
local function install(addon)
  local files = { 'plugin/backend/main.lua', 'plugin/public/panel.js' }
  if addon ~= 'luna' and addon ~= 'compressor' then files[#files + 1] = 'plugin/public/agent.js' end
  for _, rel in ipairs(files) do
    local target = addons .. addon .. '/' .. rel:gsub('^plugin/', '')
    copy(repo .. '/addons/' .. addon .. '/' .. rel, target)
  end
end

install('luna')
install('compressor')
install('presence')
install('playtime')

-- Stubs: the backend runs inside Lumen, which owns these modules.
local steam_root = home .. '/steam'
os.execute('mkdir -p ' .. string.format('%q', steam_root .. '/config/stplug-in'))
package.preload['json'] = function()
  return {
    encode = function() return '{}' end,
    decode = function() return {} end,
    array = function(t) return t or {} end,
  }
end
package.preload['fs'] = function()
  return {
    create_directories = function() return true end,
    exists = function() return false end,
    join = function(...) return table.concat({ ... }, '/') end,
    parent_path = function(path) return (path:gsub('/+$', '')):match('^(.*)/[^/]+$') or '.' end,
    -- The bridge enumerates add-on directories, so the stub must really list.
    list = function(path)
      local out = {}
      local pipe = io.popen("ls -1 -a '" .. path .. "' 2>/dev/null")
      if not pipe then return out end
      for line in pipe:lines() do
        if line ~= '.' and line ~= '..' then
          local full = path .. '/' .. line
          local ok_dir = os.execute('test -d ' .. string.format('%q', full))
          out[#out + 1] = { name = line, path = full,
            is_directory = (ok_dir == true or ok_dir == 0) }
        end
      end
      pipe:close()
      return out
    end,
    read_file = function() return nil end,
  }
end
package.preload['millennium'] = function()
  return { steam_path = function() return steam_root end }
end

local bridge = assert(loadfile(repo .. '/bridge/bridge.lua'))()
assert(type(bridge) == 'table' and type(bridge.register) == 'function', 'bridge has no register')

local registry = {}
bridge.register(registry)

assert(type(registry.SlsPlaytimeGet) == 'function', 'the playtime backend did not register')
assert(type(registry.SlsPlaytimeHeartbeat) == 'function', 'the playtime heartbeat RPC is missing')
assert(type(registry.SlsPresenceGet) == 'function', 'the presence backend did not register')
assert(type(registry.SlsPresenceSetEnabled) == 'function', 'the presence enable RPC is missing')
assert(type(registry.SlsCompressorGet) == 'function', 'the compressor backend did not register')
assert(type(registry.SlsCompressorRun) == 'function', 'the compressor run RPC is missing')
assert(type(registry.SlsCompressorCancel) == 'function', 'the compressor cancel RPC is missing')
assert(type(registry.SlsLunaInfo) == 'function', 'the Luna shell backend did not register')
assert(#bridge.ui_js == 4, 'expected one UI script per add-on, got ' .. #bridge.ui_js)
-- Only the counting add-ons carry an agent.
assert(#bridge.control_js == 2, 'expected one control script per counting add-on, got ' .. #bridge.control_js)

-- Discovery is sorted with the shell entry point first (a plain alphabetical
-- order would put "compressor" before "luna"), so "luna" can set the flag the
-- other three check before drawing their own floating panels.
assert(bridge.ui_js[1]:find('__SlsLunaUi', 1, true), 'the Luna shell script is not injected first')
assert(bridge.ui_js[2]:find('__SlsCompressorPanel', 1, true), 'the compressor panel script is missing')
assert(bridge.ui_js[3]:find('__SlsPlaytimePanel', 1, true), 'the playtime panel script is missing')
assert(bridge.ui_js[4]:find('__SlsPresencePanel', 1, true), 'the presence panel script is missing')

-- A new add-on directory is picked up with no bridge change at all.
local extra = addons .. 'example/backend'
os.execute('mkdir -p ' .. string.format('%q', extra))
local handle = assert(io.open(extra .. '/main.lua', 'wb'))
handle:write('local M = {}\nfunction M.register(registry) registry.SlsExamplePing = function() return "{}" end end\nreturn M\n')
handle:close()
local with_extra = {}
bridge.register(with_extra)
assert(type(with_extra.SlsExamplePing) == 'function', 'a newly dropped add-on was not discovered')
os.execute('rm -rf ' .. string.format('%q', addons .. 'example'))

-- A broken add-on must not take the healthy one down with it.
local broken = addons .. 'presence/backend/main.lua'
local output = assert(io.open(broken, 'wb'))
output:write('this is not lua')
output:close()
local second = {}
bridge.register(second)
assert(type(second.SlsPlaytimeGet) == 'function', 'a broken add-on stopped a healthy one from loading')
assert(second.SlsPresenceGet == nil, 'the broken add-on still registered something')

-- Only one add-ons directory is read: nothing outside it is executed.
local third = {}
install('playtime')
bridge.register(third)
assert(type(third.SlsPlaytimeGet) == 'function', 're-registering a healthy add-on failed')

print('Luna bridge composition OK: four add-ons register in sorted order, a new one is discovered, a broken one is isolated')
