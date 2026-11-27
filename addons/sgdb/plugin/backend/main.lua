-- SLS Luna SteamGridDB backend.
--
-- Manages the user's SteamGridDB API key and applies artwork chosen from
-- SteamGridDB to a game's Steam custom-artwork slots. Everything is local:
-- the key never leaves this machine except as the Authorization header of the
-- user's own SteamGridDB requests, and artwork bytes go straight to Steam's
-- grid folder with a backup of whatever was there before.
--
-- Steam expects custom artwork in userdata/<accountid>/config/grid/ named
-- <appid>p.<ext> (portrait grid), <appid>_hero.<ext>, <appid>_logo.<ext> and
-- <appid>_icon.<ext>. Writes are atomic and always backed up.
local fs = require('fs')
local json = require('json')
local M = {}
local VERSION = '0.2.3'
local home = os.getenv('HOME') or ''
local data_dir = home .. '/.local/share/slssteam-integrations'
local config_path = data_dir .. '/sgdb.json'

local API = 'https://www.steamgriddb.com/api/v2'
local USER_AGENT = 'SLS-Luna/0.1 (Steam custom artwork manager)'
local MAX_IMAGE_BYTES = 8 * 1024 * 1024
-- Live apply pushes the bytes through Steam's own API; keep that path for
-- reasonable sizes so the CDP binding is not asked to carry huge strings.
local MAX_LIVE_BYTES = 4 * 1024 * 1024
local MAX_ASSETS = 60
-- Lumen runs every RPC on one injector thread: a request must never be given
-- more than this to answer, and the Auto pass must not wait at all.
local HTTP_TIMEOUT = 8
-- Largest response body accepted from SteamGridDB (API JSON or image bytes).
local MAX_BODY_BYTES = MAX_IMAGE_BYTES
-- An Auto pass is abandoned after this many seconds; unfinished slots are
-- reported as failures instead of leaving the page waiting.
local AUTO_JOB_TTL = 180
local AUTO_KINDS = { 'grid', 'wide', 'hero', 'logo', 'icon' }

-- kind -> Steam file suffix (the part between the appid and the extension)
local KINDS = {
  grid = 'p',
  hero = '_hero',
  logo = '_logo',
  icon = '_icon',
  wide = '',
}

-- Steam's SetCustomArtworkForApp asset types, mapped empirically against a
-- live client (a 1x1 image per type shows which file Steam writes):
-- 0 -> <appid>p (grid), 1 -> <appid>_hero, 2 -> <appid>_logo, 3 -> <appid>
-- (wide/landscape). Icons have no live type; they are file-only.
local LIVE_TYPES = { grid = 0, hero = 1, logo = 2, wide = 3 }

-- Allowed image types for Steam's grid files. Anything else (webp, gif) would
-- need conversion, which a Lua add-on cannot do faithfully.
local ALLOWED_MIME = { ['image/png'] = 'png', ['image/jpeg'] = 'jpg' }

local function reply(data) return json.encode(data) end

-- A throw inside a handler becomes a structured reply (the same shape Luna's
-- handlers use), so the page shows a reason instead of waiting out the RPC
-- timeout and reporting nothing.
local function err_text(err)
  return (tostring(err):gsub('^.-:%d+: ', ''))
end

local function protected(handler)
  return function(...)
    local done, out = pcall(handler, ...)
    if done and out ~= nil then return out end
    return reply({ success = false, error = done and 'The backend returned nothing.'
      or ('Backend error: ' .. err_text(out)) })
  end
end

local function read_file(path)
  local f = io.open(path, 'rb')
  if not f then return nil end
  local raw = f:read('*a')
  f:close()
  return raw
end

local function write_bytes(path, body)
  local tmp = path .. '.tmp'
  local f, why = io.open(tmp, 'wb')
  if not f then return false, why or 'cannot write file' end
  local written, werr = f:write(body)
  local closed, cerr = f:close()
  if not written or not closed then
    os.remove(tmp)
    return false, werr or cerr or 'write failed'
  end
  local ok, rerr = os.rename(tmp, path)
  if not ok then os.remove(tmp); return false, rerr or 'rename failed' end
  return true
end

local function write_file_atomic(path, body)
  fs.create_directories(fs.parent_path(path))
  -- Keep the last good bytes beside the file: a torn write, or a hand-edited
  -- JSON file, is then recoverable instead of losing the slot, key or history.
  local previous = read_file(path)
  if previous and previous ~= '' then
    local good = true
    if path:match('%.json$') then
      local ok, parsed = pcall(json.decode, previous)
      good = ok and type(parsed) == 'table'
    end
    if good then write_bytes(path .. '.bak', previous) end
  end
  return write_bytes(path, body)
end

-- Read a JSON file, falling back to its .bak when the live copy is torn.
-- Returns the table and whether it came from the backup.
local function read_json(path)
  local raw = read_file(path)
  if raw then
    local ok, parsed = pcall(json.decode, raw)
    if ok and type(parsed) == 'table' then return parsed, false end
  end
  local backup = read_file(path .. '.bak')
  if backup then
    local ok, restored = pcall(json.decode, backup)
    if ok and type(restored) == 'table' then return restored, true end
  end
  return nil, false
end

-- ------------------------------------------------------------------ config

local function load_config()
  local parsed, recovered = read_json(config_path)
  if not parsed then return { version = 1, apiKey = '' }, false end
  parsed.version = 1
  parsed.apiKey = type(parsed.apiKey) == 'string' and parsed.apiKey or ''
  return parsed, recovered
end

local function save_config(data)
  local ok = write_file_atomic(config_path, json.encode(data))
  if ok then
    -- The SteamGridDB key is a user secret: owner-only, even though the
    -- directory itself stays traversable.
    os.execute('/bin/chmod 600 ' .. "'" .. tostring(config_path):gsub("'", "'\\''") .. "' 2>/dev/null")
  end
  return ok
end

-- ------------------------------------------------------------------ steam

