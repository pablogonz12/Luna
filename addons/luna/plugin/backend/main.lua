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
local VERSION = '0.14.12'
local home = os.getenv('HOME') or ''
local base = home .. '/.local/share/Lumen/sls-addons/'
local lumen_root = home .. '/.local/share/Lumen'
-- The standalone updater copy the installer keeps here, plus the small files
-- that describe an update in flight. This tree is not synced between machines.
local update_dir = lumen_root .. '/sls-luna'
local data_dir = home .. '/.local/share/slssteam-integrations'
local store_path = data_dir .. '/hltb.json'
local library_path = data_dir .. '/hltb-library.json'
local prefs_path = data_dir .. '/luna.json'

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

-- ------------------------------------------------------------------- prefs
--
-- Luna-owned settings that more than one surface needs: the store page runs on
-- its own origin and cannot read the shell's localStorage, so the switches it
-- obeys live here, where both the panel and the store script can ask over RPC.
local PREF_KEYS = {
  store_hide_followed = 'boolean',
  -- Client settings used to live only in the shell's localStorage: a CEF cache
  -- reset lost them and the laptop could not follow the desktop. They live here
  -- now, and the panel mirrors them back into localStorage for the agent.
  beta = 'boolean',
  hltb_order = 'string',
  -- Updates: off (never checks), notify (records what it found) or install
  -- (the user timer applies a newer release whenever Steam is closed).
  auto_update = 'string',
}

-- Allowed values for the string settings.
local PREF_VALUES = {
  hltb_order = { short = true, long = true },
  auto_update = { off = true, notify = true, install = true },
}

local function default_prefs()
  return { version = 1 }
end

local function load_prefs()
  local raw = read_file(prefs_path)
  if not raw then return default_prefs() end
  local ok, decoded = pcall(json.decode, raw)
  if not ok or type(decoded) ~= 'table' then return default_prefs() end
  local prefs = default_prefs()
  for key, kind in pairs(PREF_KEYS) do
    local value = decoded[key]
    if kind == 'boolean' and type(value) == 'boolean' then
      prefs[key] = value
    elseif kind == 'string' and type(value) == 'string' then
      local allowed = PREF_VALUES[key]
      if allowed and allowed[value] then prefs[key] = value end
    end
  end
  return prefs
end

local function persist_prefs(prefs)
  fs.create_directories(fs.parent_path(prefs_path))
  local tmp = prefs_path .. '.tmp'
  local f = io.open(tmp, 'wb')
  if not f then return false, 'cannot write prefs' end
  f:write(json.encode(prefs))
  local closed = f:close()
  if not closed then os.remove(tmp); return false, 'cannot write prefs' end
  local ok, err = os.rename(tmp, prefs_path)
  if not ok then os.remove(tmp); return false, tostring(err) end
  return true
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
    return nil, 'The Lumen HTTP module is unavailable; cannot reach HowLongToBeat.', 'unavailable'
  end
  local function token()
    local response, err = http.get(INIT_URL .. tostring(os.time()) .. '000',
      { headers = request_headers(), timeout = 15 })
    if type(response) ~= 'table' then return nil, tostring(err or 'no response'), 'unavailable' end
    if response.status ~= 200 then
      return nil, 'HowLongToBeat search init failed (HTTP ' .. tostring(response.status) .. ').', 'unavailable'
    end
    local parsed = decode_body(response.body)
    if not parsed or type(parsed.token) ~= 'string' or parsed.token == '' then
      return nil, 'HowLongToBeat returned no search token.', 'unavailable'
    end
    return parsed.token
  end

  local current, why, code = token()
  if not current then return nil, why, code end
  local payload = json.encode(search_payload(name))
  for attempt = 1, 2 do
    local response, err = http.post(SEARCH_URL, payload, {
      headers = request_headers({ ['Content-Type'] = 'application/json', ['x-auth-token'] = current }),
      timeout = 20,
    })
    if type(response) ~= 'table' then
      return nil, 'HowLongToBeat request failed: ' .. tostring(err or 'no response'), 'unavailable'
    end
    if response.status == 403 and attempt == 1 then
      current, why, code = token()
      if not current then return nil, why, code end
    elseif response.status == 429 then
      return nil, 'HowLongToBeat is rate-limiting searches; wait a minute and try again.', 'rate_limit'
    elseif response.status ~= 200 then
      return nil, 'HowLongToBeat search failed (HTTP ' .. tostring(response.status) .. ').', 'unavailable'
    else
      local parsed = decode_body(response.body)
      local rows = parsed and (parsed.data or parsed.results)
      if type(rows) ~= 'table' then
        return nil, 'HowLongToBeat returned an unexpected response.', 'unavailable'
      end
      return rows
    end
  end
  return nil, 'HowLongToBeat rejected the search token twice.', 'unavailable'
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

