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
-- One-game-at-a-time queue: the backend writes a list and a driver script, the
-- driver runs `--compact` per game and appends state, and the UI reads both.
local QUEUE_LIST = DIR .. '/queue.list'
local QUEUE_STATE = DIR .. '/queue.state'
local QUEUE_LOG = DIR .. '/run.log'
-- run.log grew to 77 MB and was never rotated. Past this size the file is kept
-- as run.log.1 (one previous pass) instead of being truncated, and only when no
-- pass is live: a rotation mid-run would pull the log out from under it.
local LOG_PREV = QUEUE_LOG .. '.1'
local LOG_LIMIT = 8 * 1048576
local DRIVER = DIR .. '/run-queue.sh'
-- The tool's own configuration, one file per knob (see its --help FILES).
local TOOL_DIR = HOME .. '/.config/btrfs-game-compressor'
-- The tool's own ledgers, read-only, powering the Stats tab: one line per game
-- with its measured sizes, and one measurement per dedupe pass per directory.
-- Both carry a '#' version header, and rows that do not match the expected
-- shape are skipped, so a future format degrades to missing numbers instead of
-- a broken page.
local TOOL_STATE_DIR = HOME .. '/.local/state/btrfs-game-compressor'
local TOOL_STATE_FILE = TOOL_STATE_DIR .. '/compressed_games.db'
local TOOL_DEDUPE_HISTORY = TOOL_STATE_DIR .. '/dedupe_physical_history.db'

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
  -- Remeasures the installed games and refreshes the tool's own saved-size
  -- ledger; it never recompresses anything. This is what fills the Stats tab.
  recheck = { args = '--recheck' },
  run = { args = '--run' },
  assets = { args = '--assets-all' },
}

-- The alpha resolution profile set, exactly as the tool names it. `native`
-- (no asset changes) is represented by the feature toggle being off.
local PROFILES = {
  ['ultra-performance'] = 1, performance = 1, balanced = 1,
  quality = 1, ['ultra-quality'] = 1, lossless = 1,
}

-- How the per-game queue is ordered. Smallest first is the default: a first
-- run then shows completed games almost immediately.
local RUN_ORDERS = { smallest = 1, largest = 1, library = 1 }

local function shq(value)
  return "'" .. tostring(value):gsub("'", "'\\''") .. "'"
end

local function read_text(path)
  local f = io.open(path, 'rb')
  if not f then return nil end
  local data = f:read('*a'); f:close()
  return data
end

-- ---------------------------------------------------------------- read cache
--
-- SlsCompressorGet decodes the tool's report, tails up to 512 KB of the queue
-- log and reads the ledger on every call, and the panel polls it. A settled
-- answer is therefore memoised on the stamps (size, and modification time when
-- lfs is there) of every file below it was built from; a change on disk
-- invalidates it at once and a short TTL bounds a same-size rewrite. The wire
-- string is cached, so a hit does no decode, no log read and no encode.
local GET_CACHE_TTL = 15

local lfs = select(2, pcall(require, 'lfs'))
if type(lfs) ~= 'table' or type(lfs.attributes) ~= 'function' then lfs = nil end
-- With no stat source a cache cannot notice a change, so it stays off.
local has_stat = lfs ~= nil or type(fs.size) == 'function'

-- The size of a file in bytes, from whatever stat source exists (a missing file
-- is zero). Used for the pass log and its rotation.
local function file_bytes(path)
  if lfs then
    local ok, attr = pcall(lfs.attributes, path)
    if ok and type(attr) == 'table' then return tonumber(attr.size) or 0 end
  end
  if type(fs.size) == 'function' then
    local ok, size = pcall(fs.size, path)
    if ok and size ~= nil then return tonumber(size) or 0 end
  end
  local f = io.open(path, 'rb')
  if not f then return 0 end
  local size = f:seek('end') or 0
  f:close()
  return size
end

local function file_stamp(path)
  if lfs then
    local ok, attr = pcall(lfs.attributes, path)
    if ok and type(attr) == 'table' then
      return tostring(attr.size or 0) .. ':' .. tostring(attr.modification or 0)
    end
    return 'missing'
  end
  local ok, size = pcall(fs.size, path)
  if ok and size ~= nil then return tostring(size) end
  return 'missing'
end

local function stamps_rev(paths)
  if not has_stat then return nil end
  local parts = {}
  for index, path in ipairs(paths) do parts[index] = file_stamp(path) end
  return table.concat(parts, '|')
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

-- A pid is ours while /proc still shows the wrapper or the queue driver.
-- Anything else (a recycled pid, a vanished process) counts as finished.
local function alive(pid)
  if type(pid) ~= 'number' or pid < 2 then return false end
  local f = io.open('/proc/' .. math.floor(pid) .. '/cmdline', 'rb')
  if not f then return false end
  local data = f:read('*a'); f:close()
  if type(data) ~= 'string' then return false end
  return data:find('btrfs-game-compressor', 1, true) ~= nil
    or data:find('run-queue.sh', 1, true) ~= nil
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
    runOrder = 'smallest',
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
  if RUN_ORDERS[value.runOrder] then data.runOrder = value.runOrder end
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

-- ---------------------------------------------------------------------------
-- The one-game-at-a-time queue. The list is written game by game so the UI can
-- show a state and a bar per game, and the driver is a small detached shell
-- script so the pass continues even when Steam or Lumen goes away.

