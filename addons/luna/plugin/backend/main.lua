-- SLS Luna shell add-on backend.
--
-- The menu itself lives in public/panel.js and talks to the Playtime and
-- Presence backends directly through the shared registry. This backend exists so
-- the add-on is a real, discoverable add-on (the bridge loads a directory only
-- when it has one) and so the About tab can report what is actually installed
-- instead of guessing from the menu's own state.
local fs = require('fs')
local json = require('json')
local M = {}
local VERSION = '0.1.0'
local home = os.getenv('HOME') or ''
local base = home .. '/.local/share/Lumen/sls-addons/'

local function installed()
  local names = {}
  local ok, items = pcall(fs.list, base)
  if ok and type(items) == 'table' then
    for _, item in ipairs(items) do
      local name = tostring(item.name or '')
      if item.is_directory and name ~= '' and name:sub(1, 1) ~= '.' then
        -- Only directories that actually carry a payload.
        local probe = io.open(base .. name .. '/backend/main.lua', 'r')
        if probe then probe:close(); names[#names + 1] = name end
      end
    end
  end
  table.sort(names)
  return names
end

function M.register(registry)
  registry.SlsLunaInfo = function()
    return json.encode({success = true, version = VERSION, addons = installed(),
      data = home .. '/.local/share/slssteam-integrations'})
  end
end
return M
