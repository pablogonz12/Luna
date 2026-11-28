-- SLS Presence backend extension for swwayps/lumen.
-- Runs within the existing Lumen process; no extra network listener.
local json = require('json')
local fs = require('fs')
local millennium = require('millennium')
local M = {}
local HOME = os.getenv('HOME') or ''
local DIR = HOME .. '/.local/share/slssteam-integrations'
local FILE = DIR .. '/presence.json'
local BAK = FILE .. '.bak'
local LOCK = FILE .. '.corrupt'
local corruption_locked = false
local MAX = 4294967295
local function valid_id(v)
  local n = tonumber(v)
  return n and n >= 1 and n <= MAX and n == math.floor(n)
end
-- runningAt is the heartbeat of a `running` entry: the epoch of the last time
-- the backend saw the game's process alive (or the agent reported it started).
-- It lives beside the numeric running map so the wire shape the agent reads is
-- unchanged; without it an entry could outlive its game forever.
local function blank() return {enabled=false, sessionTime=true, discordEnabled=true, discordAppId='', owned={}, running={}, runningAt={}, unknownSince={}, cleanup=0, account=nil, accountSource=nil} end
-- Only the 8-hex FNV digest of an account identity is ever persisted; the raw
-- SteamID or account name must never reach this file or a log line.
local function valid_account(v)
  return type(v)=='string' and v:match('^[0-9a-f]+$')~=nil and #v<=16
end
local function valid_source(v)
  return type(v)=='string' and v:match('^[a-z%-]+$')~=nil and #v>=4 and #v<=16
end
local function read_all(path)
  local f = io.open(path, 'rb')
  if not f then return nil end
  local s = f:read('*a')
  f:close()
  return s
end
local function exists(path)
  local ok,found=pcall(fs.exists,path)
  return ok and found == true
end
-- A torn or hand-edited file must never cost the `owned` ledger: the last good
-- copy is kept as presence.json.bak and restored from there.
local function parse_state(s)
  if type(s) ~= 'string' then return nil end
  local ok,value=pcall(json.decode,s)
  if not ok or type(value)~='table' then return nil end
  if type(value.owned)~='table' or (value.running~=nil and type(value.running)~='table') then return nil end
  local data=blank()
  data.enabled=value.enabled==true
  data.cleanup=tonumber(value.cleanup) or 0
  -- Enrichment: while a game runs, its hidden placeholder gains a session-time
  -- suffix (e.g. "(42m)"). On by default; only false disables it.
  data.sessionTime=value.sessionTime~=false
  data.discordEnabled=value.discordEnabled~=false
  if type(value.discordAppId)=='string' and value.discordAppId:match('^%d+$') and #value.discordAppId<=25 then
    data.discordAppId=value.discordAppId
  end
  if valid_account(value.account) then data.account=value.account end
  if valid_source(value.accountSource) then data.accountSource=value.accountSource end
  for original,shortcut in pairs(type(value.owned)=='table' and value.owned or {}) do
    if valid_id(original) and valid_id(shortcut) and tostring(original)~=tostring(shortcut) then
      data.owned[tostring(original)]=tonumber(shortcut)
    end
  end
  for original,shortcut in pairs(type(value.running)=='table' and value.running or {}) do
    if valid_id(original) and tonumber(shortcut)==data.owned[tostring(original)] then
      data.running[tostring(original)]=tonumber(shortcut)
    end
  end
  for original,at in pairs(type(value.runningAt)=='table' and value.runningAt or {}) do
    local key = tostring(original)
    if data.running[key] ~= nil and tonumber(at) and tonumber(at) > 0 then
      data.runningAt[key] = math.floor(tonumber(at))
    end
  end
  for original,at in pairs(type(value.unknownSince)=='table' and value.unknownSince or {}) do
    local key=tostring(original)
    if data.owned[key] and tonumber(at) and tonumber(at)>0 then data.unknownSince[key]=math.floor(tonumber(at)) end
  end
  return data
end
local function read_file()
  local raw = read_all(FILE)
  local data = parse_state(raw)
  if data then return data, {recovered=false} end
  local backup = read_all(BAK)
  local restored = parse_state(backup)
  if restored then
    -- Heal the live file from the backup on the next read so a torn write does
    -- not keep costing the ledger on every call.
    return restored, {recovered=true, reason='invalid'}
  end
  return blank(), {recovered=false, reason = (raw or backup or exists(FILE) or exists(BAK)) and 'invalid' or 'missing'}
end
local function write_atomic(path, body)
  local tmp=path..'.tmp'
  local f,err=io.open(tmp,'wb')
  if not f then return false,err end
  local written,werr=f:write(body)
  local closed,cerr=f:close()
  if not written or not closed then os.remove(tmp);return false,werr or cerr or 'write failed' end
  local done,why=os.rename(tmp,path)
  if not done then os.remove(tmp);return false,why end
  return true
end
local function quarantine_corruption()
  corruption_locked=true
  fs.create_directories(DIR)
  if not read_all(LOCK) then write_atomic(LOCK,'Ownership ledger requires acknowledgement\n') end
  local function quote(s) return "'"..s:gsub("'", "'\\''").."'" end
  for _,path in ipairs(read_all(LOCK) and {FILE,BAK} or {}) do
    local raw=read_all(path)
    if (raw and not parse_state(raw)) or (not raw and exists(path)) then
      for i=1,10000 do
        local dest=path..'.quarantine.'..os.time()..'.'..i
        if not read_all(dest) and not exists(dest) then
          local ok=os.execute('ln -- '..quote(path)..' '..quote(dest)..' 2>/dev/null')
          if ok==true or ok==0 then os.remove(path) end
          break
        end
      end
    end
  end
