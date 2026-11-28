-- Mocked Lua backend integration test: persistent state, RPC surface and guards.
-- Only run with HOME pointing to a disposable tmpdir (enforced by runner).
assert(os.getenv('SLS_TEST_HOME') == os.getenv('HOME'), 'Test must run in isolated HOME')
local contents = {}
local n = 0
package.preload['json'] = function()
  return {
    encode=function(t)
      n=n+1
      contents[n]=t
      return 'mock-json:'..tostring(n)
    end,
    decode=function(s)
      local i=tonumber((s or ''):match('^mock%-json:(%d+)$'))
      assert(i and contents[i], 'invalid fake JSON')
      return contents[i]
    end,
  }
end
package.preload['fs'] = function()
  return {
    create_directories=function(dir)
      local success=os.execute('mkdir -p '..string.format('%q',dir))
      assert(success==true or success==0)
      return true
    end,
    exists=function(path)
      return path:match('loginusers%.vdf$')~=nil
    end,
    join=function(...) return table.concat({...},'/') end,
    list=function(path)
      -- The installed-library scan: appmanifest_<appid>.acf under steamapps.
      assert(path:find('steamapps$'), 'unexpected listing: '..tostring(path))
      return { {name='appmanifest_620.acf',is_directory=false},
        {name='appmanifest_621.acf.disabled',is_directory=false},
        {name='libraryfolders.vdf',is_directory=false} }
    end,
  }
end
package.preload['millennium'] = function()
  return {steam_path=function() return os.getenv('HOME')..'/steam' end}
end
local json=require('json')
-- The account identity comes from Steam's config/loginusers.vdf. Write a
-- realistic fixture so the digests in the assertions are the real ones the JS
-- agent computes for the same SteamID64 (the two implementations must agree).
local steam_root=os.getenv('HOME')..'/steam'
assert(os.execute('mkdir -p '..string.format('%q',steam_root..'/config')))
do
  local f=assert(io.open(steam_root..'/config/loginusers.vdf','wb'))
  f:write([[
"users"
{
	"76561198000000000"
	{
		"AccountName"		"testaccount"
		"PersonaName"		"LePapot"
		"MostRecent"		"1"
		"Timestamp"		"1791884517"
	}
}
]])
  f:close()
end
local function call(registry, name, arg)
  assert(type(registry[name])=='function', 'missing RPC: '..name)
  local raw=registry[name](arg)
  return json.decode(raw)
