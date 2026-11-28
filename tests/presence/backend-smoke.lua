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
      return path:sub(-9)=='stplug-in' or path:match('loginusers%.vdf$')~=nil
    end,
    join=function(...) return table.concat({...},'/') end,
    list=function(path)
      if path:match('/slssteam%-integrations$') then
        local result={}
        local pipe=assert(io.popen('ls -1 '..string.format('%q',path)..' 2>/dev/null'))
        for name in pipe:lines() do result[#result+1]={name=name,is_directory=false} end
        pipe:close()
        return result
      end
      assert(path:find('steamapps$'))
      return { {name='appmanifest_620.acf',is_directory=false}, {name='note.txt',is_directory=false}, {name='appmanifest_999.acf.disabled',is_directory=false} }
    end,
  }
end
local fixture_pipe=assert(io.popen('mktemp -d'))
local steam_root=assert(fixture_pipe:read('*l'))
assert(fixture_pipe:close())
package.preload['millennium'] = function()
  return {steam_path=function() return steam_root end}
end
local json=require('json')
-- The account identity comes from Steam's config/loginusers.vdf. Write a
-- realistic fixture so the digests in the assertions are the real ones the JS
-- agent computes for the same SteamID64 (the two implementations must agree).
assert(os.execute('mkdir -p '..string.format('%q',steam_root..'/config')))
do
  assert(not io.open(steam_root..'/config/loginusers.vdf','rb'), 'fixture already exists')
  local f=assert(io.open(steam_root..'/config/loginusers.vdf','wb'))
  f:write([[
"users"
{
	"76561199026823325"
	{
		"AccountName"		"dirtysidemusic"
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
local get=call(reg,os.getenv('SLS_KIND')=='presence' and 'SlsPresenceGet' or 'SlsPlaytimeGet')
assert(get.success and #get.apps==1 and get.apps[1]==620, 'manifest filtering failed')
if os.getenv('SLS_KIND')=='presence' then
  do
    local old=1000
    local s={owned={['620']=3000000001},running={['620']=3000000001},
      runningAt={['620']=old},unknownSince={['620']=old}}
    plugin.reconcile_running(s,{},false,old+7*86400+1,{grace=120,ttl=21600})
    assert(not s.running['620'], 'a seven-day scan gap must not reset a running unknown timer')
  end
  do
    local open,clock,scan=io.open,os.time,plugin.scan_games
    local now=100000
    os.time=function() return now end
    plugin.scan_games=function() return {},false end
    local function fail_next_save()
      local attempts=0
      io.open=function(path,mode)
        if mode=='wb' and path:match('presence%.json%.tmp$') then
          attempts=attempts+1
          if attempts==1 then return nil,'injected write failure' end
        end
        return open(path,mode)
      end
      return function() return attempts end
    end
    local function persisted_unknown()
      local f=assert(open(os.getenv('HOME')..'/.local/share/slssteam-integrations/presence.json','rb'))
      local s=json.decode(f:read('*a')); f:close(); return s.unknownSince['620']
    end
    assert(call(reg,'SlsPresencePut',json.encode({owned={['620']=3000000001},running={['620']=3000000001}})).success)
    plugin.register(reg)
    -- Start with no unknown timestamp, then fail the first heartbeat write.
    plugin.scan_games=function() return {620},true end
    now=now+31; plugin.tick(now)
    plugin.scan_games=function() return {},false end
    local attempts=fail_next_save(); now=now+31; plugin.tick(now)
    now=now+31; plugin.tick(now)
    assert(attempts()==2 and persisted_unknown()==now-31, 'unchanged reconciliation must retry a failed heartbeat save')
    -- The same retry is required when initial recovery fails to save.
    plugin.scan_games=function() return {620},true end
    now=now+31; plugin.tick(now)
    plugin.scan_games=function() return {},false end
    attempts=fail_next_save(); plugin.register(reg)
    now=now+31; plugin.tick(now)
    assert(attempts()==2 and persisted_unknown()==now-31, 'heartbeat must retry failed startup reconciliation save')
    assert(call(reg,'SlsPresencePut',json.encode({owned={},running={}})).success)
    io.open=open; os.time=clock; plugin.scan_games=scan
    plugin.register(reg)
  end
  do
    local clock,scan=os.time,plugin.scan_games
    local now=100000
    os.time=function() return now end
    plugin.scan_games=function() return {},false end
    assert(call(reg,'SlsPresencePut',json.encode({owned={['620']=3000000001},running={['620']=3000000001}})).success)
    plugin.tick(now); now=now+21601; plugin.tick(now)
    local expired=call(reg,'SlsPresenceGet')
    assert(expired.expired and tonumber(expired.expired[1])==620, 'Get must report sticky expiry')
    assert(expired.state.unknownSince['620']==100000, 'expiry must preserve the original unknown timer')
    plugin.register(reg)
    local retry=call(reg,'SlsPresencePut',json.encode({owned={['620']=3000000001},running={['620']=3000000001}}))
    assert(retry.expired and tonumber(retry.expired[1])==620, 'stale running write must be rejected')
    assert(not call(reg,'SlsPresenceGet').state.running['620'], 'expired running record was re-admitted')
    assert(call(reg,'SlsPresenceGet').state.unknownSince['620']==100000, 'retry must not reset unknown timer')
    plugin.scan_games=function() return {620},false end
    now=now+31; plugin.tick(now)
    assert(call(reg,'SlsPresencePut',json.encode({owned={['620']=3000000001},running={['620']=3000000001}})).success)
    assert(call(reg,'SlsPresenceGet').state.running['620'], 'positive match must permit re-admission')
    plugin.scan_games=function() return {},false end
    now=now+31; plugin.tick(now)
    plugin.scan_games=function() return {},true end
    now=now+31; plugin.tick(now)
    assert(not call(reg,'SlsPresenceGet').state.unknownSince['620'], 'complete scan must clear sticky timer')
    assert(call(reg,'SlsPresencePut',json.encode({owned={},running={}})).success)
    os.time=clock; plugin.scan_games=scan
  end
  local init=get.state
  assert(init.enabled==false, 'presence must start disabled')
  assert(type(get.session)=='table' and type(get.session.helpersAllowed)=='boolean', 'session helper policy missing')
  local desktop_policy=plugin.helper_policy(function() return nil end)
  assert(desktop_policy.helpersAllowed==true and desktop_policy.mode=='desktop', 'desktop helpers must stay available')
  for _,key in ipairs({'GAMESCOPE_WAYLAND_DISPLAY','GAMESCOPE_LIMITER_FILE','XDG_CURRENT_DESKTOP'}) do
    local policy=plugin.helper_policy(function(name) return name==key and 'gamescope' or nil end)
    assert(policy.helpersAllowed==true and policy.requiresGameSelection==true and policy.mode=='gamescope',
      'Gamescope must require selection restoration: '..key)
  end
  local rejected=call(reg,'SlsPresencePut',json.encode({owned={['620']=3000000001,['621']=3000000001},running={}}))
  assert(rejected.success==false, 'shortcut ownership collision not rejected')
  local selfmap=call(reg,'SlsPresencePut',json.encode({owned={['620']=620},running={}}))
  assert(selfmap.success==false, 'a shortcut cannot be the game itself')
  local admitted=call(reg,'SlsPresencePut',json.encode({owned={['620']=3000000001},running={}}))
  assert(admitted.success==true, 'valid owned shortcut rejected')
  assert(call(reg,'SlsPresenceSetEnabled',true).enabled==true)
  assert(call(reg,'SlsPresenceGet').state.enabled==true)
  assert(call(reg,'SlsPresenceReport',json.encode({healthy=false,error='API missing'})).success)
  assert(call(reg,'SlsPresenceGet').runtime.error=='API missing')
  -- The account digest is persisted but only in its validated 8-hex form.
  assert(call(reg,'SlsPresencePut',json.encode({owned={['620']=3000000001},running={},account='a1b2c3d4'})).success)
  assert(call(reg,'SlsPresenceGet').state.account=='a1b2c3d4', 'account digest not stored')
  assert(call(reg,'SlsPresencePut',json.encode({owned={['620']=3000000001},running={},account='NOT-HEX!'})).success)
  assert(call(reg,'SlsPresenceGet').state.account=='a1b2c3d4', 'invalid account digest overwrote the ledger')
  -- The raw SteamID must never be accepted into the ledger.
  assert(call(reg,'SlsPresencePut',json.encode({owned={},running={},account='76561198000000000'})).success)
  assert(call(reg,'SlsPresenceGet').state.account=='a1b2c3d4', 'raw SteamID was stored')
  -- Name enrichment: defaults on, persists, and survives agent ledger saves.
  assert(call(reg,'SlsPresenceGet').state.sessionTime==true, 'session time must default on')
  assert(call(reg,'SlsPresenceSetSessionTime','nonsense').success==false, 'non-boolean accepted')
  assert(call(reg,'SlsPresenceSetSessionTime',false).sessionTime==false)
  assert(call(reg,'SlsPresenceGet').state.sessionTime==false)
  assert(call(reg,'SlsPresencePut',json.encode({owned={['620']=3000000001},running={}})).success)
  assert(call(reg,'SlsPresenceGet').state.sessionTime==false, 'agent ledger save dropped the enrichment toggle')
  assert(call(reg,'SlsPresenceSetSessionTime',true).sessionTime==true)
  -- Runtime capability diagnostics surface to the panel.
  local diag=call(reg,'SlsPresenceReport',json.encode({healthy=true,active=1,owned=1,running=0,
    accountMismatch=true,hideStrategy='collectionHidden',hideVerified=true,
    contracts={addShortcut=4,setShortcutHidden=nil},activeIds={620}}))
  assert(diag.success)
  local rt=call(reg,'SlsPresenceGet').runtime
  assert(rt.hideStrategy=='collectionHidden' and rt.hideVerified==true, 'hide diagnostics lost')
  assert(rt.accountMismatch==true, 'account mismatch flag lost')
  assert(rt.contracts.addShortcut==4, 'contract arity lost')
  assert(rt.contracts.setShortcutHidden==json.null or rt.contracts.setShortcutHidden==nil, 'absent API must stay absent')
  assert(rt.owned==1 and rt.running==0, 'ledger counts lost')
  -- The account identity is derived from loginusers.vdf and must match the JS
  -- FNV-1a implementation byte for byte (see tests/presence-lifecycle.test.cjs).
  local ident=call(reg,'SlsPresenceGet').identity
  assert(ident, 'identity missing')
  assert(ident.key=='04830297', 'identity digest mismatch: '..tostring(ident.key))
  assert(ident.source=='loginusers', 'identity source must be reported')
  assert(ident.known==1 and ident.ambiguous==false, 'single account must not be ambiguous')
  -- Read-only contract probe: proves the {raw: string} argument shape.
  local probe=call(reg,'SlsPresenceContractProbe',json.encode({a=1}))
  assert(probe.success and probe.argType=='string' and probe.decoded==true, 'contract probe failed')
  local noarg=call(reg,'SlsPresenceContractProbe')
  assert(noarg.success and noarg.argType=='nil' and noarg.decoded==false, 'missing arg must not decode')
  -- Read-only process snapshot used to recover state after a context recreation.
  local function list_proc(path)
    assert(path=='/proc')
    return { {name='1',is_directory=true}, {name='999',is_directory=true},
             {name='not-a-pid',is_directory=true}, {name='self',is_directory=true},
             {name='2222',is_directory=true} }
  end
  do
    local empty=os.getenv('SLS_TEST_HOME')..'/empty-proc-fixture'
    local f=assert(io.open(empty,'wb')); assert(f:close())
    assert(type(plugin.read_proc)=='function' and plugin.read_proc(empty)=='',
      'successful empty proc file must read as an empty string')
    local missing_file,missing_error,missing_code=plugin.read_proc(empty..'-missing')
    assert(missing_file==nil and missing_error and (missing_code==2 or missing_error:find('No such file or directory',1,true)),
      'missing proc file must expose its error')
    local failed,read_error=plugin.read_proc(os.getenv('SLS_TEST_HOME'))
    assert(failed==nil and read_error, 'a real read error must not be treated as empty')
    local one=function() return {{name='42'}} end
    local function stat(state,start)
      return '42 (game name) '..state..' '..string.rep('0 ',18)..start..' 0'
    end
    local empty_stat='42 () S '..string.rep('0 ',18)..'100 0'
    local tricky_stat='42 (name ) S 9 (foo) S bar) S '..string.rep('0 ',18)..'100 0'
    for _,value in ipairs({empty_stat,tricky_stat}) do
      local games,_,complete=plugin.collect_running(one,function(path)
        if path:match('/stat$') then return value end
        return '/reaper\0AppId=620\0'
      end)
      assert(complete and games[1]==620, 'empty or parenthesized comm must parse using the last closing parenthesis')
    end
    local function scan(cmd, before, after)
      local reads=0
      return plugin.collect_running(one,function(path)
        if path:match('/stat$') then reads=reads+1; return reads==1 and before or after end
        return cmd
      end)
    end
    local missing,_,missing_complete=plugin.collect_running(one,function()
      return nil,'No such file or directory',2
    end)
    local gone_state={running={['620']=3000000000},runningAt={['620']=1}}
    plugin.reconcile_running(gone_state,missing,missing_complete,100,{grace=1,ttl=21600})
    assert(missing_complete and not gone_state.running['620'], 'vanished PID must not prevent expiry')
    local _,_,permission_complete=plugin.collect_running(one,function()
      return nil,'Permission denied',13
    end)
    assert(not permission_complete, 'permission-denied stat must remain unknown')
    local large,_,large_complete=scan('/reaper\0AppId=620\0'..string.rep('x',10000),stat('S',100),stat('S',100))
    assert(large[1]==620 and large_complete, '10 KiB cmdline must be matched without truncation')
    local denied_games,_,denied=scan(nil,stat('S',100),stat('S',100))
    assert(not denied, 'unreadable cmdline must be unknown')
    local partial=plugin.process_snapshot(function() return {{name='42'},{name='43'}} end,function(path)
      if path:find('/43/',1,true) then return nil,'Permission denied',13 end
      if path:match('/stat$') then return stat('S',100) end
      return '/reaper\0AppId=620\0'
    end)
    assert(partial.success and not partial.complete and partial.games[1]==620,
      'partial scan must return positive matches successfully')
    local failed_scan=plugin.process_snapshot(function() error('proc unavailable') end,function() end)
    assert(not failed_scan.success and not failed_scan.complete, 'listing failure must be an RPC failure')
    local heartbeat={running={['620']=3000000000,['621']=3000000001},runningAt={['620']=1,['621']=1}}
    plugin.reconcile_running(heartbeat,partial.games,partial.complete,100,{grace=1,ttl=21600})
    assert(heartbeat.runningAt['620']==100 and heartbeat.running['621'],
      'partial scan must refresh matches and retain absent games')
    local long='/reaper\0AppId=620\0'..string.rep('x',262144)
    local games,_,long_complete=scan(long,stat('S',100),stat('S',100))
    assert(games[1]==620 and not long_complete, 'truncated cmdline must match its prefix but remain unknown')
    local _,_,gone_cmdline=plugin.collect_running(one,function(path)
      if path:match('/cmdline$') then return nil,'No such process',3 end
      return stat('S',100)
    end)
    assert(gone_cmdline, 'vanished cmdline must not block expiry')
    local zombies,_,zcomplete=scan('/reaper\0AppId=620\0',stat('Z',100),stat('Z',100))
    assert(#zombies==0 and zcomplete, 'zombie must be exited')
    local reused,_,rcomplete=scan('/reaper\0AppId=620\0',stat('S',100),stat('S',101))
    assert(#reused==0 and not rcomplete, 'reused PID during scan must be unknown')
    local previous={['42']='99'}
    local changed,_,changed_complete,identities=plugin.collect_running(one,function(path)
      if path:match('/stat$') then return stat('S',100) end
      return '/reaper\0AppId=620\0'
    end,previous)
    assert(#changed==0 and not changed_complete and identities['42']=='100',
      'reused recorded PID must not be attributed to its former process')
    local running={running={['620']=3000000000},runningAt={['620']=1}}
    plugin.reconcile_running(running,{620},false,100,{grace=1,ttl=1})
    assert(running.running['620'] and running.runningAt['620']==100, 'positive match must heartbeat')
    plugin.reconcile_running(running,{},true,102,{grace=1,ttl=1})
    assert(not running.running['620'], 'confirmed exit must clear after grace')
    local state={running={['620']=3000000000},runningAt={['620']=1}}
    plugin.reconcile_running(state,{},false,100000,{grace=1,ttl=1})
    assert(state.running['620'], 'unknown must not expire even after TTL')
    local empty_games,_,empty_complete=plugin.collect_running(one,function(path)
      if path:match('/stat$') then return stat('S',100) end
      return plugin.read_proc(empty)
    end)
    local empty_state={running={['620']=3000000000},runningAt={['620']=1}}
    plugin.reconcile_running(empty_state,empty_games,empty_complete,100,{grace=1,ttl=21600})
    assert(not empty_state.running['620'], 'empty kernel-thread cmdline must not block exit')
    local denied_state={running={['620']=3000000000},runningAt={['620']=1}}
    plugin.reconcile_running(denied_state,denied_games,denied,100,{grace=1,ttl=21600})
    plugin.reconcile_running(denied_state,denied_games,denied,21700,{grace=1,ttl=21600})
    assert(not denied_state.running['620'], 'unrelated unreadable pid must not block expiry forever')
    local interrupted={running={['620']=3000000000},runningAt={['620']=1}}
    plugin.reconcile_running(interrupted,{},false,100,{grace=100000,ttl=21600})
    plugin.reconcile_running(interrupted,{620},false,200,{grace=100000,ttl=21600})
    plugin.reconcile_running(interrupted,{},false,21700,{grace=100000,ttl=21600})
    assert(interrupted.running['620'] and interrupted.unknownSince['620']==21700,
      'positive evidence must reset the continuous unknown ceiling')
    os.remove(empty)
  end
  local function read_cmd(path)
    if path=='/proc/1/cmdline' then return 'systemd\0--user\0' end
    if path=='/proc/999/cmdline' then
      return table.concat({'/home/x/Steam/ubuntu12_32/reaper','SteamLaunch','AppId=620',
        '--','/usr/bin/sleep','86400'}, string.char(0)) .. string.char(0)
    end
    if path=='/proc/2222/cmdline' then
      return table.concat({'/home/x/reaper','AppId=2101960','--','/game/Cronos.exe'}, string.char(0))
    end
    return nil
  end
  local function reader(cmd)
    return function(path)
      if path:match('/stat$') then return path:match('/(%d+)/stat$')..' (test) S '..string.rep('0 ',18)..'100 0' end
      return cmd(path)
    end
  end
  local read_proc=reader(read_cmd)
  local games,helpers,complete=plugin.collect_running(list_proc,read_proc)
  assert(complete==true, 'complete scan must be marked complete')
  assert(#games==2 and #helpers==1, 'process scan did not separate games from helpers')
  local has={}
  for _,id in ipairs(games) do has[id]=true end
  assert(has[620] and has[2101960], 'AppId markers not parsed')
  assert(helpers[1]==620, 'the sleep helper was not identified')
  -- A reader that throws must not take the backend down.
  local safe_games,safe_helpers,safe_complete=plugin.collect_running(list_proc,function() error('denied') end)
  assert(#safe_games==0 and #safe_helpers==0, 'scan must fail closed, not raise')
  assert(safe_complete==false, 'failed readers must not produce an authoritative empty snapshot')
  local none_games,none_helpers,none_complete=plugin.collect_running(function() return nil end,read_proc)
  assert(#none_games==0 and #none_helpers==0, 'missing /proc must not raise')
  assert(none_complete==false, 'missing /proc must be marked incomplete')
  local pids={}
  for i=1,8193 do pids[i]={name=tostring(i)} end
  local _,_,truncated=plugin.collect_running(function() return pids end,read_proc)
  assert(truncated==false, 'PID cap must not masquerade as a complete snapshot')
  local duplicates=function() return {{name='10'},{name='11'}} end
  local duplicate_games,duplicate_helpers=plugin.collect_running(duplicates,reader(function()
    return '/reaper\0AppId=620\0--\0/usr/bin/sleep\00086400\0'
  end))
  assert(#duplicate_games==1 and #duplicate_helpers==1, 'snapshots must deduplicate AppIDs, not just PIDs')
  local single=function() return {{name='10'}} end
  for _,raw in ipairs({'/tool\0FakeAppId=620\0', '/tool\0AppId=620invalid\0'}) do
    local spoofed=plugin.collect_running(single,reader(function() return raw end))
    assert(#spoofed==0, 'AppId must occupy an entire cmdline token')
  end
  local _,not_helpers=plugin.collect_running(single,reader(function()
    return '/reaper\0AppId=620\0--\0/usr/bin/sleep-other\00086400\0'
  end))
  assert(#not_helpers==0, 'helper executable matching must not accept a substring')
  local string_games,_,string_complete=plugin.collect_running(function() return {'10'} end,reader(function()
    return '/reaper\0AppId=620\0/game\0'
  end))
  assert(#string_games==1 and string_complete, 'string-shaped directory entries remain supported')
  -- The loginusers parser drives the account guard; it must not be fooled by
  -- numeric keys that are not SteamIDs.
  local users=plugin.login_users('"shortkey"{ "AccountName" "x" }\n"76561199026823325"{ "MostRecent" "1" }')
  assert(#users==1, 'only 17+ digit keys are accounts')
  assert(users[1].id=='76561199026823325' and users[1].most_recent==true, 'SteamID block not parsed')
  assert(users[1].account_name==nil, 'fields must not leak between account blocks')
  -- A stop on a shortcut the ledger cannot confirm is refused by the backend too.
  assert(call(reg,'SlsPresencePut',json.encode({owned={['620']=3000000001},running={['620']=3000000002}})).success==false,
    'running entry for an unowned shortcut accepted')
  local file=os.getenv('HOME')..'/.local/share/slssteam-integrations/presence.json'
  local function write(path,body) local f=assert(io.open(path,'wb')); assert(f:write(body)); assert(f:close()) end
  write(file..'.bak',json.encode({owned={['620']=3000000001},running={}}))
  write(file,json.encode({owned=false,running={}}))
  local recovered={}; plugin.register(recovered)
  assert(call(recovered,'SlsPresenceGet').state.owned['620']==3000000001, 'malformed ownership must use backup')
  -- A failed quarantine must be retried and discard the cached pre-corruption state.
  write(file,'damaged live'); write(file..'.bak','damaged backup')
  local execute=os.execute
  os.execute=function(cmd) if cmd:match('^ln %-%-') then return false end return execute(cmd) end
  assert(not call(recovered,'SlsPresenceSetEnabled',true).success)
  assert(not call(recovered,'SlsPresenceAcknowledgeCorruption',json.encode({acknowledge=true})).success)
  os.execute=execute
  assert(call(recovered,'SlsPresenceAcknowledgeCorruption',json.encode({acknowledge=true})).success)
  assert(next(call(recovered,'SlsPresenceGet').state.owned)==nil, 'stale cached ownership restored')
  local function quarantine_count()
    local p=assert(io.popen('find '..string.format('%q',file..'.quarantine.')..'* -type f | wc -l'))
    local count=tonumber(p:read('*a')); p:close(); return count
  end
  local before_count=quarantine_count()
  write(file,'damaged live'); write(file..'.bak','damaged backup')
  local damaged={}; plugin.register(damaged)
  local warning=call(damaged,'SlsPresenceGet')
  assert(warning.corrupt and #warning.quarantinePaths>=2, 'both quarantine paths must be reported')
  assert(warning.quarantineDirectory==os.getenv('HOME')..'/.local/share/slssteam-integrations')
  assert(not call(damaged,'SlsPresenceSetEnabled',true).success)
  assert(not call(damaged,'SlsPresenceAcknowledgeCleanup').success)
  assert(not call(damaged,'SlsPresenceAcknowledgeCorruption',json.encode({})).success)
  local restarted={}; plugin.register(restarted)
  assert(call(restarted,'SlsPresenceGet').corruptionRequiresAcknowledgement)
  assert(not call(restarted,'SlsPresencePut',json.encode({owned={},running={}})).success)
  assert(call(restarted,'SlsPresenceAcknowledgeCorruption',json.encode({acknowledge=true})).success)
  assert(call(restarted,'SlsPresenceSetEnabled',true).success)
  write(file,'second damaged live'); write(file..'.bak','second damaged backup')
  local again={}; plugin.register(again)
  assert(call(again,'SlsPresenceGet').corrupt)
  assert(quarantine_count()==before_count+2,'quarantines must not overwrite previous evidence')
  for _,reply in ipairs({
    {cmd='SET_ACTIVITY',nonce='1',evt='ERROR'},
    {cmd='SET_ACTIVITY',nonce='wrong'},
    {cmd='SET_ACTIVITY',nonce='1'},
  }) do
    local frames={}
    local function frame(body) return string.pack('<I4I4',1,#body)..body end
    frames[1]=frame(json.encode({evt='READY'}))
    frames[2]=frame(json.encode(reply))
    local buffer=table.concat(frames)
    local conn={settimeout=function() end,connect=function() return true end,getfd=function() return 3 end,
      send=function(_,data) return #data end,close=function() end,
      receive=function(_,n) local s=buffer:sub(1,n);buffer=buffer:sub(n+1);return s end}
    local session=plugin.new_discord_session({unix=function() return conn end,paths=function() return {'fake'} end,pid=function() return 1 end})
    local status=session.step({enabled=true,app_id='123'},{appid=620,name='Game'},100)
    assert((status=='connected')==(reply.nonce=='1' and reply.evt~='ERROR'), 'SET_ACTIVITY acknowledgement required')
    if reply.evt=='ERROR' then assert(session.next_try>100) end
  end
  do
    -- Discord may PING before acknowledging: answer it and keep waiting.
    local function frame(op,body) return string.pack('<I4I4',op,#body)..body end
    local buffer=frame(1,json.encode({evt='READY'}))..frame(3,'{"p":1}')..frame(4,'{}')
      ..frame(1,json.encode({cmd='SET_ACTIVITY',nonce='other'}))..frame(1,json.encode({cmd='SET_ACTIVITY',nonce='1'}))
    local sent={}
    local conn={settimeout=function() end,connect=function() return true end,getfd=function() return 3 end,
      send=function(_,data) sent[#sent+1]=data; return #data end,close=function() end,
      receive=function(_,n) local s=buffer:sub(1,n);buffer=buffer:sub(n+1);return s end}
    local session=plugin.new_discord_session({unix=function() return conn end,paths=function() return {'fake'} end,pid=function() return 1 end})
    assert(session.step({enabled=true,app_id='123'},{appid=620,name='Game'},100)=='connected',
      'a PING before the SET_ACTIVITY acknowledgement dropped the connection')
    local pong=false
    for _,data in ipairs(sent) do if string.unpack('<I4',data)==4 then pong=true end end
    assert(pong,'the PING was not answered with a PONG')
  end
else
  assert(call(reg,'SlsPlaytimeHeartbeat',json.encode({id=999,kind='start'})).success==false)
  assert(call(reg,'SlsPlaytimeHeartbeat',json.encode({id=620,kind='start',title='Portal 2'})).success)
  local backup=call(reg,'SlsPlaytimeExport').data
  assert(call(reg,'SlsPlaytimeImport',json.encode(backup)).success==false, 'import while active accepted')
  assert(call(reg,'SlsPlaytimeHeartbeat',json.encode({id=620,kind='pulse',elapsed=10})).success)
  assert(call(reg,'SlsPlaytimeHeartbeat',json.encode({id=620,kind='stop',elapsed=3})).success)
  assert(call(reg,'SlsPlaytimeEdit',json.encode({id=620,hours=2})).success)
  local item=call(reg,'SlsPlaytimeGet').games['620']
  assert(math.abs(item.trackedSeconds+item.adjustmentSeconds-7200)<0.001)
  assert(call(reg,'SlsPlaytimeImport',json.encode(backup)).success)
  assert(call(reg,'SlsPlaytimeReport',json.encode({healthy=true})).success)
  assert(call(reg,'SlsPlaytimeGet').runtime.healthy==true)
end
assert(os.execute('rm -rf -- '..string.format('%q',steam_root)))
print('Lua backend smoke OK ('..os.getenv('SLS_KIND')..')')