end
local function save_file(state)
  if corruption_locked or read_all(LOCK) then return false,'Corrupt ownership ledger requires acknowledgement' end
  local _, status=read_file()
  if status.reason=='invalid' and not status.recovered then
    quarantine_corruption()
    return false,'Corrupt ownership ledger requires acknowledgement'
  end
  fs.create_directories(DIR)
  local ok,body=pcall(json.encode,state)
  if not ok then return false,body end
  -- Keep the previous good copy before replacing it: a crash between the write
  -- and the rename, or a truncation afterwards, still leaves the ledger safe.
  local previous=read_all(FILE)
  if previous and parse_state(previous) then write_atomic(BAK,previous) end
  return write_atomic(FILE,body)
end
local function response(o)
  if o.error=='Corrupt ownership ledger requires acknowledgement' then o.code='state-corrupt' end
  return json.encode(o)
end
local function quarantine_paths()
  local paths={}
  local ok,entries=pcall(fs.list,DIR)
  if ok and type(entries)=='table' then
    for _,entry in ipairs(entries) do
      local name=type(entry)=='table' and entry.name or entry
      if type(name)=='string' and (name:match('^presence%.json%.quarantine%.%d+%.%d+$')
        or name:match('^presence%.json%.bak%.quarantine%.%d+%.%d+$')) then
        paths[#paths+1]=DIR..'/'..name
      end
    end
  end
  table.sort(paths)
  return paths
end
-- A `running` entry must not outlive its game. The agent records the entry when
-- it launches a placeholder and the backend heartbeats it while the game's
-- process is alive; an entry the process table no longer shows is retired after
-- a short grace window. A continuously incomplete scan has a hard ceiling:
-- unreadable unrelated processes would otherwise retain every record forever.
local RUNNING_LIMITS = {grace=120, ttl=6*3600}
local STICKY_LIMIT = 7*24*3600
local function expired_ids(data,now)
  local result={}
  for original,at in pairs(data.unknownSince or {}) do
    if data.owned[original] and RUNNING_LIMITS.ttl>0 and now-at>=RUNNING_LIMITS.ttl and now-at<STICKY_LIMIT then
      result[#result+1]=tonumber(original)
    end
  end
  return result
end
function M.running_limits(opts)
  if type(opts)=='table' then
    for _,key in ipairs({'grace','ttl'}) do
      local value=tonumber(opts[key])
      if value and value>=0 then RUNNING_LIMITS[key]=value end
    end
  end
  return {grace=RUNNING_LIMITS.grace, ttl=RUNNING_LIMITS.ttl}
end
function M.reconcile_running(data, alive, complete, now, opts)
  opts = opts or RUNNING_LIMITS
  now = tonumber(now) or os.time()
  local grace = tonumber(opts.grace) or 120
  local ttl = tonumber(opts.ttl) or 0
  local games = {}
  for _,id in ipairs(type(alive)=='table' and alive or {}) do
    if valid_id(id) then games[tostring(math.floor(tonumber(id)))]=true end
  end
  data.running = data.running or {}
  data.unknownSince = data.unknownSince or {}
  local cleared, refreshed, changed = {}, 0, false
  for original,since in pairs(data.unknownSince) do
    -- A positively observed new launch clears the sticky expiry. An entirely
    -- unreadable new launch waits for a positive match or a complete scan.
    if games[original] or complete==true or now-since>=STICKY_LIMIT or (data.owned and not data.owned[original]) then
      data.unknownSince[original]=nil
      changed=true
    end
  end
  for original in pairs(data.running) do
    local at = tonumber((data.runningAt or {})[original]) or 0
    local alive_now = games[tostring(math.floor(tonumber(original) or 0))]==true
    if (alive_now or complete==true) and data.unknownSince[original] then
      data.unknownSince[original]=nil
      changed=true
    end
    if alive_now then
      data.runningAt = data.runningAt or {}
      -- Heartbeat, but only when it moves: a steady game must not rewrite the
      -- state file on every scan.
      if at==0 or now-at>=30 then data.runningAt[original]=now; refreshed=refreshed+1 end
    elseif complete==true and (at==0 or now-at>=grace) then
      data.running[original]=nil
      if data.runningAt then data.runningAt[original]=nil end
      cleared[#cleared+1]=original
    elseif complete~=true and not alive_now then
      local since=data.unknownSince[original]
      if not since or now<since then
        data.unknownSince[original]=now
        changed=true
      elseif ttl>0 and now-since>=ttl then
        data.running[original]=nil
        data.runningAt[original]=nil
        cleared[#cleared+1]=original
      end
    end
  end
  return {cleared=cleared, refreshed=refreshed, changed=changed}
end
-- Discord honesty: an enabled toggle with a missing or malformed Application ID
-- (or a host with no unix socket) cannot publish anything, so every reply says
-- so instead of leaving a green switch with no effect.
function M.app_id_valid(value)
  return type(value)=='string' and value:match('^%d+$')~=nil and #value>=17 and #value<=20
end
function M.socket_available()
  local ok, socket = pcall(require, 'socket')
  return ok and type(socket)=='table' and type(socket.unix)=='function'
end
function M.discord_status(state, status, socket)
  local enabled = state.discordEnabled ~= false
  local has = type(state.discordAppId)=='string' and state.discordAppId~=''
  local valid = M.app_id_valid(state.discordAppId)
  local reason
  if not enabled then reason='disabled'
  elseif not has then reason='no-app-id'
  elseif not valid then reason='invalid-app-id'
  elseif socket ~= true then reason='no-socket'
  elseif status=='connected' then reason='ok'
  elseif status=='no_discord' or status==nil then reason='no-discord'
  else reason='idle' end
  return {enabled=enabled, hasAppId=has, appIdValid=valid, publishing=reason=='ok', reason=reason}
end
-- A windowless RunGame shortcut still becomes Big Picture's active game. In
-- Gamescope this leaves its loading screen selected instead of the real game.
-- Require a verified selection-restoration capability in this process. The
-- agent keeps the helper but puts the real game first; only unsupported builds
-- pause helpers, without overwriting the user's setting.
local function helper_policy(getenv)
  getenv = getenv or os.getenv
  local display = getenv('GAMESCOPE_WAYLAND_DISPLAY') or ''
  local limiter = getenv('GAMESCOPE_LIMITER_FILE') or ''
  local desktop = (getenv('XDG_CURRENT_DESKTOP') or ''):lower()
  local gamescope = display ~= '' or limiter ~= '' or desktop:find('gamescope', 1, true) ~= nil
  return {helpersAllowed=true, requiresGameSelection=gamescope,
    mode=gamescope and 'gamescope' or 'desktop', reason=''}
end
-- Which Steam-launched processes are alive right now.
--
-- Steam's reaper tags every process it starts with `AppId=<id>`, so a scan of
-- /proc gives the two facts a recreated SharedJSContext cannot otherwise recover:
-- which manifest games are still running, and which of our helper shortcuts are
-- still alive. Pure input functions are injected so tests never read /proc.
local MAX_PIDS = 8192
local MAX_CMDLINE = 256 * 1024
local function proc_stat(raw)
  if type(raw)~='string' then return nil end
  -- comm is parenthesized and can itself contain spaces or parentheses.
  local state,rest=raw:match('^%d+ %(.+%) ([A-Za-z]) (.*)$')
  if not state then return nil end
  local fields={}
  for field in rest:gmatch('%S+') do fields[#fields+1]=field end
  local start=fields[19] -- field 22, after pid, comm and state
  if not start or not start:match('^%d+$') then return nil end
  return state,start
end
local function read_proc(path)
  local f,err,code=io.open(path,'rb')
  if not f then return nil,err,code end
  -- One extra byte detects truncation, including for the proc pseudo-files.
  local data,read_error,read_code=f:read(MAX_CMDLINE+1)
  f:close()
  if data==nil and read_error==nil then return '' end
  return data,read_error,read_code
end
local function vanished(err,code)
  return code==2 or code==3 or (type(err)=='string' and
    (err:find('No such file or directory',1,true) ~= nil or err:find('No such process',1,true) ~= nil))
end
local function collect_running(list_dir, read_file, previous)
  local games, helpers = {}, {}
  local identities = {}
  local seen_games, seen_helpers, seen_pids = {}, {}, {}
  local ok, entries = pcall(list_dir, '/proc')
  if not ok or type(entries) ~= 'table' then return games, helpers, false, nil, true end
  local scanned, complete = 0, true
  for _, entry in ipairs(entries) do
    local name = type(entry)=='table' and entry.name or (type(entry)=='string' and entry or nil)
    if type(name)=='string' and name:match('^%d+$') and not seen_pids[name] then
      if scanned >= MAX_PIDS then return games, helpers, false end
      seen_pids[name] = true
      scanned = scanned + 1
      local base='/proc/'..name..'/'
      local ok_stat, stat, stat_error, stat_code = pcall(read_file, base..'stat')
      local state,start = proc_stat(ok_stat and stat)
      if start then identities[name]=start end
      if not state then
        if not (ok_stat and stat==nil and vanished(stat_error,stat_code)) then complete=false end
      elseif previous and previous[name] and previous[name]~=start then
        -- A formerly observed PID now names another process. Do not attribute
        -- its cmdline to the old process; the next scan can classify the new one.
        complete=false
      elseif state~='Z' and state~='X' then
        local ok_read, raw, read_error, read_code = pcall(read_file, base..'cmdline')
        local ok_after, after, after_error, after_code = pcall(read_file, base..'stat')
        local state_after,start_after=proc_stat(ok_after and after)
        if state_after and start~=start_after then
          complete=false
        elseif (ok_read and raw==nil and vanished(read_error,read_code)) or
           (ok_after and after==nil and vanished(after_error,after_code)) then
          identities[name]=nil
        elseif not ok_read or type(raw)~='string' or not state_after or start~=start_after then
          complete=false
        elseif state_after~='Z' and state_after~='X' then
          if #raw>MAX_CMDLINE then
            raw=raw:sub(1,MAX_CMDLINE)
            -- A later token could contain an AppId or the helper command.
            complete=false
          end
          -- Match a complete argv token, not FakeAppId= or AppId=123suffix.
          local id = raw:match('^AppId=(%d+)%z') or raw:match('%zAppId=(%d+)%z')
          if id then
            local value = tonumber(id)
            if value and value >= 1 and value <= MAX then
              if not seen_games[value] then
                games[#games + 1] = value
                seen_games[value] = true
              end
              if raw:find('\0/usr/bin/sleep\00086400\0', 1, true) and not seen_helpers[value] then
                helpers[#helpers + 1] = value
                seen_helpers[value] = true
              end
            end
          end
        end
      end
    end
  end
  return games, helpers, complete, identities
end
local function process_snapshot(list_dir, reader)
  local games,helpers,complete,_,failed=collect_running(list_dir,reader)
  return {success=not failed,complete=complete,games=games,helpers=helpers,scannedAt=os.time()}
end
-- FNV-1a/32, byte-identical to the JS implementation in plugin/public/agent.js
-- (verified by tests/probe-roundtrip assertions). Used to reduce a SteamID64 to
-- an account-scope fingerprint so the raw id never reaches disk or a log.
local function fnv1a(text)
  local h = 0x811c9dc5
  for i = 1, #text do
    h = (h ~ text:byte(i)) & 0xffffffff
    h = (h * 0x01000193) & 0xffffffff
  end
  return string.format('%08x', h)
end
-- Parse the account blocks out of Steam's config/loginusers.vdf. The file is a
-- plain keyvalues document; each account is a "<steamid64>" { ... } block. This
-- is the same source Lumen's own settings tab uses, and it is the only reliable
-- account signal on a client whose SharedJSContext exposes no SteamID.
local function login_users(text)
  local users = {}
  if type(text) ~= 'string' then return users end
  local pos = 1
  while true do
    local s, e, id = text:find('"(%d%d%d%d%d%d%d%d%d%d%d%d%d%d%d%d%d+)"%s*{', pos)
    if not s then break end
    local depth, i = 1, e + 1
    while depth > 0 and i <= #text do
      local ch = text:sub(i, i)
      if ch == '{' then depth = depth + 1
      elseif ch == '}' then depth = depth - 1 end
      i = i + 1
    end
    local body = text:sub(e + 1, i - 2)
    users[#users + 1] = {
      id = id,
      most_recent = body:match('"MostRecent"%s*"([01])"') == '1',
      timestamp = tonumber(body:match('"Timestamp"%s*"(%d+)"')) or 0,
      account_name = body:match('"AccountName"%s*"([^"]*)"'),
    }
    pos = i
  end
  return users
end
local identity_cache = {at = 0, value = nil}local function identity()
  local now = os.time()
  if now - identity_cache.at < 60 then return identity_cache.value end
  local value = nil
  local root = millennium.steam_path()
  local path = root ~= '' and fs.join(root, 'config', 'loginusers.vdf') or ''
  local text = nil
  if path ~= '' and fs.exists(path) then
    local f = io.open(path, 'rb')
    if f then text = f:read('*a'); f:close() end
  end
  local users = login_users(text)
  if #users > 0 then
    local chosen, ambiguous = users[1], false
    if #users > 1 then
      local most = nil
      for _, user in ipairs(users) do
        if user.most_recent then most = user end
      end
      if most then chosen = most
      else
        -- No MostRecent flag: the newest Timestamp is the best available guess,
        -- but the scope is no longer certain, so it is reported as ambiguous and
        -- the guard stays advisory rather than locking the ledger.
        ambiguous = true
        for _, user in ipairs(users) do
          if user.timestamp > chosen.timestamp then chosen = user end
        end
      end
    end
    value = {
      key = fnv1a('steamid:' .. chosen.id),
      source = 'loginusers',
      known = #users,
      ambiguous = ambiguous,
    }
  end
  identity_cache = {at = now, value = value}
  return value
end
-- Discord Rich Presence (pure helpers; no socket is opened here).
-- Discord names an undetected game after its executable ("megabonk.x86_64").
-- A SET_ACTIVITY over discord-ipc-N needs a registered Discord application ID
-- (client_id); its application name is what Discord displays. Without one nothing
-- can be sent, so the RPC below only plans the payload.
local function acf_name(text)
  if type(text) ~= 'string' then return nil end
  local name = text:match('"name"%s*"([^"]+)"')
  if name then name = name:gsub('\\(.)', '%1') end
  return name
end
-- Name for a Steam app: add-on titles map first, then appmanifest_<id>.acf in
-- each library dir. read_file(path) is injected so tests never touch disk.
local function resolve_game_name(appid, titles, dirs, read)
  if not valid_id(appid) then return nil end
  local id = tostring(math.floor(tonumber(appid)))
  if type(titles) == 'table' and type(titles[id]) == 'string' and #titles[id] > 0 then return titles[id] end
  for _, dir in ipairs(dirs or {}) do
    local ok, text = pcall(read, dir .. '/appmanifest_' .. id .. '.acf')
    local name = ok and acf_name(text)
    if name and #name > 0 then return name end
  end
  return nil
end
local function discord_socket_paths(getenv)
  getenv = getenv or os.getenv
  local base = getenv('XDG_RUNTIME_DIR') or ''
  local out = {}
  if base == '' then return out end
  for n = 0, 9 do
    for _, sub in ipairs({'', 'app/com.discordapp.Discord/', 'snap.discord/', '.flatpak/com.discordapp.Discord/xdg-run/'}) do
      out[#out + 1] = base .. '/' .. sub .. 'discord-ipc-' .. n
    end
  end
  return out
end
-- IPC frame: int32le opcode, int32le length, UTF-8 JSON. Opcode 0 = handshake, 1 = frame.
local function discord_frame(op, body) return string.pack('<I4I4', op, #body) .. body end
local function discord_handshake(client_id)
  return discord_frame(0, json.encode({v = 1, client_id = tostring(client_id)}))
end
-- activity = nil clears the activity (send when the game stops).
local function discord_set_activity(pid, name, started, nonce)
  local activity = nil
  if name then
    activity = {name = name, details = name, type = 0, timestamps = {start = started}}
  end
  return discord_frame(1, json.encode({cmd = 'SET_ACTIVITY', nonce = tostring(nonce),
    args = {pid = pid, activity = activity}}))
end

-- Discord Rich Presence session manager. The transport is injected (a
-- luasocket-like unix client), so tests run against a fake. step() is called from
-- the host's plugin tick; it never raises. Status: needs_app_id | no_discord |
-- idle | connected.
local RETRY_SECONDS = 15
local function new_discord_session(deps)
  local S = {conn = nil, status = 'idle', appid = nil, name = nil, started = nil,
    sent = nil, next_try = 0, nonce = 0, path = nil}
  local function close()
    if S.conn then pcall(S.conn.close, S.conn) end
    S.conn, S.sent, S.path = nil, nil, nil
  end
  local function send(frame)
    if not S.conn then return false end
    local ok, n = pcall(S.conn.send, S.conn, frame)
    if not ok or not n then close(); return false end
    return true
  end
  -- Reads one frame; nil, err on timeout or a dead connection.
  local function read_frame(timeout)
    local c = S.conn
    c:settimeout(timeout)
    local h, err = c:receive(8)
    if not h or #h < 8 then return nil, err or 'short frame' end
    local op, len = string.unpack('<I4I4', h)
    if len > 65536 then return nil, 'oversized' end
    local body = ''
    if len > 0 then
      c:settimeout(1)
      body, err = c:receive(len)
      if not body then return nil, err end
    end
    return op, body
  end
  local function try_connect(app_id)
    for _, path in ipairs(deps.paths()) do
      local c = deps.unix()
      if not c then return false end
      c:settimeout(0.5)
      local ok = pcall(function() return c:connect(path) end) and c:getfd() >= 0
      if ok then
        S.conn, S.path = c, path
        if send(discord_handshake(app_id)) then
          local op, body = read_frame(2)
          local okj, msg = pcall(json.decode, body or '')
          if op == 1 and okj and type(msg) == 'table' and msg.evt == 'READY' then return true end
        end
        close()
      else
        pcall(c.close, c)
      end
    end
    return false
  end
  local function pump()
    while S.conn do
      local op, body = read_frame(0)
      if not op then
        if body ~= 'timeout' then close() end
        return
      end
      if op == 3 then send(discord_frame(4, body))
      elseif op == 2 then close() end
    end
  end
  local function seconds()
    local ok, socket = pcall(require, 'socket')
    if ok and type(socket) == 'table' and type(socket.gettime) == 'function' then return socket.gettime() end
    return os.time()
  end
  -- Wait for the SET_ACTIVITY reply with this nonce. Control frames may come
  -- first (PING is answered like pump() does), and so may unrelated dispatches;
  -- only a matching ERROR, CLOSE, a read failure or the deadline give up.
  local function await_ack(nonce, budget)
    local deadline = seconds() + budget
    for _ = 1, 16 do
      local remaining = deadline - seconds()
      if remaining <= 0 then return false end
      local op, body = read_frame(remaining)
      if not op then return false end
      if op == 3 then send(discord_frame(4, body))
      elseif op == 2 then return false
      elseif op == 1 then
        local okj, msg = pcall(json.decode, body or '')
        if okj and type(msg) == 'table' and msg.cmd == 'SET_ACTIVITY' and tostring(msg.nonce) == tostring(nonce) then
          return msg.evt ~= 'ERROR'
        end
      end
    end
    return false
  end
  local function clear()
    if S.conn and S.sent then
      S.nonce = S.nonce + 1
      send(discord_set_activity(deps.pid(), nil, nil, S.nonce))
    end
    close()
  end
  -- settings = {enabled, app_id}; game = {appid, name} or nil. Returns status.
  function S.step(settings, game, now)
    local ok, err = pcall(function()
      if type(settings.app_id) ~= 'string' or not settings.app_id:match('^%d+$') then
        clear(); S.status = 'needs_app_id'; S.appid, S.name = nil, nil; return
      end
      if not settings.enabled or not game or not game.name then
        clear(); S.status = 'idle'; S.appid, S.name = game and game.appid or nil, game and game.name or nil
        S.started = nil; return
      end
      if S.appid ~= game.appid then S.started = now end
      S.appid, S.name = game.appid, game.name
      if S.conn then pump() end
      if S.conn and S.sent and S.sent ~= game.appid then clear() end
      if not S.conn then
        if now < S.next_try then S.status = 'no_discord'; return end
        if not deps.unix or not deps.unix() then S.status = 'no_discord'; return end
        if not try_connect(settings.app_id) then
          S.next_try = now + RETRY_SECONDS
          S.status = 'no_discord'; return
        end
      end
      if S.sent ~= game.appid then
        S.nonce = S.nonce + 1
        if send(discord_set_activity(deps.pid(), game.name, S.started, S.nonce)) then
          if await_ack(S.nonce, 0.5) then
            S.sent = game.appid
          else
            close(); S.next_try = now + RETRY_SECONDS
          end
        else
          S.next_try = now + RETRY_SECONDS
        end
      end
      S.status = S.conn and S.sent and 'connected' or 'no_discord'
    end)
    if not ok then close(); S.status = 'no_discord'; S.error = tostring(err) end
    return S.status
  end
  function S.shutdown() pcall(clear) end
  return S
end
local function proc_pid()
  local f = io.open('/proc/self/stat', 'rb')
  local line = f and f:read('*l'); if f then f:close() end
  return tonumber(line and line:match('^(%d+)')) or 0
end
local function default_unix()
  local ok, socket = pcall(require, 'socket')
  if ok and type(socket) == 'table' and type(socket.unix) == 'function' then return socket.unix() end
  return nil
end
-- Steam's text KeyValues (libraryfolders.vdf, appmanifest_*.acf). Only
-- quoted keys with string or object values are needed; junk degrades to {}.
local function kv_parse(text)
  local pos, length = 1, #text
  local function skip()
    while pos <= length do
      local ch = text:sub(pos, pos)
      if ch == ' ' or ch == '\t' or ch == '\r' or ch == '\n' then pos = pos + 1
      elseif ch == '/' and text:sub(pos + 1, pos + 1) == '/' then
        local nl = text:find('\n', pos, true)
        pos = nl and nl + 1 or length + 1
      else return end
    end
  end
  local function read_string()
    local out = {}
    pos = pos + 1
    while pos <= length do
      local ch = text:sub(pos, pos)
      if ch == '\\' then out[#out + 1] = text:sub(pos + 1, pos + 1); pos = pos + 2
      elseif ch == '"' then pos = pos + 1; return table.concat(out)
      else out[#out + 1] = ch; pos = pos + 1 end
    end
    return table.concat(out)
  end
  local parse_object
  parse_object = function()
    local out = {}
    while true do
      skip()
      if pos > length then return out end
      if text:sub(pos, pos) == '}' then pos = pos + 1; return out end
      if text:sub(pos, pos) == '"' then
        local key = read_string()
        skip()
        local nextch = text:sub(pos, pos)
        if nextch == '{' then pos = pos + 1; out[key] = parse_object()
        elseif nextch == '"' then out[key] = read_string()
        else out[key] = '' end
      else pos = pos + 1 end
    end
  end
  local ok, out = pcall(parse_object)
  return ok and type(out) == 'table' and out or {}
end
-- The installed Steam library, from Steam's own files: every library root in
-- steamapps/libraryfolders.vdf plus the Steam root, then one AppID per
-- steamapps/appmanifest_<appid>.acf. A missing or corrupt file is reported as
-- an `error` note next to the ids that could be read, never thrown.
local function library_apps(root)
  local apps, err, seen = {}, nil, {}
  if type(root) ~= 'string' or root == '' then return apps, 'Steam root not found' end
  local libraries = { root }
  seen[root] = true
  local folders = nil
  for _, rel in ipairs({ '/steamapps/libraryfolders.vdf', '/config/libraryfolders.vdf' }) do
    local f = io.open(root .. rel, 'rb')
    if f then folders = f:read('*a'); f:close(); break end
  end
  if folders then
    local bucket = kv_parse(folders)
    bucket = type(bucket.libraryfolders) == 'table' and bucket.libraryfolders
      or (type(bucket.LibraryFolders) == 'table' and bucket.LibraryFolders or nil)
    if bucket then
      for _, value in pairs(bucket) do
        local path = type(value) == 'table' and tostring(value.path or '') or ''
        if path ~= '' and not seen[path] then seen[path] = true; libraries[#libraries + 1] = path end
      end
    else
      err = 'libraryfolders.vdf could not be parsed'
    end
  end
  for _, library in ipairs(libraries) do
    local steamapps = library:gsub('/+$', '') .. '/steamapps'
    local ok, items = pcall(fs.list, steamapps)
    if ok and type(items) == 'table' then
      for _, entry in ipairs(items) do
        if not entry.is_directory then
          local id = tostring(entry.name):match('^appmanifest_(%d+)%.acf$')
          if valid_id(id) and not seen['#' .. id] then
            seen['#' .. id] = true
            apps[#apps + 1] = tonumber(id)
          end
        end
      end
    elseif library == root then
      err = err or ('steamapps directory could not be listed under ' .. library)
    end
  end
  table.sort(apps)
  return apps, err
end
M.library_apps = library_apps
local manifest_cache={at=0,apps={},error=''}
local function manifests()
  local now=os.time()
  if now-manifest_cache.at<30 then return manifest_cache.apps end
  local apps,err=library_apps(millennium.steam_path())
  manifest_cache={at=now,apps=apps,error=err or ''}
  return apps
end
local live = {}
local expired_running = 0
local scan_identities = {}
-- The games the process table currently shows, minus our own helper shortcuts.
function M.scan_games()
  local games, helpers, complete, identities = collect_running(fs.list, read_proc, scan_identities)
  if identities then scan_identities=identities end
  local skip = {}
  for _, id in ipairs(helpers) do skip[id] = true end
  local out = {}
  for _, id in ipairs(games) do if not skip[id] then out[#out + 1] = id end end
  return out, complete
end
function M.register(registry)
  -- Start-time recovery: a torn file is healed from .bak, and every entry the
  -- file still calls running is reconciled with the processes that exist now,
  -- so an entry whose game (and agent) is long gone cannot survive a restart.
  local state,loaded=read_file()
  corruption_locked = read_all(LOCK) ~= nil or (loaded.reason=='invalid' and not loaded.recovered)
  if corruption_locked then
    -- Persist the lock before moving anything. A hard link atomically reserves
    -- each quarantine name without ever replacing an earlier quarantine.
    quarantine_corruption()
  end
  if loaded.recovered then save_file(state) end
  do
    local alive,complete=nil,false
    if next(state.running)~=nil then alive,complete=M.scan_games() end
    if alive then
      local change=M.reconcile_running(state,alive,complete,os.time())
      expired_running=expired_running+#change.cleared
      if #change.cleared>0 or change.refreshed>0 or change.changed then save_file(state) end
    end
  end
  local runtime={active=0,healthy=false,error='',activeIds={},reportedAt=0,
    owned=0,running=0,accountMismatch=false,identityAmbiguous=false,accountSource='',
    reconciledActivate=0,reconciledOrphaned=0,hideStrategy='',hideVerified=false,contracts={},
    helpersPaused=false,helperPauseReason='',restoredSelections=0,
    expiredRunning=0,recovered=loaded.recovered==true}
  local titles={}
  registry.SlsPresenceGet=function()
    local apps=manifests()
    runtime.expiredRunning=expired_running
    return response({success=true, state=state, apps=apps, libraryError=manifest_cache.error,
      runtime=runtime, recovered=loaded.recovered==true, expired=expired_ids(state,os.time()),
      recoveredReason=loaded.reason or '',
      corrupt=corruption_locked, corruptionRequiresAcknowledgement=corruption_locked,
      quarantineDirectory=DIR, quarantinePaths=quarantine_paths(),
      discord=M.discord_status(state,live.session and live.session.status,M.socket_available()),
      titles=titles, identity=identity(), session=helper_policy()})
  end
  registry.SlsPresenceAcknowledgeCorruption=function(raw)
    local ok,obj=pcall(json.decode,raw or '')
    if not ok or type(obj)~='table' or obj.acknowledge~=true then
      return response({success=false,error='Explicit acknowledgement required'})
    end
    if not corruption_locked then return response({success=true}) end
    quarantine_corruption()
    for _,path in ipairs({FILE,BAK}) do
      local content=read_all(path)
      if (content and not parse_state(content)) or (not content and exists(path)) then
        return response({success=false,error='Quarantine failed; damaged files remain'})
      end
    end
    if read_all(LOCK) then
      local removed,err=os.remove(LOCK)
      if not removed then return response({success=false,error=err}) end
    end
    state=read_file()
    corruption_locked=false
    return response({success=true})
  end
  -- Read-only contract probe: proves the wire shape Lumen actually delivers
  -- (alphabetical-key positional args, one serialized string) without changing
  -- any state. Used by scripts/probe-live-apis.js and the report.
  registry.SlsPresenceContractProbe=function(raw)
    local ok,obj=pcall(json.decode,raw or '')
    return response({success=true, argType=type(raw), argLength=type(raw)=='string' and #raw or -1,
      decoded=(ok and type(obj)=='table')==true})
  end
  -- Read-only process snapshot. Used at boot and every five seconds to recover state that a
  -- recreated SharedJSContext lost: which manifest games are actually running and
  -- which of our helpers are still alive.
  registry.SlsPresenceProbeProcesses=function()
    return response(process_snapshot(fs.list, read_proc))
  end
  -- Read-only plan for a Discord activity: resolved name and socket candidates.
  -- Reports needsClientId because no Discord application ID is configured.
  registry.SlsPresenceDiscordPlan=function(raw)
    local ok,obj=pcall(json.decode,raw or '')
    if not ok or type(obj)~='table' then return response({success=false,error='Invalid payload'}) end
    local root=millennium.steam_path()
    local dirs=root~='' and {fs.join(root,'steamapps')} or {}
    local name=resolve_game_name(obj.appid,titles,dirs,function(path)
      local f=io.open(path,'rb'); if not f then return nil end
      local s=f:read('*a'); f:close(); return s
    end)
    local cid=type(obj.clientId)=='string' and obj.clientId:match('^%d+$') or nil
    return response({success=name~=nil,name=name,needsClientId=cid==nil,
      status=state.discordAppId=='' and 'needs_app_id' or (live.session and live.session.status or 'idle'),
      sockets=discord_socket_paths()})
  end
  -- Discord session: driven by M.tick (host plugin tick, ~5 s). Inert when the
  -- host has no tick or socket.unix.
  local session = new_discord_session({unix=default_unix, paths=function() return discord_socket_paths() end, pid=proc_pid})
  local function read_text(path)
    local f=io.open(path,'rb'); if not f then return nil end
    local data=f:read('*a'); f:close(); return data
  end
  local function current_game()
    local games,helpers=collect_running(fs.list,read_proc)
    local skip={}
    for _,id in ipairs(helpers) do skip[id]=true end
    local appid
    for _,id in ipairs(games) do
      if not skip[id] and (appid==nil or id==session.appid) then appid=id end
    end
    if not appid and runtime.reportedAt and os.time()-runtime.reportedAt<=20 then
      appid=runtime.activeIds[1]
    end
    if not appid then return nil end
    local root=millennium.steam_path()
    local dirs=root~='' and {fs.join(root,'steamapps')} or {}
    return {appid=appid,name=resolve_game_name(appid,titles,dirs,read_text)}
  end
  local HEARTBEAT_SECONDS=30
  local last_heartbeat=0
  live.tick=function(now)
    session.step({enabled=state.discordEnabled~=false,app_id=state.discordAppId},current_game(),now)
    -- Running-entry heartbeat: while a game is alive its epoch moves, and one
    -- the process table no longer shows is retired after the grace window.
    if now-last_heartbeat<HEARTBEAT_SECONDS then return end
    last_heartbeat=now
    if next(state.running)==nil and next(state.unknownSince)==nil then return end
    local alive,complete=M.scan_games()
    local change=M.reconcile_running(state,alive,complete,now)
    if #change.cleared>0 or change.refreshed>0 or change.changed then
      save_file(state)
      expired_running=expired_running+#change.cleared
    end
  end
  live.shutdown=function() session.shutdown() end
  live.session=session
  registry.SlsPresenceDiscordStatus=function()
    local game=current_game()
    local socket=M.socket_available()
    return response({success=true,status=session.status,name=game and game.name or session.name,
      appid=game and game.appid or nil,enabled=state.discordEnabled~=false,hasAppId=state.discordAppId~='',
      appIdValid=M.app_id_valid(state.discordAppId),socketAvailable=socket,
      discord=M.discord_status(state,session.status,socket)})
  end
  registry.SlsPresenceSetDiscord=function(raw)
    if type(raw)~='string' or #raw>500 then return response({success=false,error='Invalid payload'}) end
    local ok,obj=pcall(json.decode,raw)
    if not ok or type(obj)~='table' then return response({success=false,error='Invalid payload'}) end
    local updated=read_file()
    if obj.enabled~=nil then
      if type(obj.enabled)~='boolean' then return response({success=false,error='Expected boolean'}) end
      updated.discordEnabled=obj.enabled
    end
    if obj.appId~=nil then
      if type(obj.appId)~='string' then
        return response({success=false,code='invalid-app-id',error='Application ID must be 17-20 digits.'})
      end
      if obj.appId~='' and not M.app_id_valid(obj.appId) then
        return response({success=false,code='invalid-app-id',
          error='Application ID must be 17-20 digits.',
          discord=M.discord_status(state,live.session and live.session.status,M.socket_available())})
      end
      updated.discordAppId=obj.appId
    end
    local saved,err=save_file(updated)
    if saved then state=updated end
    return response({success=saved,error=err,discordEnabled=state.discordEnabled,discordAppId=state.discordAppId,
      discord=M.discord_status(state,live.session and live.session.status,M.socket_available())})
  end
  registry.SlsPresencePut=function(raw)
    if type(raw)~='string' or #raw>1000000 then return response({success=false,error='Invalid payload'}) end
    local ok,obj=pcall(json.decode,raw)
    if not ok or type(obj)~='table' or type(obj.owned)~='table' or type(obj.running)~='table' then
      return response({success=false,error='Invalid presence state'})
    end
    local next_state=blank()
    next_state.enabled=state.enabled -- UI owns enabled setting, not agent
    next_state.cleanup=state.cleanup
    next_state.sessionTime=state.sessionTime -- UI owns the enrichment toggle too
    next_state.discordEnabled=state.discordEnabled
    next_state.discordAppId=state.discordAppId
    next_state.account=valid_account(obj.account) and obj.account or state.account
    next_state.accountSource=valid_source(obj.accountSource) and obj.accountSource or state.accountSource
    local count=0
    local usedShortcuts={}
    for original,shortcut in pairs(obj.owned) do
      count=count+1
      if count>4096 or not valid_id(original) or not valid_id(shortcut) or tostring(original)==tostring(shortcut) then
        return response({success=false,error='Invalid shortcut mapping'})
      end
      local sid=tostring(math.floor(tonumber(shortcut)))
      if usedShortcuts[sid] then
        return response({success=false,error='A shortcut cannot belong to two original games'})
      end
      usedShortcuts[sid]=true
      next_state.owned[tostring(original)]=tonumber(shortcut)
    end
    local now=os.time()
    for original,since in pairs(state.unknownSince) do
      if next_state.owned[original] and now-since<STICKY_LIMIT then next_state.unknownSince[original]=since end
    end
    local expired=expired_ids(next_state,now)
    local blocked={}
    for _,original in ipairs(expired) do blocked[tostring(original)]=true end
    for original,shortcut in pairs(obj.running) do
      if tonumber(shortcut)~=next_state.owned[tostring(original)] then
        return response({success=false,error='Invalid active shortcut'})
      end
      if not blocked[tostring(original)] then
        next_state.running[tostring(original)]=tonumber(shortcut)
        -- The agent has just seen this game running: that is the heartbeat.
        next_state.runningAt[tostring(original)]=now
      end
    end
    local saved,err=save_file(next_state)
    if saved then state=next_state end
    return response({success=saved,error=err,expired=expired})
  end
  registry.SlsPresenceSetEnabled=function(value)
    if type(value)~='boolean' then return response({success=false,error='Expected boolean'}) end
    local updated=read_file();updated.enabled=value
    local ok,err=save_file(updated)
    if ok then state=updated end
    return response({success=ok,error=err,enabled=state.enabled})
  end
  -- Enrichment toggle: when on, a running placeholder's name gains a session
  -- time suffix like "(42m)" and is restored to the plain title on stop.
  registry.SlsPresenceSetSessionTime=function(value)
    if type(value)~='boolean' then return response({success=false,error='Expected boolean'}) end
    local updated=read_file();updated.sessionTime=value
    local saved,err=save_file(updated)
    if saved then state=updated end
    return response({success=saved,error=err,sessionTime=state.sessionTime})
  end
  registry.SlsPresenceRequestCleanup=function()
    local updated=read_file();updated.enabled=false;updated.cleanup=updated.cleanup+1
    local ok,err=save_file(updated)
    if ok then state=updated end
    return response({success=ok,error=err,cleanup=state.cleanup})
  end
  registry.SlsPresenceAcknowledgeCleanup=function()
    local updated=read_file();updated.cleanup=0
    local ok,err=save_file(updated)
    if ok then state=updated end
    return response({success=ok,error=err})
  end
  registry.SlsPresenceSetTitles=function(raw)
    if type(raw)~='string' or #raw>3000 then return response({success=false,error='Invalid titles'}) end
    local ok,obj=pcall(json.decode,raw)
    if not ok or type(obj)~='table' then return response({success=false,error='Invalid titles'}) end
    for id,title in pairs(obj) do
      if valid_id(id) and type(title)=='string' and #title>0 and #title<=160 then
        titles[tostring(id)]=title
      end
    end
    return response({success=true})
  end
  registry.SlsPresenceReport=function(raw)
    if type(raw)~='string' or #raw>4000 then return response({success=false}) end
    local ok,obj=pcall(json.decode,raw)
    if ok and type(obj)=='table' then
      runtime={active=tonumber(obj.active) or 0,healthy=obj.healthy==true,error=tostring(obj.error or ''):sub(1,300),activeIds={},reportedAt=os.time(),
        owned=math.max(0,math.min(4096,tonumber(obj.owned) or 0)),
        running=math.max(0,math.min(4096,tonumber(obj.running) or 0)),
        accountMismatch=obj.accountMismatch==true,
        identityAmbiguous=obj.identityAmbiguous==true,
        reconciledActivate=math.max(0,math.min(4096,tonumber(obj.reconciledActivate) or 0)),
        reconciledOrphaned=math.max(0,math.min(4096,tonumber(obj.reconciledOrphaned) or 0)),
        accountSource=tostring(obj.accountSource or ''):sub(1,16),
        hideStrategy=tostring(obj.hideStrategy or ''):sub(1,32),
        hideVerified=obj.hideVerified==true,
        helpersPaused=obj.helpersPaused==true,
        helperPauseReason=tostring(obj.helperPauseReason or ''):sub(1,250),
        restoredSelections=math.max(0,tonumber(obj.restoredSelections) or 0),
        contracts={}}
      if type(obj.contracts)=='table' then
        for name,arity in pairs(obj.contracts) do
          if type(name)=='string' and #name<=40 and (arity==nil or type(arity)=='number') then
            runtime.contracts[name]=arity
          end
        end
      end
      if type(obj.activeIds)=='table' then
        for _,id in ipairs(obj.activeIds) do
          if valid_id(id) and #runtime.activeIds<12 then runtime.activeIds[#runtime.activeIds+1]=tonumber(id) end
        end
      end
    end
    return response({success=true})
  end
end
function M.tick(now) if live.tick then pcall(live.tick, now or os.time()) end end
function M.on_unload() if live.shutdown then pcall(live.shutdown) end end
M.new_discord_session = new_discord_session
M.collect_running = collect_running
M.read_proc = read_proc
M.process_snapshot = process_snapshot
M.login_users = login_users
M.fnv1a = fnv1a
M.acf_name = acf_name
M.resolve_game_name = resolve_game_name
M.discord_socket_paths = discord_socket_paths
M.discord_handshake = discord_handshake
M.discord_set_activity = discord_set_activity
M.helper_policy = helper_policy
return M
