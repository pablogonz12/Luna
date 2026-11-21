-- SLS Luna HLTB backend smoke test: the local store, the user-triggered fetch
-- (token dance, expiry retry, rate limiting) and the name matching.
--
-- The network is replaced by a fake `http` module injected through
-- package.preload, so this test never talks to HowLongToBeat.
assert(os.getenv('LUNA_TEST_HOME') == os.getenv('HOME'), 'Test must run in isolated HOME')

-- A small real JSON codec: the backend round-trips its own payloads, so the
-- stub must encode and parse like the real one.
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
    parent_path = function(path) return (path:match('^(.*)/[^/]*$')) or '.' end,
    exists = function(path)
      local f = io.open(path, 'rb')
      if f then f:close(); return true end
      return false
    end,
    join = function(...) return table.concat({ ... }, '/') end,
    list = function(path)
      -- The hygiene/Health code needs a real listing of the data directory.
      local out = {}
      local pipe = io.popen('ls -1Ap ' .. string.format('%q', path) .. ' 2>/dev/null')
      if not pipe then return out end
      for line in pipe:lines() do
        if line ~= '' then
          local directory = line:sub(-1) == '/'
          out[#out + 1] = { name = directory and line:sub(1, -2) or line, is_directory = directory }
        end
      end
      pipe:close()
      return out
    end,
  }
end

local json = require('json')

-- Fake HowLongToBeat: it echoes the query back as the exact match, which is
-- what the site's ranking does for an unambiguous title. `mode` drives the
-- failure scenarios; the wire is recorded so the token dance can be asserted.
local wire = {
  mode = 'ok',
  expired_armed = false,
  get_calls = 0,
  post_calls = 0,
  last_token = nil,
}
function wire.get(url)
  wire.get_calls = wire.get_calls + 1
  assert(url:find('/api/search/site/init?t=', 1, true), 'init URL shape changed: ' .. tostring(url))
  return { status = 200, body = '{"token":"tok-' .. wire.get_calls .. '"}' }
end
function wire.post(url, body, options)
  wire.post_calls = wire.post_calls + 1
  wire.last_token = options and options.headers and options.headers['x-auth-token']
  assert(url == 'https://howlongtobeat.com/api/search/site', 'search URL shape changed: ' .. tostring(url))
  assert(type(body) == 'string' and body:find('"searchTerms"', 1, true), 'search payload shape changed')
  if wire.mode == 'expired' and wire.expired_armed then
    wire.expired_armed = false
    return { status = 403, body = '{"error":"forbidden"}' }
  end
  if wire.mode == 'ratelimit' then
    return { status = 429, body = '{"error":"slow down"}' }
  end
  if wire.mode == 'noname' then
    return { status = 200, body = '{"count":1,"data":[{"game_id":1,"game_name":"Orion Drift",' ..
      '"comp_main":3600,"comp_plus":7200,"comp_100":10800,"comp_all":4000}]}' }
  end
  if wire.mode == 'garbage' then
    return { status = 200, body = 'not json at all' }
  end
  if wire.mode == 'capture' then
    -- Verbatim rows captured from a real HowLongToBeat search for "Hades"
    -- (three of the five results), so the parser is exercised against the
    -- live response shape and its noise rows, not only against fixtures.
    return { status = 200, body = '{"count":5,"data":[' ..
      '{"game_id":37527,"game_name":"Hades","comp_main":84940,"comp_plus":175028,"comp_100":343055,"comp_all":159766},' ..
      '{"game_id":118387,"game_name":"Hades II","comp_main":115073,"comp_plus":192059,"comp_100":377989,"comp_all":201415},' ..
      '{"game_id":8328,"game_name":"Might & Magic Heroes VI: Shades of Darkness","comp_main":62520,' ..
      '"comp_plus":77940,"comp_100":0,"comp_all":68015}]}' }
  end
  local payload = json.decode(body)
  local query = table.concat(payload.searchTerms, ' ')
  return { status = 200, body = '{"count":1,"data":[{"game_id":1,"game_name":' .. json.encode(query) ..
    ',"comp_main":84940,"comp_plus":175028,"comp_100":343055,"comp_all":159766}]}' }
end
package.preload['http'] = function() return wire end

local home = os.getenv('HOME')
local repo = os.getenv('LUNA_REPO') or '.'
local store_path = home .. '/.local/share/slssteam-integrations/hltb.json'

-- The backend keys its payload cache on file stamps. No stat module is
-- installed here, so a tiny lfs stand-in supplies size + a modification time
-- the test can bump, which makes cache invalidation deterministic.
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

local backend = dofile(repo .. '/addons/luna/plugin/backend/main.lua')
local registry = {}
backend.register(registry)
-- The rate limiter is exercised explicitly below; keep it out of the way for
-- the rest of the scenarios.
backend.min_fetch_interval = 0

