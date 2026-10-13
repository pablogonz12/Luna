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

-- The standard install location is the discovery path under test.
local tool = home .. '/.local/bin/btrfs-game-compressor'
do
  assert(os.execute('mkdir -p ' .. string.format('%q', home .. '/.local/bin')))
  local f = assert(io.open(tool, 'wb'))
  f:write([==[
#!/usr/bin/env bash
echo "ARGS: $*" >&2
case "$1" in
  --status) sleep 0.3; printf '%s\n' '{"version":"9.9.9","libraries":["/lib"],"games":[{"name":"A","status":"UNCOMPRESSED","size_bytes":10,"size_display":"10B","dedupe_pending":true},{"name":"B","status":"COMPACTED","size_bytes":5,"size_display":"5B","dedupe_pending":false}],"summary":{"game_count":2,"pending":1,"compacted":1,"compressed":0,"compressed_only":0,"low_yield":0,"running":0,"skipped_empty":0}}' ;;
  --dry-run) echo 'dry run: nothing changed'; sleep 0.4 ;;
  --run) echo 'batch running'; sleep 2.5 ;;
  --dedupe) echo 'dedupe pass'; sleep 0.5 ;;
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

local function wait_for(registry, predicate, seconds)
  local deadline = os.time() + (seconds or 5)
  while os.time() <= deadline do
    local state = call(registry, 'SlsCompressorGet')
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

local reg = {}
local plugin = assert(loadfile(repo .. '/addons/compressor/plugin/backend/main.lua'))()
assert(type(plugin) == 'table' and type(plugin.register) == 'function')
plugin.register(reg)

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

-- First open: the tool is found and the read-only scan starts on its own.
local first = call(reg, 'SlsCompressorGet')
assert(first.success, 'Get failed')
assert(first.tool.found and first.tool.path == tool, 'the installed tool was not discovered')
assert(first.autoStarted == true and first.job and first.job.running and first.job.action == 'scan',
  'the first Get must start the read-only scan')

local done = wait_for(reg, function(state) return state.job and not state.job.running end)
assert(done.status and done.status.summary.pending == 1, 'the scan output was not adopted')
assert(done.status.games[1].name == 'A' and done.tool.version == '9.9.9', 'the report was not decoded')
assert(done.statusAt > 0, 'the scan time was not stamped')
local again = call(reg, 'SlsCompressorGet')
assert(again.autoStarted == false and again.status ~= nil, 'a later Get must not rescan')

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

-- Action composition from the settings. The fake tool records its argv, so the
-- composition is asserted without touching a library.
idle(reg)
assert(call(reg, 'SlsCompressorRun', json.encode({action = 'run'})).success, 'run refused')
local ran = wait_for(reg, function(state) return state.job and not state.job.running end)
assert(ran.job.log:find('ARGS: --run', 1, true), 'default run must use --run')
assert(ran.job.action == 'run', 'the finished job was not the run')

assert(call(reg, 'SlsCompressorPutSettings', json.encode({
  compress = true, dedupe = false, assets = false, maxSizeGb = 0, excluded = {},
})).success, 'compress-only settings refused')
assert(call(reg, 'SlsCompressorRun', json.encode({action = 'run'})).success, 'compress-only run refused')
local compress_only = wait_for(reg, function(state) return state.job and not state.job.running end)
assert(compress_only.job.log:find('ARGS: --run --no-dedupe', 1, true),
  'compress-only must pass --no-dedupe')

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

-- The preview is read-only and reports its own argv.
assert(call(reg, 'SlsCompressorRun', json.encode({action = 'preview'})).success, 'the preview was refused')
local preview = wait_for(reg, function(state) return state.job and not state.job.running end)
assert(preview.job.log:find('ARGS: --dry-run --pending', 1, true), 'the preview used the wrong command')
assert(preview.job.log:find('dry run: nothing changed', 1, true), 'the log tail lost the tool output')

print('Luna compressor backend OK: discovery, jobs, settings, mirroring, composition, guards, cancel')
