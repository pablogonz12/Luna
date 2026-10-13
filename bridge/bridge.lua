-- SLS integrations bridge for swwayps/lumen (does not replace LuaTools).
-- Loads every add-on directory it finds under sls-addons/. Nothing else.
--
-- Add-ons are discovered, not listed: installing one means dropping a directory
-- in, and this file never has to change. The order is sorted, which matters on
-- purpose - the "luna" shell entry point sorts before the feature add-ons so it
-- can claim the menubar before they decide whether to draw their own floating
-- panels.
local M = { ui_js = {}, control_js = {} }
local home = os.getenv('HOME') or ''
local base = home .. '/.local/share/Lumen/sls-addons/'

local function read(path)
  local f = io.open(path, 'rb')
  if not f then return nil end
  local value = f:read('*a'); f:close(); return value
end

-- A directory is an add-on when it ships backend/main.lua. That single check
-- also filters the fallback listing, so no directory metadata is needed.
local function addon_names()
  local names, seen = {}, {}
  local function consider(name)
    if type(name) ~= 'string' or name == '' or seen[name] then return end
    if name:sub(1, 1) == '.' then return end
    if name:find('/', 1, true) then return end
    if not read(base .. name .. '/backend/main.lua') then return end
    seen[name] = true
    names[#names + 1] = name
  end
  local ok, fs = pcall(require, 'fs')
  local listed = false
  if ok and type(fs) == 'table' and type(fs.list) == 'function' then
    local ok_list, items = pcall(fs.list, base)
    if ok_list and type(items) == 'table' and #items > 0 then
      listed = true
      for _, item in ipairs(items) do
        if item.is_directory then consider(tostring(item.name)) end
      end
    end
  end
  if not listed then
    -- Used by the test harness and by any Lumen without the fs shim.
    local pipe = io.popen("ls -1 '" .. base .. "' 2>/dev/null")
    if pipe then
      for line in pipe:lines() do consider(line) end
      pipe:close()
    end
  end
  -- The shell entry point must load before the feature add-ons so it can claim
  -- the menubar and set the flag they check before drawing their own floating
  -- panels; plain alphabetical order would put "compressor" before "luna".
  table.sort(names, function(a, b)
    if a == 'luna' then return b ~= 'luna' end
    if b == 'luna' then return false end
    return a < b
  end)
  return names
end

-- Exposed so the test harness and `luna status` can show what would load.
M.discover = addon_names

function M.register(registry)
  for _, name in ipairs(addon_names()) do
    local root = base .. name .. '/'
    local ok, plugin = pcall(dofile, root .. 'backend/main.lua')
    if ok and type(plugin) == 'table' and type(plugin.register) == 'function' then
      local registered, err = pcall(plugin.register, registry)
      if registered then
        local ui = read(root .. 'public/panel.js')
        local control = read(root .. 'public/agent.js')
        if ui then M.ui_js[#M.ui_js + 1] = ui end
        if control then M.control_js[#M.control_js + 1] = control end
      else
        io.stderr:write('[sls-addons] ' .. name .. ' register failed: ' .. tostring(err) .. '\n')
      end
    else
      io.stderr:write('[sls-addons] ' .. name .. ' failed to load: ' .. tostring(plugin) .. '\n')
    end
  end
end
return M
