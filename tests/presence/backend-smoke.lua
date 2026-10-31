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
      assert(path:find('stplug%-in$'))
      return { {name='620.lua',is_directory=false}, {name='note.txt',is_directory=false}, {name='999.lua.disabled',is_directory=false} }
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
	"76561190000000000"
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
  local function read_proc(path)
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
  local duplicate_games,duplicate_helpers=plugin.collect_running(duplicates,function()
    return '/reaper\0AppId=620\0--\0/usr/bin/sleep\00086400\0'
  end)
  assert(#duplicate_games==1 and #duplicate_helpers==1, 'snapshots must deduplicate AppIDs, not just PIDs')
  local single=function() return {{name='10'}} end
  for _,raw in ipairs({'/tool\0FakeAppId=620\0', '/tool\0AppId=620invalid\0'}) do
    local spoofed=plugin.collect_running(single,function() return raw end)
    assert(#spoofed==0, 'AppId must occupy an entire cmdline token')
  end
  local _,not_helpers=plugin.collect_running(single,function()
    return '/reaper\0AppId=620\0--\0/usr/bin/sleep-other\00086400\0'
  end)
  assert(#not_helpers==0, 'helper executable matching must not accept a substring')
  local string_games,_,string_complete=plugin.collect_running(function() return {'10'} end,function()
    return '/reaper\0AppId=620\0/game\0'
  end)
  assert(#string_games==1 and string_complete, 'string-shaped directory entries remain supported')
  -- The loginusers parser drives the account guard; it must not be fooled by
  -- numeric keys that are not SteamIDs.
  local users=plugin.login_users('"shortkey"{ "AccountName" "x" }\n"76561190000000000"{ "MostRecent" "1" }')
  assert(#users==1, 'only 17+ digit keys are accounts')
  assert(users[1].id=='76561190000000000' and users[1].most_recent==true, 'SteamID block not parsed')
  assert(users[1].account_name==nil, 'fields must not leak between account blocks')
  -- A stop on a shortcut the ledger cannot confirm is refused by the backend too.
  assert(call(reg,'SlsPresencePut',json.encode({owned={['620']=3000000001},running={['620']=3000000002}})).success==false,
    'running entry for an unowned shortcut accepted')
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
print('Lua backend smoke OK ('..os.getenv('SLS_KIND')..')')
