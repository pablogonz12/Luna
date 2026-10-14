-- SLS Luna backend: shell information and the local HowLongToBeat cache.
--
-- Luna never queries HowLongToBeat on its own: the store below is filled by
-- user-triggered fetches (the game right-click action) or by manual overrides,
-- and is what the future Steam-UI surfacing (game-page stat cell, library sort)
-- reads from. Times are kept in seconds exactly as the site reports them.
--
-- The store lives outside the add-on directory, next to the Playtime and
-- Presence data, so reinstalling the add-on never touches it.
local fs = require('fs')
local json = require('json')
local M = {}
local VERSION = '0.4.0'
local home = os.getenv('HOME') or ''
local base = home .. '/.local/share/Lumen/sls-addons/'
local data_dir = home .. '/.local/share/slssteam-integrations'
local store_path = data_dir .. '/hltb.json'
local library_path = data_dir .. '/hltb-library.json'

local INIT_URL = 'https://howlongtobeat.com/api/search/site/init?t='
local SEARCH_URL = 'https://howlongtobeat.com/api/search/site'
local UA = 'Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0'

-- How close two requests may be. The site itself rate-limits (HTTP 429), and
-- the fetch is user-triggered, so a small spacing keeps refreshes polite.
M.min_fetch_interval = 2
local last_fetch_at = 0

local function reply(data) return json.encode(data) end

local function read_file(path)
  local f = io.open(path, 'rb')
  if not f then return nil end
  local raw = f:read('*a')
  f:close()
  return raw
end

local function valid_appid(v)
  local n = tonumber(v)
  return n and n > 0 and n <= 4294967295 and n == math.floor(n)
end

-- ------------------------------------------------------------------ storage

local function blank_store()
  return { version = 1, games = {} }
end

local function load_store()
  local raw = read_file(store_path)
  if not raw then return blank_store() end
  local ok, parsed = pcall(json.decode, raw)
  if not ok or type(parsed) ~= 'table' or type(parsed.games) ~= 'table' then
    return blank_store()
  end
  parsed.version = 1
  return parsed
end

-- Atomic like the Playtime ledger: tmp file + rename, so a crash never leaves
-- a half-written store behind.
local function persist_store(data)
  fs.create_directories(fs.parent_path(store_path))
  local tmp = store_path .. '.tmp'
  local f, why = io.open(tmp, 'wb')
  if not f then return false, why or 'cannot write store' end
  local written, werr = f:write(json.encode(data))
  local closed, cerr = f:close()
  if not written or not closed then
    os.remove(tmp)
    return false, werr or cerr or 'write failed'
  end
  local ok, rerr = os.rename(tmp, store_path)
  if not ok then os.remove(tmp); return false, rerr or 'rename failed' end
  return true
end

-- The game list the Steam agent reports (appid + display name). Kept apart from
-- the times themselves: it is inventory, not measurements, and the panel uses
-- it to show what is still missing. Written atomically like the store.
local function load_library()
  local raw = read_file(library_path)
  if not raw then return { version = 1, updatedAt = 0, games = {} } end
  local ok, parsed = pcall(json.decode, raw)
  if not ok or type(parsed) ~= 'table' or type(parsed.games) ~= 'table' then
    return { version = 1, updatedAt = 0, games = {} }
  end
  parsed.version = 1
  return parsed
end

local function persist_library(data)
  fs.create_directories(fs.parent_path(library_path))
  local tmp = library_path .. '.tmp'
  local f, why = io.open(tmp, 'wb')
  if not f then return false, why or 'cannot write library' end
  local written, werr = f:write(json.encode(data))
  local closed, cerr = f:close()
  if not written or not closed then
    os.remove(tmp)
    return false, werr or cerr or 'write failed'
  end
  local ok, rerr = os.rename(tmp, library_path)
  if not ok then os.remove(tmp); return false, rerr or 'rename failed' end
  return true
end

-- ------------------------------------------------------------------ matching