-- Lumen's dispatch turns an args object into positional arguments ordered by
-- sorted key, exactly like Millennium used to; RPCs are written for that.
local function call(name, arg)
  assert(type(registry[name]) == 'function', 'missing RPC: ' .. name)
  if type(arg) ~= 'table' then return json.decode(registry[name](arg)) end
  local keys = {}
  for key in pairs(arg) do
    if type(key) == 'string' then keys[#keys + 1] = key end
  end
  table.sort(keys)
  local vals = {}
  for index, key in ipairs(keys) do vals[index] = arg[key] end
  return json.decode(registry[name](table.unpack(vals, 1, #keys)))
end

local function read_store()
  local f = io.open(store_path, 'rb')
  if not f then return nil end
  local raw = f:read('*a'); f:close()
  return json.decode(raw)
end

-- ---------------------------------------------------------------- matching

assert(backend.normalize('Hades™') == 'hades', 'trademark noise must be stripped')
assert(backend.normalize('  Pokémon   Legends: Arceus ') == 'pokemon legends arceus',
  'accents must fold and spacing must collapse')
assert(backend.normalize('Ori & the Blind Forest') == 'ori and the blind forest',
  'an ampersand is the word "and"')
assert(backend.normalize('The Witcher 3') == 'witcher 3', 'a leading article is not part of a title')
assert(backend.normalize("Assassin's Creed") == 'assassins creed', 'apostrophes fold away')
assert(backend.normalize('') == '')

local score, quality = backend.name_score('Hades', 'Hades')
assert(score == 1 and quality == 'exact')
score, quality = backend.name_score('Hades', 'Hades II')
assert(quality == 'weak' and score < 0.6, 'a sequel must never score as a strong match')
score, quality = backend.name_score('The Witcher 3', 'The Witcher 3: Wild Hunt')
assert(quality == 'strong')
local weak, weak_quality = backend.name_score('Hades', 'Saint Seiya: The Hades')
assert(weak_quality == 'weak' and weak < 0.6, 'a token coincidence must stay below the trust floor')
assert(backend.pick_match({ { game_name = 'Saint Seiya: The Hades' } }, 'Hades') == nil,
  'a shared common word must not be stored')
assert(backend.pick_match({ { game_name = 'Hades II' }, { game_name = 'Hades' } }, 'Hades').game_name == 'Hades',
  'the exact title must win')
assert(backend.pick_match({ { game_name = 'Orion Drift' } }, 'Hades') == nil,
  'no confident match must return nil')
assert(backend.pick_match({ { game_name = 'Pokémon Legends: Arceus' } }, 'Pokemon Legends Arceus').game_name
  == 'Pokémon Legends: Arceus', 'accent-folded titles must still match')
-- A tie is broken by a release year only when both rows carry one.
assert(backend.pick_match({
  { game_name = 'Doom', release_world = 2016 },
  { game_name = 'Doom', release_world = 1993 },
}, 'Doom (2016)').release_world == 2016, 'a matching release year must break a tie')

-- Table-driven: every pair the store has been fooled by. `want` is true when
-- the candidate is the same game (an edition or a subtitle), false when it is a
-- sequel, another game or a non-game page.
local MATCH_CASES = {
  -- same game: exact, editions, subtitles, numeral spellings
  { 'Hades', 'Hades', true },
  { 'Portal', 'Portal', true },
  { 'DOOM', 'Doom', true },
  { 'The Witcher 3', 'Witcher 3: Wild Hunt', true },
  { 'Witcher 3', 'The Witcher 3: Wild Hunt', true },
  { 'Ori and the Blind Forest', 'Ori & the Blind Forest: Definitive Edition', true },
  { 'Dark Souls', 'Dark Souls: Remastered', true },
  { 'The Last of Us', 'The Last of Us Remastered', true },
  { 'Batman: Arkham City', 'Batman: Arkham City - Game of the Year Edition', true },
  { 'Sekiro: Shadows Die Twice', 'Sekiro: Shadows Die Twice - GOTY Edition', true },
  { 'Disco Elysium', 'Disco Elysium: The Final Cut', true },
  { 'Death Stranding', "Death Stranding Director's Cut", true },
  { 'The Elder Scrolls V: Skyrim', 'The Elder Scrolls V: Skyrim Special Edition', true },
  { 'Hollow Knight', 'Hollow Knight: Voidheart Edition', true },
  { 'Final Fantasy VII', 'Final Fantasy VII Remake', true },
  { "Assassin's Creed II", "Assassin's Creed 2", true },
  { 'Grand Theft Auto V', 'Grand Theft Auto 5', true },
  { 'Civilization VI', 'Civilization 6', true },
  { 'Resident Evil 4', 'Resident Evil 4 (2023)', true },
  { 'God of War', 'God of War (2018)', true },
  { 'Pokémon Legends: Arceus', 'Pokemon Legends Arceus', true },
  { 'NieR:Automata', 'Nier: Automata', true },
  { 'LISA', 'LISA: The Painful', true },
  { 'DuckMan', 'Duckman: The Graphic Adventures of a Private Dick', true },
  { 'Celeste', 'Celeste', true },
  -- sequels: the base game must never take its sequel's times, and vice versa
  { 'Hades', 'Hades II', false },
  { 'Hades', 'Hades 2', false },
  { 'Hades II', 'Hades', false },
  { 'Portal', 'Portal 2', false },
  { 'Portal 2', 'Portal', false },
  { 'Half-Life', 'Half-Life 2', false },
  { 'Half-Life 2', 'Half-Life 2: Episode One', false },
  { 'Half-Life 2', 'Half-Life 2: Episode 2', false },
  { 'Left 4 Dead', 'Left 4 Dead 2', false },
  { 'Dark Souls', 'Dark Souls II', false },
  { 'Dark Souls II', 'Dark Souls III', false },
  { 'The Witcher 2', 'The Witcher 3', false },
  { 'God of War', 'God of War II', false },
  { 'Mass Effect', 'Mass Effect 2', false },
  { 'Mass Effect 2', 'Mass Effect 3', false },
  { 'Dishonored', 'Dishonored 2', false },
  { 'Titanfall', 'Titanfall 2', false },
  { 'Overwatch', 'Overwatch 2', false },
  { 'Destiny', 'Destiny 2', false },
  { "Baldur's Gate", "Baldur's Gate II", false },
  { 'Age of Empires II', 'Age of Empires III', false },
  { 'Total War: Rome', 'Total War: Rome II', false },
  { 'Grand Theft Auto V', 'Grand Theft Auto VI', false },
  { 'Doom', 'DOOM Eternal', false },
  { 'Hades', "Hades' Star", false },
  { 'Football Manager 2024', 'Football Manager 2025', false },
  -- remakes with the same name are the same page; a year in brackets is noise
  { 'Resident Evil 4', 'Resident Evil 4 (2005)', true },
  -- other games that share words
  { 'Batman: Arkham Asylum', 'Batman: Arkham City', false },
  { 'Hades', 'Saint Seiya: The Hades', false },
  { 'Horizon Zero Dawn', 'Horizon Forbidden West', false },
  { 'Wild World', 'Animal Crossing: Wild World', false },
  -- non-game pages: a soundtrack, a demo, a playtest must be skipped
  { 'Hollow Knight', 'Hollow Knight: Gods & Glory Soundtrack', false },
  { 'Hades', 'Hades Original Soundtrack', false },
  { 'Undertale', 'Undertale Soundtrack', false },
  { 'Cyberpunk 2077', 'Cyberpunk 2077 Demo', false },
  { 'Half-Life: Alyx', 'Half-Life: Alyx Playtest', false },
  { 'Hades II', 'Hades II Playtest', false },
  { 'Terraria', 'Terraria Dedicated Server', false },
  { 'Cities: Skylines', 'Cities: Skylines - Expansion Pass', false },
}

local case_matched, case_refused = 0, 0
for _, case in ipairs(MATCH_CASES) do
  local value, kind = backend.name_score(case[1], case[2])
  local confident = value ~= nil and kind ~= 'weak' and value >= 0.6
  assert(confident == case[3], string.format(
    'matching table: "%s" vs "%s" expected %s, got %s (%s)',
    case[1], case[2], case[3] and 'match' or 'no match', tostring(value), tostring(kind)))
  local row = backend.pick_match({ { game_name = case[2] } }, case[1])
  assert((row ~= nil) == case[3], 'pick_match disagrees with name_score for: ' .. case[2])
  if case[3] then case_matched = case_matched + 1 else case_refused = case_refused + 1 end
end
assert(#MATCH_CASES >= 40, 'the matching table needs at least 40 real-world pairs')

-- ---------------------------------------------------------------- storage

local state = call('SlsHltbGet')
assert(state.success == true and state.count == 0 and next(state.games) == nil, 'the store must start empty')

local out = call('SlsHltbFetch', { appid = 1145360, name = 'Hades' })
assert(out.success == true, 'fetch failed: ' .. tostring(out.error))
assert(out.record.match == 'exact' and out.record.name == 'Hades')
assert(out.record.main == 84940 and out.record.extra == 175028 and out.record.completionist == 343055)
assert(out.record.source == 'hltb-search' and out.record.fetchedAt > 0)
assert(wire.get_calls == 1 and wire.post_calls == 1, 'one init and one search per fetch')

local stored = read_store()
assert(stored and stored.games['1145360'] and stored.games['1145360'].main == 84940,
  'the fetch must persist to hltb.json')

state = call('SlsHltbGet')
assert(state.count == 1 and state.games['1145360'].name == 'Hades')

-- A second backend instance (Steam restart) must see the same store.
local second = dofile(repo .. '/addons/luna/plugin/backend/main.lua')
local second_registry = {}
second.register(second_registry)
local second_state = json.decode(second_registry.SlsHltbGet())
assert(second_state.count == 1, 'the store must survive an add-on reload')

-- ---------------------------------------------------------------- fetch failure modes

wire.mode = 'expired'
wire.expired_armed = true
out = call('SlsHltbFetch', { appid = 1091500, name = 'Cyberpunk 2077' })
assert(out.success == true, 'a 403 must refresh the token and retry: ' .. tostring(out.error))
assert(wire.get_calls == 3 and wire.post_calls == 3, 'retry must fetch a fresh token and POST once more')
assert(wire.last_token == 'tok-3', 'the retry must use the refreshed token, got ' .. tostring(wire.last_token))

wire.mode = 'ratelimit'
local before = call('SlsHltbGet').count
out = call('SlsHltbFetch', { appid = 367520, name = 'Hollow Knight' })
assert(out.success == false and out.code == 'rate_limit' and out.error:find('rate%-limit') ~= nil,
  '429 must surface as a retryable rate-limit code')
assert(call('SlsHltbGet').count == before, 'a rate-limited fetch must not change the store')

wire.mode = 'noname'
out = call('SlsHltbFetch', { appid = 367520, name = 'Hollow Knight' })
assert(out.success == false and out.code == 'no_match', 'a weak match must be refused')
assert(out.error:find('No confident', 1, true) ~= nil)
local refused = call('SlsHltbGet')
assert(refused.count == before + 1, 'the refusal must be remembered once')
assert(refused.games['367520'] and refused.games['367520'].match == 'none')
assert(refused.games['367520'].main == nil, 'a no-match record carries no times')

wire.mode = 'garbage'
out = call('SlsHltbFetch', { appid = 70, name = 'Half-Life' })
assert(out.success == false and out.code == 'unavailable', 'a broken response is a hard failure')
assert(out.error:find('unexpected response', 1, true) ~= nil)
assert(call('SlsHltbGet').count == before + 1, 'a hard failure must not write a record')

wire.mode = 'ok'
out = call('SlsHltbFetch', { appid = 0, name = 'Hades' })
assert(out.success == false and out.code == 'invalid' and out.error:find('Invalid app id', 1, true) ~= nil)
out = call('SlsHltbFetch', { appid = 1145360, name = '   ' })
assert(out.success == false and out.code == 'invalid' and out.error:find('no name', 1, true) ~= nil)

-- ---------------------------------------------------------------- rate limiting

local posts_before = wire.post_calls
backend.min_fetch_interval = 600
out = call('SlsHltbFetch', { appid = 1091500, name = 'Cyberpunk 2077' })
assert(out.success == false and out.code == 'throttled' and out.error:find('Wait a moment', 1, true) ~= nil,
  'back-to-back fetches must be refused politely')
assert(wire.post_calls == posts_before, 'a refused fetch must not reach the network')
backend.min_fetch_interval = 0

-- ---------------------------------------------------------------- manual overrides

out = call('SlsHltbPut', { raw = json.encode({ appid = 367520, name = 'Hollow Knight',
  main = 26.5, extra = 39, completionist = 62 }) })
assert(out.success == true and out.record.match == 'manual' and out.record.source == 'manual')
assert(out.record.main == 95400 and out.record.extra == 140400 and out.record.completionist == 223200,
  'manual hours must be stored as seconds')
assert(out.record.all == nil)

out = call('SlsHltbPut', { raw = json.encode({ appid = 367520 }) })
assert(out.success == false and out.error:find('at least one', 1, true) ~= nil)
out = call('SlsHltbPut', { raw = 'not json' })
assert(out.success == false)

out = call('SlsHltbForget', { appid = 367520 })
assert(out.success == true and out.removed == true)
assert(call('SlsHltbGet').count == before, 'forget must remove exactly one record')
out = call('SlsHltbForget', { appid = 367520 })
assert(out.success == true and out.removed == false)

-- Real captured response shape: "Hades" must win over "Hades II" and the
-- unrelated noise row, and the seconds must map to the right buckets.
wire.mode = 'capture'
out = call('SlsHltbFetch', { appid = 1091500, name = 'Hades' })
assert(out.success == true, 'capture fetch failed: ' .. tostring(out.error))
assert(out.record.match == 'exact' and out.record.name == 'Hades')
assert(out.record.main == 84940 and out.record.extra == 175028 and out.record.completionist == 343055)
assert(out.record.all == 159766)

-- ---------------------------------------------------------------- library

local pushed = call('SlsHltbLibrary', { raw = json.encode({ games = {
  { appid = 1145360, name = 'Hades' },
  { appid = 620, name = 'Portal 2', shortcut = false },
  { appid = 3212112756, name = 'Minecraft', shortcut = true },
  { appid = 1145360, name = 'Hades again' },
  { appid = 0, name = 'bad' },
  { appid = 70, name = '' },
} }) })
assert(pushed.success == true and pushed.count == 3,
  'the inventory must dedupe and validate, got ' .. tostring(pushed.count))
local library_state = call('SlsHltbGet')
assert(type(library_state.library) == 'table' and #library_state.library == 3)
assert(library_state.libraryAt > 0)
local seen_names = {}
for _, entry in ipairs(library_state.library) do seen_names[entry.name] = entry end
assert(seen_names['Hades'] and seen_names['Hades'].name == 'Hades', 'the first duplicate must win')
assert(seen_names['Minecraft'] and seen_names['Minecraft'].shortcut == true)
assert(seen_names['Minecraft'].appid == 3212112756)
assert(seen_names['Portal 2'].shortcut == false)

local fourth = dofile(repo .. '/addons/luna/plugin/backend/main.lua')
local fourth_registry = {}
fourth.register(fourth_registry)
assert(#(json.decode(fourth_registry.SlsHltbGet()).library) == 3,
  'the inventory must survive an add-on reload')

out = call('SlsHltbLibrary', { raw = json.encode({ games = { { appid = 0, name = 'x' } } }) })
assert(out.success == false and out.error:find('No valid games', 1, true) ~= nil)
out = call('SlsHltbLibrary', { raw = 'not json' })
assert(out.success == false)
out = call('SlsHltbLibrary', { raw = json.encode({ games = 'nope' }) })
assert(out.success == false)

out = call('SlsLunaInfo')
-- The expected version is read from the payload itself: releases bump it and
-- the test must not need a matching edit.
local plugin_file = io.open(repo .. '/addons/luna/plugin/plugin.json', 'rb')
local plugin_json = plugin_file and plugin_file:read('*a') or ''
if plugin_file then plugin_file:close() end
local pinned = plugin_json:match('"version"%s*:%s*"([^"]+)"')
assert(pinned, 'the luna payload has no version to pin the test to')
assert(out.success == true and out.version == pinned and type(out.addons) == 'table')
assert(out.prefs and out.prefs.store_hide_followed == nil, 'prefs must start empty')

-- Preferences: the store switch writes here (the store page runs on another
-- origin and cannot read localStorage), values are whitelisted and typed.
out = call('SlsLunaPrefsSet', { raw = json.encode({ key = 'store_hide_followed', value = true }) })
assert(out.success == true and out.prefs.store_hide_followed == true,
  'the preference was not saved: ' .. tostring(out.error))
out = call('SlsLunaInfo')
assert(out.prefs.store_hide_followed == true, 'the preference did not persist')
out = call('SlsLunaPrefsSet', { raw = json.encode({ key = 'store_hide_followed', value = false }) })
assert(out.success == true and out.prefs.store_hide_followed == false)
out = call('SlsLunaPrefsSet', { raw = json.encode({ key = 'store_hide_followed', value = 'yes' }) })
assert(out.success == false, 'a wrongly typed preference was accepted')
out = call('SlsLunaPrefsSet', { raw = json.encode({ key = 'nonsense', value = true }) })
assert(out.success == false, 'an unknown preference was accepted')
out = call('SlsLunaPrefsSet', { raw = json.encode({ value = true }) })
assert(out.success == false, 'a keyless preference was accepted')
-- The bridge may hand the payload over already decoded; both shapes work.
out = call('SlsLunaPrefsSet', { payload = { key = 'store_hide_followed', value = true } })
assert(out.success == true and out.prefs.store_hide_followed == true,
  'a decoded table payload must be accepted too')

-- The client settings (beta switch, HLTB order) live here too: boolean and
-- string keys with a value whitelist.
out = call('SlsLunaPrefsSet', { raw = json.encode({ key = 'beta', value = true }) })
assert(out.success == true and out.prefs.beta == true, 'the beta switch was not saved')
out = call('SlsLunaPrefsSet', { raw = json.encode({ key = 'hltb_order', value = 'long' }) })
assert(out.success == true and out.prefs.hltb_order == 'long', 'the HLTB order was not saved')
out = call('SlsLunaPrefsSet', { raw = json.encode({ key = 'hltb_order', value = 'sideways' }) })
assert(out.success == false, 'an unknown order value was accepted')
out = call('SlsLunaInfo')
assert(out.prefs.beta == true and out.prefs.hltb_order == 'long', 'the client settings did not persist')
out = call('SlsLunaPrefsSet', { raw = json.encode({ key = 'beta', value = 'yes' }) })
assert(out.success == false, 'a wrongly typed beta value was accepted')
out = call('SlsLunaPrefsSet', { raw = json.encode({ key = 'lang', value = 'es' }) })
assert(out.success == true and out.prefs.lang == 'es', 'the language preference was not saved')
out = call('SlsLunaPrefsSet', { raw = json.encode({ key = 'lang', value = 'fr' }) })
assert(out.success == false, 'an unknown language was accepted')
out = call('SlsLunaPrefsSet', { raw = json.encode({ key = 'lang', value = 'en' }) })
assert(out.success == true and out.prefs.lang == 'en', 'the language preference did not switch back')

-- Health: read-only diagnostics, tolerant of a bare environment.
out = call('SlsLunaHealth')
assert(out.success == true and type(out.health) == 'table', 'the health report was not returned')
assert(out.health.version == pinned, 'the health report has no version')
assert(type(out.health.lumen) == 'table' and type(out.health.lumen.boot) == 'table',
  'the Lumen block is missing')
assert(out.health.steam.localconfigConflicts == 0,
  'the bare harness has no sync conflicts to report')
assert(type(out.health.data) == 'table' and type(out.health.data.prefs) == 'table',
  'the data block is missing')
assert(out.health.data.prefs.beta == true, 'the health report does not carry the preferences')
assert(type(out.health.steam.client) == 'table', 'the Steam client block is missing')

-- Steam client detection: a zero report carries no information, and the
-- installed package manifest wins when runtime logs are stale or missing.
local fake_steam = os.getenv('HOME') .. '/.local/share/Steam'
assert(os.execute('mkdir -p ' .. string.format('%q', fake_steam .. '/package')))
assert(os.execute('mkdir -p ' .. string.format('%q', fake_steam .. '/logs')))
local function steam_build()
  return call('SlsLunaHealth').health.steam.client.build
end
assert(steam_build() == '', 'no logs or manifest must mean no reported build')
local manifest = io.open(fake_steam .. '/package/steam_client_ubuntu12.manifest', 'wb')
manifest:write('"ubuntu12"\n{\n\t"version"\t\t"1792618285"\n}\n')
manifest:close()
assert(steam_build() == '1792618285', 'the installed manifest is ground truth without logs')
local clog = io.open(fake_steam .. '/logs/console_log.txt', 'wb')
clog:write('[x] Client version: 0\n')
clog:close()
assert(steam_build() == '1792618285', 'a zero report must not override the manifest')
clog = io.open(fake_steam .. '/logs/console_log.txt', 'wb')
clog:write('[x] Client version: 111\n[x] Client version: 222\n')
clog:close()
assert(steam_build() == '222', 'the newest nonzero runtime report still wins')

-- Steam client queue: status, queue, cancel round-trip in disposable HOME.
out = call('SlsLunaSteam', { raw = json.encode({ action = 'status' }) })
assert(out.success == true and type(out.steam) == 'table', 'the Steam status was not returned')
assert(out.steam.pending == '', 'nothing may be queued in a fresh harness')
out = call('SlsLunaSteam', { raw = json.encode({ action = 'queue-update' }) })
assert(out.success == true and out.queued == 'update', 'queueing must report the job')
out = call('SlsLunaSteam', { raw = json.encode({ action = 'status' }) })
assert(out.steam.pending == 'update', 'the queued job must be visible')
out = call('SlsLunaSteam', { raw = json.encode({ action = 'queue-cache' }) })
assert(out.success == false, 'a second queue must be refused while one stands')
out = call('SlsLunaSteam', { raw = json.encode({ action = 'cancel' }) })
assert(out.success == true and out.steam.pending == '', 'cancel must clear the queue')
out = call('SlsLunaSteam', { raw = json.encode({ action = 'status' }) })
assert(out.steam.hardLock == false and out.steam.job == '', 'a fresh harness is neither locked nor busy')
out = call('SlsLunaSteam', { raw = json.encode({ action = 'lock; rm -rf /' }) })
assert(out.success == false, 'unknown actions must be refused, never reach a shell')
out = call('SlsLunaSteam', { raw = json.encode({ action = 'lock' }) })
assert(out.success == false, 'without an installed updater the lock must refuse instead of spawning')
out = call('SlsLunaRefresh')
assert(out.success == true and out.refreshPending == false, 'a fresh harness has no refresh queued')
out = call('SlsLunaRefresh', { raw = json.encode({ action = 'queue' }) })
assert(out.success == true and out.refreshPending == true, 'queueing must report the refresh')
out = call('SlsLunaRefresh', { raw = json.encode({ action = 'queue' }) })
assert(out.success == false, 'a second refresh queue must be refused while one stands')
out = call('SlsLunaRefresh', { raw = json.encode({ action = 'cancel' }) })
assert(out.success == true and out.refreshPending == false, 'cancel must clear the refresh')
out = call('SlsLunaRefresh', { raw = json.encode({ action = 'nonsense' }) })
assert(out.success == false, 'an unknown refresh action must be refused')
out = call('SlsLunaSteam', { raw = json.encode({ action = 'pin' }) })
assert(out.success == false, 'pin with no updater copy and no Steam log must fail closed')
out = call('SlsLunaLaunchers')
assert(out.success == true and out.helper == false, 'a fresh harness has no launcher helper')
out = call('SlsLunaLaunchers', { raw = json.encode({ action = 'create' }) })
assert(out.success == false, 'creation with no updater copy must fail closed, not shell out')
out = call('SlsLunaLaunchers', { raw = json.encode({ action = 'nonsense' }) })
assert(out.success == false, 'an unknown launchers action must be refused')
out = call('SlsLunaRepair')
assert(out.success == false, 'repair with no updater copy must fail closed, not shell out')

-- A repair job past its deadline is reported as timed out, the marker is
-- removed, and the status read stays cheap. No installer is started here: the
-- kill itself is the one group signal the job helper sends.
local repair_dir = home .. '/.local/share/Lumen/sls-luna'
os.execute('mkdir -p ' .. string.format('%q', repair_dir))
local marker = assert(io.open(repair_dir .. '/repair-job.json', 'wb'))
marker:write('{"id":"1","pid":0,"startedAt":' .. tostring(os.time() - 500)
  .. ',"phase":"running","state":"running","limit":180}')
marker:close()
out = call('SlsLunaRepair', { raw = json.encode({ action = 'status' }) })
assert(out.success == true and out.job.state == 'timeout', 'a stale job must report a timeout')
assert(tostring(out.job.message):find('process group was stopped'),
  'a timeout must say the process group was stopped')
assert(io.open(repair_dir .. '/repair-job.json', 'rb') == nil, 'the stale marker is removed')
out = call('SlsLunaRepair', { raw = json.encode({ action = 'status' }) })
assert(out.success == true and out.job.state == 'timeout',
  'and the last outcome stays a timeout, not a false success')

-- Non-blocking fetch: with http.start/http.poll available, SlsHltbFetch never
-- waits on the network. The fake answers each request after two empty polls,
-- reusing the blocking fake's responses so both paths see the same site.
local started = 0
wire.start = function(url, options)
  started = started + 1
  return { url = url, options = options or {}, polls = 2 }
end
wire.poll = function(handle, timeout)
  assert(timeout == 0, 'polling must never wait')
  handle.polls = handle.polls - 1
  if handle.polls > 0 then return false end
  if handle.options.method == 'POST' then
    return true, wire.post(handle.url, handle.options.data, handle.options)
  end
  return true, wire.get(handle.url, handle.options)
end
local function fetch_async(appid, name)
  local pending = 0
  for _ = 1, 40 do
    local result = call('SlsHltbFetch', { appid = appid, name = name })
    if result.code ~= 'pending' then return result, pending end
    assert(result.pending == true and result.success == false, 'a pending fetch must say so')
    pending = pending + 1
  end
  error('the non-blocking fetch never finished')
end
wire.mode = 'ok'
local gets, posts = wire.get_calls, wire.post_calls
out, pending = fetch_async(413150, 'Stardew Valley')
assert(out.success == true and out.record.name == 'Stardew Valley', 'the non-blocking fetch must store a record')
assert(pending >= 2, 'the RPC must return while requests are in flight')
assert(started == 2 and wire.get_calls == gets + 1 and wire.post_calls == posts + 1,
  'one token request and one search, through http.start only')
wire.mode = 'expired'
wire.expired_armed = true
out = fetch_async(504230, 'Celeste')
assert(out.success == true, 'an expired token is refreshed on the non-blocking path too')
assert(started == 6, 'token, rejected search, new token and the retried search')
wire.mode = 'ratelimit'
out = fetch_async(250900, 'The Binding of Isaac')
assert(out.success == false and out.code == 'rate_limit', 'rate limiting is reported, not hidden')
wire.mode = 'ok'
-- An abandoned job expires instead of pinning the game forever.
local first = call('SlsHltbFetch', { appid = 105600, name = 'Terraria' })
assert(first.code == 'pending', 'the first call starts the job')
local before = started
backend.fetch_job_ttl = -1
assert(call('SlsHltbFetch', { appid = 105600, name = 'Terraria' }).code == 'pending')
assert(started == before + 1, 'a stale job is dropped and a fresh token request starts')
backend.fetch_job_ttl = 60
out = fetch_async(105600, 'Terraria')
assert(out.success == true, 'the restarted job completes')
wire.start, wire.poll = nil, nil

-- The update check runs detached: a slow GitHub must not hold the RPC (Lumen
-- runs every RPC on its single injector thread). The fake updater takes two
-- seconds; the RPC has to answer before it finishes, and the result shows up
-- in the status afterwards.
local tools = os.getenv('HOME') .. '/.local/share/Lumen/sls-luna'
assert(os.execute('mkdir -p ' .. string.format('%q', tools)))
local fake = io.open(tools .. '/luna.py', 'wb')
fake:write([[
import json, os, sys, time
tools = os.path.dirname(os.path.abspath(__file__))
if sys.argv[1:2] == ['check']:
    time.sleep(2)
    with open(os.path.join(tools, 'update-check.json'), 'w') as out:
        json.dump({'latest': '9.9.9', 'checkedAt': int(time.time()), 'checkError': '',
                   'available': True}, out)
elif sys.argv[1:2] == ['timer']:
    print('unattended updates: ' + sys.argv[2])
]])
fake:close()
out = call('SlsLunaUpdate', { raw = json.encode({ action = 'check', force = true }) })
assert(out.success == true and out.update.checking == true, 'a started check must report checking')
assert(out.update.latest == '', 'the RPC must return before the check has finished')
out = call('SlsLunaUpdate', { raw = json.encode({ action = 'check', force = true }) })
assert(out.success == true and out.update.checking == true, 'a second request joins the running check')
local finished = false
for _ = 1, 50 do
  os.execute('sleep 0.2')
  out = call('SlsLunaUpdate', { raw = json.encode({ action = 'status' }) })
  if not out.update.checking then finished = true; break end
end
assert(finished and out.update.latest == '9.9.9', 'the detached check result must reach the status')
out = call('SlsLunaUpdate', { raw = json.encode({ action = 'timer', value = 'on' }) })
assert(out.success == true and type(out.job) == 'table', 'the timer job returns at once')
local timer_state = out.job.state
for _ = 1, 50 do
  if timer_state ~= 'running' and timer_state ~= 'finishing' then break end
  os.execute('sleep 0.1')
  out = call('SlsLunaUpdate', { raw = json.encode({ action = 'status' }) })
  timer_state = (type(out.job) == 'table' and out.job.state) or timer_state
end
assert(timer_state == 'ok', 'the detached timer job must finish cleanly, got ' .. tostring(timer_state))
local timer_log = io.open(home .. '/.local/share/Lumen/sls-luna/timer-job.log', 'rb')
assert(timer_log, 'a detached job keeps its output in a log file')
local logged = timer_log:read('*a'); timer_log:close()
assert(logged:find('unattended updates: on'), 'bounded updater runs keep their output')

-- ----------------------------------------------------------- payload cache
--
-- SlsHltbGet is the biggest polled reply; it is memoised on the stamps of the
-- two files it reads, and a client that already has a payload can ask with the
-- `rev` it got back for a tiny "nothing changed" answer.
local library_path = home .. '/.local/share/slssteam-integrations/hltb-library.json'
local first_text = registry.SlsHltbGet()
local first_count = json.decode(first_text).count
local first_rev = json.decode(first_text).rev
assert(type(first_rev) == 'string' and #first_rev > 0, 'the payload must carry a revision')
assert(registry.SlsHltbGet() == first_text, 'a cache hit must return the identical wire payload')
local unchanged = json.decode(registry.SlsHltbGet(first_rev))
assert(unchanged.success == true and unchanged.unchanged == true, 'the rev must be recognised')
assert(unchanged.games == nil and unchanged.library == nil, 'an unchanged reply must carry no data')
assert(#registry.SlsHltbGet(first_rev) < 200, 'the unchanged reply must be tiny')
-- A change written by something else (not this backend) is seen at once.
do
  local f = io.open(store_path, 'rb')
  local store = json.decode(f:read('*a')); f:close()
  store.games['999'] = {id = 0, name = 'External', match = 'manual', main = 3600}
  local out = io.open(store_path, 'wb')
  out:write(json.encode(store)); out:close()
end
touch(store_path)
local grown = json.decode(registry.SlsHltbGet())
assert(grown.count == first_count + 1 and grown.games['999'], 'a changed source must invalidate the cache')
assert(registry.SlsHltbGet(first_rev).unchanged == nil, 'a stale rev must never answer unchanged')
local new_rev = grown.rev
assert(new_rev ~= first_rev, 'the revision must move with the file')
assert(json.decode(registry.SlsHltbGet(new_rev)).unchanged == true, 'the new rev must be recognised')
-- The backend's own writes drop the memo without waiting for a stat.
assert(call('SlsHltbPut', {raw = json.encode({appid = 4242, name = 'Own', main = 2})}).success)
assert(call('SlsHltbGet').games['4242'] ~= nil, 'a local write must be visible immediately')
-- Health is memoised the same way and the escape hatch bypasses it.
local health_text = registry.SlsLunaHealth()
assert(registry.SlsLunaHealth() == health_text, 'health must be memoised')
local refreshed = json.decode(registry.SlsLunaHealth(json.encode({refresh = true})))
assert(refreshed.success == true and refreshed.health.version ~= nil, 'refresh must rebuild the payload')
local cfg = io.open(home .. '/.local/share/Steam/steam.cfg', 'wb')
cfg:write('BootStrapperInhibitAll\n'); cfg:close()
assert(json.decode(registry.SlsLunaHealth()).health.steam.client.updatesBlocked == true,
  'a changed input must invalidate the health memo')

print('Luna HLTB backend OK: matching, token refresh, rate limits, manual overrides, persistence, prefs, health')

-- ------------------------------------------------------------ re-validation
--
-- A store written by an older scorer (or by another machine) is checked once
-- against the Steam names in the library: a record whose title no longer scores
-- as a confident match is flagged, never deleted, and the pass is idempotent.
local function write_store_file(path, value)
  local handle = assert(io.open(path, 'wb'))
  handle:write(json.encode(value))
  handle:close()
end

write_store_file(store_path, { version = 1, games = {
  ['111'] = { id = 1, name = 'Hades II', match = 'exact', main = 100 },
  ['222'] = { id = 2, name = 'Hades', match = 'exact', main = 200 },
  ['333'] = { id = 3, name = 'Unrelated', match = 'manual', main = 50 },
} })
touch(store_path)
out = call('SlsHltbLibrary', { raw = json.encode({ games = {
  { appid = 111, name = 'Hades' }, { appid = 222, name = 'Hades' }, { appid = 333, name = 'Something Else' },
  { appid = 444, name = 'Hades Original Soundtrack' }, { appid = 445, name = 'Half-Life: Alyx Playtest' },
} }) })
assert(out.success and out.count == 3 and out.skipped == 2,
  'soundtrack/playtest inventory entries must be skipped: ' .. tostring(out.count) .. '/' .. tostring(out.skipped))
out = call('SlsHltbRevalidate')
assert(out.success and out.checked == 2 and out.flagged == 1 and out.applied == true,
  'one stored sequel match must be flagged: ' .. json.encode(out))
local reviewed = read_store()
assert(reviewed.games['111'].needsReview == true and reviewed.games['111'].stale == true,
  'the sequel record must be marked for review')
assert(reviewed.games['111'].main == 100, 'a flagged record keeps its times (not deleted)')
assert(reviewed.games['222'].needsReview == nil, 'a title that still matches must stay clean')
assert(reviewed.games['333'].needsReview == nil, 'manual records are never reviewed')
local after_first = json.encode(read_store())
out = call('SlsHltbRevalidate')
assert(out.applied == false and out.flagged == 1, 'a second pass must be a no-op')
assert(json.encode(read_store()) == after_first, 'an idempotent pass must not rewrite the store')
assert(call('SlsHltbGet').needsReview == 1, 'the read reply must expose the review count')

-- ------------------------------------------------------------- durability
--
-- Torn store + good backup: restore it, keep the broken file and say so.
local good_store = { version = 1, games = {
  ['4242'] = { id = 0, name = 'Own', match = 'manual', main = 7200 } } }
write_store_file(store_path .. '.bak', good_store)
write_store_file(store_path, 'x') -- overwritten below with a truncated document
local torn = assert(io.open(store_path, 'wb'))
torn:write('{"version":1,"games":{"42')
torn:close()
touch(store_path)
local rescued = call('SlsHltbGet')
assert(rescued.recovered == true, 'a torn store with a good backup must be reported recovered')
assert(rescued.recovery and rescued.recovery.message:find('restored', 1, true),
  'the reply must carry a recovery message')
assert(rescued.games['4242'] and rescued.games['4242'].main == 7200, 'the backup records must be served')
local kept = io.open(rescued.recovery.kept and (home .. '/.local/share/slssteam-integrations/' .. rescued.recovery.kept)
  or '/nonexistent', 'rb')
assert(kept, 'the broken file must be kept as *.corrupt-<ts>')
kept:close()

-- Both copies bad: keep the broken file, start empty, report an error.
write_store_file(store_path, 'not json at all')
write_store_file(store_path .. '.bak', 'also broken')
touch(store_path)
local broken = call('SlsHltbGet')
assert(broken.success == true and broken.count == 0, 'a lost store starts empty, it does not fail the read')
assert(broken.recovery and broken.recovery.error and broken.recovery.error:find('started empty', 1, true),
  'the reply must say the store started empty')
assert(broken.recovery.kept, 'the unreadable original must be kept, never discarded')

-- A failed write leaves the old file and no partial *.tmp behind.
local before_fail = assert(io.open(store_path, 'rb')):read('*a')
local data_dir_path = home .. '/.local/share/slssteam-integrations'
os.execute('chmod 0555 ' .. string.format('%q', data_dir_path))
local refused_put = call('SlsHltbPut', { raw = json.encode({ appid = 555, name = 'Blocked', main = 1 }) })
os.execute('chmod 0755 ' .. string.format('%q', data_dir_path))
assert(refused_put.success == false and type(refused_put.error) == 'string',
  'a write into a read-only directory must fail visibly')
local after_fail = assert(io.open(store_path, 'rb')):read('*a')
assert(after_fail == before_fail, 'a failed write must leave the previous store intact')
local leftovers = io.popen('ls -1 ' .. string.format('%q', data_dir_path) .. ' | grep -c "\\.tmp$"')
local tmp_count = tonumber(leftovers:read('*a') or '0') or 0
leftovers:close()
assert(tmp_count == 0, 'a failed write must not leave a partial *.tmp file')

-- ------------------------------------------------------- data-dir hygiene
--
-- Syncthing leaves *.sync-conflict-* copies in the shared directory. They are
-- reported in Health and moved (never deleted) into conflicts/<date>/.
local conflicts = {
  'hltb-library.sync-conflict-20260101-120000-AAAAAAA.json',
  'hltb-library.sync-conflict-20260102-120000-BBBBBBB.json',
  'presence.sync-conflict-20260103-120000-CCCCCCC.json',
}
for _, name in ipairs(conflicts) do
  local handle = assert(io.open(data_dir_path .. '/' .. name, 'wb'))
  handle:write('{"version":1,"games":{}}')
  handle:close()
end
-- A conflict copy can sit next to the playtime ledger too, not only next to
-- hltb-library.json: the sweep covers the data directory's subdirectories.
local nested = 'playtime.sync-conflict-20260104-120000-DDDDDDD.json'
os.execute('mkdir -p ' .. string.format('%q', data_dir_path .. '/playtime'))
local nested_handle = assert(io.open(data_dir_path .. '/playtime/' .. nested, 'wb'))
nested_handle:write('{"events":[]}')
nested_handle:close()
local hygiene = call('SlsLunaHygiene', { raw = json.encode({ action = 'status' }) })
assert(hygiene.success and hygiene.hygiene.count == 4, 'four conflict copies must be reported')
assert(hygiene.hygiene.totalBytes > 0 and hygiene.hygiene.files[1].name ~= nil, 'each copy has a size')
assert(hygiene.hygiene.caps.store == 4 * 1024 * 1024, 'the caps must be reported with the files')
local health_payload = call('SlsLunaHealth', { raw = json.encode({ refresh = true }) })
assert(health_payload.health.data.hygiene.conflicts.count == 4, 'Health must count the conflict copies')
assert(health_payload.health.data.caps.hltb == 4 * 1024 * 1024
  and health_payload.health.data.caps.records == 8000, 'Health must publish the growth caps')
assert(type(health_payload.health.data.hltb.needsReview) == 'number',
  'Health must expose the review count from the marker, not a store decode')
local capped = false
for _, file in ipairs(health_payload.health.data.files) do
  if file.name == 'hltb.json' then
    assert(file.size and file.cap == 4 * 1024 * 1024, 'Health must show each data file with its cap')
    capped = true
  end
end
assert(capped, 'Health must list the store with its cap')
local clean = call('SlsLunaHygiene', { raw = json.encode({ action = 'clean' }) })
assert(clean.success and clean.moved == 4 and type(clean.folder) == 'string', 'clean must move every copy')
for _, name in ipairs(conflicts) do
  local gone = io.open(data_dir_path .. '/' .. name, 'rb')
  assert(gone == nil, 'clean must remove the copy from the live directory: ' .. name)
  local moved = io.open(clean.folder .. '/' .. name, 'rb')
  assert(moved, 'clean must keep the copy in conflicts/<date>/: ' .. name)
  moved:close()
end
local kept_nested = io.open(clean.folder .. '/playtime/' .. nested, 'rb')
assert(kept_nested, 'a subdirectory copy keeps its subdirectory under conflicts/<date>/')
kept_nested:close()
assert(call('SlsLunaHygiene', { raw = json.encode({ action = 'status' }) }).hygiene.count == 0,
  'the live directory must be clean afterwards')
-- Health stays memoised with the hygiene report inside it: building it must not
-- write the sidecar it is keyed on.
local health_once = registry.SlsLunaHealth()
assert(registry.SlsLunaHealth() == health_once, 'Health must stay memoised after a hygiene clean')
assert(call('SlsLunaHygiene', { raw = json.encode({ action = 'nonsense' }) }).success == false,
  'an unknown hygiene action must be refused')

print('Luna HLTB robustness OK: ' .. tostring(case_matched + case_refused) .. ' matching pairs, revalidation, '
  .. '.bak recovery, corrupt retention, atomic write, sync-conflict status/clean, health caps')
