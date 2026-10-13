-- SLS Compressor backend extension for swwayps/lumen.
-- Runs within the existing Lumen process; no extra network listener. Wraps the
-- btrfs-game-compressor CLI: a read-only status scan, and long library passes
-- that keep running detached while this RPC surface only watches their pid and
-- a bounded tail of their log. Nothing changes without an explicit action.
local json = require('json')
local fs = require('fs')
local M = {}
local HOME = os.getenv('HOME') or ''
local DIR = HOME .. '/.local/share/slssteam-integrations/compressor'
local STATUS = DIR .. '/status.json'
local STATUS_TMP = DIR .. '/status.tmp'
local STATUS_AT = DIR .. '/status.at'
local ATTEMPT_AT = DIR .. '/attempt.at'
local JOB_FILE = DIR .. '/job.json'
local JOB_DONE = DIR .. '/job.done'
local JOB_LOG = DIR .. '/last.log'

-- Where the CLI may live, in preference order: the env override is the test
-- seam, then an installed copy, then a source checkout (bgc-native has to sit
-- beside the script, which a checkout keeps). Kept dense: a nil first entry
-- would stop ipairs dead.
local CANDIDATES = {}
local function candidate(path)
  if type(path) == 'string' and path ~= '' then CANDIDATES[#CANDIDATES + 1] = path end
end
candidate(os.getenv('SLS_COMPRESSOR_BIN'))
candidate(HOME .. '/.local/bin/btrfs-game-compressor')
candidate('/usr/local/bin/btrfs-game-compressor')
candidate('/usr/bin/btrfs-game-compressor')
candidate(HOME .. '/Documents/Scripts/btrfs-game-compressor/btrfs-game-compressor')

-- The whole action surface and nothing else. status is read-only; compact is
-- the checkpointed pass that is irreversible and can run for hours.
local ACTIONS = {
  status = { args = '--status --json', scan = true },
  dryrun = { args = '--dry-run' },
  recheck = { args = '--recheck' },
  dedupe = { args = '--dedupe' },
  compact = { args = '--compact-all-no-backup' },
}

local function shq(value)
  return "'" .. tostring(value):gsub("'", "'\\''") .. "'"
end

local function read_text(path)
  local f = io.open(path, 'rb')
  if not f then return nil end
  local data = f:read('*a'); f:close()
  return data
end

-- Writes through a temp file so a reader never sees a half-written report.
local function write_text(path, text)
  fs.create_directories(DIR)
  local tmp = path .. '.tmp'
  local f = io.open(tmp, 'wb')
  if not f then return false end
  local written = f:write(text)
  local closed = f:close()
  if not written or not closed then os.remove(tmp); return false end
  if not os.rename(tmp, path) then os.remove(tmp); return false end
  return true
end

-- Up to `lines` log lines from the end of a file, so a multi-megabyte pass
-- cannot flood one RPC response.
local function tail(path, lines, limit)
  local f = io.open(path, 'rb')
  if not f then return '' end
  local size = f:seek('end') or 0
  local start = size - (limit or 65536)
  if start < 0 then start = 0 end
  f:seek('set', start)
  local data = f:read('*a') or ''
  f:close()
  local out = {}
  for line in data:gmatch('[^\n]+') do out[#out + 1] = line end
  while #out > lines do table.remove(out, 1) end
  return table.concat(out, '\n')
end

local function find_tool()
  for _, path in ipairs(CANDIDATES) do
    if type(path) == 'string' and path ~= '' and fs.exists(path) then return path end
  end
  return nil
end

-- A pid is ours while /proc still shows the wrapper. Anything else (a recycled
-- pid, a vanished process) counts as finished.
local function alive(pid)
  if type(pid) ~= 'number' or pid < 2 then return false end
  local f = io.open('/proc/' .. math.floor(pid) .. '/cmdline', 'rb')
  if not f then return false end
  local data = f:read('*a'); f:close()
  return type(data) == 'string' and data:find('btrfs-game-compressor', 1, true) ~= nil
end

-- The process group of a live pid, so a cancel can reach the stage children
-- without ever signalling Steam. The comm field may contain spaces, hence the
-- parse after the last ')'.
local function process_group(pid)
  local raw = read_text('/proc/' .. math.floor(pid) .. '/stat')
  local rest = raw and raw:match('%)%s*(.+)$')
  if not rest then return nil end
  local fields = {}
  for field in rest:gmatch('%S+') do fields[#fields + 1] = field end
  return tonumber(fields[3]) -- state, ppid, pgrp
end

local function read_job()
  local raw = read_text(JOB_FILE)
  if not raw then return nil end
  local ok, value = pcall(json.decode, raw)
  if not ok or type(value) ~= 'table' then return nil end
  local pid = tonumber(value.pid)
  local action = type(value.action) == 'string' and value.action or ''
  if not pid or pid < 2 or pid ~= math.floor(pid) or not ACTIONS[action] then return nil end
  return { action = action, pid = pid, startedAt = tonumber(value.startedAt) or 0 }
end

-- Launches detached so a pass survives Steam and Lumen both going away. The
-- tool's own shell records $$ in a pid file before exec, which stays correct
-- even in the rare case where setsid has to fork, and it leads its own process
-- group so a cancel can take the whole pass down.
local function spawn(tool, action)
  local spec = ACTIONS[action]
  fs.create_directories(DIR)
  os.remove(JOB_DONE)
  write_text(JOB_LOG, '')
  if spec.scan then os.remove(STATUS_TMP) end
  local pidfile = DIR .. '/job.pid'
  os.remove(pidfile)
  local redirect = spec.scan
    and ('> ' .. shq(STATUS_TMP) .. ' 2>> ' .. shq(JOB_LOG))
    or ('> ' .. shq(JOB_LOG) .. ' 2>&1')
  local inner = string.format('echo $$ > %s; exec nohup %s %s %s < /dev/null',
    shq(pidfile), shq(tool), spec.args, redirect)
  local outer = string.format('setsid sh -c %s < /dev/null > /dev/null 2>&1 &', shq(inner))
  os.execute(outer)
  local pid
  for _ = 1, 20 do
    local raw = read_text(pidfile)
    pid = raw and tonumber(raw:match('(%d+)'))
    if pid then break end
    os.execute('sleep 0.05')
  end
  os.remove(pidfile)
  if not pid then return nil end
  write_text(JOB_FILE, json.encode({ action = action, pid = pid, startedAt = os.time() }))
  if spec.scan then write_text(ATTEMPT_AT, tostring(os.time())) end
  return pid
end

-- A finished scan only counts once its whole JSON landed; a scan that was
-- cancelled or crashed leaves nothing to adopt.
local function adopt_status()
  if not fs.exists(STATUS_TMP) then return end
  local raw = read_text(STATUS_TMP)
  os.remove(STATUS_TMP)
  if type(raw) ~= 'string' then return end
  local trimmed = raw:gsub('%s+$', '')
  if #trimmed < 16 or trimmed:sub(1, 1) ~= '{' or trimmed:sub(-1) ~= '}' then return end
  if write_text(STATUS, trimmed) then write_text(STATUS_AT, tostring(os.time())) end
end

local function job_state()
  local job = read_job()
  if not job then return nil end
  local running = alive(job.pid)
  local finishedAt = tonumber(read_text(JOB_DONE) or '') or 0
  if not running and finishedAt == 0 then
    finishedAt = os.time()
    write_text(JOB_DONE, tostring(finishedAt))
    if job.action == 'status' then adopt_status() end
  end
  if running then finishedAt = 0 end
  return {
    action = job.action, pid = job.pid, running = running,
    startedAt = job.startedAt, finishedAt = finishedAt,
    elapsed = running and (os.time() - job.startedAt) or 0,
    log = tail(JOB_LOG, 40, 65536),
  }
end

local function response(o) return json.encode(o) end

function M.register(registry)
  registry.SlsCompressorGet = function()
    local tool = find_tool()
    -- job_state() first: observing a finished scan adopts its report, which is
    -- what this same response must already include.
    local job = job_state()
    local status = nil
    local raw = read_text(STATUS)
    if raw then
      local ok, value = pcall(json.decode, raw)
      if ok and type(value) == 'table' then status = value end
    end
    local statusAt = tonumber(read_text(STATUS_AT) or '') or 0
    -- The first open starts the read-only scan itself; a recent failed attempt
    -- is not retried in a loop, and a running job is always left alone.
    local autoStarted = false
    if tool and not (job and job.running) and status == nil then
      local attempt = tonumber(read_text(ATTEMPT_AT) or '') or 0
      if os.time() - attempt > 600 then
        local pid = spawn(tool, 'status')
        if pid then
          autoStarted = true
          job = job_state()
        end
      end
    end
    return response({
      success = true,
      tool = { found = tool ~= nil, path = tool or '' },
      status = status,
      statusAt = statusAt,
      job = job,
      autoStarted = autoStarted,
    })
  end
  registry.SlsCompressorRun = function(raw)
    if type(raw) ~= 'string' or #raw > 1000 then return response({success = false, error = 'Invalid payload'}) end
    local ok, obj = pcall(json.decode, raw)
    local action = ok and type(obj) == 'table' and tostring(obj.action or '') or ''
    if not ACTIONS[action] then return response({success = false, error = 'Unknown action'}) end
    local job = job_state()
    if job and job.running then return response({success = false, error = 'A job is already running'}) end
    local tool = find_tool()
    if not tool then return response({success = false, error = 'btrfs-game-compressor was not found'}) end
    local pid = spawn(tool, action)
    if not pid then return response({success = false, error = 'Could not start the tool'}) end
    return response({success = true, job = {action = action, pid = pid, running = true, startedAt = os.time()}})
  end
  -- Best effort, and deliberately terminal: a compaction is checkpointed and
  -- resumable, so a stopped pass loses at most the current game's progress.
  registry.SlsCompressorCancel = function()
    local job = job_state()
    if not job or not job.running then return response({success = false, error = 'No job is running'}) end
    local group = process_group(job.pid)
    if group and group == job.pid then
      os.execute('kill -TERM -- -' .. math.floor(job.pid))
    else
      os.execute('kill -TERM ' .. math.floor(job.pid))
    end
    return response({success = true, cancelled = true, pid = job.pid})
  end
end
M.tail = tail
M.binary_candidates = CANDIDATES
return M