end
local reg={}
local plugin=assert(loadfile('addons/presence/plugin/backend/main.lua'))()
assert(type(plugin)=='table' and type(plugin.register)=='function')
plugin.register(reg)
  -- The account identity is derived from loginusers.vdf and must match the JS
  -- FNV-1a implementation byte for byte (see tests/presence-lifecycle.test.cjs).
  local ident=call(reg,'SlsPresenceGet').identity
  assert(ident, 'identity missing')
  assert(ident.key=='5bda05e3', 'identity digest mismatch: '..tostring(ident.key))
  assert(ident.source=='loginusers', 'identity source must be reported')
  assert(ident.known==1 and ident.ambiguous==false, 'single account must not be ambiguous')
  -- Discord Rich Presence: name resolution and IPC framing (no socket opened).
  assert(plugin.acf_name('"AppState"\n{\n\t"appid"\t\t"3405340"\n\t"name"\t\t"Megabonk"\n}')=='Megabonk', 'acf name')
  local function reader(path)
    return path=='/lib/appmanifest_3405340.acf' and '"name"\t"Megabonk"' or nil
  end
  assert(plugin.resolve_game_name(3405340,{},{'/nope','/lib'},reader)=='Megabonk', 'manifest name')
  assert(plugin.resolve_game_name(3405340,{['3405340']='Custom'},{'/lib'},reader)=='Custom', 'titles map wins')
  assert(plugin.resolve_game_name(1,{},{'/lib'},reader)==nil and plugin.resolve_game_name('x',{},{},reader)==nil, 'unknown app')
  local paths=plugin.discord_socket_paths(function(k) return k=='XDG_RUNTIME_DIR' and '/run/user/1000' or nil end)
  assert(paths[1]=='/run/user/1000/discord-ipc-0' and paths[2]=='/run/user/1000/app/com.discordapp.Discord/discord-ipc-0', 'socket paths')
  assert(#plugin.discord_socket_paths(function() return nil end)==0, 'no runtime dir')
  local function unframe(frame)
    local op,len=string.unpack('<I4I4',frame)
    assert(#frame==8+len, 'frame length')
    return op,json.decode(frame:sub(9))
  end
  local op,hs=unframe(plugin.discord_handshake(123456789012345678))
  assert(op==0 and hs.v==1 and hs.client_id=='123456789012345678', 'handshake')
  local op2,act=unframe(plugin.discord_set_activity(4242,'Megabonk',1700000000,'n1'))
  assert(op2==1 and act.cmd=='SET_ACTIVITY' and act.args.pid==4242 and act.nonce=='n1', 'set activity')
  assert(act.args.activity.name=='Megabonk' and act.args.activity.type==0
    and act.args.activity.timestamps.start==1700000000, 'activity payload')
  local _,cleared=unframe(plugin.discord_set_activity(4242,nil,nil,'n2'))
  assert(cleared.args.activity==nil, 'clear activity')
  local plan=call(reg,'SlsPresenceDiscordPlan',json.encode({appid=620}))
  assert(plan.needsClientId==true and type(plan.sockets)=='table', 'plan must report the missing client id')
  -- Discord session state machine against an injected fake transport.
  do
    local sent_ops, alive_conn = {}, nil
    local function frame(op, tbl) local b=json.encode(tbl); return string.pack('<I4I4',op,#b)..b end
    local function mkconn(up, readyframe)
      local c={fd=-1,queue={},closed=false,timeout=0}
      function c:settimeout(t) self.timeout=t end
      function c:getfd() return self.fd end
      function c:connect() if not up.on then return nil,'refused' end self.fd=7 return 1 end
      function c:close() self.closed=true self.fd=-1 end
      function c:send(data)
        if self.closed then return nil,'closed' end
        local op,len=string.unpack('<I4I4',data)
        local body=json.decode(data:sub(9,8+len))
        sent_ops[#sent_ops+1]={op=op,body=body}
        if op==0 then self.queue[#self.queue+1]=readyframe end
        -- Discord answers SET_ACTIVITY with the same nonce; presence only
        -- reports connected once it has that acknowledgement.
        if op==1 and type(body)=='table' and body.cmd=='SET_ACTIVITY' and not self.silent then
          self.queue[#self.queue+1]=frame(1,{cmd='SET_ACTIVITY',nonce=body.nonce,data={}})
        end
        return #data
      end
      function c:receive(n)
        local q=self.queue
        if self.dead then return nil,'closed' end
        if #q==0 then return nil,'timeout' end
        local cur=q[1]
        if #cur<n then return nil,'timeout' end
        q[1]=cur:sub(n+1); local out=cur:sub(1,n)
        if #q[1]==0 then table.remove(q,1) end
        return out
      end
      return c
    end
    local up={on=false}
    local ready=frame(1,{cmd='DISPATCH',evt='READY'})
    local deps={paths=function() return {'/fake/ipc-0'} end, pid=function() return 4242 end,
      unix=function() alive_conn=mkconn(up,ready); return alive_conn end}
    local S=plugin.new_discord_session(deps)
    local game={appid=620,name='Portal 2'}
    assert(S.step({enabled=true,app_id=''},game,100)=='needs_app_id', 'empty app id must report needs_app_id')
    assert(#sent_ops==0, 'nothing may be sent without an app id')
    assert(S.step({enabled=true,app_id='123456789012345678'},nil,100)=='idle', 'no game is idle')
    assert(S.step({enabled=true,app_id='123456789012345678'},game,100)=='no_discord', 'unreachable Discord')
    assert(S.step({enabled=true,app_id='123456789012345678'},game,105)=='no_discord' and #sent_ops==0, 'backoff must hold')
    up.on=true
    assert(S.step({enabled=true,app_id='123456789012345678'},game,120)=='connected', 'must connect after backoff')
    assert(sent_ops[1].op==0 and sent_ops[1].body.client_id=='123456789012345678', 'handshake with app id')
    assert(sent_ops[2].op==1 and sent_ops[2].body.cmd=='SET_ACTIVITY', 'activity sent')
    local act=sent_ops[2].body.args
    assert(act.pid==4242 and act.activity.name=='Portal 2' and act.activity.details=='Portal 2'
      and act.activity.type==0 and act.activity.timestamps.start==100, 'activity payload')
    local n=#sent_ops
    assert(S.step({enabled=true,app_id='123456789012345678'},game,125)=='connected' and #sent_ops==n, 'steady state sends nothing')
    alive_conn.queue[#alive_conn.queue+1]=frame(3,{nonce='x'})
    S.step({enabled=true,app_id='123456789012345678'},game,130)
    assert(sent_ops[#sent_ops].op==4, 'ping must be answered with pong')
    alive_conn.dead=true -- Discord restarted
    up.on=false
    assert(S.step({enabled=true,app_id='123456789012345678'},game,135)=='no_discord', 'dead connection detected')
    up.on=true
    assert(S.step({enabled=true,app_id='123456789012345678'},game,160)=='connected', 'reconnects and resends')
    assert(sent_ops[#sent_ops].body.cmd=='SET_ACTIVITY' and sent_ops[#sent_ops].body.args.activity, 'activity resent')
    local conn=alive_conn
    assert(S.step({enabled=true,app_id='123456789012345678'},nil,170)=='idle', 'game stop is idle')
    assert(sent_ops[#sent_ops].body.args.activity==nil and conn.closed, 'clear activity then close')
    assert(S.step({enabled=false,app_id='123456789012345678'},game,200)=='idle' and #sent_ops>0, 'disabled never connects')
    -- RPCs and the host tick hook
    local st=call(reg,'SlsPresenceDiscordStatus')
    assert(st.success and st.hasAppId==false and st.status=='idle', 'status RPC before configuration')
    assert(call(reg,'SlsPresenceSetDiscord',json.encode({appId='abc'})).success==false, 'non numeric app id rejected')
    local set=call(reg,'SlsPresenceSetDiscord',json.encode({enabled=false,appId='123456789012345678'}))
    assert(set.success and set.discordEnabled==false and set.discordAppId=='123456789012345678', 'settings saved')
    assert(call(reg,'SlsPresenceGet').state.discordAppId=='123456789012345678', 'settings exposed in state')
    assert(call(reg,'SlsPresencePut',json.encode({owned={},running={}})).success)
    assert(call(reg,'SlsPresenceGet').state.discordEnabled==false, 'Put must keep discord settings')
    -- Discord honesty: the reply must say whether publishing can work at all.
    local valid_app_id='1234567890123456789'
    assert(plugin.app_id_valid(valid_app_id) and plugin.app_id_valid('12345678901234567'),
      'a 17-20 digit Application ID is valid')
    assert(not plugin.app_id_valid('1234') and not plugin.app_id_valid('123456789012345678901')
      and not plugin.app_id_valid('1234567890123456a') and not plugin.app_id_valid(nil),
      'short, long and non-numeric ids are invalid')
    local empty=plugin.discord_status({discordEnabled=true,discordAppId=''},'idle',true)
    assert(empty.enabled and empty.hasAppId==false and empty.appIdValid==false
      and empty.publishing==false and empty.reason=='no-app-id', 'an empty id must reason no-app-id')
    local short=plugin.discord_status({discordEnabled=true,discordAppId='12345'},'connected',true)
    assert(short.reason=='invalid-app-id' and short.publishing==false and short.hasAppId==true,
      'a malformed id must reason invalid-app-id, never report on')
    local nosock=plugin.discord_status({discordEnabled=true,discordAppId=valid_app_id},'connected',false)
    assert(nosock.reason=='no-socket' and nosock.publishing==false,
      'a host without socket.unix must report no-socket rather than claim to publish')
    local okay=plugin.discord_status({discordEnabled=true,discordAppId=valid_app_id},'connected',true)
    assert(okay.publishing==true and okay.reason=='ok')
    local off=plugin.discord_status({discordEnabled=false,discordAppId=valid_app_id},'connected',true)
    assert(off.publishing==false and off.reason=='disabled')
    local rejected_id=call(reg,'SlsPresenceSetDiscord',json.encode({appId='12345'}))
    assert(rejected_id.success==false and rejected_id.code=='invalid-app-id',
      'an invalid non-empty Application ID must be rejected with a structured error')
    assert(call(reg,'SlsPresenceGet').state.discordAppId=='123456789012345678',
      'a rejected Application ID must not be stored')
    local saved_id=call(reg,'SlsPresenceSetDiscord',json.encode({appId=valid_app_id}))
    assert(saved_id.success==true and saved_id.discord.appIdValid==true
      and saved_id.discord.hasAppId==true, 'a valid id must be saved and reported')
    assert(saved_id.discord.publishing==(saved_id.discord.reason=='ok'),
      'publishing must never be true unless the reason is ok')
    local honest=call(reg,'SlsPresenceGet')
    assert(type(honest.discord)=='table' and honest.discord.appIdValid==true
      and type(honest.discord.reason)=='string', 'Get must carry the discord verdict')
    local status_honest=call(reg,'SlsPresenceDiscordStatus')
    assert(type(status_honest.discord)=='table' and type(status_honest.socketAvailable)=='boolean',
      'DiscordStatus must carry the same verdict')
    -- Durability: a torn presence.json is healed from .bak and keeps `owned`;
    -- the live file's stale running entry is retired on the next backend start.
    local state_file=os.getenv('HOME')..'/.local/share/slssteam-integrations/presence.json'
    assert(call(reg,'SlsPresencePut',json.encode({owned={['620']=3000000001},running={}})).success)
    assert(call(reg,'SlsPresencePut',json.encode({owned={['620']=3000000001},running={['620']=3000000001}})).success)
    do
      local f=assert(io.open(state_file,'rb'))
      local good=f:read('*a'); f:close()
      assert(good and #good>0, 'the state file must be written')
      local bak=assert(io.open(state_file..'.bak','rb'))
      local previous=bak:read('*a'); bak:close()
      assert(previous and #previous>0, 'the previous good copy must be kept as .bak')
      local torn=assert(io.open(state_file,'wb'))
      torn:write('{"enabled": true, "owned": {"620"'); torn:close()
    end
    local reg2={}
    local plugin2=assert(loadfile('addons/presence/plugin/backend/main.lua'))()
    plugin2.register(reg2)
    local revived=call(reg2,'SlsPresenceGet')
    assert(revived.success and revived.recovered==true, 'a torn file must be reported as recovered from .bak')
    assert(revived.state.owned['620']==3000000001, 'a torn file must not lose the owned ledger')
    -- Running-entry TTL: alive keeps and heartbeats, gone expires, grace protects
    -- a just-started game, and an unreadable process table only expires on the TTL.
    local now=os.time()
    local st={running={['620']=3000000001},runningAt={['620']=now}}
    assert(#plugin.reconcile_running(st,{620},true,now+50,{grace=120,ttl=3600}).cleared==0
      and st.running['620']==3000000001, 'a live game must be kept')
    local beat=plugin.reconcile_running(st,{620},true,now+200,{grace=120,ttl=3600})
    assert(beat.refreshed==1 and st.runningAt['620']==now+200, 'a live game must heartbeat')
    local gone=plugin.reconcile_running(st,{},true,now+400,{grace=120,ttl=3600})
    assert(#gone.cleared==1 and st.running['620']==nil and st.runningAt['620']==nil,
      'an entry whose process is gone must expire')
    local young={running={['621']=3000000002},runningAt={['621']=now}}
    assert(#plugin.reconcile_running(young,{},true,now+30,{grace=120,ttl=3600}).cleared==0,
      'the grace window must protect a game that just launched')
    local blind={running={['622']=3000000003},runningAt={['622']=now}}
    assert(#plugin.reconcile_running(blind,{},false,now+60,{grace=120,ttl=3600}).cleared==0,
      'an incomplete process scan must not expire anything')
    assert(#plugin.reconcile_running(blind,{},false,now+7200,{grace=120,ttl=3600}).cleared==1,
      'without /proc the hard TTL must still expire the entry')
    -- Restart cleanup: the file still calls a long-dead game running, the
    -- process table says otherwise, so the start pass retires it.
    assert(call(reg2,'SlsPresencePut',json.encode({owned={['620']=3000000001},running={['620']=3000000001}})).success)
    local reg3={}
    local plugin3=assert(loadfile('addons/presence/plugin/backend/main.lua'))()
    plugin3.running_limits({grace=0,ttl=7200})
    plugin3.scan_games=function() return {},true end
    plugin3.register(reg3)
    local restarted=call(reg3,'SlsPresenceGet')
    assert(next(restarted.state.running)==nil, 'a start must clear a running entry whose game is gone')
    assert(restarted.state.owned['620']==3000000001, 'cleanup must not touch the owned ledger')
    assert(restarted.runtime.expiredRunning>=1, 'the expired count must be reported')
    assert(type(plugin.tick)=='function' and plugin.tick(1)==nil, 'tick hook never raises')
  end
print('Luna presence extra backend OK')