-- Only a bootstrapped Steam root is accepted; callers create directories
-- beneath it, and a phantom ~/.steam/steam blocks Valve's own bootstrap.
function M.steam_root(home_dir)
  for _, candidate in ipairs({
    home_dir .. '/.steam/steam',
    home_dir .. '/.local/share/Steam',
    home_dir .. '/.steam/debian-installation',
  }) do
    if fs.exists(candidate .. '/steam.sh') then return candidate end
  end
  return nil
end

-- Same account identity rule as the Playtime add-on: loginusers.vdf is the
-- only local account list, and ambiguity is reported rather than guessed.
local function read_login_users(root)
  local raw = read_file(root .. '/config/loginusers.vdf')
  if not raw then return {} end
  local users, current = {}, nil
  for line in raw:gmatch('[^\r\n]+') do
    local id = line:match('^%s*"(%d+)"%s*$')
    if id and #id >= 16 and #id <= 20 then
      current = { id = id, recent = false }
      users[#users + 1] = current
    elseif current and line:match('"MostRecent"%s*"1"') then
      current.recent = true
    end
  end
  return users
end

function M.resolve_account(root)
  local override = os.getenv('SLS_SGDB_ACCOUNT')
  if override and override:match('^%d+$') and #override >= 16 and #override <= 20 then
    return override, 'SLS_SGDB_ACCOUNT'
  end
  local users = read_login_users(root)
  if #users == 0 then return nil, 'no Steam accounts recorded on this machine' end
  local recent = nil
  for _, user in ipairs(users) do
    if user.recent then
      if recent then return nil, 'several accounts are marked MostRecent' end
      recent = user.id
    end
  end
  if recent then return recent, 'loginusers.vdf MostRecent' end
  if #users == 1 then return users[1].id, 'the only recorded Steam account' end
  return nil, ('ambiguous: %d Steam accounts recorded and none marked MostRecent'):format(#users)
end

-- SteamID64 -> the 32-bit account id Steam uses for userdata directories.
function M.account_id(steamid64)
  local value = tonumber(steamid64)
  if not value or value < 76561197960265728 then return nil end
  return value - 76561197960265728
end

function M.kind_suffix(kind)
  return KINDS[kind]
end

-- Resolving the grid directory walks Steam's account records. The panel asks
-- for it on every refresh, so the answer is cached against the loginusers.vdf
-- content: a different account list (or a new login) resolves again at once.
local grid_cache = {}
local function grid_dir()
  local root = M.steam_root(home)
  if not root then return nil, 'no bootstrapped Steam installation found' end
  local raw = read_file(root .. '/config/loginusers.vdf') or ''
  local key = root .. '|' .. #raw .. '|' .. raw:sub(1, 96)
  if grid_cache.key == key then return grid_cache.dir, grid_cache.error end
  local account, why = M.resolve_account(root)
  local directory, err
  if not account then
    err = why
  else
    local id = M.account_id(account)
    if not id then
      err = 'could not derive the account id'
    else
      directory = root .. '/userdata/' .. tostring(id) .. '/config/grid'
    end
  end
  grid_cache.key, grid_cache.dir, grid_cache.error = key, directory, err
  return directory, err
end

-- All files belonging to one slot, whatever extension Steam might have.
local SLOT_EXTS = { 'png', 'jpg', 'jpeg', 'webp', 'ico' }

local function slot_files(directory, appid, kind)
  local base = directory .. '/' .. tostring(appid) .. KINDS[kind]
  local out = {}
  for _, ext in ipairs(SLOT_EXTS) do
    if fs.exists(base .. '.' .. ext) then out[#out + 1] = base .. '.' .. ext end
  end
  return out
end

-- Undo history: every replaced file lands in
-- <grid>/.sls-luna-history/<appid><suffix>/<timestamp>-<n>.<ext>, newest kept
-- (HISTORY_KEEP each), plus one append-only op log at the history root.
-- Undo steps back through the chain; Reset removes the custom file so Steam
-- shows its own artwork again. History survives both.
local HISTORY_KEEP = 5
local OP_LOG_MAX = 200

local function history_root(directory)
  return directory .. '/.sls-luna-history'
end

local function slot_history_dir(directory, appid, kind)
  return history_root(directory) .. '/' .. tostring(appid) .. KINDS[kind]
end

local function op_log_path(directory)
  return history_root(directory) .. '/op-log.json'
end

local function read_bytes(path)
  local f = io.open(path, 'rb')
  if not f then return nil end
  local raw = f:read('*a')
  f:close()
  return raw
end

local function list_dir(dir)
  local names, seen = {}, {}
  local function add(name)
    name = tostring(name or '')
    if name ~= '' and not seen[name] then seen[name] = true; names[#names + 1] = name end
  end
  local ok, fs = pcall(require, 'fs')
  if ok and type(fs) == 'table' and type(fs.list) == 'function' then
    local ok_list, items = pcall(fs.list, dir)
    if ok_list and type(items) == 'table' then
      for _, item in ipairs(items) do
        add(type(item) == 'table' and item.name or item)
      end
    end
  end
  -- The test harness ships an fs shim whose list() is a stub; ls is the
  -- fallback there (and on any Lumen without the shim).
  local pipe = io.popen("ls -1 '" .. dir:gsub("'", "'\\''") .. "' 2>/dev/null")
  if pipe then
    for line in pipe:lines() do add(line) end
    pipe:close()
  end
  return names
end

local function history_entries(directory, appid, kind)
  local out = {}
  for _, name in ipairs(list_dir(slot_history_dir(directory, appid, kind))) do
    if name:match('^%d+%-%d+%.[%w]+$') then out[#out + 1] = name end
  end
  -- Numeric (stamp, sequence) order: oldest first, newest last.
  local function key(name)
    local s, n = name:match('^(%d+)%-(%d+)%.')
    return tonumber(s), tonumber(n)
  end
  table.sort(out, function(a, b)
    local sa, na = key(a)
    local sb, nb = key(b)
    if sa ~= sb then return sa < sb end
    if na ~= nb then return na < nb end
    return a < b
  end)
  return out
end

local function prune_history(directory, appid, kind)
  local entries = history_entries(directory, appid, kind)
  while #entries > HISTORY_KEEP do
    os.remove(slot_history_dir(directory, appid, kind) .. '/' .. entries[1])
    table.remove(entries, 1)
  end
end

local function log_op(directory, entry)
  local path = op_log_path(directory)
  local log = read_json(path) or {}
  log[#log + 1] = entry
  while #log > OP_LOG_MAX do table.remove(log, 1) end
  fs.create_directories(history_root(directory))
  write_file_atomic(path, json.encode(log))
end

-- When each slot was last changed, recovered from the op-log the backend
-- already writes. Additive: the page can show "when" next to the undo count.
local function last_times(directory, appid)
  local times, recovered = {}, false
  local log
  log, recovered = read_json(op_log_path(directory))
  for _, entry in ipairs(type(log) == 'table' and log or {}) do
    if type(entry) == 'table' and tonumber(entry.appid) == appid
        and KINDS[tostring(entry.kind)] and tonumber(entry.at) then
      local kind = tostring(entry.kind)
      local at = math.floor(tonumber(entry.at))
      if at > 0 and (not times[kind] or at > times[kind]) then times[kind] = at end
    end
  end
  return times, recovered
end

-- Stash the slot's current file(s) into history before they are replaced.
-- Old single-level `.sls-luna.bak` files are imported, not dropped.
local function push_history(directory, appid, kind, author)
  local slot = math.floor(appid)
  local current = slot_files(directory, slot, kind)
  local legacy = {}
  do
    local base = directory .. '/' .. tostring(slot) .. KINDS[kind]
    os.remove(base .. '.sls-luna.bak') -- legacy naming, if any
    for _, ext in ipairs(SLOT_EXTS) do
      local path = base .. '.' .. ext .. '.sls-luna.bak'
      if fs.exists(path) then legacy[#legacy + 1] = path end
    end
  end
  if #current == 0 and #legacy == 0 then return 0 end
  local dir = slot_history_dir(directory, slot, kind)
  fs.create_directories(dir)
  local stamp = os.time()
  local stored = 0
  -- Entries sort by name, so the sequence continues after any entry already
  -- stamped this second: two changes within one second never collide (which
  -- overwrote the older step) and the newer one always sorts last.
  local seq = 0
  for _, name in ipairs(history_entries(directory, slot, kind)) do
    local s, n = name:match('^(%d+)%-(%d+)%.')
    if tonumber(s) == stamp and tonumber(n) > seq then seq = tonumber(n) end
  end
  local function stash(path)
    local body = read_bytes(path)
    if not body then return end
    local ext = path:match('%.([%w]+)$') or 'png'
    seq = seq + 1
    local name = string.format('%d-%06d.%s', stamp, seq, ext)
    if write_file_atomic(dir .. '/' .. name, body) then stored = stored + 1 end
  end
  for _, path in ipairs(current) do stash(path) end
  for _, path in ipairs(legacy) do
    stash(path)
    os.remove(path)
  end
  prune_history(directory, slot, kind)
  if stored > 0 then
    log_op(directory, { at = stamp, appid = slot, kind = kind,
      author = tostring(author or ''), files = stored })
  end
  return stored
end

-- Files removed by Undo go to <slot history>/undone/ (same naming, newest
-- last, pruned like the history) so an Undo never destroys artwork outright.
-- Returns true (plus the files it wrote), or false and why after removing
-- anything it wrote; the caller must not delete on false.
local function keep_undone(directory, appid, kind, paths)
  local dir = slot_history_dir(directory, math.floor(appid), kind) .. '/undone'
  local made, made_why = pcall(fs.create_directories, dir)
  if not made then return false, tostring(made_why) end
  local stamp = os.time()
  local names = {}
  for _, name in ipairs(list_dir(dir)) do
    if name:match('^%d+%-%d+%.[%w]+$') then names[#names + 1] = name end
  end
  local seq = 0
  for _, name in ipairs(names) do
    local st, n = name:match('^(%d+)%-(%d+)%.')
    if tonumber(st) == stamp and tonumber(n) > seq then seq = tonumber(n) end
  end
  local created = {}
  local function undo_created()
    for _, file in ipairs(created) do os.remove(file); os.remove(file .. '.bak') end
  end
  for _, path in ipairs(paths) do
    local body = read_bytes(path)
    if not body then undo_created(); return false, 'cannot read ' .. path end
    seq = seq + 1
    local name = string.format('%d-%06d.%s', stamp, seq, path:match('%.([%w]+)$') or 'png')
    local ok, why = write_file_atomic(dir .. '/' .. name, body)
    if not ok then undo_created(); return false, why or ('cannot write ' .. dir .. '/' .. name) end
    created[#created + 1] = dir .. '/' .. name
    names[#names + 1] = name
  end
  table.sort(names)
  while #names > HISTORY_KEEP do
    os.remove(dir .. '/' .. names[1])
    table.remove(names, 1)
  end
  -- The caller removes these again if its own later step fails.
  return true, nil, created
end

local function history_depth(directory, appid, kind)
  return #history_entries(directory, appid, kind)
end

-- Remove up to `count` newest history entries and return their bytes with
-- extensions, so callers can write them back under the slot's own names.
local function pop_entries(directory, appid, kind, count)
  local dir = slot_history_dir(directory, math.floor(appid), kind)
  local entries = history_entries(directory, appid, kind)
  local popped = {}
  while count > 0 and #entries > 0 do
    local name = entries[#entries]
    local body = read_bytes(dir .. '/' .. name)
    os.remove(dir .. '/' .. name)
    entries[#entries] = nil
    if body then
      popped[#popped + 1] = { ext = name:match('%.([%w]+)$') or 'png', body = body }
    end
    count = count - 1
  end
  return popped
end

-- ------------------------------------------------------------------ http

local ok_http, http = pcall(require, 'http')

-- Every request carries its own deadline and a body cap, and any failure
-- (including the module throwing) comes back as a reason, never as an error.
local function http_get(url, headers, timeout)
  if not ok_http or type(http) ~= 'table' or type(http.get) ~= 'function' then
    return nil, 'The Lumen HTTP module is unavailable; cannot reach SteamGridDB.'
  end
  local ok, response, err = pcall(http.get, url,
    { headers = headers, timeout = timeout or HTTP_TIMEOUT })
  if not ok then
    return nil, 'SteamGridDB request failed: ' .. err_text(response)
  end
  if type(response) ~= 'table' then
    return nil, 'SteamGridDB request failed: ' .. tostring(err or 'no response')
  end
  if type(response.body) == 'string' and #response.body > MAX_BODY_BYTES then
    return nil, 'The SteamGridDB response was too large.'
  end
  return response
end

local function api_headers(key)
  return {
    ['User-Agent'] = USER_AGENT,
    ['Accept'] = 'application/json',
    ['Authorization'] = 'Bearer ' .. tostring(key or ''),
  }
end

local function decode_body(body)
  if type(body) ~= 'string' or body == '' then return nil end
  local ok, parsed = pcall(json.decode, body)
  if ok and type(parsed) == 'table' then return parsed end
  return nil
end

local function url_encode(text)
  return tostring(text):gsub('[^%w%-%._~]', function(ch)
    return string.format('%%%02X', ch:byte())
  end)
end

-- Every non-200 answer is classified in one place, so a rate limit or an
-- invalid key reads the same whichever handler asked.
local function header_value(response, name)
  local headers = type(response) == 'table' and response.headers or nil
  if type(headers) ~= 'table' then return nil end
  local want = tostring(name):lower()
  for key, value in pairs(headers) do
    if tostring(key):lower() == want then return value end
  end
  return nil
end

local function retry_after(response)
  local seconds = tonumber(tostring(header_value(response, 'retry-after') or ''):match('^%s*(%d+)'))
  return seconds and math.floor(seconds) or nil
end

-- Returns {code, error, retryAfter?} for a failed response.
local function http_failure(response, what)
  local status = tonumber(type(response) == 'table' and response.status) or 0
  if status == 401 then
    return { code = 'bad_key', error = 'SteamGridDB rejected that API key.' }
  end
  if status == 429 then
    local wait = retry_after(response)
    local hint = wait and (' SteamGridDB asked to wait ' .. tostring(wait) .. ' s.')
      or ' SteamGridDB asked to wait before retrying.'
    return { code = 'rate_limit', retryAfter = wait,
      error = 'Rate limited by SteamGridDB.' .. hint }
  end
  return { code = 'http',
    error = 'SteamGridDB ' .. tostring(what) .. ' failed (HTTP ' .. tostring(status) .. ').' }
end

-- Steam shows a non-Steam shortcut under its 32-bit AppID (the form in
-- SteamClient.Apps and in presence.json), but the 64-bit encoding
-- (id << 32) | 0x02000000 also circulates. Artwork files are always named by
-- the 32-bit form, so both are accepted and normalised here.
function M.normalize_appid(value)
  local n = tonumber(value)
  if not n or n ~= math.floor(n) or n <= 0 then return nil end
  if n <= 4294967295 then return n end
  local high = math.floor(n / 4294967296)
  if high >= 1 and high <= 4294967295 then return high end
  return nil
end

function M.base64(data)
  local alphabet = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/'
  local out = {}
  for index = 1, #data, 3 do
    local a, b, c = data:byte(index, index + 2)
    local group = a * 65536 + (b or 0) * 256 + (c or 0)
    local first = math.floor(group / 262144) % 64
    local second = math.floor(group / 4096) % 64
    local third = math.floor(group / 64) % 64
    local fourth = group % 64
    out[#out + 1] = alphabet:sub(first + 1, first + 1) .. alphabet:sub(second + 1, second + 1)
      .. (b and alphabet:sub(third + 1, third + 1) or '=')
      .. (c and alphabet:sub(fourth + 1, fourth + 1) or '=')
  end
  return table.concat(out)
end

-- The assets endpoint: grids take a dimension filter for the wide slot, the
-- other kinds are their own collections.
local function assets_url(gameId, kind)
  if kind == 'grid' then return API .. '/grids/game/' .. tostring(gameId) end
  if kind == 'wide' then return API .. '/grids/game/' .. tostring(gameId) .. '?dimensions=920x430' end
  local collection = kind == 'hero' and 'heroes' or (tostring(kind) .. 's')
  return API .. '/' .. collection .. '/game/' .. tostring(gameId)
end

-- Shared by the panel's Assets call and the auto picker: fetch and sanitize the
-- candidate list. Returns assets, total or nil, code, error.
local function fetch_assets(config, gameId, kind)
  local response, why = http_get(assets_url(gameId, kind), api_headers(config.apiKey))
  if not response then return nil, nil, 'unavailable', why end
  if response.status ~= 200 then
    local failure = http_failure(response, 'assets')
    return nil, nil, failure.code, failure.error, failure.retryAfter
  end
  local parsed = decode_body(response.body)
  if not parsed or type(parsed.data) ~= 'table' then
    return nil, nil, 'unexpected', 'SteamGridDB returned an unexpected response.'
  end
  return M.sanitize_assets(parsed.data), #parsed.data
end

-- Store already-downloaded bytes in the slot, stashing whatever was there.
-- Split from the download so the Auto job can fetch asynchronously and hand
-- the bytes back here without a second request.
local function store_asset(appid, kind, body, mime, author)
  if not ALLOWED_MIME[tostring(mime)] then
    return false, { code = 'mime', error = 'That image type cannot be used by Steam (use PNG or JPEG).' }
  end
  if type(body) ~= 'string' or #body == 0 then
    return false, { code = 'unexpected', error = 'The image download was empty.' }
  end
  if #body > MAX_IMAGE_BYTES then
    return false, { code = 'too_big', error = 'That image is too large.' }
  end
  local directory, why = grid_dir()
  if not directory then return false, { code = 'no_steam', error = tostring(why) } end
  fs.create_directories(directory)
  local slot = math.floor(appid)
  local base = directory .. '/' .. tostring(slot) .. KINDS[kind]
  local existing = slot_files(directory, slot, kind)
  -- Transactional order: stash first, write second, clean up last. The old
  -- bytes are safe in history before anything is removed; if the write
  -- fails, the stashed copies are put back and the slot is unchanged.
  local stashed = 0
  if #existing > 0 then
    stashed = push_history(directory, slot, kind, author)
  end
  local extension = ALLOWED_MIME[tostring(mime)]
  local target = base .. '.' .. extension
  local saved, save_why = write_file_atomic(target, body)
  if not saved then
    if stashed > 0 then
      os.remove(target)
      for _, item in ipairs(pop_entries(directory, slot, kind, stashed)) do
        write_file_atomic(base .. '.' .. item.ext, item.body)
      end
    end
    return false, { code = 'io', error = tostring(save_why) }
  end
  for _, path in ipairs(existing) do
    if path ~= target then os.remove(path) end
  end
  return true, { path = target, kind = kind, backedUp = stashed > 0,
    history = history_depth(directory, slot, kind) }
end

-- Shared by Apply: one download and one store, both bounded.
local function place_asset(appid, kind, url, mime, author)
  if not ALLOWED_MIME[tostring(mime)] then
    return false, { code = 'mime', error = 'That image type cannot be used by Steam (use PNG or JPEG).' }
  end
  if tostring(url or '') == '' then
    return false, { code = 'invalid', error = 'Missing image URL.' }
  end
  -- Only SteamGridDB's own CDN is fetched; a caller cannot point this at an
  -- arbitrary host (localhost, link-local) and make the backend write its bytes.
  if not tostring(url):match('^https://') then
    return false, { code = 'invalid', error = 'Only https image URLs are accepted.' }
  end
  local directory, why = grid_dir()
  if not directory then return false, { code = 'no_steam', error = tostring(why) } end
  local response, down_why = http_get(url, { ['User-Agent'] = USER_AGENT })
  if not response then return false, { code = 'unavailable', error = down_why } end
  if response.status ~= 200 then
    local failure = http_failure(response, 'image download')
    return false, { code = failure.code, error = failure.error, retryAfter = failure.retryAfter }
  end
  return store_asset(appid, kind, response.body, mime, author)
end

-- Keep only what the panel shows, drop flagged content, and offer the four
-- styles Steam can actually display. Sorted by SteamGridDB's own score.
function M.sanitize_assets(rows)
  local out = {}
  for _, row in ipairs(rows or {}) do
    if type(row) == 'table' and not row.nsfw and not row.humor and not row.epilepsy then
      local mime = tostring(row.mime or '')
      if ALLOWED_MIME[mime] and type(row.url) == 'string' and row.url ~= '' then
        out[#out + 1] = {
          id = tonumber(row.id) or 0,
          url = row.url,
          thumb = tostring(row.thumb or row.url),
          width = tonumber(row.width) or 0,
          height = tonumber(row.height) or 0,
          style = tostring(row.style or ''),
          mime = mime,
          author = tostring((type(row.author) == 'table' and row.author.name) or ''),
        }
      end
    end
  end
  table.sort(out, function(a, b)
    if a.width * a.height ~= b.width * b.height then
      return a.width * a.height > b.width * b.height
    end
    return a.id < b.id
  end)
  local capped = {}
  for index = 1, math.min(#out, MAX_ASSETS) do capped[index] = out[index] end
  return capped
end

-- ------------------------------------------------------------------ auto job
--
-- One Auto pass is five API lookups plus five image downloads. Run inline,
-- that held Lumen's only injector thread for minutes. SlsSgdbAuto now creates
-- a job and returns at once; each SlsSgdbAutoStatus call advances it by at
-- most one request (http.start/http.poll where the host has them, otherwise a
-- single GET with an 8 s deadline), so a dead network costs one short timeout
-- per poll instead of freezing Steam.

local auto_job = nil
local auto_seq = 0

local function async_http()
  return ok_http and type(http) == 'table'
    and type(http.start) == 'function' and type(http.poll) == 'function'
end

local function job_kind(job) return AUTO_KINDS[job.index] end

-- The panel-facing shape. Unfinished kinds are listed as pending so the page
-- can always show one row per slot and fill in each result as it lands.
local function job_snapshot(job)
  local results = {}
  for _, result in ipairs(job.results) do results[#results + 1] = result end
  for index = job.done + 1, #AUTO_KINDS do
    results[#results + 1] = { kind = AUTO_KINDS[index], ok = false, pending = true,
      error = 'Still working...' }
  end
  return {
    success = true,
    state = job.state,
    pending = job.state == 'running',
    job = { id = job.id, state = job.state, done = job.done, total = job.total,
      startedAt = job.startedAt, appid = job.appid, gameId = job.gameId },
    results = json.array(results),
    error = job.error or '',
  }
end

local function job_finish_item(job, result)
  result.kind = job_kind(job)
  job.results[#job.results + 1] = result
  job.done = #job.results
  job.index = job.index + 1
  job.stage, job.asset, job.handle, job.response = 'assets', nil, nil, nil
  if job.index > #AUTO_KINDS then
    job.state, job.error = 'done', ''
  end
end

-- Begin the current kind's request. Async mode leaves a handle for the poll;
-- the blocking fallback returns the response in the same call.
local function job_start(job)
  local kind = job_kind(job)
  local url, headers
  if job.stage == 'assets' then
    url, headers = assets_url(job.gameId, kind), api_headers(job.config.apiKey)
  else
    url, headers = job.asset.url, { ['User-Agent'] = USER_AGENT }
  end
  if async_http() then
    local ok, handle, err = pcall(http.start, url, { headers = headers, timeout = HTTP_TIMEOUT })
    if not ok or not handle then
      job_finish_item(job, { ok = false, code = 'unavailable',
        error = 'SteamGridDB request failed: ' .. err_text(ok and err or handle) })
      return
    end
    job.handle = handle
    return
  end
  local response, why = http_get(url, headers)
  if not response then
    job_finish_item(job, { ok = false, code = 'unavailable', error = tostring(why) })
    job.sync_used = true
    return
  end
  job.response, job.sync_used = response, true
end

-- Turn the finished response for the current stage into a result.
local function job_finish(job)
  local kind = job_kind(job)
  local response = job.response
  job.response = nil
  if type(response.body) == 'string' and #response.body > MAX_BODY_BYTES then
    job_finish_item(job, { ok = false, code = 'too_big',
      error = 'That SteamGridDB response was too large.' })
    return
  end
  if job.stage == 'assets' then
    if response.status ~= 200 then
      local failure = http_failure(response, 'assets')
      job_finish_item(job, { ok = false, code = failure.code, error = failure.error,
        retryAfter = failure.retryAfter })
    else
      local parsed = decode_body(response.body)
      if not parsed or type(parsed.data) ~= 'table' then
        job_finish_item(job, { ok = false, code = 'unexpected',
          error = 'SteamGridDB returned an unexpected response.' })
      else
        local assets = M.sanitize_assets(parsed.data)
        if #assets == 0 then
          job_finish_item(job, { ok = false, code = 'none',
            error = 'No usable ' .. kind .. ' images.' })
        else
          job.asset, job.stage = assets[1], 'download'
        end
      end
    end
    return
  end
  local asset = job.asset
  if response.status ~= 200 then
    local failure = http_failure(response, 'image download')
    job_finish_item(job, { ok = false, code = failure.code, error = failure.error,
      retryAfter = failure.retryAfter })
    return
  end
  local placed, detail = store_asset(job.appid, kind, response.body, asset.mime, asset.author)
  if placed then
    job_finish_item(job, { ok = true, path = detail.path, backedUp = detail.backedUp,
      author = asset.author, width = asset.width, height = asset.height,
      live = LIVE_TYPES[kind] ~= nil })
  else
    job_finish_item(job, { ok = false, code = detail.code, error = detail.error })
  end
end

-- Advance the job without ever waiting: at most four cheap transitions per
-- call, and at most one network request.
local function auto_advance(job)
  if not job or job.state ~= 'running' then return end
  local actions = 0
  while job.state == 'running' and actions < 4 do
    if os.time() - job.startedAt > AUTO_JOB_TTL then
      while job.state == 'running' do
        job_finish_item(job, { ok = false, code = 'timeout',
          error = 'SteamGridDB did not answer in time.' })
      end
      return
    end
    if job.handle then
      local ok, done, response, err = pcall(http.poll, job.handle, 0)
      if not ok then
        job.handle = nil
        job_finish_item(job, { ok = false, code = 'unavailable',
          error = 'SteamGridDB request failed: ' .. err_text(done) })
        actions = actions + 1
      elseif not done then
        return -- in flight; the next status poll asks again
      else
        job.handle = nil
        if type(response) == 'table' then
          job.response = response
        else
          job_finish_item(job, { ok = false, code = 'unavailable',
            error = 'SteamGridDB request failed: ' .. tostring(err or 'no response') })
          actions = actions + 1
        end
      end
    elseif job.response then
      job_finish(job)
      actions = actions + 1
      -- The blocking fallback performs one bounded request per poll, no more:
      -- a slow network must not chain several timeouts inside one RPC.
      if job.sync_used then job.sync_used = nil; return end
    else
      job_start(job)
      actions = actions + 1
      if job.sync_used then job.sync_used = nil; return end
    end
  end
end

-- ------------------------------------------------------------------ registry

function M.register(registry)
  registry.SlsSgdbGet = function()
    local config, recovered = load_config()
    local directory, why = grid_dir()
    local masked = ''
    if config.apiKey ~= '' then
      local tail = config.apiKey:sub(-4)
      masked = '••••' .. tail
    end
    return reply({
      success = true,
      version = VERSION,
      hasKey = config.apiKey ~= '',
      keyMasked = masked,
      gridDir = directory or '',
      gridError = directory and '' or tostring(why),
      recovered = recovered == true,
      recoveredNote = recovered == true and 'The key file was torn; restored from sgdb.json.bak.' or '',
    })
  end

  registry.SlsSgdbSetKey = function(raw)
    local ok, entry = pcall(json.decode, tostring(raw or ''))
    if not ok or type(entry) ~= 'table' or type(entry.key) ~= 'string' then
      return reply({ success = false, error = 'Send {"key": "..."}.' })
    end
    local config = load_config()
    config.apiKey = entry.key:gsub('^%s+', ''):gsub('%s+$', '')
    local saved, why = save_config(config)
    if not saved then return reply({ success = false, error = tostring(why) }) end
    return reply({ success = true, hasKey = config.apiKey ~= '' })
  end

  -- A cheap authenticated call that proves the stored key works.
  registry.SlsSgdbTest = function()
    local config = load_config()
    if config.apiKey == '' then
      return reply({ success = false, code = 'no_key', error = 'Add your SteamGridDB API key first.' })
    end
    local response, why = http_get(API .. '/search/autocomplete/portal', api_headers(config.apiKey))
    if not response then return reply({ success = false, code = 'unavailable', error = why }) end
    if response.status ~= 200 then
      local failure = http_failure(response, 'call')
      return reply({ success = false, code = failure.code, error = failure.error,
        retryAfter = failure.retryAfter })
    end
    return reply({ success = true })
  end

  registry.SlsSgdbSearch = function(raw)
    local ok, entry = pcall(json.decode, tostring(raw or ''))
    if not ok or type(entry) ~= 'table' or type(entry.term) ~= 'string' then
      return reply({ success = false, error = 'Send {"term": "..."}.' })
    end
    local term = entry.term:gsub('^%s+', ''):gsub('%s+$', '')
    if term == '' then return reply({ success = false, error = 'Type a game name first.' }) end
    local config = load_config()
    if config.apiKey == '' then
      return reply({ success = false, code = 'no_key', error = 'Add your SteamGridDB API key first.' })
    end
    local response, why = http_get(API .. '/search/autocomplete/' .. url_encode(term),
      api_headers(config.apiKey))
    if not response then return reply({ success = false, code = 'unavailable', error = why }) end
    if response.status ~= 200 then
      local failure = http_failure(response, 'search')
      return reply({ success = false, code = failure.code, error = failure.error,
        retryAfter = failure.retryAfter })
    end
    local parsed = decode_body(response.body)
    local rows = parsed and parsed.data
    if type(rows) ~= 'table' then
      return reply({ success = false, code = 'unexpected', error = 'SteamGridDB returned an unexpected response.' })
    end
    local games = {}
    for _, row in ipairs(rows) do
      if type(row) == 'table' and tonumber(row.id) and row.name then
        games[#games + 1] = { id = tonumber(row.id), name = tostring(row.name) }
      end
      if #games >= 12 then break end
    end
    return reply({ success = true, games = games })
  end

  registry.SlsSgdbAssets = function(raw)
    local ok, entry = pcall(json.decode, tostring(raw or ''))
    if not ok or type(entry) ~= 'table' or not tonumber(entry.gameId) or not KINDS[tostring(entry.kind)] then
      return reply({ success = false, error = 'Send {"gameId": <id>, "kind": "grid|wide|hero|logo|icon"}.' })
    end
    local config = load_config()
    if config.apiKey == '' then
      return reply({ success = false, code = 'no_key', error = 'Add your SteamGridDB API key first.' })
    end
    local assets, total, code, why, wait = fetch_assets(config, math.floor(entry.gameId), tostring(entry.kind))
    if not assets then
      return reply({ success = false, code = code, error = why, retryAfter = wait })
    end
    return reply({ success = true, assets = assets, total = total })
  end

  -- Download one asset and place it in the Steam grid folder, backing up the
  -- current file for that slot so Restore can put it back.
  registry.SlsSgdbApply = function(raw)
    local ok, entry = pcall(json.decode, tostring(raw or ''))
    if not ok or type(entry) ~= 'table' then
      return reply({ success = false, error = 'Send {"appid", "kind", "url", "mime"}.' })
    end
    local appid = M.normalize_appid(entry.appid)
    local kind = tostring(entry.kind)
    if not appid then
      return reply({ success = false, code = 'invalid', error = 'Invalid app id.' })
    end
    if not KINDS[kind] then
      return reply({ success = false, code = 'invalid', error = 'Unknown artwork kind.' })
    end
    local placed, detail = place_asset(appid, kind, entry.url, entry.mime)
    if not placed then
      return reply({ success = false, code = detail.code, error = detail.error })
    end
    return reply({ success = true, path = detail.path, kind = kind, backedUp = detail.backedUp,
      appid = appid, live = LIVE_TYPES[kind] ~= nil })
  end

  -- Start the auto pass and return at once. The page polls SlsSgdbAutoStatus
  -- for progress; nothing here waits for SteamGridDB.
  registry.SlsSgdbAuto = function(raw)
    local ok, entry = pcall(json.decode, tostring(raw or ''))
    if not ok or type(entry) ~= 'table' or not tonumber(entry.appid) or not tonumber(entry.gameId) then
      return reply({ success = false, error = 'Send {"appid": <id>, "gameId": <id>}.' })
    end
    local appid = M.normalize_appid(entry.appid)
    local gameId = math.floor(tonumber(entry.gameId))
    if not appid then
      return reply({ success = false, code = 'invalid', error = 'Invalid app id.' })
    end
    local config = load_config()
    if config.apiKey == '' then
      return reply({ success = false, code = 'no_key', error = 'Add your SteamGridDB API key first.' })
    end
    appid = math.floor(appid)
    -- A pass already running for this game is reused: a second one would only
    -- double the requests.
    if auto_job and auto_job.state == 'running'
        and auto_job.appid == appid and auto_job.gameId == gameId then
      return reply(job_snapshot(auto_job))
    end
    auto_seq = auto_seq + 1
    auto_job = { id = auto_seq, appid = appid, gameId = gameId, config = config,
      startedAt = os.time(), index = 1, done = 0, total = #AUTO_KINDS,
      stage = 'assets', results = {}, state = 'running', error = '' }
    -- Kick off the first request when that is non-blocking; with only the
    -- blocking http.get the first status poll starts it instead.
    if async_http() then job_start(auto_job) end
    return reply(job_snapshot(auto_job))
  end

  -- Cheap progress poll: reads the in-memory job (advancing it by one bounded
  -- request) and reports every slot, including which one failed and why.
  registry.SlsSgdbAutoStatus = function(raw)
    local ok, entry = pcall(json.decode, tostring(raw or ''))
    if not ok or type(entry) ~= 'table' then entry = {} end
    local job = auto_job
    if not job then
      return reply({ success = false, code = 'no_job',
        error = 'No artwork pass has been started.' })
    end
    if entry.id ~= nil and tonumber(entry.id) ~= job.id then
      return reply({ success = false, code = 'stale',
        error = 'That artwork pass is no longer running.' })
    end
    auto_advance(job)
    return reply(job_snapshot(job))
  end

  -- The bytes of the slot's current file, base64-encoded, so the agent can ask
  -- Steam's own SetCustomArtworkForApp to apply and refresh it immediately
  -- (the only context with SteamClient.Apps is SharedJSContext).
  registry.SlsSgdbLive = function(raw)
    local ok, entry = pcall(json.decode, tostring(raw or ''))
    if not ok or type(entry) ~= 'table' or not tonumber(entry.appid) or not KINDS[tostring(entry.kind)] then
      return reply({ success = false, error = 'Send {"appid": <id>, "kind": "..."}.' })
    end
    local kind = tostring(entry.kind)
    local type_number = LIVE_TYPES[kind]
    if type_number == nil then
      return reply({ success = false, code = 'no_live', error = 'Steam has no live update for this slot.' })
    end
    local directory, why = grid_dir()
    if not directory then return reply({ success = false, code = 'no_steam', error = tostring(why) }) end
    local appid = M.normalize_appid(entry.appid)
    if not appid then
      return reply({ success = false, code = 'invalid', error = 'Invalid app id.' })
    end
    local files = slot_files(directory, appid, kind)
    if #files == 0 then
      return reply({ success = false, code = 'none', error = 'Nothing has been applied to this slot yet.' })
    end
    -- Size first: reading a huge file into memory on the RPC path would stall
    -- the injector before the cap below ever ran.
    local probe = io.open(files[1], 'rb')
    local size = probe and probe:seek('end')
    if probe then probe:close() end
    if not size or size > MAX_LIVE_BYTES then
      return reply({ success = false, code = 'too_big',
        error = 'The image is too large for a live update; restart Steam to see it.' })
    end
    local body = read_file(files[1])
    if not body then return reply({ success = false, code = 'io', error = 'Could not read the artwork file.' }) end
    if #body > MAX_LIVE_BYTES then
      return reply({ success = false, code = 'too_big',
        error = 'The image is too large for a live update; restart Steam to see it.' })
    end
    local extension = files[1]:match('%.([%w]+)$') or 'png'
    return reply({ success = true, base64 = M.base64(body), extension = extension, type = type_number })
  end

  -- Undo one Luna change for a slot: the current file is stashed (so Undo
  -- toggles losslessly) and the newest history entry takes its place.
  registry.SlsSgdbUndo = function(raw)
    local ok, entry = pcall(json.decode, tostring(raw or ''))
    if not ok or type(entry) ~= 'table' or not tonumber(entry.appid) or not KINDS[tostring(entry.kind)] then
      return reply({ success = false, error = 'Send {"appid": <id>, "kind": "..."}.' })
    end
    local appid = M.normalize_appid(entry.appid)
    local kind = tostring(entry.kind)
    if not appid then
      return reply({ success = false, code = 'invalid', error = 'Invalid app id.' })
    end
    local directory, why = grid_dir()
    if not directory then return reply({ success = false, code = 'no_steam', error = tostring(why) }) end
    local current = slot_files(directory, appid, kind)
    if history_depth(directory, appid, kind) == 0 then
      return reply({ success = false, code = 'empty', error = 'Nothing to undo for this slot.' })
    end
    local base = directory .. '/' .. tostring(appid) .. KINDS[kind]
    -- Transactional, so a failed write never loses artwork:
    --   1. read the previous step without consuming it;
    --   2. archive the current file(s) to undone/ (outside the undo stack:
    --      stashing them on the stack made a second Undo act as a redo);
    --   3. write the restored artwork;
    --   4. only then remove the replaced file(s) and consume the history entry.
    -- Any failure before step 4 leaves the current artwork and the history
    -- entry in place for a retry.
    local entries = history_entries(directory, appid, kind)
    local name = entries[#entries]
    local history_dir = slot_history_dir(directory, math.floor(appid), kind)
    local body = name and read_bytes(history_dir .. '/' .. name)
    if not body then
      return reply({ success = false, code = 'io', error = 'Could not read the undo entry.' })
    end
    local archive_files = {}
    if #current > 0 then
      local archived, archive_why, written = keep_undone(directory, appid, kind, current)
      archive_files = written or {}
      if not archived then
        return reply({ success = false, code = 'io',
          error = 'Could not keep a copy of the current artwork; nothing was changed: ' .. tostring(archive_why) })
      end
    end
    local target = base .. '.' .. (name:match('%.([%w]+)$') or 'png')
    local saved, save_why = write_file_atomic(target, body)
    if not saved then
      for _, file in ipairs(archive_files) do os.remove(file); os.remove(file .. '.bak') end
      return reply({ success = false, code = 'io',
        error = 'Could not restore the previous artwork; nothing was changed: ' .. tostring(save_why) })
    end
    for _, path in ipairs(current) do
      if path ~= target then os.remove(path) end
    end
    os.remove(history_dir .. '/' .. name)
    return reply({ success = true, undone = true, path = target,
      history = history_depth(directory, appid, kind) })
  end

  -- Reset one slot to Steam's own artwork: the custom file is removed (the
  -- history is kept, so Undo can still bring a Luna change back).
  registry.SlsSgdbReset = function(raw)
    local ok, entry = pcall(json.decode, tostring(raw or ''))
    if not ok or type(entry) ~= 'table' or not tonumber(entry.appid) or not KINDS[tostring(entry.kind)] then
      return reply({ success = false, error = 'Send {"appid": <id>, "kind": "..."}.' })
    end
    local appid = M.normalize_appid(entry.appid)
    local kind = tostring(entry.kind)
    if not appid then
      return reply({ success = false, code = 'invalid', error = 'Invalid app id.' })
    end
    local directory, why = grid_dir()
    if not directory then return reply({ success = false, code = 'no_steam', error = tostring(why) }) end
    local current = slot_files(directory, appid, kind)
    if #current == 0 then return reply({ success = true, removed = false }) end
    for _, path in ipairs(current) do os.remove(path) end
    return reply({ success = true, removed = true,
      history = history_depth(directory, appid, kind) })
  end

  -- How many undo steps a slot has, so the panel can enable the button.
  registry.SlsSgdbHistory = function(raw)
    local ok, entry = pcall(json.decode, tostring(raw or ''))
    if not ok or type(entry) ~= 'table' or not tonumber(entry.appid) then
      return reply({ success = false, error = 'Send {"appid": <id>}.' })
    end
    local appid = M.normalize_appid(entry.appid)
    if not appid then
      return reply({ success = false, code = 'invalid', error = 'Invalid app id.' })
    end
    local directory, why = grid_dir()
    if not directory then return reply({ success = false, code = 'no_steam', error = tostring(why) }) end
    local depths = {}
    for kind in pairs(KINDS) do depths[kind] = history_depth(directory, appid, kind) end
    local times, recovered = last_times(directory, appid)
    return reply({ success = true, appid = appid, history = depths, times = times,
      recovered = recovered == true })
  end

  -- An exception inside any handler must still produce a reply: otherwise the
  -- page waits for the RPC timeout and shows nothing at all.
  for name in pairs(registry) do
    if type(name) == 'string' and name:match('^SlsSgdb') and type(registry[name]) == 'function' then
      registry[name] = protected(registry[name])
    end
  end
end

return M
