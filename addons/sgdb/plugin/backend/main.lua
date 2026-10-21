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
local VERSION = '0.2.1'
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

local function read_file(path)
  local f = io.open(path, 'rb')
  if not f then return nil end
  local raw = f:read('*a')
  f:close()
  return raw
end

local function write_file_atomic(path, body)
  fs.create_directories(fs.parent_path(path))
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

-- ------------------------------------------------------------------ config

local function load_config()
  local raw = read_file(config_path)
  if not raw then return { version = 1, apiKey = '' } end
  local ok, parsed = pcall(json.decode, raw)
  if not ok or type(parsed) ~= 'table' then return { version = 1, apiKey = '' } end
  parsed.version = 1
  parsed.apiKey = type(parsed.apiKey) == 'string' and parsed.apiKey or ''
  return parsed
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

local function grid_dir()
  local root = M.steam_root(home)
  if not root then return nil, 'no bootstrapped Steam installation found' end
  local account, why = M.resolve_account(root)
  if not account then return nil, why end
  local id = M.account_id(account)
  if not id then return nil, 'could not derive the account id' end
  return root .. '/userdata/' .. tostring(id) .. '/config/grid'
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

-- The backup keeps the original full name so Restore can put it back with the
-- right extension (Steam keys the slot by name, not by bytes).
local function slot_backup(directory, appid, kind)
  local base = directory .. '/' .. tostring(appid) .. KINDS[kind]
  for _, ext in ipairs(SLOT_EXTS) do
    local path = base .. '.' .. ext .. '.sls-luna.bak'
    if fs.exists(path) then return path, base .. '.' .. ext end
  end
  return nil
end

local function clear_backups(directory, appid, kind)
  local base = directory .. '/' .. tostring(appid) .. KINDS[kind]
  os.remove(base .. '.sls-luna.bak') -- legacy naming, if any
  for _, ext in ipairs(SLOT_EXTS) do os.remove(base .. '.' .. ext .. '.sls-luna.bak') end
end

-- ------------------------------------------------------------------ http

local ok_http, http = pcall(require, 'http')

local function http_get(url, headers)
  if not ok_http or type(http) ~= 'table' or type(http.get) ~= 'function' then
    return nil, 'The Lumen HTTP module is unavailable; cannot reach SteamGridDB.'
  end
  local response, err = http.get(url, { headers = headers, timeout = 20 })
  if type(response) ~= 'table' then
    return nil, 'SteamGridDB request failed: ' .. tostring(err or 'no response')
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
  if response.status == 401 then
    return nil, nil, 'bad_key', 'SteamGridDB rejected that API key.'
  end
  if response.status ~= 200 then
    return nil, nil, 'http', 'SteamGridDB assets failed (HTTP ' .. tostring(response.status) .. ').'
  end
  local parsed = decode_body(response.body)
  if not parsed or type(parsed.data) ~= 'table' then
    return nil, nil, 'unexpected', 'SteamGridDB returned an unexpected response.'
  end
  return M.sanitize_assets(parsed.data), #parsed.data
end

-- Shared by Apply and Auto: download one asset and place it in the grid
-- folder, backing up whatever occupied the slot. ok, detail.
local function place_asset(appid, kind, url, mime)
  local extension = ALLOWED_MIME[tostring(mime)]
  if not extension then
    return false, { code = 'mime', error = 'That image type cannot be used by Steam (use PNG or JPEG).' }
  end
  if tostring(url or '') == '' then
    return false, { code = 'invalid', error = 'Missing image URL.' }
  end
  local directory, why = grid_dir()
  if not directory then return false, { code = 'no_steam', error = tostring(why) } end
  fs.create_directories(directory)
  local response, down_why = http_get(url, { ['User-Agent'] = USER_AGENT })
  if not response then return false, { code = 'unavailable', error = down_why } end
  if response.status ~= 200 then
    return false, { code = 'http',
      error = 'The image download failed (HTTP ' .. tostring(response.status) .. ').' }
  end
  local body = response.body
  if type(body) ~= 'string' or #body == 0 then
    return false, { code = 'unexpected', error = 'The image download was empty.' }
  end
  if #body > MAX_IMAGE_BYTES then
    return false, { code = 'too_big', error = 'That image is too large.' }
  end
  local slot = math.floor(appid)
  local base = directory .. '/' .. tostring(slot) .. KINDS[kind]
  local existing = slot_files(directory, slot, kind)
  local backup = nil
  if #existing > 0 then
    clear_backups(directory, slot, kind)
    backup = existing[1] .. '.sls-luna.bak'
    local moved, move_why = os.rename(existing[1], backup)
    if not moved then
      return false, { code = 'io', error = 'Could not back up the current artwork: ' .. tostring(move_why) }
    end
    for index = 2, #existing do os.remove(existing[index]) end
  end
  local target = base .. '.' .. extension
  local saved, save_why = write_file_atomic(target, body)
  if not saved then return false, { code = 'io', error = tostring(save_why) } end
  return true, { path = target, kind = kind, backedUp = backup ~= nil }
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

-- ------------------------------------------------------------------ registry

