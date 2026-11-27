-- SLS Luna SteamGridDB backend smoke test: key storage, search, asset
-- filtering, applying with backup, restore, and the refusal paths.
--
-- The network is a fake http module and HOME is disposable, so this never
-- talks to SteamGridDB and never touches real Steam artwork.
assert(os.getenv('LUNA_TEST_HOME') == os.getenv('HOME'), 'Test must run in isolated HOME')

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
    list = function() return {} end,
  }
end

local json = require('json')
local home = os.getenv('HOME')
local repo = os.getenv('LUNA_REPO') or '.'

-- A disposable, bootstrapped Steam root with one MostRecent account.
local STEAMID = '76561198000000000'
local ACCOUNT_ID = 39734272
local steam_root = home .. '/.local/share/Steam'
local grid_dir = steam_root .. '/userdata/' .. ACCOUNT_ID .. '/config/grid'
do
  assert(os.execute('mkdir -p ' .. string.format('%q', steam_root .. '/config')))
  local marker = assert(io.open(steam_root .. '/steam.sh', 'wb')); marker:write('#!/bin/sh\n'); marker:close()
  local users = assert(io.open(steam_root .. '/config/loginusers.vdf', 'wb'))
  users:write('"users"\n{\n\t"' .. STEAMID .. '"\n\t{\n\t\t"MostRecent" "1"\n\t}\n}\n')
  users:close()
end

-- Fake SteamGridDB.
local wire = { calls = {}, handles = {}, hang = false, down = false, throw = false }
function wire.get(url, options)
  if wire.throw then error('synthetic network failure') end
  if wire.down then return nil, 'connection refused' end
  table.insert(wire.calls, { url = url, headers = options and options.headers })
  local authorization = options and options.headers and options.headers['Authorization'] or ''
  if url:find('/search/autocomplete/', 1, true) then
    if authorization ~= 'Bearer good-key' then return { status = 401, body = '{"success":false}' } end
    return { status = 200, body = '{"success":true,"data":[{"id":1,"name":"Hades"},{"id":2,"name":"Hades II"}]}' }
  end
  if url:find('dimensions=920x430', 1, true) then
    if authorization ~= 'Bearer good-key' then return { status = 401, body = '{"success":false}' } end
    return { status = 200, body = '{"success":true,"data":[' ..
      '{"id":31,"url":"https://cdn.example/wide1.png","thumb":"https://cdn.example/tw.png",' ..
      '"width":920,"height":430,"mime":"image/png","author":{"name":"artistW"}}]}' }
  end
  if url:find('/heroes/game/1', 1, true) then
    if authorization ~= 'Bearer good-key' then return { status = 401, body = '{"success":false}' } end
    return { status = 200, body = '{"success":true,"data":[' ..
      '{"id":21,"url":"https://cdn.example/hero1.jpg","thumb":"https://cdn.example/th.jpg",' ..
      '"width":1920,"height":620,"mime":"image/jpeg","author":{"name":"artistH"}}]}' }
  end
  if url:find('/logos/game/1', 1, true) then
    if authorization ~= 'Bearer good-key' then return { status = 401, body = '{"success":false}' } end
    return { status = 200, body = '{"success":true,"data":[' ..
      '{"id":41,"url":"https://cdn.example/logo1.png","thumb":"https://cdn.example/tl.png",' ..
      '"width":800,"height":310,"mime":"image/png","author":{"name":"artistL"}}]}' }
  end
  if url:find('/icons/game/1', 1, true) then
    if authorization ~= 'Bearer good-key' then return { status = 401, body = '{"success":false}' } end
    return { status = 200, body = '{"success":true,"data":[' ..
      '{"id":51,"url":"https://cdn.example/icon1.png","thumb":"https://cdn.example/ti.png",' ..
      '"width":256,"height":256,"mime":"image/png","author":{"name":"artistI"}}]}' }
  end
  if url:find('/grids/game/1', 1, true) then
    if authorization ~= 'Bearer good-key' then return { status = 401, body = '{"success":false}' } end
    return { status = 200, body = '{"success":true,"data":[' ..
      '{"id":11,"url":"https://cdn.example/grid1.png","thumb":"https://cdn.example/t1.png",' ..
      '"width":600,"height":900,"style":"alternate","mime":"image/png","author":{"name":"artist1"}},' ..
      '{"id":12,"url":"https://cdn.example/grid2.jpg","thumb":"https://cdn.example/t2.jpg",' ..
      '"width":600,"height":900,"style":"alternate","mime":"image/jpeg","author":{"name":"artist2"}},' ..
      '{"id":13,"url":"https://cdn.example/grid3.webp","width":600,"height":900,"mime":"image/webp"},' ..
      '{"id":14,"url":"https://cdn.example/grid4.png","width":600,"height":900,"mime":"image/png","nsfw":true}' ..
      ']}' }
  end
  if url == 'https://cdn.example/grid1.png' then return { status = 200, body = 'PNG1DATA' } end
  if url == 'https://cdn.example/grid2.jpg' then return { status = 200, body = 'JPG2DATA' } end
  if url == 'https://cdn.example/wide1.png' then return { status = 200, body = 'WIDEDATA' } end
  if url == 'https://cdn.example/hero1.jpg' then return { status = 200, body = 'HERODATA' } end
  if url == 'https://cdn.example/logo1.png' then return { status = 200, body = 'LOGODATA' } end
  if url == 'https://cdn.example/icon1.png' then return { status = 200, body = 'ICONDATA' } end
  return { status = 404, body = '{"success":false}' }
