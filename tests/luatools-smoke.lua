-- SLS Luna LuaTools footer smoke test: the read-only account state behind the
-- menu's Discord row. It covers the avatar URL allow-list, the refresh-token
-- rule for "signed in", and that nothing token-like ever leaves the backend.
--
-- Run in an isolated HOME (like the other backend smoke tests):
--   HOME=$tmp LUNA_TEST_HOME=$tmp lua tests/luatools-smoke.lua
assert(os.getenv('LUNA_TEST_HOME') == os.getenv('HOME'), 'Test must run in isolated HOME')

-- A small real JSON codec: the backend round-trips its own payloads.
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
    local names = {}
    for key in pairs(value) do names[#names + 1] = tostring(key) end
    table.sort(names)
    local parts = {}
    for _, name in ipairs(names) do
      parts[#parts + 1] = encode(name) .. ':' .. encode(value[name])
    end
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
    list = function() return {} end,
  }
end

local home = os.getenv('HOME')
local repo = os.getenv('LUNA_REPO') or '.'
local auth_path = home .. '/.local/share/Lumen/luatools/backend/data/lua_tools_auth.json'
local SECRET = 'sb-refresh-token-value-that-must-never-leak'

local backend = dofile(repo .. '/addons/luna/plugin/backend/main.lua')
local registry = {}
backend.register(registry)

local function write_auth(value)
  os.execute('mkdir -p ' .. string.format('%q', auth_path:match('^(.*)/[^/]*$')))
  local handle = assert(io.open(auth_path, 'wb'))
  handle:write(encode(value))
  handle:close()
end

local function call()
  local raw = registry.SlsLunaLuaTools()
  local decoded = decode_value(raw, 1)
  assert(decoded.success == true, 'the RPC reports success')
  assert(type(decoded.luatools) == 'table', 'the state is a table')
  return decoded.luatools
end

local function fixture(overrides)
  local user = {
    app_metadata = { provider = 'discord', providers = { 'discord' } },
    user_metadata = {
      full_name = 'Pablo Test',
      name = 'Pablo Test',
      custom_claims = { global_name = 'Pablo' },
      avatar_url = 'https://cdn.discordapp.com/avatars/1/abc.png?size=64',
      picture = 'https://cdn.discordapp.com/avatars/1/pic.png',
    },
  }
  local value = {
    refresh_token = SECRET,
    access_token = 'access-token-value',
    token_type = 'bearer',
    expires_in = 3600,
    expires_at = os.time() - 60, -- a stale one-hour token: still signed in
    user = user,
  }
  for key, override in pairs(overrides or {}) do value[key] = override end
  return value
end

-- 1. Signed in, a stale access token, and an accepted avatar.
write_auth(fixture())
local state = call()
assert(state.ok == true, 'the file parses')
assert(state.signedIn == true, 'a refresh token keeps the session signed in')
assert(state.name == 'Pablo', 'the Discord display name is returned, got ' .. tostring(state.name))
assert(state.expired == true, 'expired is still reported as a fact')
assert(state.avatar == 'https://cdn.discordapp.com/avatars/1/abc.png?size=64', 'the CDN avatar is returned')

-- 2. Avatar host checks: only the two Discord CDNs over https.
local function avatar_of(url, picture)
  local value = fixture()
  value.user.user_metadata.avatar_url = url
  value.user.user_metadata.picture = picture or ''
  write_auth(value)
  return call().avatar
end
assert(avatar_of('https://media.discordapp.net/avatars/1/a.png') == 'https://media.discordapp.net/avatars/1/a.png',
  'media.discordapp.net is accepted')
assert(avatar_of('http://cdn.discordapp.com/avatars/1/a.png') == '', 'http is rejected')
assert(avatar_of('https://evil.example/avatars/1/a.png') == '', 'another host is rejected')
assert(avatar_of('https://cdn.discordapp.com.evil.example/a.png') == '', 'a suffix host is rejected')
assert(avatar_of('https://cdn.discordapp.com/avatars/1/a.png" onerror="x') == '', 'quotes are rejected')
assert(avatar_of('https://cdn.discordapp.com/a b.png') == '', 'spaces are rejected')
assert(avatar_of('https://cdn.discordapp.com/' .. string.rep('a', 320) .. '.png') == '', 'over-long URLs are rejected')
assert(avatar_of('http://cdn.discordapp.com/x.png', 'https://cdn.discordapp.com/avatars/1/pic.png')
  == 'https://cdn.discordapp.com/avatars/1/pic.png', 'picture is the fallback when avatar_url is unusable')

-- 3. No refresh token: not signed in, even with a name.
local no_refresh = fixture({ refresh_token = '' })
write_auth(no_refresh)
state = call()
assert(state.signedIn == false, 'no refresh token means not signed in')
assert(state.name == 'Pablo', 'the name is still reported')

local wrong_type = fixture({ refresh_token = 12345 })
write_auth(wrong_type)
assert(call().signedIn == false, 'a non-string refresh token is not a session')

-- 4. Non-Discord provider is not a Discord sign-in.
local other = fixture()
other.user.app_metadata.provider = 'github'
write_auth(other)
assert(call().signedIn == false, 'another provider is not Discord')

-- 5. Nothing token-like leaves the backend.
write_auth(fixture())
state = call()
local allowed = { ok = true, signedIn = true, name = true, avatar = true, expired = true, at = true,
  path = true, error = true }
for key in pairs(state) do
  assert(allowed[key], 'unexpected field returned: ' .. tostring(key))
end
for _, key in ipairs({ 'refresh', 'access', 'token', 'user', 'expires_in' }) do
  assert(state[key] == nil, 'no ' .. key .. ' field is returned')
end
for key, value in pairs(state) do
  assert(not tostring(value):find(SECRET, 1, true), 'the refresh token never appears in ' .. tostring(key))
  assert(not tostring(value):find('access-token-value', 1, true), 'the access token never appears')
end
assert(state.path == auth_path, 'the path of the file read is reported')

-- 6. Missing file: not signed in, with an error and no invented name.
os.remove(auth_path)
state = call()
assert(state.ok == false, 'no file is not an answer')
assert(state.signedIn == false)
assert(state.name == '', 'no name is invented')
assert(state.avatar == '', 'no avatar is invented')
assert(type(state.error) == 'string' and state.error ~= '', 'the reason is returned')

print('luatools-smoke: ok')