-- Lowercase, fold common Latin accents (byte-wise so no utf8 library is
-- required), strip trademark noise and collapse everything non-alphanumeric
-- into single spaces. Purely presentational rules: no transliteration.
local ACCENTS = {
  ['\195\137'] = 'e', ['\195\169'] = 'e', ['\195\168'] = 'e', ['\195\170'] = 'e', ['\195\171'] = 'e',
  ['\195\129'] = 'a', ['\195\161'] = 'a', ['\195\160'] = 'a', ['\195\164'] = 'a', ['\195\162'] = 'a',
  ['\195\132'] = 'a', ['\195\131'] = 'a', ['\195\133'] = 'a',
  ['\195\141'] = 'i', ['\195\172'] = 'i', ['\195\174'] = 'i', ['\195\140'] = 'i',
  ['\195\147'] = 'o', ['\195\179'] = 'o', ['\195\178'] = 'o', ['\195\182'] = 'o', ['\195\181'] = 'o',
  ['\195\146'] = 'o', ['\195\148'] = 'o', ['\195\152'] = 'o',
  ['\195\154'] = 'u', ['\195\186'] = 'u', ['\195\185'] = 'u', ['\195\187'] = 'u', ['\195\188'] = 'u',
  ['\195\145'] = 'n', ['\195\177'] = 'n', ['\195\135'] = 'c', ['\195\167'] = 'c',
}
function M.normalize(text)
  local out = tostring(text or ''):lower()
  out = out:gsub('\226\132\162', ''):gsub('\194\174', ''):gsub('\194\169', '') -- ™ ® ©
  for bytes, plain in pairs(ACCENTS) do out = out:gsub(bytes, plain) end
  out = out:gsub('[^%w]+', ' ')
  out = out:gsub('^%s+', ''):gsub('%s+$', '')
  return out
end

