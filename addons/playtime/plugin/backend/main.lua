-- Persistent local ledger for swwayps/lumen SLS Playtime.
-- Does not change Steam's official tracked hours or Steam Cloud data.
--
-- Schema 2/3 storage contract (schema 3 = the two-machine merge):
--   <dir>/playtime/<steamid64>.json       baseline ledger (schema 2), READ ONLY
--                                         in schema 3; never written or deleted
--   <dir>/playtime/<steamid64>.json.bak   previous good baseline
--   <dir>/playtime/<steamid64>.<host>.json       this machine's contribution
--   <dir>/playtime/<steamid64>.<host>.json.bak   its previous good contents
--   <dir>/playtime/<steamid64>.merge.json        one-time migration marker
--   <dir>/playtime.json                  legacy shared schema-1 ledger (kept)
--   <dir>/corrupt/<name>-<stamp>          quarantined unreadable ledgers (kept)
--
-- The directory is shared between machines (Syncthing). Schema 2 wrote the
-- whole ledger back, so the last writer won and the other machine's sessions
-- were lost. Schema 3 never writes the shared baseline: each machine appends
-- only its own `<account>.<host>.json`, and every reader merges baseline +
-- all hosts. A host's own sync-conflict copies are snapshots of a monotone
-- counter, so the per-field max across them counts each session once.
--
-- Every save is atomic (tmp + flush + optional fsync + rename) and always
-- leaves the previous good file as .bak. A ledger that cannot be decoded is
-- restored from .bak with a note, or quarantined, never silently reset.
--
-- Heartbeats are idempotent. The agent sends an opaque per-session id plus a
-- monotonically increasing sequence number, and a replay of an already
-- committed sequence returns success without counting the elapsed time twice.
-- That is the response-loss case: the backend persisted the pulse but the RPC
-- reply never reached the page, so the page retried it.
local json=require('json')
local fs=require('fs')
local millennium=require('millennium')
local M={}
local HOME=os.getenv('HOME') or ''
local DIR=HOME..'/.local/share/slssteam-integrations'
local LEGACY=DIR..'/playtime.json'
local MAX=4294967295
local SCHEMA=2
-- Ledger model (not the file schema): 1 means "the official lifetime playtime
-- from localconfig.vdf is the base for games Steam tracks". A ledger without
-- this marker predates the model and its adjustments are retired once.
local OFFICIAL_BASE=1
local MAX_EVENTS=200
local MAX_PULSE=30
local MAX_SECONDS=36000000000
local MAX_BYTES=8*1024*1024
local MAX_ACKED=64
local MAX_DAYS=400
local MAX_TITLES=2000
local DAY=86400
-- Schema-3 contributions. Bounded like the ledger, for the same reason: the
-- file is synced between machines. The pulse tail only feeds `recent` and the
-- post-import gap; totals are the monotone counters beside it.
local CONTRIB=3
local MAX_SESSIONS=MAX_EVENTS
local STATE_DIR=(os.getenv('XDG_STATE_HOME') or (HOME..'/.local/state'))..'/slssteam-integrations'
-- File stamps for the poll cache and rev. LFS is optional.
local LFS=nil
do
  local ok,mod=pcall(require,'lfs')
  if ok and type(mod)=='table' then LFS=mod end
end
local function valid_id(v)
  local n=tonumber(v)
  return n and n>=1 and n<=MAX and n==math.floor(n)
end
local function finite(v,min,max)
  local n=tonumber(v)
  if not n or n~=n or n==math.huge or n==-math.huge then return nil end
  if n<min then n=min elseif n>max then n=max end
  return n
end
local function valid_day_key(k)
  local y,m,d=k:match('^(%d%d%d%d)%-(%d%d)%-(%d%d)$')
  if not y then return false end
  local t=os.time{year=tonumber(y),month=tonumber(m),day=tonumber(d),hour=12}
  if not t then return false end
  -- A key like 2026-13-99 matches the shape above but is not a real date;
  -- os.time normalises it, so only the round trip proves it.
  return os.date('%Y-%m-%d',t)==k
end
local function blank(account)
  return {version=SCHEMA,account=account or '',games={},events={},daily={},titles={},active={},acked={},ackOrder={},officialBase=OFFICIAL_BASE}
