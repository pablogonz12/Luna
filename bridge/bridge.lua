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
-- Same file under the lumen host and under luna-host: Luna's state dir does not move with
-- the host. `luna doctor` / `luna status` read it to tell "Lumen runs" from "add-ons attached".
local luna_home = os.getenv('LUNA_HOME')
if not luna_home or luna_home == '' then luna_home = home .. '/.local/share/Luna' end
M.heartbeat_path = luna_home .. '/state/bridge-heartbeat.json'

local function read(path)
  local f = io.open(path, 'rb')
  if not f then return nil end
  local value = f:read('*a'); f:close(); return value
end

-- Directory listing without shell quoting risk: LuaFileSystem when the runtime
-- has it, otherwise `ls` with a POSIX single-quoted argument, so a HOME with a
-- quote or a space cannot change the command. Returns a plain name list.
local function quote_sh(value)
  return "'" .. tostring(value):gsub("'", "'\\''") .. "'"
end

local function list_dir(path)
  local names = {}
  local ok_lfs, lfs = pcall(require, 'lfs')
  if ok_lfs and type(lfs) == 'table' and type(lfs.dir) == 'function' then
    local ok_dir, iterator, state = pcall(lfs.dir, path)
    if ok_dir and iterator then
      for entry in iterator, state do names[#names + 1] = entry end
      return names
    end
  end
  local pipe = io.popen('ls -1 ' .. quote_sh(path) .. ' 2>/dev/null')
  if pipe then
    for line in pipe:lines() do names[#names + 1] = line end
    pipe:close()
  end
  return names
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
    for _, name in ipairs(list_dir(base)) do consider(name) end
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

-- Two moments matter: this file is loaded when the host boots (`time`/`pid`), which
-- can be minutes before Steam exists, and Steam calls the first add-on RPC
-- (`attachedAt`), the only evidence available here that it serves a live session.
-- `lastSeen` follows the attach at most once a minute. All of it is additive: an
-- older CLI reads `time`, and an older bridge that writes no `attachedAt` is read
-- as attached when its pid is alive, so neither version breaks the other.
local beat = { at = 0, pid = 0, attached = 0, seen = 0, written = 0, loaded = {}, failed = {} }

local function json_string(value)
  return '"' .. tostring(value):gsub('[%c"\\]', function(ch)
    return string.format('\\u%04x', ch:byte())
  end) .. '"'
end

-- Best effort and never fatal: no shell-out, and a missing state dir or a read-only disk
-- must not stop add-ons from loading (the whole call is under pcall).
local function write_heartbeat()
  local fields = {}
  -- Additive marker: without it a file is from a bridge that only knew its load
  -- time, and a new CLI must keep reading that the way it always did.
  fields[#fields + 1] = '"format":2'
  fields[#fields + 1] = string.format('"time":%d', beat.at)
  fields[#fields + 1] = string.format('"pid":%d', beat.pid)
  if beat.attached > 0 then
    fields[#fields + 1] = string.format('"attachedAt":%d', beat.attached)
  end
  if beat.seen > 0 then
    fields[#fields + 1] = string.format('"lastSeen":%d', beat.seen)
  end
  local parts = {}
  for _, name in ipairs(beat.loaded) do parts[#parts + 1] = json_string(name) end
  local errors = {}
  for _, item in ipairs(beat.failed) do
    errors[#errors + 1] = '{"name":' .. json_string(item.name) .. ',"error":' .. json_string(item.error) .. '}'
  end
  fields[#fields + 1] = '"addons":[' .. table.concat(parts, ',') .. ']'
  fields[#fields + 1] = '"failed":[' .. table.concat(errors, ',') .. ']'
  local now = os.time()
  if beat.at <= 0 then beat.at = now end
  local handle = io.open(M.heartbeat_path .. '.tmp', 'wb')
  if not handle then return end
  handle:write('{' .. table.concat(fields, ',') .. '}\n')
  handle:close()
  os.rename(M.heartbeat_path .. '.tmp', M.heartbeat_path)
  beat.written = now
end

-- First RPC served: Steam is driving the add-ons, so stamp the attach and then
-- refresh the file at most once a minute (cheap, and never on every call).
function M.attach()
  local now = os.time()
  local fresh = beat.attached <= 0
  if fresh then beat.attached = now end
  beat.seen = now
  if fresh or now - beat.written >= 60 then pcall(write_heartbeat) end
  return M
end

function M.register(registry)
  local loaded, failed = {}, {}
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
        loaded[#loaded + 1] = name
      else
        failed[#failed + 1] = { name = name, error = 'register failed: ' .. tostring(err) }
        io.stderr:write('[sls-addons] ' .. name .. ' register failed: ' .. tostring(err) .. '\n')
      end
    else
      failed[#failed + 1] = { name = name, error = 'failed to load: ' .. tostring(plugin) }
      io.stderr:write('[sls-addons] ' .. name .. ' failed to load: ' .. tostring(plugin) .. '\n')
    end
  end
  local pid = 0
  local stat = read('/proc/self/stat')
  if stat then pid = tonumber(stat:match('^(%d+)')) or 0 end
  beat.at, beat.pid, beat.loaded, beat.failed = os.time(), pid, loaded, failed
  pcall(write_heartbeat)
  -- Any add-on call proves Steam is attached, so the first one stamps it.
  for key, value in pairs(registry) do
    if type(value) == 'function' then
      registry[key] = function(...)
        M.attach()
        return value(...)
      end
    end
  end
end
return M
