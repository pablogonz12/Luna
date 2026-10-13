-- SLS Compressor backend extension for swwayps/lumen.
-- Runs within the existing Lumen process; no extra network listener. Wraps the
-- btrfs-game-compressor CLI: a read-only status scan, plus long passes that keep
-- running detached while this RPC surface only watches their pid and a bounded
-- tail of their log. The safe knobs (ZSTD compression, deduplication, exclusions,
-- a size ceiling) live here and are mirrored into the tool's own config files, so
-- the TUI and every pass agree. Beta asset optimization is off by default and
-- never runs unattended from a batch.
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
local SETTINGS = DIR .. '/settings.json'
-- The tool's own configuration, one file per knob (see its --help FILES).
local TOOL_DIR = HOME .. '/.config/btrfs-game-compressor'

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

-- Fixed, no-argument operations. `run` is composed from the saved settings at
-- call time; `preview` stays read-only; `assets` is the alpha inventory.
local ACTIONS = {
  scan = { args = '--status --json', scan = true },
  preview = { args = '--dry-run --pending' },
  run = { args = '--run' },
  assets = { args = '--assets-all' },
}

-- The alpha resolution profile set, exactly as the tool names it. `native`
-- (no asset changes) is represented by the feature toggle being off.
local PROFILES = {
  ['ultra-performance'] = 1, performance = 1, balanced = 1,
  quality = 1, ['ultra-quality'] = 1, lossless = 1,
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

local function dirname(path)
  return tostring(path):match('^(.*)/[^/]+$') or '.'
end

-- Writes through a temp file so a reader never sees a half-written report.
local function write_text(path, text)
  fs.create_directories(dirname(path))
  local tmp = path .. '.tmp'
  local f = io.open(tmp, 'wb')
  if not f then return false end
  local written = f:write(text)
  local closed = f:close()
  if not written or not closed then os.remove(tmp); return false end
  if not os.rename(tmp, path) then os.remove(tmp); return false end
  return true
end

local function strip_ansi(text)
  return (tostring(text):gsub('\27%[[0-9;?]*[%a]', ''))
end

-- Up to `lines` log lines from the end of a file, so a multi-megabyte pass
-- cannot flood one RPC response. Progress lines are redrawn with carriage
-- returns; the terminal shows the last rewrite, so only that is kept.
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
  for raw in data:gmatch('[^\n]+') do
    local line = strip_ansi(raw)
    -- A carriage return rewinds to column 0, so everything before the last
    -- rewrite on the line is invisible to a terminal; keep the last non-empty
    -- segment (a line ending in '\r' still shows the text before it).
    local segments = {}
    for segment in (line .. '\r'):gmatch('([^\r]*)\r') do segments[#segments + 1] = segment end
    line = ''
    for index = #segments, 1, -1 do
      if segments[index] ~= '' then line = segments[index]; break end
    end
    out[#out + 1] = line
  end
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

-- ---------------------------------------------------------------------------
-- Settings: the source of truth is our own file; the tool's config files are a
-- mirror written on every save so its TUI and a manual CLI run see the same
-- policy. Nothing here is ever free-form: game names are one line each.

local function valid_game_name(value)
  return type(value) == 'string' and #value > 0 and #value <= 255
    and value:find('[\r\n/]') == nil and value ~= '.' and value ~= '..'
end

local function normalize_languages(raw)
  local seen, list = {}, {}
  for token in tostring(raw or ''):gmatch('[^,%s]+') do
    local code = token:lower()
    if code:match('^[a-z0-9%-]+$') and not seen[code] then
      seen[code] = true
      list[#list + 1] = code
    end
  end
  table.sort(list)
  return table.concat(list, ',')
end

local function default_settings()
  return {
    version = 1,
    compress = true,
    dedupe = true,
    eco = false,
    assets = false,
    assetProfile = 'balanced',
    keepLanguages = '',
    maxSizeGb = 0,
    excluded = {},
  }
end

local function read_settings()
  local data = default_settings()
  local raw = read_text(SETTINGS)
  if not raw then return data end
  local ok, value = pcall(json.decode, raw)
  if not ok or type(value) ~= 'table' then return data end
  if type(value.compress) == 'boolean' then data.compress = value.compress end
  if type(value.dedupe) == 'boolean' then data.dedupe = value.dedupe end
  if type(value.eco) == 'boolean' then data.eco = value.eco end
  if type(value.assets) == 'boolean' then data.assets = value.assets end
  if PROFILES[value.assetProfile] then data.assetProfile = value.assetProfile end
  data.keepLanguages = normalize_languages(value.keepLanguages)
  local size = tonumber(value.maxSizeGb)
  if size and size >= 0 and size <= 100000 then
    data.maxSizeGb = math.floor(size * 1000 + 0.5) / 1000
  end
  for _, name in ipairs(type(value.excluded) == 'table' and value.excluded or {}) do
    if valid_game_name(name) and #data.excluded < 4096 then
      data.excluded[#data.excluded + 1] = name
    end
  end
  return data
end

-- Mirror the knobs the tool owns. Assets off pins visual_target to native, so
-- no pass and no TUI action can change game assets while the toggle is off.
local function save_settings(data)
  if not write_text(SETTINGS, json.encode(data)) then return false end
  fs.create_directories(TOOL_DIR)
  write_text(TOOL_DIR .. '/throttle.conf', data.eco and '1\n' or '0\n')
  write_text(TOOL_DIR .. '/visual_target', (data.assets and data.assetProfile or 'native') .. '\n')
  write_text(TOOL_DIR .. '/max_size_mib', tostring(math.floor(data.maxSizeGb * 1024 + 0.5)) .. '\n')
  write_text(TOOL_DIR .. '/keep_languages', data.keepLanguages .. '\n')
  write_text(TOOL_DIR .. '/exclude_games',
    (#data.excluded > 0 and table.concat(data.excluded, '\n') .. '\n' or ''))
  return true
end

-- What the current settings would actually do, straight from the last scan.
local function build_plan(status, settings)
  local plan = { total = 0, pending = 0, compacted = 0, running = 0,
    excluded = 0, overSize = 0, compress = 0, dedupe = 0, blocked = 0 }
  local blocked = {}
  for _, name in ipairs(settings.excluded) do blocked[name] = true end
  local ceiling = tonumber(settings.maxSizeGb) or 0
  local ceiling_bytes = ceiling > 0 and math.floor(ceiling * 1024 * 1024 * 1024) or 0
  for _, game in ipairs((status and status.games) or {}) do
    plan.total = plan.total + 1
    local state = tostring(game.status or '')
    local size = tonumber(game.size_bytes) or 0
    if state == 'COMPACTED' then
      plan.compacted = plan.compacted + 1
    elseif state == 'RUNNING' then
      plan.running = plan.running + 1
    else
      plan.pending = plan.pending + 1
      local excluded = blocked[game.name] == true
      local over = ceiling_bytes > 0 and size >= ceiling_bytes
      if excluded then plan.excluded = plan.excluded + 1 end
      if over then plan.overSize = plan.overSize + 1 end
      if excluded or over then
        plan.blocked = plan.blocked + 1
      elseif state == 'UNCOMPRESSED' or state == 'UPDATED' then
        plan.compress = plan.compress + 1
      elseif state == 'COMPRESSED' then
        plan.dedupe = plan.dedupe + 1
      elseif state == 'LOW YIELD' and game.dedupe_pending == true then
        plan.dedupe = plan.dedupe + 1
      end
    end
  end
  return plan
end

-- The exact command for the current settings.
local function run_args(settings)
  if settings.compress and settings.dedupe then return '--run' end
  if settings.compress then return '--run --no-dedupe' end
  if settings.dedupe then return '--dedupe' end
  return nil
end

-- ---------------------------------------------------------------------------
-- Pending-job bookkeeping.

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
local function spawn(tool, action, args)
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
    shq(pidfile), shq(tool), args or spec.args, redirect)
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
    if job.action == 'scan' then adopt_status() end
  end
  if running then finishedAt = 0 end
  return {
    action = job.action, pid = job.pid, running = running,
    startedAt = job.startedAt, finishedAt = finishedAt,
    elapsed = running and (os.time() - job.startedAt) or 0,
    log = tail(JOB_LOG, 40, 65536),
  }
end

-- The wire shape for settings: `excluded` must be a real JSON array, or an
-- empty list arrives as {} and a full one as {"1":..,"2":..}.
local function encode_settings(data)
  return {
    version = data.version,
    compress = data.compress,
    dedupe = data.dedupe,
    eco = data.eco,
    assets = data.assets,
    assetProfile = data.assetProfile,
    keepLanguages = data.keepLanguages,
    maxSizeGb = data.maxSizeGb,
    excluded = json.array(data.excluded),
  }
end

local function response(o) return json.encode(o) end

function M.register(registry)
  local settings = read_settings()

  local function scan_state()
    local status = nil
    local raw = read_text(STATUS)
    if raw then
      local ok, value = pcall(json.decode, raw)
      if ok and type(value) == 'table' then status = value end
    end
    return status
  end

  registry.SlsCompressorGet = function()
    local tool = find_tool()
    -- Settings can change on disk (a hand edit, another surface), so the file
    -- is the source of truth on every read.
    settings = read_settings()
    -- job_state() first: observing a finished scan adopts its report, which is
    -- what this same response must already include.
    local job = job_state()
    local status = scan_state()
    local statusAt = tonumber(read_text(STATUS_AT) or '') or 0
    -- The first open starts the read-only scan itself; a recent failed attempt
    -- is not retried in a loop, and a running job is always left alone.
    local autoStarted = false
    if tool and not (job and job.running) and status == nil then
      local attempt = tonumber(read_text(ATTEMPT_AT) or '') or 0
      if os.time() - attempt > 600 then
        local pid = spawn(tool, 'scan', ACTIONS.scan.args)
        if pid then
          autoStarted = true
          job = job_state()
        end
      end
    end
    return response({
      success = true,
      tool = {
        found = tool ~= nil,
        path = tool or '',
        version = status and tostring(status.version or '') or '',
      },
      status = status,
      statusAt = statusAt,
      settings = encode_settings(settings),
      plan = build_plan(status, settings),
      job = job,
      autoStarted = autoStarted,
    })
  end

  registry.SlsCompressorPutSettings = function(raw)
    if type(raw) ~= 'string' or #raw > 200000 then
      return response({success = false, error = 'Invalid payload'})
    end
    local ok, obj = pcall(json.decode, raw)
    if not ok or type(obj) ~= 'table' then
      return response({success = false, error = 'Invalid settings'})
    end
    local next_settings = default_settings()
    for _, key in ipairs({'compress', 'dedupe', 'eco', 'assets'}) do
      if type(obj[key]) == 'boolean' then next_settings[key] = obj[key] end
    end
    if PROFILES[obj.assetProfile] then next_settings.assetProfile = obj.assetProfile end
    next_settings.keepLanguages = normalize_languages(obj.keepLanguages)
    local size = tonumber(obj.maxSizeGb)
    if size and size >= 0 and size <= 100000 then
      next_settings.maxSizeGb = math.floor(size * 1000 + 0.5) / 1000
    end
    local seen = {}
    for _, name in ipairs(type(obj.excluded) == 'table' and obj.excluded or {}) do
      if valid_game_name(name) and not seen[name] and #next_settings.excluded < 4096 then
        seen[name] = true
        next_settings.excluded[#next_settings.excluded + 1] = name
      end
    end
    table.sort(next_settings.excluded)
    if not save_settings(next_settings) then
      return response({success = false, error = 'Could not save settings'})
    end
    settings = next_settings
    return response({success = true, settings = encode_settings(settings)})
  end

  registry.SlsCompressorRun = function(raw)
    if type(raw) ~= 'string' or #raw > 1000 then
      return response({success = false, error = 'Invalid payload'})
    end
    local ok, obj = pcall(json.decode, raw)
    local action = ok and type(obj) == 'table' and tostring(obj.action or '') or ''
    if not ACTIONS[action] then
      return response({success = false, error = 'Unknown action'})
    end
    local args = ACTIONS[action].args
    if action == 'run' then
      args = run_args(settings)
      if not args then
        return response({success = false, error = 'Nothing is enabled: turn on compression or deduplication first'})
      end
    end
    local job = job_state()
    if job and job.running then
      return response({success = false, error = 'A job is already running'})
    end
    local tool = find_tool()
    if not tool then
      return response({success = false, error = 'btrfs-game-compressor was not found'})
    end
    local pid = spawn(tool, action, args)
    if not pid then
      return response({success = false, error = 'Could not start the tool'})
    end
    return response({
      success = true,
      job = {action = action, pid = pid, running = true, startedAt = os.time(), args = args},
    })
  end

  -- Best effort, and deliberately terminal: a compaction is checkpointed and
  -- resumable, so a stopped pass loses at most the current game's progress.
  registry.SlsCompressorCancel = function()
    local job = job_state()
    if not job or not job.running then
      return response({success = false, error = 'No job is running'})
    end
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
M.normalize_languages = normalize_languages
M.binary_candidates = CANDIDATES
return M