end
local function reply(data) return json.encode(data) end
-- Rebuild a trusted in-memory ledger from decoded JSON. Anything unusable is
-- dropped here, once, instead of throwing in the middle of a later heartbeat.
local function sanitize(obj,account,keep_sessions)
  local data=blank(account)
  -- A ledger only belongs to the official-base model when it says so; anything
  -- older is migrated by the caller once a localconfig.vdf is available. The
  -- default from blank() must not be inherited here, or an old ledger would
  -- look migrated without its adjustments ever being retired.
  if tonumber(obj.officialBase)==OFFICIAL_BASE then data.officialBase=OFFICIAL_BASE else data.officialBase=nil end
  for k,v in pairs(type(obj.games)=='table' and obj.games or {}) do
    if valid_id(k) and type(v)=='table' then
      local key=tostring(tonumber(k))
      data.games[key]={
        title=type(v.title)=='string' and v.title:sub(1,160) or ('App '..key),
        trackedSeconds=finite(v.trackedSeconds,0,MAX_SECONDS) or 0,
        adjustmentSeconds=finite(v.adjustmentSeconds,-MAX_SECONDS,MAX_SECONDS) or 0,
        lastPlayed=finite(v.lastPlayed,0,1e12) or 0,
      }
    end
  end
  -- Display-name cache. The Steam shell window has no appStore, so only the
  -- SharedJSContext agent can resolve a name; it reports them here. Purely
  -- cosmetic and rebuildable, so it is bounded and never trusted as data.
  local names={}
  for k,v in pairs(type(obj.titles)=='table' and obj.titles or {}) do
    if valid_id(k) and type(v)=='string' and v~='' then names[#names+1]=tostring(tonumber(k)) end
  end
  table.sort(names)
  for i=math.max(1,#names-MAX_TITLES+1),#names do
    data.titles[names[i]]=obj.titles[names[i]]:sub(1,160)
  end
  for _,e in ipairs(type(obj.events)=='table' and obj.events or {}) do
    if type(e)=='table' and valid_id(e.id) then
      local seconds=finite(e.seconds,0,MAX_PULSE)
      local ended=finite(e.endedAt,0,1e12)
      if seconds and ended then
        data.events[#data.events+1]={id=tonumber(e.id),seconds=seconds,endedAt=ended}
        if #data.events>MAX_EVENTS then table.remove(data.events,1) end
      end
    end
  end
  -- Daily buckets. The bounded pulse ring above only spans a few hours of
  -- pulses, so it can never answer a 7- or 30-day question; the buckets can.
  local days={}
  for k in pairs(type(obj.daily)=='table' and obj.daily or {}) do
    if type(k)=='string' and valid_day_key(k) then days[#days+1]=k end
  end
  table.sort(days)
  for i=math.max(1,#days-MAX_DAYS+1),#days do
    local v=finite(obj.daily[days[i]],0,MAX_SECONDS)
    if v then data.daily[days[i]]=v end
  end
  -- Ack records for finished sessions survive a restart on purpose: a stop
  -- whose response was lost can still be replayed after the page reconnects.
  local order={}
  for sid in pairs(type(obj.acked)=='table' and obj.acked or {}) do
    if type(sid)=='string' and #sid>=1 and #sid<=64 then order[#order+1]=sid end
  end
  table.sort(order)
  for i=math.max(1,#order-MAX_ACKED+1),#order do
    local seq=finite(obj.acked[order[i]],0,1e15)
    if seq then data.acked[order[i]]=seq;data.ackOrder[#data.ackOrder+1]=order[i] end
  end
  -- Session bookkeeping is deliberately not restored from disk: a session from
  -- a previous process cannot be verified. It IS restored from an in-process
  -- rollback snapshot, where it is our own state and dropping it would stop
  -- recording until the game was relaunched.
  if keep_sessions then
    for k,v in pairs(type(obj.active)=='table' and obj.active or {}) do
      if valid_id(k) and type(v)=='table' and type(v.sid)=='string' and #v.sid<=64 then
        data.active[tostring(tonumber(k))]={sid=v.sid,seq=finite(v.seq,0,1e15) or 0,
          timestamp=finite(v.timestamp,0,1e12) or 0}
      end
    end
  end
  return data
end
-- ---------------------------------------------------------------------------
-- Schema 3: per-machine contributions and their merge.
-- ---------------------------------------------------------------------------
local function blank_own(account,host,nonce,installation)
  return {version=CONTRIB,host=host,nonce=nonce,installation=installation,
    account=account or '',games={},daily={},
    sessions={},edits={},titles={},import=nil,acked={},ackOrder={},resetAt=0,active={}}
end
local function own_record(store,k)
  local rec=store[k]
  if not rec then
    rec={title='App '..k,trackedSeconds=0,adjustmentSeconds=0,lastPlayed=0}
    store[k]=rec
  end
  return rec
end
-- Decode one contribution file. Unusable pieces are dropped, never guessed.
local function sanitize_own(obj,account)
  if type(obj)~='table' then return nil,'not a JSON object' end
  if tonumber(obj.version)~=CONTRIB then return nil,'unsupported contribution schema '..tostring(obj.version) end
  local declared=type(obj.host)=='string' and obj.host or ''
  if not declared:match('^[%w%-_]+$') or #declared<1 or #declared>32 then return nil,'missing host id' end
  -- Installation identity. Two machines can be handed the same host id (a
  -- copied state dir, a restored backup); the nonce is what tells their
  -- contributions apart, so they are summed instead of max-merged.
  local nonce=type(obj.nonce)=='string' and obj.nonce or nil
  if nonce and (not nonce:match('^[%w%-_]+$') or #nonce>64) then return nil,'invalid installation id' end
  local declared_account=type(obj.account)=='string' and obj.account or ''
  if declared_account~='' and account and account~='' and declared_account~=account then
    return nil,'contribution belongs to account '..declared_account
  end
  local installation=type(obj.installation)=='string' and obj.installation or nil
  if installation and (not installation:match('^[%w%-_]+$') or #installation>64) then
    return nil,'invalid installation fingerprint'
  end
  local own=blank_own(account,declared,nonce,installation)
  own.resetAt=finite(obj.resetAt,0,1e12) or 0
  for k,v in pairs(type(obj.games)=='table' and obj.games or {}) do
    if valid_id(k) and type(v)=='table' then
      local key=tostring(tonumber(k))
      own.games[key]={title=type(v.title)=='string' and v.title:sub(1,160) or ('App '..key),
        trackedSeconds=finite(v.trackedSeconds,0,MAX_SECONDS) or 0,
        adjustmentSeconds=finite(v.adjustmentSeconds,-MAX_SECONDS,MAX_SECONDS) or 0,
        lastPlayed=finite(v.lastPlayed,0,1e12) or 0}
    end
  end
  local days={}
  for k in pairs(type(obj.daily)=='table' and obj.daily or {}) do
    if type(k)=='string' and valid_day_key(k) then days[#days+1]=k end
  end
  table.sort(days)
  for i=math.max(1,#days-MAX_DAYS+1),#days do
    local v=finite(obj.daily[days[i]],0,MAX_SECONDS)
    if v then own.daily[days[i]]=v end
  end
  for _,e in ipairs(type(obj.sessions)=='table' and obj.sessions or {}) do
    if type(e)=='table' and valid_id(e.id) then
      local seconds=finite(e.seconds,0,MAX_PULSE)
      local ended=finite(e.endedAt,0,1e12)
      if seconds and ended then
        own.sessions[#own.sessions+1]={id=tonumber(e.id),seconds=seconds,endedAt=ended}
      end
    end
  end
  while #own.sessions>MAX_SESSIONS do table.remove(own.sessions,1) end
  for k,v in pairs(type(obj.edits)=='table' and obj.edits or {}) do
    if valid_id(k) and type(v)=='table' then
      local at=finite(v.at,0,1e12)
      local adjustment=finite(v.adjustmentSeconds,-MAX_SECONDS,MAX_SECONDS)
      if at and adjustment then
        own.edits[tostring(tonumber(k))]={at=at,host=declared,adjustmentSeconds=adjustment}
      end
    end
  end
  for k,v in pairs(type(obj.titles)=='table' and obj.titles or {}) do
    if valid_id(k) and type(v)=='table' and type(v.v)=='string' and v.v~='' then
      own.titles[tostring(tonumber(k))]={v=v.v:sub(1,160),at=finite(v.at,0,1e12) or 0}
    end
  end
  if type(obj.import)=='table' then
    local at=finite(obj.import.at,0,1e12)
    if at then
      local record={at=at,host=declared,legacy=imp.legacy==true,games={},daily={}}
      for k,v in pairs(type(obj.import.games)=='table' and obj.import.games or {}) do
        if valid_id(k) and type(v)=='table' then
          record.games[tostring(tonumber(k))]={
            trackedSeconds=finite(v.trackedSeconds,0,MAX_SECONDS) or 0,
            adjustmentSeconds=finite(v.adjustmentSeconds,-MAX_SECONDS,MAX_SECONDS) or 0}
        end
      end
      for k,v in pairs(type(obj.import.daily)=='table' and obj.import.daily or {}) do
        if type(k)=='string' and valid_day_key(k) then
          local seconds=finite(v,0,MAX_SECONDS)
          if seconds then record.daily[k]=seconds end
        end
      end
      own.import=record
    end
  end
  local order={}
  for sid in pairs(type(obj.acked)=='table' and obj.acked or {}) do
    if type(sid)=='string' and #sid>=1 and #sid<=64 then order[#order+1]=sid end
  end
  table.sort(order)
  for i=math.max(1,#order-MAX_ACKED+1),#order do
    local seq=finite(obj.acked[order[i]],0,1e15)
    if seq then own.acked[order[i]]=seq;own.ackOrder[#own.ackOrder+1]=order[i] end
  end
  return own
end
-- Canonical content fingerprint of a contribution. Used to tell an identical
-- copy (counted once) from a divergent writer that happens to share a host id
-- and nonce (summed): a cloned state directory copies both, the histories do
-- not match, and neither side may be dropped.
local function own_digest(c)
  local h1,h2=11,23
  local function mixn(n)
    n=math.floor(tonumber(n) or 0)%16777216
    h1=(h1*131+n+1)%16777216
    h2=(h2*65599+n+3)%16777216
  end
  local function mixt(text)
    text=tostring(text or '')
    for i=1,#text do mixn(text:byte(i)) end
  end
  local keys={}
  for k in pairs(c.games) do keys[#keys+1]=k end
  table.sort(keys)
  for _,k in ipairs(keys) do
    local g=c.games[k]
    mixt(k);mixn(g.trackedSeconds);mixn(g.adjustmentSeconds);mixt(g.title)
  end
  keys={}
  for k in pairs(c.daily) do keys[#keys+1]=k end
  table.sort(keys)
  for _,k in ipairs(keys) do mixt(k);mixn(c.daily[k]) end
  for _,e in ipairs(c.sessions) do mixn(e.id);mixn(e.seconds);mixn(e.endedAt) end
  keys={}
  for k in pairs(c.edits) do keys[#keys+1]=k end
  table.sort(keys)
  for _,k in ipairs(keys) do
    local e=c.edits[k]
    mixt(k);mixn(e.at);mixn(e.adjustmentSeconds)
  end
  return ('%06x%06x'):format(h1,h2)
end
-- Same totals for every game and day: what two conflict copies of one writer
-- look like (they may differ only in the pulse tail). Anything else is either
-- a genuinely newer snapshot of the same writer or a clone; since a no-field
-- fingerprint cannot prove which, divergent totals are summed (never lost) and
-- reported, while equal totals are merged by max.
local function equal_totals(a,b)
  for k,rec in pairs(b.games) do
    local other=a.games[k]
    if not other or other.trackedSeconds~=rec.trackedSeconds then return false end
  end
  for k,rec in pairs(a.games) do
    if not b.games[k] then return false end
    if rec.trackedSeconds~=b.games[k].trackedSeconds then return false end
  end
  for k,v in pairs(b.daily) do
    if (a.daily[k] or 0)~=v then return false end
  end
  for k,v in pairs(a.daily) do
    if (b.daily[k] or 0)~=v then return false end
  end
  return true
end
-- (at, host) is the only logical clock there is. Comparing the stored pair,
-- never the local wall clock, is what makes the merge deterministic on both
-- machines and independent of file order.
local function wins(at,host,other_at,other_host)
  if not other_at then return true end
  if at~=other_at then return at>other_at end
  return tostring(host)>tostring(other_host)
end
-- Fold one contribution into its per-host group. Two files from the SAME host
-- are the same monotone counter at two moments (a main file and a
-- sync-conflict copy of it), so the per-field max counts a session that is in
-- both exactly once instead of twice.
local function fold_own(group,own)
  for k,rec in pairs(own.games) do
    local g=group.games[k]
    if not g then
      group.games[k]={title=rec.title,trackedSeconds=rec.trackedSeconds,
        adjustmentSeconds=rec.adjustmentSeconds,lastPlayed=rec.lastPlayed}
    else
      g.trackedSeconds=math.max(g.trackedSeconds,rec.trackedSeconds)
      g.lastPlayed=math.max(g.lastPlayed,rec.lastPlayed)
      if rec.title~='' and rec.title~=('App '..k) then g.title=rec.title end
    end
  end
  for k,v in pairs(own.daily) do group.daily[k]=math.max(group.daily[k] or 0,v) end
  for _,e in ipairs(own.sessions) do
    local key=own.host..'|'..e.id..'|'..e.endedAt..'|'..e.seconds
    if not group.sessionKeys[key] then
      group.sessionKeys[key]=true
      group.sessions[#group.sessions+1]=e
    end
  end
  for k,rec in pairs(own.edits) do
    local cur=group.edits[k]
    if not cur or wins(rec.at,rec.host,cur.at,cur.host) then group.edits[k]=rec end
  end
  for k,rec in pairs(own.titles) do
    local cur=group.titles[k]
    if not cur or wins(rec.at,rec.host,cur.at,cur.host) then group.titles[k]=rec end
  end
  if own.import then
    local cur=group.import
    if not cur or wins(own.import.at,own.import.host,cur.at,cur.host) then group.import=own.import end
  end
  for sid,seq in pairs(own.acked) do
    if not group.acked[sid] or seq>group.acked[sid] then group.acked[sid]=seq end
  end
end
-- The one reader every surface goes through: baseline + their contributions.
local function merge_view(baseline,own,peers,recovered,account)
  local cloneFiles={}
  local view=blank(account)
  view.officialBase=baseline.officialBase
  for k,rec in pairs(baseline.games) do
    view.games[k]={title=rec.title,trackedSeconds=rec.trackedSeconds,
      adjustmentSeconds=rec.adjustmentSeconds,lastPlayed=rec.lastPlayed}
  end
  for k,v in pairs(baseline.daily) do view.daily[k]=v end
  local groups,order={},{}
  local own_key=nil
  if own then
    own_key=own.installation and (own.host..'|fp|'..own.installation..'|'..(own.nonce or ''))
      or (own.host..'|nofp|'..own_digest(own))
  end
  local function add(c)
    -- One group per writer installation. New files carry an installation
    -- fingerprint: their own conflict copies share it and are max-merged. A
    -- file whose fingerprint matches but whose content neither is identical
    -- nor a monotone extension is a CLONE (VM image, copied state dir): its
    -- play is summed and reported, never maxed away.
    -- Always recompute: `mine` is the same table across pulses, so a cached
    -- digest would go stale and make an identical copy look like a clone.
    c.digest=own_digest(c)
    local key
    if c.installation then
      key=c.host..'|fp|'..c.installation..'|'..(c.nonce or '')
      local existing=groups[key]
      if existing then
        local same=false
        for _,member in ipairs(existing.members) do
          if member.digest==c.digest or equal_totals(c,member.own) then
            same=true
            break
          end
        end
        if not same then
          key=key..'|clone|'..c.digest
          for _,member in ipairs(existing.members) do
            cloneFiles[#cloneFiles+1]=member.file
          end
          cloneFiles[#cloneFiles+1]=c.file or ''
        end
      end
    elseif c.nonce then
      -- No fingerprint but an installation nonce: content is the identity.
      key=c.host..'|nofp|'..c.digest
    else
      -- Pre-nonce file: one group per host, max-merged, so two monotone
      -- snapshots of the same old writer do not add up.
      key=c.host..'|legacy'
    end
    local group=groups[key]
    if not group then
      group={key=key,host=c.host,nonce=c.nonce,installation=c.installation,
        games={},daily={},sessions={},sessionKeys={},edits={},titles={},
        import=nil,acked={},members={}}
      groups[key]=group
      order[#order+1]=key
    end
    group.members[#group.members+1]={own=c,digest=c.digest,file=c.file}
    fold_own(group,c)
  end
  if own then add(own) end
  for _,p in ipairs(peers) do add(p) end
  table.sort(order)
  -- Time recorded on different installations is additive; the baseline is the
  -- pre-migration shared total and must not be added twice, so it is only
  -- ever the starting point, never a host.
  local contributed,contributed_daily={},{}
  local contributed_peer,contributed_peer_daily={},{}
  for _,key in ipairs(order) do
    local group=groups[key]
    for k,rec in pairs(group.games) do
      local g=view.games[k]
      if not g then
        g={title=rec.title,trackedSeconds=0,adjustmentSeconds=0,lastPlayed=0}
        view.games[k]=g
      end
      g.trackedSeconds=math.min(MAX_SECONDS,g.trackedSeconds+rec.trackedSeconds)
      g.lastPlayed=math.max(g.lastPlayed,rec.lastPlayed)
      if rec.title~='' and rec.title~=('App '..k) then g.title=rec.title end
      contributed[k]=(contributed[k] or 0)+rec.trackedSeconds
      if key~=own_key then contributed_peer[k]=(contributed_peer[k] or 0)+rec.trackedSeconds end
    end
    for k,v in pairs(group.daily) do
      view.daily[k]=(view.daily[k] or 0)+v
      contributed_daily[k]=(contributed_daily[k] or 0)+v
      if key~=own_key then contributed_peer_daily[k]=(contributed_peer_daily[k] or 0)+v end
    end
  end
  -- One winner per game for the whole shared directory: the last edit (or the
  -- last backup import) by (at, host). Both machines therefore agree.
  local chosen_edits,chosen_titles,chosen_import={},{},nil
  for _,key in ipairs(order) do
    local group=groups[key]
    for k,rec in pairs(group.edits) do
      local cur=chosen_edits[k]
      if not cur or wins(rec.at,rec.host,cur.at,cur.host) then chosen_edits[k]=rec end
    end
    for k,rec in pairs(group.titles) do
      local cur=chosen_titles[k]
      if not cur or wins(rec.at,rec.host,cur.at,cur.host) then chosen_titles[k]=rec end
    end
    local imp=group.import
    if imp and (not chosen_import or wins(imp.at,imp.host,chosen_import.at,chosen_import.host)) then
      chosen_import=imp
    end
  end
  if chosen_import then
    for k,rec in pairs(chosen_import.games) do
      local g=view.games[k]
      if not g then
        g={title='App '..k,trackedSeconds=0,adjustmentSeconds=0,lastPlayed=0}
        view.games[k]=g
      end
      if chosen_import.legacy then
        -- Gate per game: a backup whose total is not above what the shared
        -- baseline already holds cannot contain post-migration play, so its
        -- time is already covered and nothing is added. When it is above the
        -- baseline, only the excess is added, once, together with what was
        -- recorded since (so a pre-migration backup never hides our own play).
        local base_rec=baseline.games[k]
        local base_total=(base_rec and (base_rec.trackedSeconds+base_rec.adjustmentSeconds)) or 0
        local imported=rec.trackedSeconds+rec.adjustmentSeconds
        if imported>base_total then
          local extra=imported-base_total
          local current=(base_rec and base_rec.trackedSeconds or 0)+(contributed[k] or 0)
          g.trackedSeconds=math.min(MAX_SECONDS,current+extra)
        else
          g.trackedSeconds=math.min(MAX_SECONDS,
            (base_rec and base_rec.trackedSeconds or 0)+(contributed[k] or 0))
          g.adjustmentSeconds=(base_rec and base_rec.adjustmentSeconds) or 0
        end
      else
        g.trackedSeconds=rec.trackedSeconds
      end
      g.adjustmentSeconds=rec.adjustmentSeconds
    end
    for k,v in pairs(chosen_import.daily) do
      if chosen_import.legacy then
        view.daily[k]=math.max(v,(baseline.daily[k] or 0))+(contributed_daily[k] or 0)
      else
        view.daily[k]=v
      end
    end
    local added={}
    if not chosen_import.legacy then
      for _,key in ipairs(order) do
        for _,e in ipairs(groups[key].sessions) do
          local gk=key..'|'..e.id..'|'..e.endedAt..'|'..e.seconds
          local k=tostring(e.id)
          if e.endedAt>chosen_import.at and chosen_import.games[k] and not added[gk] then
            added[gk]=true
            local g=view.games[k]
            g.trackedSeconds=math.min(MAX_SECONDS,g.trackedSeconds+e.seconds)
            local day=os.date('%Y-%m-%d',e.endedAt)
            view.daily[day]=(view.daily[day] or 0)+e.seconds
          end
        end
      end
    end
  end
  for k,rec in pairs(chosen_edits) do
    if not chosen_import or rec.at>=chosen_import.at then
      local g=view.games[k]
      if not g then
        g={title='App '..k,trackedSeconds=0,adjustmentSeconds=0,lastPlayed=0}
        view.games[k]=g
      end
      g.adjustmentSeconds=rec.adjustmentSeconds
    end
  end
  for k,rec in pairs(chosen_titles) do view.titles[k]=rec.v end
  for k,v in pairs(baseline.titles) do
    if not view.titles[k] then view.titles[k]=v end
  end
  -- Pulses recovered once from schema-2 sync-conflict copies (see migrate).
  -- Once the shared ledger starts carrying a pulse (an old-version machine
  -- saved it), it must not be added a second time.
  local in_baseline={}
  for _,e in ipairs(baseline.events) do
    in_baseline[e.id..'|'..e.endedAt..'|'..e.seconds]=true
  end
  for _,e in ipairs(recovered or {}) do
    if e.id and e.seconds and e.endedAt
      and not in_baseline[e.id..'|'..e.endedAt..'|'..e.seconds] then
      local k=tostring(e.id)
      local g=view.games[k]
      if not g then
        g={title='App '..k,trackedSeconds=0,adjustmentSeconds=0,lastPlayed=0}
        view.games[k]=g
      end
      g.trackedSeconds=math.min(MAX_SECONDS,g.trackedSeconds+e.seconds)
      g.lastPlayed=math.max(g.lastPlayed,e.endedAt)
      if e.day then view.daily[e.day]=(view.daily[e.day] or 0)+e.seconds end
    end
  end
  -- `recent`: the baseline tail plus every host's tail, deduplicated.
  local seen,events={},{}
  local function remember(e)
    if not e or not e.id or not e.endedAt then return end
    local key=e.id..'|'..e.endedAt..'|'..e.seconds
    if not seen[key] then
      seen[key]=true
      events[#events+1]={id=e.id,seconds=e.seconds,endedAt=e.endedAt}
    end
  end
  for _,e in ipairs(baseline.events) do remember(e) end
  for _,host in ipairs(order) do
    for _,e in ipairs(groups[host].sessions) do remember(e) end
  end
  table.sort(events,function(a,b) return a.endedAt<b.endedAt end)
  if #events>MAX_EVENTS then
    local tail={}
    for i=#events-MAX_EVENTS+1,#events do tail[#tail+1]=events[i] end
    events=tail
  end
  view.events=events
  view.account=account or baseline.account
  return view,cloneFiles
end
-- Cheap change detector for the 1 s poll. Order independent (each entry is
-- hashed on its own, then mixed in), so pairs() order cannot change it.
local function revision(view,extra)
  local h1,h2=7,13
  local function mix(v)
    local n=math.floor(tonumber(v) or 0)%16777216
    h1=(h1*16777+n+3)%16777216
    h2=(h2*8191+n+7)%16777216
  end
  local function entry(seed,text,numbers)
    local h=seed
    text=tostring(text or '')
    for i=1,#text do h=(h*131+text:byte(i))%16777216 end
    for _,n in ipairs(numbers or {}) do
      h=(h*16777+(math.floor(tonumber(n) or 0)%16777216)+1)%16777216
    end
    mix(h)
  end
  for k,g in pairs(view.games) do
    entry(3,k,{g.trackedSeconds,g.adjustmentSeconds,g.lastPlayed})
    entry(4,g.title,{})
  end
  for k,v in pairs(view.daily) do entry(5,k,{v}) end
  -- The whole name, not just its length: a same-length rename is a change.
  for k,v in pairs(view.titles) do entry(7,k,{});entry(8,v,{}) end
  for k,v in pairs(view.active) do entry(11,k,{v.seq}) end
  for _,e in ipairs(view.events) do entry(13,'',{e.id,e.seconds,e.endedAt}) end
  entry(17,extra or '',{})
  return string.format('%06x%06x',h1,h2)
end
local function read_file(path)
  local f=io.open(path,'rb')
  if not f then return nil,'missing' end
  local raw=f:read(MAX_BYTES+1)
  f:close()
  if not raw then return nil,'unreadable' end
  if #raw==0 then return nil,'empty file' end
  if #raw>MAX_BYTES then return nil,'oversized file' end
  return raw
end
-- Valve's binary KeyValues, as used by shortcuts.vdf. Only the value types that
-- file actually contains are decoded, and an unknown type byte stops the walk
-- instead of guessing: a format change must degrade to "no shortcuts", never to
-- invented entries.
local function vdf_parse(raw,pos,depth)
  local out={}
  if depth>8 then return out,#raw+1 end
  while pos<=#raw do
    local kind=raw:byte(pos);pos=pos+1
    if kind==0x08 then return out,pos end
    local stop=raw:find('\0',pos,true)
    if not stop then return out,#raw+1 end
    local name=raw:sub(pos,stop-1);pos=stop+1
    if kind==0x00 then
      local child,nextpos=vdf_parse(raw,pos,depth+1)
      out[name]=child;pos=nextpos
    elseif kind==0x01 then
      local entry=raw:find('\0',pos,true)
      if not entry then return out,#raw+1 end
      out[name]=raw:sub(pos,entry-1);pos=entry+1
    elseif kind==0x02 then
      if pos+3>#raw then return out,#raw+1 end
      local b1,b2,b3,b4=raw:byte(pos,pos+3)
      -- Read as unsigned: a shortcut AppID has the high bit set, and the file
      -- stores it as the matching negative int32.
      out[name]=b1+b2*256+b3*65536+b4*16777216
      pos=pos+4
    elseif kind==0x03 or kind==0x04 then
      pos=pos+4
    elseif kind==0x07 or kind==0x0a or kind==0x0b then
      pos=pos+8
    else
      return out,#raw+1
    end
  end
  return out,#raw+1
end

-- Steam's text KeyValues (localconfig.vdf and friends). The binary parser above
-- covers shortcuts.vdf; this one walks the text form into nested tables, with
-- '//' comments skipped and quoted escapes kept simple on purpose. Only the
-- shape the official playtime needs is guaranteed; junk degrades to {}.
local function text_vdf_parse(raw)
  local pos,length=1,#raw
  local function skip()
    while pos<=length do
      local ch=raw:sub(pos,pos)
      if ch==' ' or ch=='\t' or ch=='\r' or ch=='\n' then
        pos=pos+1
      elseif ch=='/' and raw:sub(pos+1,pos+1)=='/' then
        local nl=raw:find('\n',pos,true)
        pos=nl and nl+1 or length+1
      else
        return
      end
    end
  end
  local function read_string()
    local out={}
    pos=pos+1
    while pos<=length do
      local ch=raw:sub(pos,pos)
      if ch=='\\' then
        out[#out+1]=raw:sub(pos+1,pos+1)
        pos=pos+2
      elseif ch=='"' then
        pos=pos+1
        return table.concat(out)
      else
        out[#out+1]=ch
        pos=pos+1
      end
    end
    return table.concat(out)
  end
  local parse_object
  parse_object=function()
    local out={}
    while true do
      skip()
      if pos>length then return out end
      if raw:sub(pos,pos)=='}' then
        pos=pos+1
        return out
      end
      if raw:sub(pos,pos)=='"' then
        local key=read_string()
        skip()
        local nextch=raw:sub(pos,pos)
        if nextch=='{' then
          pos=pos+1
          out[key]=parse_object()
        elseif nextch=='"' then
          out[key]=read_string()
        else
          out[key]=''
        end
      else
        pos=pos+1
      end
    end
  end
  local out={}
  while true do
    skip()
    if pos>length then return out end
    if raw:sub(pos,pos)=='"' then
      local key=read_string()
      skip()
      local nextch=raw:sub(pos,pos)
      if nextch=='{' then
        pos=pos+1
        out[key]=parse_object()
      elseif nextch=='"' then
        out[key]=read_string()
      end
    else
      pos=pos+1
    end
  end
end

local function copy_file(src,dst)
  local raw=read_file(src)
  if not raw then return false end
  local f=io.open(dst,'wb')
  if not f then return false end
  local written=f:write(raw)
  local closed=f:close()
  if not written or not closed then os.remove(dst); return false end
  return true
end

-- Official lifetime playtime per app, straight from Steam's own localconfig.vdf
-- (`Playtime`, minutes). For a game Steam tracks, this - not the ledger's own
-- since-inception count - is the number the user sees in the library, so it is
-- the base every surface must agree with. Cached by file size with a slow TTL:
-- the panel polls once a second and must not re-parse half a megabyte each time.
local official_cache={path='',size=-1,at=0,map={}}
local function official_playtimes(root,account)
  if root=='' or account=='' then return {} end
  -- Steam's userdata directory is named after the 32-bit account id, while the
  -- ledger is keyed by the steamid64.
  local account_number=tonumber(account)
  if not account_number or account_number<=76561197960265728 then return {} end
  local path=root..'/userdata/'..(account_number-76561197960265728)..'/config/localconfig.vdf'
  local now=os.time()
  local size=-1
  local handle=io.open(path,'rb')
  if handle then size=handle:seek('end') or -1; handle:close() end
  if path==official_cache.path and size==official_cache.size and now-official_cache.at<300 then
    return official_cache.map
  end
  local map={}
  local raw=read_file(path)
  if raw then
    local ok,store=pcall(text_vdf_parse,raw)
    if ok and type(store)=='table' then
      local node=store.UserLocalConfigStore
      node=type(node)=='table' and node.Software or nil
      node=type(node)=='table' and node.Valve or nil
      node=type(node)=='table' and node.Steam or nil
      node=type(node)=='table' and node.apps or nil
      if type(node)=='table' then
        for key,value in pairs(node) do
          if type(value)=='table' then
            local id=tonumber(key)
            local minutes=tonumber(value.Playtime)
            if id and valid_id(id) and minutes and minutes>0 then
              map[tostring(math.floor(id))]=math.floor(minutes*60)
            end
          end
        end
      end
    end
  end
  official_cache={path=path,size=size,at=now,map=map}
  return map
end
local function account_id(steamid64)
  local n=tonumber(steamid64)
  if not n then return nil end
  return tostring(n-76561197960265728)
end
-- Non-Steam games (library shortcuts) have no stplug-in manifest, but Steam
-- still sends lifetime notifications for their 32-bit AppID. shortcuts.vdf is
-- the only local source that lists them together with their names.
local function shortcut_inventory(root,steamid64)
  local dirs={}
  if account_id(steamid64) then
    dirs[1]=root..'/userdata/'..account_id(steamid64)
  else
    local userdata=root..'/userdata'
    if fs.exists(userdata) then
      local ok,items=pcall(fs.list,userdata)
      if ok and type(items)=='table' then
        for _,entry in ipairs(items) do
          local name=tostring(entry.name)
          if entry.is_directory and name~='0' and name:match('^%d+$') then
            dirs[#dirs+1]=fs.join(userdata,name)
          end
        end
      end
    end
  end
  local names={}
  for _,dir in ipairs(dirs) do
    local raw=read_file(fs.join(dir,'config','shortcuts.vdf'))
    if raw then
      local parsed=vdf_parse(raw,1,1)
      local bucket=type(parsed)=='table' and parsed.shortcuts or nil
      if type(bucket)=='table' then
        for _,entry in pairs(bucket) do
          if type(entry)=='table' then
            local id=valid_id(entry.appid) and tonumber(entry.appid) or nil
            if id then
              local label=entry.appname
              if type(label)=='string' and label~='' then names[tostring(id)]=label:sub(1,160) end
            end
          end
        end
      end
    end
  end
  return names
end
-- Steam-native game inventory: the installed Steam library, read from Steam's
-- own files only. Library roots come from steamapps/libraryfolders.vdf (plus
-- the Steam root itself); each root's steamapps/appmanifest_<appid>.acf
-- declares one installed app. Entry fields: appid, name, size (SizeOnDisk),
-- lastPlayed (LastPlayed, 0 when absent). A missing root or an unparseable
-- file yields the entries that could be read plus an `error` note; this
-- function never throws and never shells out.
local function steam_library(root)
  local entries, err = {}, nil
  if type(root) ~= 'string' or root == '' then return entries, 'Steam root not found' end
  local libraries, seen = { root }, { [root] = true }
  local folders = read_file(root .. '/steamapps/libraryfolders.vdf')
    or read_file(root .. '/config/libraryfolders.vdf')
  if folders then
    local ok, store = pcall(text_vdf_parse, folders)
    local bucket = ok and type(store) == 'table'
      and (store.libraryfolders or store.LibraryFolders) or nil
    if type(bucket) == 'table' then
      for _, value in pairs(bucket) do
        local path = type(value) == 'table' and tostring(value.path or '') or ''
        if path ~= '' and not seen[path] then
          seen[path] = true
          libraries[#libraries + 1] = path
        end
      end
    else
      err = 'libraryfolders.vdf could not be parsed'
    end
  end
  local installed = {}
  for _, library in ipairs(libraries) do
    local steamapps = library:gsub('/+$', '') .. '/steamapps'
    local ok, items = pcall(fs.list, steamapps)
    if ok and type(items) == 'table' then
      for _, item in ipairs(items) do
        if not item.is_directory then
          local appid = tostring(item.name):match('^appmanifest_(%d+)%.acf$')
          -- One entry per AppID: a stale manifest can sit in a second library.
          if appid and valid_id(appid) and not installed[tonumber(appid)] then
            local raw = read_file(steamapps .. '/appmanifest_' .. appid .. '.acf')
            local app = nil
            if raw then
              local parsed, store = pcall(text_vdf_parse, raw)
              app = parsed and type(store) == 'table'
                and (store.AppState or store.appstate) or nil
            end
            if type(app) ~= 'table' then
              err = err or ('appmanifest_' .. appid .. '.acf could not be parsed')
            end
            local name = type(app) == 'table' and app.name or ''
            installed[tonumber(appid)] = true
            entries[#entries + 1] = {
              appid = tonumber(appid),
              name = type(name) == 'string' and name:sub(1, 160) or '',
              size = type(app) == 'table' and (tonumber(app.SizeOnDisk) or 0) or 0,
              lastPlayed = type(app) == 'table' and (tonumber(app.LastPlayed) or 0) or 0,
            }
          end
        end
      end
    elseif #libraries == 1 or library == root then
      err = err or ('steamapps directory could not be listed under ' .. library)
    end
  end
  table.sort(entries, function(a, b) return a.appid < b.appid end)
  return entries, err
end
-- Exposed for tests and diagnostics: the Steam-native installed library.
M.steam_library = steam_library
local function decode_ledger(raw)
  local ok,obj=pcall(json.decode,raw)
  if not ok then return nil,'not valid JSON' end
  if type(obj)~='table' then return nil,'not a JSON object' end
  if type(obj.version)~='number' then return nil,'missing schema version' end
  if obj.version~=1 and obj.version~=SCHEMA then return nil,'unsupported schema version '..tostring(obj.version) end
  if type(obj.games)~='table' then return nil,'missing game table' end
  if obj.events~=nil and type(obj.events)~='table' then return nil,'invalid event table' end
  if obj.daily~=nil and type(obj.daily)~='table' then return nil,'invalid daily table' end
  return obj
end
local function quarantine(path)
  if not fs.exists(path) then return nil end
  local dir=DIR..'/corrupt'
  fs.create_directories(dir)
  local target=dir..'/'..(path:match('[^/]+$') or 'ledger')..'-'..os.date('%Y%m%d-%H%M%S')
  local base,n=target,0
  while fs.exists(target) do
    n=n+1
    target=base..'-'..n
  end
  local ok=os.rename(path,target)
  return ok and target or nil
end
local function read_state(path,account,keep)
  local raw=read_file(path)
  if raw then
    local obj,err=decode_ledger(raw)
    if obj then return sanitize(obj,account),'' end
    -- Keep the bytes, never overwrite them with a blank ledger. The baseline
    -- of a schema-3 directory is read-only, so `keep` leaves it in place even
    -- when it is unreadable instead of moving the user's file away.
    local moved=keep and nil or quarantine(path)
    local backup=read_file(path..'.bak')
    if backup then
      local restored=decode_ledger(backup)
      if restored then
        return sanitize(restored,account),('ledger was %s; recovered the previous good backup%s'):format(
          err,moved and (' ('..tostring(moved)..')') or '')
      end
    end
    return blank(account),('ledger was %s and no usable backup existed%s'):format(
      err,moved and ('; quarantined as '..moved) or '; its bytes were kept')
  end
  -- No ledger at all, but a .bak means a crash landed between the two renames
  -- that keep the previous good copy.
  local backup=read_file(path..'.bak')
  if backup then
    local restored=decode_ledger(backup)
    if restored then return sanitize(restored,account),'recovered the previous good backup' end
  end
  return blank(account),''
end
-- fsync where the host exposes it; not every fs shim does, and correctness of
-- the rename does not depend on it (it only narrows the crash window).
local function sync_card(path)
  local sync=type(fs)=='table' and fs.fsync
  if type(sync)=='function' then pcall(sync,path) end
end
local function persist(data,path)
  fs.create_directories(fs.parent_path(path))
  local ok,text=pcall(json.encode,data)
  if not ok then return false,tostring(text) end
  local tmp=path..'.tmp'
  local f,why=io.open(tmp,'wb')
  if not f then return false,why end
  local written,werr=f:write(text)
  if written then f:flush() end
  local closed,cerr=f:close()
  if not written or not closed then os.remove(tmp);return false,werr or cerr or 'write failed' end
  sync_card(tmp)
  if fs.exists(path) then
    local moved,merr=os.rename(path,path..'.bak')
    if not moved then os.remove(tmp);return false,merr or 'backup rename failed' end
  end
  local done,err=os.rename(tmp,path)
  if not done then
    -- Put the previous good ledger back instead of leaving the user with none.
    if fs.exists(path..'.bak') then os.rename(path..'.bak',path) end
    os.remove(tmp)
    return false,err or 'atomic replace failed'
  end
  return true
end
-- Machine identity. It has to be machine-local: an id inside the synced data
-- directory would be copied to the other machine and the race would be back.
-- XDG state lives outside the Syncthing folder, like the compressor's state
-- dir. SLS_PLAYTIME_HOST exists for tests and for recovering a lost file.
local function random_hex(bytes)
  local urandom=io.open('/dev/urandom','rb')
  if urandom then
    local raw=urandom:read(bytes);urandom:close()
    if raw and #raw==bytes then
      local out={}
      for i=1,#raw do out[#out+1]=('%02x'):format(raw:byte(i)) end
      return table.concat(out)
    end
  end
  return ('%08x%08x'):format(os.time()%0xffffffff,math.floor(os.clock()*1000000)%0xffffffff)
end
local function short_digest(text)
  local h1,h2=7,13
  text=tostring(text or '')
  for i=1,#text do
    local b=text:byte(i)
    h1=(h1*131+b+1)%16777216
    h2=(h2*65599+b+3)%16777216
  end
  return ('%06x%06x'):format(h1,h2)
end
-- A machine-only fingerprint. /etc/machine-id is read and immediately hashed:
-- the raw id is never stored, in the state dir or in a synced file. It is what
-- tells two boxes apart when a state directory (and its nonce) was copied.
local function machine_hash()
  local f=io.open('/etc/machine-id','rb')
  if f then
    local raw=f:read(128);f:close()
    local id=raw and raw:match('%x+')
    if id and #id>=8 then return short_digest('machine:'..id) end
  end
  local host=os.getenv('HOSTNAME') or os.getenv('COMPUTERNAME')
  if type(host)=='string' and host~='' then return short_digest('host:'..host) end
  return short_digest('unknown')
end
local function read_identity(path)
  local raw=read_file(path)
  if not raw then return nil end
  local ok,obj=pcall(json.decode,raw)
  if not ok or type(obj)~='table' then return nil end
  local host=type(obj.host)=='string' and obj.host or nil
  local nonce=type(obj.nonce)=='string' and obj.nonce or nil
  if not host or not host:match('^[%w%-_]+$') or #host>32 then return nil end
  if not nonce or not nonce:match('^[%w%-_]+$') or #nonce>64 then return nil end
  return {host=host,nonce=nonce,machine=tostring(obj.machine or ''),
    install=tostring(obj.install or '')}
end
local function save_identity(path,identity)
  local ok=pcall(function() fs.create_directories(STATE_DIR) end)
  if not ok then return false end
  local tmp=path..'.tmp'
  local f=io.open(tmp,'wb')
  if not f then return false end
  local written=f:write(json.encode(identity))
  if written then f:flush() end
  local closed=f:close()
  if not written or not closed then os.remove(tmp);return false end
  sync_card(tmp)
  if not os.rename(tmp,path) then os.remove(tmp);return false end
  return true
end
-- Installation identity = machine hash (never stored raw) + a random id minted
-- once. Both are needed: the nonce alone is copied with the state dir, and the
-- machine hash alone would not separate two accounts on one box.
local function installation_of(ident)
  return short_digest('install:'..tostring(ident.machine or '')
    ..':'..tostring(ident.install or ''))
end
-- Machine identity. It has to be machine-local: an id inside the synced data
-- directory would be copied to the other machine. SLS_PLAYTIME_HOST exists for
-- tests and for recovering a lost file; SLS_PLAYTIME_NONCE is test-only.
local function host_id()
  local override=os.getenv('SLS_PLAYTIME_HOST')
  local machine=machine_hash()
  if override and #override>=1 and #override<=32 and override:match('^[%w%-_]+$') then
    local forced=os.getenv('SLS_PLAYTIME_NONCE')
    if forced and #forced>=1 and #forced<=64 and forced:match('^[%w%-_]+$') then
      return override,'SLS_PLAYTIME_HOST',forced,machine,
        short_digest('forced:'..override..':'..forced),false
    end
    local path=STATE_DIR..'/identity-'..override..'.json'
    local ident=read_identity(path)
    local moved=false
    if ident and ident.machine~='' and ident.machine~=machine then
      -- The state directory came from another machine: keep its host label,
      -- mint a new nonce so the two installs are two writers, not one counter.
      ident.nonce=random_hex(8)
      ident.machine=machine
      ident.install=random_hex(6)
      moved=true
      save_identity(path,ident)
    elseif not ident then
      ident={host=override,nonce=random_hex(8),machine=machine,install=random_hex(6)}
      save_identity(path,ident)
    end
    return ident.host,'SLS_PLAYTIME_HOST',ident.nonce,ident.machine or '',
      installation_of(ident),moved
  end
  local path=STATE_DIR..'/identity.json'
  local ident=read_identity(path)
  local moved=false
  if ident and ident.machine~='' and ident.machine~=machine then
    ident.nonce=random_hex(8)
    ident.machine=machine
    ident.install=random_hex(6)
    moved=true
    save_identity(path,ident)
  elseif not ident then
    local legacy=nil
    local f=io.open(STATE_DIR..'/host-id','rb')
    if f then
      local raw=f:read(64);f:close()
      legacy=raw and raw:match('^%s*([%w%-_]+)')
    end
    local host=(legacy and #legacy>=4 and #legacy<=32) and legacy or random_hex(6)
    ident={host=host,nonce=random_hex(8),machine=machine,install=random_hex(6)}
    save_identity(path,ident)
  end
  return ident.host,'machine-local state',ident.nonce,ident.machine or '',
    installation_of(ident),moved
end
local function merge_marker_path(account)
  return DIR..'/playtime/'..account..'.merge.json'
end
local function highwater_path(account,host,nonce)
  return DIR..'/playtime/'..account..'.highwater.'..host..'-'..nonce..'.json'
end
local function highwater_files(account)
  local dir=DIR..'/playtime'
  local out={}
  local ok,items=pcall(fs.list,dir)
  if not ok or type(items)~='table' then return out end
  local prefix=account..'.highwater'
  for _,item in ipairs(items) do
    local name=type(item)=='table' and tostring(item.name) or ''
    if item.is_directory~=true and name:sub(1,#prefix)==prefix and name:sub(-5)=='.json' then
      out[#out+1]=dir..'/'..name
    end
  end
  table.sort(out)
  return out
end
-- High-water floors are per writer, and every conflict copy of every part is
-- read, taking the max: a stale Syncthing copy, or a part written by a machine
-- that has not seen the latest baseline, can never lower a floor. The whole
-- set is monotone, whichever writer saved last, and this writer only ever
-- raises its own part.
local function apply_highwater(baseline,account,host,nonce)
  if not account or account=='' then return false end
  local games,days={},{}
  for _,path in ipairs(highwater_files(account)) do
    local raw=read_file(path)
    if raw then
      local ok,obj=pcall(json.decode,raw)
      if ok and type(obj)=='table' and tonumber(obj.version)==CONTRIB then
        for k,v in pairs(type(obj.games)=='table' and obj.games or {}) do
          local n=finite(v,0,MAX_SECONDS)
          if valid_id(k) and n and n>(games[k] or 0) then games[k]=n end
        end
        for k,v in pairs(type(obj.daily)=='table' and obj.daily or {}) do
          local n=finite(v,0,MAX_SECONDS)
          if type(k)=='string' and valid_day_key(k) and n and n>(days[k] or 0) then days[k]=n end
        end
      end
    end
  end
  -- The live baseline is itself a floor: whatever it holds must be kept even
  -- if no part has recorded it yet (first run, or a part was pruned).
  for k,rec in pairs(baseline.games) do
    if rec.trackedSeconds>(games[k] or 0) then games[k]=rec.trackedSeconds end
  end
  for k,v in pairs(baseline.daily) do
    if v>(days[k] or 0) then days[k]=v end
  end
  local dropped=0
  for k,rec in pairs(baseline.games) do
    local high=games[k]
    if high and high>rec.trackedSeconds then
      dropped=dropped+1
      rec.trackedSeconds=high
    end
  end
  for k,v in pairs(baseline.daily) do
    local high=days[k]
    if high and high>v then
      dropped=dropped+1
      baseline.daily[k]=high
    end
  end
  local path=highwater_path(account,host or 'anon',nonce or 'nonce')
  local own=nil
  local own_raw=read_file(path)
  if own_raw then
    local ok,obj=pcall(json.decode,own_raw)
    if ok and type(obj)=='table' then own=obj end
  end
  local need=not own
  if not need then
    for k,v in pairs(games) do
      if (tonumber(own.games and own.games[k]) or 0)<v then need=true;break end
    end
  end
  if not need then
    for k,v in pairs(days) do
      if (tonumber(own.daily and own.daily[k]) or 0)<v then need=true;break end
    end
  end
  if need then
    persist({version=CONTRIB,account=account,games=games,daily=days},path)
  end
  return dropped>0
end
-- Read this machine's own contribution (its installation nonce is in the
-- file). A torn file is restored from its .bak with a note; it is never
-- silently replaced by an empty contribution.
local function read_own(path,account,host,nonce,installation)
  local raw=read_file(path)
  if raw then
    local ok,obj=pcall(json.decode,raw)
    if ok then
      local parsed,why=sanitize_own(obj,account)
      if parsed then
        if parsed.host==host and (parsed.nonce==nil or parsed.nonce==nonce)
          and (parsed.installation==nil or parsed.installation==installation) then
          parsed.nonce=nonce
          return parsed,''
        end
        -- Another writer ended up at our exact path (a copied identity, a
        -- restored backup). Move it to a name of its own so it is merged as a
        -- peer instead of being overwritten by the next save.
        local target=DIR..'/playtime/'..account..'.'..parsed.host..'-'
          ..(parsed.nonce or random_hex(6))..'-'..random_hex(3)..'.json'
        if os.rename(path,target) then
          return nil,('the file at this machine\'s own path belonged to another writer (%s); kept it as %s'
            ):format(tostring(parsed.host),target:match('[^/]+$') or target)
        end
        return nil,('the file at this machine\'s own path belongs to another writer (%s)'
          ..(why and (': '..why) or '')):format(tostring(parsed.host)),path
      end
    end
    local backup=read_file(path..'.bak')
    if backup then
      local ok2,obj2=pcall(json.decode,backup)
      if ok2 then
        local restored=sanitize_own(obj2,account)
        if restored and restored.host==host
          and (restored.nonce==nil or restored.nonce==nonce)
          and (restored.installation==nil or restored.installation==installation) then
          -- Move the unreadable bytes aside first: otherwise the next save
          -- would rotate them into .bak and overwrite the good copy.
          local moved=quarantine(path)
          restored.nonce=nonce
          return restored,('recovered this machine\'s previous contribution from .bak'
            ..(moved and ('; unreadable bytes kept as '..moved) or ''))
        end
      end
    end
    local moved=quarantine(path)
    return nil,('this machine\'s contribution was unreadable; started a fresh one'
      ..(moved and (' (kept as '..moved..')') or ''))
  end
  local backup=read_file(path..'.bak')
  if backup then
    local ok,obj=pcall(json.decode,backup)
    if ok then
      local restored=sanitize_own(obj,account)
      if restored and restored.host==host
        and (restored.nonce==nil or restored.nonce==nonce)
        and (restored.installation==nil or restored.installation==installation) then
        restored.nonce=nonce
        return restored,'recovered this machine\'s contribution from .bak'
      end
    end
  end
  return nil,''
end
-- Every contribution file for an account, including `sync-conflict` copies of
-- them. The baseline itself, its .bak, .pre-official and .pre-decky-import
-- copies are deliberately not matched: they are schema-2 aggregated totals,
-- not per-host counters, and folding them in would double count.
-- Every contribution file for an account, including `sync-conflict` copies of
-- them. The match is an explicit allow-list: `<acct>.<token>.json` and
-- `<acct>.<token>.sync-conflict-*.json`, with the migration marker, the
-- high-water file, .bak/.tmp and anything else excluded by name. The baseline
-- itself is never matched: it is a schema-2 aggregate, not a host counter.
local function contribution_files(account)
  local dir=DIR..'/playtime'
  local out={}
  local ok,items=pcall(fs.list,dir)
  if not ok or type(items)~='table' then return out end
  local base=account..'.'
  local reserved={}
  reserved[base..'merge.json']=true
  local function is_reserved(name)
    if reserved[name] then return true end
    local tail=name:sub(#base+1)
    return tail:sub(1,7)=='merge-' or tail:sub(1,9)=='highwater'
  end
  for _,item in ipairs(items) do
    local name=type(item)=='table' and tostring(item.name) or ''
    local candidate=name~='' and item.is_directory~=true and name:sub(1,#base)==base
      and name:sub(-5)=='.json' and not is_reserved(name)
    if candidate then
      local rest=name:sub(#base+1)
      local token=rest:match('^([%w%-_]+)%.json$')
        or rest:match('^([%w%-_]+)%.sync%-conflict%-[^.]+%.json$')
      if token then
        out[#out+1]={name=name,path=dir..'/'..name,token=token,
          conflict=rest:find('%.sync%-conflict%-')~=nil}
      end
    end
  end
  table.sort(out,function(a,b) return a.name<b.name end)
  return out
end
-- Schema-2 sync-conflict copies of the account ledger itself.
local function baseline_conflicts(account)
  local dir=DIR..'/playtime'
  local out={}
  local ok,items=pcall(fs.list,dir)
  if not ok or type(items)~='table' then return out end
  local prefix=account..'.sync-conflict-'
  for _,item in ipairs(items) do
    local name=type(item)=='table' and tostring(item.name) or ''
    if item.is_directory~=true and name:sub(1,#prefix)==prefix and name:sub(-5)=='.json' then
      out[#out+1]={name=name,path=dir..'/'..name}
    end
  end
  table.sort(out,function(a,b) return a.name<b.name end)
  return out
end
-- One-time migration of the shared directory. The schema-2 ledger becomes the
-- read-only baseline and stays on disk. A sync-conflict copy of it can only be
-- used for pulses that are certainly missing from the baseline: same
-- (app, endedAt, seconds) absent there, and either the baseline's 200-pulse
-- ring never rolled or the pulse is newer than its newest entry. Anything
-- else stays untouched and is reported, because attributing an aggregated
-- total would either lose time or count it twice.
local function migrate_merge(account,baseline,note)
  local marker_path=merge_marker_path(account)
  local raw=read_file(marker_path)
  local marker=nil
  if raw then
    local ok,obj=pcall(json.decode,raw)
    if ok and type(obj)=='table' and tonumber(obj.version)==CONTRIB then marker=obj end
  end
  if not marker then
    local recovered,seen={},{}
    local newest=-1
    for _,e in ipairs(baseline.events) do
      if e.endedAt>newest then newest=e.endedAt end
    end
    local rolled=#baseline.events>=MAX_EVENTS
    local have={}
    for _,e in ipairs(baseline.events) do
      have[e.id..'|'..e.endedAt..'|'..e.seconds]=true
    end
    local names={}
    for _,conflict in ipairs(baseline_conflicts(account)) do
      names[#names+1]=conflict.name
      local craw=read_file(conflict.path)
      local other=nil
      if craw then
        -- decode_ledger returns (ledger, err), not (ok, ledger).
        other=decode_ledger(craw)
      end
      if other then
        for _,e in ipairs(type(other.events)=='table' and other.events or {}) do
          local id=type(e)=='table' and tonumber(e.id) or nil
          local seconds=type(e)=='table' and tonumber(e.seconds) or nil
          local ended=type(e)=='table' and tonumber(e.endedAt) or nil
          if id and seconds and ended and seconds>=0 and seconds<=MAX_PULSE and ended>0 then
            local key=id..'|'..ended..'|'..seconds
            if not have[key] and not seen[key] and (not rolled or ended>newest) then
              seen[key]=true
              recovered[#recovered+1]={id=id,seconds=seconds,endedAt=ended,
                day=os.date('%Y-%m-%d',ended)}
            end
          end
        end
      end
    end
    local floor_games,floor_daily={},{}
    for k,rec in pairs(baseline.games) do floor_games[k]=rec.trackedSeconds end
    for k,v in pairs(baseline.daily) do floor_daily[k]=v end
    marker={version=CONTRIB,account=account,migratedAt=os.time(),
      recovered=json.array(recovered),conflicts=json.array(names),
      floorGames=floor_games,floorDaily=floor_daily}
    local saved,why=persist(marker,marker_path)
    if not saved then
      -- Recovering without the marker would add the same pulses on every
      -- start, so nothing is recovered until the marker is durable.
      return note..'. Could not record the one-time merge: '..tostring(why),{},names,nil
    end
    return note,recovered,names,{games=floor_games,daily=floor_daily}
  end
  local recovered={}
  for _,e in ipairs(type(marker.recovered)=='table' and marker.recovered or {}) do
    local id=type(e)=='table' and tonumber(e.id) or nil
    local seconds=type(e)=='table' and tonumber(e.seconds) or nil
    local ended=type(e)=='table' and tonumber(e.endedAt) or nil
    if id and seconds and ended then
      recovered[#recovered+1]={id=id,seconds=seconds,endedAt=ended,
        day=type(e.day)=='string' and e.day or os.date('%Y-%m-%d',ended)}
    end
  end
  local names={}
  for _,v in ipairs(type(marker.conflicts)=='table' and marker.conflicts or {}) do
    if type(v)=='string' then names[#names+1]=v end
  end
  local floor=nil
  if type(marker.floorGames)=='table' or type(marker.floorDaily)=='table' then
    floor={games=type(marker.floorGames)=='table' and marker.floorGames or {},
      daily=type(marker.floorDaily)=='table' and marker.floorDaily or {}}
  end
  return note,recovered,names,floor
end
-- loginusers.vdf is Valve's own account list; "MostRecent" marks the account
-- Steam will sign in next. It is the only account identity a local add-on can
-- read without touching credentials. Ambiguity is reported as "unknown" rather
-- than guessed: guessing would file one account's hours under another.
local function read_login_users(root)
  local raw=read_file(root..'/config/loginusers.vdf')
  if not raw then return {} end
  local users,current={},nil
  for line in raw:gmatch('[^\r\n]+') do
    local id=line:match('^%s*"(%d+)"%s*$')
    if id and #id>=16 and #id<=20 then
      current={id=id,recent=false}
      users[#users+1]=current
    elseif current and line:match('"MostRecent"%s*"1"') then
      current.recent=true
    end
  end
  return users
end
local function resolve_account(root)
  local override=os.getenv('SLS_PLAYTIME_ACCOUNT')
  if override and override:match('^%d+$') and #override>=16 and #override<=20 then
    return override,'SLS_PLAYTIME_ACCOUNT'
  end
  local users=read_login_users(root)
  if #users==0 then return nil,'no Steam accounts recorded on this machine' end
  local recent=nil
  for _,u in ipairs(users) do
    if u.recent then
      if recent then return nil,'several accounts are marked MostRecent' end
      recent=u.id
    end
  end
  if recent then return recent,'loginusers.vdf MostRecent' end
  if #users==1 then return users[1].id,'the only recorded Steam account' end
  return nil,('ambiguous: %d Steam accounts recorded and none marked MostRecent'):format(#users)
end
-- Copy (never move) the legacy shared ledger into the account ledger so the
-- original stays on disk as a backup. Only done when the account is known.
local function migrate_legacy(path,account,note)
  if account=='' or fs.exists(path) then return note end
  local raw=read_file(LEGACY)
  if not raw then return note end
  local obj,err=decode_ledger(raw)
  if not obj then return note..'; shared ledger was not migrated ('..tostring(err)..')' end
  local ok,perr=persist(sanitize(obj,account),path)
  if not ok then return note..'; shared ledger migration failed: '..tostring(perr) end
  return note..'; migrated the shared schema-1 ledger into this account (original kept)'
end
local function remember_ack(data,sid,seq)
  if not data.acked[sid] then
    data.ackOrder[#data.ackOrder+1]=sid
    while #data.ackOrder>MAX_ACKED do
      local oldest=table.remove(data.ackOrder,1)
      data.acked[oldest]=nil
    end
  end
  data.acked[sid]=seq
end
local function day_key(offset)
  return os.date('%Y-%m-%d',os.time()-offset*DAY)
end
local function stats(data)
  local cut7,cut14,cut30=day_key(6),day_key(13),day_key(29)
  local out={last7d=0,last30d=0,prev7d=0,today=data.daily[day_key(0)] or 0}
  for k,v in pairs(data.daily) do
    if k>=cut7 then out.last7d=out.last7d+v end
    -- The seven days before last7d, for a "this week vs last week" line.
    if k>=cut14 and k<cut7 then out.prev7d=out.prev7d+v end
    if k>=cut30 then out.last30d=out.last30d+v end
  end
  return out
end
-- The last 30 days, oldest first, zero-filled: what a chart needs without
-- walking the daily map itself. Purely derived from data.daily.
local function daily_series(data)
  local out={}
  for offset=29,0,-1 do
    local day=day_key(offset)
    out[#out+1]={day=day,seconds=data.daily[day] or 0}
  end
  return json.array(out)
end
local function recent_events(data,limit)
  local out={}
  for i=math.max(1,#data.events-limit+1),#data.events do out[#out+1]=data.events[i] end
  return json.array(out)
end
function M.register(registry)
  local root=millennium.steam_path()
  local account,source=resolve_account(root)
  local ledger=LEGACY
  local note='Ledger account resolved from '..tostring(source)
  local host,mine,own_path,nonce,installation=nil,nil,nil,'',''
  local peers,recovered,conflictFiles={},{},{}
  local recovered_floor=nil
  local unmoved=nil
  local baseline=nil
  -- Notes are deduplicated and capped: a 30 s reload must not grow the reply
  -- (or its rev) by appending the same warning forever.
  local base_note=note
  local notes={}
  local function add_note(text)
    if text==nil or text=='' then return end
    for _,existing in ipairs(notes) do
      if existing==text then return end
    end
    notes[#notes+1]=text
    while #notes>8 do table.remove(notes,1) end
    note=base_note..(#notes>0 and ('. '..table.concat(notes,'. ')) or '')
  end
  local scoped=account~=nil
  if scoped then
    ledger=DIR..'/playtime/'..account..'.json'
    note=migrate_legacy(ledger,account,note)
  else
    note=tostring(source)..'; using the shared legacy ledger'
  end
  local data,loadnote=read_state(ledger,account or '',scoped)
  if loadnote~='' then add_note(loadnote) end
  baseline=data
  if scoped then
    local hsource,machine,moved
    host,hsource,nonce,machine,installation,moved=host_id()
    if moved then
      add_note('this machine\'s saved identity came from another computer; minted a new installation id')
    end
    own_path=DIR..'/playtime/'..account..'.'..host..'-'..nonce..'.json'
    local own,ownnote,refused=read_own(own_path,account,host,nonce,installation)
    if refused then
      -- A foreign contribution could not be moved aside; never rotate it into
      -- .bak. Give this writer a different file and report the leftover.
      own_path=DIR..'/playtime/'..account..'.'..host..'-'..nonce..'-own.json'
      unmoved=refused:match('[^/]+$') or refused
      own,ownnote=read_own(own_path,account,host,nonce,installation)
    end
    mine=own or blank_own(account,host,nonce,installation)
    mine.host=host
    mine.nonce=nonce
    mine.file=own_path:match('[^/]+$') or own_path
    -- A file that predates fingerprints keeps content identity for this
    -- session, so an identical copy elsewhere is counted once, not twice.
    if own then
      mine.installation=own.installation
    else
      mine.installation=installation
    end
    if ownnote~='' then add_note(ownnote) end
    local dropped
    dropped=apply_highwater(baseline,account,host,nonce)
    if dropped then
      add_note('the shared ledger went backwards on some counters; kept the recorded high-water mark')
    end
    recovered_floor=nil
    note,recovered,conflictFiles,recovered_floor=migrate_merge(account,baseline,note)
    if #recovered>0 then
      add_note('recovered '..#recovered..' pulse(s) from a sync-conflict copy of the shared ledger')
    end
    if #conflictFiles>0 then
      add_note('kept '..#conflictFiles..' sync-conflict cop'..
        (#conflictFiles==1 and 'y' or 'ies')..
        ' of the shared ledger (its aggregated totals cannot be split without guessing)')
    end
  end
  -- Ledger model change (2026-10-15): for games Steam itself tracks, the base
  -- is now the official lifetime playtime from localconfig.vdf, so any old
  -- since-inception adjustment is retired once. The pre-change bytes are kept
  -- beside the ledger; a fresh ledger is born on the new model already.
  -- In schema 3 the baseline is read-only, so the retirement is expressed as
  -- a lowest-priority edit (at=0) that any real edit still beats.
  local official=official_playtimes(root,data.account)
  local hasOfficial=false
  for _ in pairs(official) do hasOfficial=true; break end
  if hasOfficial and tonumber(data.officialBase or 0)~=OFFICIAL_BASE then
    local retired=0
    for key,record in pairs(data.games) do
      if (official[key] or 0)>0 and math.abs(record.adjustmentSeconds or 0)>1 then
        retired=retired+1
        if scoped then
          if not mine.edits[key] then
            mine.edits[key]={at=0,host='',adjustmentSeconds=0}
          end
        else
          record.adjustmentSeconds=0
        end
      end
    end
    if scoped then
      if retired>0 then
        local saved,migerr=persist(mine,own_path)
        if not saved then note=note..'. Could not persist the official-base migration: '..tostring(migerr) end
        note=note..('. Retired '..retired..' legacy adjustment(s) where Steam owns the total')
      end
    else
      data.officialBase=OFFICIAL_BASE
      if retired>0 then
        if not fs.exists(ledger..'.pre-official') then copy_file(ledger,ledger..'.pre-official') end
        note=note..('. Retired '..retired..' legacy adjustment(s) where Steam owns the total')
      end
      local saved,migerr=persist(data,ledger)
      if not saved then note=note..'. Could not persist the official-base migration: '..tostring(migerr) end
    end
  end
  -- Every read of the merged view goes through here. Peer files are parsed
  -- once and then cached by content, so the 1 s poll does not re-decode
  -- megabytes and same-size rewrites cannot hide behind coarse timestamps.
  local peer_cache={}
  local fingerprints={}
  local peerNotes,nameMismatch={},{}
  local duplicateHost=false
  local peerSignature=''
  local function file_state(path)
    local f=io.open(path,'rb')
    if not f then return 'missing' end
    local size=f:seek('end') or -1
    local mtime=0
    if LFS and type(LFS.attributes)=='function' then
      local ok,attr=pcall(LFS.attributes,path)
      if ok and type(attr)=='table' then mtime=tonumber(attr.modification) or 0 end
    end
    f:seek('set',0)
    -- Bounded like read_file: an oversized file is rejected there anyway, so
    -- its bytes are never read in full or retained here.
    local raw=f:read(MAX_BYTES+1) or ''
    f:close()
    if #raw>MAX_BYTES then raw=false end
    local old=fingerprints[path]
    local version=old and old.version or 0
    if not old or old.raw~=raw then version=version+1 end
    fingerprints[path]={raw=raw,version=version}
    -- The bytes are returned too, so the caller parses exactly what was
    -- fingerprinted (a second read could see a newer write).
    return tostring(size)..':'..tostring(mtime)..':'..version, raw
  end
  -- A newer backup import supersedes every machine's earlier contribution (it
  -- is a replace-all by design; the RPC already refuses to run during a live
  -- session). Each machine zeroes its own counters the first time it sees one,
  -- so pre-import time is never added twice and later sessions count from 0.
  local function absorb_import()
    if not scoped then return end
    local best=nil
    local function consider(c)
      if c.import and not c.import.legacy
        and (not best or wins(c.import.at,c.import.host,best.at,best.host)) then
        best=c.import
      end
    end
    consider(mine)
    for _,peer in ipairs(peers) do consider(peer) end
    if best and best.at>(mine.resetAt or 0) then
      mine.games,mine.daily,mine.sessions,mine.edits={},{},{},{}
      mine.resetAt=best.at
    end
  end
  local function reload_peers()
    local list,index,seen={},{},{}
    peerNotes,nameMismatch={},{}
    duplicateHost=false
    if scoped then
      for _,file in ipairs(contribution_files(account)) do
        seen[file.path]=true
        local state,content=file_state(file.path)
        index[#index+1]=file.name..'='..state
        if file.path~=own_path then
          local cached=peer_cache[file.path]
          local peer=nil
          if cached and cached.state==state then
            peer=cached.peer
            if cached.problem then peerNotes[#peerNotes+1]=cached.problem end
            if cached.mismatch then nameMismatch[#nameMismatch+1]=cached.mismatch end
          else
            local problem=nil
            local raw=type(content)=='string' and #content>0 and content or nil
            if raw then
              local ok,obj=pcall(json.decode,raw)
              if ok then peer=sanitize_own(obj,account) end
              if not peer then problem=file.name..' (unreadable contribution)' end
            else
              problem=file.name..' (unreadable contribution)'
            end
            if peer then
              -- Identity is what the file says, not what it is called. A
              -- copied file is reported when the two disagree and counted
              -- once, under its declared identity.
              local mismatch=nil
              local accept=file.token==peer.host
                or (peer.nonce and file.token==peer.host..'-'..peer.nonce)
              if not accept then
                mismatch=('file %s declares %s'):format(file.name,peer.host)
                nameMismatch[#nameMismatch+1]=mismatch
                peerNotes[#peerNotes+1]=mismatch
              end
              peer.file=file.name
              peer_cache[file.path]={state=state,peer=peer,mismatch=mismatch}
            else
              peer_cache[file.path]={state=state,problem=problem}
              peerNotes[#peerNotes+1]=problem
            end
          end
          if peer then
            peer.file=file.name
            if peer.host==host and peer.nonce and peer.nonce~=nonce then duplicateHost=true end
            list[#list+1]=peer
          end
        end
      end
    end
    -- Forget files that are gone, so deleted peers release their bytes and a
    -- recreated path never reuses a stale entry.
    for path in pairs(fingerprints) do if not seen[path] then fingerprints[path]=nil end end
    for path in pairs(peer_cache) do if not seen[path] then peer_cache[path]=nil end end
    table.sort(index)
    peerSignature=table.concat(index,';')
    peers=list
    absorb_import()
  end
  reload_peers()
  -- A recovered pulse is only added while the live baseline does not already
  -- account for it. The 200-event ring is not enough (an old writer rolls it
  -- over), so the marker also stores the per-game/per-day floor seen at
  -- migration: when the baseline's aggregate has grown past floor+recovered,
  -- the pulse has been folded in and must not be added again.
  local function active_recovered()
    local out={}
    if #recovered==0 then return out end
    local floor_games=recovered_floor and recovered_floor.games or {}
    local floor_days=recovered_floor and recovered_floor.daily or {}
    local per_game,per_day={},{}
    for _,e in ipairs(recovered) do
      local k=tostring(e.id)
      per_game[k]=(per_game[k] or 0)+e.seconds
      if e.day then per_day[e.day]=(per_day[e.day] or 0)+e.seconds end
    end
    local ring={}
    for _,e in ipairs(baseline.events) do
      ring[e.id..'|'..e.endedAt..'|'..e.seconds]=true
    end
    for _,e in ipairs(recovered) do
      local k=tostring(e.id)
      local contained=ring[e.id..'|'..e.endedAt..'|'..e.seconds] and true or false
      local floor=tonumber(floor_games[k])
      if not contained and floor then
        local tracked=(baseline.games[k] and baseline.games[k].trackedSeconds) or 0
        if tracked>=floor+per_game[k] then contained=true end
      end
      if not contained and e.day and floor_days[e.day] then
        if (baseline.daily[e.day] or 0)>=floor_days[e.day]+per_day[e.day] then
          contained=true
        end
      end
      if not contained then out[#out+1]=e end
    end
    return out
  end
  local last_signature=nil
  local cloneSuspect=false
  local cloneFileList={}
  local function rebuild()
    if scoped then
      local files
      data,files=merge_view(baseline,mine,peers,active_recovered(),account or baseline.account)
      cloneFileList=files or {}
      cloneSuspect=#cloneFileList>0
      -- Session bookkeeping is this machine's own: an "active" session from
      -- the other machine cannot be verified here, and its pulses arrive as
      -- ordinary contributions.
      data.active=mine.active
      data.acked=mine.acked
      data.ackOrder=mine.ackOrder
      data.officialBase=baseline.officialBase
    end
    last_signature=peerSignature
  end
  rebuild()
  -- The baseline is the one file an old add-on version can still rewrite. It
  -- is folded in on a slow beat (same TTL as the library inventory) so those
  -- sessions appear without a restart, but the 1 s poll never re-parses 65 KB.
  local baseline_at=os.time()
  local function reload_baseline()
    if not scoped or os.time()-baseline_at<30 then return end
    baseline_at=os.time()
    local fresh,why=read_state(ledger,account,true)
    baseline=fresh
    if why~='' then add_note(why) end
    if apply_highwater(baseline,account,host,nonce) then
      add_note('the shared ledger went backwards on some counters; kept the recorded high-water mark')
    end
    rebuild()
  end
  local function merge_info()
    local seen,count={},0
    if scoped then
      seen[host]=true;count=1
      for _,peer in ipairs(peers) do
        if not seen[peer.host] then seen[peer.host]=true;count=count+1 end
      end
    end
    return {host=host or '',hosts=count,nonce=mine and mine.nonce or '',
      installation=mine and mine.installation or '',unmoved=unmoved or '',
      duplicateHostId=(duplicateHost or cloneSuspect) and true or false,
      duplicateFiles=json.array(cloneFileList),
      conflictFiles=json.array(conflictFiles),recovered=#recovered,
      unreadable=json.array(peerNotes),nameMismatch=json.array(nameMismatch)}
  end
  local runtime={healthy=false,error='',reportedAt=0,note=note,
    file=(ledger:match('[^/]+$') or ledger)}

  -- Two different questions, so two different sets:
  --   * trackable - non-Steam library shortcuts. Nothing else records their
  --     playtime, so the agent must count sessions while they run.
  --   * editable  - every game the panel may show and edit: shortcuts plus the
  --     installed Steam library (steamapps/appmanifest_*.acf across every
  --     steamapps/libraryfolders.vdf root; see steam_library above). Steam
  --     itself already records playtime for those apps, so they are
  --     deliberately edit-only and are never auto-counted.
  --     SLS_PLAYTIME_TRACK_MANIFESTS=1 restores counting them.
  -- Counted: installed Steam manifests + non-Steam shortcuts. Nothing else.
  local inventory={ts=0,ids={},trackIds={},trackSet={},editSet={},
    manifestCount=0,shortcutCount=0,shortcutNames={},libraryError=''}
  local function refresh_inventory()
    local now=os.time()
    if now-inventory.ts<30 then return inventory end
    local editset,trackids={},{}
    local libraryError=''
    if root~='' then
      local installed,why=steam_library(root)
      libraryError=why or ''
      for _,entry in ipairs(installed) do
        local id=tonumber(entry.appid)
        if valid_id(id) then editset[id]=true end
      end
    end
    local shortcutNames={}
    if root~='' then shortcutNames=shortcut_inventory(root,data.account) end
    local shortcutCount=0
    local trackset={}
    for key in pairs(shortcutNames) do
      local id=tonumber(key)
      -- A numeric manifest always wins: an AppID cannot be both a Steam game
      -- and a non-Steam shortcut, and the manifest is the authoritative one.
      if id and not editset[id] then
        shortcutCount=shortcutCount+1
        editset[id]=true
        trackset[id]=true
        trackids[#trackids+1]=id
      end
    end
    local manifestCount=0
    for id in pairs(editset) do
      if not trackset[id] then manifestCount=manifestCount+1 end
    end
    if os.getenv('SLS_PLAYTIME_TRACK_MANIFESTS')=='1' then
      trackids={}
      trackset={}
      for id in pairs(editset) do trackset[id]=true;trackids[#trackids+1]=id end
      manifestCount=0
    end
    local ids={}
    for id in pairs(editset) do ids[#ids+1]=id end
    inventory={ts=now,ids=ids,trackIds=trackids,trackSet=trackset,editSet=editset,
      manifestCount=manifestCount,shortcutCount=shortcutCount,shortcutNames=shortcutNames,
      libraryError=libraryError}
    return inventory
  end
  local function manifests()
    -- The Steam manifest ids on their own; used for reporting and by tests.
    local snap=refresh_inventory()
    local ids={}
    for id in pairs(snap.editSet) do
      if not snap.trackSet[id] then ids[#ids+1]=id end
    end
    return ids
  end
  -- Auto-counting is limited to what nothing else records.
  local function allowed(id)
    return refresh_inventory().trackSet[id]==true
  end
  -- Editing and naming are offered for every trackable or manifest game.
  local function editable(id)
    return refresh_inventory().editSet[id]==true
  end
  -- Everything that can change what the page must redraw, cheap to compute:
  -- peer file stamps (rename/new/rewrite), the inventory, merge warnings and
  -- the note. Paired with the content hash in revision().
  local function rev_key()
    local snap=refresh_inventory()
    return table.concat({peerSignature,tostring(#snap.ids),tostring(#snap.trackIds),
      tostring(#snap.shortcutNames),tostring(duplicateHost),tostring(#nameMismatch),
      tostring(#peerNotes),tostring(#conflictFiles),tostring(unmoved and 1 or 0),
      tostring(#cloneFileList)},'|')
  end
  -- Recorded time always lands in this machine's own contribution; the merged
  -- view (data) is rebuilt from it, never written back to the shared file.
  local function game(id)
    local k=tostring(id)
    if not scoped then return own_record(data.games,k) end
    return own_record(mine.games,k)
  end
  local function own_daily()
    return scoped and mine.daily or data.daily
  end
  local function log_pulse(id,seconds,endedAt)
    local list=scoped and mine.sessions or data.events
    list[#list+1]={id=id,seconds=seconds,endedAt=endedAt}
    if #list>(scoped and MAX_SESSIONS or MAX_EVENTS) then table.remove(list,1) end
  end
  -- A failed atomic rename must not count playtime in RAM. Otherwise the client
  -- retries a failed pulse and the next successful save counts it twice.
  -- Snapshot before mutation; the ledger is bounded, and durable correctness is
  -- worth more than the encode/decode of a few hundred kilobytes per pulse.
  local function transaction(fn)
    local target=scoped and mine or data
    local ok,snapshot=pcall(json.encode,target)
    if not ok then return false,snapshot end
    local success,err=pcall(fn)
    if success then
      local saved,why=persist(target,scoped and own_path or ledger)
      if saved then
        reload_peers()
        rebuild()
        return true
      end
      err=why
    end
    -- Restore even when the callback or the underlying fs shim throws.
    local restored,original=pcall(json.decode,snapshot)
    if restored and type(original)=='table' then
      if scoped then
        mine=original
        mine.host=host
      else
        data=sanitize(original,data.account,true)
      end
    end
    rebuild()
    return false,err
  end
  registry.SlsPlaytimeReport=function(raw)
    if type(raw)~='string' or #raw>2000 then return reply({success=false,error='Invalid report'}) end
    local ok,obj=pcall(json.decode,raw)
    if not ok or type(obj)~='table' then return reply({success=false,error='Malformed report'}) end
    runtime.healthy=obj.healthy==true
    runtime.error=tostring(obj.error or ''):sub(1,250)
    runtime.reportedAt=os.time()
    -- Reject a report from a different account instead of filing its time here.
    local reported=obj.account
    if type(reported)=='string' and reported:match('^%d+$') and #reported>=16 and #reported<=20
      and data.account~='' and reported~=data.account then
      runtime.healthy=false
      runtime.error='Account mismatch: Steam reports '..reported..', this ledger belongs to '..data.account
      return reply({success=false,error=runtime.error})
    end
    return reply({success=true})
  end
  -- JS side sends elapsed seconds computed from monotonic+wall time on each
  -- heartbeat. Persist at most 30s at a time and never treat a missing
  -- heartbeat as playing forever.
  registry.SlsPlaytimeHeartbeat=function(raw)
    if type(raw)~='string' or #raw>10000 then return reply({success=false,error='Invalid heartbeat'}) end
    local ok,obj=pcall(json.decode,raw)
    if not ok or type(obj)~='table' or not valid_id(obj.id) then return reply({success=false,error='Invalid AppID'}) end
    local id=tonumber(obj.id);local k=tostring(id)
    if obj.kind~='start' and obj.kind~='pulse' and obj.kind~='stop' then
      return reply({success=false,error='Invalid heartbeat kind'})
    end
    local sid=obj.sid
    if type(sid)~='string' or #sid<1 or #sid>64 or not sid:match('^[%w%-%._]+$') then
      return reply({success=false,error='Invalid session identifier'})
    end
    local seq=0
    if obj.kind~='start' then
      seq=tonumber(obj.seq)
      if not seq or seq~=math.floor(seq) or seq<1 or seq>1e15 then
        return reply({success=false,error='Invalid heartbeat sequence'})
      end
    end
    if obj.kind=='start' and not allowed(id) then
      return reply({success=false,error='Not a manifest game'})
    end
    local elapsed=0
    if obj.kind~='start' then
      elapsed=tonumber(obj.elapsed)
      if not elapsed or elapsed<0 or elapsed>MAX_PULSE or elapsed~=elapsed then
        return reply({success=false,error='Invalid heartbeat duration'})
      end
      -- A backwards wall clock (or a frozen page after suspend) must not
      -- credit a phantom interval. The per-pulse cap above already bounds an
      -- over-count at 30 s; this only removes the impossible case.
      local current=data.active[k]
      local started=current and current.timestamp or os.time()
      if os.time()-started<0 then elapsed=0 end
    end
    -- Idempotent replay. The ledger already committed this sequence, so the
    -- response was lost on the way back to the page; report success and do not
    -- count the interval a second time.
    if obj.kind~='start' then
      local current=data.active[k]
      if not current then
        if data.acked[sid] then return reply({success=true,replayed=true}) end
        return reply({success=false,error='No active session',code='no_session'})
      end
      if current.sid~=sid then
        if data.acked[sid] then return reply({success=true,replayed=true}) end
        return reply({success=false,error='Session was replaced',code='no_session'})
      end
      if seq<=current.seq then return reply({success=true,replayed=true}) end
    end
    local saved,err=transaction(function()
      if obj.kind=='start' then
        local current=data.active[k]
        -- Keep the committed sequence on a replayed start. Resetting it would
        -- make the next pulse look new and count an already counted interval.
        if not current or current.sid~=sid then data.active[k]={sid=sid,seq=0,timestamp=os.time()} end
        local entry=game(id)
        if type(obj.title)=='string' and #obj.title<=160 and obj.title~='' then entry.title=obj.title end
        entry.lastPlayed=os.time()
        return
      end
      local current=data.active[k]
      local entry=game(id)
      entry.trackedSeconds=math.min(MAX_SECONDS,entry.trackedSeconds+elapsed)
      entry.lastPlayed=os.time()
      local day=day_key(0)
      local daily=own_daily()
      daily[day]=math.min(MAX_SECONDS,(daily[day] or 0)+elapsed)
      if elapsed>0 then log_pulse(id,elapsed,os.time()) end
      current.seq=seq
      if obj.kind=='stop' then
        data.active[k]=nil
        remember_ack(data,sid,seq)
      end
    end)
    return reply({success=saved,error=err})
  end
  registry.SlsPlaytimeTitles=function(raw)
    if type(raw)~='string' or #raw>400000 then return reply({success=false,error='Invalid name list'}) end
    local ok,obj=pcall(json.decode,raw)
    if not ok or type(obj)~='table' or type(obj.titles)~='table' then
      return reply({success=false,error='Malformed name list'})
    end
    local incoming,count={},0
    for k,v in pairs(obj.titles) do
      count=count+1
      if count>MAX_TITLES then return reply({success=false,error='Too many names'}) end
      -- Names are accepted for anything the panel can show and edit.
      if not valid_id(k) or type(v)~='string' or v=='' or #v>160 or not editable(tonumber(k)) then
        return reply({success=false,error='Invalid name entry'})
      end
      incoming[tostring(tonumber(k))]=v
    end
    if next(incoming)==nil then return reply({success=true,unchanged=true}) end
    local changed=false
    for k,v in pairs(incoming) do
      if data.titles[k]~=v then changed=true;break end
    end
    if not changed then return reply({success=true,unchanged=true}) end
    local saved,err=transaction(function()
      if scoped then
        -- Names are cosmetic and rebuildable; the (at, host) pair decides the
        -- winner so both machines show the same name.
        local at=os.time()
        for k,v in pairs(incoming) do mine.titles[k]={v=v,at=at} end
      else
        for k,v in pairs(incoming) do data.titles[k]=v end
      end
    end)
    return reply({success=saved,error=err})
  end
  registry.SlsPlaytimeGet=function(raw)
    -- Re-read the other machines' contributions first: a light poll with the
    -- previous rev must see a session they wrote since the last call.
    reload_peers()
    reload_baseline()
    if peerSignature~=last_signature then rebuild() end
    runtime.note=note
    local rev=revision(data,rev_key())
    local want=nil
    if type(raw)=='string' and #raw<=200 then
      want=raw:match('"rev"%s*:%s*"([%w%-_]+)"')
    end
    if want and want==rev then
      -- Nothing changed: the page polls every second, and the full reply is
      -- ~65 KB. The caller keeps its last full snapshot.
      return reply({success=true,unchanged=true,rev=rev,schema=SCHEMA})
    end
    -- `events` is deliberately not part of this payload: a full pulse history
    -- is megabytes, and the panel only needs totals plus a short diagnostic
    -- tail. SlsPlaytimeExport still returns the complete record for backups.
    local snap=refresh_inventory()
    -- Wire copy: officialSeconds rides along for display only and is never
    -- persisted, so the ledger stays the source of truth for our own counts
    -- while every surface can agree with Steam's own number.
    local official=official_playtimes(root,data.account)
    local wire={}
    for key,record in pairs(data.games) do
      local copy={title=record.title,trackedSeconds=record.trackedSeconds,
        adjustmentSeconds=record.adjustmentSeconds,lastPlayed=record.lastPlayed}
      if official[key] then copy.officialSeconds=official[key] end
      wire[key]=copy
    end
    return reply({success=true,games=wire,daily30=daily_series(data),active=data.active,
      apps=json.array(snap.ids),trackedApps=json.array(snap.trackIds),
      stats=stats(data),account=data.account,rev=rev,merge=merge_info(),
      runtime=runtime,schema=SCHEMA,recent=recent_events(data,20),titles=data.titles,
      manifestCount=snap.manifestCount,shortcutCount=snap.shortcutCount,
      shortcutNames=snap.shortcutNames,libraryError=snap.libraryError,
      policy={trackManifests=os.getenv('SLS_PLAYTIME_TRACK_MANIFESTS')=='1'}})
  end
  registry.SlsPlaytimeEdit=function(raw)
    if type(raw)~='string' then return reply({success=false,error='Invalid value'}) end
    local ok,obj=pcall(json.decode,raw)
    if not ok or type(obj)~='table' or not valid_id(obj.id) then return reply({success=false,error='Invalid AppID'}) end
    if not editable(tonumber(obj.id)) then return reply({success=false,error='Not a manifest game or non-Steam shortcut'}) end
    local hours=tonumber(obj.hours)
    if not hours or hours<0 or hours>10000000 or hours~=hours then return reply({success=false,error='Invalid hours'}) end
    local saved,err=transaction(function()
      -- For games Steam tracks itself the correction is anchored to Steam's
      -- own lifetime number: the edit sets the displayed total, Steam's
      -- counter keeps running underneath, and both stay in agreement.
      local k=tostring(math.floor(tonumber(obj.id)))
      local official=official_playtimes(root,data.account)[k]
      local recorded=(data.games[k] and data.games[k].trackedSeconds) or 0
      local base=official or recorded
      if scoped then
        -- A stamped edit, not a rewritten total: the whole shared directory
        -- picks the same winner by (at, host), so both machines show the same
        -- number and last-writer-wins cannot silently drop the other edit.
        mine.edits[k]={at=os.time(),host=host,adjustmentSeconds=hours*3600-base}
      else
        local entry=game(tonumber(obj.id))
        entry.adjustmentSeconds=hours*3600-base
      end
    end)
    return reply({success=saved,error=err})
  end
  registry.SlsPlaytimeExport=function()
    return reply({success=true,data={version=SCHEMA,account=data.account,games=data.games,
      events=json.array(data.events),daily=data.daily,titles=data.titles,stats=stats(data),
      exportedAt=os.time()}})
  end
  registry.SlsPlaytimeImport=function(raw)
    -- Never replace the ledger while an agent is accumulating sessions; the
    -- agent and the backend would disagree about what is already counted.
    if next(data.active)~=nil then
      return reply({success=false,error='Stop all tracked games before importing a backup'})
    end
    if type(raw)~='string' or #raw>5000000 then return reply({success=false,error='Oversized or invalid backup'}) end
    local ok,obj=pcall(json.decode,raw)
    if not ok or type(obj)~='table' then return reply({success=false,error='Unsupported backup schema'}) end
    if obj.version~=1 and obj.version~=SCHEMA then return reply({success=false,error='Unsupported backup schema'}) end
    if type(obj.games)~='table' then return reply({success=false,error='Backup has no game records'}) end
    if obj.events~=nil and type(obj.events)~='table' then return reply({success=false,error='Backup has an invalid event list'}) end
    if obj.daily~=nil and type(obj.daily)~='table' then return reply({success=false,error='Backup has an invalid daily list'}) end
    -- Refuse to mix two accounts by accident. Copying hours between accounts is
    -- possible, but it has to be asked for explicitly.
    local declared=obj.account
    if type(declared)=='string' and declared~='' and data.account~='' and declared~=data.account
      and obj.allowAccountMismatch~=true then
      return reply({success=false,error='Backup belongs to Steam account '..declared
        ..', this ledger belongs to '..data.account..'. Import it with allowAccountMismatch to copy it deliberately.'})
    end
    -- A backup is a point-in-time snapshot: it must never resurrect a session.
    local replacement=sanitize(obj,data.account)
    replacement.version=SCHEMA
    if data.account~='' then replacement.account=data.account end
    if scoped then
      -- Expressed as this machine's one import record rather than as a
      -- rewritten shared total: the whole directory picks the same import by
      -- (at, host), so both machines converge on the same numbers.
      local exported_at=finite(obj.exportedAt,0,1e12)
      local record={at=exported_at or 0,host=host,legacy=exported_at==nil,games={},daily={}}
      for k,v in pairs(replacement.games) do
        record.games[k]={trackedSeconds=v.trackedSeconds,adjustmentSeconds=v.adjustmentSeconds}
      end
      for k,v in pairs(replacement.daily) do record.daily[k]=v end
      mine.import=record
      local import_note=nil
      if record.legacy then
        -- Older exports carry no export time, so nothing can prove what they
        -- already contain. Keep this machine's counters: merge_view adds them
        -- on top of the imported floor, which may double count a pre-export
        -- session but never loses one.
        import_note='Backup has no export time; only the excess over the current ledger is added, and recorded play is never hidden'
      else
        mine.resetAt=record.at
        mine.games,mine.daily,mine.sessions,mine.edits={},{},{},{}
      end
      local saved,err=persist(mine,own_path)
      if saved then
        reload_peers()
        rebuild()
        runtime.error=''
        return reply({success=true,note=import_note})
      end
      return reply({success=false,error=err})
    end
    local saved,err=persist(replacement,ledger)
    if saved then
      data=replacement
      runtime.error=''
      return reply({success=true})
    end
    return reply({success=false,error=err})
  end
  return M
end
return M
