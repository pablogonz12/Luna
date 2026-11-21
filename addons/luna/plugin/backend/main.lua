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
local VERSION = '0.14.19'
local home = os.getenv('HOME') or ''
-- OPTIONAL third-party host (Lumen). This is the only place in this backend
-- that names a path under Lumen: every other read goes through
-- optional_lumen_root() and missing files mean "not present", never an error.
-- Nothing here is required by a Luna page; with no Lumen installed the
-- backend behaves exactly as on luna-host.
local function optional_lumen_root()
  return home .. '/.local/share/Lumen'
end
local lumen_root = optional_lumen_root()
-- luna-host (Luna's own plugin host) exports LUNA_HOME; the third-party Lumen
-- host never does. That decides where add-ons and Luna's state live.
local luna_home = os.getenv('LUNA_HOME') or ''
local on_luna_host = luna_home ~= ''
local base = (on_luna_host and (luna_home .. '/addons/') or (lumen_root .. '/sls-addons/'))
-- The standalone updater copy the installer keeps here, plus the small files
-- that describe an update in flight. This tree is not synced between machines.
local SESSION_STARTED_AT = os.time()
local update_dir = on_luna_host and (luna_home .. '/state') or (lumen_root .. '/sls-luna')
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
-- In-flight non-blocking searches by app id; see M.advance_fetch_job.
M.fetch_job_ttl = 60
local fetch_jobs = {}

local function reply(data) return json.encode(data) end

local function read_file(path)
  local f = io.open(path, 'rb')
  if not f then return nil end
  local raw = f:read('*a')
  f:close()
  return raw
end

-- --------------------------------------------------------------- durability
--
-- Every store this backend owns is written to a temporary name and renamed over
-- the target, so a reader never sees a half-written file. The previous good
-- copy is kept as <file>.bak first: a truncated store is then recovered on the
-- next read instead of starting from nothing. These files live in a
-- Syncthing-shared directory (~/.local/share/slssteam-integrations), so each
-- one also has a hard size cap; the numbers below are the answer to "how big
-- may this file get":
--   hltb.json          4 MB  (and at most 8000 records)
--   hltb-library.json  2 MB  (the agent already sends at most 5000 entries)
--   luna.json          64 KB (a dozen booleans and short strings)
--   hltb-wire.txt      6 MB  (a cache of the encoded store, ~1.35x its size; dropped and rebuilt)
--   sidecars           8 KB  each (count, scorer marker, hygiene summary)
--   *.bak              same cap as the file it mirrors, one copy
--   *.corrupt-<ts>     the broken original, at most 3 kept per file
local FILE_CAPS = {
  store = 4 * 1024 * 1024,
  library = 2 * 1024 * 1024,
  prefs = 64 * 1024,
  wire = 6 * 1024 * 1024,
  sidecar = 8 * 1024,
}
local MAX_STORE_RECORDS = 8000
local MAX_CORRUPT_COPIES = 3
local BAK_SUFFIX = '.bak'
-- Only the last few recoveries are worth carrying into a reply; they are
-- transient on purpose (the files themselves are the durable record).
local recovery_log = {}

local function note_recovery(entry)
  recovery_log[#recovery_log + 1] = entry
  while #recovery_log > 5 do table.remove(recovery_log, 1) end
end

local function last_recovery()
  return recovery_log[#recovery_log]
end

local function file_exists(path)
  local handle = io.open(path, 'rb')
  if handle then handle:close(); return true end
  return false
end

local function read_json_small(path)
  local raw = read_file(path)
  if not raw then return nil end
  local ok, parsed = pcall(json.decode, raw)
  if ok and type(parsed) == 'table' then return parsed end
  return nil
end

-- tmp + rename only, no backup: the primitive the .bak copy itself uses.
local function write_bytes(path, text)
  if type(text) ~= 'string' then return false, 'nothing to write' end
  local dir = fs.parent_path(path)
  if dir and dir ~= '' then fs.create_directories(dir) end
  local tmp = path .. '.tmp'
  local handle, why = io.open(tmp, 'wb')
  if not handle then return false, tostring(why or ('cannot write ' .. path)) end
  local written = handle:write(text)
  local closed = handle:close()
  if not written or not closed then
    os.remove(tmp)
    return false, 'write failed for ' .. path
  end
  if not os.rename(tmp, path) then
    os.remove(tmp)
    return false, 'rename failed for ' .. path
  end
  return true
end

-- The durable write: keep the current good copy as .bak, then replace atomically.
local function atomic_write(path, text, cap)
  if type(text) ~= 'string' then return false, 'nothing to write' end
  if cap and #text > cap then
    return false, (path:match('[^/]+$') or path) .. ' would exceed its '
      .. tostring(cap) .. '-byte cap (' .. tostring(#text) .. ' bytes)'
  end
  local previous = read_file(path)
  if previous and previous ~= '' and (not cap or #previous <= cap) then
    -- Best effort: a missing .bak must not lose the new write.
    write_bytes(path .. BAK_SUFFIX, previous)
  end
  return write_bytes(path, text)
end

-- A corrupt original is kept for inspection, never deleted silently. At most
-- MAX_CORRUPT_COPIES are retained per file; the oldest goes when a fourth
-- arrives and the recovery message says so.
local function prune_corrupt(path)
  local dir = fs.parent_path(path)
  local prefix = (path:match('[^/]+$') or path) .. '.corrupt-'
  local ok, items = pcall(fs.list, dir)
  if not ok or type(items) ~= 'table' then return end
  local found = {}
  for _, item in ipairs(items) do
    local name = tostring((type(item) == 'table' and item.name) or item or '')
    if name:sub(1, #prefix) == prefix then found[#found + 1] = name end
  end
  table.sort(found)
  local removed = {}
  while #found > MAX_CORRUPT_COPIES do
    local oldest = table.remove(found, 1)
    if os.remove(dir .. '/' .. oldest) then removed[#removed + 1] = oldest end
  end
  return removed
end

local function keep_corrupt(path)
  local base = path .. '.corrupt-' .. tostring(os.time())
  local candidate, index = base, 1
  while file_exists(candidate) do
    index = index + 1
    candidate = base .. '-' .. tostring(index)
  end
  if not os.rename(path, candidate) then return nil end
  local removed = prune_corrupt(path)
  return candidate, removed
end

-- Reads a JSON store, recovering the previous good copy when the live file is
-- torn. Returns the decoded table, or nil when nothing usable is left (the file
-- is then kept as .corrupt-<ts> and the caller starts from a blank store).
local function read_store_file(path, label)
  local raw = read_file(path)
  if not raw then return nil end
  local ok, parsed = pcall(json.decode, raw)
  if ok and type(parsed) == 'table' then return parsed end
  local backup = read_file(path .. BAK_SUFFIX)
  local restored = nil
  if backup then
    local ok_backup, decoded = pcall(json.decode, backup)
    if ok_backup and type(decoded) == 'table' then restored = decoded end
  end
  local kept, pruned = keep_corrupt(path)
  local kept_name = kept and kept:match('[^/]+$') or nil
  if restored then
    write_bytes(path, backup)
    note_recovery({
      file = label,
      recovered = true,
      from = 'bak',
      kept = kept_name,
      message = label .. ' was unreadable; the previous good copy (' .. label .. BAK_SUFFIX
        .. ') was restored' .. (kept_name and (' and the broken file is kept as ' .. kept_name) or '') .. '.',
    })
    return restored
  end
  note_recovery({
    file = label,
    recovered = false,
    kept = kept_name,
    pruned = pruned,
    error = label .. ' was unreadable and no usable backup exists; '
      .. (kept_name and ('the broken file is kept as ' .. kept_name .. ' and ') or '')
      .. 'it started empty. Nothing was discarded.',
  })
  return nil
end

-- ------------------------------------------------------------------- caching
--
-- Two read-only RPCs build a whole payload from local files on every call
-- (SlsHltbGet re-encodes the entire cache, ~1 MB; SlsLunaHealth walks every
-- add-on and data directory). Both are polled, so the answer is remembered
-- until one of its sources changes. `rev` is a compact string of each source's
-- size and modification time: a polling client can send it back and get
-- {unchanged = true} instead of a payload it already has. Without lfs (a
-- host without stat) only the size is known, so a same-size rewrite is noticed
-- at the next TTL, which is why the TTL stays short.
local CACHE_TTL = 30

local lfs = select(2, pcall(require, 'lfs'))
if type(lfs) ~= 'table' or type(lfs.attributes) ~= 'function' then lfs = nil end
-- Without any stat source a cache cannot tell "changed" from "unchanged", so
-- it stays off and the RPC behaves exactly as before.
local has_stat = lfs ~= nil or type(fs.size) == 'function'
-- Only a modification time makes a same-size rewrite visible, so only then may
-- a client be told `unchanged`.
local mtime_known = lfs ~= nil

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

-- `text` is the exact wire string, so a hit costs one table lookup and no
-- decode or encode at all.
local hltb_cache = { rev = '', text = nil, expires = 0 }
local health_cache = { rev = '', text = nil, expires = 0 }

local function cache_drop(cache)
  cache.rev, cache.text, cache.expires = '', nil, 0
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
  -- Steam window gradient that follows the Noctalia palette. On unless the
  -- user turns it off (a missing key counts as on).
  steam_gradient = 'boolean',
  -- Client settings used to live only in the shell's localStorage: a CEF cache
  -- reset lost them and the laptop could not follow the desktop. They live here
  -- now, and the panel mirrors them back into localStorage for the agent.
  beta = 'boolean',
  hltb_order = 'string',
  -- Updates: off (never checks), notify (records what it found) or install
  -- (the user timer applies a newer release whenever Steam is closed).
  auto_update = 'string',
  -- Health page language. The technical paths and bug-report dumps stay in
  -- English in both languages; the attention list, headers and actions switch.
  lang = 'string',
  -- Whether Lumen's own launcher stays in the menubar. Off by default: Luna's
  -- menu carries a Lumen page, so there is one launcher and one menu.
  lumen_button = 'boolean',
  -- Whether the Lumen page renders Lumen's UI inside Luna's window (the
  -- default) instead of closing Luna and opening Lumen's own overlay.
  lumen_embed = 'boolean',
}

-- Allowed values for the string settings.
local PREF_VALUES = {
  hltb_order = { short = true, long = true },
  auto_update = { off = true, notify = true, install = true },
  lang = { en = true, es = true },
}

local function default_prefs()
  return { version = 1 }
end

local function load_prefs()
  local decoded = read_store_file(prefs_path, 'luna.json')
  if not decoded then return default_prefs() end
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
  local ok, err = atomic_write(prefs_path, json.encode(prefs), FILE_CAPS.prefs)
  if not ok then return false, tostring(err) end
  return true
end

-- ------------------------------------------------------------------ storage

local function blank_store()
  return { version = 1, games = {} }
end

-- Decoding the store (~700 KB) is the most expensive thing a page-load handler
-- can do, and two of them read it: SlsHltbGet (to encode the reply) and
-- SlsLunaHealth (only to count its records). So the decoded table is kept
-- until the file's stamp moves, and the count lives in a tiny sidecar written
-- whenever the store is decoded or saved: Health then never decodes the store
-- at all, and a page load pays one decode at most.
local store_memo = { rev = '', data = nil }
local count_path = data_dir .. '/hltb-count.json'

local function store_stamp()
  if not has_stat then return nil end
  return file_stamp(store_path)
end

local function read_sidecar_count(rev)
  if not rev then return nil end
  local handle = io.open(count_path, 'rb')
  if not handle then return nil end
  local raw = handle:read('*a')
  handle:close()
  local ok, parsed = pcall(json.decode, raw or '')
  if ok and type(parsed) == 'table' and parsed.rev == rev and type(parsed.count) == 'number' then
    return parsed.count
  end
  return nil
end

local function write_sidecar_count(rev, count)
  if not rev or type(count) ~= 'number' then return end
  atomic_write(count_path, json.encode({ rev = rev, count = count }), FILE_CAPS.sidecar)
end

-- Which scorer produced the stored matches. Bumped whenever the matching rules
-- change: the next read then re-validates every stored record once (no network)
-- against the Steam name in the library, marks the ones that no longer score as
-- a confident match `needsReview`/`stale` and leaves them in place, so a fetch
-- replaces them with a fresh answer. The marker keeps that pass off the cached
-- page-load path: a matching rev means the store was already reviewed.
local SCORER_VERSION = 2
local scorer_path = data_dir .. '/hltb-scorer.json'

local function review_count(store)
  local count = 0
  for _, record in pairs(type(store) == 'table' and type(store.games) == 'table' and store.games or {}) do
    if type(record) == 'table' and record.needsReview then count = count + 1 end
  end
  return count
end

local function write_scorer_marker(rev, scorer, review)
  if not rev then return end
  atomic_write(scorer_path, json.encode({ rev = rev, scorer = scorer or 0,
    needsReview = review or 0 }), FILE_CAPS.sidecar)
end

local function read_scorer_marker()
  local marker = read_json_small(scorer_path)
  if type(marker) ~= 'table' then marker = read_json_small(scorer_path .. BAK_SUFFIX) end
  if type(marker) ~= 'table' then return nil end
  return marker
end

local function validation_pending()
  local rev = store_stamp()
  if not rev then
    -- Without stat the marker cannot be trusted across calls; only the memo can.
    return not (store_memo.data and store_memo.data.scorer == SCORER_VERSION)
  end
  local marker = read_scorer_marker()
  if not marker then return true end
  return marker.rev ~= rev or tonumber(marker.scorer) ~= SCORER_VERSION
end

-- The exact SlsHltbGet wire text is kept on disk as well, keyed by `rev`: a
-- fresh Lua VM (a Steam restart) then serves its first HLTB page with one file
-- read instead of decoding the store and re-encoding ~1 MB. Two files, not one
-- wrapping JSON, so the text is never escaped again.
local wire_cache = { rev = '', text = nil, expires = 0 }
local wire_text_path = data_dir .. '/hltb-wire.txt'
local wire_rev_path = data_dir .. '/hltb-wire.rev'

local function read_wire_text(rev)
  if not rev then return nil end
  local handle = io.open(wire_rev_path, 'rb')
  if not handle then return nil end
  local stored = (handle:read('*a') or ''):gsub('%s+$', '')
  handle:close()
  if stored ~= rev then return nil end
  local body = io.open(wire_text_path, 'rb')
  if not body then return nil end
  local text = body:read('*a')
  body:close()
  if not text or #text < 2 then return nil end
  return text
end

local function write_wire_text(rev, text)
  if not rev or type(text) ~= 'string' then return end
  if #text > FILE_CAPS.wire then return end
  if not write_bytes(wire_text_path, text) then return end
  write_bytes(wire_rev_path, rev)
end

-- What the reply actually needs: the times, the name and the match quality.
-- No reader in the repo (panel.js, agent.js, the store page) reads a record's
-- `source` or `fetchedAt`, and those two fields are ~22% of the wire text
-- (~210 KB of 950 KB here), so the read path leaves them out. Fetch and Put
-- still return the whole record they just stored. Review flags are kept: the
-- panel needs them to badge a record the re-validation no longer trusts.
local function wire_games(games)
  local out = {}
  for key, record in pairs(games) do
    if type(record) == 'table' then
      out[key] = { name = record.name, match = record.match, id = record.id,
        main = record.main, extra = record.extra, all = record.all,
        completionist = record.completionist,
        needsReview = record.needsReview == true or nil }
    end
  end
  return out
end

local function load_store()
  local rev = store_stamp()
  if rev and store_memo.rev == rev and store_memo.data then return store_memo.data end
  local parsed = read_store_file(store_path, 'hltb.json')
  if not parsed or type(parsed.games) ~= 'table' then
    if parsed then
      note_recovery({ file = 'hltb.json', recovered = false,
        error = 'hltb.json has no game table; it started empty and the file was left in place.' })
    end
    return blank_store()
  end
  parsed.version = 1
  if rev then store_memo.rev, store_memo.data = rev, parsed end
  return parsed
end

-- Durable like the Playtime ledger: .bak of the previous good copy, then tmp
-- file + rename, so a crash never leaves a half-written store behind.
local function persist_store(data)
  local games = type(data.games) == 'table' and data.games or {}
  local count = 0
  for _ in pairs(games) do count = count + 1 end
  if count > MAX_STORE_RECORDS then
    return false, 'the HowLongToBeat store is full (' .. tostring(count) .. ' of '
      .. tostring(MAX_STORE_RECORDS) .. ' records)'
  end
  local ok, err = atomic_write(store_path, json.encode(data), FILE_CAPS.store)
  if not ok then return false, tostring(err) end
  cache_drop(hltb_cache)
  cache_drop(wire_cache)
  local rev = store_stamp()
  if rev then store_memo.rev, store_memo.data = rev, data end
  write_sidecar_count(rev, count)
  write_scorer_marker(rev, data.scorer or 0, review_count(data))
  return true
end

-- The game list the Steam agent reports (appid + display name). Kept apart from
-- the times themselves: it is inventory, not measurements, and the panel uses
-- it to show what is still missing. Written atomically like the store.
local function load_library()
  local parsed = read_store_file(library_path, 'hltb-library.json')
  if not parsed or type(parsed.games) ~= 'table' then
    return { version = 1, updatedAt = 0, games = {} }
  end
  parsed.version = 1
  return parsed
end

local function persist_library(data)
  local ok, err = atomic_write(library_path, json.encode(data), FILE_CAPS.library)
  if not ok then return false, tostring(err) end
  cache_drop(hltb_cache)
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
  out = out:gsub("['\226\128\153]", '') -- apostrophes: Assassin's == Assassins
  out = out:gsub('%s*&%s*', ' and ')
  for bytes, plain in pairs(ACCENTS) do out = out:gsub(bytes, plain) end
  out = out:gsub('[^%w]+', ' ')
  out = out:gsub('^%s+', ''):gsub('%s+$', '')
  -- A leading article is not part of the title ("The Witcher 3" == "Witcher 3").
  out = out:gsub('^the%s+', '')
  return out
end

local ROMAN = {
  i = 1, ii = 2, iii = 3, iv = 4, v = 5, vi = 6, vii = 7, viii = 8, ix = 9, x = 10,
  xi = 11, xii = 12, xiii = 13, xiv = 14, xv = 15, xvi = 16, xvii = 17, xviii = 18,
  xix = 19, xx = 20,
}
local NUMBER_WORDS = {
  one = 1, two = 2, three = 3, four = 4, five = 5, six = 6, seven = 7, eight = 8,
  nine = 9, ten = 10, eleven = 11, twelve = 12, thirteen = 13, fourteen = 14,
  fifteen = 15, sixteen = 16, seventeen = 17, eighteen = 18, nineteen = 19, twenty = 20,
}
-- Edition words describe another release of the SAME game, so they are noise at
-- the end of a title. "Part"/"Chapter" are NOT here: they mark a sequel.
local EDITION_TOKENS = {
  remastered = true, remaster = true, remake = true, redux = true, hd = true, goty = true,
  definitive = true, deluxe = true, ultimate = true, complete = true, enhanced = true,
  special = true, collectors = true, edition = true, anniversary = true, gold = true,
  game = true, of = true, the = true, year = true, directors = true, director = true,
  cut = true, final = true, extended = true, legacy = true, premium = true, digital = true,
}
-- A candidate that is really a soundtrack, demo or playtest of the game is not
-- the game: the query would get its times from the wrong page.
local NON_GAME_TOKENS = {
  soundtrack = true, ost = true, demo = true, playtest = true, beta = true, dlc = true,
  expansion = true, expansions = true, artbook = true, sdk = true, dedicated = true,
  server = true, benchmark = true, editor = true,
}
local MATCH_FLOOR = 0.6

-- A trailing parenthesised/bracketed year ("Resident Evil 4 (2023)") is a
-- release marker, not part of the title; a bare trailing year is a sequel year
-- and stays in the tokens (see classify_suffix).
local function strip_bracket_year(text)
  local out = tostring(text or '')
  for _, pattern in ipairs({ '(%s*%b()%s*)$', '(%s*%b[]%s*)$' }) do
    out = out:gsub(pattern, function(group)
      local inner = group:gsub('[%(%)%[%]%s]', '')
      if inner:match('^%d%d%d%d$') then return '' end
      return group
    end)
  end
  return out
end

-- The comparison form of a title: normalised words, no leading article, roman
-- numerals as arabic (so "II" == "2" and "V" == "5"), no edition suffix.
local function core_tokens(text)
  local normalized = M.normalize(strip_bracket_year(text))
  if normalized == '' then return {} end
  local list = {}
  for token in normalized:gmatch('%S+') do list[#list + 1] = token end
  for index, token in ipairs(list) do
    local value = ROMAN[token]
    if value then list[index] = tostring(value) end
  end
  while #list > 1 and EDITION_TOKENS[list[#list]] do table.remove(list) end
  return list
end

local function is_year(token)
  return token and token:match('^%d%d%d%d$') ~= nil
    and tonumber(token) >= 1950 and tonumber(token) <= 2035
end

local function is_number_token(token)
  if not token then return false end
  if token:match('^%d+$') then return true end
  return NUMBER_WORDS[token] ~= nil
end

-- What a longer title adds after the shared prefix: a subtitle ("Wild Hunt",
-- same game), a sequel marker ("II", "2", "Part 2", "2024") or the name of a
-- non-game page (a soundtrack, a demo, a playtest).
local function classify_suffix(suffix)
  if #suffix == 0 then return 'subtitle' end
  for _, token in ipairs(suffix) do
    if NON_GAME_TOKENS[token] then return 'nongame' end
  end
  local head = suffix[1]
  if is_number_token(head) then return 'sequel' end
  if (head == 'part' or head == 'chapter' or head == 'episode' or head == 'vol'
      or head == 'volume' or head == 'season' or head == 'act') and is_number_token(suffix[2]) then
    return 'sequel'
  end
  return 'subtitle'
end

local function coverage(list, other)
  local set = {}
  for _, token in ipairs(other) do set[token] = true end
  local hits = 0
  for _, token in ipairs(list) do if set[token] then hits = hits + 1 end end
  return hits / #list
end

-- How well one candidate title answers the query: (score, quality) or nil.
-- Precision matters more than recall: a sequel, a soundtrack or a playtest page
-- must never supply the times of the game itself, so those score 'weak' and
-- pick_match refuses them. Editions of the same game (Remastered, Definitive,
-- GOTY, Director's Cut) are stripped and still match.
function M.name_score(query, name)
  local q = core_tokens(query)
  local c = core_tokens(name)
  if #q == 0 or #c == 0 then return nil end
  local qs, cs = table.concat(q, ' '), table.concat(c, ' ')
  if qs == cs then return 1, 'exact' end
  local short, long
  if #c ~= #q then
    if #c > #q then short, long = q, c else short, long = c, q end
  end
  if short then
    local shared = true
    for index = 1, #short do
      if short[index] ~= long[index] then shared = false; break end
    end
    if shared then
      local suffix = {}
      for index = #short + 1, #long do suffix[#suffix + 1] = long[index] end
      local kind = classify_suffix(suffix)
      if kind == 'sequel' or kind == 'nongame' then return 0.35, 'weak' end
      -- One bare word plus one extra word is too generic to trust: "Doom" must
      -- not take "DOOM Eternal", "Hades" must not take "Hades' Star". A longer
      -- subtitle ("LISA: The Painful") is the usual same-game shape and stays.
      if #short == 1 and #suffix == 1 then return 0.45, 'weak' end
      return 0.9, 'strong'
    end
  end
  local q_cov = coverage(q, c)
  local c_cov = coverage(c, q)
  local q_set, c_set = {}, {}
  for _, token in ipairs(q) do q_set[token] = true end
  for _, token in ipairs(c) do c_set[token] = true end
  -- A number on one side only is a sequel boundary, never a coincidence.
  for _, token in ipairs(q) do
    if not c_set[token] and (token:match('^%d+$') or is_year(token)) then return 0.3, 'weak' end
  end
  for _, token in ipairs(c) do
    if not q_set[token] and (token:match('^%d+$') or is_year(token)) then return 0.3, 'weak' end
  end
  if q_cov >= 0.8 and c_cov >= 0.7 then
    return math.min(0.8 * q_cov + 0.2 * c_cov, 0.82), 'strong'
  end
  return math.min((0.8 * q_cov + 0.2 * c_cov) * 0.6, 0.5), 'weak'
end

-- Release year, when the site's row carries one. The search endpoint does not
-- today, but a row with a year lets a tie be broken instead of guessed.
local function row_year(row)
  for _, field in ipairs({ 'release_world', 'release_date', 'release', 'year' }) do
    local value = tonumber(row[field])
    if value and value >= 1950 and value <= 2035 then return math.floor(value) end
  end
  for _, field in ipairs({ 'release_world', 'release_date', 'release' }) do
    local found = tostring(row[field] or ''):match('(%d%d%d%d)')
    if found then
      local value = tonumber(found)
      if value >= 1950 and value <= 2035 then return value end
    end
  end
  return nil
end

local function query_year(query)
  for found in tostring(query or ''):gmatch('%d%d%d%d') do
    local value = tonumber(found)
    if value >= 1950 and value <= 2035 then return value end
  end
  return nil
end

-- Best candidate for `query` among the site's rows. Ties prefer exact
-- normalised equality, then a release year that matches a year in the query.
-- Returns nil when nothing is a confident match, so the caller stores
-- "no confident match" instead of the best weak guess.
function M.pick_match(rows, query)
  local best, best_score, best_quality, best_exact, best_year
  local wanted = query_year(query)
  for _, row in ipairs(rows or {}) do
    if type(row) == 'table' then
      local score, quality = M.name_score(query, row.game_name or row.name)
      if score and quality ~= 'weak' and score >= MATCH_FLOOR then
        local exact = quality == 'exact'
        local year = row_year(row)
        local better = false
        if best == nil then
          better = true
        elseif exact ~= best_exact then
          better = exact
        elseif score > best_score + 0.0001 then
          better = true
        elseif math.abs(score - best_score) <= 0.0001 and wanted and year then
          better = (best_year ~= wanted) and (year == wanted)
        end
        if better then
          best, best_score, best_quality, best_exact, best_year = row, score, quality, exact, year
        end
      end
    end
  end
  if best == nil then return nil end
  return best, best_quality, best_score
end

-- One-time, idempotent re-validation of the stored times against the current
-- scorer, using the Steam names from the library (no network). A record whose
-- title no longer scores as a confident match is marked `needsReview`/`stale`,
-- never deleted: the times stay visible (flagged) and the next fetch of that
-- game replaces the record with a fresh answer.
function M.revalidate_store(store, library, now)
  if type(store) ~= 'table' or type(store.games) ~= 'table' then return 0, 0, false end
  local titles = {}
  if type(library) == 'table' and type(library.games) == 'table' then
    for _, item in ipairs(library.games) do
      if type(item) == 'table' and item.appid ~= nil and type(item.name) == 'string' then
        local key = tostring(math.floor(tonumber(item.appid) or 0))
        if key ~= '0' then titles[key] = item.name end
      end
    end
  end
  local checked, flagged, changed = 0, 0, false
  for key, record in pairs(store.games) do
    if type(record) == 'table' then
      local skip = record.match == 'manual' or record.match == 'none' or record.source == 'manual'
        or type(record.name) ~= 'string' or record.name == ''
      local title = titles[tostring(key)]
      if not skip and title and title ~= '' then
        checked = checked + 1
        local score, quality = M.name_score(title, record.name)
        local strong = score ~= nil and quality ~= 'weak' and score >= MATCH_FLOOR
        if strong then
          if record.needsReview or record.stale then
            record.needsReview, record.stale, record.staleReason = nil, nil, nil
            changed = true
          end
          if record.match ~= quality then record.match = quality; changed = true end
        else
          if not record.needsReview or not record.stale then changed = true end
          record.needsReview = true
          record.stale = true
          record.staleReason = 'the stored title no longer matches "' .. title .. '"'
        end
      end
      if record.needsReview then flagged = flagged + 1 end
    end
  end
  if store.scorer ~= SCORER_VERSION then store.scorer = SCORER_VERSION; changed = true end
  return checked, flagged, changed
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

-- Response checks shared by the blocking and the non-blocking paths.
local function token_from(response, err)
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

-- Rows, or nil + reason + code; code 'expired' asks the caller for a new token.
local function rows_from(response, err)
  if type(response) ~= 'table' then
    return nil, 'HowLongToBeat request failed: ' .. tostring(err or 'no response'), 'unavailable'
  end
  if response.status == 403 then
    return nil, 'HowLongToBeat rejected the search token twice.', 'expired'
  elseif response.status == 429 then
    return nil, 'HowLongToBeat is rate-limiting searches; wait a minute and try again.', 'rate_limit'
  elseif response.status ~= 200 then
    return nil, 'HowLongToBeat search failed (HTTP ' .. tostring(response.status) .. ').', 'unavailable'
  end
  local parsed = decode_body(response.body)
  local rows = parsed and (parsed.data or parsed.results)
  if type(rows) ~= 'table' then
    return nil, 'HowLongToBeat returned an unexpected response.', 'unavailable'
  end
  return rows
end

local function token_request()
  return INIT_URL .. tostring(os.time()) .. '000', { headers = request_headers(), timeout = 15 }
end

local function search_request(name, token)
  return SEARCH_URL, json.encode(search_payload(name)), {
    headers = request_headers({ ['Content-Type'] = 'application/json', ['x-auth-token'] = token }),
    timeout = 20,
  }
end

-- Two requests, exactly like the site's own search page: fetch a token, then
-- POST the query with it. A 403 means the token expired; refresh once.
-- Returns rows, or nil + a user-facing reason. This blocking form is only the
-- fallback for a Lumen without http.start/http.poll.
local function fetch_rows(name)
  if not ok_http or type(http) ~= 'table'
      or type(http.get) ~= 'function' or type(http.post) ~= 'function' then
    return nil, 'The Lumen HTTP module is unavailable; cannot reach HowLongToBeat.', 'unavailable'
  end
  local function token()
    local url, options = token_request()
    return token_from(http.get(url, options))
  end
  local current, why, code = token()
  if not current then return nil, why, code end
  for attempt = 1, 2 do
    local rows
    rows, why, code = rows_from(http.post(search_request(name, current)))
    if code == 'expired' and attempt == 1 then
      current, why, code = token()
      if not current then return nil, why, code end
    elseif code == 'expired' then
      return nil, why, 'unavailable'
    else
      return rows, why, code
    end
  end
end

-- Non-blocking form. Lumen runs every RPC on its single injector thread, so a
-- blocking search froze Steam's UI for each game of a fetch pass. A job holds
-- one in-flight request; each SlsHltbFetch call polls it without waiting and
-- reports 'pending' until the rows (or the reason there are none) are known.
local function async_http()
  return ok_http and type(http) == 'table'
    and type(http.start) == 'function' and type(http.poll) == 'function'
end

local function start_token(job)
  local url, options = token_request()
  local ok, handle, err = pcall(http.start, url, options)
  if not ok or not handle then
    return nil, 'HowLongToBeat request failed: ' .. tostring(ok and err or handle), 'unavailable'
  end
  job.step, job.handle = 'token', handle
  return true
end

local function start_search(job)
  local url, body, options = search_request(job.name, job.token)
  options.method, options.data = 'POST', body
  local ok, handle, err = pcall(http.start, url, options)
  if not ok or not handle then
    return nil, 'HowLongToBeat request failed: ' .. tostring(ok and err or handle), 'unavailable'
  end
  job.step, job.handle = 'search', handle
  return true
end

function M.new_fetch_job(name)
  local job = { name = name, attempt = 1, startedAt = os.time() }
  local started, why, code = start_token(job)
  if not started then return nil, why, code end
  return job
end

-- 'pending', or 'done' + rows, or 'failed' + reason + code.
function M.advance_fetch_job(job)
  local ok, done, response, err = pcall(http.poll, job.handle, 0)
  if not ok then return 'failed', nil, 'HowLongToBeat request failed: ' .. tostring(done), 'unavailable' end
  if not done then return 'pending' end
  if job.step == 'token' then
    local token, why, code = token_from(response, err)
    if not token then return 'failed', nil, why, code end
    job.token = token
    local started
    started, why, code = start_search(job)
    if not started then return 'failed', nil, why, code end
    return 'pending'
  end
  local rows, why, code = rows_from(response, err)
  if code == 'expired' and job.attempt == 1 then
    job.attempt = 2
    local started
    started, why, code = start_token(job)
    if not started then return 'failed', nil, why, code end
    return 'pending'
  end
  if not rows then return 'failed', nil, why, code == 'expired' and 'unavailable' or code end
  return 'done', rows
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

-- The Health page only shows the number of cached games. Read the sidecar the
-- store writes on save/decode; decode once (and remember) only when it is
-- missing or the store moved, never on the plain status read.
local function store_game_count()
  local rev = store_stamp()
  local cached = read_sidecar_count(rev)
  if cached then return cached end
  local count = count_keys(load_store().games)
  write_sidecar_count(rev, count)
  return count
end

local FILE_CAP_BY_NAME = {
  ['hltb.json'] = FILE_CAPS.store,
  ['hltb-library.json'] = FILE_CAPS.library,
  ['luna.json'] = FILE_CAPS.prefs,
  ['hltb-wire.txt'] = FILE_CAPS.wire,
  ['hltb-count.json'] = FILE_CAPS.sidecar,
  ['hltb-scorer.json'] = FILE_CAPS.sidecar,
  ['hltb-hygiene.json'] = FILE_CAPS.sidecar,
}

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
        local cap = FILE_CAP_BY_NAME[name]
        if not cap and name:match('sync%-conflict%-') then cap = FILE_CAPS.store end
        if not cap and name:match('%.corrupt%-%d') then cap = FILE_CAPS.store end
        if not cap and name:match('%.bak$') then cap = FILE_CAP_BY_NAME[name:gsub('%.bak$', '')] end
        if cap then entry.cap = cap end
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
  -- Python reads these files while this runs: write a temp name, then rename,
  -- so a reader never sees a half-written file.
  local temp = path .. '.tmp'
  local handle = io.open(temp, 'wb')
  if not handle then return false end
  local written = handle:write(json.encode(value))
  local closed = handle:close()
  if not (written and closed) then os.remove(temp); return false end
  if os.rename(temp, path) then return true end
  os.remove(temp)
  return false
end

-- Lumen runs every RPC on its single injector thread, so a call that waits
-- here stalls Steam's UI and every other add-on. Synchronous runs are capped
-- with coreutils timeout; the network check never runs synchronously at all.
local function bounded(seconds)
  for _, candidate in ipairs({ '/usr/bin/timeout', '/bin/timeout' }) do
    if path_exists(candidate) then
      return candidate .. ' -k 5 ' .. tostring(math.floor(seconds)) .. ' '
    end
  end
  return ''
end

-- Runs the standalone updater. Only the small actions come through here: the
-- download-and-apply belongs to the user's timer, never inside an RPC.
local function run_updater(args, seconds)
  if not path_exists(updater_path()) then
    return nil, 'the updater is not installed yet; run install --force once'
  end
  local pipe = io.popen(bounded(seconds or 60) .. shq(python_bin()) .. ' ' .. shq(updater_path())
    .. ' ' .. args .. ' 2>&1')
  if not pipe then return nil, 'could not run the updater' end
  local output = pipe:read('*a') or ''
  local ok, why, code = pipe:close()
  if not ok then
    local message = (output:gsub('%s+$', ''))
    return nil, (message ~= '' and message or ('the updater exited with ' .. tostring(code or why)))
  end
  return output, nil
end

-- ------------------------------------------------------------ detached jobs
-- Anything that runs the installer can take minutes. The host has one injector
-- thread, so a handler that waits freezes Steam's UI for exactly that long.
-- Each such action instead starts a job in its own session (`setsid`, so a
-- timeout can stop the whole tree), records it in one small state file, and
-- returns at once. Status reads are a file read plus a /proc check; the
-- deadline is enforced on read, and a job past it is killed and reported.
local JOB_LIMIT = 180
local JOBS = {
  repair = { label = 'Repair', seconds = 180, args = 'install --keep-steam-client' },
  pin = { label = 'Steam pin', seconds = 60, args = 'steam pin' },
  launchers = { label = 'Shortcuts', seconds = 60, args = 'launchers' },
  timer = { label = 'Auto-update timer', seconds = 30, args = 'timer on' },
}

local function job_paths(name)
  local base_path = update_dir .. '/' .. tostring(name) .. '-job'
  return base_path .. '.json', base_path .. '.log', base_path .. '-result.json'
end

local function file_tail(path, bytes)
  local handle = io.open(path, 'rb')
  if not handle then return '' end
  local size = handle:seek('end') or 0
  local from = size - (bytes or 2048)
  if from < 0 then from = 0 end
  handle:seek('set', from)
  local raw = handle:read('*a') or ''
  handle:close()
  return raw
end

local function last_line(text)
  local line = ''
  for candidate in tostring(text or ''):gmatch('[^\r\n]+') do
    if candidate:match('%S') then line = candidate end
  end
  return (line:gsub('%s+$', ''))
end

local function process_alive(pid)
  pid = math.floor(tonumber(pid) or 0)
  if pid <= 0 then return false end
  return path_exists('/proc/' .. tostring(pid))
end

-- The job runs in its own session, so its pid is the process-group id: a
-- negative signal reaches the installer and everything it started.
local function stop_group(pid)
  pid = math.floor(tonumber(pid) or 0)
  if pid <= 0 then return false end
  os.execute('kill -TERM -' .. tostring(pid) .. ' 2>/dev/null; sleep 0.3; '
    .. 'kill -KILL -' .. tostring(pid) .. ' 2>/dev/null')
  return true
end

local function job_message(name, result)
  local state = type(result) == 'table' and tostring(result.state or '') or ''
  if state == 'ok' then return 'The ' .. name .. ' job finished.' end
  if state == 'timeout' then
    return 'The ' .. name .. ' job ' .. tostring(result.message
      or 'did not finish in time') .. '.'
  end
  local message = type(result) == 'table' and tostring(result.message or '') or ''
  local code = type(result) == 'table' and tonumber(result.code) or nil
  if state == 'failed' then
    return 'The ' .. name .. ' job failed'
      .. (code and (' (exit ' .. tostring(code) .. ')') or '')
      .. (message ~= '' and (': ' .. message) or '.')
  end
  return ''
end

-- Pure-ish state: one file read, one result read, one /proc check. A job past
-- its deadline is stopped here, because the status poll is the only thing
-- guaranteed to run while it is stuck.
local function job_state(name)
  local spec = JOBS[name] or { seconds = JOB_LIMIT }
  local limit = tonumber(spec.seconds) or JOB_LIMIT
  local state_path, log_path, result_path = job_paths(name)
  local job = read_json(state_path)
  local result = read_json(result_path)
  local state, started, pid, age = 'idle', 0, 0, 0
  if type(job) == 'table' then
    started = tonumber(job.startedAt) or 0
    pid = math.floor(tonumber(job.pid) or 0)
    age = math.max(0, os.time() - started)
    if age > limit then
      if process_alive(pid) then stop_group(pid) end
      state = 'timeout'
      result = { state = 'timeout', at = os.time(),
        message = 'did not finish in ' .. tostring(limit) .. ' s; its process group was stopped'
          .. (last_line(file_tail(log_path)) ~= '' and (' - ' .. last_line(file_tail(log_path))) or '') }
      write_json(result_path, result)
      os.remove(state_path)
    elseif pid == 0 and age > 15 then
      state = 'failed'
      result = { state = 'failed', at = os.time(), message = 'the installer never started' }
      write_json(result_path, result)
      os.remove(state_path)
    elseif pid ~= 0 and not process_alive(pid) then
      if type(result) == 'table' and (tonumber(result.at) or 0) >= started then
        state = tostring(result.state or 'failed')
      else
        state = 'failed'
        result = { state = 'failed', at = os.time(), message = 'the job ended without writing an outcome'
          .. (last_line(file_tail(log_path)) ~= '' and (': ' .. last_line(file_tail(log_path))) or '') }
        write_json(result_path, result)
      end
      os.remove(state_path)
    else
      state = age > (limit - 30) and 'finishing' or 'running'
    end
  elseif type(result) == 'table' then
    -- No job in flight: the last outcome is the state (ok/failed/timeout),
    -- so a timeout stays visible instead of turning into "done".
    state = tostring(result.state or 'done')
  end
  return {
    name = tostring(name),
    id = type(job) == 'table' and tostring(job.id or started) or '',
    pid = pid,
    startedAt = started,
    phase = type(job) == 'table' and tostring(job.phase or '') or '',
    state = state,
    age = age,
    limit = limit,
    log = log_path,
    message = job_message(name, result),
    result = type(result) == 'table' and result or nil,
  }
end

local function start_job(name, args)
  local spec = JOBS[name]
  if not spec then return nil, 'unknown job' end
  if not path_exists(updater_path()) then
    return nil, 'the updater is not installed yet; run install --force once'
  end
  local current = job_state(name)
  if current.state == 'running' or current.state == 'finishing' then
    return current, nil, true -- already running: hand back its state
  end
  fs.create_directories(update_dir)
  local state_path, log_path, result_path = job_paths(name)
  os.remove(result_path)
  os.remove(log_path)
  local id = tostring(os.time()) .. '-' .. tostring(math.random(1000, 9999))
  if not write_json(state_path, { id = id, startedAt = os.time(), phase = 'starting',
      state = 'running', limit = spec.seconds }) then
    return nil, 'could not record the job'
  end
  -- The child writes the state file again with its own pid (that is what a
  -- timeout signals), runs the bounded command, records the outcome, and
  -- removes the marker. Everything is constants plus shq(); no request text.
  local command = tostring(args or spec.args)
  local inner = "printf '{\"id\":\"%s\",\"pid\":%s,\"startedAt\":%s,\"phase\":\"running\","
    .. "\"state\":\"running\",\"limit\":%s}\\n' " .. shq(id) .. " \"$$\" \"$(date +%s)\" "
    .. "'" .. tostring(spec.seconds) .. "' > " .. shq(state_path .. '.tmp')
    .. ' && mv -f ' .. shq(state_path .. '.tmp') .. ' ' .. shq(state_path)
    .. '; ' .. bounded(spec.seconds) .. shq(python_bin()) .. ' ' .. shq(updater_path())
    .. ' ' .. command .. ' >> ' .. shq(log_path) .. ' 2>&1; code=$?; '
    .. "printf '{\"state\":\"%s\",\"code\":%s,\"at\":%s}\\n' "
    .. '"$([ $code -eq 0 ] && echo ok || echo failed)" "$code" "$(date +%s)"'
    .. ' > ' .. shq(result_path .. '.tmp')
    .. '; mv -f ' .. shq(result_path .. '.tmp') .. ' ' .. shq(result_path)
    .. '; rm -f ' .. shq(state_path)
  os.execute('setsid sh -c ' .. shq(inner) .. ' < /dev/null > /dev/null 2>&1 &')
  return { name = tostring(name), id = id, pid = 0, startedAt = os.time(),
    phase = 'starting', state = 'running', age = 0,
    limit = spec.seconds, log = log_path, message = '' }
end

-- The update check talks to GitHub, so it runs detached: the RPC returns at
-- once and the panel follows update-check.json, which the check always writes
-- (errors included). One check at a time; a stuck one is retried after 90s.
local CHECK_GRACE = 90
local check_started_path = update_dir .. '/check-started'

local function check_started_at()
  local handle = io.open(check_started_path, 'rb')
  if not handle then return 0 end
  local raw = handle:read('*a') or ''
  handle:close()
  return tonumber(raw:match('%d+')) or 0
end

local function checking(cached)
  local started = check_started_at()
  return started > 0 and started > (tonumber(cached.checkedAt) or 0)
    and os.time() - started < CHECK_GRACE
end

local function spawn_check(force)
  if not path_exists(updater_path()) then
    return false, 'the updater is not installed yet; run install --force once'
  end
  fs.create_directories(update_dir)
  local handle = io.open(check_started_path, 'wb')
  if not handle then return false, 'could not record the update check' end
  handle:write(tostring(os.time()))
  handle:close()
  local inner = 'exec ' .. bounded(CHECK_GRACE - 10) .. shq(python_bin()) .. ' ' .. shq(updater_path())
    .. ' check' .. (force and ' --force' or '')
  os.execute('setsid sh -c ' .. shq(inner) .. ' < /dev/null > /dev/null 2>&1 &')
  return true
end

local function update_state()
  local snapshot = read_json(base .. 'luna-manifest.json') or {}
  local cached = read_json(update_dir .. '/update-check.json') or {}
  local pending = read_json(update_dir .. '/pending.json') or {}
  local prefs = load_prefs()
  return {
    installed = tostring((snapshot.versions or {}).luna or ''),
    -- What the installed snapshot pins per add-on, and the repo revision it
    -- was installed from, so the Updates page can show installed vs pinned.
    pinned = type(snapshot.versions) == 'table' and snapshot.versions or {},
    source = tostring(snapshot.source or ''),
    installedAt = tonumber(snapshot.installedAt) or 0,
    toolsInstalled = path_exists(update_dir .. '/luna.py'),
    latest = tostring(cached.latest or ''),
    available = cached.available == true,
    checkedAt = tonumber(cached.checkedAt) or 0,
    checkError = tostring(cached.checkError or ''),
    checking = checking(cached),
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

-- ------------------------------------------------------------ data hygiene
--
-- The data directory is shared with Syncthing. When two machines write at once
-- Syncthing leaves `*.sync-conflict-<date>-<time>-<host>.json` copies behind;
-- they are snapshots, never live data, so nothing here reads them (every loader
-- opens one exact path). They are reported in Health and moved - never deleted -
-- into conflicts/<date>/ by the SlsLunaHygiene RPC.
local CONFLICTS_DIR = data_dir .. '/conflicts'
local hygiene_path = data_dir .. '/hltb-hygiene.json'
local CORRUPT_PATTERN = '%.corrupt%-%d'

local function size_and_time(path)
  if lfs then
    local ok, attr = pcall(lfs.attributes, path)
    if ok and type(attr) == 'table' then
      return tonumber(attr.size) or 0, tonumber(attr.modification) or 0
    end
  end
  local ok, size = pcall(fs.size, path)
  if ok and size then return tonumber(size) or 0, 0 end
  return 0, 0
end

local function data_dirs()
  local dirs = { data_dir }
  local ok, items = pcall(fs.list, data_dir)
  if ok and type(items) == 'table' then
    for _, item in ipairs(items) do
      local name = tostring((type(item) == 'table' and item.name) or '')
      if type(item) == 'table' and item.is_directory == true and name ~= ''
        and name:sub(1, 1) ~= '.' and name ~= 'conflicts' then
        dirs[#dirs + 1] = data_dir .. '/' .. name
      end
    end
  end
  return dirs
end

-- The data directory and its immediate subdirectories; a conflict copy can sit
-- next to the playtime ledger too, not only next to hltb-library.json.
local function hygiene_rev()
  if not has_stat then return nil end
  local parts = {}
  for index, dir in ipairs(data_dirs()) do parts[index] = file_stamp(dir) end
  return table.concat(parts, '|')
end

local function data_files(pattern)
  local files = {}
  for _, dir in ipairs(data_dirs()) do
    local ok, items = pcall(fs.list, dir)
    if ok and type(items) == 'table' then
      local relative = (dir == data_dir) and '' or (dir:match('[^/]+$') or '')
      for _, item in ipairs(items) do
        local name = tostring((type(item) == 'table' and item.name) or item or '')
        local directory = type(item) == 'table' and item.is_directory == true
        if name ~= '' and not directory and name:match(pattern) then
          local size, modified = size_and_time(dir .. '/' .. name)
          files[#files + 1] = { name = name, dir = relative, path = dir .. '/' .. name,
            size = size, modified = modified }
        end
      end
    end
  end
  table.sort(files, function(a, b)
    if a.dir == b.dir then return a.name < b.name end
    return a.dir < b.dir
  end)
  return files
end

local function summarize(files)
  local bytes, oldest = 0, nil
  for _, file in ipairs(files) do
    bytes = bytes + (file.size or 0)
    if (file.modified or 0) > 0 and (oldest == nil or file.modified < oldest) then
      oldest = file.modified
    end
  end
  return { count = #files, totalBytes = bytes, oldest = oldest }
end

-- The expensive part is one directory listing plus a stat per conflict file, so
-- the result is kept in a sidecar keyed on the directory stamp; Health and a
-- repeated status call then pay one small read. The sidecar is only rewritten
-- when the summary really changed, so the write cannot churn the directory
-- timestamp Health is memoised on.
local function hygiene_state(force, persist)
  if not force then
    local rev = hygiene_rev()
    local cached = read_json_small(hygiene_path)
    if rev and type(cached) == 'table' and cached.rev == rev
      and type(cached.count) == 'number' then
      return { count = cached.count, totalBytes = cached.totalBytes or 0,
        oldest = cached.oldest, cached = true }
    end
  end
  local files = data_files('sync%-conflict%-')
  local summary = summarize(files)
  summary.files = files
  -- Only an explicit status/clean writes the sidecar. Health must not: its
  -- cache is keyed on the directory stamp this write would move.
  if persist then
    local rev = hygiene_rev()
    local previous = read_json_small(hygiene_path)
    local same = type(previous) == 'table' and previous.rev == rev
      and previous.count == summary.count and previous.totalBytes == summary.totalBytes
      and (previous.oldest or nil) == (summary.oldest or nil)
    if not same then
      write_bytes(hygiene_path, json.encode({ rev = rev, count = summary.count,
        totalBytes = summary.totalBytes, oldest = summary.oldest }))
    end
  end
  return summary
end

local function corrupt_state()
  local files = data_files(CORRUPT_PATTERN)
  local summary = summarize(files)
  summary.files = files
  return summary
end

local function hygiene_clean()
  local files = data_files('sync%-conflict%-')
  if #files == 0 then return { success = true, moved = 0 } end
  local folder = CONFLICTS_DIR .. '/' .. os.date('%Y-%m-%d')
  fs.create_directories(folder)
  local moved, failed = 0, {}
  for _, file in ipairs(files) do
    local target_dir = folder
    if file.dir and file.dir ~= '' then
      target_dir = folder .. '/' .. file.dir
      fs.create_directories(target_dir)
    end
    if os.rename(file.path, target_dir .. '/' .. file.name) then
      moved = moved + 1
    else
      failed[#failed + 1] = (file.dir ~= '' and (file.dir .. '/') or '') .. file.name
    end
  end
  cache_drop(health_cache)
  return { success = #failed == 0, moved = moved, folder = folder, failed = failed }
end

local function steam_client_state()  -- What `luna steam status` reports on the shell, read-only for the menu:
  -- running build, pin, match, update block and local package cache.
  local state = { build = '', pinned = '', hardLock = false, match = false, updatesBlocked = false,
    cachedBuild = '', cacheReady = false }
  local steam_root = home .. '/.local/share/Steam'
  local log_path = steam_root .. '/logs/console_log.txt'
  local handle = io.open(log_path, 'rb')
  if handle then
    local size = handle:seek('end') or 0
    handle:seek('set', math.max(0, size - 65536))
    local tail = handle:read('*a') or ''
    handle:close()
    -- Newer clients log "Client version: 0" at startup; a zero report carries
    -- no information and must never override a real version or a pin.
    for build in tail:gmatch('Client version:%s*(%d+)') do
      if build ~= '0' then state.build = build end
    end
  end
  if state.build == '' then
    local manifest = read_file(steam_root .. '/package/steam_client_ubuntu12.manifest')
    if manifest then
      for version in manifest:gmatch('"version"%s+"(%d+)"') do
        if version ~= '0' then state.build = version end
      end
    end
  end
  local tools = update_dir
  local pin_raw = read_file(tools .. '/steam-pin.json')
  if pin_raw then
    local ok, pin = pcall(json.decode, pin_raw)
    if ok and type(pin) == 'table' then
      state.pinned = tostring(pin.clientBuild or '')
      state.hardLock = type(pin.hardLock) == 'table'
    end
  end
  -- A detached lock/unlock job (see SlsLunaSteam) leaves this marker until it ends.
  state.job = ''
  do
    local job_raw = read_file(tools .. '/steam-job.json')
    if job_raw then
      local ok_job, job = pcall(json.decode, job_raw)
      if ok_job and type(job) == 'table' and os.time() - (tonumber(job.at) or 0) < 7200 then
        state.job = tostring(job.action or '')
      end
    end
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
    local pending_raw = read_file(update_dir .. '/pending-steam.json')
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

-- Which plugin host runs this backend. `running` is what is executing us now;
-- `configured` is what `luna host` last selected (host.json), so a pending
-- restart shows up as running ~= configured.
local function luna_home_dir()
  return on_luna_host and luna_home or (home .. '/.local/share/Luna')
end

local function host_info()
  local state = read_json((on_luna_host and luna_home or (home .. '/.local/share/Luna'))
    .. '/host.json') or {}
  return {
    running = on_luna_host and 'luna' or 'lumen',
    configured = state.active == 'luna' and 'luna' or 'lumen',
    standalone = (on_luna_host and state.active ~= 'lumen'),
    addons = base,
    state = update_dir,
    -- luna-host has no version string of its own: report the binary Luna
    -- installed (present + size) so the Updates page can show it.
    binary = path_exists(luna_home_dir() .. '/bin/luna-host'),
  }
end

local function build_health()
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
  local sgdb = read_json(data_dir .. '/sgdb.json') or {}
  local scorer_marker = read_scorer_marker()
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
    host = host_info(),
    -- Optional third-party host. `present` is false when none of its files
    -- exist, which is the normal state on luna-host; each flag below is then
    -- false and the Health page shows "not present" instead of an error.
    lumen = {
      present = path_exists(lumen_root .. '/lumen') or path_exists(boot_path)
        or path_exists(lumen_root .. '/luatools/backend/main.lua')
        or path_exists(lumen_root .. '/sls-addons') or path_exists(lumen_root .. '/sls-luna'),
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
    -- The last detached repair job (state, deadline, message): one file read,
    -- so the Health page can show its outcome after a reopen without an RPC.
    repair = job_state('repair'),
    data = {
      dir = data_dir,
      prefs = load_prefs(),
      hltb = {
        cached = store_game_count(),
        scorer = SCORER_VERSION,
        -- From the scorer marker: no store decode on the page-load path.
        needsReview = tonumber(scorer_marker and scorer_marker.needsReview) or 0,
        recovery = last_recovery(),
      },
      sgdb = { hasKey = type(sgdb.apiKey) == 'string' and sgdb.apiKey ~= '' },
      -- Every file this backend writes, with the cap it is held to.
      caps = { hltb = FILE_CAPS.store, library = FILE_CAPS.library, prefs = FILE_CAPS.prefs,
        wire = FILE_CAPS.wire, sidecar = FILE_CAPS.sidecar, records = MAX_STORE_RECORDS },
      hygiene = { conflicts = hygiene_state(false, false), corrupt = corrupt_state() },
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
  -- and file sizes, so a problem can be described without a shell. The build
  -- walks every add-on and data directory and probes a dozen paths, so it is
  -- memoised on the stamps of everything it reads; {refresh: true} bypasses it
  -- for a caller that just changed something and cannot wait out the TTL.
  local health_inputs = function()
    local steam = home .. '/.local/share/Steam'
    local paths = {
      lumen_root .. '/lua/boot.lua', lumen_root .. '/lumen', base,
      base .. 'luna-manifest.json', base .. 'bridge.lua',
      data_dir, store_path, library_path, data_dir .. '/sgdb.json',
      steam, steam .. '/steam.sh', steam .. '/steam.cfg', steam .. '/userdata',
      steam .. '/logs', steam .. '/logs/console_log.txt',
      steam .. '/package', steam .. '/package/steam_client_ubuntu12.manifest',
      update_dir, update_dir .. '/update-check.json', update_dir .. '/autoupdate.json',
      update_dir .. '/steam-pin.json', update_dir .. '/steam-job.json',
      update_dir .. '/steam-client-cache', update_dir .. '/steam-client-cache/build.txt',
      home .. '/.config/systemd/user/luna-autoupdate.timer',
    }
    for _, name in ipairs(installed()) do
      paths[#paths + 1] = base .. name .. '/plugin.json'
    end
    return paths
  end
  registry.SlsLunaHealth = function(raw)
    local refresh = type(raw) == 'table' and raw.refresh == true
    local rev = stamps_rev(health_inputs())
    local now = os.time()
    if not refresh and rev and health_cache.text and health_cache.rev == rev
      and now < health_cache.expires then
      return health_cache.text
    end
    local text = reply({ success = true, health = build_health() })
    if rev and not refresh then
      health_cache.text = text
      health_cache.rev = rev
      health_cache.expires = now + CACHE_TTL
    end
    return text
  end

  -- Syncthing conflict copies in the shared data directory: `status` reports
  -- them (count, total size, oldest) and `clean` is the only action that
  -- writes - it moves them into conflicts/<date>/, it never deletes.
  registry.SlsLunaHygiene = function(raw)
    local ok, entry = pcall(json.decode, tostring(raw or ''))
    local action = (ok and type(entry) == 'table') and tostring(entry.action or '') or ''
    if action == '' or action == 'status' then
      local state = hygiene_state(true, true)
      return reply({ success = true, hygiene = {
        dir = data_dir,
        count = state.count, totalBytes = state.totalBytes, oldest = state.oldest,
        files = json.array(state.files or {}),
        corrupt = (function()
          local corrupt = corrupt_state()
          return { count = corrupt.count, totalBytes = corrupt.totalBytes }
        end)(),
        caps = { store = FILE_CAPS.store, library = FILE_CAPS.library, prefs = FILE_CAPS.prefs,
          wire = FILE_CAPS.wire, sidecar = FILE_CAPS.sidecar, records = MAX_STORE_RECORDS },
      } })
    end
    if action == 'clean' then
      local result = hygiene_clean()
      local state = hygiene_state(true, true)
      return reply({ success = result.success, moved = result.moved, folder = result.folder,
        failed = json.array(result.failed or {}),
        hygiene = { count = state.count, totalBytes = state.totalBytes, oldest = state.oldest } })
    end
    return reply({ success = false, error = 'Unknown action; use status or clean.' })
  end

  -- Update plumbing for the Health page. `status` reads local files only;
  -- `check` asks GitHub through the standalone updater (cached for six hours);
  -- `install` queues the update for the user timer, which applies it while
  -- Steam is closed; `timer` toggles that unattended mode.
  registry.SlsLunaUpdate = function(raw)
    local ok, entry = pcall(json.decode, tostring(raw or ''))
    local action = (ok and type(entry) == 'table') and tostring(entry.action or '') or ''
    if action == '' or action == 'status' then
      return reply({ success = true, update = update_state(), job = job_state('timer') })
    end
    if action == 'check' then
      if not checking(read_json(update_dir .. '/update-check.json') or {}) then
        local started, err = spawn_check(entry.force == true)
        if not started then return reply({ success = false, error = err }) end
      end
      return reply({ success = true, update = update_state() })
    end
    if action == 'install' then
      local tag = tostring(entry.tag or '')
      if tag ~= '' and (#tag > 64 or not tag:match('^[0-9A-Za-z][0-9A-Za-z._-]*$')) then
        return reply({ success = false, error = 'That release tag is not valid.' })
      end
      local queued = write_json(update_dir .. '/pending.json',
        { tag = tag, requestedAt = os.time() })
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
      local job, err, already = start_job('timer', 'timer ' .. value)
      if not job then return reply({ success = false, error = err }) end
      -- The units are written in well under a second; the status poll still
      -- follows the same job contract rather than waiting here.
      return reply({ success = true, job = job, alreadyRunning = already == true,
        update = update_state() })
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
      return reply({ success = true, steam = steam_client_state(), job = job_state('pin') })
    end
    if action == 'pin' then
      local job, err, already = start_job('pin')
      if not job then return reply({ success = false, error = err }) end
      return reply({ success = true, job = job, alreadyRunning = already == true,
        steam = steam_client_state() })
    end
    if action == 'lock' or action == 'unlock' then
      -- Lock kills Steam (and this very Lua VM with it), so the job runs fully
      -- detached: setsid -f puts it in its own session, the CLI notifies and
      -- reopens Steam itself, and the job marker is removed when it ends. The
      -- command line is built from constants only; nothing from the request.
      local tools = update_dir
      if not path_exists(updater_path()) then
        return reply({ success = false, error = 'the updater is not installed yet; run install --force once' })
      end
      if steam_client_state().job ~= '' then
        return reply({ success = false, error = 'A Steam lock/unlock job is already running.' })
      end
      local flags = action == 'lock' and 'steam lock --force --relaunch' or 'steam unlock --relaunch'
      local job_path = tools .. '/steam-job.json'
      local log_path = tools .. '/steam-job.log'
      if not write_json(job_path, { action = action, at = os.time() }) then
        return reply({ success = false, error = 'could not record the Steam job' })
      end
      local inner = shq(python_bin()) .. ' ' .. shq(updater_path()) .. ' ' .. flags
        .. '; rm -f ' .. shq(job_path)
      os.execute('setsid -f sh -c ' .. shq(inner) .. ' < /dev/null >> ' .. shq(log_path) .. ' 2>&1')
      return reply({ success = true, started = action, log = log_path, steam = steam_client_state() })
    end
    local pending_path = update_dir .. '/pending-steam.json'
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

  -- Which plugin host runs this backend, for the Health/Settings "Host" line.
  -- Read-only: switching hosts is `luna host cutover|rollback` with Steam closed.
  registry.SlsLunaHost = function()
    return reply({ success = true, host = host_info() })
  end

  -- Third-party data, read-only: LuaTools (a Lumen plugin) keeps its signed-in
  -- lua.tools account in <Lumen>/luatools/backend/data/lua_tools_auth.json. This
  -- reads only the OAuth provider, the Discord display name, an https avatar
  -- URL and the expiry/path -- and never returns, prints or logs a token value
  -- or the rest of the file. Luna does not write, refresh or repair that file.
  local LUA_TOOLS_AUTH_REL = '/luatools/backend/data/lua_tools_auth.json'

  local function luatools_auth_candidates()
    local out = { lumen_root .. LUA_TOOLS_AUTH_REL }
    -- Luna's own host keeps its plugin trees under LUNA_HOME (add-ons under
    -- /addons, host plugins under /plugins); try the same relative layout
    -- there too, so the read is not Lumen-only.
    if luna_home ~= '' then
      out[#out + 1] = luna_home .. LUA_TOOLS_AUTH_REL
      out[#out + 1] = luna_home .. '/plugins' .. LUA_TOOLS_AUTH_REL
    end
    return out
  end

  -- Only an https avatar on Discord's own CDN is used: anything else (http,
  -- another host, quotes, whitespace, control bytes, an absurd length) becomes
  -- an empty string, so the page can put the value straight on an <img>.
  local AVATAR_HOSTS = { ['cdn.discordapp.com'] = true, ['media.discordapp.net'] = true }

  local function luatools_avatar(value)
    if type(value) ~= 'string' or #value == 0 or #value > 300 then return '' end
    for index = 1, #value do
      local byte = value:byte(index)
      if byte <= 32 or byte == 127 or byte == 34 or byte == 39 then return '' end
    end
    local host = value:match('^https://([^/%?#]+)/')
    if not host or not AVATAR_HOSTS[host:lower()] then return '' end
    return value
  end

  -- ok = the question could be answered at all. signedIn = the file parses, the
  -- provider is discord, a display name exists AND a refresh token is present
  -- (its presence and type are tested, its value is never copied): LuaTools
  -- renews the one-hour access token from it by itself. `expired` is kept for
  -- callers but is not a state the UI may label with: a past expires_at only
  -- means nothing has refreshed it yet. `at` is expires_at, 0 when absent.
  local function luatools_state()
    local path, raw = '', nil
    for _, candidate in ipairs(luatools_auth_candidates()) do
      raw = read_file(candidate)
      if raw then path = candidate; break end
    end
    -- No file means LuaTools was never here: "not present", not an error.
    -- Every Luna page, including this footer row, works without it.
    if not raw then
      return { ok = true, present = false, signedIn = false, name = '', avatar = '',
        expired = false, at = 0, path = '' }
    end
    local ok, data = pcall(json.decode, raw)
    if not ok or type(data) ~= 'table' then
      return { ok = false, present = true, signedIn = false, name = '', avatar = '',
        expired = false, at = 0, path = path,
        error = 'the LuaTools session file could not be read' }
    end
    local user = type(data.user) == 'table' and data.user or {}
    local app = type(user.app_metadata) == 'table' and user.app_metadata or {}
    local meta = type(user.user_metadata) == 'table' and user.user_metadata or {}
    local claims = type(meta.custom_claims) == 'table' and meta.custom_claims or {}
    local name = type(claims.global_name) == 'string' and claims.global_name or ''
    if name:match('^%s*$') then
      -- The same profile repeats the display name as full_name/name.
      name = type(meta.full_name) == 'string' and meta.full_name
        or (type(meta.name) == 'string' and meta.name or '')
    end
    name = tostring(name or ''):gsub('^%s+', ''):gsub('%s+$', '')
    local has_refresh = type(data.refresh_token) == 'string' and data.refresh_token ~= ''
    local avatar = luatools_avatar(meta.avatar_url)
    if avatar == '' then avatar = luatools_avatar(meta.picture) end
    local expires_at = tonumber(data.expires_at) or 0
    return {
      ok = true,
      present = true,
      signedIn = tostring(app.provider or '') == 'discord' and name ~= '' and has_refresh,
      name = name,
      avatar = avatar,
      expired = expires_at > 0 and expires_at < os.time(),
      at = expires_at,
      path = path,
    }
  end

  -- Parameterless: the footer asks when the menu opens and on Refresh (never
  -- on a timer).
  registry.SlsLunaLuaTools = function()
    return reply({ success = true, luatools = luatools_state() })
  end

  -- Lumen base update for the Updates page (Lumen host only). Steam is running
  -- while the menu is open, so `update` only QUEUES a job (pending-lumen.json);
  -- the `luna autoupdate` timer applies it once Steam is closed. Nothing here
  -- runs the installer or touches a process.
  -- The network check (`lumen status --check --json`) runs detached and
  -- writes lumen-check.json when done; one at a time, a stuck one is
  -- abandoned after 90 s. The page asks for it on a click, never on a poll.
  local lumen_check_path = update_dir .. '/lumen-check.json'
  local lumen_check_started = update_dir .. '/lumen-check-started'
  -- Last `luna lumen channels` read this session, so the page can show it
  -- without running the CLI on every status poll. Filled on demand only.
  local channels_cache = nil

  local function checking_lumen()
    local handle = io.open(lumen_check_started, 'rb')
    if not handle then return false end
    local started = tonumber((handle:read('*a') or ''):match('%d+')) or 0
    handle:close()
    return started > 0 and os.time() - started < CHECK_GRACE
  end

  local function spawn_lumen_check()
    if not path_exists(updater_path()) then
      return false, 'the updater is not installed yet; run install --force once'
    end
    fs.create_directories(update_dir)
    local marker = io.open(lumen_check_started, 'wb')
    if not marker then return false, 'could not record the check' end
    marker:write(tostring(os.time()))
    marker:close()
    local temp = lumen_check_path .. '.tmp'
    local inner = 'now=$(date +%s); if out=$(' .. bounded(CHECK_GRACE - 10) .. shq(python_bin()) .. ' '
      .. shq(updater_path()) .. ' lumen status --check --json 2>/dev/null); then '
      .. "printf '{\"checkedAt\":%s,\"status\":%s}' \"$now\" \"$out\" > " .. shq(temp) .. '; else '
      .. "printf '{\"checkedAt\":%s,\"failed\":true}' \"$now\" > " .. shq(temp) .. '; fi; '
      .. 'mv -f ' .. shq(temp) .. ' ' .. shq(lumen_check_path) .. '; rm -f ' .. shq(lumen_check_started)
    os.execute('setsid sh -c ' .. shq(inner) .. ' < /dev/null > /dev/null 2>&1 &')
    return true
  end

  local function lumen_state()
    local versions = {}
    local data = read_json(lumen_root .. '/versions.json')
    if type(data) == 'table' then
      for name, item in pairs(data) do
        if type(item) == 'table' then versions[name] = tostring(item.tag or '') end
      end
    end
    local blocked, bridge = false, false
    local about = read_file(lumen_root .. '/lua/about.lua')
    if about and about:find('LUNA-LUMEN-UPDATE-BLOCK: begin', 1, true) then blocked = true end
    local boot = read_file(lumen_root .. '/lua/boot.lua')
    if boot and boot:find('SLS-ADDONS-INTEGRATION', 1, true) then bridge = true end
    -- The installed copy needs its support tree beside luna.py; without it an
    -- applied update would lose Luna (it could not re-patch or reinstall).
    local missing = {}
    local needed = {
      'scripts/patch-lumen.py', 'scripts/steam-machine.py', 'bridge/bridge.lua',
      'vendor/MANIFEST.json', 'addons/luna/plugin/plugin.json',
      'addons/sgdb/plugin/plugin.json', 'addons/graphics/plugin/plugin.json',
      'addons/playtime/plugin/plugin.json', 'addons/presence/plugin/plugin.json',
      'addons/compressor/plugin/plugin.json',
    }
    for _, rel in ipairs(needed) do
      if not path_exists(update_dir .. '/runtime/' .. rel) then missing[#missing + 1] = rel end
    end
    local tooling_ok = #missing == 0
    local host = host_info()
    local result = read_json(update_dir .. '/lumen-update.json')
    if type(result) ~= 'table' then result = {} end
    local pending_job = read_json(update_dir .. '/pending-lumen.json')
    local job = nil
    if type(pending_job) == 'table' then
      job = { action = tostring(pending_job.action or 'update'),
        attempts = tonumber(pending_job.attempts) or 0,
        lastError = tostring(pending_job.lastError or '') }
    end
    -- A running record whose process is gone was interrupted; the next
    -- `luna lumen update` or the timer recovers it.
    local interrupted = false
    if result.state == 'running' then
      local pid = tonumber(result.pid)
      interrupted = (pid ~= nil and pid > 0) and not path_exists('/proc/' .. tostring(math.floor(pid))) or false
    end
    local backups = {}
    local backup_root = update_dir .. '/lumen-backups'
    local listed, names = pcall(fs.list, backup_root)
    if listed and type(names) == 'table' then
      for _, item in ipairs(names) do
        local name = type(item) == 'table' and tostring(item.name or '') or tostring(item)
        if name:match('^[0-9A-Za-z._-]+$') then
          local meta = read_json(backup_root .. '/' .. name .. '/backup.json')
          if type(meta) == 'table' then
            backups[#backups + 1] = { name = name, at = tonumber(meta.at) or 0,
              bytes = tonumber(meta.bytes) or 0, failed = meta.failed == true }
          end
        end
      end
      table.sort(backups, function(a, b) return a.name > b.name end)
      while #backups > 8 do backups[#backups] = nil end
    end
    local options = read_json(update_dir .. '/lumen-options.json')
    local choice = (type(options) == 'table' and (options.millennium == 'keep' or options.millennium == 'remove'))
      and options.millennium or ''
    -- The same places the CLI's plan lists as Millennium leftovers.
    local present = {}
    for _, path in ipairs({ home .. '/.config/millennium', home .. '/.local/share/millennium',
        home .. '/.local/share/Steam/millennium', '/usr/lib/millennium', '/usr/share/millennium' }) do
      if path_exists(path) then present[#present + 1] = path end
    end
    local check = read_json(lumen_check_path)
    if type(check) ~= 'table' then check = {} end
    check.checking = checking_lumen()
    return {
      host = host.configured,
      running = host.running,
      applicable = host.configured == 'lumen' and host.running == 'lumen',
      versions = versions,
      blockInPlace = blocked,
      bridge = bridge,
      tooling = { ok = tooling_ok, missing = missing },
      toolingIncomplete = tooling_ok and '' or ('tooling incomplete: missing ' .. table.concat(missing, ', ')),
      applyService = path_exists(home .. '/.config/systemd/user/luna-apply.service'),
      pending = path_exists(update_dir .. '/pending-lumen.json'),
      job = job,
      result = result,
      interrupted = interrupted,
      backups = json.array(backups),
      millennium = { choice = choice, present = json.array(present) },
      check = check,
      -- Which channel each component follows, as the CLI last reported it.
      channels = channels_cache,
      -- When this backend loaded: the plugin host starts with Steam, so it is
      -- the start of this Steam session. Older final results are history.
      sessionStartedAt = SESSION_STARTED_AT,
    }
  end

  -- Every Lumen change goes through the CLI (it takes the writer lock and
  -- refuses while a job runs). This file never writes the job files and never
  -- starts or stops a unit. The two synchronous calls below (queue, cancel) are
  -- short and bounded; the network check runs detached.
  local MILLENNIUM_CHOICES = { keep = '--keep-millennium', remove = '--remove-millennium' }

  local function clean(text)
    return ((tostring(text or ''):gsub('%s+$', '')))
  end

  -- Which channel each Lumen component follows. The CLI owns
  -- <LUMEN_DIR>/update-channels.json and writes it atomically; this backend
  -- only reads it through `luna lumen channels` and changes it through
  -- `luna lumen channels set`, so it can never race the CLI's rename and never
  -- has to guess what "no file yet" means (stable, per the CLI).
  local CHANNEL_NAMES = { lumen = true, plugin = true, slsteam_moon = true }
  local CHANNEL_VALUES = { stable = true, beta = true }

  local function parse_channels(output)
    local values, file = {}, ''
    for line in tostring(output or ''):gmatch('[^\r\n]+') do
      local name, value = line:match('^([%w_]+)%s+([%a_]+)%s*$')
      if name and CHANNEL_NAMES[name] and CHANNEL_VALUES[value] then values[name] = value end
      local listed = line:match('^file%s*:%s*(.+)$')
      if listed then file = (listed:gsub('%s+$', '')) end
    end
    return values, file
  end

  -- The CLI read spawns Python, which is the slowest thing the Lumen page can
  -- trigger. The file it prints only changes through this backend or a manual
  -- `luna lumen channels set`, so a page load inside this window reuses the
  -- last answer; `channels-set` passes force to read it back fresh.
  local CHANNELS_TTL = 300
  local function read_channels(force)
    local now = os.time()
    if not force and channels_cache and channels_cache.at
      and now - channels_cache.at < CHANNELS_TTL then
      return channels_cache
    end
    local out, err = run_updater('lumen channels', 20)
    if not out then return nil, clean(err) end
    local values, file = parse_channels(out)
    if not next(values) then return nil, clean(out) end
    channels_cache = { values = values, file = file, at = os.time(), output = clean(out) }
    return channels_cache
  end

  -- The `--terminal` update runs as a detached job: it asks Steam itself to
  -- quit (up to 60 s), then opens a window and tees to its own log. It outlives
  -- this Lua VM, so its raw output and exit code go to two small files and the
  -- page follows them instead of blocking the injector thread for a minute.
  local TERMINAL_STARTED = update_dir .. '/terminal-update-started'
  local TERMINAL_OUT = update_dir .. '/terminal-update.out'
  local TERMINAL_CODE = update_dir .. '/terminal-update.code'
  local TERMINAL_LIMIT = 240

  local function terminal_state()
    local started = tonumber((read_file(TERMINAL_STARTED) or ''):match('%d+')) or 0
    local code = (read_file(TERMINAL_CODE) or ''):match('%d+')
    local out = (file_tail(TERMINAL_OUT, 4000):gsub('%s+$', ''))
    local log = (out:match('[Ll]og%s*:%s*([^\r\n]+)') or '')
    local window = (out:match('terminal%s*:%s*([^\r\n]+)') or '')
    local state = 'idle'
    if code ~= nil then
      state = (tonumber(code) == 0) and 'opened' or 'refused'
    elseif started > 0 and os.time() - started < TERMINAL_LIMIT then
      state = 'starting'
    end
    return { state = state, code = tonumber(code) or -1, at = started,
      log = (log:gsub('%s+$', '')), window = (window:gsub('%s+$', '')), output = out }
  end

  local function lumen_view()
    local state = lumen_state()
    state.terminal = terminal_state()
    return state
  end

  registry.SlsLunaLumen = function(raw)
    local ok, entry = pcall(json.decode, tostring(raw or ''))
    local action = (ok and type(entry) == 'table') and tostring(entry.action or '') or ''
    if action == '' or action == 'status' then
      return reply({ success = true, lumen = lumen_view() })
    end
    if action == 'check' then
      if not checking_lumen() then
        local started, err = spawn_lumen_check()
        if not started then return reply({ success = false, error = err }) end
      end
      return reply({ success = true, lumen = lumen_view() })
    end
    if action == 'channels' then
      local channels, err = read_channels()
      if not channels then return reply({ success = false, error = err, lumen = lumen_view() }) end
      return reply({ success = true, channels = channels, lumen = lumen_view() })
    end
    if action == 'channels-set' then
      -- The whitelists make this a fixed command line: no request text ever
      -- reaches the shell. Refusals (exit 2) carry the CLI's own REFUSING
      -- text, which the page shows unchanged.
      local component = tostring(entry.component or '')
      local channel = tostring(entry.channel or '')
      if not CHANNEL_NAMES[component] then
        return reply({ success = false, error = 'Unknown channel component.' })
      end
      if not CHANNEL_VALUES[channel] then
        return reply({ success = false, error = 'Choose stable or beta.' })
      end
      if entry.confirmed ~= true then
        return reply({ success = false, error = 'The channel change needs a confirmation first.' })
      end
      local out, err = run_updater('lumen channels set ' .. channel .. ' --only ' .. component, 20)
      if not out then return reply({ success = false, error = clean(err), lumen = lumen_view() }) end
      local channels = read_channels(true)
      return reply({ success = true, message = clean(out), channels = channels, lumen = lumen_view() })
    end
    if action == 'terminal' then
      -- `update-all --terminal` asks Steam itself to quit, waits for it, then
      -- opens a terminal that runs the same update live and tees it to a log.
      -- This backend never runs steam -shutdown, pkill or any lock command.
      if entry.confirmed ~= true then
        return reply({ success = false, error = 'The terminal update needs a confirmation first.' })
      end
      if not path_exists(updater_path()) then
        return reply({ success = false, error = 'the updater is not installed yet; run install --force once' })
      end
      if terminal_state().state == 'starting' then
        return reply({ success = false, error = 'A terminal update is already starting.' })
      end
      fs.create_directories(update_dir)
      os.remove(TERMINAL_OUT)
      os.remove(TERMINAL_CODE)
      local marker = io.open(TERMINAL_STARTED, 'wb')
      if not marker then return reply({ success = false, error = 'could not record the terminal update' }) end
      marker:write(tostring(os.time()))
      marker:close()
      local inner = shq(python_bin()) .. ' ' .. shq(updater_path()) .. ' update-all --terminal --yes > '
        .. shq(TERMINAL_OUT) .. ' 2>&1; echo $? > ' .. shq(TERMINAL_CODE)
      os.execute('setsid sh -c ' .. shq(inner) .. ' < /dev/null > /dev/null 2>&1 &')
      return reply({ success = true, started = true, lumen = lumen_view() })
    end
    if action == 'update' then
      local state = lumen_state()
      if not state.applicable then
        return reply({ success = false, error = 'Lumen is not the active host; nothing to update.' })
      end
      if not state.tooling.ok then
        return reply({ success = false, error = state.toolingIncomplete
          .. '. Re-run: luna install --force' })
      end
      -- The page asks for a confirmation first; a call without it is refused
      -- here too, so nothing can be queued by a stray request.
      if entry.confirmed ~= true then
        return reply({ success = false, error = 'The update needs a confirmation first.' })
      end
      local flag = ''
      if entry.millennium ~= nil and entry.millennium ~= '' then
        flag = MILLENNIUM_CHOICES[tostring(entry.millennium)]
        if not flag then return reply({ success = false, error = 'Choose keep or remove for Millennium.' }) end
        flag = ' ' .. flag
      end
      local out, err = run_updater('lumen update --queue' .. flag, 20)
      if not out then return reply({ success = false, error = clean(err) }) end
      return reply({ success = true, queued = true, message = clean(out), lumen = lumen_state() })
    end
    if action == 'cancel' then
      local out, err = run_updater('lumen cancel', 20)
      if not out then return reply({ success = false, error = clean(err), lumen = lumen_state() }) end
      return reply({ success = true, message = clean(out), lumen = lumen_state() })
    end
    if action == 'restore' or action == 'reinstall' then
      -- Both need Steam closed and cannot be queued, and this menu only exists
      -- while Steam is open: the page shows the terminal command instead.
      return reply({ success = false, error = 'Close Steam and run this from a terminal.' })
    end
    return reply({ success = false, error = 'Unknown Lumen action.' })
  end

  -- The Steam window gradient follows the desktop. Noctalia regenerates its
  -- Steam colour file whenever the wallpaper or palette changes; this only
  -- reads the few colours the gradient needs, so the panel can repaint live.
  registry.SlsLunaGradient = function()
    local prefs = load_prefs()
    local path = home .. '/.steam/steam/steamui/skins/Material-Theme/css/main/colors/matugen.css'
    local css = read_file(path)
    local wanted = { primary = true, secondary = true, tertiary = true, surface = true,
      ['surface-container'] = true, ['primary-container'] = true }
    local colors = {}
    if css then
      for name, r, g, b in css:gmatch('%-%-md%-sys%-color%-([%w%-]+)%s*:%s*rgba?%(%s*(%d+)%s*,%s*(%d+)%s*,%s*(%d+)') do
        if wanted[name] then
          colors[name] = { tonumber(r), tonumber(g), tonumber(b) }
        end
      end
    end
    return reply({ success = true, enabled = prefs.steam_gradient ~= false,
      available = colors.primary ~= nil, colors = colors, source = path })
  end

  -- One-click bridge repair for the Health page. A base (Lumen/Moon) update
  -- replaces boot.lua and wipes the bridge while the payloads stay current;
  -- this reruns the installer keeping the Steam client untouched. It refuses
  -- while Steam runs (same guard as the shell), so the panel surfaces the
  -- refusal instead of changing anything.
  -- The installer touches hundreds of files, so it runs as a detached job:
  -- this call only starts it (or reports the one already running) and the
  -- panel follows repair-job.json through action='status'. Steam's UI is
  -- never held for the length of an install.
  registry.SlsLunaRepair = function(raw)
    local ok, entry = pcall(json.decode, tostring(raw or ''))
    local action = (ok and type(entry) == 'table') and tostring(entry.action or '') or ''
    if action == 'status' then
      return reply({ success = true, job = job_state('repair') })
    end
    if action == '' or action == 'start' then
      local job, err, already = start_job('repair')
      if not job then return reply({ success = false, error = err }) end
      return reply({ success = true, job = job, alreadyRunning = already == true,
        message = job.state == 'running' and 'Repair started.' or 'A repair is already running.' })
    end
    return reply({ success = false, error = 'Unknown repair action.' })
  end

  registry.SlsLunaRefresh = function(raw)
    local ok, entry = pcall(json.decode, tostring(raw or ''))
    local action = (ok and type(entry) == 'table') and tostring(entry.action or '') or ''
    local pending_path = update_dir .. '/pending-refresh.json'
    if action == '' or action == 'status' then
      return reply({ success = true, refreshPending = path_exists(pending_path) })
    end
    if action == 'queue' then
      if path_exists(pending_path) then
        return reply({ success = false, error = 'A refresh is already queued.' })
      end
      if not write_json(pending_path, { at = os.time() }) then
        return reply({ success = false, error = 'could not queue the refresh' })
      end
      return reply({ success = true, queued = true, refreshPending = true })
    end
    if action == 'cancel' then
      os.remove(pending_path)
      return reply({ success = true, refreshPending = false })
    end
    return reply({ success = false, error = 'Unknown refresh action.' })
  end

  -- SteamOS Mode shortcuts for the Settings page. Creating them only writes
  -- .desktop entries (one per detected screen) plus the managed helper, so it
  -- is safe while Steam runs; refreshing the menu picks them up.
  registry.SlsLunaLaunchers = function(raw)
    local ok, entry = pcall(json.decode, tostring(raw or ''))
    local action = (ok and type(entry) == 'table') and tostring(entry.action or '') or ''
    local tools = update_dir
    if action == '' or action == 'status' then
      return reply({ success = true, helper = path_exists(tools .. '/steam-machine.py'),
        job = job_state('launchers') })
    end
    if action == 'create' then
      local job, err, already = start_job('launchers')
      if not job then return reply({ success = false, error = err }) end
      return reply({ success = true, job = job, alreadyRunning = already == true })
    end
    return reply({ success = false, error = 'Unknown launchers action.' })
  end

  -- An error inside any of the Luna handlers becomes a structured reply, so
  -- the page shows a reason instead of getting nothing back.
  for _, name in ipairs({ 'SlsLunaInfo', 'SlsLunaPrefsSet', 'SlsLunaHealth', 'SlsLunaHygiene', 'SlsLunaUpdate',
      'SlsLunaSteam', 'SlsLunaHost', 'SlsLunaLumen', 'SlsLunaGradient', 'SlsLunaRepair',
      'SlsLunaRefresh', 'SlsLunaLaunchers' }) do
    local handler = registry[name]
    if type(handler) == 'function' then
      registry[name] = function(...)
        local done, out = pcall(handler, ...)
        if done and out ~= nil then return out end
        return reply({ success = false, error = done and 'The backend returned nothing.'
          or ('Backend error: ' .. tostring(out):gsub('^.-:%d+: ', '')) })
      end
    end
  end

  -- Whole local cache: appid -> record, plus the game list the agent reported.
  -- Small by design (a library at most). The encoded reply is memoised on the
  -- stamps of both files: a poll with the `rev` it got last time answers
  -- `{unchanged = true}` in a few bytes instead of the whole megabyte, and a
  -- poll without one (the current panel, a fresh window) gets the full shape.
  registry.SlsHltbGet = function(raw)
    local asked = nil
    if type(raw) == 'string' then asked = raw
    elseif type(raw) == 'table' and type(raw.rev) == 'string' then asked = raw.rev end
    -- After a scorer change (or a store written by another tool) every stored
    -- record is checked once against the library's Steam names: it costs one
    -- decode and is then recorded in the scorer marker, so the cached path
    -- below never pays for it again.
    if validation_pending() then
      local store = load_store()
      local library = load_library()
      local checked, flagged, changed = M.revalidate_store(store, library)
      if changed then persist_store(store) end
      write_scorer_marker(store_stamp(), SCORER_VERSION, flagged)
    end
    local rev = stamps_rev({ store_path, library_path })
    local now = os.time()
    local cached = rev ~= nil and hltb_cache.text and hltb_cache.rev == rev
      and now < hltb_cache.expires
    if not cached then
      -- A previous VM (or a previous page load) may already have encoded this
      -- exact revision: reuse its wire text instead of decode + encode.
      local text = read_wire_text(rev)
      if not text then
        local store = load_store()
        local library = load_library()
        local count = count_keys(store.games)
        write_sidecar_count(rev, count)
        text = reply({ success = true, rev = rev, games = wire_games(store.games), count = count,
          needsReview = review_count(store), scorer = SCORER_VERSION,
          recovered = last_recovery() ~= nil and last_recovery().recovered == true or nil,
          recovery = last_recovery(),
          library = library.games, libraryAt = library.updatedAt,
          minFetchInterval = M.min_fetch_interval })
        if rev then write_wire_text(rev, text) end
      end
      if rev then
        hltb_cache.text = text
        hltb_cache.rev = rev
        hltb_cache.expires = now + CACHE_TTL
      else
        return text
      end
    end
    if asked and asked ~= '' and mtime_known and asked == rev then
      return reply({ success = true, unchanged = true, rev = rev })
    end
    return hltb_cache.text
  end

  -- Re-run the title check over the whole store on demand. Idempotent: a store
  -- already reviewed by this scorer is answered with applied = false and the
  -- same flagged count, and nothing is written.
  registry.SlsHltbRevalidate = function()
    local store = load_store()
    local library = load_library()
    local checked, flagged, changed = M.revalidate_store(store, library)
    local saved = true
    local err = nil
    if changed then
      saved, err = persist_store(store)
    else
      write_scorer_marker(store_stamp(), SCORER_VERSION, flagged)
    end
    if not saved then
      return reply({ success = false, error = tostring(err) })
    end
    return reply({ success = true, checked = checked, flagged = flagged, applied = changed,
      scorer = SCORER_VERSION })
  end

  -- The Steam agent's inventory of owned games and non-Steam shortcuts, so the
  -- panel can show what has no times yet. Purely local; never sent anywhere.
  -- Soundtracks, demos, playtests, dedicated servers and SDKs ship as their own
  -- appids; they are dropped from the inventory (and reported as `skipped`)
  -- because they have no times of their own to find.
  local SKIPPABLE_TAIL = {
    soundtrack = true, ost = true, demo = true, playtest = true, artbook = true, sdk = true,
    server = true, benchmark = true, tool = true, tools = true, pass = true,
    expansion = true, expansions = true, dlc = true,
  }
  local function library_skippable(name)
    local normalized = M.normalize(name)
    if normalized == '' then return true end
    local last = normalized:match('(%S+)$')
    if last and SKIPPABLE_TAIL[last] then return true end
    if normalized:sub(-# 'art book') == 'art book' then return true end
    return false
  end

  registry.SlsHltbLibrary = function(raw)
    local ok, entry = pcall(json.decode, tostring(raw or ''))
    if not ok or type(entry) ~= 'table' or type(entry.games) ~= 'table' then
      return reply({ success = false, error = 'Send {games: [{appid, name, shortcut?}]}.' })
    end
    local games, seen = {}, {}
    local skipped = 0
    for _, game in ipairs(entry.games) do
      if type(game) == 'table' and valid_appid(game.appid) then
        local key = tostring(math.floor(tonumber(game.appid)))
        local name = tostring(game.name or ''):sub(1, 200)
        if name ~= '' and not seen[key] then
          seen[key] = true
          if library_skippable(name) then
            skipped = skipped + 1
          else
            games[#games + 1] = { appid = tonumber(key), name = name, shortcut = game.shortcut == true }
          end
        end
      end
      if #games >= 5000 then break end
    end
    if #games == 0 and #entry.games > 0 then
      return reply({ success = false, error = 'No valid games in the inventory.' })
    end
    local saved, serr = persist_library({ version = 1, updatedAt = os.time(), games = games })
    if not saved then return reply({ success = false, error = tostring(serr) }) end
    return reply({ success = true, count = #games, skipped = skipped })
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
    local key = tostring(math.floor(tonumber(appid)))
    local now = os.time()
    local job = fetch_jobs[key]
    -- A job nobody polled for a minute is abandoned (menu closed mid-fetch).
    if job and now - job.startedAt > M.fetch_job_ttl then
      fetch_jobs[key], job = nil, nil
    end
    local rows, why, code
    if not job then
      if now - last_fetch_at < M.min_fetch_interval then
        return reply({ success = false, code = 'throttled',
          error = 'Wait a moment before the next HowLongToBeat request.' })
      end
      last_fetch_at = now
      if not async_http() then
        rows, why, code = fetch_rows(name)
      else
        job, why, code = M.new_fetch_job(name)
        if not job then return reply({ success = false, code = code or 'unavailable', error = why }) end
        fetch_jobs[key] = job
      end
    end
    if job then
      local state
      state, rows, why, code = M.advance_fetch_job(job)
      if state == 'pending' then
        return reply({ success = false, pending = true, code = 'pending',
          error = 'HowLongToBeat is still answering.' })
      end
      fetch_jobs[key] = nil
      name = job.name
    end
    local store = load_store()
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