function M.register(registry)
  registry.SlsSgdbGet = function()
    local config = load_config()
    local directory, why = grid_dir()
    return reply({
      success = true,
      version = VERSION,
      apiKey = config.apiKey,
      gridDir = directory or '',
      gridError = directory and '' or tostring(why),
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
    if response.status == 401 then
      return reply({ success = false, code = 'bad_key', error = 'SteamGridDB rejected that API key.' })
    end
    if response.status ~= 200 then
      return reply({ success = false, code = 'http',
        error = 'SteamGridDB answered HTTP ' .. tostring(response.status) .. '.' })
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
    if response.status == 401 then
      return reply({ success = false, code = 'bad_key', error = 'SteamGridDB rejected that API key.' })
    end
    if response.status ~= 200 then
      return reply({ success = false, code = 'http',
        error = 'SteamGridDB search failed (HTTP ' .. tostring(response.status) .. ').' })
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
    local assets, total, code, why = fetch_assets(config, math.floor(entry.gameId), tostring(entry.kind))
    if not assets then return reply({ success = false, code = code, error = why }) end
    return reply({ success = true, assets = assets, total = total })
  end

  -- Download one asset and place it in the Steam grid folder, backing up the
  -- current file for that slot so Restore can put it back.
  registry.SlsSgdbApply = function(raw)
    local ok, entry = pcall(json.decode, tostring(raw or ''))
    if not ok or type(entry) ~= 'table' then
      return reply({ success = false, error = 'Send {"appid", "kind", "url", "mime"}.' })
    end
    local appid = tonumber(entry.appid)
    local kind = tostring(entry.kind)
    if not (appid and appid > 0 and appid <= 4294967295 and appid == math.floor(appid)) then
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
      live = LIVE_TYPES[kind] ~= nil })
  end

  -- One pass that fills every slot with SteamGridDB's top-ranked usable image.
  -- Per-kind results so the page can show exactly what happened.
  registry.SlsSgdbAuto = function(raw)
    local ok, entry = pcall(json.decode, tostring(raw or ''))
    if not ok or type(entry) ~= 'table' or not tonumber(entry.appid) or not tonumber(entry.gameId) then
      return reply({ success = false, error = 'Send {"appid": <id>, "gameId": <id>}.' })
    end
    local appid = tonumber(entry.appid)
    local gameId = math.floor(tonumber(entry.gameId))
    if not (appid > 0 and appid <= 4294967295) then
      return reply({ success = false, code = 'invalid', error = 'Invalid app id.' })
    end
    local config = load_config()
    if config.apiKey == '' then
      return reply({ success = false, code = 'no_key', error = 'Add your SteamGridDB API key first.' })
    end
    local results = {}
    for _, kind in ipairs({ 'grid', 'wide', 'hero', 'logo', 'icon' }) do
      local assets, _, code, why = fetch_assets(config, gameId, kind)
      if not assets then
        results[#results + 1] = { kind = kind, ok = false, code = code, error = why }
      elseif #assets == 0 then
        results[#results + 1] = { kind = kind, ok = false, code = 'none',
          error = 'No usable ' .. kind .. ' images.' }
      else
        local asset = assets[1]
        local placed, detail = place_asset(appid, kind, asset.url, asset.mime)
        if placed then
          results[#results + 1] = { kind = kind, ok = true, path = detail.path,
            backedUp = detail.backedUp, author = asset.author, width = asset.width, height = asset.height,
            live = LIVE_TYPES[kind] ~= nil }
        else
          results[#results + 1] = { kind = kind, ok = false, code = detail.code, error = detail.error }
        end
      end
    end
    return reply({ success = true, results = results })
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
    local files = slot_files(directory, math.floor(tonumber(entry.appid)), kind)
    if #files == 0 then
      return reply({ success = false, code = 'none', error = 'Nothing has been applied to this slot yet.' })
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

  -- Restore the backup for one slot, or remove the custom artwork when there
  -- is nothing to restore (Steam then shows its own default art again).
  registry.SlsSgdbRestore = function(raw)
    local ok, entry = pcall(json.decode, tostring(raw or ''))
    if not ok or type(entry) ~= 'table' or not tonumber(entry.appid) or not KINDS[tostring(entry.kind)] then
      return reply({ success = false, error = 'Send {"appid": <id>, "kind": "..."}.' })
    end
    local appid = math.floor(tonumber(entry.appid))
    local kind = tostring(entry.kind)
    local directory, why = grid_dir()
    if not directory then return reply({ success = false, code = 'no_steam', error = tostring(why) }) end
    local current = slot_files(directory, appid, kind)
    local backup, original = slot_backup(directory, appid, kind)
    if backup then
      for _, path in ipairs(current) do os.remove(path) end
      local restored, restore_why = os.rename(backup, original)
      if not restored then
        return reply({ success = false, code = 'io', error = 'Could not restore: ' .. tostring(restore_why) })
      end
      return reply({ success = true, restored = true })
    end
    if #current == 0 then return reply({ success = true, restored = false, removed = false }) end
    for _, path in ipairs(current) do os.remove(path) end
    return reply({ success = true, restored = false, removed = true })
  end
end

return M
