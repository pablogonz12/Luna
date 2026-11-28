-- Compressor backend integration test: tool discovery, the detached job runner,
-- status adoption, settings persistence and mirroring, action composition, the
-- guards and cancellation. Only run with HOME pointing to a disposable tmpdir.
assert(os.getenv('LUNA_TEST_HOME') == os.getenv('HOME'), 'Test must run in isolated HOME')

-- A small real JSON codec: the backend round-trips its own payloads, and the
-- fake tool writes its status report as real JSON, so the stub must parse.
local function skip(text, pos)
  return (text:find('[^ \t\r\n]', pos) or (#text + 1))
end

local function decode_value(text, pos)
  pos = skip(text, pos)
  local c = text:sub(pos, pos)
  if c == '{' then
    local out = {}
    pos = skip(text, pos + 1)
    if text:sub(pos, pos) == '}' then return out, pos + 1 end
    while true do
      local key
      key, pos = decode_value(text, pos)
      pos = skip(text, pos) + 1 -- ':'
      local value
      value, pos = decode_value(text, pos)
      out[key] = value
      pos = skip(text, pos)
      if text:sub(pos, pos) == '}' then return out, pos + 1 end
      pos = pos + 1 -- ','
    end
  end
  if c == '[' then
    local out = {}
    pos = skip(text, pos + 1)
    if text:sub(pos, pos) == ']' then return out, pos + 1 end
    while true do
      local value
      value, pos = decode_value(text, pos)
      out[#out + 1] = value
      pos = skip(text, pos)
      if text:sub(pos, pos) == ']' then return out, pos + 1 end
      pos = pos + 1 -- ','
    end
  end
  if c == '"' then
    local out, escaped = {}, false
    pos = pos + 1
    while pos <= #text do
      local ch = text:sub(pos, pos)
      if escaped then
        if ch == 'n' then out[#out + 1] = '\n'
        elseif ch == 'r' then out[#out + 1] = '\r'
        elseif ch == 't' then out[#out + 1] = '\t'
        elseif ch == 'u' then
          out[#out + 1] = utf8.char(tonumber(text:sub(pos + 1, pos + 4), 16))
          pos = pos + 4
        else out[#out + 1] = ch end
        escaped = false
      elseif ch == '\\' then escaped = true
      elseif ch == '"' then return table.concat(out), pos + 1
      else out[#out + 1] = ch end
      pos = pos + 1
    end
    error('unterminated string')
  end
  local number = text:match('^-?%d+%.?%d*[eE]?[-+]?%d*', pos)
  if number then return tonumber(number), pos + #number end
  if text:sub(pos, pos + 3) == 'true' then return true, pos + 4 end
  if text:sub(pos, pos + 4) == 'false' then return false, pos + 5 end
  if text:sub(pos, pos + 3) == 'null' then return nil, pos + 4 end
  error('unexpected byte at ' .. pos)
end

local function encode(value)
  local kind = type(value)
  if kind == 'nil' then return 'null' end
  if kind == 'boolean' then return value and 'true' or 'false' end
  if kind == 'number' then
    if value == math.floor(value) and math.abs(value) < 2 ^ 53 then return string.format('%d', value) end
    return string.format('%.10g', value)
  end
  if kind == 'string' then
    return '"' .. value:gsub('[%z\1-\31\\"]', function(ch)
      if ch == '"' then return '\\"' end
      if ch == '\\' then return '\\\\' end
      if ch == '\n' then return '\\n' end
      if ch == '\r' then return '\\r' end
      if ch == '\t' then return '\\t' end
      return string.format('\\u%04x', ch:byte())
    end) .. '"'
  end
  if kind == 'table' then
    local count, array = 0, true
    for key in pairs(value) do
      count = count + 1
      if type(key) ~= 'number' or key ~= math.floor(key) or key < 1 then array = false end
    end
    if count > 0 and array then
      local parts = {}
      for index = 1, #value do parts[#parts + 1] = encode(value[index]) end
      return '[' .. table.concat(parts, ',') .. ']'
    end
    local names = {}
    for key in pairs(value) do names[#names + 1] = tostring(key) end
    table.sort(names)
    local parts = {}
    for _, name in ipairs(names) do parts[#parts + 1] = encode(name) .. ':' .. encode(value[name]) end
    return '{' .. table.concat(parts, ',') .. '}'
  end
  error('cannot encode ' .. kind)
end

package.preload['json'] = function()
  return {
    encode = encode,
    decode = function(text) return decode_value(text, 1) end,
    array = function(t) return t or {} end,
  }
end
package.preload['fs'] = function()
  return {
    create_directories = function(dir)
      local ok = os.execute('mkdir -p ' .. string.format('%q', dir))
      assert(ok == true or ok == 0, 'mkdir failed')
      return true
    end,
    exists = function(path)
      local f = io.open(path, 'rb')
      if f then f:close(); return true end
      return false
    end,
    join = function(...) return table.concat({ ... }, '/') end,
  }
end

local home = os.getenv('HOME')
local json = require('json')
local repo = os.getenv('LUNA_REPO') or '.'

-- The backend memoises SlsCompressorGet on file stamps. No stat module is
-- installed here, so a tiny lfs stand-in supplies size + a bumpable
-- modification time, which is what makes the cache observable.
local stamps = {}
package.preload['lfs'] = function()
  return { attributes = function(path)
    local f = io.open(path, 'rb')
    if not f then return nil end
    local size = f:seek('end'); f:close()
    return { size = size, modification = stamps[path] or 0 }
  end }
end
local function touch(path) stamps[path] = (stamps[path] or 0) + 1 end

-- The standard install location is the discovery path under test. A mode file
-- in HOME switches the fake engine's behaviour, so every exit code and reason of
-- the machine contract can be exercised in the same process.
local tool = home .. '/.local/bin/btrfs-game-compressor'
do
  assert(os.execute('mkdir -p ' .. string.format('%q', home .. '/.local/bin')))
  local f = assert(io.open(tool, 'wb'))
  f:write([==[
#!/usr/bin/env bash
echo "ARGS: $*" >&2
mode=ok
[ -f "$HOME/.compressor-mode" ] && mode=$(cat "$HOME/.compressor-mode")
games_one='[{"name":"A","path":"/games/A","status":"UNCOMPRESSED","size_bytes":10,"size_display":"10B","dedupe_pending":true},{"name":"B","path":"/games/B","status":"COMPACTED","size_bytes":5,"size_display":"5B","dedupe_pending":false}]'
games_two='[{"name":"A","path":"/games/A","status":"UNCOMPRESSED","size_bytes":10,"size_display":"10B","dedupe_pending":true},{"name":"B","path":"/games/B","status":"UNCOMPRESSED","size_bytes":5,"size_display":"5B","dedupe_pending":true}]'
summary_one='{"game_count":2,"pending":1,"compacted":1,"compressed":0,"compressed_only":0,"low_yield":0,"running":0,"skipped_empty":0}'
summary_two='{"game_count":2,"pending":2,"compacted":0,"compressed":0,"compressed_only":0,"low_yield":0,"running":0,"skipped_empty":0}'
libs_plain='[{"path":"/lib","mount":"/mnt/lib","fstype":"btrfs","options":"rw,compress=zstd:1","compress":"zstd","level":1,"readOnly":false,"snapshots":"no","freeMib":9000,"freePercent":30,"metadataPercent":20,"eligible":true,"reason":null},{"path":"/data/common","mount":"/data","fstype":"ext4","options":"rw","compress":null,"level":0,"readOnly":false,"snapshots":"unknown","freeMib":100,"freePercent":5,"eligible":false,"reason":"not-btrfs"}]'
libs_snap='[{"path":"/lib","mount":"/mnt/lib","fstype":"btrfs","options":"rw","compress":"zstd","level":1,"readOnly":false,"snapshots":"unknown","freeMib":9000,"freePercent":30,"eligible":false,"reason":"snapshots-unknown","reason_detail":"snapper did not answer","snapshotTool":"none"}]'
libs_configured='[{"path":"/lib","mount":"/mnt/lib","fstype":"btrfs","options":"rw","compress":"zstd","level":1,"readOnly":false,"snapshots":"unknown","freeMib":9000,"freePercent":30,"eligible":false,"reason":"snapshots-unknown","reason_detail":"snapper did not answer","snapshotTool":"configured"}]'
libs_unknown='[{"path":"/lib","mount":"/mnt/lib","fstype":"btrfs","options":"rw","compress":"zstd","level":1,"readOnly":false,"snapshots":"unknown","freeMib":9000,"freePercent":30,"eligible":false,"reason":"snapshots-unknown","reason_detail":"no snapshot tool answered","snapshotTool":"unknown"}]'
libs_snapyes='[{"path":"/lib","mount":"/mnt/lib","fstype":"btrfs","options":"rw","compress":"zstd","level":1,"readOnly":false,"snapshots":"yes","freeMib":9000,"freePercent":30,"eligible":false,"reason":"snapshots","reason_detail":"snapper config on /mnt/lib","snapshotTool":"configured"}]'
libs_nofield='[{"path":"/lib","mount":"/mnt/lib","fstype":"btrfs","options":"rw","compress":"zstd","level":1,"readOnly":false,"snapshots":"unknown","freeMib":9000,"freePercent":30,"eligible":false,"reason":"snapshots-unknown"}]'
libs_allowed='[{"path":"/lib","mount":"/mnt/lib","fstype":"btrfs","options":"rw","compress":"zstd","level":1,"readOnly":false,"snapshots":"unknown","freeMib":9000,"freePercent":30,"eligible":true,"reason":null,"detail":"","snapshotTool":"none","reason_detail":""}]'
acc=false
accfile="$HOME/.config/btrfs-game-compressor/accept_snapshots_unknown"
[ -f "$accfile" ] && read -r accval < "$accfile"
[ "${accval:-}" = true ] && acc=true
case "$mode" in
  snapnone) libs="$libs_snap"; games="$games_one"; summary="$summary_one" ;;
  snapconfigured) libs="$libs_configured"; games="$games_one"; summary="$summary_one" ;;
  snapunknown) libs="$libs_unknown"; games="$games_one"; summary="$summary_one" ;;
  snapyes) libs="$libs_snapyes"; games="$games_one"; summary="$summary_one" ;;
  nofield) libs="$libs_nofield"; games="$games_one"; summary="$summary_one" ;;
  nolib) libs=''; games="$games_one"; summary="$summary_one" ;;
  twogames) libs="$libs_plain"; games="$games_two"; summary="$summary_two" ;;
  *) libs="$libs_plain"; games="$games_one"; summary="$summary_one" ;;
esac
if [ "$acc" = true ] && [ -n "$libs" ]; then
  case "$mode" in
    snapnone|snapconfigured|snapunknown) libs="$libs_allowed" ;;
  esac
fi
case "$1" in
  --status)
    printf '%s\n' "$acc" >> "$HOME/.scan-invocations"
    [ "$mode" = scanfail ] && exit 7
    if [ "$mode" = scanescape ]; then
      setsid sh -c 'echo $$ > "$HOME/.escaped-scan"; exec timeout 120 tail -f /dev/null' </dev/null >/dev/null 2>&1 &
      sleep 120
    fi
    if [ "$mode" = scanhandoff ] && [ "$acc" = true ]; then
      : > "$HOME/.successor-started"
      for _ in {1..600}; do [ -f "$HOME/.successor-release" ] && break; sleep 0.1; done
      [ -f "$HOME/.successor-release" ] || exit 1
    elif [ "$mode" = scanhandoff ]; then sleep 2
    elif [ "$mode" = scanblock ]; then sleep 6
    elif [ "$mode" = scanlong ]; then sleep 2; else sleep 0.3; fi
    if [ -n "$libs" ]; then
      printf '{"version":"9.9.9","schema":2,"accept_snapshots_unknown":%s,"libraries":["/lib"],"libraries_detail":%s,"games":%s,"summary":%s}\n' "$acc" "$libs" "$games" "$summary"
    else
      printf '{"version":"9.9.9","schema":2,"accept_snapshots_unknown":%s,"libraries":["/lib"],"games":%s,"summary":%s}\n' "$acc" "$games" "$summary"
    fi ;;
  --dry-run) echo 'dry run: nothing changed'; sleep 0.4 ;;
  --compact)
    case "$mode" in
      ok) printf 'COMPACT|begin|A|%s|10\n' "$2"; echo 'PROGRESS|compress|64|128'; sleep 1.2; echo 'PROGRESS|compress|128|128'; echo 'COMPACT|phase|dedupe'; echo 'PROGRESS|dedupe|64|0'; sleep 1.2; printf 'COMPACT|done|A|COMPACTED|12345678|1\n' ;;
      unmeasured) printf 'COMPACT|done|A|COMPACTED|unmeasured|1\n' ;;
      zero) printf 'COMPACT|done|A|COMPACTED|0|1\n' ;;
      estimate) printf 'COMPACT|estimate|A|900|unprivileged\n'; printf 'COMPACT|done|A|COMPACTED|unmeasured|1\n' ;;
      skip) printf 'COMPACT|skip|A|running|%s\n' "$2"; exit 3 ;;
      nodatacow) printf 'COMPACT|skip|A|nodatacow|%s\n' "$2"; exit 3 ;;
      locked) echo 'REFUSED|locked|/mnt/lib|pid 4242 cmd --compact'; exit 6 ;;
      snapshots) echo 'REFUSED|snapshots|/mnt/lib|20 GiB would be rewritten'; exit 5 ;;
      snapunknown) echo 'REFUSED|snapshots-unknown|/mnt/lib|snapper did not answer'; exit 5 ;;
      lowspace) echo 'REFUSED|low-space|/mnt/lib|100 MiB free, need 2048'; exit 5 ;;
      spaceunknown) echo 'REFUSED|space-unknown|/mnt/lib|stat -f failed'; exit 5 ;;
      notbtrfs) echo 'REFUSED|not-btrfs|/mnt/lib|ext4'; exit 5 ;;
      readonly) echo 'REFUSED|read-only|/mnt/lib|mounted ro'; exit 5 ;;
      interrupt) printf 'COMPACT|fail|A|interrupted\n'; exit 143 ;;
      slow) printf 'COMPACT|begin|A|%s|1000000\n' "$2"; echo 'PROGRESS|compress|0|1000000'; sleep 1.0; echo 'PROGRESS|compress|500000|1000000'; sleep 1.4; echo 'PROGRESS|compress|750000|1000000'; sleep 1.4; echo 'PROGRESS|compress|1000000|1000000'; sleep 1.5; printf 'COMPACT|done|A|COMPACTED|500|1\n' ;;
      *) exit 2 ;;
    esac ;;
  --run) echo 'batch running'; sleep 2.5 ;;
  --dedupe) echo 'dedupe pass'; sleep 0.5 ;;
  --recheck) echo 'rechecking measurements'; sleep 0.5 ;;
  --assets-all) echo 'asset inventory'; sleep 2.5 ;;
  *) echo "unknown $1" >&2; exit 2 ;;
