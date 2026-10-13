-- SLS Presence backend extension for swwayps/lumen.
-- Runs within the existing Lumen process; no extra network listener.
local json = require('json')
local fs = require('fs')
local millennium = require('millennium')
local M = {}
local HOME = os.getenv('HOME') or ''
local DIR = HOME .. '/.local/share/slssteam-integrations'
local FILE = DIR .. '/presence.json'
local MAX = 4294967295
local function valid_id(v)
  local n = tonumber(v)
  return n and n >= 1 and n <= MAX and n == math.floor(n)
end
local function blank() return {enabled=false, owned={}, running={}, cleanup=0, account=nil, accountSource=nil} end
-- Only the 8-hex FNV digest of an account identity is ever persisted; the raw
-- SteamID or account name must never reach this file or a log line.
local function valid_account(v)
  return type(v)=='string' and v:match('^[0-9a-f]+$')~=nil and #v<=16
end
local function valid_source(v)
  return type(v)=='string' and v:match('^[a-z%-]+$')~=nil and #v>=4 and #v<=16
end
local function read_file()
  local f = io.open(FILE, 'rb')
  if not f then return blank() end
  local s=f:read('*a'); f:close()
  local ok,value=pcall(json.decode,s)
  if not ok or type(value)~='table' then return blank() end
  local data=blank()
  data.enabled=value.enabled==true
  data.cleanup=tonumber(value.cleanup) or 0
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
  return data
end
local function save_file(state)
  fs.create_directories(DIR)
  local tmp=FILE..'.tmp'
  local f,err=io.open(tmp,'wb')
  if not f then return false,err end
  local ok,body=pcall(json.encode,state)
  if not ok then f:close(); os.remove(tmp); return false,body end
  local written,werr=f:write(body)
  local closed,cerr=f:close()
  if not written or not closed then os.remove(tmp);return false,werr or cerr or 'write failed' end
  local done,why=os.rename(tmp,FILE)
  if not done then os.remove(tmp);return false,why end
  return true
end
local function response(o) return json.encode(o) end
-- Which Steam-launched processes are alive right now.
--
-- Steam's reaper tags every process it starts with `AppId=<id>`, so a scan of
-- /proc gives the two facts a recreated SharedJSContext cannot otherwise recover:
-- which manifest games are still running, and which of our helper shortcuts are
-- still alive. Pure input functions are injected so tests never read /proc.
local MAX_PIDS = 8192
local MAX_CMDLINE = 4096
local function collect_running(list_dir, read_file)
  local games, helpers = {}, {}
  local seen = {}
  local ok, entries = pcall(list_dir, '/proc')
  if not ok or type(entries) ~= 'table' then return games, helpers end
  local scanned = 0
  for _, entry in ipairs(entries) do
    if scanned >= MAX_PIDS then break end
    local name = entry.name or (type(entry) == 'string' and entry or nil)
    if name and name:match('^%d+$') then
      scanned = scanned + 1
      local ok_read, raw = pcall(read_file, '/proc/' .. name .. '/cmdline')
      if ok_read and type(raw) == 'string' and #raw > 0 and #raw <= MAX_CMDLINE then
        local flat = raw:gsub('%z', ' ')
        local id = flat:match('AppId=(%d+)')
        if id and not seen[name] then
          seen[name] = true
          local value = tonumber(id)
          if value and value >= 1 and value <= MAX then
            games[#games + 1] = value
            if flat:find('/usr/bin/sleep', 1, true) then
              helpers[#helpers + 1] = value
            end
          end
        end
      end
    end
  end
  return games, helpers
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
local manifest_cache={at=0,apps={}}
local function manifests()
  local now=os.time()
  if now-manifest_cache.at<30 then return manifest_cache.apps end
  local root=millennium.steam_path()
  local dir=fs.join(root,'config','stplug-in')
  local apps={}
  if root~='' and fs.exists(dir) then
    local ok,files=pcall(fs.list,dir)
    if ok and type(files)=='table' then
      for _,entry in ipairs(files) do
        if not entry.is_directory then
          local id=tostring(entry.name):match('^(%d+)%.lua$')
          if valid_id(id) then apps[#apps+1]=tonumber(id) end
        end
      end
      table.sort(apps)
    end
  end
  manifest_cache={at=now,apps=apps}
  return apps
end
function M.register(registry)
  local state=read_file()
  local runtime={active=0,healthy=false,error='',activeIds={},reportedAt=0,
    owned=0,running=0,accountMismatch=false,identityAmbiguous=false,accountSource='',
    reconciledActivate=0,reconciledOrphaned=0,hideStrategy='',hideVerified=false,contracts={}}
  local titles={}
  registry.SlsPresenceGet=function()
    return response({success=true, state=state, apps=manifests(), runtime=runtime, titles=titles, identity=identity()})
  end
  -- Read-only contract probe: proves the wire shape Lumen actually delivers
  -- (alphabetical-key positional args, one serialized string) without changing
  -- any state. Used by scripts/probe-live-apis.js and the report.
  registry.SlsPresenceContractProbe=function(raw)
    local ok,obj=pcall(json.decode,raw or '')
    return response({success=true, argType=type(raw), argLength=type(raw)=='string' and #raw or -1,
      decoded=(ok and type(obj)=='table')==true})
  end
  -- Read-only process snapshot. Used once per agent boot to recover state that a
  -- recreated SharedJSContext lost: which manifest games are actually running and
  -- which of our helpers are still alive.
  registry.SlsPresenceProbeProcesses=function()
    local games, helpers = collect_running(fs.list, function(path)
      local f = io.open(path, 'rb')
      if not f then return nil end
      local data = f:read('*a'); f:close(); return data
    end)
    return response({success=true, games=games, helpers=helpers, scannedAt=os.time()})
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
    for original,shortcut in pairs(obj.running) do
      if tonumber(shortcut)~=next_state.owned[tostring(original)] then
        return response({success=false,error='Invalid active shortcut'})
      end
      next_state.running[tostring(original)]=tonumber(shortcut)
    end
    local saved,err=save_file(next_state)
    if saved then state=next_state end
    return response({success=saved,error=err})
  end
  registry.SlsPresenceSetEnabled=function(value)
    if type(value)~='boolean' then return response({success=false,error='Expected boolean'}) end
    local updated=read_file();updated.enabled=value
    local ok,err=save_file(updated)
    if ok then state=updated end
    return response({success=ok,error=err,enabled=state.enabled})
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
M.collect_running = collect_running
M.login_users = login_users
M.fnv1a = fnv1a
return M