-- The queue mirrors the plan: compression targets first, then games whose
-- dedupe pass is outstanding. Running and compacted games are never queued, and
-- the user's filters (exclusions, size ceiling) already applied.
local function build_queue(status, settings)
  local blocked = {}
  for _, name in ipairs(settings.excluded) do blocked[name] = true end
  local ceiling = tonumber(settings.maxSizeGb) or 0
  local ceiling_bytes = ceiling > 0 and math.floor(ceiling * 1024 * 1024 * 1024) or 0
  local queue = {}
  for _, game in ipairs((status and status.games) or {}) do
    local state = tostring(game.status or '')
    local size = tonumber(game.size_bytes) or 0
    local path = tostring(game.path or '')
    local name = tostring(game.name or '')
    local excluded = blocked[name] == true
    local over = ceiling_bytes > 0 and size >= ceiling_bytes
    if not excluded and not over and path ~= '' and name ~= '' then
      local compress = settings.compress and (state == 'UNCOMPRESSED' or state == 'UPDATED')
      local dedupe = settings.dedupe and (state == 'COMPRESSED'
        or (state == 'LOW YIELD' and game.dedupe_pending == true))
      if compress or dedupe then
        queue[#queue + 1] = { name = name, path = path, size = size }
      end
    end
  end
  if settings.runOrder == 'largest' then
    table.sort(queue, function(a, b) return a.size > b.size end)
  elseif settings.runOrder == 'smallest' then
    table.sort(queue, function(a, b) return a.size < b.size end)
  end
  return queue
end

-- ---------------------------------------------------------------- statistics
--
-- The Stats tab reads the compressor's own ledgers instead of re-measuring:
-- nothing here runs the tool, and a missing ledger is simply an empty report.

local MIB = 1048576

-- The tool escapes '%', '|' and control characters in its keys.
local function decode_field(value)
  return (tostring(value):gsub('%%(%x%x)', function(hex)
    local code = tonumber(hex, 16)
    if code == 0x25 then return '%' end
    if code == 0x7C then return '|' end
    if code == 0x09 then return '\t' end
    if code == 0x0A then return '\n' end
    if code == 0x0D then return '\r' end
    return '%' .. hex
  end))
end

local function pipe_fields(line)
  local fields = {}
  for field in (tostring(line) .. '|'):gmatch('([^|]*)|') do fields[#fields + 1] = field end
  return fields
end

-- compressed_games.db, state v2: path|name|stamp|original|on-disk|saved-label|
-- saved MiB|original MiB|on-disk MiB. Only rows the tool has measured (a
-- non-zero original MiB) carry numbers; the rest are records without a size.
function M.parse_state_db(text)
  local records, measured = 0, {}
  for line in tostring(text or ''):gmatch('[^\n]+') do
    if line:sub(1, 1) ~= '#' then
      local fields = pipe_fields(line)
      if #fields >= 9 and fields[1] ~= '' then
        records = records + 1
        local original = tonumber(fields[8]) or 0
        local disk = tonumber(fields[9]) or 0
        if original > 0 then
          measured[#measured + 1] = {
            name = decode_field(fields[2]),
            path = decode_field(fields[1]),
            at = tonumber(fields[3]) or 0,
            original_bytes = original * MIB,
            disk_bytes = disk * MIB,
            saved_bytes = math.max(0, (original - disk) * MIB),
          }
        end
      end
    end
  end
  return { records = records, measured = measured }
end

-- dedupe_physical_history.db: stamp|path|before|...|after|...|saved. The latest
-- measurement per directory wins; earlier passes for the same directory would
-- otherwise count the same reclaimed bytes twice.
function M.parse_dedupe_history(text)
  local by_path, passes, newest = {}, 0, 0
  for line in tostring(text or ''):gmatch('[^\n]+') do
    if line:sub(1, 1) ~= '#' then
      local fields = pipe_fields(line)
      if #fields >= 9 and fields[2] ~= '' then
        passes = passes + 1
        local at = tonumber(fields[1]) or 0
        local current = by_path[fields[2]]
        if not current or at >= current.at then
          by_path[fields[2]] = {
            path = decode_field(fields[2]),
            at = at,
            before_bytes = tonumber(fields[3]) or 0,
            after_bytes = tonumber(fields[7]) or 0,
            saved_bytes = tonumber(fields[9]) or 0,
          }
        end
        if at > newest then newest = at end
      end
    end
  end
  local entries, saved_total = {}, 0
  for _, entry in pairs(by_path) do
    entries[#entries + 1] = entry
    saved_total = saved_total + entry.saved_bytes
  end
  table.sort(entries, function(a, b) return a.saved_bytes > b.saved_bytes end)
  return { passes = passes, directories = #entries, saved_bytes = saved_total,
    newest = newest, entries = entries }
end

local function top_list(list, count)
  local out = {}
  for index = 1, math.min(#list, count) do out[#out + 1] = list[index] end
  return out
end

-- Everything the Stats tab shows, in one read-only pass over small files.
local function build_stats(status, queue)
  local state = M.parse_state_db(read_text(TOOL_STATE_FILE))
  local dedupe = M.parse_dedupe_history(read_text(TOOL_DEDUPE_HISTORY))
  local measured = state.measured
  table.sort(measured, function(a, b) return a.saved_bytes > b.saved_bytes end)
  local original, disk, saved = 0, 0, 0
  for _, entry in ipairs(measured) do
    original = original + entry.original_bytes
    disk = disk + entry.disk_bytes
    saved = saved + entry.saved_bytes
  end
  -- Luna's own passes: the queue driver records the ZSTD bytes each game
  -- reclaimed, which works even when the tool kept no MiB ledger.
  local luna_saved, luna_games = 0, {}
  for _, entry in ipairs((queue and queue.games) or {}) do
    if (entry.saved or 0) > 0 then
      luna_saved = luna_saved + entry.saved
      luna_games[#luna_games + 1] = { name = entry.name, saved_bytes = entry.saved }
    end
  end
  table.sort(luna_games, function(a, b) return a.saved_bytes > b.saved_bytes end)
  -- Library totals come from the last scan: exact sizes per installed game.
  local library_bytes, library_games = 0, 0
  for _, game in ipairs((status and status.games) or {}) do
    local size = tonumber(game.size_bytes) or 0
    if size > 0 then
      library_bytes = library_bytes + size
      library_games = library_games + 1
    end
  end
  return {
    compression = { records = state.records, measured = #measured,
      original_bytes = original, disk_bytes = disk, saved_bytes = saved,
      games = json.array(top_list(measured, 10)) },
    dedupe = { passes = dedupe.passes, directories = dedupe.directories,
      saved_bytes = dedupe.saved_bytes, newest = dedupe.newest,
      entries = json.array(top_list(dedupe.entries, 10)) },
    luna = { measured = #luna_games, saved_bytes = luna_saved,
      games = json.array(top_list(luna_games, 10)) },
    library = { games = library_games, bytes = library_bytes },
  }
end

-- The driver runs one `--compact` per game and appends QUEUE\t<event> lines to
-- the state file; the log holds the tool's own output, including its
-- COMPACT| and PROGRESS| lines.
local function write_driver(tool)
  local template = [[#!/bin/bash
# SLS Luna compressor queue driver: one game at a time, so the panel can show a
# state and a bar per game. Written by the backend; do not edit by hand.
# The log is appended to, never truncated: a RUN marker separates this pass from
# the records of an older one, and rotation is the backend's job.
set -u
DIR=@DIR@
TOOL=@TOOL@
LIST=@LIST@
LOG=@LOG@
STATE=@STATE@
printf 'RUN\t%s\n' "$(date +%s)" >> "$LOG"
total=0
while IFS=$'\t' read -r name path size; do
    [ -n "$path" ] || continue
    total=$((total+1))
done < "$LIST"
i=0
while IFS=$'\t' read -r name path size; do
    [ -n "$path" ] || continue
    i=$((i+1))
    printf 'start\t%s\t%s\t%s\t%s\t%s\n' "$i" "$total" "$size" "$name" "$path" >> "$STATE"
    start=$(stat -c %s "$LOG" 2>/dev/null || echo 0)
    BGC_PROGRESS=1 BGC_PROGRESS_TOTAL="$size" "$TOOL" --compact "$path" --verbose >> "$LOG" 2>&1
    rc=$?
    # This game's own lines only, never the previous game's: the done record is
    # the durable measurement (SAVED_MIB may be the literal unmeasured); a skip,
    # fail or estimate line is kept when there is no done line.
    slice=$(tail -c +$((start+1)) "$LOG" 2>/dev/null)
    record=$(printf '%s\n' "$slice" | grep -a '^COMPACT|done|' | head -n 1)
    [ -n "$record" ] || record=$(printf '%s\n' "$slice" | grep -a '^COMPACT|\(skip\|fail\|estimate\)|' | head -n 1)
    estimate=$(printf '%s\n' "$slice" | grep -a '^COMPACT|estimate|' | head -n 1)
    printf 'done\t%s\t%s\t%s\t%s\t%s\n' "$i" "$total" "$rc" "$record" "$estimate" >> "$STATE"
    # Interrupted, or the library refused before the next game could start:
    # nothing more is attempted, and the remaining games are not failures.
    if [ "$rc" -eq 130 ] || [ "$rc" -eq 143 ]; then
        break
    fi
    if [ "$rc" -eq 5 ] || [ "$rc" -eq 6 ]; then
        break
    fi
done < "$LIST"
printf 'finish\t%s\t%s\n' "$i" "$total" >> "$STATE"
]]
  local script = template:gsub('@(%u+)@', {
    DIR = shq(DIR), TOOL = shq(tool), LIST = shq(QUEUE_LIST),
    LOG = shq(QUEUE_LOG), STATE = shq(QUEUE_STATE),
  })
  if not write_text(DRIVER, script) then return false end
  os.execute('chmod +x ' .. shq(DRIVER))
  return true
end

local function read_tail_bytes(path, bytes, floor_offset)
  local f = io.open(path, 'rb')
  if not f then return '' end
  local size = f:seek('end') or 0
  local start = size - (bytes or 262144)
  if start < 0 then start = 0 end
  start = math.max(start, floor_offset or 0)
  f:seek('set', start)
  local data = f:read('*a') or ''
  f:close()
  return data
end

local function split_tabs(line)
  local out = {}
  for field in (line .. '\t'):gmatch('([^\t]*)\t') do out[#out + 1] = field end
  return out
end

-- ------------------------------------------------------------------ outcomes
--
-- The engine's machine lines carry the only honest per-game outcome. Plain
-- words live here so every surface (the Compressor page, its standalone panel)
-- shows the same sentence, and a library-level refusal is never counted as a
-- pile of failed games.
local SKIP_TEXT = {
  running = 'the game is running',
  nodatacow = 'NODATACOW is set, so it is never compressed',
  busy = 'written in the last few minutes; it is retried once it is quiet',
  ['unsafe-path'] = 'the path is not safe to rewrite',
  ['escapes-library'] = 'it resolves outside the library',
  ['already-compacted'] = 'already compacted, nothing to do',
  ['no-gain'] = 'nothing to do or no gain',
}
local FAIL_TEXT = {
  compress = 'the compression step failed',
  dedupe = 'the deduplication step failed',
  ['not-one-match'] = 'more than one game matched that name',
}
local REFUSAL_TEXT = {
  locked = { words = 'another pass holds the compressor lock',
    step = 'wait for it to finish, then press Run again' },
  snapshots = { words = 'the library has snapshots, so a run would keep the old copies',
    step = 'only if that is acceptable, allow snapshot growth and run again' },
  ['snapshots-unknown'] = { words = 'snapshot state could not be read without root',
    step = 'run once as root so snapshots can be read, or accept unknown snapshots' },
  ['low-space'] = { words = 'free space is below the safety margin',
    step = 'free space on the mount and run again' },
  ['space-unknown'] = { words = 'free space could not be read',
    step = 'make free space readable, or accept unknown safety, and run again' },
  ['not-btrfs'] = { words = 'the library is not on btrfs',
    step = 'point the tool at a btrfs library; other filesystems are left alone' },
  ['read-only'] = { words = 'the library is mounted read-only',
    step = 'remount it read-write and run again' },
}

local function refusal(reason, path, detail)
  reason = tostring(reason or ''):lower()
  path = decode_field(path or '')
  detail = decode_field(detail or '')
  local known = REFUSAL_TEXT[reason]
  local words = known and known.words or ('refused: ' .. (reason ~= '' and reason or 'unknown reason'))
  local step = known and known.step or 'read the log and try again'
  if reason == 'locked' then
    local pid = detail:match('pid%s+(%d+)')
    if pid then step = 'wait for pid ' .. pid .. ' to finish, then press Run again' end
  end
  return {
    reason = reason, path = path, detail = detail,
    text = words .. (detail ~= '' and (' (' .. detail .. ')') or ''),
    next_step = step,
  }
end
M.refusal = refusal

-- Two samples of the current file, seconds apart, give a rolling rate and an
-- ETA. Only compress progress carries a total, so dedupe passes report none.
local function eta_from(prev, cur)
  if type(prev) ~= 'table' or type(cur) ~= 'table' then return nil end
  local dt = (tonumber(cur.at) or 0) - (tonumber(prev.at) or 0)
  local db = (tonumber(cur.doneBytes) or 0) - (tonumber(prev.doneBytes) or 0)
  if dt <= 0 or db <= 0 then return nil end
  local rate = db / dt
  local remain = math.max(0, (tonumber(cur.totalBytes) or 0) - (tonumber(cur.doneBytes) or 0))
  return { rate_bytes = rate, seconds = math.max(0, math.floor(remain / rate + 0.5)) }
end
M.eta_from = eta_from

-- The last sample per (pass, game), so a poll that brought no new PROGRESS line
-- cannot invent a rate. Cleared with the pass, not with the page.
local progress_samples = {}

local function log_lines(log)
  local out = {}
  for chunk in (strip_ansi(tostring(log or '')) .. '\n'):gmatch('([^\r\n]*)[\r\n]') do
    out[#out + 1] = chunk
  end
  return out
end

-- Records from an earlier pass must not colour this one: the driver writes a
-- RUN marker, and only what follows the last one belongs here.
local function pass_start(lines)
  local start = 1
  for index, line in ipairs(lines) do
    if line:sub(1, 4) == 'RUN\t' then start = index + 1 end
  end
  return start
end

local function log_record(entry, fields)
  local kind = fields[1]
  if kind == 'begin' then
    entry.status = 'running'
  elseif kind == 'skip' then
    local reason = decode_field(fields[3] or '')
    entry.status = 'skipped'
    entry.reason = reason
    entry.reasonText = 'skipped: ' .. (SKIP_TEXT[reason] or (reason ~= '' and reason or 'unknown reason'))
  elseif kind == 'fail' then
    local reason = decode_field(fields[3] or '')
    entry.reason = reason
    if reason == 'interrupted' then
      entry.status = 'interrupted'
      entry.reasonText = 'interrupted at ' .. entry.name
    else
      entry.status = 'failed'
      entry.reasonText = 'failed: ' .. (FAIL_TEXT[reason] or (reason ~= '' and reason or 'unknown reason'))
    end
  elseif kind == 'done' then
    entry.status = 'done'
    -- COMPACT|done|NAME|STATUS|SAVED_MIB|DEDUPE_OK. SAVED_MIB is MiB, or the
    -- literal unmeasured; the byte copy is what a byte formatter must use.
    local saved = tostring(fields[4] or '')
    if saved:lower() == 'unmeasured' then
      entry.unmeasured = true
      entry.savedMib = nil
      entry.saved = 0
    else
      local mib = tonumber(saved)
      entry.savedMib = mib
      entry.saved = (mib or 0) * MIB
      entry.unmeasured = mib == nil
    end
  elseif kind == 'estimate' then
    local mib = tonumber(fields[3] or '')
    if mib then
      entry.estimateMib = mib
      entry.estimate = true
    end
  end
end

-- Everything the panel needs about the queue: per-game outcomes from the
-- engine's own lines, a pass-level outcome that distinguishes "not started"
-- from "failed", the current phase and byte counts, and a rolling ETA.
local function read_queue(limit, job)
  local raw = read_text(QUEUE_LIST)
  if not raw then return nil end
  local games, byIndex, byName = {}, {}, {}
  local index = 0
  for line in raw:gmatch('[^\n]+') do
    local fields = split_tabs(line)
    if fields[2] and fields[2] ~= '' then
      index = index + 1
      local entry = {
        index = index, name = fields[1] or '', path = fields[2],
        size = tonumber(fields[3]) or 0, status = 'pending', saved = 0,
      }
      games[#games + 1] = entry
      byIndex[index] = entry
      byName[entry.name] = entry
    end
  end
  local queue = { total = index, index = 0, current = nil, done = 0, failed = 0,
    skipped = 0, interrupted = 0, saved = 0, finished = false, cancelled = false,
    phase = '', doneBytes = 0, totalBytes = 0, scannedBytes = 0,
    outcome = nil, savings = nil, refusal = nil, games = games }
  -- A cancelled pass must never read as finished: bash defers SIGTERM while it
  -- waits for a running game, so the driver can still append its final line
  -- after the signal. The cancel marker the backend writes before signalling
  -- therefore wins over any later `finish`.
  local cancelled = false
  local lastRc = nil
  local passId, logOffset
  local state = read_text(QUEUE_STATE)
  if state then
    for line in state:gmatch('[^\n]+') do
      local fields = split_tabs(line)
      local kind = fields[1] or ''
      if kind == 'pass' then
        passId, logOffset = fields[2], tonumber(fields[3])
      elseif kind == 'start' then
        queue.index = tonumber(fields[2]) or 0
        queue.total = tonumber(fields[3]) or queue.total
        queue.current = {
          name = fields[5] or '', path = fields[6] or '',
          size = tonumber(fields[4]) or 0,
        }
        local entry = byIndex[queue.index]
        if entry then entry.status = 'running' end
      elseif kind == 'done' then
        local idx = tonumber(fields[2]) or 0
        local rc = tonumber(fields[4]) or 0
        lastRc = rc
        local entry = byIndex[idx]
        if entry then
          -- The driver stores this game's own COMPACT| line in field 5. It is
          -- the durable copy: the log tail may have scrolled past it, and a
          -- missing line must mean unmeasured, never a measured zero.
          local record = tostring(fields[5] or '')
          if record:sub(1, 8) == 'COMPACT|' then
            local rec = pipe_fields(record:sub(9))
            if rec[1] == 'done' or rec[1] == 'skip' or rec[1] == 'fail'
              or rec[1] == 'estimate' then
              log_record(entry, rec)
              entry.recorded = true
            end
          else
            local legacy = tonumber(record)
            if legacy and legacy ~= 0 then
              entry.savedMib, entry.saved, entry.unmeasured = legacy, legacy * MIB, false
              entry.recorded = true
            end
          end
          local estimate = tostring(fields[6] or '')
          if estimate:sub(1, 17) == 'COMPACT|estimate|' then
            log_record(entry, pipe_fields(estimate:sub(9)))
          end
          if rc == 0 then
            entry.status = 'done'
          elseif rc == 3 then
            entry.status = 'skipped'
            entry.reason = entry.reason or 'skipped'
            entry.reasonText = entry.reasonText or 'skipped by the compressor'
          elseif rc == 4 then
            entry.status = 'skipped'
            entry.reason = entry.reason or 'no-gain'
            entry.reasonText = entry.reasonText or ('skipped: ' .. SKIP_TEXT['no-gain'])
          elseif rc == 130 or rc == 143 then
            entry.status = 'interrupted'
            entry.reasonText = entry.reasonText or ('interrupted at ' .. entry.name)
          elseif rc == 5 or rc == 6 then
            -- Nothing was attempted for this game: it stays pending, and the
            -- pass-level outcome below carries the reason.
            entry.status = 'pending'
          else
            entry.status = 'failed'
            entry.reasonText = 'failed: the compressor reported an error'
          end
        end
      elseif kind == 'cancel' then
        cancelled = true
        queue.finished = false
      elseif kind == 'finish' then
        queue.finished = not cancelled
        queue.current = nil
      end
    end
  end
  queue.cancelled = cancelled
  -- The engine's own lines for this pass: reasons, savings, refusal, progress.
  local log = read_tail_bytes(QUEUE_LOG, limit or 262144, logOffset)
  local lines = log_lines(log)
  local start = pass_start(lines)
  local refusalSeen = nil
  for position = start, #lines do
    local line = lines[position]
    if line:sub(1, 8) == 'COMPACT|' then
      local fields = pipe_fields(line:sub(9))
      local kind = fields[1]
      local name = decode_field(fields[2] or '')
      local entry = byName[name]
      if kind == 'phase' then
        queue.phase = fields[2] or ''
      elseif kind == 'skip' or kind == 'fail' or kind == 'done' or kind == 'estimate' then
        -- Each record is already in the shape log_record reads: kind in field
        -- 1, the reason in field 3 (skip/fail), SAVED_MIB in field 4 (done).
        -- The log only fills a game the state record did not answer.
        -- An estimate is additive (not a measurement), so it is never
        -- suppressed by the durable record.
        if entry and (not entry.recorded or kind == 'estimate') then
          log_record(entry, fields)
        end
      end
    elseif line:sub(1, 9) == 'PROGRESS|' then
      local fields = pipe_fields(line:sub(10))
      if fields[1] == 'compress' then
        local done_bytes, total_bytes = tonumber(fields[2]), tonumber(fields[3])
        if done_bytes and total_bytes then
          queue.phase = 'compress'
          queue.doneBytes = done_bytes
          queue.totalBytes = total_bytes
        end
      elseif fields[1] == 'dedupe' then
        queue.phase = 'dedupe'
        queue.scannedBytes = tonumber(fields[2]) or 0
      end
    elseif line:sub(1, 8) == 'REFUSED|' then
      local fields = pipe_fields(line:sub(9))
      refusalSeen = refusal(fields[1], fields[2], fields[3])
    end
  end
  if queue.current then
    queue.current.phase = queue.phase
    queue.current.doneBytes = queue.doneBytes
    queue.current.totalBytes = queue.totalBytes
    queue.current.scannedBytes = queue.scannedBytes
  end
  -- A rolling rate and ETA for the current file, from two polls apart.
  local eta = nil
  local sampleKey = passId and queue.current and (passId .. ':' .. tostring(queue.index)
    .. ':' .. queue.current.path) or nil
  if progress_samples.key ~= sampleKey then progress_samples = { key = sampleKey } end
  if sampleKey and queue.phase == 'compress' and queue.totalBytes > 0 and queue.current then
    local cur = { at = os.time(), doneBytes = queue.doneBytes, totalBytes = queue.totalBytes }
    local prev = progress_samples[sampleKey]
    eta = eta_from(prev, cur)
    if not prev or cur.doneBytes ~= prev.doneBytes then
      progress_samples[sampleKey] = cur
    else
      progress_samples[sampleKey] = { at = prev.at, doneBytes = prev.doneBytes,
        totalBytes = cur.totalBytes }
    end
    if eta then
      queue.eta = {
        seconds = eta.seconds,
        rateBytes = math.floor(eta.rate_bytes + 0.5),
        game = queue.current.name,
      }
      queue.current.etaSeconds = eta.seconds
      queue.current.rateBytes = queue.eta.rateBytes
    end
  end
  -- Per-game counters, then the savings contract: measured MiB only, never a
  -- total that quietly includes an unmeasured game, and estimates apart.
  local savings = { measuredMib = 0, measuredGames = 0, unmeasuredGames = 0,
    estimateMib = 0, estimateGames = 0 }
  for _, entry in ipairs(games) do
    if entry.status == 'done' then
      queue.done = queue.done + 1
      -- A done game with no measurement recorded anywhere is unmeasured, never
      -- a measured zero (0 is only a real, recorded no-gain result).
      if entry.savedMib == nil then entry.unmeasured = true end
      if entry.unmeasured then
        savings.unmeasuredGames = savings.unmeasuredGames + 1
        entry.saved = 0
      else
        savings.measuredMib = savings.measuredMib + entry.savedMib
        savings.measuredGames = savings.measuredGames + 1
        entry.saved = entry.savedMib * MIB
        queue.saved = queue.saved + entry.saved
      end
    elseif entry.status == 'skipped' then
      queue.skipped = queue.skipped + 1
    elseif entry.status == 'interrupted' then
      queue.interrupted = queue.interrupted + 1
    elseif entry.status == 'failed' then
      queue.failed = queue.failed + 1
    end
    if entry.estimate then
      savings.estimateMib = savings.estimateMib + (entry.estimateMib or 0)
      savings.estimateGames = savings.estimateGames + 1
    end
    if entry.savedMib then entry.savedBytes = entry.savedMib * MIB end
  end
  local parts = {}
  if savings.measuredGames > 0 then
    parts[#parts + 1] = string.format('%d MiB measured in %d game(s)', savings.measuredMib,
      savings.measuredGames)
  end
  if savings.unmeasuredGames > 0 then
    parts[#parts + 1] = string.format('%d game(s) not measured (no root at the time)',
      savings.unmeasuredGames)
  end
  if savings.estimateGames > 0 then
    parts[#parts + 1] = string.format('about %d MiB freed (estimate)', savings.estimateMib)
  end
  savings.text = #parts > 0 and table.concat(parts, ' \194\183 ') or ''
  -- Byte counts too, for surfaces whose only formatter is the byte one: the
  -- number is still the measured MiB, merely expressed in bytes.
  savings.measuredBytes = savings.measuredMib * MIB
  savings.estimateBytes = savings.estimateMib * MIB
  -- One unambiguous unit for the page: measuredBytes matches the existing byte
  -- formatter, measuredMib is the engine's own unit.
  savings.unit = 'MiB'
  queue.savings = savings
  -- A refusal leaves the current and unreached games pending. Earlier games
  -- may already have completed, in which case the pass stopped early.
  local outcome
  if refusalSeen then
    outcome = { kind = queue.done == 0 and 'not-started' or 'stopped', reason = refusalSeen.reason,
      reasonText = refusalSeen.text, detail = refusalSeen.detail,
      nextStep = refusalSeen.next_step, path = refusalSeen.path }
  elseif queue.interrupted > 0 then
    local game = ''
    for _, entry in ipairs(games) do
      if entry.status == 'interrupted' then game = entry.name; break end
    end
    outcome = { kind = 'interrupted', reason = 'interrupted', game = game,
      reasonText = 'interrupted at ' .. (game ~= '' and game or 'a game'),
      nextStep = 'the next run resumes with that game' }
  elseif queue.finished then
    outcome = { kind = 'finished', reasonText = '' }
  elseif cancelled then
    outcome = { kind = 'stopped', reason = 'cancelled',
      reasonText = 'stopped by you', nextStep = 'the next run resumes with the current game' }
  elseif queue.total == 0 then
    outcome = { kind = 'empty', reasonText = '' }
  elseif not (job and job.action == 'run' and job.running) then
    outcome = { kind = 'stopped', reason = 'stopped', reasonText = 'the queue stopped early',
      nextStep = 'read the log, then run again' }
  else
    outcome = { kind = 'running', reasonText = '' }
  end
  if refusalSeen or outcome.kind == 'interrupted' then queue.finished = false end
  queue.outcome = outcome
  queue.refusal = refusalSeen
  -- A short "next up" list so even the light poll can show what follows.
  queue.next = {}
  for _, entry in ipairs(games) do
    if entry.status == 'pending' and #queue.next < 5 then
      queue.next[#queue.next + 1] = { name = entry.name, size = entry.size }
    end
  end
  return queue
end

-- ----------------------------------------------------------------- libraries
--
-- `libraries_detail` (new in the engine's schema 2) turns a library into one
-- compact row of plain words. An engine that predates the field simply has no
-- table, which is exactly today's view.
local function library_notes(lib)
  local notes = {}
  local fstype = tostring(lib.fstype or '')
  local reason = lib.reason and tostring(lib.reason) or ''
  if reason == 'not-btrfs' or (fstype ~= '' and fstype ~= 'btrfs') then
    notes[#notes + 1] = 'ignored: ' .. (fstype ~= '' and fstype or 'not btrfs')
  end
  if lib.readOnly == true or reason == 'read-only' then notes[#notes + 1] = 'read-only' end
  if lib.snapshots == 'yes' or reason == 'snapshots' then
    notes[#notes + 1] = 'snapshots present: a run would keep old copies'
  elseif lib.snapshots == 'unknown' or reason == 'snapshots-unknown' then
    notes[#notes + 1] = 'snapshots unknown: not readable without root'
  end
  if reason == 'low-space' then notes[#notes + 1] = 'free space too low'
  elseif reason == 'space-unknown' then notes[#notes + 1] = 'free space unknown' end
  local free = tonumber(lib.freeMib)
  if free then
    local percent = tonumber(lib.freePercent)
    notes[#notes + 1] = string.format('%d MiB free%s', math.floor(free),
      percent and string.format(' (%.0f%%)', percent) or '')
  end
  if lib.eligible == false and #notes == 0 and reason ~= '' then
    notes[#notes + 1] = 'not used: ' .. reason
  end
  return notes
end

local function build_libraries(status)
  local detail = status and status.libraries_detail
  if type(detail) ~= 'table' then return nil end
  local out = {}
  for _, lib in ipairs(detail) do
    if type(lib) == 'table' then
      local entry = {
        path = tostring(lib.path or ''),
        mount = tostring(lib.mount or ''),
        fstype = tostring(lib.fstype or ''),
        options = tostring(lib.options or ''),
        compress = lib.compress or nil,
        level = tonumber(lib.level),
        readOnly = lib.readOnly == true,
        -- Never inferred: absent keys stay nil so an older engine simply shows
        -- fewer facts instead of a guess.
        snapshots = lib.snapshots and tostring(lib.snapshots) or nil,
        freeMib = tonumber(lib.freeMib),
        freePercent = tonumber(lib.freePercent),
        reason = lib.reason and tostring(lib.reason) or nil,
        detail = lib.detail and tostring(lib.detail) or nil,
      }
      -- Never inferred, and never the `cond and false or nil` trap: an absent
      -- eligible key stays nil.
      if type(lib.eligible) == 'boolean' then entry.eligible = lib.eligible end
      entry.notes = library_notes(entry)
      entry.compressionText = entry.compress
        and (tostring(entry.compress) .. (entry.level and (' level ' .. entry.level) or ''))
        or 'no mount compression'
      entry.snapshotTool = lib.snapshotTool and tostring(lib.snapshotTool) or nil
      entry.reasonDetail = lib.reason_detail and tostring(lib.reason_detail) or nil
      out[#out + 1] = entry
    end
  end
  return out
end
M.build_libraries = build_libraries

-- --------------------------------------------------------------- snapshots
--
-- `snapshots-unknown` is a usability trap: without root the engine cannot see
-- whether snapshots exist, so every game refuses until the user opts in, and a
-- detached pass cannot inherit an exported variable. The engine's
-- `accept_snapshots_unknown` config key exists for exactly this, and the button
-- writes it — one key, one file, atomically, and never on its own.
local SNAPSHOT_FILE = TOOL_DIR .. '/accept_snapshots_unknown'

-- The engine accepts only the words true and false in this file (anything else
-- is ignored and stays false), and it reports its own view of it as the
-- top-level `accept_snapshots_unknown` of --status --json. The banner renders
-- that value, never the file we wrote.
local function write_accept_snapshots(enabled)
  fs.create_directories(TOOL_DIR)
  return write_text(SNAPSHOT_FILE, enabled and 'true\n' or 'false\n')
end
M.accept_snapshots_file = SNAPSHOT_FILE

local function build_snapshots(libraries, accept)
  if type(libraries) ~= 'table' then return nil end
  if #libraries == 0 and accept ~= true then return nil end
  local enabled = nil
  if type(accept) == 'boolean' then enabled = accept end
  local tool, paths = nil, {}
  local detected = nil
  local has_tool_field = false
  for _, lib in ipairs(libraries) do
    if lib.snapshotTool ~= nil then has_tool_field = true; break end
  end
  for _, lib in ipairs(libraries) do
    -- `snapshotTool` and `reason_detail` are per-library engine fields; absent
    -- keys mean an older engine, and then nothing beyond today's view is shown.
    local unreadable = lib.snapshots == 'unknown' or lib.reason == 'snapshots-unknown'
    if has_tool_field and unreadable
      and (lib.reason == 'snapshots-unknown' or enabled == true) then
      local current = lib.snapshotTool
      if current ~= 'none' and current ~= 'configured' then current = 'unknown' end
      if not tool or current == 'unknown' or (current == 'configured' and tool == 'none') then
        tool = current
      end
      paths[#paths + 1] = lib.path
    end
    if lib.reason == 'snapshots' or lib.snapshots == 'yes' then
      detected = detected or lib.path
    end
  end
  -- An override left on must always be visible and reversible, even when the
  -- next scan made every library eligible and no banner would otherwise show.
  if enabled == true and not tool then
    for _, lib in ipairs(libraries) do
      if lib.snapshotTool ~= nil then tool = lib.snapshotTool; break end
    end
    tool = tool or 'unknown'
  end
  if not tool and not detected then return nil end
  local out = {}
  if detected then
    out.detected = {
      path = detected,
      text = 'This library has snapshots: a run would keep the old copies, so disk use can grow. '
        .. 'Deduplication is unaffected.',
    }
  end
  if tool then
    tool = tostring(tool)
    local text, button, message
    if enabled == true then
      text = 'This override is on: compressing keeps the old copies and can use more disk.'
      button = 'Turn the override off'
    elseif tool == 'none' then
      text = 'No snapshot tool (snapper, Timeshift, btrbk) is configured for it, so compressing '
        .. 'is usually safe.'
      button = 'Allow compression (writes the setting for you)'
    elseif tool == 'configured' then
      text = 'A snapshot tool is configured: compressing keeps the old copies and can use more disk.'
      button = 'Compress anyway (keeps snapshots)'
    else
      tool = 'unknown'
      text = 'A snapshot tool may be configured: compressing keeps the old copies and can use '
        .. 'more disk. Check for snapshots yourself first.'
      button = 'Compress anyway (keeps snapshots)'
    end
    out.choice = {
      tool = tool,
      title = 'Luna cannot see whether this library has snapshots without root.',
      text = text,
      button = button,
      -- nil when the engine did not report the top-level value: no toggle then.
      enabled = enabled,
      enabledKnown = enabled ~= nil,
      paths = json.array(paths),
      setting = SNAPSHOT_FILE,
    }
  end
  return out
end
M.build_snapshots = build_snapshots

-- What Health reports about the pass log: its size now, the cap, and the kept
-- previous pass. Nothing is deleted, only renamed.
local function build_health()
  return {
    logBytes = file_bytes(QUEUE_LOG),
    logLimitBytes = LOG_LIMIT,
    logRotatedBytes = file_bytes(LOG_PREV),
    logKept = fs.exists(LOG_PREV) and true or false,
  }
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

-- ------------------------------------------------------------ log rotation
--
-- The pass log is the audit trail of a compaction, so it is rotated rather than
-- truncated: run.log becomes run.log.1 (replacing the one before) once it grows
-- past the cap, and the size is reported in Health. A live pass is never rotated
-- out from under itself; the caller may check this without a run at all.
local function rotate_log(limit)
  limit = limit or LOG_LIMIT
  local job = read_job()
  if job and alive(job.pid) then return false, file_bytes(QUEUE_LOG), 'live' end
  local size = file_bytes(QUEUE_LOG)
  if size <= limit then return false, size, 'small' end
  local ok = os.rename(QUEUE_LOG, LOG_PREV)
  if not ok then return false, size, 'rename-failed' end
  return true, 0, 'rotated'
end
M.rotate_log = rotate_log
M.log_limit = LOG_LIMIT

-- Launches detached so a pass survives Steam and Lumen both going away. The
-- command's own shell records $$ in a pid file before exec, which stays correct
-- even in the rare case where setsid has to fork, and it leads its own process
-- group so a cancel can take the whole pass down.
local function spawn_detached(command, action, scan)
  fs.create_directories(DIR)
  os.remove(JOB_DONE)
  write_text(JOB_LOG, '')
  if scan then os.remove(STATUS_TMP) end
  local pidfile = DIR .. '/job.pid'
  os.remove(pidfile)
  local redirect = scan
    and ('> ' .. shq(STATUS_TMP) .. ' 2>> ' .. shq(JOB_LOG))
    or ('> ' .. shq(JOB_LOG) .. ' 2>&1')
  local inner = string.format('echo $$ > %s; exec nohup %s %s < /dev/null',
    shq(pidfile), command, redirect)
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
  if scan then write_text(ATTEMPT_AT, tostring(os.time())) end
  return pid
end

local function spawn(tool, action, args)
  local spec = ACTIONS[action]
  return spawn_detached(shq(tool) .. ' ' .. (args or spec.args), action, spec.scan == true)
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

-- observe: report the job as it is, without recording its completion or
-- adopting its report (status --live and the selftest must not write).
local function job_state(observe)
  local job = read_job()
  if not job then return nil end
  local running = alive(job.pid)
  local finishedAt = tonumber(read_text(JOB_DONE) or '') or 0
  if not running and finishedAt == 0 and observe then
    finishedAt = os.time()
  elseif not running and finishedAt == 0 then
    finishedAt = os.time()
    write_text(JOB_DONE, tostring(finishedAt))
    if job.action == 'scan' then adopt_status() end
  end
  if running then finishedAt = 0 end
  local jobLog = tail(JOB_LOG, 40, 65536)
  local refused = nil
  for line in jobLog:gmatch('[^\n]+') do
    if line:sub(1, 8) == 'REFUSED|' then
      local fields = pipe_fields(line:sub(9))
      refused = refusal(fields[1], fields[2], fields[3])
    end
  end
  return {
    action = job.action, pid = job.pid, running = running,
    startedAt = job.startedAt, finishedAt = finishedAt,
    elapsed = running and (os.time() - job.startedAt) or 0,
    log = jobLog,
    refusal = refused,
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
    runOrder = data.runOrder,
    excluded = json.array(data.excluded),
  }
end

local function response(o) return json.encode(o) end

-- One memo per backend instance; see GET_CACHE_TTL above.
local get_cache = { rev = '', text = nil, expires = 0 }

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

  registry.SlsCompressorGet = function(raw)
    local tool = find_tool()
    -- Settings can change on disk (a hand edit, another surface), so the file
    -- is the source of truth on every read.
    settings = read_settings()
    -- Polling clients can ask for the light shape: the plan and the job, with
    -- the per-game list left out so a one-second poll stays tiny.
    local light = false
    local refresh = false
    -- observe: a strictly read-only look (no auto-scan, no report adoption).
    local observe = false
    if type(raw) == 'string' and #raw <= 200 then
      local ok, obj = pcall(json.decode, raw)
      if ok and type(obj) == 'table' then
        light = obj.light == true
        refresh = obj.refresh == true
        observe = obj.observe == true
      end
    end
    -- Every file the reply is built from, so the memo below cannot outlive a
    -- scan that landed or a queue that moved.
    local inputs = { SETTINGS, STATUS, STATUS_AT, ATTEMPT_AT, JOB_FILE, JOB_DONE,
      JOB_LOG, QUEUE_LIST, QUEUE_STATE, QUEUE_LOG, TOOL_STATE_FILE,
      TOOL_DEDUPE_HISTORY, SNAPSHOT_FILE, DIR, TOOL_DIR, tool or '' }
    local key = function()
      local rev = stamps_rev(inputs)
      if not rev then return nil end
      -- Two scans can be byte-identical in every stamped file (same pid width,
      -- same second): the job pid keeps a new scan from answering from the
      -- previous one's memo.
      local pid = (read_text(JOB_FILE) or ''):match('"pid"%s*:%s*(%d+)') or ''
      return rev .. '|' .. pid .. '|' .. (light and '1' or '0') .. (observe and 'o' or '')
    end
    local now = os.time()
    local cacheKey = key()
    -- `refresh` is the explicit escape hatch: a caller that just changed a
    -- setting or finished a pass can skip the memo instead of waiting the TTL.
    if not refresh and cacheKey and get_cache.text and get_cache.rev == cacheKey
      and now < get_cache.expires then
      return get_cache.text
    end
    -- job_state() first: observing a finished scan adopts its report, which is
    -- what this same response must already include.
    local job = job_state(observe)
    local status = scan_state()
    local statusAt = tonumber(read_text(STATUS_AT) or '') or 0
    -- The first open starts the read-only scan itself; a recent failed attempt
    -- is not retried in a loop, and a running job is always left alone.
    local autoStarted = false
    if tool and not observe and not (job and job.running) and status == nil then
      local attempt = tonumber(read_text(ATTEMPT_AT) or '') or 0
      if os.time() - attempt > 600 then
        local pid = spawn(tool, 'scan', ACTIONS.scan.args)
        if pid then
          autoStarted = true
          job = job_state()
        end
      end
    end
    local plan = build_plan(status, settings)
    local queue = read_queue(light and 262144 or 524288, job)
    if queue then
      queue.active = job ~= nil and job.running and job.action == 'run' and not queue.finished
      queue.next = json.array(queue.next or {})
      if light then
        -- The light poll still carries the pass outcome and its savings: they
        -- are what the page shows while it watches.
        queue.savings = queue.savings
        queue.outcome = queue.outcome
        queue.games = nil
      else
        queue.games = json.array(queue.games)
      end
    end
    local libraries = build_libraries(status)
    local snapshots = build_snapshots(libraries, status and status.accept_snapshots_unknown)
    local wire_status = status
    if light and status then
      -- libraries_detail is tiny and the page needs it on every poll.
      wire_status = { version = status.version, summary = status.summary,
        libraries = status.libraries, libraries_detail = status.libraries_detail }
    end
    -- The Stats tab is part of the full shape only; polling clients never pay
    -- for it.
    local stats = nil
    if not light then stats = build_stats(status, queue) end
    local out = response({
      success = true,
      tool = {
        found = tool ~= nil,
        path = tool or '',
        version = status and tostring(status.version or '') or '',
      },
      status = wire_status,
      statusAt = statusAt,
      libraries = libraries,
      snapshots = snapshots,
      health = build_health(),
      settings = encode_settings(settings),
      plan = plan,
      queue = queue,
      stats = stats,
      job = job,
      autoStarted = autoStarted,
    })
    -- A running pass (or a scan this call just started) rewrites its log and
    -- state while the client polls, and the next call should see that at once,
    -- so only a settled backend is memoised.
    if not refresh and cacheKey and not autoStarted and not (job and job.running) then
      get_cache.text = out
      get_cache.rev = key()
      get_cache.expires = os.time() + GET_CACHE_TTL
    end
    return out
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
    if RUN_ORDERS[obj.runOrder] then next_settings.runOrder = obj.runOrder end
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

  -- The snapshot refusal is the one place where the add-on writes a config
  -- file for the user: only from this explicit press, never on load or during a
  -- run, and only with an explicit answer. The accepted shapes are exactly
  -- {"enabled":true|false} and a bare JSON true/false; anything else (a typo
  -- such as {"enable":false}, a string, {}) is refused, because guessing would
  -- silently enable snapshot-unknown compression.
  registry.SlsCompressorAcceptSnapshots = function(raw)
    local payload = nil
    if type(raw) == 'string' and raw ~= '' then
      local ok, parsed = pcall(json.decode, raw)
      if ok then payload = parsed end
    end
    local want = nil
    if type(payload) == 'boolean' then want = payload end
    if type(payload) == 'table' and type(payload.enabled) == 'boolean' then
      want = payload.enabled
    end
    if want == nil then
      return response({success = false, code = 'invalid-payload',
        error = 'Expected {"enabled":true} or {"enabled":false}.'})
    end
    -- A live queue rereads the setting for every remaining game, so changing it
    -- mid-pass would apply to games the user has not seen the banner for.
    local live = job_state()
    if live and live.running and live.action == 'run' then
      return response({success = false, code = 'run-live',
        error = 'A compression run is in progress; stop it before changing the snapshot setting.'})
    end
    local current = read_text(SNAPSHOT_FILE)
    local already = current == (want and 'true\n' or 'false\n')
    if not already and not write_accept_snapshots(want) then
      return response({success = false, error = 'Could not write the snapshot setting'})
    end
    -- Re-read the engine's view: a detached scan is the only honest way, since
    -- the eligibility comes from it and not from the setting alone.
    -- A scan that started before this write would report the old value. Wait
    -- for it (bounded) so the rescan below really reads the new file.
    local job = job_state()
    local waited = 0
    while job and job.running and waited < 20 do
      os.execute('sleep 0.1')
      waited = waited + 1
      job = job_state()
    end
    local scanning = false
    local tool = find_tool()
    if tool and not (job and job.running) then
      scanning = spawn(tool, 'scan', ACTIONS.scan.args) ~= nil
    end
    return response({
      success = true,
      already = already,
      enabled = want,
      setting = SNAPSHOT_FILE,
      value = want and 'true' or 'false',
      scanning = scanning,
      message = want
        and (already and 'Snapshot growth was already allowed for compression.'
          or 'Snapshot growth is now allowed (accept_snapshots_unknown = true).')
        or (already and 'Snapshot growth was already not allowed.'
          or 'Snapshot growth is no longer allowed (accept_snapshots_unknown = false).'),
    })
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
    local job = job_state()
    if job and job.running then
      return response({success = false, error = 'A job is already running'})
    end
    local tool = find_tool()
    if not tool then
      return response({success = false, error = 'btrfs-game-compressor was not found'})
    end
    if action ~= 'run' then
      local pid = spawn(tool, action, ACTIONS[action].args)
      if not pid then
        return response({success = false, error = 'Could not start the tool'})
      end
      return response({
        success = true,
        job = {action = action, pid = pid, running = true, startedAt = os.time(),
          args = ACTIONS[action].args},
      })
    end
    -- A run is game by game: the backend writes the queue and a detached driver
    -- runs one `--compact` per game, so the panel gets a state and a bar per
    -- game and the pass survives the UI closing.
    if not settings.compress then
      if not settings.dedupe then
        return response({success = false, error = 'Nothing is enabled: turn on compression or deduplication first'})
      end
      -- Dedupe-only has no per-game command; the tool's library pass is the
      -- maintenance path and the queue does not apply.
      local pid = spawn(tool, 'run', '--dedupe')
      if not pid then
        return response({success = false, error = 'Could not start the tool'})
      end
      return response({
        success = true,
        job = {action = 'run', pid = pid, running = true, startedAt = os.time(), args = '--dedupe'},
      })
    end
    local status = scan_state()
    if not status then
      return response({success = false, error = 'Scan the library first so the queue can be built'})
    end
    local queue = build_queue(status, settings)
    if #queue == 0 then
      return response({success = false, error = 'Nothing to do with the current settings'})
    end
    -- A grown pass log is rotated before this pass writes anything. No pass is
    -- live here (a running job was refused above), so this can never move the
    -- log out from under a run.
    rotate_log()
    local lines = {}
    for _, game in ipairs(queue) do
      lines[#lines + 1] = game.name .. '\t' .. game.path .. '\t' .. tostring(game.size)
    end
    if not write_text(QUEUE_LIST, table.concat(lines, '\n') .. '\n') then
      return response({success = false, error = 'Could not write the queue'})
    end
    -- Per-pass state: a stale `finish` (or a cancel marker) from the previous
    -- pass must not colour this one.
    local passId = read_text('/proc/sys/kernel/random/uuid')
    passId = passId and passId:match('^[%x%-]+')
    if not passId then return response({success = false, error = 'Could not identify the queue pass'}) end
    if not write_text(QUEUE_STATE, 'pass\t' .. passId .. '\t' .. file_bytes(QUEUE_LOG) .. '\n') then
      return response({success = false, error = 'Could not reset the queue state'})
    end
    if not write_driver(tool) then
      return response({success = false, error = 'Could not write the queue driver'})
    end
    local pid = spawn_detached(shq(DRIVER), 'run', false)
    if not pid then
      return response({success = false, error = 'Could not start the queue'})
    end
    return response({
      success = true,
      job = {action = 'run', pid = pid, running = true, startedAt = os.time(),
        args = 'queue', queue = #queue},
    })
  end

  -- Best effort, and deliberately terminal: a compaction is checkpointed and
  -- resumable, so a stopped pass loses at most the current game's progress.
  -- Uses /bin/kill explicitly: dash's builtin kill rejects the `--` separator
  -- (CI failure: "Illegal number: -"), while procps kill handles group and
  -- single-PID signalling portably. The outcome is verified, not assumed.
  registry.SlsCompressorCancel = function()
    local job = job_state()
    if not job or not job.running then
      return response({success = false, error = 'No job is running'})
    end
    local pid = math.floor(job.pid)
    if job.action == 'run' and fs.exists(QUEUE_STATE) then
      -- Before the signal, so a driver that outlives it cannot make the pass
      -- read as finished; the marker also survives the driver's own finish.
      local marker = io.open(QUEUE_STATE, 'ab')
      if marker then
        marker:write('cancel\t' .. tostring(os.time()) .. '\n')
        marker:close()
      end
    end
    local group = process_group(pid)
    local command
    -- `-s TERM -- [-]PID`: procps-ng kill (Ubuntu's /bin/kill) misreads
    -- `-TERM -PGID` and was seen signalling unrelated groups and even -1
    -- (every process of the user); util-linux kill and the shell builtins read
    -- this form the same way.
    if pid <= 1 then
      return response({success = false, code = 'signal', error = 'Refusing to signal pid ' .. pid})
    end
    if group and group == pid then
      command = '/bin/kill -s TERM -- -' .. pid
    else
      command = '/bin/kill -s TERM -- ' .. pid
    end
    local ok = os.execute(command)
    -- os.execute returns true/0 on success depending on the Lua version;
    -- anything else means the signal was not delivered.
    local delivered = ok == true or ok == 0
    if not delivered then
      return response({success = false, code = 'signal',
        error = 'Could not signal the running pass (pid ' .. pid .. ')'})
    end
    return response({success = true, cancelled = true, pid = pid})
  end
end

M.tail = tail
M.normalize_languages = normalize_languages
M.binary_candidates = CANDIDATES
return M