local function tokens(normalized)
  local list = {}
  for token in normalized:gmatch('%S+') do list[#list + 1] = token end
  return list
end

local function token_overlap(query, name)
  local q = tokens(query)
  local n = {}
  for _, token in ipairs(tokens(name)) do n[token] = true end
  if #q == 0 then return 0 end
  local hits = 0
  for _, token in ipairs(q) do if n[token] then hits = hits + 1 end end
  return hits / #q
end

-- How well one candidate title answers the query: (score, quality) or nil.
-- Precision matters more than recall here: a common word shared with an
-- unrelated title must not clear the floor, so token overlap is measured in
-- both directions and only good coverage of both sides counts as strong.
function M.name_score(query, name)
  local q = M.normalize(query)
  local candidate = M.normalize(name)
  if q == '' or candidate == '' then return nil end
  if candidate == q then return 1, 'exact' end
  -- Word-boundary prefixes: "Hades" -> "Hades II" or "The Witcher 3" ->
  -- "The Witcher 3: Wild Hunt", the usual sequel/subtitle shape.
  if candidate:sub(1, #q + 1) == q .. ' ' then return 0.9, 'strong' end
  if q:sub(1, #candidate + 1) == candidate .. ' ' then return 0.85, 'strong' end
  local q_cov = token_overlap(q, candidate)
  local c_cov = token_overlap(candidate, q)
  if q_cov >= 0.75 and c_cov >= 0.5 then
    return math.min(0.8 * q_cov + 0.2 * c_cov, 0.8), 'strong'
  end
  return (0.8 * q_cov + 0.2 * c_cov) * 0.6, 'weak'
end

-- Best candidate for `query` among the site's rows, refusing weak guesses:
-- pick_match returns nil when even the best score is too low to trust.
function M.pick_match(rows, query)
  local best, best_score, best_quality
  for _, row in ipairs(rows or {}) do
    if type(row) == 'table' then
      local score, quality = M.name_score(query, row.game_name or row.name)
      if score and (best_score == nil or score > best_score) then
        best, best_score, best_quality = row, score, quality
      end
    end
  end
  if best == nil or best_score < 0.6 then return nil end
  return best, best_quality, best_score
end

-- ------------------------------------------------------------------ fetching

local ok_http, http = pcall(require, 'http')

local function request_headers(extra)
  local headers = {
    ['User-Agent'] = UA,
    ['Referer'] = 'https://howlongtobeat.com/',
    ['Accept'] = 'application/json, text/plain, */*',
  }
  for key, value in pairs(extra or {}) do headers[key] = value end
  return headers
end

local function search_payload(name)
  local terms = {}
  for token in tostring(name):gmatch('%S+') do terms[#terms + 1] = token end
  return {
    searchType = 'games',
    searchTerms = terms,
    searchPage = 1,
    size = 10,
    searchOptions = {
      games = { userId = 0, platform = '', sortCategory = 'popular',
        rangeCategory = 'main', rangeTime = { min = 0, max = 0 },
        gameplay = { perspective = '', flow = '', genre = '' }, year = '', modifier = '' },
      users = { sortCategory = 'postcount' },
      lists = { sortCategory = 'follows' },
      filter = '', sort = 0, randomizer = 0,
    },
    useCache = true,
  }
end

local function decode_body(body)
  if type(body) ~= 'string' or body == '' then return nil end
  local ok, parsed = pcall(json.decode, body)
  if ok and type(parsed) == 'table' then return parsed end
  return nil
end

-- Two requests, exactly like the site's own search page: fetch a token, then
-- POST the query with it. A 403 means the token expired; refresh once.
-- Returns rows, or nil + a user-facing reason.
local function fetch_rows(name)
  if not ok_http or type(http) ~= 'table'
      or type(http.get) ~= 'function' or type(http.post) ~= 'function' then
    return nil, 'The Lumen HTTP module is unavailable; cannot reach HowLongToBeat.'
  end
  local function token()
    local response, err = http.get(INIT_URL .. tostring(os.time()) .. '000',
      { headers = request_headers(), timeout = 15 })
    if type(response) ~= 'table' then return nil, tostring(err or 'no response') end
    if response.status ~= 200 then
      return nil, 'HowLongToBeat search init failed (HTTP ' .. tostring(response.status) .. ').'
    end
    local parsed = decode_body(response.body)
    if not parsed or type(parsed.token) ~= 'string' or parsed.token == '' then
      return nil, 'HowLongToBeat returned no search token.'
    end
    return parsed.token
  end

  local current, why = token()
  if not current then return nil, why end
  local payload = json.encode(search_payload(name))
  for attempt = 1, 2 do
    local response, err = http.post(SEARCH_URL, payload, {
      headers = request_headers({ ['Content-Type'] = 'application/json', ['x-auth-token'] = current }),
      timeout = 20,
    })
    if type(response) ~= 'table' then
      return nil, 'HowLongToBeat request failed: ' .. tostring(err or 'no response')
    end
    if response.status == 403 and attempt == 1 then
      current, why = token()
      if not current then return nil, why end
    elseif response.status == 429 then
      return nil, 'HowLongToBeat is rate-limiting searches; wait a minute and try again.'
    elseif response.status ~= 200 then
      return nil, 'HowLongToBeat search failed (HTTP ' .. tostring(response.status) .. ').'
    else
      local parsed = decode_body(response.body)
      local rows = parsed and (parsed.data or parsed.results)
      if type(rows) ~= 'table' then return nil, 'HowLongToBeat returned an unexpected response.' end
      return rows
    end
  end
  return nil, 'HowLongToBeat rejected the search token twice.'
end

-- ------------------------------------------------------------------ records

local function number(v)
  local n = tonumber(v)
  if n and n >= 0 then return math.floor(n) end
  return 0
end

function M.record_from_row(row, quality, now)
  return {
    id = number(row.game_id),
    name = tostring(row.game_name or ''),
    match = quality or 'strong',
    main = number(row.comp_main),
    extra = number(row.comp_plus),
    completionist = number(row.comp_100),
    all = number(row.comp_all),
    source = 'hltb-search',
    fetchedAt = now or os.time(),
  }
end

-- Manual override: hours in, seconds stored. Zero clears a bucket.
local function record_from_manual(appid, entry, now)
  local function seconds(v)
    if v == nil then return nil end
    local hours = tonumber(v)
    if not hours or hours < 0 or hours > 100000 then return nil end
    return math.floor(hours * 3600 + 0.5)
  end
  local record = {
    id = 0,
    name = tostring(entry.name or ''),
    match = 'manual',
    main = seconds(entry.main),
    extra = seconds(entry.extra),
    completionist = seconds(entry.completionist),
    all = nil,
    source = 'manual',
    fetchedAt = now or os.time(),
  }
  if record.main == nil and record.extra == nil and record.completionist == nil then return nil end
  return record
end

-- ------------------------------------------------------------------ registry

local function installed()
  local names = {}
  local ok, items = pcall(fs.list, base)
  if ok and type(items) == 'table' then
    for _, item in ipairs(items) do
      local name = tostring(item.name or '')
      if item.is_directory and name ~= '' and name:sub(1, 1) ~= '.' then
        local probe = io.open(base .. name .. '/backend/main.lua', 'r')
        if probe then probe:close(); names[#names + 1] = name end
      end
    end
  end
  table.sort(names)
  return names
end

function M.register(registry)
  registry.SlsLunaInfo = function()
    return reply({ success = true, version = VERSION, addons = installed(), data = data_dir })
  end

  -- Whole local cache: appid -> record, plus the game list the agent reported.
  -- Small by design (a library at most).
  registry.SlsHltbGet = function()
    local store = load_store()
    local library = load_library()
    local count = 0
    for _ in pairs(store.games) do count = count + 1 end
    return reply({ success = true, games = store.games, count = count,
      library = library.games, libraryAt = library.updatedAt,
      minFetchInterval = M.min_fetch_interval })
  end

  -- The Steam agent's inventory of owned games and non-Steam shortcuts, so the
  -- panel can show what has no times yet. Purely local; never sent anywhere.
  registry.SlsHltbLibrary = function(raw)
    local ok, entry = pcall(json.decode, tostring(raw or ''))
    if not ok or type(entry) ~= 'table' or type(entry.games) ~= 'table' then
      return reply({ success = false, error = 'Send {games: [{appid, name, shortcut?}]}.' })
    end
    local games, seen = {}, {}
    for _, game in ipairs(entry.games) do
      if type(game) == 'table' and valid_appid(game.appid) then
        local key = tostring(math.floor(tonumber(game.appid)))
        local name = tostring(game.name or ''):sub(1, 200)
        if name ~= '' and not seen[key] then
          seen[key] = true
          games[#games + 1] = { appid = tonumber(key), name = name, shortcut = game.shortcut == true }
        end
      end
      if #games >= 5000 then break end
    end
    if #games == 0 and #entry.games > 0 then
      return reply({ success = false, error = 'No valid games in the inventory.' })
    end
    local saved, serr = persist_library({ version = 1, updatedAt = os.time(), games = games })
    if not saved then return reply({ success = false, error = tostring(serr) }) end
    return reply({ success = true, count = #games })
  end

  -- User-triggered fetch for one game. Never runs by itself and never batches.
  registry.SlsHltbFetch = function(appid, name)
    if not valid_appid(appid) then return reply({ success = false, error = 'Invalid app id.' }) end
    name = tostring(name or ''):gsub('^%s+', ''):gsub('%s+$', '')
    if name == '' then return reply({ success = false, error = 'This game has no name to search for.' }) end
    local now = os.time()
    if now - last_fetch_at < M.min_fetch_interval then
      return reply({ success = false, error = 'Wait a moment before the next HowLongToBeat request.' })
    end
    last_fetch_at = now
    local rows, why = fetch_rows(name)
    if not rows then return reply({ success = false, error = why }) end
    local row, quality = M.pick_match(rows, name)
    if not row then
      return reply({ success = false,
        error = 'No confident HowLongToBeat match for "' .. name .. '". You can set the times manually.' })
    end
    local store = load_store()
    local record = M.record_from_row(row, quality, now)
    store.games[tostring(math.floor(tonumber(appid)))] = record
    local saved, serr = persist_store(store)
    if not saved then return reply({ success = false, error = tostring(serr) }) end
    return reply({ success = true, record = record })
  end

  -- Manual correction path: {appid, name?, main, extra, completionist} in hours.
  registry.SlsHltbPut = function(raw)
    local ok, entry = pcall(json.decode, tostring(raw or ''))
    if not ok or type(entry) ~= 'table' or not valid_appid(entry.appid) then
      return reply({ success = false, error = 'Send {appid, name?, main?, extra?, completionist?} in hours.' })
    end
    local record = record_from_manual(entry.appid, entry)
    if not record then
      return reply({ success = false, error = 'Provide at least one of main, extra or completionist in hours.' })
    end
    local store = load_store()
    store.games[tostring(math.floor(tonumber(entry.appid)))] = record
    local saved, serr = persist_store(store)
    if not saved then return reply({ success = false, error = tostring(serr) }) end
    return reply({ success = true, record = record })
  end

  registry.SlsHltbForget = function(appid)
    if not valid_appid(appid) then return reply({ success = false, error = 'Invalid app id.' }) end
    local store = load_store()
    local key = tostring(math.floor(tonumber(appid)))
    local existed = store.games[key] ~= nil
    if existed then
      store.games[key] = nil
      local saved, serr = persist_store(store)
      if not saved then return reply({ success = false, error = tostring(serr) }) end
    end
    return reply({ success = true, removed = existed })
  end
end

return M