-- A refused search is remembered too: without it the panel would show the
-- same unfetchable game as missing forever and queues would retry it.
local function record_no_match(name, now)
  return {
    id = 0,
    name = tostring(name or ''),
    match = 'none',
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

-- ---------------------------------------------------------------- health

-- Read-only diagnostics for the Health page: everything support needs to see at
-- a glance, from local files only, and never anything secret (the SteamGridDB
-- key is reported as a boolean, never its value).
local function path_exists(path)
  local handle = io.open(path, 'rb')
  if handle then handle:close(); return true end
  return false
end

local function read_json(path)
  local raw = read_file(path)
  if not raw then return nil end
  local ok, decoded = pcall(json.decode, raw)
  if ok and type(decoded) == 'table' then return decoded end
  return nil
end

local function count_keys(value)
  local count = 0
  for _ in pairs(type(value) == 'table' and value or {}) do count = count + 1 end
  return count
end

local function dir_report(path, depth)
  local report = {}
  local ok, items = pcall(fs.list, path)
  if not ok or type(items) ~= 'table' then return report end
  for _, item in ipairs(items) do
    local name = tostring(item.name or '')
    if name ~= '' and name:sub(1, 1) ~= '.' then
      local entry = { name = name, directory = item.is_directory == true }
      if item.is_directory and (depth or 0) > 0 then
        local nested = 0
        local ok_nested, nested_items = pcall(fs.list, path .. '/' .. name)
        if ok_nested and type(nested_items) == 'table' then
          for _ in ipairs(nested_items) do nested = nested + 1 end
        end
        entry.files = nested
      else
        local handle = io.open(path .. '/' .. name, 'rb')
        if handle then
          entry.size = handle:seek('end') or 0
          handle:close()
        end
      end
      report[#report + 1] = entry
    end
  end
  table.sort(report, function(a, b) return a.name < b.name end)
  return report
end

local function addon_report()
  local report = {}
  for _, name in ipairs(installed()) do
    local meta = read_json(base .. name .. '/plugin.json') or {}
    local files = 0
    for _ in ipairs(dir_report(base .. name)) do files = files + 1 end
    report[#report + 1] = {
      name = name,
      title = tostring(meta.name or name),
      version = tostring(meta.version or ''),
      files = files,
    }
  end
  return json.array(report)
end

-- Expected payload layout per add-on (mirrors scripts/luna.py ADDON_FILES).
-- Lets the Health page flag a half-installed add-on even when its
-- plugin.json version still matches the snapshot.
local EXPECTED_FILES = {
  luna = { 'backend/main.lua', 'public/agent.js', 'public/panel.js', 'plugin.json' },
  sgdb = { 'backend/main.lua', 'plugin.json' },
  graphics = { 'backend/main.lua', 'plugin.json' },
  playtime = { 'backend/main.lua', 'public/agent.js', 'public/panel.js', 'plugin.json' },
  presence = { 'backend/main.lua', 'public/agent.js', 'public/panel.js', 'plugin.json' },
  compressor = { 'backend/main.lua', 'public/panel.js', 'plugin.json' },
}

-- ------------------------------------------------------------------ updates

local function shq(value)
  return "'" .. tostring(value):gsub("'", "'\\''") .. "'"
end

local function updater_path()
  return update_dir .. '/luna.py'
end

local function python_bin()
  for _, candidate in ipairs({ '/usr/bin/python3', '/usr/local/bin/python3' }) do
    if path_exists(candidate) then return candidate end
  end
  return 'python3'
end

local function write_json(path, value)
  fs.create_directories(fs.parent_path(path))
  local handle = io.open(path, 'wb')
  if not handle then return false end
  local written = handle:write(json.encode(value))
  local closed = handle:close()
  return (written and closed) and true or false
end

-- Runs the standalone updater. Only the small actions come through here: the
-- download-and-apply belongs to the user's timer, never inside an RPC.
local function run_updater(args)
  if not path_exists(updater_path()) then
    return nil, 'the updater is not installed yet; run install --force once'
  end
  local pipe = io.popen(python_bin() .. ' ' .. shq(updater_path()) .. ' ' .. args .. ' 2>&1')
  if not pipe then return nil, 'could not run the updater' end
  local output = pipe:read('*a') or ''
  local ok, why, code = pipe:close()
  if not ok then
    local message = (output:gsub('%s+$', ''))
    return nil, (message ~= '' and message or ('the updater exited with ' .. tostring(code or why)))
  end
  return output, nil
end

local function update_state()
  local snapshot = read_json(base .. 'luna-manifest.json') or {}
  local cached = read_json(update_dir .. '/update-check.json') or {}
  local pending = read_json(update_dir .. '/pending.json') or {}
  local prefs = load_prefs()
  return {
    installed = tostring((snapshot.versions or {}).luna or ''),
    latest = tostring(cached.latest or ''),
    available = cached.available == true,
    checkedAt = tonumber(cached.checkedAt) or 0,
    checkError = tostring(cached.checkError or ''),
    pending = pending.tag ~= nil,
    pendingTag = tostring(pending.tag or ''),
    autoUpdate = tostring(prefs.auto_update or 'off'),
    -- The mode is shared between machines (the prefs file syncs) but the timer
    -- is per machine, so the page has to say whether this one is set up.
    timerUnits = path_exists(home .. '/.config/systemd/user/luna-autoupdate.timer'),
    last = read_json(update_dir .. '/autoupdate.json'),
  }
end

local function count_steam_conflicts()
  -- Syncthing sync-conflict copies of Steam's localconfig.vdf: each one is a
  -- machine that wrote recents while another copy was in flight. `luna
  -- reconcile` (Steam closed) merges them so neither machine's play history
  -- is lost to last-writer-wins.
  local users_dir = home .. '/.local/share/Steam/userdata'
  local count = 0
  local ok_users, users = pcall(fs.list, users_dir)
  if not ok_users or type(users) ~= 'table' then return 0 end
  for _, user in ipairs(users) do
    if type(user) == 'table' and user.is_directory then
      local config_dir = users_dir .. '/' .. tostring(user.name) .. '/config'
      local ok_conf, entries = pcall(fs.list, config_dir)
      if ok_conf and type(entries) == 'table' then
        for _, entry in ipairs(entries) do
          local name = tostring((type(entry) == 'table' and entry.name) or '')
          if name:match('^localconfig%.sync%-conflict%-') then count = count + 1 end
        end
      end
    elseif type(user) == 'string' then
      local config_dir = users_dir .. '/' .. user .. '/config'
      local ok_conf, entries = pcall(fs.list, config_dir)
      if ok_conf and type(entries) == 'table' then
        for _, entry in ipairs(entries) do
          local name = tostring((type(entry) == 'table' and entry.name) or entry or '')
          if name:match('^localconfig%.sync%-conflict%-') then count = count + 1 end
        end
      end
    end
  end
  return count
end

local function steam_client_state()
  -- What `luna steam status` reports on the shell, read-only for the menu:
  -- running build, pin, match, update block and local package cache.
  local state = { build = '', pinned = '', match = false, updatesBlocked = false,
    cachedBuild = '', cacheReady = false }
  local steam_root = home .. '/.local/share/Steam'
  local log_path = steam_root .. '/logs/console_log.txt'
  local handle = io.open(log_path, 'rb')
  if handle then
    local size = handle:seek('end') or 0
    handle:seek('set', math.max(0, size - 65536))
    local tail = handle:read('*a') or ''
    handle:close()
    for build in tail:gmatch('Client version:%s*(%d+)') do state.build = build end
  end
  local tools = home .. '/.local/share/Lumen/sls-luna'
  local pin_raw = read_file(tools .. '/steam-pin.json')
  if pin_raw then
    local ok, pin = pcall(json.decode, pin_raw)
    if ok and type(pin) == 'table' then state.pinned = tostring(pin.clientBuild or '') end
  end
  local cfg = read_file(steam_root .. '/steam.cfg')
  state.updatesBlocked = cfg ~= nil and cfg:find('BootStrapperInhibitAll', 1, true) ~= nil
  local cache_build = read_file(tools .. '/steam-client-cache/build.txt')
  if cache_build then
    state.cachedBuild = cache_build:gsub('%s+', '')
    state.cacheReady = state.cachedBuild ~= ''
      and path_exists(tools .. '/steam-client-cache/package')
  end
  state.match = state.build ~= '' and state.pinned ~= '' and state.build == state.pinned
  -- Queued Health-menu work (update/cache while Steam is closed).
  state.pending = ''
  do
    local pending_raw = read_file(home .. '/.local/share/Lumen/sls-luna/pending-steam.json')
    if pending_raw then
      local ok_pending, pending = pcall(json.decode, pending_raw)
      if ok_pending and type(pending) == 'table' then
        state.pending = tostring(pending.action or '')
      end
    end
  end
  -- Pacman vector (Arch/CachyOS): a system update can replace /usr/bin/steam.
  -- No root needed to detect: the local db lists the package, and IgnorePkg
  -- lives in a world-readable conf file.
  state.pacmanManaged, state.pacmanIgnored = false, false
  do
    local ok_users, entries = pcall(fs.list, '/var/lib/pacman/local')
    if ok_users and type(entries) == 'table' then
      for _, entry in ipairs(entries) do
        local name = tostring(type(entry) == 'table' and entry.name or entry or '')
        if name:match('^steam%-%d') then state.pacmanManaged = true break end
      end
    end
    if state.pacmanManaged then
      local conf = read_file('/etc/pacman.conf')
      if conf then
        for line in conf:gmatch('[^\r\n]+') do
          local stripped = line:gsub('#.*$', ''):gsub('^%s+', ''):gsub('%s+$', '')
          local key, value = stripped:match('^([^=]+)=(.+)$')
          if key and key:lower():gsub('%s+', '') == 'ignorepkg' then
            for pkg in value:gmatch('%S+') do
              if pkg == 'steam' then state.pacmanIgnored = true end
            end
          end
        end
      end
    end
  end
  return state
end

local function build_health()
  local lumen_root = home .. '/.local/share/Lumen'
  local boot_path = lumen_root .. '/lua/boot.lua'
  local boot = read_file(boot_path)
  local markers = 0
  if boot then
    for _ in boot:gmatch('SLS%-ADDONS%-INTEGRATION') do markers = markers + 1 end
  end
  local addons = addon_report()
  local snapshot = read_json(base .. 'luna-manifest.json')
  local drift = {}
  if snapshot and type(snapshot.versions) == 'table' then
    local seen = {}
    for _, entry in ipairs(addons) do
      seen[entry.name] = true
      local pinned = snapshot.versions[entry.name]
      if pinned and entry.version ~= tostring(pinned) then
        drift[#drift + 1] = entry.name .. ': installed ' .. entry.version .. ', pinned ' .. tostring(pinned)
      end
      for _, rel in ipairs(EXPECTED_FILES[entry.name] or {}) do
        if not path_exists(base .. entry.name .. '/' .. rel) then
          drift[#drift + 1] = entry.name .. '/' .. rel .. ': missing from the installed add-on'
        end
      end
    end
    for name, pinned in pairs(snapshot.versions) do
      if not seen[name] then drift[#drift + 1] = name .. ': pinned ' .. tostring(pinned) .. ', not installed' end
    end
  end
  local store = load_store()
  local sgdb = read_json(data_dir .. '/sgdb.json') or {}
  local steam_root = home .. '/.local/share/Steam'
  return {
    version = VERSION,
    generatedAt = os.time(),
    install = snapshot and {
      generated = snapshot.generated,
      installedAt = snapshot.installedAt,
      source = snapshot.source,
      versions = snapshot.versions,
      drift = json.array(drift),
    } or { drift = json.array({}) },
    lumen = {
      root = lumen_root,
      binary = path_exists(lumen_root .. '/lumen'),
      luatools = path_exists(lumen_root .. '/luatools/backend/main.lua'),
      bridge = path_exists(base .. 'bridge.lua'),
      boot = {
        path = boot_path,
        patched = markers > 0,
        markers = markers,
        backup = path_exists(boot_path .. '.sls-addons.original'),
      },
      addons = addons,
    },
    steam = {
      root = steam_root,
      exists = path_exists(steam_root .. '/steam.sh'),
      localconfigConflicts = count_steam_conflicts(),
      client = steam_client_state(),
    },
    data = {
      dir = data_dir,
      prefs = load_prefs(),
      hltb = { cached = count_keys(store.games) },
      sgdb = { hasKey = type(sgdb.apiKey) == 'string' and sgdb.apiKey ~= '' },
      files = json.array(dir_report(data_dir, 1)),
    },
  }
end

function M.register(registry)
  registry.SlsLunaInfo = function()
    return reply({ success = true, version = VERSION, addons = installed(), data = data_dir,
      prefs = load_prefs() })
  end

  -- One write surface for the switches above. Keys are whitelisted so this can
  -- never grow into a general file-write primitive. The payload arrives either
  -- as a JSON string (the panel wraps it in `raw`) or already decoded.
  registry.SlsLunaPrefsSet = function(raw)
    local entry = nil
    if type(raw) == 'table' then
      entry = raw
    else
      local ok, decoded = pcall(json.decode, tostring(raw or ''))
      if ok and type(decoded) == 'table' then entry = decoded end
    end
    if not entry or type(entry.key) ~= 'string' or #entry.key > 64 then
      return reply({ success = false, error = 'Send {key, value}.' })
    end
    local kind = PREF_KEYS[entry.key]
    if kind == nil then
      return reply({ success = false, error = 'Unknown preference.' })
    end
    if type(entry.value) ~= kind then
      return reply({ success = false, error = 'Wrong value type for ' .. entry.key .. '.' })
    end
    local allowed = PREF_VALUES[entry.key]
    if allowed and not allowed[entry.value] then
      return reply({ success = false, error = 'Unknown value for ' .. entry.key .. '.' })
    end
    local prefs = load_prefs()
    prefs[entry.key] = entry.value
    local saved, serr = persist_prefs(prefs)
    if not saved then return reply({ success = false, error = tostring(serr) }) end
    return reply({ success = true, prefs = prefs })
  end

  -- Read-only diagnostics for the Health page: paths, versions, marker counts
  -- and file sizes, so a problem can be described without a shell.
  registry.SlsLunaHealth = function()
    return reply({ success = true, health = build_health() })
  end

  -- Update plumbing for the Health page. `status` reads local files only;
  -- `check` asks GitHub through the standalone updater (cached for six hours);
  -- `install` queues the update for the user timer, which applies it while
  -- Steam is closed; `timer` toggles that unattended mode.
  registry.SlsLunaUpdate = function(raw)
    local ok, entry = pcall(json.decode, tostring(raw or ''))
    local action = (ok and type(entry) == 'table') and tostring(entry.action or '') or ''
    if action == '' or action == 'status' then
      return reply({ success = true, update = update_state() })
    end
    if action == 'check' then
      local out, err = run_updater('check' .. (entry.force == true and ' --force' or ''))
      if not out then return reply({ success = false, error = err }) end
      return reply({ success = true, update = update_state() })
    end
    if action == 'install' then
      local queued = write_json(update_dir .. '/pending.json',
        { tag = tostring(entry.tag or ''), requestedAt = os.time() })
      if not queued then return reply({ success = false, error = 'could not queue the update' }) end
      return reply({ success = true, queued = true, update = update_state() })
    end
    if action == 'cancel' then
      os.remove(update_dir .. '/pending.json')
      return reply({ success = true, update = update_state() })
    end
    if action == 'timer' then
      local value = tostring(entry.value or '')
      if value ~= 'on' and value ~= 'off' and value ~= 'status' then
        return reply({ success = false, error = 'Send on, off or status.' })
      end
      local out, err = run_updater('timer ' .. value)
      if not out then return reply({ success = false, error = err }) end
      return reply({ success = true, timer = (out:gsub('%s+$', '')), update = update_state() })
    end
    return reply({ success = false, error = 'Unknown update action.' })
  end

  -- Steam client management for the Health page. `status` is read-only;
  -- `pin` runs the standalone updater (safe while Steam is open: it only
  -- reads); queue/cancel write the pending file the timer consumes while
  -- Steam is closed. Nothing here touches a running Steam.
  registry.SlsLunaSteam = function(raw)
    local ok, entry = pcall(json.decode, tostring(raw or ''))
    local action = (ok and type(entry) == 'table') and tostring(entry.action or '') or ''
    if action == '' or action == 'status' then
      return reply({ success = true, steam = steam_client_state() })
    end
    if action == 'pin' then
      local out, err = run_updater('steam pin')
      if not out then return reply({ success = false, error = err }) end
      return reply({ success = true, message = (out:gsub('%s+$', '')),
        steam = steam_client_state() })
    end
    local pending_path = home .. '/.local/share/Lumen/sls-luna/pending-steam.json'
    if action == 'cancel' then
      os.remove(pending_path)
      return reply({ success = true, steam = steam_client_state() })
    end
    if action == 'queue-update' or action == 'queue-cache' then
      if path_exists(pending_path) then
        return reply({ success = false, error = 'A Steam task is already queued.' })
      end
      local job = action == 'queue-update' and 'update' or 'cache'
      local queued = write_json(pending_path, { action = job, at = os.time() })
      if not queued then return reply({ success = false, error = 'could not queue the task' }) end
      return reply({ success = true, queued = job, steam = steam_client_state() })
    end
    return reply({ success = false, error = 'Unknown Steam action.' })
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
  -- Failures carry a `code` so a queue can tell "this game has no match"
  -- (skip and remember) from "the site is rate-limiting us" (wait and retry).
  registry.SlsHltbFetch = function(appid, name)
    if not valid_appid(appid) then
      return reply({ success = false, code = 'invalid', error = 'Invalid app id.' })
    end
    name = tostring(name or ''):gsub('^%s+', ''):gsub('%s+$', '')
    if name == '' then
      return reply({ success = false, code = 'invalid', error = 'This game has no name to search for.' })
    end
    local now = os.time()
    if now - last_fetch_at < M.min_fetch_interval then
      return reply({ success = false, code = 'throttled',
        error = 'Wait a moment before the next HowLongToBeat request.' })
    end
    last_fetch_at = now
    local rows, why, code = fetch_rows(name)
    local store = load_store()
    local key = tostring(math.floor(tonumber(appid)))
    if not rows then
      return reply({ success = false, code = code or 'unavailable', error = why })
    end
    local row, quality = M.pick_match(rows, name)
    if not row then
      -- Remember the refusal so the game stops counting as "missing" and the
      -- queue does not retry it on every run.
      store.games[key] = record_no_match(name, now)
      local saved, serr = persist_store(store)
      if not saved then return reply({ success = false, code = 'unavailable', error = tostring(serr) }) end
      return reply({ success = false, code = 'no_match',
        error = 'No confident HowLongToBeat match for "' .. name .. '". Set the times manually or try Search.' })
    end
    local record = M.record_from_row(row, quality, now)
    store.games[key] = record
    local saved, serr = persist_store(store)
    if not saved then return reply({ success = false, code = 'unavailable', error = tostring(serr) }) end
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
