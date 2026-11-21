-- SLS Playtime schema-3 merge smoke test: two simulated machines share one
-- data directory (the Syncthing case) while each keeps its own contribution.
--
-- No real Steam data: HOME is disposable (enforced by the runner), the Steam
-- root is a synthetic fixture, and the clock is fake so suspend and clock
-- jumps are deterministic.
assert(os.getenv('LUNA_TEST_HOME') == os.getenv('HOME'), 'Test must run in isolated HOME')

local REAL_HOME = os.getenv('HOME')
local REPO = os.getenv('LUNA_REPO') or '.'

-- --- a real JSON codec (the backend round-trips its own payloads) -----------
local function skip(text, pos) return (text:find('[^ \t\r\n]', pos) or (#text + 1)) end
local function decode_value(text, pos)
  pos = skip(text, pos)
  local c = text:sub(pos, pos)
  if c == '{' then
    local out = {}
    pos = skip(text, pos + 1)
    if text:sub(pos, pos) == '}' then return out, pos + 1 end
    while true do
      local key; key, pos = decode_value(text, pos)
      pos = skip(text, pos) + 1
      local value; value, pos = decode_value(text, pos)
      out[key] = value
      pos = skip(text, pos)
      if text:sub(pos, pos) == '}' then return out, pos + 1 end
      pos = pos + 1
    end
  end
  if c == '[' then
    local out = {}
    pos = skip(text, pos + 1)
    if text:sub(pos, pos) == ']' then return out, pos + 1 end
    while true do
      local value; value, pos = decode_value(text, pos)
      out[#out + 1] = value
      pos = skip(text, pos)
      if text:sub(pos, pos) == ']' then return out, pos + 1 end
      pos = pos + 1
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
    parent_path = function(path) return (path:match('^(.*)/[^/]*$')) or '.' end,
    exists = function(path)
      local f = io.open(path, 'rb')
      if f then f:close(); return true end
      return false
    end,
    join = function(...) return table.concat({ ... }, '/') end,
    -- Real listing: contribution files must be discovered on disk, exactly as
    -- the host's fs shim does. -p marks directories with a trailing slash.
    list = function(path)
      local pipe = io.popen('ls -1ap ' .. string.format('%q', path) .. ' 2>/dev/null')
      if not pipe then return {} end
      local out = {}
      for name in pipe:lines() do
        if name ~= '.' and name ~= '..' then
          local entry, directory = name, name:sub(-1) == '/'
          if directory then entry = name:sub(1, -2) end
          out[#out + 1] = { name = entry, is_directory = directory }
        end
      end
      pipe:close()
      return out
    end,
  }
end
package.preload['millennium'] = function()
  return { steam_path = function() return os.getenv('HOME') .. '/steam' end }
end

-- --- environment stubs: HOME per world, host id per machine, fake clock -----
local REAL_GETENV = os.getenv
local ENV = { home = REAL_HOME, host = nil }
os.getenv = function(name)
  if name == 'HOME' then return ENV.home end
  if name == 'SLS_PLAYTIME_HOST' then return ENV.host end
  if name == 'SLS_PLAYTIME_TRACK_MANIFESTS' then return '1' end
  return REAL_GETENV(name)
end
local CLOCK = 1760000000
local REAL_TIME = os.time
os.time = function(t)
  if t then return REAL_TIME(t) end
  return CLOCK
end

local json = require('json')
local ACCOUNT = '76561198000000000'
local function quote(path) return string.format('%q', path) end
local function mkdir(path) os.execute('mkdir -p ' .. quote(path)) end
local function write_file(path, text)
  mkdir(path:match('^(.*)/[^/]*$') or '.')
  local f = assert(io.open(path, 'wb'))
  f:write(text); f:close()
end
local function read_file(path)
  local f = io.open(path, 'rb')
  if not f then return nil end
  local text = f:read('*a'); f:close()
  return text
end
local function playtime_dir(home) return home .. '/.local/share/slssteam-integrations/playtime' end
local function baseline_path(home, account) return playtime_dir(home) .. '/' .. (account or ACCOUNT) .. '.json' end
local function contrib_path(home, host, account) return playtime_dir(home) .. '/' .. (account or ACCOUNT) .. '.' .. host .. '.json' end

-- Synthetic Steam root: one MostRecent account, one installed manifest and a
-- localconfig Playtime for app 620 (60 min), so officialSeconds is exercised.
local function write_steam(home)
  write_file(home .. '/steam/config/loginusers.vdf', [[
"users"
{
	"76561198000000000"
	{
		"AccountName"		"testaccount"
		"PersonaName"		"Tester"
		"MostRecent"		"1"
		"Timestamp"		"1791884517"
	}
}
]])
  write_file(home .. '/steam/steamapps/appmanifest_620.acf', [[
"AppState"
{
	"appid"		"620"
	"name"		"Portal 2"
	"SizeOnDisk"		"1000"
	"LastPlayed"		"0"
}
]])
  local account_number = tostring(tonumber(ACCOUNT) - 76561197960265728)
  write_file(home .. '/steam/userdata/' .. account_number .. '/config/localconfig.vdf', [[
"UserLocalConfigStore"
{
	"Software"
	{
		"Valve"
		{
			"Steam"
			{
				"apps"
				{
					"620"
					{
						"Playtime"		"60"
					}
				}
			}
		}
	}
}
]])
end

local function new_world(name)
  local home = REAL_HOME .. '/' .. name
  mkdir(home)
  ENV.home = home
  ENV.host = nil
  write_steam(home)
  CLOCK = 1760000000
  return home
end

local function machine(home, host)
  ENV.home = home
  ENV.host = host
  local plugin = assert(loadfile(REPO .. '/addons/playtime/plugin/backend/main.lua'))()
  local registry = {}
  plugin.register(registry)
  return registry
end

local function call(registry, name, arg)
  assert(type(registry[name]) == 'function', 'missing RPC: ' .. name)
  local raw = registry[name](arg)
  return json.decode(raw), raw
end
local function get(registry, extra)
  return call(registry, 'SlsPlaytimeGet', extra and json.encode(extra) or nil)
end
local function total(registry)
  local reply = get(registry)
  local sum = 0
  for _, record in pairs(reply.games or {}) do
    sum = sum + (tonumber(record.trackedSeconds) or 0) + (tonumber(record.adjustmentSeconds) or 0)
  end
  return sum
end
local function daily_total(registry)
  local reply = get(registry)
  local sum = 0
  for _, seconds in pairs(reply.daily30 or {}) do sum = sum + (tonumber(seconds.seconds) or 0) end
  return sum
end
local function start(registry, appid, sid, title)
  return call(registry, 'SlsPlaytimeHeartbeat', json.encode({ id = appid, kind = 'start', sid = sid, title = title }))
end
local function pulse(registry, appid, sid, seq, elapsed)
  return call(registry, 'SlsPlaytimeHeartbeat', json.encode({ id = appid, kind = 'pulse', sid = sid, seq = seq, elapsed = elapsed }))
end
local function stop(registry, appid, sid, seq, elapsed)
  return call(registry, 'SlsPlaytimeHeartbeat', json.encode({ id = appid, kind = 'stop', sid = sid, seq = seq, elapsed = elapsed }))
end

-- A ledger with the real file's shape: per-game totals, 200-pulse ring, 16
-- daily buckets, 1017 names, 64 ack records, official-base marker.
local function fixture_ledger(games, events, days, titles, acked)
  local ledger = { version = 2, account = ACCOUNT, officialBase = 1,
    games = {}, events = {}, daily = {}, titles = {}, active = {}, acked = {}, ackOrder = {} }
  for i = 1, games do
    local id = 1000 + i
    ledger.games[tostring(id)] = {
      title = 'Game ' .. i, trackedSeconds = i * 60,
      adjustmentSeconds = (i % 5 == 0) and 120 or 0,
      lastPlayed = CLOCK - 5000 + i,
    }
  end
  for i = 1, events do
    ledger.events[#ledger.events + 1] = {
      id = 1000 + (i % games) + 1, seconds = 10, endedAt = CLOCK - 100000 + i * 10,
    }
  end
  for i = 0, days - 1 do
    ledger.daily[os.date('%Y-%m-%d', CLOCK - i * 86400)] = 600 + i
  end
  for i = 1, titles do ledger.titles[tostring(900000 + i)] = 'Title ' .. i end
  for i = 1, acked do
    local sid = 'fixture-' .. i
    ledger.acked[sid] = i
    ledger.ackOrder[#ledger.ackOrder + 1] = sid
  end
  return ledger
end

local function fixture_totals(ledger)
  local games, daily = 0, 0
  for _, record in pairs(ledger.games) do
    games = games + record.trackedSeconds + record.adjustmentSeconds
  end
  for _, seconds in pairs(ledger.daily) do daily = daily + seconds end
  return games, daily
end

local passed = 0
local function test(name, fn)
  local ok, err = pcall(fn)
  if not ok then
    io.stderr:write('FAIL ' .. name .. ': ' .. tostring(err) .. '\n')
    os.exit(1)
  end
  passed = passed + 1
  print('ok ' .. passed .. ' - ' .. name)
end
local function expect(value, message)
  if not value then error(message or 'expectation failed', 2) end
end

-- 1. Migration. The schema-2 ledger becomes the read-only baseline exactly
--    once; totals are unchanged and its bytes are never touched.
test('migration keeps the baseline, its bytes and the totals', function()
  local home = new_world('migrate')
  local ledger = fixture_ledger(139, 200, 16, 1017, 64)
  local expected_games, expected_daily = fixture_totals(ledger)
  write_file(baseline_path(home), json.encode(ledger))
  local before = read_file(baseline_path(home))
  local a = machine(home, 'hosta')
  local reply = get(a)
  expect(reply.success, 'Get must succeed')
  expect(total(a) == expected_games, 'game totals changed by migration')
  expect(daily_total(a) == expected_daily, 'daily totals changed by migration')
  expect(#reply.daily30 == 30, 'daily30 must stay 30 days')
  expect(reply.stats and type(reply.stats.prev7d) == 'number', 'stats.prev7d missing')
  expect(reply.merge and reply.merge.hosts == 1 and reply.merge.host == 'hosta', 'merge info missing')
  expect(#reply.merge.conflictFiles == 0, 'no conflict copies expected')
  expect(read_file(baseline_path(home)) == before, 'the baseline must never be rewritten')
  local marker = read_file(playtime_dir(home) .. '/' .. ACCOUNT .. '.merge.json')
  expect(marker ~= nil, 'migration marker missing')
  -- idempotent: a second machine/reload sees the same marker and totals
  local b = machine(home, 'hostb')
  expect(total(b) == expected_games, 'second load changed the totals')
  expect(read_file(playtime_dir(home) .. '/' .. ACCOUNT .. '.merge.json') == marker,
    'migration must be idempotent')
  expect(read_file(baseline_path(home)) == before, 'the baseline must stay untouched')
  -- a new machine starts its own contribution rather than the shared file
  expect(read_file(contrib_path(home, 'hosta')) == nil, 'reading must not invent a contribution')
end)

-- 2. Two machines writing between syncs, in both orders: nothing is lost and
--    the totals converge. Neither writes the baseline.
test('both machines sessions survive in both write orders', function()
  for _, order in ipairs({ 'ab', 'ba' }) do
    local home = new_world('order-' .. order)
    local ledger = { version = 2, account = ACCOUNT, officialBase = 1,
      games = {}, events = {}, daily = {}, titles = {}, active = {}, acked = {}, ackOrder = {} }
    write_file(baseline_path(home), json.encode(ledger))
    local before = read_file(baseline_path(home))
    local a, b = machine(home, 'hosta'), machine(home, 'hostb')
    start(a, 620, 'a1', 'Portal 2')
    start(b, 620, 'b1', 'Portal 2')
    if order == 'ab' then
      pulse(a, 620, 'a1', 1, 10)
      pulse(b, 620, 'b1', 1, 20)
    else
      pulse(b, 620, 'b1', 1, 20)
      pulse(a, 620, 'a1', 1, 10)
    end
    expect(total(a) == 30, 'machine A lost the other machine session (' .. order .. ')')
    expect(total(b) == 30, 'machine B lost the other machine session (' .. order .. ')')
    expect(read_file(baseline_path(home)) == before, 'a contribution must not touch the baseline')
    expect(read_file(contrib_path(home, 'hosta')) ~= nil and read_file(contrib_path(home, 'hostb')) ~= nil,
      'each machine must have its own file')
  end
end)

-- 3. A Syncthing conflict copy of a host file is the same counter twice: it
--    must be merged by max, never summed.
test('a sync-conflict copy of a contribution is not double counted', function()
  local home = new_world('conflict-host')
  write_file(baseline_path(home), json.encode({ version = 2, account = ACCOUNT, officialBase = 1,
    games = {}, events = {}, daily = {}, titles = {}, active = {}, acked = {}, ackOrder = {} }))
  local a, b = machine(home, 'hosta'), machine(home, 'hostb')
  start(a, 620, 'a1'); pulse(a, 620, 'a1', 1, 10)
  start(b, 620, 'b1'); pulse(b, 620, 'b1', 1, 20)
  local source = read_file(contrib_path(home, 'hosta'))
  write_file(playtime_dir(home) .. '/' .. ACCOUNT .. '.hosta.sync-conflict-20261121-101500-KWHI6GR.json', source)
  expect(total(b) == 30, 'a duplicated contribution was counted twice')
  expect(total(a) == 30, 'the owning machine saw its own copy twice')
end)

-- 4. A machine still running the OLD add-on keeps rewriting the baseline. New
--    code folds that in exactly once, next to the contributions.
test('an old-version writer is folded in without double counting', function()
  local home = new_world('old-writer')
  write_file(baseline_path(home), json.encode({ version = 2, account = ACCOUNT, officialBase = 1,
    games = {}, events = {}, daily = {}, titles = {}, active = {}, acked = {}, ackOrder = {} }))
  local a, b = machine(home, 'hosta'), machine(home, 'hostb')
  start(a, 620, 'a1'); pulse(a, 620, 'a1', 1, 10)
  start(b, 620, 'b1'); pulse(b, 620, 'b1', 1, 20)
  -- old code: load the shared ledger, add its own 60 s, write the whole file
  local old = json.decode(read_file(baseline_path(home)))
  old.games['620'] = { title = 'Portal 2', trackedSeconds = 60, adjustmentSeconds = 0, lastPlayed = CLOCK }
  old.daily[os.date('%Y-%m-%d', CLOCK)] = 60
  write_file(baseline_path(home), json.encode(old))
  CLOCK = CLOCK + 31 -- the slow baseline reload beat
  expect(total(a) == 90, 'old-version sessions must be folded in (got ' .. total(a) .. ')')
  expect(total(b) == 90, 'the other machine must agree on the folded total')
  expect(read_file(contrib_path(home, 'hosta')) ~= nil, 'old code must not read/erase contributions')
end)

-- 5. A torn contribution is restored from .bak with a visible note, never
--    reset to zero; the bad bytes are kept.
test('a torn contribution is restored from .bak', function()
  local home = new_world('torn')
  write_file(baseline_path(home), json.encode({ version = 2, account = ACCOUNT, officialBase = 1,
    games = {}, events = {}, daily = {}, titles = {}, active = {}, acked = {}, ackOrder = {} }))
  local a = machine(home, 'hosta')
  start(a, 620, 'a1')
  pulse(a, 620, 'a1', 1, 10)
  pulse(a, 620, 'a1', 2, 10)
  pulse(a, 620, 'a1', 3, 10)
  write_file(contrib_path(home, 'hosta'), '{"version":3,"host":"hosta"')
  local again = machine(home, 'hosta')
  local reply = get(again)
  expect(reply.runtime and reply.runtime.note:find('recovered', 1, true), 'recovery must be visible in the note')
  expect(total(again) == 20, 'the last good contribution must be restored (got ' .. total(again) .. ')')
  local corrupt = io.popen('ls -1 ' .. quote(home .. '/.local/share/slssteam-integrations/corrupt') .. ' 2>/dev/null')
  expect(corrupt:read('*a') ~= '', 'the unreadable bytes must be kept, not deleted')
  corrupt:close()
end)

-- 6. Edits: last (at, host) wins for the whole directory, identically on both
--    machines. A later edit on the other machine supersedes it everywhere.
test('edits merge deterministically on both machines', function()
  local home = new_world('edits')
  write_file(baseline_path(home), json.encode({ version = 2, account = ACCOUNT, officialBase = 1,
    games = {}, events = {}, daily = {}, titles = {}, active = {}, acked = {}, ackOrder = {} }))
  local a, b = machine(home, 'hosta'), machine(home, 'hostb')
  CLOCK = CLOCK + 10
  expect(call(a, 'SlsPlaytimeEdit', json.encode({ id = 620, hours = 5 })).success, 'edit A failed')
  CLOCK = CLOCK + 10
  expect(call(b, 'SlsPlaytimeEdit', json.encode({ id = 620, hours = 7 })).success, 'edit B failed')
  local ga, gb = get(a).games['620'], get(b).games['620']
  expect(ga and gb, 'edited game missing')
  -- official base is 3600 s, so 7 h means adjustment 25200-3600
  expect(ga.adjustmentSeconds == 21600, 'edit A result wrong: ' .. tostring(ga.adjustmentSeconds))
  expect(gb.adjustmentSeconds == 21600, 'the two machines disagree on the edit winner')
  CLOCK = CLOCK + 10
  expect(call(a, 'SlsPlaytimeEdit', json.encode({ id = 620, hours = 9 })).success, 'second edit A failed')
  expect(get(b).games['620'].adjustmentSeconds == 28800, 'a later edit must win on the other machine')
end)

-- 7. Clock jumps and suspend must not create negative or absurd sessions.
test('clock jumps and suspend caps', function()
  local home = new_world('clock')
  write_file(baseline_path(home), json.encode({ version = 2, account = ACCOUNT, officialBase = 1,
    games = {}, events = {}, daily = {}, titles = {}, active = {}, acked = {}, ackOrder = {} }))
  local a = machine(home, 'hosta')
  start(a, 620, 'a1')
  expect(pulse(a, 620, 'a1', 1, 31).success == false, 'a pulse above 30 s must be rejected')
  expect(pulse(a, 620, 'a1', 1, -1).success == false, 'a negative pulse must be rejected')
  CLOCK = CLOCK - 3600 -- clock jumped back an hour
  expect(pulse(a, 620, 'a1', 1, 10).success, 'a backwards clock must not fail the session')
  expect(total(a) == 0, 'a backwards clock must not credit phantom time')
  CLOCK = CLOCK + 3600 + 5
  expect(pulse(a, 620, 'a1', 2, 30).success, 'normal pulse after recovery rejected')
  expect(total(a) == 30, 'a normal pulse must still count 30 s')
  CLOCK = CLOCK + 5
  expect(stop(a, 620, 'a1', 3, 5).success, 'stop failed')
  expect(total(a) == 35, 'stop must add the last interval')
end)

-- 8. rev poll: nothing changed returns a tiny unchanged reply, a change does
--    not.
test('rev poll returns unchanged only when nothing changed', function()
  local home = new_world('rev')
  write_file(baseline_path(home), json.encode({ version = 2, account = ACCOUNT, officialBase = 1,
    games = {}, events = {}, daily = {}, titles = {}, active = {}, acked = {}, ackOrder = {} }))
  local a = machine(home, 'hosta')
  local full = get(a)
  expect(type(full.rev) == 'string' and #full.rev > 0, 'full reply must carry a rev')
  local light, raw = get(a, { rev = full.rev })
  expect(light.unchanged == true and light.rev == full.rev, 'matching rev must be unchanged')
  expect(#raw < 2048, 'the unchanged reply must stay small (' .. #raw .. ' B)')
  start(a, 620, 'a1'); pulse(a, 620, 'a1', 1, 10)
  local after = get(a, { rev = full.rev })
  expect(after.unchanged ~= true and after.games, 'a change must return the full reply')
  local again = get(a, { rev = after.rev })
  expect(again.unchanged == true, 'the new rev must be unchanged again')
end)

-- 9. Export/import round trip through the merged view, including the other
--    machine converging on the imported numbers.
test('export and import round-trip the merged totals', function()
  local home = new_world('export')
  local ledger = fixture_ledger(20, 30, 10, 50, 5)
  write_file(baseline_path(home), json.encode(ledger))
  local a = machine(home, 'hosta')
  start(a, 620, 'a1'); pulse(a, 620, 'a1', 1, 15)
  local exported = call(a, 'SlsPlaytimeExport')
  expect(exported.success and exported.data, 'export failed')
  local before = total(a)
  local other = new_world('import')
  local b = machine(other, 'hostb')
  local imported = call(b, 'SlsPlaytimeImport', json.encode(exported.data))
  expect(imported.success, 'import failed: ' .. tostring(imported.error))
  expect(total(b) == before, 'imported totals differ from the export (' .. total(b) .. ' vs ' .. before .. ')')
  expect(get(b).stats.last7d == get(a).stats.last7d, 'imported daily stats differ')
  -- the exporting machine learns the import from the shared directory
  ENV.home = home
  ENV.host = 'hosta'
  local back = get(a)
  expect(total(a) == before, 'the import must not change the exporting machine totals')
  expect(back.games['620'], 'imported game record missing on the exporting machine')
end)

-- 10. Schema-2 sync-conflict copies of the shared ledger: recover only pulses
--     that are certainly missing, keep the file, report it.
test('old sync-conflict copies are recovered safely and reported', function()
  local home = new_world('conflict-ledger')
  local ledger = fixture_ledger(10, 200, 4, 5, 1)
  write_file(baseline_path(home), json.encode(ledger))
  local newest = 0
  for _, e in ipairs(ledger.events) do if e.endedAt > newest then newest = e.endedAt end end
  local conflict = json.decode(json.encode(ledger))
  -- newer than anything the baseline kept, and absent there: definitely missing
  conflict.events[#conflict.events + 1] = { id = 1003, seconds = 20, endedAt = newest + 10 }
  -- older than the newest baseline pulse: cannot be attributed safely
  conflict.events[#conflict.events + 1] = { id = 1004, seconds = 20, endedAt = newest - 5000 }
  local name = ACCOUNT .. '.sync-conflict-20261120-090000-KWHI6GR.json'
  write_file(playtime_dir(home) .. '/' .. name, json.encode(conflict))
  local before = read_file(baseline_path(home))
  local a = machine(home, 'hosta')
  local reply = get(a)
  local expected_games = fixture_totals(ledger)
  expect(total(a) == expected_games + 20, 'the identifiable missing pulse was not recovered')
  expect(reply.merge.recovered == 1, 'exactly one pulse must be recovered')
  expect(#reply.merge.conflictFiles == 1 and reply.merge.conflictFiles[1] == name,
    'the kept conflict copy must be reported')
  expect(read_file(playtime_dir(home) .. '/' .. name) ~= nil, 'the conflict copy must be kept')
  expect(read_file(baseline_path(home)) == before, 'the baseline must not be rewritten')
  -- idempotent: reloading does not add the same pulse again
  local b = machine(home, 'hostb')
  expect(total(b) == expected_games + 20, 'recovery ran twice')
  expect(get(b).merge.recovered == 1, 'recovery count changed on reload')
end)

print('playtime merge smoke: ' .. passed .. ' tests passed')