end
-- The async half of the same wire: a handle, and a poll that never waits.
-- `hang` models a dead network (poll never completes), `down` a refused one.
function wire.start(url, options)
  if wire.throw then error('synthetic network failure') end
  table.insert(wire.calls, { url = url, headers = options and options.headers })
  wire.handles[#wire.handles + 1] = { url = url, options = options }
  return { index = #wire.handles }
end
function wire.poll(handle, wait)
  if wire.hang then return false end
  local job = wire.handles[handle.index]
  if not job then return true, nil, 'unknown handle' end
  if wire.down then return true, nil, 'connection refused' end
  return true, wire.get(job.url, job.options)
end
package.preload['http'] = function() return wire end

local backend = dofile(repo .. '/addons/sgdb/plugin/backend/main.lua')
local registry = {}
backend.register(registry)

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

local function file_text(path)
  local f = io.open(path, 'rb')
  if not f then return nil end
  local raw = f:read('*a'); f:close()
  return raw
end

-- ---------------------------------------------------------------- config

local state = call('SlsSgdbGet')
assert(state.success == true and state.hasKey == false and state.apiKey == nil,
  'the key must start unset and never be echoed back')
assert(state.gridDir == grid_dir, 'the grid directory must resolve from loginusers: ' .. tostring(state.gridDir))
assert(state.gridError == '')

local saved = call('SlsSgdbSetKey', { raw = json.encode({ key = 'good-key' }) })
assert(saved.success == true and saved.hasKey == true)
local after = call('SlsSgdbGet')
assert(after.hasKey == true and after.apiKey == nil, 'the full key must never leave the backend')
assert(after.keyMasked == '••••-key', 'the masked hint must show the tail: ' .. tostring(after.keyMasked))

local tested = call('SlsSgdbTest')
assert(tested.success == true, 'a good key must validate: ' .. tostring(tested.error))

call('SlsSgdbSetKey', { raw = json.encode({ key = 'bad-key' }) })
local rejected = call('SlsSgdbTest')
assert(rejected.success == false and rejected.code == 'bad_key', 'a bad key must be reported as such')
local refused = call('SlsSgdbSearch', { raw = json.encode({ term = 'hades' }) })
assert(refused.success == false and refused.code == 'bad_key')
call('SlsSgdbSetKey', { raw = json.encode({ key = 'good-key' }) })

-- ---------------------------------------------------------------- search & assets

local search = call('SlsSgdbSearch', { raw = json.encode({ term = 'hades' }) })
assert(search.success == true and #search.games == 2 and search.games[1].name == 'Hades')
assert(search.games[1].id == 1)

local assets = call('SlsSgdbAssets', { raw = json.encode({ gameId = 1, kind = 'grid' }) })
assert(assets.success == true and #assets.assets == 2, 'webp and nsfw must be filtered out')
assert(assets.assets[1].mime == 'image/png' and assets.assets[1].author == 'artist1')
assert(assets.total == 4)

-- ---------------------------------------------------------------- apply, undo & reset

local applied = call('SlsSgdbApply', { raw = json.encode({ appid = 1145360, kind = 'grid',
  url = 'https://cdn.example/grid1.png', mime = 'image/png' }) })
assert(applied.success == true and applied.backedUp == false, tostring(applied.error))
assert(file_text(grid_dir .. '/1145360p.png') == 'PNG1DATA', 'the image must land in the grid folder')

local replaced = call('SlsSgdbApply', { raw = json.encode({ appid = 1145360, kind = 'grid',
  url = 'https://cdn.example/grid2.jpg', mime = 'image/jpeg' }) })
assert(replaced.success == true and replaced.backedUp == true, 'the previous file must be stashed')
assert(file_text(grid_dir .. '/1145360p.jpg') == 'JPG2DATA')
assert(file_text(grid_dir .. '/1145360p.png') == nil, 'only one extension may remain')
assert(file_text(grid_dir .. '/.sls-luna-history/1145360p/') == nil, 'history is a directory, not a file')

local history = call('SlsSgdbHistory', { raw = json.encode({ appid = 1145360 }) })
assert(history.success == true and history.history.grid == 1, 'one undo step must be recorded')

local undone = call('SlsSgdbUndo', { raw = json.encode({ appid = 1145360, kind = 'grid' }) })
assert(undone.success == true and undone.undone == true, tostring(undone.error))
assert(file_text(grid_dir .. '/1145360p.png') == 'PNG1DATA', 'undo must bring the previous file back')
assert(file_text(grid_dir .. '/1145360p.jpg') == nil)

local empty = call('SlsSgdbUndo', { raw = json.encode({ appid = 1145360, kind = 'wide' }) })
assert(empty.success == false and empty.code == 'empty', 'undo with no history must fail closed')

-- Regression: Undo must restore the previous step even when a second or more
-- separates the change and the undo (it used to stash the current file first
-- and then pop that same file back), and two changes inside one second must
-- not overwrite each other's history entry.
do
  local real_time = os.time
  local clock = real_time()
  os.time = function(t) if t then return real_time(t) end return clock end
  local function apply(url, mime)
    local r = call('SlsSgdbApply', { raw = json.encode({ appid = 400, kind = 'grid', url = url, mime = mime }) })
    assert(r.success == true, tostring(r.error))
  end
  apply('https://cdn.example/grid1.png', 'image/png')   -- step 1: PNG1DATA
  apply('https://cdn.example/wide1.png', 'image/png')   -- step 2: WIDEDATA, same second, same ext
  apply('https://cdn.example/grid2.jpg', 'image/jpeg')  -- step 3: JPG2DATA, same second
  local depth = call('SlsSgdbHistory', { raw = json.encode({ appid = 400 }) })
  assert(depth.history.grid == 2, 'two changes in one second must keep two undo steps, got '
    .. tostring(depth.history.grid))
  clock = clock + 5
  local first = call('SlsSgdbUndo', { raw = json.encode({ appid = 400, kind = 'grid' }) })
  assert(first.success == true, tostring(first.error))
  assert(file_text(grid_dir .. '/400p.png') == 'WIDEDATA',
    'undo after the clock moved must restore the previous step, not the current file')
  assert(file_text(grid_dir .. '/400p.jpg') == nil)
  clock = clock + 5
  local second = call('SlsSgdbUndo', { raw = json.encode({ appid = 400, kind = 'grid' }) })
  assert(second.success == true, tostring(second.error))
  assert(file_text(grid_dir .. '/400p.png') == 'PNG1DATA',
    'a second undo must go one step further back (the same-second entry must not be overwritten)')
  local kept = 0
  local pipe = io.popen("ls -1 '" .. grid_dir .. "/.sls-luna-history/400p/undone' 2>/dev/null")
  for _ in pipe:lines() do kept = kept + 1 end
  pipe:close()
  assert(kept == 2, 'both undone files must be kept outside the undo stack, found ' .. kept)
  local after = call('SlsSgdbHistory', { raw = json.encode({ appid = 400 }) })
  assert(after.history.grid == 0, 'two undos must use up both steps, not count the undone files')
  os.time = real_time
end

-- Undo is transactional: if the current artwork cannot be archived, or the
-- previous artwork cannot be written back, nothing is deleted and the step
-- stays available for a retry.
do
  local function apply(url, mime)
    local r = call('SlsSgdbApply', { raw = json.encode({ appid = 401, kind = 'grid', url = url, mime = mime }) })
    assert(r.success == true, tostring(r.error))
  end
  apply('https://cdn.example/grid1.png', 'image/png')
  apply('https://cdn.example/grid2.jpg', 'image/jpeg')
  local slot_hist = grid_dir .. '/.sls-luna-history/401p'
  -- 1. archive fails: undone/ is a plain file, so nothing can be written in it.
  local blocker = io.open(slot_hist .. '/undone', 'w'); blocker:write('x'); blocker:close()
  local r1 = call('SlsSgdbUndo', { raw = json.encode({ appid = 401, kind = 'grid' }) })
  assert(r1.success == false and r1.code == "io", "a failed archive must refuse the undo: " .. json.encode(r1))
  assert(file_text(grid_dir .. '/401p.jpg') == 'JPG2DATA', 'a failed archive must keep the current artwork')
  assert(call('SlsSgdbHistory', { raw = json.encode({ appid = 401 }) }).history.grid == 1,
    'a failed archive must keep the history step')
  os.remove(slot_hist .. '/undone')
  local function undone_count()
    local n, pipe = 0, io.popen("ls -1 '" .. slot_hist .. "/undone' 2>/dev/null")
    for _ in pipe:lines() do n = n + 1 end
    pipe:close()
    return n
  end
  assert(undone_count() == 0, 'a refused undo must not leave archive copies behind')
  -- 2. restore fails: the grid folder is read-only, so the restored file cannot land.
  -- A read-only directory does not stop root, so this case only runs unprivileged.
  local id = io.popen('id -u'); local uid = tonumber(id:read('*l')); id:close()
  if uid == 0 then
    io.stderr:write('SKIP sgdb restore-failure case: running as root, chmod cannot force the failure\n')
  else
    os.execute("chmod 555 '" .. grid_dir .. "'")
    local r2 = call('SlsSgdbUndo', { raw = json.encode({ appid = 401, kind = 'grid' }) })
    os.execute("chmod 755 '" .. grid_dir .. "'")
    assert(r2.success == false and r2.code == 'io', 'a failed restore must refuse the undo')
    assert(file_text(grid_dir .. '/401p.jpg') == 'JPG2DATA', 'a failed restore must keep the current artwork')
    assert(call('SlsSgdbHistory', { raw = json.encode({ appid = 401 }) }).history.grid == 1,
      'a failed restore must keep the history step for a retry')
    assert(undone_count() == 0, 'a failed restore must remove the archive copy it made')
  end
  -- 3. the retry then works.
  local r3 = call('SlsSgdbUndo', { raw = json.encode({ appid = 401, kind = 'grid' }) })
  assert(r3.success == true, tostring(r3.error))
  assert(file_text(grid_dir .. '/401p.png') == 'PNG1DATA' and file_text(grid_dir .. '/401p.jpg') == nil)
end

-- Reset removes the custom file so Steam shows its own art; history survives.
call('SlsSgdbApply', { raw = json.encode({ appid = 620, kind = 'hero',
  url = 'https://cdn.example/grid1.png', mime = 'image/png' }) })
assert(file_text(grid_dir .. '/620_hero.png') == 'PNG1DATA')
local reset = call('SlsSgdbReset', { raw = json.encode({ appid = 620, kind = 'hero' }) })
assert(reset.success == true and reset.removed == true)
assert(file_text(grid_dir .. '/620_hero.png') == nil)
local reset_again = call('SlsSgdbReset', { raw = json.encode({ appid = 620, kind = 'hero' }) })
assert(reset_again.success == true and reset_again.removed == false)

-- ---------------------------------------------------------------- refusals

local bad_mime = call('SlsSgdbApply', { raw = json.encode({ appid = 1145360, kind = 'grid',
  url = 'https://cdn.example/grid3.webp', mime = 'image/webp' }) })
assert(bad_mime.success == false and bad_mime.code == 'mime')
local bad_appid = call('SlsSgdbApply', { raw = json.encode({ appid = 0, kind = 'grid',
  url = 'https://cdn.example/grid1.png', mime = 'image/png' }) })
assert(bad_appid.success == false and bad_appid.code == 'invalid')
local bad_kind = call('SlsSgdbApply', { raw = json.encode({ appid = 1, kind = 'banner',
  url = 'https://cdn.example/grid1.png', mime = 'image/png' }) })
assert(bad_kind.success == false and bad_kind.code == 'invalid')
local missing = call('SlsSgdbApply', { raw = json.encode({ appid = 1, kind = 'grid',
  url = 'https://cdn.example/nope.png', mime = 'image/png' }) })
assert(missing.success == false and missing.code == 'http')
local no_search = call('SlsSgdbSearch', { raw = json.encode({ term = '   ' }) })
assert(no_search.success == false)

-- ---------------------------------------------------------------- accounts

do
  local users = assert(io.open(steam_root .. '/config/loginusers.vdf', 'wb'))
  users:write('"users"\n{\n\t"' .. STEAMID .. '"\n\t{\n\t}\n\t"76561198000000001"\n\t{\n\t}\n}\n')
  users:close()
  local ambiguous = call('SlsSgdbGet')
  assert(ambiguous.gridDir == '' and ambiguous.gridError:find('ambiguous', 1, true) ~= nil,
    'two accounts without MostRecent must be refused: ' .. tostring(ambiguous.gridError))
  -- The override environment variable resolves it anyway.
  local account, note = backend.resolve_account(steam_root)
  assert(account == nil and note:find('ambiguous', 1, true) ~= nil)
end

assert(backend.kind_suffix('grid') == 'p' and backend.kind_suffix('hero') == '_hero'
  and backend.kind_suffix('logo') == '_logo' and backend.kind_suffix('icon') == '_icon'
  and backend.kind_suffix('wide') == '')
assert(backend.account_id(STEAMID) == ACCOUNT_ID)
assert(backend.account_id('123') == nil)

-- ---------------------------------------------------------------- base64

assert(backend.base64('') == '')
assert(backend.base64('a') == 'YQ==')
assert(backend.base64('ab') == 'YWI=')
assert(backend.base64('abc') == 'YWJj')
assert(backend.base64('PNG1DATA') == 'UE5HMURBVEE=')

-- ---------------------------------------------------------------- wide assets

do
  -- The accounts section below poisons loginusers.vdf on purpose; restore a
  -- single MostRecent account for the artwork sections.
  local users = assert(io.open(steam_root .. '/config/loginusers.vdf', 'wb'))
  users:write('"users"\n{\n\t"' .. STEAMID .. '"\n\t{\n\t\t"MostRecent" "1"\n\t}\n}\n')
  users:close()
end

local wide = call('SlsSgdbAssets', { raw = json.encode({ gameId = 1, kind = 'wide' }) })
assert(wide.success == true and #wide.assets == 1 and wide.assets[1].width == 920
  and wide.assets[1].author == 'artistW', 'the wide grid must use the dimension filter')

-- ---------------------------------------------------------------- auto picker

-- The pass must return at once and do its work on the status polls.
local before_auto = #wire.calls
local started = call('SlsSgdbAuto', { raw = json.encode({ appid = 620, gameId = 1 }) })
assert(started.success == true and started.pending == true and started.state == 'running',
  'Auto must return a running job, not wait for the pass')
assert(#wire.calls - before_auto <= 1, 'Auto must not fire every request before replying')
assert(started.job and tonumber(started.job.id) and started.job.total == 5)
assert(#started.results == 5, 'the page needs one row per slot from the first reply')

local auto, polls = started, 0
while auto.state == 'running' and polls < 40 do
  auto = call('SlsSgdbAutoStatus', { raw = json.encode({ id = started.job.id }) })
  polls = polls + 1
end
assert(auto.state == 'done', 'the auto pass must finish, got ' .. tostring(auto.state))
assert(#auto.results == 5, 'the auto picker must cover five slots')
local by_kind = {}
for _, result in ipairs(auto.results) do by_kind[result.kind] = result end
for _, kind in ipairs({ 'grid', 'wide', 'hero', 'logo', 'icon' }) do
  assert(by_kind[kind] and by_kind[kind].ok == true, 'auto must fill ' .. kind .. ': '
    .. tostring(by_kind[kind] and by_kind[kind].error))
end
assert(by_kind.grid.live == true and by_kind.wide.live == true and by_kind.hero.live == true
  and by_kind.logo.live == true and by_kind.icon.live == false,
  'icons have no live update, the other slots do')
assert(file_text(grid_dir .. '/620p.png') == 'PNG1DATA')
assert(file_text(grid_dir .. '/620.png') == 'WIDEDATA')
assert(file_text(grid_dir .. '/620_hero.jpg') == 'HERODATA')
assert(file_text(grid_dir .. '/620_logo.png') == 'LOGODATA')
assert(file_text(grid_dir .. '/620_icon.png') == 'ICONDATA')

-- ---------------------------------------------------------------- live payload

local live = call('SlsSgdbLive', { raw = json.encode({ appid = 620, kind = 'grid' }) })
assert(live.success == true and live.type == 0 and live.extension == 'png'
  and live.base64 == 'UE5HMURBVEE=', 'the live payload must carry the applied file')
local live_wide = call('SlsSgdbLive', { raw = json.encode({ appid = 620, kind = 'wide' }) })
assert(live_wide.success == true and live_wide.type == 3 and live_wide.extension == 'png')
local live_icon = call('SlsSgdbLive', { raw = json.encode({ appid = 620, kind = 'icon' }) })
assert(live_icon.success == false and live_icon.code == 'no_live')
local live_none = call('SlsSgdbLive', { raw = json.encode({ appid = 1145360, kind = 'logo' }) })
assert(live_none.success == false and live_none.code == 'none')

-- ---------------------------------------------------------------- slow & failing network

-- A dead network: Auto still answers at once and the status poll stays cheap
-- (the request is in flight, so it reports pending rather than waiting).
wire.hang = true
local slow = call('SlsSgdbAuto', { raw = json.encode({ appid = 777, gameId = 1 }) })
assert(slow.success == true and slow.state == 'running' and slow.pending == true,
  'Auto must not wait for a request that never answers')
local slow_poll = call('SlsSgdbAutoStatus', { raw = json.encode({ id = slow.job.id }) })
assert(slow_poll.state == 'running' and slow_poll.pending == true and slow_poll.job.done == 0,
  'a hung request must leave the pass pending, not block or fail it')
wire.hang = false

-- A refused connection: every slot reports its own error and the pass still ends.
wire.down = true
local refused = call('SlsSgdbAuto', { raw = json.encode({ appid = 778, gameId = 1 }) })
local failed, tries = refused, 0
while failed.state == 'running' and tries < 30 do
  failed = call('SlsSgdbAutoStatus', { raw = json.encode({ id = refused.job.id }) })
  tries = tries + 1
end
assert(failed.state == 'done', 'a failing pass must end, not hang')
assert(#failed.results == 5)
for _, result in ipairs(failed.results) do
  assert(result.ok == false and type(result.error) == 'string' and result.error ~= '',
    'every failed slot must say why (got ' .. tostring(result.kind) .. ')')
end
assert(failed.results[1].code == 'unavailable', 'the failure code must reach the page')
wire.down = false

-- A synchronous call that throws is reported, never ignored.
wire.throw = true
local threw = call('SlsSgdbApply', { raw = json.encode({ appid = 1, kind = 'grid',
  url = 'https://cdn.example/grid1.png', mime = 'image/png' }) })
assert(threw.success == false and threw.code == 'unavailable'
  and threw.error:find('request failed', 1, true) ~= nil, 'a throwing http.get must become an error reply')
wire.throw = false

-- Any handler that throws at all answers with the shared error shape.
local real_root = backend.steam_root
backend.steam_root = function() error('synthetic failure') end
local guarded = call('SlsSgdbGet')
backend.steam_root = real_root
assert(guarded.success == false and type(guarded.error) == 'string'
  and guarded.error:find('Backend error', 1, true) ~= nil, 'handlers must be pcall-wrapped')

-- Only https artwork URLs are fetched from the RPC path.
local insecure = call('SlsSgdbApply', { raw = json.encode({ appid = 1, kind = 'grid',
  url = 'http://127.0.0.1/secret.png', mime = 'image/png' }) })
assert(insecure.success == false and insecure.code == 'invalid')

-- Without http.start/http.poll the pass falls back to one bounded request per
-- status poll; Auto still returns at once and no single poll runs the pass.
local real_start, real_poll = wire.start, wire.poll
wire.start, wire.poll = nil, nil
local sync_started = call('SlsSgdbAuto', { raw = json.encode({ appid = 779, gameId = 1 }) })
assert(sync_started.success == true and sync_started.state == 'running'
  and sync_started.job.done == 0, 'the blocking fallback must still start as a job')
local calls_before = #wire.calls
local sync_poll = call('SlsSgdbAutoStatus', { raw = json.encode({ id = sync_started.job.id }) })
assert(#wire.calls - calls_before <= 1, 'one poll must not chain requests: '
  .. tostring(#wire.calls - calls_before))
assert(sync_poll.state == 'running', 'one request per poll cannot finish five slots')
wire.start, wire.poll = real_start, real_poll

-- ---------------------------------------------------------------- durability, honesty, ids

-- The API key is never echoed, not even on an error path: every handler's raw
-- reply is scanned for the fixture secret.
do
  local secret = 'sgdb-smoke-SECRET-0123456789abcdef'
  assert(call('SlsSgdbSetKey', { raw = json.encode({ key = secret }) }).success)
  local get = call('SlsSgdbGet')
  assert(get.keyMasked == '••••cdef' and get.apiKey == nil, 'only the key tail may be shown')
  local probes = {
    { 'SlsSgdbGet' }, { 'SlsSgdbTest' },
    { 'SlsSgdbSearch', json.encode({ term = 'x' }) },
    { 'SlsSgdbAssets', json.encode({ gameId = 1, kind = 'grid' }) },
    { 'SlsSgdbApply', json.encode({ appid = 1, kind = 'grid',
      url = 'http://127.0.0.1/x.png', mime = 'image/png' }) },
    { 'SlsSgdbUndo', json.encode({ appid = 1, kind = 'grid' }) },
    { 'SlsSgdbReset', json.encode({ appid = 1, kind = 'grid' }) },
    { 'SlsSgdbHistory', json.encode({ appid = 1 }) },
    { 'SlsSgdbLive', json.encode({ appid = 1, kind = 'icon' }) },
    { 'SlsSgdbSetKey', json.encode({ key = 42 }) },
  }
  for _, probe in ipairs(probes) do
    local raw = registry[probe[1]](probe[2])
    assert(type(raw) == 'string' and raw:find(secret, 1, true) == nil,
      'the API key leaked from ' .. probe[1])
  end
  assert(call('SlsSgdbSetKey', { raw = json.encode({ key = 'good-key' }) }).success)
end

-- A torn sgdb.json is restored from sgdb.json.bak, key included, with a note.
do
  local path = steam_root and (os.getenv('HOME') .. '/.local/share/slssteam-integrations/sgdb.json')
  assert(path, 'the config path must be known')
  call('SlsSgdbSetKey', { raw = json.encode({ key = 'good-key' }) })
  call('SlsSgdbSetKey', { raw = json.encode({ key = 'good-key' }) })
  assert(file_text(path .. '.bak'), 'the previous good key file must be kept as .bak')
  local f = assert(io.open(path, 'wb')); f:write('{"version":1,"apiKey":"good'); f:close()
  local restored = call('SlsSgdbGet')
  assert(restored.success == true and restored.recovered == true,
    'a torn key file must be reported as recovered')
  assert(restored.hasKey == true and restored.keyMasked == '••••-key', 'the key must come back from .bak')
end

-- Per-slot timestamps of the last change, read from the existing op-log.
local times = call('SlsSgdbHistory', { raw = json.encode({ appid = 1145360 }) })
assert(times.success == true and type(times.times) == 'table'
  and type(times.times.grid) == 'number' and times.times.grid > 0,
  'the last change epoch must be reported per slot: ' .. tostring(times.times.grid))
assert(times.times.wide == nil and times.times.hero == nil,
  'a slot never changed must have no timestamp')

-- A torn op-log is restored from its .bak and still carries the timestamps.
do
  local log_path = grid_dir .. '/.sls-luna-history/op-log.json'
  assert(file_text(log_path .. '.bak'), 'the op-log must keep a .bak')
  local f = assert(io.open(log_path, 'wb')); f:write('[{"at":1,'); f:close()
  local recovered = call('SlsSgdbHistory', { raw = json.encode({ appid = 1145360 }) })
  assert(recovered.success == true and recovered.recovered == true,
    'a torn op-log must be reported as recovered')
  assert(type(recovered.times.grid) == 'number', 'the timestamps must survive the recovery')
end

-- Rate limit, malformed body and network down each have their own code.
do
  local real_get = wire.get
  wire.get = function()
    return { status = 429, body = '{"success":false}', headers = { ['Retry-After'] = '42' } }
  end
  local limited = call('SlsSgdbTest')
  assert(limited.success == false and limited.code == 'rate_limit' and limited.retryAfter == 42
    and limited.error:find('42', 1, true) ~= nil,
    'a rate limit must be structured, with retry-after')
  local limited_assets = call('SlsSgdbAssets', { raw = json.encode({ gameId = 1, kind = 'grid' }) })
  assert(limited_assets.code == 'rate_limit' and limited_assets.retryAfter == 42,
    'the assets call must classify a rate limit the same way')
  wire.get = function() return { status = 200, body = 'not json at all' } end
  local malformed = call('SlsSgdbSearch', { raw = json.encode({ term = 'hades' }) })
  assert(malformed.success == false and malformed.code == 'unexpected'
    and type(malformed.error) == 'string', 'a malformed body must be a structured error')
  wire.get = function() return nil, 'connection refused' end
  local down = call('SlsSgdbSearch', { raw = json.encode({ term = 'hades' }) })
  assert(down.success == false and down.code == 'unavailable' and type(down.error) == 'string',
    'a dead network must be a structured error')
  wire.get = real_get
end

-- Non-Steam shortcuts: the picker hands over the 32-bit AppID (the form
-- Steam's app list and presence.json use). The 64-bit encoding that agent.js
-- builds for Steam API calls resolves to the same 32-bit slot, so Apply, Undo,
-- Reset, History and Live all name the same file for either form.
assert(backend.normalize_appid(3913598990) == 3913598990, 'a 32-bit shortcut id must pass through')
assert(backend.normalize_appid(3913598990 * 2 ^ 32) == 3913598990,
  'the 64-bit shortcut encoding must map to the 32-bit id')
assert(backend.normalize_appid(620) == 620, 'a normal app id must pass through')
assert(backend.normalize_appid(0) == nil and backend.normalize_appid(-1) == nil
  and backend.normalize_appid('x') == nil and backend.normalize_appid(1.5) == nil,
  'invalid ids must still be refused')
local shortcut = call('SlsSgdbApply', { raw = json.encode({ appid = 3913598990, kind = 'grid',
  url = 'https://cdn.example/grid1.png', mime = 'image/png' }) })
assert(shortcut.success == true and shortcut.appid == 3913598990,
  'a 32-bit shortcut id must be accepted: ' .. tostring(shortcut.error))
assert(file_text(grid_dir .. '/3913598990p.png') == 'PNG1DATA',
  'shortcut artwork must land under the 32-bit AppID')
local shortcut_undo = call('SlsSgdbUndo', { raw = json.encode({ appid = 3913598990, kind = 'icon' }) })
assert(shortcut_undo.success == false and shortcut_undo.code == 'empty',
  'Undo must resolve the same slot and fail closed when empty')

print('Luna SteamGridDB backend OK: key gate, search, asset filtering, apply/backup/restore, wide, auto job (async, slow, failing), live, refusals, accounts, .bak recovery, key masking, slot times, rate limit, shortcut ids')