esac
]==])
  f:close()
  assert(os.execute('chmod +x ' .. string.format('%q', tool)))
end

local function call(registry, name, arg)
  assert(type(registry[name]) == 'function', 'missing RPC: ' .. name)
  return json.decode(registry[name](arg))
end

local function wait_for(registry, predicate, seconds, arg)
  local deadline = os.time() + (seconds or 5)
  while os.time() <= deadline do
    local state = call(registry, 'SlsCompressorGet', arg)
    if predicate(state) then return state end
    os.execute('sleep 0.15')
  end
  error('timed out waiting for the backend')
end

local function idle(registry)
  local state = call(registry, 'SlsCompressorGet')
  if state.job and state.job.running then
    call(registry, 'SlsCompressorCancel')
  end
end

local function read(path)
  local f = io.open(path, 'rb')
  if not f then return nil end
  local data = f:read('*a'); f:close()
  return data
end

local function fixture_signal(signal, id, group, job)
  id = tonumber(id)
  assert(id and id == math.floor(id) and id > 1, 'unsafe fixture signal target')
  local function fields(pid)
    local stat = read('/proc/' .. pid .. '/stat')
    local result = {}
    if stat then
      for field in (stat:match('%)%s*(.+)') or ''):gmatch('%S+') do result[#result + 1] = field end
    end
    return result
  end
  local self = fields('self')
  assert(id ~= tonumber(read('/proc/self/stat'):match('^(%d+)'))
    and (not group or id ~= tonumber(self[3])), 'fixture cannot signal its caller')
  local target = fields(id)
  if job then
    assert(id == (group and job.pgid or job.pid) and job.pgid == job.pid)
    if target[1] and target[1] ~= 'Z' then
      assert(target[20] == job.procStart and tonumber(target[3]) == job.pgid
        and tonumber(target[4]) == job.pgid, 'fixture leader identity changed')
    else
      assert(group, 'fixture leader is not alive')
      local ps = assert(io.popen('ps -eo pid=,pgid=,sid=,stat=', 'r'))
      local owned = false
      for line in ps:lines() do
        local pid, pgid, sid, state = line:match('^%s*(%d+)%s+(%d+)%s+(%d+)%s+(%S+)')
        if tonumber(pgid) == id and tonumber(sid) == id and state:sub(1, 1) ~= 'Z' then
          local env = read('/proc/' .. pid .. '/environ') or ''
          if env:find('LUNA_COMPRESSOR_TOKEN=' .. job.token .. '\0', 1, true) then owned = true end
        end
      end
      ps:close()
      assert(owned, 'no verified fixture member remains')
    end
  else
    assert(target[1] and tonumber(target[3]) == id and tonumber(target[4]) == id,
      'escaped fixture is not its own session leader')
    local env = read('/proc/' .. id .. '/environ') or ''
    assert(env:find('LUNA_COMPRESSOR_TOKEN=', 1, true), 'not a compressor fixture')
  end
  -- Never `/bin/kill -SIG -ID`: procps-ng (Ubuntu) misparses it and can hit -1.
  assert(id > 1, 'refusing to signal pid/pgid <= 1')
  return os.execute('/bin/kill -s ' .. signal .. ' -- ' .. (group and '-' or '') .. id)
end

local reg = {}
local plugin = assert(loadfile(repo .. '/addons/compressor/plugin/backend/main.lua'))()
assert(type(plugin) == 'table' and type(plugin.register) == 'function')
plugin.register(reg)

-- Group signals use the one form every kill implementation reads the same way.
do
  assert(plugin.group_kill_command('TERM', 4242) == '/bin/kill -s TERM -- -4242')
  for _, bad in ipairs({0, 1, -5, 'x'}) do
    assert(not pcall(plugin.group_kill_command, 'TERM', bad), 'unsafe kill target accepted: ' .. tostring(bad))
  end
  local src = read(repo .. '/addons/compressor/plugin/backend/main.lua')
  assert(not src:find("/bin/kill %-[A-Z]+ %-'"), 'a `/bin/kill -SIG -PGID` form came back')
end

-- The log tail is bounded and terminal-friendly: escape sequences and progress
-- redraws must not reach the panel.
do
  local path = home .. '/tail.log'
  local f = assert(io.open(path, 'wb'))
  for i = 1, 100 do f:write('line ' .. i .. '\n') end
  f:write('\27[32mok\27[0m\n')
  f:write('step 1\rstep 2\rstep 3\r\n')
  f:close()
  local out = plugin.tail(path, 5)
  assert(out:find('line 100', 1, true), 'the tail window is wrong')
  assert(not out:find('\27', 1, true), 'ANSI escapes leaked into the tail')
  assert(out:find('step 3', 1, true) and not out:find('step 1', 1, true),
    'a carriage-return redraw was not collapsed to its last frame')
end

-- An observe-only Get (status --live, the selftest) never starts the scan.
local data_dir = home .. '/.local/share/slssteam-integrations/compressor'
local function exists(path) local f = io.open(path, 'rb'); if f then f:close() end; return f ~= nil end
local OBSERVE = json.encode({ light = true, observe = true })
local looked = call(reg, 'SlsCompressorGet', OBSERVE)
assert(looked.success and looked.tool.found, 'observe must still report the tool')
assert(looked.autoStarted == false and looked.job == nil, 'an observe-only Get started a scan')
assert(not exists(data_dir .. '/attempt.at') and not exists(data_dir .. '/job.json'),
  'an observe-only Get wrote scan state')

-- First open: the tool is found and the read-only scan starts on its own.
local first = call(reg, 'SlsCompressorGet')
assert(first.success, 'Get failed')
assert(first.tool.found and first.tool.path == tool, 'the installed tool was not discovered')
assert(first.autoStarted == true and first.job and first.job.running and first.job.action == 'scan',
  'the first Get must start the read-only scan')

-- Observing the finished scan neither stamps it done nor adopts its report;
-- the next ordinary Get does both.
local seen = wait_for(reg, function(state) return state.job and not state.job.running end, nil, OBSERVE)
assert(seen.status == nil and not exists(data_dir .. '/job.done'), 'an observe-only Get adopted the report')
local done = wait_for(reg, function(state) return state.job and not state.job.running end)
assert(done.status and done.status.summary.pending == 1, 'the scan output was not adopted')
assert(done.status.games[1].name == 'A' and done.tool.version == '9.9.9', 'the report was not decoded')
assert(done.statusAt > 0, 'the scan time was not stamped')
local again = call(reg, 'SlsCompressorGet')
assert(again.autoStarted == false and again.status ~= nil, 'a later Get must not rescan')
-- The light shape (what the panel polls) drops the per-game list for speed,
-- but the plan and the summary must stay.
local light = call(reg, 'SlsCompressorGet', json.encode({light = true}))
assert(light.status and light.status.summary and light.status.games == nil,
  'the light response must drop the games list')
assert(light.plan and light.plan.compress == 1, 'the light response must keep the plan')

-- Defaults: safe operations on, nothing destructive, no filters.
assert(again.settings.compress == true and again.settings.dedupe == true, 'safe passes must default on')
assert(again.settings.assets == false and again.settings.assetProfile == 'balanced', 'alpha must default off')
assert(again.settings.maxSizeGb == 0 and #again.settings.excluded == 0, 'filters must default empty')
assert(again.plan.compress == 1 and again.plan.compacted == 1, 'the plan must count the scan')

-- Settings round-trip and mirroring into the tool's own config.
local saved = call(reg, 'SlsCompressorPutSettings', json.encode({
  compress = true, dedupe = false, eco = true, assets = true,
  assetProfile = 'quality', maxSizeGb = 20, keepLanguages = 'English, de, en',
  excluded = { 'B', 'A', 'A' },
}))
assert(saved.success and saved.settings.dedupe == false and saved.settings.eco == true, 'settings were not saved')
assert(saved.settings.maxSizeGb == 20 and saved.settings.assetProfile == 'quality', 'values were not kept')
assert(#saved.settings.excluded == 2 and saved.settings.excluded[1] == 'A', 'exclusions were not normalised')
assert(saved.settings.keepLanguages == 'de,en,english', 'languages were not normalised')
local tool_dir = home .. '/.config/btrfs-game-compressor'
assert(read(tool_dir .. '/throttle.conf') == '1\n', 'eco mode was not mirrored')
assert(read(tool_dir .. '/visual_target') == 'quality\n', 'the alpha profile was not mirrored')
assert(read(tool_dir .. '/max_size_mib') == '20480\n', 'the size ceiling was not mirrored')
assert(read(tool_dir .. '/exclude_games') == 'A\nB\n', 'the exclusion list was not mirrored')

-- Invalid values fall back to safe defaults without touching the mirror files
-- in a broken state, and a full replace is exactly what the panel sends.
local odd = call(reg, 'SlsCompressorPutSettings', json.encode({
  compress = true, dedupe = false, maxSizeGb = -4, assetProfile = 'nonsense',
  excluded = { 'bad/name', 'ok' },
}))
assert(odd.success and odd.settings.assetProfile == 'balanced', 'an unknown profile was accepted')
assert(odd.settings.maxSizeGb == 0, 'an invalid ceiling was kept')
assert(#odd.settings.excluded == 1 and odd.settings.excluded[1] == 'ok', 'an invalid exclusion was saved')
assert(read(tool_dir .. '/exclude_games') == 'ok\n', 'the mirror kept an invalid exclusion')

-- Turning alpha off pins the tool to native, and the plan follows the filters.
local off = call(reg, 'SlsCompressorPutSettings', json.encode({
  compress = true, dedupe = true, assets = false, maxSizeGb = 0,
  excluded = { 'A' },
}))
assert(off.settings.assets == false, 'alpha could not be turned off')
assert(read(tool_dir .. '/visual_target') == 'native\n', 'alpha off must pin visual_target to native')
assert(read(tool_dir .. '/max_size_mib') == '0\n', 'the ceiling was not cleared')
local planned = call(reg, 'SlsCompressorGet')
assert(planned.plan.excluded == 1 and planned.plan.compress == 0 and planned.plan.blocked == 1,
  'the plan must hold back the excluded game')

-- The run is game by game: the backend writes a queue and a driver, and the
-- driver invokes the tool once per game with machine-readable lines.
idle(reg)
assert(call(reg, 'SlsCompressorPutSettings', json.encode({
  compress = true, dedupe = true, assets = false, maxSizeGb = 0, excluded = {},
  runOrder = 'smallest',
})).success, 'queue settings refused')
local started = call(reg, 'SlsCompressorRun', json.encode({action = 'run'}))
assert(started.success and started.job.queue == 1, 'the queue run did not start')
local queued = wait_for(reg, function(state) return state.queue and state.queue.finished end, 20)
assert(queued.queue.total == 1 and queued.queue.done == 1, 'the driver did not finish the queue')
assert(queued.queue.saved == 12345678 * 1048576, 'the per-game savings were not carried, in bytes')
assert(queued.queue.phase == 'dedupe', 'the last phase was not observed')
local full_queue = call(reg, 'SlsCompressorGet')
assert(full_queue.queue and full_queue.queue.games and full_queue.queue.games[1].status == 'done',
  'the per-game state is missing')
assert(full_queue.queue.games[1].name == 'A'
  and full_queue.queue.games[1].saved == 12345678 * 1048576,
  'the per-game entry is wrong')
local light_queue = call(reg, 'SlsCompressorGet', json.encode({light = true}))
assert(light_queue.queue and light_queue.queue.games == nil, 'the light queue must drop the game list')

-- An empty queue (everything excluded) is refused with a clear reason.
assert(call(reg, 'SlsCompressorPutSettings', json.encode({
  compress = true, dedupe = true, assets = false, maxSizeGb = 0, excluded = { 'A' },
})).success, 'exclusion settings refused')
local empty = call(reg, 'SlsCompressorRun', json.encode({action = 'run'}))
assert(empty.success == false and tostring(empty.error):find('Nothing to do', 1, true) ~= nil,
  'an empty queue must be refused')

-- The queue is ordered by size when asked.
assert(call(reg, 'SlsCompressorPutSettings', json.encode({
  compress = true, dedupe = true, assets = false, maxSizeGb = 0, excluded = {},
  runOrder = 'largest',
})).success, 'run order settings refused')
assert(call(reg, 'SlsCompressorGet').settings.runOrder == 'largest', 'the run order was not kept')

-- Dedupe-only mode keeps the tool's library pass, not the queue.
assert(call(reg, 'SlsCompressorPutSettings', json.encode({
  compress = false, dedupe = true, assets = false, maxSizeGb = 0, excluded = {},
})).success, 'dedupe-only settings refused')
assert(call(reg, 'SlsCompressorRun', json.encode({action = 'run'})).success, 'dedupe-only run refused')
local dedupe_only = wait_for(reg, function(state) return state.job and not state.job.running end)
assert(dedupe_only.job.log:find('ARGS: --dedupe', 1, true), 'dedupe-only must run --dedupe')

assert(call(reg, 'SlsCompressorPutSettings', json.encode({
  compress = false, dedupe = false, assets = false, maxSizeGb = 0, excluded = {},
})).success, 'both-off settings refused')
local nothing = call(reg, 'SlsCompressorRun', json.encode({action = 'run'}))
assert(nothing.success == false, 'a run with nothing enabled must be refused')

-- The alpha inventory is its own read-only action.
assert(call(reg, 'SlsCompressorRun', json.encode({action = 'assets'})).success, 'the asset inventory was refused')
local assets = wait_for(reg, function(state) return state.job and not state.job.running end)
assert(assets.job.log:find('ARGS: --assets-all', 1, true), 'the asset inventory used the wrong command')

-- Guards: only the known actions, one job at a time, payloads stay small.
assert(call(reg, 'SlsCompressorRun', json.encode({action = 'nonsense'})).success == false,
  'an unknown action was accepted')
assert(call(reg, 'SlsCompressorRun', 'not json').success == false, 'a broken payload was accepted')
assert(call(reg, 'SlsCompressorRun', nil).success == false, 'a missing payload was accepted')
assert(call(reg, 'SlsCompressorRun', string.rep('x', 2000)).success == false, 'an oversized payload was accepted')
assert(call(reg, 'SlsCompressorPutSettings', 'nope').success == false, 'broken settings were accepted')
assert(call(reg, 'SlsCompressorRun', json.encode({action = 'assets'})).success, 'the asset inventory was refused')
assert(call(reg, 'SlsCompressorRun', json.encode({action = 'preview'})).success == false,
  'a second job was admitted while one was running')

-- Cancel fails closed when idle, and stops a live detached pass.
local live = wait_for(reg, function(state) return state.job and state.job.running and state.job.action == 'assets' end)
assert(live.job.pid >= 2, 'the live job has no pid')
assert(call(reg, 'SlsCompressorCancel').success, 'cancel refused a live job')
local stopped = wait_for(reg, function(state) return state.job and not state.job.running end)
assert(stopped.job.action == 'assets' and stopped.job.finishedAt > 0, 'the cancelled job was not observed as stopped')
os.execute('sleep 0.3')
assert(call(reg, 'SlsCompressorCancel').success == false, 'cancel with no job must fail closed')

-- A queue run can be cancelled too; the interrupted game stays visible and the
-- queue must not read as finished.
assert(call(reg, 'SlsCompressorPutSettings', json.encode({
  compress = true, dedupe = true, assets = false, maxSizeGb = 0, excluded = {},
})).success, 'queue settings refused for cancel')
assert(call(reg, 'SlsCompressorRun', json.encode({action = 'run'})).success, 'the queue run was refused')
-- Cancel while a game is genuinely in progress: waiting only for `active` also
-- matches the moment after the last game finished but before the driver writes
-- its finish line, which makes the cancel a no-op and the check below racy.
local qlive = wait_for(reg, function(state)
  local queue = state.queue
  return queue and queue.active and queue.current ~= nil and queue.done == 0
    and not queue.finished
end, 10)
assert(qlive.queue.current ~= nil, 'the queue run must be live when it is cancelled')
assert(call(reg, 'SlsCompressorCancel').success, 'cancel refused the queue')
local qstopped = wait_for(reg, function(state) return state.queue and not state.queue.active end, 15)
assert(qstopped.queue.finished == false, 'a cancelled queue must not read as finished')
assert(qstopped.queue.cancelled == true, 'the cancel marker must be reported')
-- The TERM does not always win: bash defers it while a game runs, so the driver
-- can still append its own final lines. The marker written before the signal
-- must keep that pass from reading as finished.
local queue_state = home .. '/.local/share/slssteam-integrations/compressor/queue.state'
do
  local late = io.open(queue_state, 'ab')
  late:write('done\t1\t1\t0\t0\nfinish\t1\t1\n')
  late:close()
end
local late = call(reg, 'SlsCompressorGet')
assert(late.queue.cancelled == true, 'a late finish must not clear the cancel marker')
assert(late.queue.finished == false, 'a driver that finished after the signal must not read as finished')

-- The preview is read-only and reports its own argv.
assert(call(reg, 'SlsCompressorRun', json.encode({action = 'preview'})).success, 'the preview was refused')
local preview = wait_for(reg, function(state) return state.job and not state.job.running end)
assert(preview.job.log:find('ARGS: --dry-run --pending', 1, true), 'the preview used the wrong command')
assert(preview.job.log:find('dry run: nothing changed', 1, true), 'the log tail lost the tool output')

-- ------------------------------------------------------------------- stats
-- The Stats tab reads the compressor's own ledgers and Luna's queue; nothing in
-- this pass runs the tool. The fixtures below use the documented row shapes.
local function write_file(path, content)
  local dir = path:match('^(.*)/[^/]*$')
  if dir then os.execute('mkdir -p ' .. string.format('%q', dir)) end
  local f = assert(io.open(path, 'wb'))
  f:write(content)
  f:close()
end

local tool_state_dir = home .. '/.local/state/btrfs-game-compressor'
-- state v2 row: path|name|stamp|original|on-disk|saved-label|saved MiB|orig MiB|disk MiB
write_file(tool_state_dir .. '/compressed_games.db',
  '# btrfs-game-compressor state v2\n'
  .. '/lib/Alpha|Alpha|1700000000|2.0G|1.0G|1.0G|1024|2048|1024\n'
  .. '/lib/Beta|Beta|1700000100|512M|512M|n/a|0|0|0\n'
  .. '/lib/Gamma|Gamma|1700000200|4.0G|3.5G|512M|512|4096|3584\n')
-- dedupe history row: stamp|path|before|a|b|after|c|d|saved
write_file(tool_state_dir .. '/dedupe_physical_history.db',
  '1700000000|/lib/Alpha|2097152000|1|2097152000|1048576000|1|1048576000|1048576000\n'
  .. '1700000300|/lib/Alpha|1048576000|1|1048576000|943718400|1|943718400|104857600\n'
  .. '1700000400|/lib/Beta|1048576|1|1048576|524288|1|524288|524288\n')

local parsed_state = plugin.parse_state_db(read(tool_state_dir .. '/compressed_games.db'))
assert(parsed_state.records == 3, 'every state row must be counted as a record')
assert(#parsed_state.measured == 2, 'only rows with measured MiB carry numbers')
assert(parsed_state.measured[1].original_bytes == 2048 * 1048576
  and parsed_state.measured[1].saved_bytes == 1024 * 1048576)

local parsed_dedupe = plugin.parse_dedupe_history(read(tool_state_dir .. '/dedupe_physical_history.db'))
assert(parsed_dedupe.passes == 3 and parsed_dedupe.directories == 2, 'dedupe passes were miscounted')
assert(parsed_dedupe.saved_bytes == 104857600 + 524288, 'the latest measurement per directory must win')
assert(parsed_dedupe.entries[1].path == '/lib/Alpha' and parsed_dedupe.entries[1].saved_bytes == 104857600,
  'the dedupe entries must be sorted by savings')

-- A finished queue pass leaves per-game savings behind, and the full Get joins
-- every source; the light shape never pays for the stats.
assert(call(reg, 'SlsCompressorPutSettings', json.encode({
  compress = true, dedupe = true, assets = false, maxSizeGb = 0, excluded = {},
})).success, 'stats settings refused')
assert(call(reg, 'SlsCompressorRun', json.encode({action = 'run'})).success, 'the stats queue run was refused')
wait_for(reg, function(state) return state.queue and state.queue.finished end, 20)
local with_stats = call(reg, 'SlsCompressorGet')
local stats = with_stats.stats
assert(stats, 'the full response must carry the stats')
assert(stats.compression.records == 3 and stats.compression.measured == 2, 'compression records are wrong')
assert(stats.compression.saved_bytes == (1024 + 512) * 1048576, 'compression savings are wrong')
assert(stats.compression.original_bytes == (2048 + 4096) * 1048576, 'compression originals are wrong')
assert(stats.compression.games[1].name == 'Alpha' and stats.compression.games[1].saved_bytes == 1024 * 1048576,
  'the compression chart must sort by savings')
assert(stats.dedupe.passes == 3 and stats.dedupe.saved_bytes == 104857600 + 524288, 'dedupe totals are wrong')
assert(stats.dedupe.newest == 1700000400, 'the newest dedupe stamp is wrong')
assert(stats.luna.saved_bytes == 12345678 * 1048576 and #stats.luna.games == 1,
  'Luna per-game savings were not joined')
assert(stats.luna.games[1].name == 'A'
  and stats.luna.games[1].saved_bytes == 12345678 * 1048576,
  'the Luna chart must list the compacted game')
assert(stats.library.games == 2 and stats.library.bytes == 15, 'library totals must come from the scan')
assert(call(reg, 'SlsCompressorGet', json.encode({light = true})).stats == nil,
  'the light shape must not carry the stats')

-- The measurement refresh is its own read-only action and runs the tool.
assert(call(reg, 'SlsCompressorRun', json.encode({action = 'recheck'})).success, 'the recheck was refused')
local rechecked = wait_for(reg, function(state) return state.job and not state.job.running end)
assert(rechecked.job.log:find('ARGS: --recheck', 1, true), 'the recheck used the wrong command')


-- --------------------------------------------------------------- read cache
local settings_file = home .. '/.local/share/slssteam-integrations/compressor/settings.json'
local cached = reg.SlsCompressorGet()
assert(reg.SlsCompressorGet() == cached, 'a settled backend must answer from the memo')
-- An explicit refresh always rebuilds, even with nothing changed.
local refreshed = call(reg, 'SlsCompressorGet', json.encode({refresh = true}))
assert(refreshed.success == true and refreshed.plan.total == 2, 'refresh must rebuild the shape')
-- A write from outside (another surface, a hand edit) invalidates at once.
do
  local f = io.open(settings_file, 'rb')
  local data = json.decode(f:read('*a')); f:close()
  data.runOrder = 'library'
  local out = io.open(settings_file, 'wb')
  out:write(json.encode(data)); out:close()
end
touch(settings_file)
assert(call(reg, 'SlsCompressorGet').settings.runOrder == 'library',
  'a changed settings file must invalidate the memo')

-- ------------------------------------------------------- machine contract
-- Every exit code and reason of the engine's contract lands on the right word,
-- a library refusal is never counted as failed games, and savings are never a
-- total that quietly includes an unmeasured game.
local dir = home .. '/.local/share/slssteam-integrations/compressor'
local cfg = home .. '/.config/btrfs-game-compressor'
local accept_file = cfg .. '/accept_snapshots_unknown'
local function mode(name) write_file(home .. '/.compressor-mode', name .. '\n') end
local function stat_size(path)
  local f = io.open(path, 'rb')
  if not f then return 0 end
  local n = f:seek('end'); f:close()
  return n
end
local function scanned(name)
  idle(reg)
  mode(name)
  os.remove(dir .. '/status.json')
  os.remove(dir .. '/status.at')
  assert(call(reg, 'SlsCompressorRun', json.encode({ action = 'scan' })).success, 'fixture scan refused')
  wait_for(reg, function(state) return state.job and not state.job.running end, 15)
  return call(reg, 'SlsCompressorGet', json.encode({ refresh = true }))
end
local function queue_run(name)
  mode(name)
  local launched = call(reg, 'SlsCompressorRun', json.encode({ action = 'run' }))
  assert(launched.success,
    'the queue run was refused for ' .. name)
  return wait_for(reg, function(state)
    return state.queue and state.job and state.job.pid == launched.job.pid and not state.job.running
  end, 30)
end

-- Per-game skips carry the engine's reason in plain words.
scanned('skip')
local skipped = queue_run('skip')
local skipped_game = skipped.queue.games[1]
assert(skipped_game.status == 'skipped' and skipped_game.reason == 'running',
  'a skip reason was lost: ' .. tostring(skipped_game.reason))
assert(skipped_game.reasonText:find('running', 1, true), 'the skip reason is not in plain words')
assert(skipped.queue.failed == 0 and skipped.queue.skipped == 1, 'a skip was counted as a failure')

scanned('nodatacow')
local nodatacow = queue_run('nodatacow')
assert(nodatacow.queue.games[1].reason == 'nodatacow'
  and nodatacow.queue.games[1].reasonText:find('NODATACOW', 1, true), 'nodatacow was not explained')

-- unmeasured, measured zero and an estimate are three different things.
scanned('unmeasured')
local unmeasured = queue_run('unmeasured').queue
assert(unmeasured.games[1].status == 'done' and unmeasured.games[1].unmeasured == true,
  'an unmeasured pass must never read as a measured zero')
assert(unmeasured.savings.measuredGames == 0 and unmeasured.savings.unmeasuredGames == 1
  and unmeasured.savings.measuredMib == 0, 'unmeasured games leaked into the measured sum')
assert(unmeasured.savings.text:find('not measured', 1, true), 'the savings line hides unmeasured games')

scanned('zero')
local zero = queue_run('zero').queue
assert(zero.games[1].unmeasured == false and zero.savings.measuredGames == 1
  and zero.savings.measuredMib == 0, 'a measured zero must count as a measurement')

scanned('estimate')
local estimate = queue_run('estimate').queue
assert(estimate.games[1].estimate == true and estimate.games[1].estimateMib == 900,
  'the unprivileged estimate was not read')
assert(estimate.savings.estimateGames == 1 and estimate.savings.estimateMib == 900
  and estimate.savings.measuredGames == 0, 'the estimate was mixed into the measurement')
assert(estimate.savings.text:find('estimate', 1, true), 'the estimate is not labelled')
write_file(dir .. '/run.log', string.rep('x', 600000))
local durable_estimate = call(reg, 'SlsCompressorGet', json.encode({refresh = true})).queue
assert(durable_estimate.savings.estimateMib == 900 and durable_estimate.savings.measuredGames == 0,
  'the estimate must survive loss of the log tail')

scanned('twogames')
local interrupted = queue_run('interrupt').queue
assert(interrupted.games[1].status == 'interrupted', 'exit 143 was not an interrupt')
assert(interrupted.failed == 0, 'an interrupted game was counted as failed')
assert(interrupted.outcome.kind == 'interrupted' and interrupted.outcome.game == 'A',
  'the page cannot say where the run was interrupted')
assert(not interrupted.finished and interrupted.games[2].status == 'pending',
  'interruption must leave the rest of the queue unfinished')

-- A library refusal (exit 5/6) means nothing was attempted: "not started", the
-- engine's next step, and the untouched games still pending.
local refusals = {
  { mode = 'locked', reason = 'locked' },
  { mode = 'snapshots', reason = 'snapshots' },
  { mode = 'snapunknown', reason = 'snapshots-unknown' },
  { mode = 'lowspace', reason = 'low-space' },
  { mode = 'spaceunknown', reason = 'space-unknown' },
  { mode = 'notbtrfs', reason = 'not-btrfs' },
  { mode = 'readonly', reason = 'read-only' },
}
for _, case in ipairs(refusals) do
  scanned('twogames')
  local queue = queue_run(case.mode).queue
  assert(queue.outcome and queue.outcome.kind == 'not-started',
    case.mode .. ' must report a run that never started')
  assert(queue.finished == false, case.mode .. ' must not read as a finished pass')
  assert(queue.outcome.reason == case.reason,
    case.mode .. ' mapped to ' .. tostring(queue.outcome.reason))
  assert(queue.failed == 0 and queue.done == 0,
    case.mode .. ' counted ' .. tostring(queue.failed) .. ' failed games')
  for _, game in ipairs(queue.games) do
    assert(game.status == 'pending', case.mode .. ' left ' .. game.name .. ' as ' .. tostring(game.status))
  end
  assert(queue.outcome.nextStep and #queue.outcome.nextStep > 0,
    case.mode .. ' has no concrete next step')
  if case.mode == 'locked' then
    assert(queue.outcome.nextStep:find('4242', 1, true), 'the lock holder pid is not named')
  end
end

-- A refusal after a completed game retains its measurement and reason.
write_file(dir .. '/queue.list', 'A\t/games/A\t10\nB\t/games/B\t10\n')
write_file(dir .. '/queue.state', 'start\t1\t2\t10\tA\t/games/A\n'
  .. 'done\t1\t2\t0\tCOMPACT|done|A|COMPACTED|123|1\n'
  .. 'start\t2\t2\t10\tB\t/games/B\n' .. 'done\t2\t2\t5\t\n')
write_file(dir .. '/run.log', 'RUN\t1700000000\nREFUSED|snapshots|/mnt/lib|snapshot detail\n')
local partial = call(reg, 'SlsCompressorGet', json.encode({ refresh = true })).queue
assert(partial.outcome.kind == 'stopped' and partial.done == 1
  and partial.savings.measuredMib == 123 and partial.games[2].status == 'pending')
assert(partial.outcome.reason == 'snapshots' and partial.outcome.path == '/mnt/lib'
  and partial.outcome.detail == 'snapshot detail' and partial.outcome.nextStep
  and partial.outcome.reasonText)

-- A rolling ETA from two polls of the engine's PROGRESS lines.
scanned('twogames')
mode('slow')
assert(call(reg, 'SlsCompressorRun', json.encode({ action = 'run' })).success, 'the slow run was refused')
local eta_state = wait_for(reg, function(state)
  return state.queue and state.queue.eta and state.queue.eta.seconds
end, 15)
assert(eta_state.queue.eta.game == 'A', 'the ETA is not attached to the current game')
assert(eta_state.queue.eta.rateBytes > 0, 'the ETA has no rolling rate')
call(reg, 'SlsCompressorCancel')
wait_for(reg, function(state) return not (state.queue and state.queue.active) end, 15)

-- Per-library capabilities, and a detected snapshot state with no toggle.
local plain = scanned('plain')
assert(plain.libraries and #plain.libraries == 2, 'libraries_detail was not surfaced')
assert(plain.libraries[1].notes[1]:find('free', 1, true), 'free space is not in plain words')
assert(plain.libraries[2].notes[1] == 'ignored: ext4', 'a non-btrfs library must read "ignored: ext4"')
assert(plain.libraries[2].reason == 'not-btrfs' and plain.libraries[2].eligible == false,
  'the ext4 library facts are wrong: ' .. tostring(plain.libraries[2].reason) .. '/'
  .. tostring(plain.libraries[2].eligible))
assert(plain.snapshots == nil, 'a snapshots=no library must not raise a banner')

local none = scanned('snapnone')
assert(none.snapshots and none.snapshots.choice, 'snapshots-unknown raised no banner')
assert(none.snapshots.choice.tool == 'none', 'snapshotTool=none was not read')
assert(none.snapshots.choice.title ==
  'Luna cannot see whether this library has snapshots without root.')
assert(none.snapshots.choice.text:find('No snapshot tool', 1, true), 'the safe case is not explained')
assert(none.snapshots.choice.button == 'Allow compression (writes the setting for you)')
assert(none.snapshots.choice.enabled == false, 'the setting must not read as enabled by default')
assert(none.snapshots.choice.paths[1] == '/lib')

local configured = scanned('snapconfigured')
assert(configured.snapshots.choice.tool == 'configured')
assert(configured.snapshots.choice.text:find('keeps the old copies', 1, true))
assert(configured.snapshots.choice.button == 'Compress anyway (keeps snapshots)')
assert(not configured.snapshots.choice.text:find('Check for snapshots yourself', 1, true))

local unknown_tool = scanned('snapunknown')
assert(unknown_tool.snapshots.choice.tool == 'unknown')
assert(unknown_tool.snapshots.choice.text:find('Check for snapshots yourself', 1, true))

local detected = scanned('snapyes')
assert(detected.snapshots and detected.snapshots.detected,
  'a positively detected snapshot state is not shown')
assert(detected.snapshots.detected.text:find('keep the old copies', 1, true))
assert(detected.snapshots.choice == nil, 'a detected snapshot state must not offer a toggle')

local nofield = scanned('nofield')
assert(nofield.libraries and nofield.libraries[1].snapshotTool == nil)
assert(nofield.snapshots == nil, 'an engine without snapshotTool must show today\'s view')

local nolib = scanned('nolib')
assert(nolib.libraries == nil and nolib.snapshots == nil, 'no libraries_detail must stay absent')

-- The light poll carries the library table and the banner too.
scanned('snapnone')
local light_lib = call(reg, 'SlsCompressorGet', json.encode({ light = true }))
assert(light_lib.libraries and light_lib.status.libraries_detail,
  'the light response must keep the library table')
assert(light_lib.snapshots and light_lib.snapshots.choice,
  'the light response must keep the snapshot banner')

-- The durable per-game record in queue.state is the source of truth. The log
-- tail may have scrolled past a game (a long queue), or the log may be gone; a
-- missing measurement is unmeasured, never a measured zero.
local function state_only(with_record)
  os.execute('mkdir -p ' .. string.format('%q', dir))
  local filler = string.rep('PROGRESS|compress|1|1000\n', 30000)
  write_file(dir .. '/run.log', 'RUN\t1700000000\n' .. filler)
  write_file(dir .. '/queue.list', 'A\t/games/A\t10\n')
  local state = 'start\t1\t1\t10\tA\t/games/A\n'
  state = state .. (with_record
    and 'done\t1\t1\t0\tCOMPACT|done|A|COMPACTED|12345|1\n'
    or 'done\t1\t1\t0\t\n')
  write_file(dir .. '/queue.state', state .. 'finish\t1\t1\n')
  return call(reg, 'SlsCompressorGet', json.encode({ refresh = true }))
end

local recorded = state_only(true)
assert(recorded.queue.games[1].savedMib == 12345
  and recorded.queue.games[1].saved == 12345 * 1048576,
  'the state record was ignored and the savings came from the log tail')
assert(recorded.queue.savings.measuredGames == 1 and recorded.queue.savings.measuredMib == 12345
  and recorded.queue.savings.measuredBytes == 12345 * 1048576
  and recorded.queue.savings.unmeasuredGames == 0,
  'a recorded measurement outside the log tail was lost')
assert(recorded.queue.savings.unit == 'MiB', 'the savings carry no unit')
assert(recorded.queue.saved == 12345 * 1048576, 'queue.saved must be bytes')

local lost = state_only(false)
assert(lost.queue.games[1].status == 'done' and lost.queue.games[1].unmeasured == true,
  'a done game without a recorded measurement must be unmeasured')
assert(lost.queue.savings.measuredGames == 0 and lost.queue.savings.unmeasuredGames == 1
  and lost.queue.savings.measuredMib == 0 and lost.queue.saved == 0,
  'a lost measurement turned into a measured zero')
write_file(dir .. '/queue.state', 'done\t1\t1\t0\t123\nfinish\t1\t1\n')
local legacy = call(reg, 'SlsCompressorGet', json.encode({refresh = true})).queue
assert(legacy.savings.measuredMib == 123 and legacy.games[1].saved == 123 * 1048576)
write_file(dir .. '/queue.state', 'done\t1\t1\t0\t0\nfinish\t1\t1\n')
assert(call(reg, 'SlsCompressorGet', json.encode({refresh = true})).queue.savings.unmeasuredGames == 1)

-- A new pass must not inherit the preceding refusal or its progress sample.
write_file(dir .. '/run.log', 'RUN\t1\nREFUSED|locked|/lib|old\nPROGRESS|compress|10|100\n')
local offset = stat_size(dir .. '/run.log')
write_file(dir .. '/queue.state', 'pass\tnew-pass\t' .. offset .. '\nstart\t1\t1\t10\tA\t/games/A\n')
local fresh = call(reg, 'SlsCompressorGet', json.encode({refresh = true})).queue
assert(fresh.outcome.kind == 'stopped' and fresh.refusal == nil and fresh.eta == nil)
local real_time = os.time
local clock = real_time()
os.time = function(...) if select('#', ...) > 0 then return real_time(...) end return clock end
write_file(dir .. '/run.log', 'PROGRESS|compress|10|100\n')
write_file(dir .. '/queue.state', 'pass\tpass-one\t0\nstart\t1\t1\t100\tA\t/games/A\n')
assert(call(reg, 'SlsCompressorGet', json.encode({refresh = true})).queue.eta == nil)
clock = clock + 2
write_file(dir .. '/run.log', 'PROGRESS|compress|20|100\n')
assert(call(reg, 'SlsCompressorGet', json.encode({refresh = true})).queue.eta ~= nil)
clock = clock + 2
write_file(dir .. '/queue.state', 'pass\tpass-two\t0\nstart\t1\t1\t100\tA\t/games/A\n')
write_file(dir .. '/run.log', 'PROGRESS|compress|30|100\n')
assert(call(reg, 'SlsCompressorGet', json.encode({refresh = true})).queue.eta == nil,
  'a new pass must not inherit an ETA sample')
write_file(dir .. '/queue.state', 'pass\tpass-two\t0\nstart\t2\t2\t100\tB\t/games/B\n')
clock = clock + 2
write_file(dir .. '/run.log', 'PROGRESS|compress|40|100\n')
assert(call(reg, 'SlsCompressorGet', json.encode({refresh = true})).queue.eta == nil,
  'a new game must not inherit an ETA sample')
os.time = real_time
write_file(dir .. '/queue.state', 'pass\tanother-pass\t' .. offset .. '\n'
  .. 'done\t1\t2\t3\tCOMPACT|skip|A|running|/games/A\nstart\t2\t2\t10\tB\t/games/B\n')
write_file(dir .. '/queue.list', 'A\t/games/A\t10\nB\t/games/B\t10\n')
mode('scanlong')
local scan_job = call(reg, 'SlsCompressorRun', json.encode({action = 'scan'}))
assert(scan_job.success)
local job_file = dir .. '/job.json'
local scan_record = read(job_file)
write_file(job_file, json.encode({action = 'run', pid = scan_job.job.pid, startedAt = os.time()}))
local running_after_skip = call(reg, 'SlsCompressorGet', json.encode({refresh = true})).queue
assert(running_after_skip.outcome.kind == 'running' and running_after_skip.games[1].status == 'skipped')
write_file(job_file, scan_record)
local scanning_after_skip = call(reg, 'SlsCompressorGet', json.encode({refresh = true}))
assert(scanning_after_skip.job.running and scanning_after_skip.job.action == 'scan'
  and scanning_after_skip.queue.outcome.kind == 'stopped',
  'a scan must not make an abandoned queue look running')
wait_for(reg, function(state) return state.job and not state.job.running end, 15)
mode('snapnone')

-- Nothing is written without the explicit click, and the click writes only the
-- one key: the other config files survive untouched.
write_file(cfg .. '/compress_level', 'zstd:3\n')
write_file(cfg .. '/min_gain_pct', '7\n')
assert(read(accept_file) == nil, 'the snapshot setting was written without a click')
local accepted = call(reg, 'SlsCompressorAcceptSnapshots', json.encode({ enabled = true }))
assert(accepted.success and accepted.value == 'true', 'the snapshot setting was not written')
assert(read(accept_file) == 'true\n', 'the file must contain exactly true')
assert(read(cfg .. '/compress_level') == 'zstd:3\n', 'an unrelated config key was clobbered')
assert(read(cfg .. '/min_gain_pct') == '7\n', 'an unrelated config key was clobbered')
assert(accepted.scanning == true, 'the status was not re-read after the write')
wait_for(reg, function(state) return state.job and not state.job.running end, 15)
local enabled = call(reg, 'SlsCompressorGet', json.encode({ refresh = true }))
assert(enabled.snapshots and enabled.snapshots.choice.enabled == true,
  'the banner does not reflect the engine\'s top-level setting')
assert(enabled.snapshots.choice.button == 'Turn the override off',
  'the allowed state must offer a revert')
assert(enabled.snapshots.choice.text:find('override is on', 1, true))
assert(call(reg, 'SlsCompressorAcceptSnapshots', json.encode({ enabled = true })).already == true,
  'a second press must not rewrite the setting')

local reverted = call(reg, 'SlsCompressorAcceptSnapshots', json.encode({ enabled = false }))
assert(reverted.success and reverted.value == 'false', 'the override could not be turned off')
assert(read(accept_file) == 'false\n', 'the file must contain exactly false')
assert(read(cfg .. '/compress_level') == 'zstd:3\n', 'the revert clobbered another config key')
wait_for(reg, function(state) return state.job and not state.job.running end, 15)
local off = call(reg, 'SlsCompressorGet', json.encode({ refresh = true }))
assert(off.snapshots and off.snapshots.choice.enabled == false
  and off.snapshots.choice.button == 'Allow compression (writes the setting for you)',
  'the banner did not return to the not-allowed state')
write_file(accept_file, 'true\n') -- External change after the last scan.
local stale = call(reg, 'SlsCompressorAcceptSnapshots', json.encode({ enabled = false }))
assert(stale.success and not stale.already and read(accept_file) == 'false\n',
  'a stale scan must not suppress the requested write')
wait_for(reg, function(state) return state.job and not state.job.running end, 15)

-- The accept payload is explicit only: an empty object or a typo must never
-- enable a safety override.
for _, bad in ipairs({ '{}', '{"enable":false}', '{"enabled":"no"}', '{"enabled":1}',
  'null', '', '[]' }) do
  local refused_payload = call(reg, 'SlsCompressorAcceptSnapshots', bad)
  assert(refused_payload.success == false and refused_payload.code == 'invalid-payload',
    'a malformed payload was accepted: ' .. bad)
end
assert(read(accept_file) == 'false\n', 'a malformed payload wrote the setting')
local bare_on = call(reg, 'SlsCompressorAcceptSnapshots', 'true')
assert(bare_on.success and bare_on.value == 'true' and read(accept_file) == 'true\n',
  'a bare boolean true must be accepted')
wait_for(reg, function(state) return state.job and not state.job.running end, 15)
local bare_off = call(reg, 'SlsCompressorAcceptSnapshots', 'false')
assert(bare_off.success and bare_off.value == 'false' and read(accept_file) == 'false\n',
  'a bare boolean false must be accepted')
wait_for(reg, function(state) return state.job and not state.job.running end, 15)

-- An observer cannot consume a durable reservation, even after its owner has
-- died. Include a stale state lock: observing must not reap that either.
mode('scanlong')
assert(call(reg, 'SlsCompressorRun', '{"action":"scan"}').success)
os.execute('sleep 0.3')
local observe_old = json.decode(read(job_file))
assert(call(reg, 'SlsCompressorAcceptSnapshots', '{"enabled":true}').success)
fixture_signal('KILL', observe_old.pgid, true, observe_old)
local observe_pending = wait_for(reg, function(s)
  return s.job and not s.job.running and s.rescan ~= nil
end, 15, OBSERVE)
assert(observe_pending.rescan.enabled == true and not observe_pending.autoStarted)
write_file(dir .. '/job.lock', '99999999 1 ' .. read('/proc/sys/kernel/random/boot_id'))
local function snapshot_compressor_dir()
  local command = 'LC_ALL=C find ' .. string.format('%q', dir)
    .. " -printf '%P\\t%s\\t%T@\\n' | LC_ALL=C sort"
  local pipe = assert(io.popen(command, 'r'))
  local result = pipe:read('*a')
  assert(pipe:close())
  return result
end
local before_observe = snapshot_compressor_dir()
local observing = call(reg, 'SlsCompressorGet', OBSERVE)
local after_observe = snapshot_compressor_dir()
assert(observing.rescan and observing.rescan.enabled and not observing.job.running)
assert(before_observe == after_observe, 'observe changed compressor directory names, sizes or mtimes')
assert(read(dir .. '/job.lock'), 'observe reaped the stale job lock')
os.remove(dir .. '/job.lock')
mode('snapnone')
local observed_recovery = wait_for(reg, function(s) return not s.rescan and s.job and not s.job.running end, 15)
assert(observed_recovery.status.accept_snapshots_unknown == true)
assert(call(reg, 'SlsCompressorAcceptSnapshots', '{"enabled":false}').success)
wait_for(reg, function(s) return not s.rescan and s.job and not s.job.running end, 15)

-- A delayed release must not unlink a later acquisition by the same process.
do
local exec_for_release = os.execute
local replacement
os.execute = function(command)
  if command:find('owner-release', 1, true) and not replacement then
    local owner = assert(read(dir .. '/job.lock'))
    replacement = owner:gsub('(%S+)%s*$', 'different-acquisition\n')
    os.remove(dir .. '/job.lock')
    write_file(dir .. '/job.lock', replacement)
  end
  return exec_for_release(command)
end
assert(call(reg, 'SlsCompressorGet', '{"refresh":true}').success)
os.execute = exec_for_release
assert(replacement and read(dir .. '/job.lock') == replacement,
  'a stale release removed a newer acquisition')
os.remove(dir .. '/job.lock')
end

-- A scan that captured the old setting cannot publish after an explicit
-- setting change. The reservation survives a backend reload and blocks runs.
mode('scanhandoff')
local old = call(reg, 'SlsCompressorRun', json.encode({action = 'scan'}))
assert(old.success)
os.execute('sleep 0.3') -- the fixture captures its configuration before sleeping
local old_record = read(job_file)
local queued = call(reg, 'SlsCompressorAcceptSnapshots', '{"enabled":true}')
assert(queued.success and queued.rescan and not queued.rescan.failed and not queued.scanning)
assert(read(accept_file) == 'true\n')
assert(not call(reg, 'SlsCompressorRun', '{"action":"run"}').success)
-- Leave the page closed: no backend call is made while the original scan ends.
local invocations = home .. '/.scan-invocations'
local function scans()
  local _, count = (read(invocations) or ''):gsub('\n', '')
  return count
end
local before_chain = scans()
for _ = 1, 150 do
  if read(home .. '/.successor-started') then break end
  os.execute('sleep 0.1')
end
assert(read(home .. '/.successor-started'), 'the successor never reached its hold point')
assert(scans() == before_chain + 1, 'the detached job did not start its successor without polling')
local chained_record = json.decode(read(job_file))
assert(chained_record.generation == queued.rescan.generation and chained_record.pid == old.job.pid,
  'the successor was not owned by the original detached job')
assert(not call(reg, 'SlsCompressorRun', '{"action":"scan"}').success,
  'an RPC started a duplicate while the chained scan was running')
assert(scans() == before_chain + 1, 'the RPC raced the chain into starting another scan')
local restarted = {}
assert(loadfile(repo .. '/addons/compressor/plugin/backend/main.lua'))().register(restarted)
local fresh = wait_for(restarted, function(s) return s.rescan and s.job
  and s.job.running and s.job.generation == s.rescan.generation end, 10)
assert(fresh.status.accept_snapshots_unknown == false, 'old scan was adopted while a refresh was pending')
write_file(home .. '/.successor-release', 'release\n')
local settled = wait_for(restarted, function(s) return not s.rescan and s.job and not s.job.running end, 15)
assert(settled.status.accept_snapshots_unknown == true, 'fresh scan was not adopted after restart')

-- Replaying the old job record and its report must not replace a newer report.
local old_job = json.decode(old_record)
local old_path = dir .. '/status.scan.' .. old_job.generation .. '.' .. old_job.reportId
write_file(old_path, '{"accept_snapshots_unknown":false,"version":"stale"}')
write_file(old_path .. '.ok', '')
write_file(job_file, old_record)
os.remove(dir .. '/job.done')
local replay = call(restarted, 'SlsCompressorGet', '{"refresh":true}')
assert(replay.status.accept_snapshots_unknown == true, 'an old generation replaced the fresh report')

-- Cancelling the old scan must still release its reserved successor.
mode('scanlong')
assert(call(restarted, 'SlsCompressorRun', '{"action":"scan"}').success)
os.execute('sleep 0.3')
assert(call(restarted, 'SlsCompressorAcceptSnapshots', '{"enabled":false}').success)
assert(call(restarted, 'SlsCompressorCancel').success)
local recovered = {}
assert(loadfile(repo .. '/addons/compressor/plugin/backend/main.lua'))().register(recovered)
local after_cancel = wait_for(recovered, function(s) return not s.rescan and s.job and not s.job.running end, 15)
assert(after_cancel.status.accept_snapshots_unknown == false, 'cancel lost the follow-up')
restarted = recovered

-- Simulate a detached spawn that never creates its pid file. A failed refresh
-- remains durable, visible after reload, and only an explicit scan retries it.
local execute = os.execute
os.execute = function(command)
  if command:match('^setsid env ') then return true end
  return execute(command)
end
local failed = call(restarted, 'SlsCompressorAcceptSnapshots', '{"enabled":true}')
os.execute = execute
assert(failed.success and failed.rescan and failed.rescan.failed)
assert(call(restarted, 'SlsCompressorGet', OBSERVE).rescan.failed,
  'observe did not report the failed reservation')
local reloaded = {}
assert(loadfile(repo .. '/addons/compressor/plugin/backend/main.lua'))().register(reloaded)
assert(call(reloaded, 'SlsCompressorGet', '{"refresh":true}').rescan.failed)
assert(not call(reloaded, 'SlsCompressorRun', '{"action":"preview"}').success)
assert(call(reloaded, 'SlsCompressorRun', '{"action":"scan"}').success)
local retried = wait_for(reloaded, function(s) return not s.rescan and s.job and not s.job.running end, 15)
assert(retried.status.accept_snapshots_unknown == true)
reg = reloaded
assert(call(reg, 'SlsCompressorAcceptSnapshots', '{"enabled":false}').success)
wait_for(reg, function(s) return not s.rescan and s.job and not s.job.running end, 15)

-- A cancelled fresh scan remains pending and asks for a deliberate retry.
mode('scanlong')
assert(call(reg, 'SlsCompressorAcceptSnapshots', '{"enabled":true}').success)
assert(call(reg, 'SlsCompressorCancel').success)
local cancelled_refresh = wait_for(reg, function(s) return s.rescan and s.rescan.failed end, 10)
assert(cancelled_refresh.rescan.failed:find('retry', 1, true))
assert(call(reg, 'SlsCompressorRun', '{"action":"scan"}').success)
wait_for(reg, function(s) return not s.rescan and s.job and not s.job.running end, 15)
assert(call(reg, 'SlsCompressorAcceptSnapshots', '{"enabled":false}').success)
wait_for(reg, function(s) return not s.rescan and s.job and not s.job.running end, 15)

-- If the durable reservation cannot be committed, the setting stays untouched.
local rename = os.rename
os.rename = function(from, to)
  if to == dir .. '/rescan.json' then return nil end
  return rename(from, to)
end
local refused_reservation = call(reg, 'SlsCompressorAcceptSnapshots', '{"enabled":true}')
os.rename = rename
assert(not refused_reservation.success and refused_reservation.code == 'rescan-reservation')
assert(read(accept_file) == 'false\n')

-- A poll and an explicit scan arriving at the old scan's exit race the
-- detached handoff. The ownership lock permits only one successor.
mode('scanlong')
local race_before = scans()
assert(call(reg, 'SlsCompressorRun', '{"action":"scan"}').success)
os.execute('sleep 0.3')
assert(call(reg, 'SlsCompressorAcceptSnapshots', '{"enabled":true}').success)
os.execute('sleep 1.5')
for _ = 1, 5 do
  call(reg, 'SlsCompressorGet', '{"refresh":true}')
  assert(not call(reg, 'SlsCompressorRun', '{"action":"scan"}').success)
  os.execute('sleep 0.1')
end
local raced = wait_for(reg, function(s) return not s.rescan and s.job and not s.job.running end, 15)
assert(raced.status.accept_snapshots_unknown == true and scans() == race_before + 2,
  'the RPC and the chain must launch exactly one successor')
assert(call(reg, 'SlsCompressorAcceptSnapshots', '{"enabled":false}').success)
wait_for(reg, function(s) return not s.rescan and s.job and not s.job.running end, 15)

-- Recovery of an owner killed while holding the handoff lock.
write_file(dir .. '/job.lock', '99999999 1 ' .. read('/proc/sys/kernel/random/boot_id'))
assert(call(reg, 'SlsCompressorGet', '{"refresh":true}').success,
  'a dead lock owner prevented polling recovery')
assert(not read(dir .. '/job.lock'), 'dead owner lock was not released')

-- The detached leader can die while its child remains in the same session.
-- Do not start the reserved successor until the whole original group is gone.
mode('scanblock')
assert(call(reg, 'SlsCompressorRun', '{"action":"scan"}').success)
os.execute('sleep 0.4')
local orphan = json.decode(read(job_file))
assert(orphan.pgid == orphan.pid and orphan.procStart and orphan.token)
assert(call(reg, 'SlsCompressorAcceptSnapshots', '{"enabled":true}').success)
assert(fixture_signal('KILL', orphan.pid, false, orphan))
os.execute('sleep 0.2')
local count_before_orphan = scans()
local held = call(reg, 'SlsCompressorGet', '{"refresh":true}')
assert(held.job.running and held.job.pid == orphan.pid, 'orphaned scan group was considered dead')
assert(not call(reg, 'SlsCompressorRun', '{"action":"preview"}').success)
assert(scans() == count_before_orphan, 'a second scan started while the orphan was alive')
mode('snapnone')
assert(not call(reg, 'SlsCompressorCancel').success, 'cancel signalled an unverified group')
fixture_signal('TERM', orphan.pgid, true, orphan)
local orphan_done = wait_for(reg, function(s) return not s.rescan and s.job and not s.job.running end, 15)
assert(orphan_done.status.accept_snapshots_unknown == true)
assert(call(reg, 'SlsCompressorAcceptSnapshots', '{"enabled":false}').success)
wait_for(reg, function(s) return not s.rescan and s.job and not s.job.running end, 15)

-- A descendant that calls setsid is no longer in the wrapper's process group,
-- but still holds the inherited lifetime lock.
mode('scanescape')
assert(call(reg, 'SlsCompressorRun', '{"action":"scan"}').success)
os.execute('sleep 0.5')
local escaped_job = json.decode(read(job_file))
local escaped_pid = tonumber(read(home .. '/.escaped-scan') or '')
assert(escaped_pid, 'the escaped fixture did not start')
assert(call(reg, 'SlsCompressorAcceptSnapshots', '{"enabled":true}').success)
fixture_signal('KILL', escaped_job.pgid, true, escaped_job)
os.execute('sleep 0.2')
local escaped_count = scans()
local escaped_state = call(reg, 'SlsCompressorGet', '{"refresh":true}')
assert(escaped_state.job.running and scans() == escaped_count,
  'an escaped descendant allowed a second scan')
local observe_stuck = call(reg, 'SlsCompressorGet', OBSERVE)
assert(observe_stuck.job and observe_stuck.job.stuck and observe_stuck.rescan,
  'observe did not report the visible stuck holder and reservation')
do
  -- PID namespaces can hide an inherited flock from /proc/locks.
  local open = io.open
  io.open = function(path, mode)
    if path == '/proc/locks' then return open('/dev/null', mode) end
    return open(path, mode)
  end
  local hidden = call(reg, 'SlsCompressorGet', OBSERVE)
  io.open = open
  assert(hidden.job and hidden.job.stuck and hidden.rescan,
    'observe missed an inherited holder absent from /proc/locks')
end
assert(escaped_state.job.stuck and escaped_state.job.stuck.message:find(tostring(escaped_pid), 1, true),
  'the escaped holder is not identified with recovery guidance')
local saw_pid = false
for _, holder in ipairs(escaped_state.job.stuck.holders) do
  if holder == escaped_pid then saw_pid = true end
end
assert(saw_pid, 'the escaped holder PID was not returned')
local signalled = 0
local execute_for_cancel = os.execute
os.execute = function(command)
  if command:match('^/bin/kill ') then signalled = signalled + 1 end
  return execute_for_cancel(command)
end
local stuck_cancel = call(reg, 'SlsCompressorCancel')
os.execute = execute_for_cancel
assert(not stuck_cancel.success and stuck_cancel.code == 'stuck' and signalled == 0,
  'cancel claimed success or signalled a stuck holder')
-- Simulate recycling the old leader while the inherited lock is occupied.
local saved_escape_record = read(job_file)
local fake_pid = tonumber(read('/proc/self/stat'):match('^(%d+)'))
local fake = json.decode(saved_escape_record)
fake.pid, fake.pgid, fake.procStart = fake_pid, fake_pid, '1'
write_file(job_file, json.encode(fake))
signalled = 0
os.execute = function(command)
  if command:match('^/bin/kill ') then signalled = signalled + 1 end
  return execute_for_cancel(command)
end
local recycled_cancel = call(reg, 'SlsCompressorCancel')
os.execute = execute_for_cancel
assert(not recycled_cancel.success and signalled == 0, 'cancel signalled a recycled group')
write_file(job_file, saved_escape_record)
assert(not call(reg, 'SlsCompressorRun', '{"action":"preview"}').success)
fixture_signal('KILL', escaped_pid, true)
mode('snapnone')
local escaped_done = wait_for(reg, function(s) return not s.rescan and s.job and not s.job.running end, 15)
assert(escaped_done.status.accept_snapshots_unknown == true)
assert(call(reg, 'SlsCompressorAcceptSnapshots', '{"enabled":false}').success)
wait_for(reg, function(s) return not s.rescan and s.job and not s.job.running end, 15)

-- A live PID is not enough to own a persisted job or a handoff lock.
local self_pid = tonumber(read('/proc/self/stat'):match('^(%d+)'))
local boot = read('/proc/sys/kernel/random/boot_id'):match('^[%x%-]+')
write_file(job_file, json.encode({action='preview', pid=self_pid, pgid=self_pid,
  procStart='1', boot=boot, token='abcd', startedAt=os.time()}))
os.remove(dir .. '/job.done')
write_file(dir .. '/job.lock', self_pid .. ' 1 ' .. boot .. '\n')
local reused = call(reg, 'SlsCompressorGet', '{"refresh":true}')
assert(reused.success and not reused.job.running, 'a recycled PID claimed a job or lock')

local self_stat = read('/proc/self/stat')
local self_fields = {}
for field in self_stat:match('%)%s*(.+)'):gmatch('%S+') do self_fields[#self_fields + 1] = field end
local self_start = self_fields[20]
-- A successfully read, valid boot mismatch is affirmative stale ownership.
write_file(dir .. '/job.lock', self_pid .. ' ' .. self_start .. ' 00000000-0000-0000-0000-000000000000\n')
assert(call(reg, 'SlsCompressorGet', '{"refresh":true}').success)
assert(not read(dir .. '/job.lock'), 'a different boot ID did not release the stale lock')
write_file(job_file, json.encode({action='preview', pid=self_pid, pgid=self_pid,
  procStart=self_start, boot='00000000-0000-0000-0000-000000000000',
  token='abcd', startedAt=os.time()}))
os.remove(dir .. '/job.done')
assert(not call(reg, 'SlsCompressorGet', '{"refresh":true}').job.running,
  'a job from another boot was treated as live')

-- A live parent deliberately leaves its child unreaped, so /proc still holds
-- the zombie's original start time and boot identity.
local zombie_pid_file = home .. '/.zombie-pid'
local zombie_parent_file = home .. '/.zombie-parent'
local zombie_script = 'import os,time; p=os.fork(); p or os._exit(0); '
  .. 'open(os.environ["HOME"]+"/.zombie-pid","w").write(str(p)); time.sleep(30)'
assert(os.execute('python3 -c ' .. string.format('%q', zombie_script)
  .. ' </dev/null >/dev/null 2>&1 & echo $! > ' .. string.format('%q', zombie_parent_file)))
local zombie, zombie_fields
for _ = 1, 30 do
  zombie = tonumber(read(zombie_pid_file) or '')
  local zombie_stat = zombie and read('/proc/' .. zombie .. '/stat')
  zombie_fields = {}
  if zombie_stat then
    for field in zombie_stat:match('%)%s*(.+)'):gmatch('%S+') do zombie_fields[#zombie_fields + 1] = field end
  end
  if zombie_fields[1] == 'Z' then break end
  os.execute('sleep 0.1')
end
assert(zombie and zombie_fields[1] == 'Z', 'the fixture did not produce an unreaped zombie')
write_file(dir .. '/job.lock', zombie .. ' ' .. zombie_fields[20] .. ' ' .. boot .. '\n')
local reaped = call(reg, 'SlsCompressorGet', '{"refresh":true}')
local zombie_parent = tonumber(read(zombie_parent_file) or '')
if zombie_parent then
  assert(zombie_parent > 1 and tonumber(zombie_fields[2]) == zombie_parent)
  os.execute('/bin/kill -TERM ' .. zombie_parent)
end
assert(reaped.success and not read(dir .. '/job.lock'), 'validated zombie owner blocked the handoff')

-- Supply a fake /proc to the real reaper command. Read failures and malformed
-- stat data are not evidence that this live owner is dead.
local fakeproc = home .. '/fake-proc'
assert(os.execute('mkdir -p ' .. string.format('%q', fakeproc .. '/' .. self_pid)))
local real_execute = os.execute
for _, failure in ipairs({'boot', 'empty-boot', 'stat', 'malformed', 'boot-and-pid'}) do
  if failure == 'stat' or failure == 'boot-and-pid' then os.remove(fakeproc .. '/' .. self_pid .. '/stat')
  else write_file(fakeproc .. '/' .. self_pid .. '/stat', failure == 'malformed' and 'garbage\n' or self_stat) end
  if failure == 'boot-and-pid' then assert(os.execute('rmdir ' .. string.format('%q', fakeproc .. '/' .. self_pid))) end
  if failure == 'boot' or failure == 'boot-and-pid' then os.remove(fakeproc .. '/boot_id')
  elseif failure == 'empty-boot' then write_file(fakeproc .. '/boot_id', '')
  else write_file(fakeproc .. '/boot_id', boot .. '\n') end
  local owner = self_pid .. ' ' .. self_start .. ' ' .. boot .. '\n'
  write_file(dir .. '/job.lock', owner)
  local starts = 0
  os.execute = function(command)
    if command:match('^setsid env ') then starts = starts + 1 end
    if command == 'sleep 0.05' then return true end
    command = command:gsub('/proc/sys/kernel/random/boot_id', fakeproc .. '/boot_id')
      :gsub('/proc/%$owner/stat', fakeproc .. '/$owner/stat')
      :gsub('/proc/%$owner"', fakeproc .. '/$owner"')
    return real_execute(command)
  end
  local refused = call(reg, 'SlsCompressorRun', '{"action":"scan"}')
  os.execute = real_execute
  assert(not refused.success and starts == 0 and read(dir .. '/job.lock') == owner,
    'a failed ' .. failure .. ' read released a live owner')
  os.remove(dir .. '/job.lock')
end

-- Crash after publishing job.json but before publishing its ready marker.
-- The waiting wrapper times out; a later poll retries the reservation itself.
mode('snapnone')
local rename_for_crash = os.rename
os.rename = function(from, to)
  if to:find('/ready%.') then error('injected launch crash') end
  return rename_for_crash(from, to)
end
local survived = pcall(reg.SlsCompressorAcceptSnapshots, '{"enabled":true}')
os.rename = rename_for_crash
assert(not survived and read(dir .. '/rescan.json'), 'launch crash was not injected')
os.execute('sleep 6')
local recovered_start = wait_for(reg, function(s) return not s.rescan and s.job and not s.job.running end, 15)
assert(recovered_start.status.accept_snapshots_unknown == true, 'unreleased launch was not retried')
assert(call(reg, 'SlsCompressorAcceptSnapshots', '{"enabled":false}').success)
wait_for(reg, function(s) return not s.rescan and s.job and not s.job.running end, 15)

-- Two automatic retries are permitted; a third interrupted attempt is terminal.
mode('scanblock')
assert(call(reg, 'SlsCompressorAcceptSnapshots', '{"enabled":true}').success)
local exhausted
for attempt = 1, 3 do
  local record = json.decode(read(job_file))
  fixture_signal('KILL', record.pgid, true, record)
  local next_state = wait_for(reg, function(s)
    return s.rescan and (s.rescan.failed or (s.job and s.job.running and s.job.pid ~= record.pid))
  end, 15)
  if attempt < 3 then
    assert(not next_state.rescan.failed, 'recovery stopped before its retry limit')
  else
    exhausted = next_state
  end
end
assert(exhausted.rescan.failed and exhausted.rescan.retries == 3,
  'repeated interruptions did not become a terminal failure')
local exhausted_pid = exhausted.job.pid
local still_failed = call(reg, 'SlsCompressorGet', '{"refresh":true}')
assert(still_failed.rescan.failed and still_failed.job.pid == exhausted_pid,
  'a terminal failure was retried without user action')
mode('snapnone')
assert(call(reg, 'SlsCompressorRun', '{"action":"scan"}').success)
wait_for(reg, function(s) return not s.rescan and s.job and not s.job.running end, 15)
assert(call(reg, 'SlsCompressorAcceptSnapshots', '{"enabled":false}').success)
wait_for(reg, function(s) return not s.rescan and s.job and not s.job.running end, 15)

-- A failed final publication keeps the fresh reservation and reports retry.
do
assert(call(reg, 'SlsCompressorAcceptSnapshots', '{"enabled":true}').success)
local state_job = json.decode(read(job_file))
local state_path = dir .. '/status.scan.' .. state_job.generation .. '.' .. state_job.reportId
-- Redirect the destination to procfs, where the detached shell's actual mv
-- cannot create a file even when the tests run as root.
local saved_job = dir .. '/job.saved-test'
assert(os.rename(job_file, saved_job))
assert(os.execute('ln -s /proc/self ' .. string.format('%q', job_file)))
os.execute('sleep 1')
assert(read(state_path .. '.state-error'), 'the shell did not detect its failed final rename')
assert(os.remove(job_file))
assert(os.rename(saved_job, job_file))
local failed_state = call(reg, 'SlsCompressorGet', '{"refresh":true}')
assert(failed_state.rescan and failed_state.rescan.failed and failed_state.job.error,
  'failed final state publication was silently accepted')
assert(call(reg, 'SlsCompressorRun', '{"action":"scan"}').success)
wait_for(reg, function(s) return not s.rescan and s.job and not s.job.running end, 15)

assert(call(reg, 'SlsCompressorAcceptSnapshots', '{"enabled":false}').success)
local uncommitted_job = json.decode(read(job_file))
local uncommitted_path = dir .. '/status.scan.' .. uncommitted_job.generation .. '.' .. uncommitted_job.reportId
os.execute('sleep 1')
local rename_done = os.rename
os.rename = function(from, to)
  if to == dir .. '/job.done' then return nil end
  return rename_done(from, to)
end
local failed_done = call(reg, 'SlsCompressorGet', '{"refresh":true}')
local blocked_generation = failed_done.rescan.generation
local blocked_retry = call(reg, 'SlsCompressorRun', '{"action":"scan"}')
local still_reserved = json.decode(read(dir .. '/rescan.json'))
os.rename = rename_done
assert(failed_done.job.error and failed_done.rescan and failed_done.rescan.failed,
  'a failed completion write lost the reservation')
assert(not blocked_retry.success and still_reserved.generation == blocked_generation and still_reserved.failed,
  'a blocked retry replaced the reservation or reported success')
assert(read(uncommitted_path .. '.exit'), 'uncommitted scan evidence was removed')
assert(call(reg, 'SlsCompressorGet', '{"refresh":true}').rescan.failed,
  'a later poll cleared the failed reservation without a retry')
assert(call(reg, 'SlsCompressorRun', '{"action":"scan"}').success)
wait_for(reg, function(s) return not s.rescan and s.job and not s.job.running end, 15)
end

do
local function fixture_finished_scan(generation, id)
  local record = {action='scan', pid=99999999, pgid=99999999, procStart='1',
    boot=read('/proc/sys/kernel/random/boot_id'):match('^[%x%-]+'),
    token='abcd', startedAt=os.time(), generation=generation, reportId=id, closed=true}
  write_file(job_file, json.encode(record))
  os.remove(dir .. '/job.done')
  os.remove(dir .. '/job.error')
  local path = dir .. '/status.scan.' .. generation .. '.' .. id
  write_file(path, '{"version":"cancelled-report","accept_snapshots_unknown":false}')
  write_file(path .. '.ok', '')
  write_file(path .. '.exit', '0')
  write_file(path .. '.started', '')
  return path
end

for _, target in ipairs({'rescan.json', 'job.error'}) do
  local gen = tonumber(read(dir .. '/generation')) + 1
  write_file(dir .. '/generation', tostring(gen))
  local path = fixture_finished_scan(gen, target == 'job.error' and 'feed' or 'deaf')
  write_file(path .. '.state-error', '')
  if target == 'rescan.json' then
    write_file(dir .. '/rescan.json', json.encode({generation=gen, enabled=false}))
  end
  local rename = os.rename
  os.rename = function(from, to)
    if to == dir .. '/' .. target then return nil end
    return rename(from, to)
  end
  local count = scans()
  for _ = 1, 2 do
    local result = call(reg, 'SlsCompressorGet', '{"refresh":true}')
    assert(result.job.error and result.job.recoveryBlocked and not result.job.running)
    assert(not read(dir .. '/job.done') and read(path .. '.state-error')
      and read(path .. '.exit') and scans() == count, 'blocked recovery lost evidence or restarted')
  end
  os.rename = rename
  local recovered = call(reg, 'SlsCompressorGet', '{"refresh":true}')
  assert(recovered.job.error and not recovered.job.recoveryBlocked and read(dir .. '/job.done'))
  assert(not read(path .. '.state-error'), 'committed recovery did not clean evidence')
  if target == 'rescan.json' then
    assert(call(reg, 'SlsCompressorRun', '{"action":"scan"}').success)
    wait_for(reg, function(s) return not s.rescan and s.job and not s.job.running end, 15)
  end
end

-- Even a valid, successful report is not adoptable after cancellation.
local last_status = read(dir .. '/status.json')
local generation = tonumber(read(dir .. '/generation')) + 1
write_file(dir .. '/generation', tostring(generation))
local cancelled_path = fixture_finished_scan(generation, 'cafe')
write_file(cancelled_path .. '.cancelled', 'true')
write_file(dir .. '/rescan.json', json.encode({generation=generation, enabled=false}))
local cancelled_report = call(reg, 'SlsCompressorGet', '{"refresh":true}')
assert(cancelled_report.rescan and cancelled_report.rescan.failed
  and read(dir .. '/status.json') == last_status,
  'a cancelled report was adopted or its reservation was cleared')
assert(not read(cancelled_path) and not read(cancelled_path .. '.started')
  and not read(cancelled_path .. '.exit') and not read(cancelled_path .. '.cancelled'),
  'committed cancelled attempts left per-attempt files')
assert(call(reg, 'SlsCompressorRun', '{"action":"scan"}').success)
wait_for(reg, function(s) return not s.rescan and s.job and not s.job.running end, 15)

-- A failed final-state write belongs to scan A, not the reserved successor B.
local old_generation = tonumber(read(dir .. '/generation')) + 1
local next_gen = old_generation + 1
write_file(dir .. '/generation', tostring(next_gen))
local failed_old_path = fixture_finished_scan(old_generation, 'babe')
write_file(failed_old_path .. '.state-error', '')
write_file(dir .. '/rescan.json', json.encode({generation=next_gen, enabled=false}))
local old_failure = call(reg, 'SlsCompressorGet', '{"refresh":true}')
assert(old_failure.rescan and not old_failure.rescan.failed
  and old_failure.job.running and old_failure.job.generation == next_gen,
  'a previous scan failure blocked its reserved successor')
wait_for(reg, function(s) return not s.rescan and s.job and not s.job.running end, 15)

-- Per-attempt artifacts survive only until a completed outcome is committed.
assert(call(reg, 'SlsCompressorRun', '{"action":"scan"}').success)
local cleanup_job = json.decode(read(job_file))
local cleanup_path = dir .. '/status.scan.' .. cleanup_job.generation .. '.' .. cleanup_job.reportId
wait_for(reg, function(s) return s.job and not s.job.running end, 15)
for _, suffix in ipairs({'', '.ok', '.started', '.exit', '.cancelled', '.state-error'}) do
  assert(not read(cleanup_path .. suffix), 'committed scan left ' .. suffix .. ' behind')
end
end

-- The portable Lua binding cannot fsync; its stated power-loss limitation is
-- part of the contract rather than a claim of transactional durability.
assert(read(repo .. '/addons/compressor/plugin/backend/main.lua'):find('power loss', 1, true))

-- An ordinary unsuccessful scan reports a structured failure, even if it
-- never supplied a useful diagnostic in its output.
mode('scanfail')
assert(call(reg, 'SlsCompressorRun', '{"action":"scan"}').success)
local scan_error = wait_for(reg, function(s) return s.job and not s.job.running end, 10)
assert(scan_error.job.error and scan_error.job.error:find('exit 7', 1, true),
  'a failed ordinary scan was not reported')
mode('snapnone')
assert(call(reg, 'SlsCompressorAcceptSnapshots', '{"enabled":false}').success)
wait_for(reg, function(s) return not s.rescan and s.job and not s.job.running end, 15)

-- A live queue rereads the setting per game, so it is refused mid-run.
scanned('twogames')
mode('slow')
assert(call(reg, 'SlsCompressorRun', json.encode({ action = 'run' })).success, 'the live run was refused')
wait_for(reg, function(state) return state.queue and state.queue.active end, 10)
local live_accept = call(reg, 'SlsCompressorAcceptSnapshots', json.encode({ enabled = true }))
assert(live_accept.success == false and live_accept.code == 'run-live',
  'the snapshot setting could be changed during a run')
assert(read(accept_file) == 'false\n', 'a refused mid-run press wrote the setting')
call(reg, 'SlsCompressorCancel')
wait_for(reg, function(state) return not (state.queue and state.queue.active) end, 15)

-- An override left on stays visible and reversible with no unknown library,
-- and a known snapshot state warns without needing snapshotTool.
local override = plugin.build_snapshots(
  { { path = '/lib', snapshots = 'no', snapshotTool = 'none' } }, true)
assert(override and override.choice and override.choice.enabled == true
  and override.choice.button == 'Turn the override off',
  'an override left on must remain visible and reversible')
assert(plugin.build_snapshots({}, true).choice.enabled == true)
assert(plugin.build_snapshots({}, false) == nil)
for _, other in ipairs({ 'configured', 'unknown', 'missing' }) do
  local mixed = plugin.build_snapshots({
    { path = '/a', snapshots = 'unknown', reason = 'snapshots-unknown', snapshotTool = 'none' },
    { path = '/b', snapshots = 'unknown', reason = 'snapshots-unknown',
      snapshotTool = other ~= 'missing' and other or nil },
  }, false)
  assert(mixed.choice.tool ~= 'none' and not mixed.choice.text:find('usually safe', 1, true)
    and #mixed.choice.paths == 2, 'mixed libraries need the strongest warning')
end
local known_snap = plugin.build_snapshots({ { path = '/lib', snapshots = 'yes' } }, false)
assert(known_snap and known_snap.detected and known_snap.choice == nil,
  'a known snapshot state must warn without snapshotTool')

-- Log rotation: the grown pass log is kept whole as run.log.1, never truncated,
-- and a live run is never rotated out from under itself.
scanned('plain')
local qlog = dir .. '/run.log'
local qprev = qlog .. '.1'
write_file(qprev, 'previous audit')
local rename = os.rename
os.rename = function() return nil end
local rotated_fail, _, fail_reason = plugin.rotate_log(0)
os.rename = rename
assert(not rotated_fail and fail_reason == 'rename-failed' and read(qprev) == 'previous audit')
os.remove(qprev)
assert(os.execute('dd if=/dev/zero of=' .. string.format('%q', qlog) .. ' bs=1048576 count=9 status=none'))
local grown = stat_size(qlog)
assert(grown >= 9 * 1048576, 'the grown fixture log is wrong')
mode('ok')
assert(call(reg, 'SlsCompressorRun', json.encode({ action = 'run' })).success, 'the rotation run was refused')
wait_for(reg, function(state) return state.queue and state.queue.finished end, 30)
assert(stat_size(qprev) == grown, 'the previous pass log was not kept whole')
assert(stat_size(qlog) < grown, 'the current log was not rotated')
local health = call(reg, 'SlsCompressorGet', json.encode({ refresh = true })).health
assert(health and health.logBytes == stat_size(qlog), 'Health reports the wrong log size')
assert(health.logLimitBytes == 8 * 1048576 and health.logKept == true, 'the log cap is not reported')

mode('slow')
assert(call(reg, 'SlsCompressorRun', json.encode({ action = 'run' })).success, 'the live run was refused')
wait_for(reg, function(state) return state.queue and state.queue.active end, 10)
local kept = stat_size(qprev)
local rotated, _, why = plugin.rotate_log(0)
assert(rotated == false and why == 'live', 'a live pass must never be rotated')
assert(stat_size(qprev) == kept, 'a live pass was rotated')
call(reg, 'SlsCompressorCancel')
wait_for(reg, function(state) return not (state.queue and state.queue.active) end, 15)

print('Luna compressor backend OK: discovery, jobs, settings, mirroring, composition, guards, cancel, stats, machine contract, snapshots, rotation')
