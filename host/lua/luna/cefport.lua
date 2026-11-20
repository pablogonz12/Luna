-- Resolve the Steam CEF remote-debugging port from the contract file.
--   $LUNA_HOME/cef_port, then legacy ~/.local/share/Lumen/cef_port, then 8080.
-- Format: line 1 = port; optional line 2 = "owner <pid> <starttime_ticks>".
-- An owner line is only trusted when /proc/<pid>/stat field 22 still matches.
local M = {}
M.DEFAULT_PORT = 8080
M.MIN_PORT, M.MAX_PORT = 1024, 65535

local function read_all(path)
  local f = io.open(path, "rb")
  if not f then return nil end
  local s = f:read("*a")
  f:close()
  return s
end

-- Field 22 of /proc/<pid>/stat. comm may contain spaces/parens, so split after the last ')'.
function M.starttime(pid, proc_root)
  local stat = read_all((proc_root or "/proc") .. "/" .. tostring(pid) .. "/stat")
  if not stat then return nil end
  local rest = stat:match("^.*%)%s+(.*)$")
  if not rest then return nil end
  local n = 0
  for field in rest:gmatch("%S+") do
    n = n + 1
    if n == 20 then return tonumber(field) end   -- stat field 22 (state is field 3)
  end
  return nil
end

-- Parse file text. Returns {port=, owner_pid=, owner_start=} or nil,reason.
function M.parse(text)
  if type(text) ~= "string" then return nil, "empty" end
  local line1, rest = text:match("^%s*([^\r\n]*)[\r\n]*(.*)$")
  local port = tonumber(line1 and line1:match("^%s*(%d+)%s*$"))
  if not port then return nil, "bad port line" end
  if port < M.MIN_PORT or port > M.MAX_PORT then return nil, "port out of range" end
  local res = { port = port }
  for line in (rest or ""):gmatch("[^\r\n]+") do
    local pid, start = line:match("^%s*owner%s+(%d+)%s+(%d+)%s*$")
    if pid then res.owner_pid, res.owner_start = tonumber(pid), tonumber(start); break end
  end
  return res
end

local function try_file(path, proc_root)
  local text = read_all(path)
  if not text then return nil, "missing" end
  local res, why = M.parse(text)
  if not res then return nil, why end
  if res.owner_pid then
    local st = M.starttime(res.owner_pid, proc_root)
    if not st or st ~= res.owner_start then return nil, "stale owner" end
  end
  return res.port
end

function M.candidates(opts)
  opts = opts or {}
  local home = opts.home or os.getenv("HOME") or ""
  local luna_home = opts.luna_home or os.getenv("LUNA_HOME")
  if not luna_home or luna_home == "" then luna_home = home .. "/.local/share/Luna" end
  return {
    { path = luna_home .. "/cef_port", source = "luna" },
    { path = home .. "/.local/share/Lumen/cef_port", source = "legacy" },
  }
end

-- opts: luna_home, home, proc_root. Returns port, source, notes(table of strings).
function M.read(opts)
  opts = opts or {}
  local notes = {}
  for _, c in ipairs(M.candidates(opts)) do
    local port, why = try_file(c.path, opts.proc_root)
    if port then return port, c.source, notes end
    if why ~= "missing" then notes[#notes + 1] = c.path .. ": " .. why end
  end
  return M.DEFAULT_PORT, "default", notes
end

return M
