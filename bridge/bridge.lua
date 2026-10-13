-- SLS integrations bridge for swwayps/lumen (does not replace LuaTools).
-- Only loads the two explicitly named and installed add-ons.
local M = { ui_js = {}, control_js = {} }
local home = os.getenv('HOME') or ''
local base = home .. '/.local/share/Lumen/sls-addons/'
local function read(path)
  local f = io.open(path, 'rb')
  if not f then return nil end
  local value = f:read('*a'); f:close(); return value
end
function M.register(registry)
  for _, name in ipairs({'presence', 'playtime'}) do
    local root = base .. name .. '/'
    if read(root .. 'backend/main.lua') then
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
end
return M
