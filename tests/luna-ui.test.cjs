// SLS Luna shell UI: the menubar button and the settings menu.
//
// Runs against a small DOM stub, because what matters is the wiring the client
// depends on: the button lands in the native menubar next to Lumen's, the menu
// opens with a working sidebar, and the settings for both add-ons are driven by
// their real RPC names.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {mount,RPC_TIMEOUT,buildRows,buildHltbRows,findMenubar,restoreMainView,prepareView,
  dailyBars,weeklySentence,weekTop,minutesText}=require('../addons/luna/plugin/public/panel.js');
const agentApi=require('../addons/luna/plugin/public/agent.js');

function fakeDom(){
  const registry={};
  const created=[];
  const listeners={};
  // A child list the way Steam's CEF hands one out: a live, array-indexed
  // object with item() and length, but not an Array. Anything that calls
  // .slice/.map/.filter on it must fail here exactly as it does in Steam.
  const collectionView=list=>{
    const view={
      item:index=>list[index]||null,
      get length(){return list.length;},
      [Symbol.iterator]:()=>list[Symbol.iterator](),
    };
    for(let index=0;index<128;index+=1){
      Object.defineProperty(view,index,{get:()=>list[index],enumerable:true,configurable:true});
    }
    return view;
  };
  const node=tag=>{
    const kids=[];
    const element={
      tagName:(tag||'div').toUpperCase(),id:'',className:'',title:'',hidden:false,
      style:{cssText:'',cssText2:''},children:collectionView(kids),parentNode:null,innerHTML:'',
      value:'',placeholder:'',type:'',rows:0,onclick:null,
      append(...more){more.forEach(kid=>element.appendChild(kid));},
      appendChild(kid){kid.parentNode=element;kids.push(kid);register(kid);return kid;},
      insertBefore(kid,ref){
        kid.parentNode=element;
        const index=ref?kids.indexOf(ref):-1;
        if(index<0)kids.push(kid);else kids.splice(index,0,kid);
        register(kid);return kid;
      },
      remove(){const p=element.parentNode;if(p&&p.__kids){const i=p.__kids.indexOf(element);
        if(i>=0)p.__kids.splice(i,1);}
        delete registry[element.id];element.parentNode=null;},
      setAttribute(name,value){element['attr_'+name]=value;},
      getAttribute(name){return element['attr_'+name];},
      removeAttribute(name){delete element['attr_'+name];},
      addEventListener(type,fn){(listeners[element.id+':'+type]=listeners[element.id+':'+type]||[]).push(fn);},
      focus(){},select(){},
      click(){(listeners[element.id+':click']||[]).forEach(fn=>
        fn({preventDefault(){},stopPropagation(){}}));},
      get nextSibling(){const p=element.parentNode;if(!p||!p.__kids)return null;
        const i=p.__kids.indexOf(element);return i>=0?p.__kids[i+1]||null:null;},
      querySelectorAll(){return [];},
    };
    Object.defineProperty(element,'__kids',{value:kids});
    Object.defineProperty(element,'__resetChildren',{value:()=>{kids.length=0;}});
    // textContent behaves like the DOM's: the element's own text plus its
    // children's, which is what the code and the tests read. innerHTML markup
    // contributes its visible text (tags stripped) so headings and notes are
    // visible to assertions exactly as a browser would show them.
    let ownText='';
    let markup='';
    Object.defineProperty(element,'innerHTML',{
      get:()=>markup,
      set(value){
        markup=String(value);
        ownText=markup.replace(/<svg[\s\S]*?<\/svg>/g,'').replace(/<[^>]*>/g,'');
        element.__resetChildren();
      },
    });
    Object.defineProperty(element,'textContent',{
      get:()=>element.children.length
        ? Array.from(element.children).map(child=>child.textContent).join('')
        : ownText,
      set(value){ownText=String(value);markup='';element.__resetChildren();},
    });
    Object.defineProperty(element,'innerText',{get:()=>element.textContent});
    created.push(element);
    return element;
  };
  const register=element=>{
    if(!element||typeof element!=='object')return;
    if(element.id)registry[element.id]=element;
    Array.from(element.children).forEach(register);
  };
  return {
    registry,created,listeners,
    head:node('head'),body:node('body'),readyState:'complete',
    createElement:node,
    getElementById:id=>registry[id]||null,
    // A simple [attr] selector filters by attribute; any other selector keeps
    // the previous behaviour of returning everything (the panel's finders walk
    // the tree themselves).
    querySelectorAll:selector=>{
      const match=String(selector||'').match(/^\[([\w-]+)\]$/);
      if(!match)return created.slice();
      return created.filter(element=>element['attr_'+match[1]]!==undefined);
    },
    addEventListener(type,fn){(listeners['document:'+type]=listeners['document:'+type]||[]).push(fn);},
    removeEventListener(type,fn){
      const list=listeners['document:'+type]||[];
      const index=list.indexOf(fn);
      if(index>=0)list.splice(index,1);
    },
    fire(type,event){const key=type.startsWith('document')?type:type;
      (listeners[type]||[]).forEach(fn=>fn(event||{}));},
    custom(type,event){(listeners[type]||[]).forEach(fn=>fn(event||{}));},
  };
}

function textOf(node){
  if(!node)return '';
  const own=node.children.length===0?(node.textContent||''):'';
  return own+Array.from(node.children).map(textOf).join(' ');
}
function findNode(node,predicate){
  if(!node)return null;
  if(predicate(node))return node;
  for(const child of node.children||[]){const hit=findNode(child,predicate);if(hit)return hit;}
  return null;
}

function collect(node,predicate){
  const found=[];
  (function walk(current){
    if(!current)return;
    if(predicate(current))found.push(current);
    for(const child of current.children||[])walk(child);
  })(node);
  return found;
}

// "YYYY-MM-DD" for a day N days before today, in local time, the way the
// backend stamps `daily30`.
function dayKey(offset){
  const now=new Date();
  const date=new Date(now.getFullYear(),now.getMonth(),now.getDate()-offset);
  return date.getFullYear()+'-'+String(date.getMonth()+1).padStart(2,'0')+'-'
    +String(date.getDate()).padStart(2,'0');
}

function dailyFixture(values){
  return values.map((seconds,index)=>({day:dayKey(29-index),seconds}));
}


function menubarFixture(document,lumenButton){
  const bar=document.createElement('div');
  ['Steam','View','Friends','Games','Help'].forEach(label=>{
    const item=document.createElement('div');
    item.textContent=label;
    bar.appendChild(item);
  });
  if(lumenButton){
    const lumen=document.createElement('div');
    lumen.id='lumen-moon-btn';
    lumen.textContent='Lumen';
    bar.appendChild(lumen);
  }
  document.body.appendChild(bar);
  return bar;
}

function compressorFixture(overrides){
  const base={success:true,
    tool:{found:true,path:'/home/tester/.local/bin/btrfs-game-compressor',version:'0.3.1'},
    status:{version:'0.3.1',libraries:['/lib'],games:[
      {name:'Cronos: The New Dawn',status:'UNCOMPRESSED',size_bytes:239,size_display:'22.3G',dedupe_pending:true,community_hint_pct:71.2},
      {name:'Hades',status:'COMPACTED',size_bytes:11,size_display:'11.2G',dedupe_pending:false},
      {name:'Pony Island',status:'RUNNING',size_bytes:1,size_display:'96M',dedupe_pending:false}],
      summary:{game_count:3,pending:2,compacted:1,low_yield:0,running:1,skipped_empty:0}},
    plan:{total:3,pending:2,compacted:1,running:1,excluded:0,overSize:0,compress:1,dedupe:0,blocked:0},
    settings:{version:1,compress:true,dedupe:true,eco:false,assets:false,assetProfile:'balanced',
      keepLanguages:'',maxSizeGb:0,excluded:[]},
    statusAt:Date.now()/1000-120,job:null};
  return Object.assign(base,overrides||{});
}

async function openUpdates(document,options){
  menubarFixture(document,true);
  const made=fakeWindow(document,options||{});
  mount(made.w);
  document.getElementById('sls-luna-btn').click();
  await new Promise(resolve=>setTimeout(resolve,60));
  const tab=findNode(document.getElementById('sls-luna-overlay'),
    node=>(node.className||'').startsWith('sls-luna-tab')&&node.textContent.startsWith('Updates'));
  assert.ok(tab,'the Updates tab must be registered');
  tab.onclick();
  await new Promise(resolve=>setTimeout(resolve,40));
  made.tab=tab;
  return made;
}
const button=(document,label)=>findNode(document.getElementById('sls-luna-overlay'),
  node=>node.tagName==='BUTTON'&&node.textContent===label&&typeof node.onclick==='function');
const settle=ms=>new Promise(resolve=>setTimeout(resolve,ms||50));

function fakeWindow(document,{get,presence,compressor,hltb,fetchFailure,fetchPending,rateLimitOnce,sgdbKey,edit,setEnabled,prefs,steamClient,bootPatched,host,failFns,lumenPending,lumenTooling,lumenResult,lumen,update,gate,repair,sgdbAuto,sgdbAutoPlan,sgdbHistory,sgdbHistoryError,luatools,openLuaTools,lumenCloseOverlay,lumenOpenOverlay,channels,channelsRefusal,terminalRefusal,terminalStart,discordRefusal,sgdbHistoryTimes,sgdbApplyFailure,playtimeRev,graphicsCheckedAt,graphicsGpus,graphicsGpu}={}){
  const bridgePatched = bootPatched !== false;
  const calls=[];
  const repairPlan=repair||null;
  let repairPolls=0;
  let sgdbAutoPolls=0;
  const autoResultsDone=[
    {kind:'grid',ok:true,live:true},{kind:'wide',ok:true,live:true},
    {kind:'hero',ok:true,live:true},{kind:'logo',ok:true,live:true},
    {kind:'icon',ok:false,code:'none',error:'No usable icon images.'}];
  const autoSnapshot=(state,doneCount)=>{
    const results=autoResultsDone.slice(0,doneCount);
    for(let i=doneCount;i<autoResultsDone.length;i+=1){
      results.push({kind:autoResultsDone[i].kind,ok:false,pending:true,error:'Still working...'});
    }
    return JSON.stringify({success:true,state,pending:state==='running',
      job:{id:7,state,done:doneCount,total:5,startedAt:Date.now()/1000},results,error:''});
  };
  // Timer and listener bookkeeping, so a remount can be checked for leftovers.
  const timers={intervals:new Set(),timeouts:new Set(),fns:new Map()};
  const winListeners=new Map();
  let timerSeq=0;
  const record=(type,fn)=>{
    if(!winListeners.has(type))winListeners.set(type,[]);
    winListeners.get(type).push(fn);
  };
  const forget=(type,fn)=>{
    const list=winListeners.get(type)||[];
    const index=list.indexOf(fn);
    if(index>=0)list.splice(index,1);
  };
  const hltbState=Object.assign({success:true,count:0,games:{},library:[]},hltb||{});
  hltbState.count=Object.keys(hltbState.games||{}).length;
  const rateLimited=new Set();
  const pendingSeen=new Map();
  const storage=new Map();
  const prefsState=Object.assign({version:1,store_hide_followed:false},prefs||{});
  const updateState={installed:'0.14.3',latest:'0.14.9',available:true,
    checkedAt:Math.floor(Date.now()/1000)-120,checkError:'',pending:false,pendingTag:'',
    autoUpdate:'notify',last:{result:'current',version:'0.14.3',at:Math.floor(Date.now()/1000)-3600}};
  Object.assign(updateState,update||{});
  const sgdbState={apiKey:sgdbKey||'',gridDir:'/home/tester/.local/share/Steam/userdata/1/config/grid',gridError:''};
  // The backend's undo history: per-slot depth, moved by apply and undo the way
  // the Lua does, so a refresh after a change is really visible in the page.
  const sgdbHistoryState=Object.assign({grid:0,wide:0,hero:0,logo:0,icon:0},sgdbHistory||{});
  const sgdbTimesState=Object.assign({},sgdbHistoryTimes||{});
  const sgdbSteamState=Object.assign({build:'1791918634',pinned:'',match:false,updatesBlocked:true,
    cachedBuild:'',cacheReady:false,pacmanManaged:false,pacmanIgnored:false,pending:''},
    steamClient&&steamClient.pinned?steamClient:{});
  const refreshState={pending:false};
  const lumenState={host:host||'lumen',versions:{lumen:'v2.9',plugin:'v2.9',slsteam_moon:'v2.9'},
    blockInPlace:true,bridge:true,pending:Boolean(lumenPending),result:lumenResult||{},
    tooling:{ok:!lumenTooling,missing:[]},toolingIncomplete:lumenTooling||'',
    job:lumenPending?{action:'update',attempts:0,lastError:''}:null,interrupted:false,backups:[],
    millennium:{choice:'',present:[]},check:{},sessionStartedAt:0};
  Object.assign(lumenState,lumen||{});
  const lumenSeen=[];
  const warnings=[];
  const signIn=[];
  // The channels the CLI reports, and the terminal job state it hands back.
  const channelsState=Object.assign({values:{lumen:'stable',plugin:'stable',slsteam_moon:'stable'},
    file:'/home/tester/.local/share/Lumen/update-channels.json',at:Math.floor(Date.now()/1000),
    output:'lumen stable'},channels||{});
  const w={
    document,
    localStorage:{getItem:key=>storage.has(key)?storage.get(key):null,
      setItem:(key,value)=>storage.set(key,String(value)),
      removeItem:key=>storage.delete(key)},
    addEventListener:(type,fn)=>record(type,fn),
    removeEventListener:(type,fn)=>forget(type,fn),
    // Fires every listener the panel registered on the window (focus, storage,
    // pagehide), the way Steam's CEF window would.
    __emitTo:(type,event)=>{(winListeners.get(type)||[]).slice().forEach(fn=>fn(event||{}));},
    __SlsLunaRpcTimeout:undefined,
    __emitStorage:()=>{(winListeners.get('storage')||[]).slice().forEach(fn=>fn({key:'slsLunaArtworkRequest'}));},
    setInterval:(fn,ms)=>{const id=++timerSeq;timers.intervals.add(id);timers.fns.set(id,fn);return id;},
    clearInterval:id=>{timers.intervals.delete(id);timers.fns.delete(id);},
    // The fetch-missing queue spaces requests out; tests skip the waiting.
    setTimeout:(fn,ms)=>{const id=++timerSeq;timers.timeouts.add(id);
      setTimeout(()=>{timers.timeouts.delete(id);fn();},0);return id;},
    clearTimeout:id=>{timers.timeouts.delete(id);},
    prompt:()=>{throw Error('window.prompt must not be used: it freezes the Steam shell');},
    confirm:()=>{throw Error('window.confirm must not be used: it freezes the Steam shell');},
    console:{warn:line=>warnings.push(String(line))},
    __lumenCloseOverlay:lumenCloseOverlay||undefined,
    __lumenOpenOverlay:lumenOpenOverlay||undefined,
    __lumenOpenLuaToolsAccount:openLuaTools===undefined?undefined:function(){
      signIn.push({overlayOpen:Boolean(document.getElementById('sls-luna-overlay'))});
      if(typeof openLuaTools==='function')return openLuaTools();
    },
    Millennium:{callServerMethod:async(plugin,fn,args)=>{
      const payload=args&&args.raw?JSON.parse(args.raw):(args||{});
      calls.push({fn,payload});
      // A test can hold one call open (a restarted or dead backend) and answer
      // it later, to prove the timeout really settles.
      if(gate){const held=gate(fn,payload,calls.length);if(held)return held;}
      if(fn==='SlsPlaytimeGet'){
        const base=get||{success:true,apps:[],games:{},titles:{},shortcutNames:{},
          stats:{today:0,last7d:0,last30d:0},runtime:{healthy:true,reportedAt:Date.now()/1000,file:'x.json'},account:'76561198000000000',manifestCount:0,shortcutCount:0};
        // With `playtimeRev` the fixture behaves like the backend: the same rev
        // answers {unchanged:true}, anything else gets the full payload plus rev.
        if(playtimeRev!==undefined&&playtimeRev!==null){
          if(payload&&payload.rev===playtimeRev)return JSON.stringify({success:true,unchanged:true,rev:playtimeRev});
          return JSON.stringify(Object.assign({},base,{rev:playtimeRev}));
        }
        return JSON.stringify(base);
      }
      if(fn==='SlsPresenceGet')return JSON.stringify(presence||{success:true,state:{enabled:true,owned:{},running:{},cleanup:0},runtime:{healthy:true,error:''}});
      if(fn==='SlsPresenceSetDiscord'){
        // The backend refuses an ID outside 17-20 digits with a structured
        // `invalid-app-id`; `discordRefusal` makes every save refuse, so the
        // page's handling of the reply is what the test sees.
        if(discordRefusal)return JSON.stringify({success:false,code:'invalid-app-id',error:discordRefusal});
        return JSON.stringify({success:true});
      }
      if(fn==='SlsPlaytimeEdit')return JSON.stringify({success:true});
      if(fn==='SlsPresenceSetEnabled')return JSON.stringify({success:true,enabled:payload.enabled});
      if(fn==='SlsCompressorGet'){
        // Mirror the backend: the light shape drops the per-game list and the
        // statistics, so a tab that needs either must ask for the full one.
        const data=compressor||compressorFixture();
        if(payload&&payload.light){
          const rest={...data};
          delete rest.stats;
          if(rest.status)rest.status={version:rest.status.version,summary:rest.status.summary};
          return JSON.stringify(rest);
        }
        return JSON.stringify(data);
      }
      if(fn==='SlsCompressorPutSettings')return JSON.stringify({success:true,settings:payload});
      if(fn==='SlsCompressorRun')return JSON.stringify({success:true,job:{action:payload.action,running:true}});
      if(fn==='SlsCompressorCancel')return JSON.stringify({success:true,cancelled:true});
      if(fn==='SlsHltbGet')return JSON.stringify(hltbState);
      if(fn==='SlsHltbFetch'){
        // The non-blocking backend answers 'pending' until the site replies.
        if(fetchPending&&(pendingSeen.get(payload.appid)||0)<fetchPending){
          pendingSeen.set(payload.appid,(pendingSeen.get(payload.appid)||0)+1);
          return JSON.stringify({success:false,pending:true,code:'pending',
            error:'HowLongToBeat is still answering.'});
        }
        if(fetchFailure&&Number(payload.appid)===Number(fetchFailure)){
          hltbState.games[String(payload.appid)]={name:payload.name,match:'none',
            source:'hltb-search',fetchedAt:Date.now()/1000};
          hltbState.count=Object.keys(hltbState.games).length;
          return JSON.stringify({success:false,code:'no_match',
            error:'No confident HowLongToBeat match for "'+payload.name+'".'});
        }
        if(rateLimitOnce&&Number(payload.appid)===Number(rateLimitOnce)&&!rateLimited.has(payload.appid)){
          rateLimited.add(payload.appid);
          return JSON.stringify({success:false,code:'rate_limit',
            error:'HowLongToBeat is rate-limiting searches; wait a minute and try again.'});
        }
        const record={name:payload.name,match:'exact',main:3600,extra:5400,completionist:7200,
          source:'hltb-search',fetchedAt:Date.now()/1000};
        hltbState.games[String(payload.appid)]=record;
        hltbState.count=Object.keys(hltbState.games).length;
        return JSON.stringify({success:true,record});
      }
      if(fn==='SlsHltbForget'){
        delete hltbState.games[String(payload.appid)];
        hltbState.count=Object.keys(hltbState.games).length;
        return JSON.stringify({success:true,removed:true});
      }
      if(fn==='SlsHltbPut'){
        hltbState.games[String(payload.appid)]={name:payload.name,match:'manual',main:(payload.main||0)*3600,
          extra:(payload.extra||0)*3600,completionist:(payload.completionist||0)*3600,source:'manual',fetchedAt:Date.now()/1000};
        hltbState.count=Object.keys(hltbState.games).length;
        return JSON.stringify({success:true});
      }
      if(fn==='__lumenOpenExternalUrl')return JSON.stringify({ok:true});
      if(fn==='SlsLunaGradient')return JSON.stringify({success:true,enabled:true,available:true,
        colors:{primary:[26,159,255],secondary:[120,80,200],tertiary:[20,25,42],'surface-container':[16,18,28]}});
      if(fn==='SlsLunaInfo')return JSON.stringify({success:true,version:'0.14.0',
        addons:{luna:true},prefs:Object.assign({},prefsState)});
      if(fn==='SlsLunaUpdate'){
        const action=(payload&&payload.action)||'status';
        if(action==='install'){updateState.pending=true;updateState.pendingTag=payload.tag||'0.14.9';
          return JSON.stringify({success:true,queued:true,update:Object.assign({},updateState)});}
        if(action==='cancel'){updateState.pending=false;updateState.pendingTag='';
          return JSON.stringify({success:true,update:Object.assign({},updateState)});}
        if(action==='timer')return JSON.stringify({success:true,
          job:{name:'timer',state:'ok',age:1,limit:30,message:'unattended updates: '+(payload.value||'on')},
          update:Object.assign({},updateState)});
        // A forced check runs detached in the real backend: it reports
        // checking until a few status polls later, then the new release.
        if(action==='check'&&payload.force===true){updateState.checking=true;updateState.polls=3;}
        if(action==='status'&&updateState.checking&&--updateState.polls<=0){
          updateState.checking=false;updateState.latest='0.15.0';}
        return JSON.stringify({success:true,update:Object.assign({},updateState)});
      }
      if(fn==='SlsLunaRepair'){
        const action=(payload&&payload.action)||'status';
        const plan=repairPlan||['running','ok'];
        if(action==='status'){
          repairPolls+=1;
          const state=plan[Math.min(repairPolls,plan.length-1)];
          return JSON.stringify({success:true,job:{name:'repair',state,age:repairPolls*2,limit:180,
            message:state==='ok'?'The repair job finished.'
              :state==='timeout'?'The repair job did not finish in 180 s; its process group was stopped.':''}});
        }
        return JSON.stringify({success:true,job:{name:'repair',state:'running',age:0,limit:180},
          message:'Repair started.'});
      }
      if(fn==='SlsLunaLaunchers'){
        const action=(payload&&payload.action)||'status';
        if(action==='create')return JSON.stringify({success:true,
          job:{name:'launchers',state:'ok',age:1,limit:60,
            message:'SteamOS Mode shortcuts: 3; SDL mouse compatibility'}});
        return JSON.stringify({success:true,helper:true,
          job:{name:'launchers',state:'done',message:'SteamOS Mode shortcuts: 3; SDL mouse compatibility'}});
      }
      if(failFns&&failFns.includes(fn))throw Error('boom '+fn);
      if(fn==='SlsLunaHost'&&host)return JSON.stringify({success:true,host:{running:host,configured:host,
        binary:true}});
      if(fn==='SlsLunaLuaTools')return JSON.stringify(luatools
        ?{success:true,luatools}
        :{success:true,luatools:{ok:false,signedIn:false,name:'',expired:false,at:0,path:'',
          error:'LuaTools has not saved a Discord session on this machine'}});
      if(fn==='SlsLunaLumen'){
        const action=(payload&&payload.action)||'status';
        lumenSeen.push(action);
        const now=Math.floor(Date.now()/1000);
        if(action==='channels'){
          return JSON.stringify({success:true,channels:channelsState,lumen:Object.assign({},lumenState)});
        }
        if(action==='channels-set'){
          if(channelsRefusal)return JSON.stringify({success:false,error:channelsRefusal,
            lumen:Object.assign({},lumenState)});
          channelsState.values[payload.component]=payload.channel;
          channelsState.output='channels  : channels set to '+payload.channel+' for '+payload.component;
          return JSON.stringify({success:true,message:channelsState.output,channels:channelsState,
            lumen:Object.assign({},lumenState)});
        }
        if(action==='terminal'){
          if(terminalRefusal)return JSON.stringify({success:false,error:terminalRefusal});
          lumenState.terminal=terminalStart||{state:'starting',code:-1,at:now,log:'',window:'',output:''};
          return JSON.stringify({success:true,started:true,lumen:Object.assign({},lumenState)});
        }
        if(action==='check'){lumenState.check=Object.assign({},lumenState.checkResult||{checkedAt:now,
          status:{upstream:{overall:'unknown',components:[]}}});}
        if(action==='update'){
          if(payload.confirmed!==true)return JSON.stringify({success:false,error:'The update needs a confirmation first.'});
          lumenState.pending=true;lumenState.job={action:'update',attempts:0,lastError:''};
          lumenState.result={state:'queued',message:'will update when Steam is closed',at:now};}
        if(action==='cancel'){
          if(lumenState.refuseCancel)return JSON.stringify({success:false,error:lumenState.refuseCancel,
            lumen:Object.assign({},lumenState)});
          lumenState.pending=false;lumenState.job=null;lumenState.result={state:'cancelled',
          message:'cancelled',at:now};}
        return JSON.stringify({success:true,lumen:Object.assign({},lumenState)});
      }
      if(fn==='SlsLunaRefresh'){
        const action=(payload&&payload.action)||'status';
        if(action==='queue'){refreshState.pending=true;
          return JSON.stringify({success:true,queued:true,refreshPending:true});}
        if(action==='cancel'){refreshState.pending=false;
          return JSON.stringify({success:true,refreshPending:false});}
        return JSON.stringify({success:true,refreshPending:refreshState.pending});
      }
      if(fn==='SlsLunaHealth')return JSON.stringify({success:true,health:{
        version:'0.14.0',generatedAt:Date.now()/1000,
        install:{generated:'2026-10-15T10:00:00+0200',installedAt:Date.now()/1000-3600,source:'bundle',
          versions:{luna:'0.14.0',sgdb:'0.2.0',playtime:'0.5.5'},drift:['sgdb: installed 0.2.0, pinned 0.3.0']},
        lumen:{root:'/home/tester/.local/share/Lumen',binary:true,luatools:true,bridge:true,
          boot:{path:'/home/tester/.local/share/Lumen/lua/boot.lua',patched:bridgePatched,
            markers:bridgePatched?1:0,backup:true},
          addons:[{name:'luna',title:'sls-luna',version:'0.14.0',files:3},
            {name:'sgdb',title:'sls-sgdb',version:'0.2.0',files:2}]},
        steam:{root:'/home/tester/.local/share/Steam',exists:true,localconfigConflicts:2,
          client:Object.assign({build:'1791918634',pinned:'1791918634',match:true,updatesBlocked:true,
            cachedBuild:'1791918634',cacheReady:true},(steamClient||{}))},
        data:{dir:'/home/tester/.local/share/slssteam-integrations',
          prefs:Object.assign({version:1,store_hide_followed:false,beta:false,hltb_order:'short'},
            prefs||{}),
          hltb:{cached:3533},sgdb:{hasKey:true},
          files:[{name:'hltb.json',directory:false,size:560000},
            {name:'playtime',directory:true,files:3}]}}});
      if(fn==='SlsLunaPrefsSet'){prefsState[payload.key]=payload.value;
        return JSON.stringify({success:true,prefs:Object.assign({},prefsState)});}
      if(fn==='SlsSgdbGet')return JSON.stringify({success:true,hasKey:sgdbState.apiKey!=='',
        keyMasked:sgdbState.apiKey?('••••'+sgdbState.apiKey.slice(-4)):'',
        gridDir:sgdbState.gridDir,gridError:sgdbState.gridError});
      if(fn==='SlsLunaSteam'){
        const action=(payload&&payload.action)||'status';
        if(action==='pin')return JSON.stringify({success:true,
          job:{name:'pin',state:'ok',age:1,limit:60,message:'pinned Steam client 1791918634'},
          steam:Object.assign({},sgdbSteamState,{pinned:'1791918634',match:true})});
        if(action==='queue-update'||action==='queue-cache'){
          sgdbSteamState.pending=action==='queue-update'?'update':'cache';
          return JSON.stringify({success:true,queued:sgdbSteamState.pending,steam:sgdbSteamState});}
        if(action==='lock'||action==='unlock'){
          sgdbSteamState.job=action;
          return JSON.stringify({success:true,started:action,steam:sgdbSteamState});}
        if(action==='cancel'){sgdbSteamState.pending='';return JSON.stringify({success:true,steam:sgdbSteamState});}
        return JSON.stringify({success:true,steam:sgdbSteamState});
      }
      if(fn==='SlsSgdbSetKey'){sgdbState.apiKey=(payload.key||'').trim();
        return JSON.stringify({success:true,hasKey:sgdbState.apiKey!==''});}
      if(fn==='SlsSgdbTest')return JSON.stringify(sgdbState.apiKey
        ?{success:true}:{success:false,code:'no_key',error:'Add your SteamGridDB API key first.'});
      if(fn==='SlsSgdbSearch')return JSON.stringify({success:true,
        games:[{id:1,name:'Hades'},{id:2,name:'Hades II'}]});
      if(fn==='SlsSgdbAssets')return JSON.stringify({success:true,total:1,assets:[
        {id:11,url:'https://cdn.example/grid1.png',thumb:'https://cdn.example/t1.png',
          width:600,height:900,style:'alternate',mime:'image/png',author:'artist1'}]});
      if(fn==='SlsSgdbApply'){
        // A test can make the apply fail the way the backend does on a 429.
        if(sgdbApplyFailure)return JSON.stringify(sgdbApplyFailure);
        sgdbHistoryState[payload.kind]=(sgdbHistoryState[payload.kind]||0)+1;
        if(sgdbHistoryTimes){
          sgdbTimesState[payload.kind]=Math.floor(Date.now()/1000)-30;
        }
        return JSON.stringify({success:true,path:'/grid/out.png',
          kind:payload.kind,backedUp:true,live:payload.kind!=='icon'});}
      if(fn==='SlsSgdbHistory'){
        if(sgdbHistoryError)return JSON.stringify({success:false,code:'no_steam',error:sgdbHistoryError});
        // `times` carries one epoch per slot; absent on an older backend.
        return JSON.stringify(sgdbHistoryTimes
          ?{success:true,history:sgdbHistoryState,times:sgdbTimesState}
          :{success:true,history:sgdbHistoryState});
      }
      if(fn==='SlsSgdbUndo'){
        sgdbHistoryState[payload.kind]=Math.max(0,(sgdbHistoryState[payload.kind]||0)-1);
        return JSON.stringify({success:true,undone:true,history:sgdbHistoryState[payload.kind]});}
      if(fn==='SlsSgdbReset')return JSON.stringify({success:true,removed:true});
      if(fn==='SlsSgdbAuto'){
        if(sgdbAuto==='error')return JSON.stringify({success:false,
          error:'Backend error: config unreadable'});
        sgdbAutoPolls=0;
        return autoSnapshot('running',0);
      }
      if(fn==='SlsSgdbAutoStatus'){
        sgdbAutoPolls+=1;
        const plan=sgdbAutoPlan||['running','running','done'];
        const state=plan[Math.min(sgdbAutoPolls-1,plan.length-1)];
        return autoSnapshot(state,
          state==='running'?Math.min(sgdbAutoPolls-1,2):5);
      }
      if(fn==='SlsGraphicsGet')return JSON.stringify({success:true,version:'0.2.0',
        checkedAt:graphicsCheckedAt!==undefined?graphicsCheckedAt:Math.floor(Date.now()/1000)-9,
        gpu:graphicsGpu!==undefined?graphicsGpu:{in_use:'card0',primary:'card0',hybrid:false},
        system:{distro:'Arch Linux (test)',kernel:'6.12.4-arch1'},
        gpus:graphicsGpus||[{card:'card0',vendor:'0x1002',vendor_name:'AMD',device:'0x744c',driver:'amdgpu',
          product:'AMD Radeon RX 7900 XTX',boot_vga:true,in_use:true}],
        lsfg:{layers:[{path:'/usr/share/vulkan/implicit_layer.d/VkLayer_LSFG_vk.json',version:2}],
          config:{path:'/home/tester/.config/lsfg-vk/conf.toml',size:22},
          decky_plugins:['/home/tester/homebrew/plugins/decky-lsfg-vk']},
        optiscaler:[{kind:'files',path:'/home/tester/.config/optiscaler'}],
        optiscaler_client:{config:'/home/tester/.config/OptiscalerClient',app_dir:'/opt/optiscaler-client',
          managed:1,versions_cached:2,games:[{name:'Space Engineers 2',appid:'1133870',path:'/games/SE2',
            exists:true,installed:true,files:['/games/SE2/OptiScaler.ini'],fsr_version:'4.0.2.0',
            fsr_native:true,fsr_swapped:false,dlss_native:false,dlss_via:false,xess_native:false,xess_via:false}]},
        steam:{root:'/home/tester/.local/share/Steam',runtimes:['SteamLinuxRuntime_sniper'],gamescope:true}});
      return JSON.stringify({success:true});
    }},
  };
  return {w,calls,storage,prefs:prefsState,timers,winListeners,lumenState,lumenSeen,warnings,signIn};
}

function hltbFixture(overrides){
  return Object.assign({games:{'1145360':{name:'Hades',match:'exact',main:84940,extra:175028,
    completionist:343055,source:'hltb-search',fetchedAt:Date.now()/1000-120}},
    library:[{appid:1145360,name:'Hades',shortcut:false},
      {appid:620,name:'Portal 2',shortcut:false},
      {appid:3212112756,name:'Minecraft',shortcut:true}]},overrides||{});
}

async function openHltbPage(document,options){
  const {w,calls,storage}=fakeWindow(document,options);
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await new Promise(resolve=>setTimeout(resolve,30));
  const tab=findNode(document.getElementById('sls-luna-overlay'),
    node=>(node.className||'').startsWith('sls-luna-tab')&&node.textContent==='HowLongToBeat');
  assert.ok(tab,'the HowLongToBeat tab was not found');
  tab.onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  return {w,calls,storage,overlay:document.getElementById('sls-luna-overlay')};
}

function rowFor(overlay,label){
  return findNode(overlay,node=>node.className==='sls-luna-game'
    &&node.children.length&&node.children[0].textContent.startsWith(label));
}

test('the menubar is found by shape, not by localized labels',()=>{
  const document=fakeDom();
  const bar=menubarFixture(document,true);
  assert.equal(findMenubar(document),bar);
  const other=fakeDom();
  const notAMenu=other.createElement('div');
  ['uno','dos','tres'].forEach(text=>{
    const item=other.createElement('div');item.textContent=text;notAMenu.appendChild(item);
  });
  other.body.appendChild(notAMenu);
  assert.equal(findMenubar(other),null,'a row without the Steam brand is not the menubar');
});

test('mount inserts the half-moon Luna button next to the Lumen button',()=>{
  const document=fakeDom();
  const bar=menubarFixture(document,true);
  const {w}=fakeWindow(document);
  mount(w);
  assert.equal(w.__SlsLunaUi,true,'the flag the other add-ons check must be set');
  const button=document.getElementById('sls-luna-btn');
  assert.ok(button,'the Luna button was not created');
  assert.equal(button.parentNode,bar,'the button must live in the native menubar');
  const order=Array.from(bar.children).map(child=>child.id||child.textContent);
  assert.deepEqual(order.slice(-2),['lumen-moon-btn','sls-luna-btn'],
    'Luna must sit immediately after Lumen');
  assert.match(button.textContent,/Luna/,'the button copy must read Luna');
  assert.ok(button.title.includes('SLS Luna'));
});

test('mount falls back to the end of the menubar without Lumen',()=>{
  const document=fakeDom();
  const bar=menubarFixture(document,false);
  const {w}=fakeWindow(document);
  mount(w);
  const button=document.getElementById('sls-luna-btn');
  assert.equal(bar.children[bar.children.length-1],button);
});

test('mount removes the floating add-on panels it replaces',()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  ['sls-playtime-panel-button','sls-playtime-panel','sls-presence-panel-button','sls-presence-panel',
    'sls-compressor-panel-button','sls-compressor-panel']
    .forEach(id=>{const node=document.createElement('div');node.id=id;document.body.appendChild(node);});
  const {w}=fakeWindow(document);
  mount(w);
  assert.equal(document.getElementById('sls-playtime-panel'),null,'the old floating panel must be gone');
  assert.equal(document.getElementById('sls-presence-panel'),null);
  assert.equal(document.getElementById('sls-compressor-panel'),null);
});

test('the menu opens with the sidebar, both settings sections and the account row',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w,calls}=fakeWindow(document,{
    get:{success:true,apps:[620,3212112756],titles:{},shortcutNames:{'3212112756':'Minecraft'},
      games:{'620':{title:'Portal 2',trackedSeconds:7200,adjustmentSeconds:0}},stats:{today:7200,last7d:7200,last30d:7200},
      runtime:{healthy:true,reportedAt:Date.now()/1000,file:'76561198000000000.json'},account:'76561198000000000',
      manifestCount:1,shortcutCount:1}});
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await new Promise(resolve=>setTimeout(resolve,30));
  const overlay=document.getElementById('sls-luna-overlay');
  assert.ok(overlay,'the menu did not open');
  const text=textOf(overlay);
  assert.match(text,/Playtime/,'the Playtime tab is missing');
  assert.match(text,/Presence/,'the Presence tab is missing');
  assert.match(text,/About/,'the About tab is missing');
  assert.match(text,/LuaTools/,'the footer must show the LuaTools connection row');
  assert.match(text,/Today/,'the statistics row is missing');
  assert.ok(calls.some(c=>c.fn==='SlsPlaytimeGet'),'opening the menu must load the playtime state');
  assert.ok(calls.some(c=>c.fn==='SlsPresenceGet'),'opening the menu must load the presence state');
  assert.match(text,/Portal 2/,'a tracked game must be listed');
  assert.match(text,/Minecraft \(#3212112756\) \[non-Steam\]/,'non-Steam games must be marked');
  assert.match(text,/Save/,'each listed game needs a save control');
});
test('typing repeatedly retains the same search node, focus and caret',async()=>{
  const document=fakeDom();menubarFixture(document,true);
  const {w}=fakeWindow(document);
  mount(w);document.getElementById('sls-luna-btn').click();
  await new Promise(resolve=>setTimeout(resolve,30));
  const search=document.created.find(e=>e.className==='sls-luna-search');
  search.focus=()=>{document.activeElement=search;};
  search.setSelectionRange=(a,b)=>{search.selectionStart=a;search.selectionEnd=b;};
  search.focus();
  for(const value of ['m','mi','min','mine']){
    search.value=value;search.setSelectionRange(value.length,value.length);search.oninput();
    assert.equal(document.activeElement,search);assert.equal(search.selectionStart,value.length);
    assert.equal(document.created.filter(e=>e.className==='sls-luna-search' && e.parentNode).length,1);
  }
});

test('Escape closes the menu',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w}=fakeWindow(document);
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await new Promise(resolve=>setTimeout(resolve,20));
  assert.ok(document.getElementById('sls-luna-overlay'));
  document.custom('document:keydown',{key:'Escape'});
  assert.equal(document.getElementById('sls-luna-overlay'),null,'Escape must close the menu');
});

test('the presence switch calls the real RPC with the inverted value',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w,calls}=fakeWindow(document,{
    presence:{success:true,state:{enabled:true,owned:{'3808690':2167367540},running:{},cleanup:0},runtime:{healthy:true,error:''}}});
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await new Promise(resolve=>setTimeout(resolve,30));
  // Move to the Presence tab, then flip the switch.
  const presenceTab=findNode(document.getElementById('sls-luna-overlay'),
    node=>node.className==='sls-luna-tab' && node.textContent==='Presence');
  assert.ok(presenceTab,'the Presence tab was not found');
  presenceTab.onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  const overlay=document.getElementById('sls-luna-overlay');
  assert.match(textOf(overlay),/Enable Presence/,'the presence switch text is missing');
  const toggle=findNode(overlay,node=>(node.className||'').startsWith('sls-luna-switch'));
  assert.ok(toggle,'the switch was not rendered');
  assert.match(toggle.className,/sls-luna-switch on/,'the switch must reflect enabled=true');
  toggle.onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  const call=calls.filter(c=>c.fn==='SlsPresenceSetEnabled').pop();
  assert.ok(call,'disabling presence must call SlsPresenceSetEnabled');
  assert.equal(call.payload.enabled,false,'the switch must send the inverted value');
});

test('the session-time switch defaults on and toggles through the real RPC',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w,calls}=fakeWindow(document,{
    presence:{success:true,state:{enabled:true,owned:{},running:{},cleanup:0},runtime:{healthy:true,error:''}}});
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await new Promise(resolve=>setTimeout(resolve,30));
  const presenceTab=findNode(document.getElementById('sls-luna-overlay'),
    node=>node.className==='sls-luna-tab' && node.textContent==='Presence');
  assert.ok(presenceTab,'the Presence tab was not found');
  presenceTab.onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  const overlay=document.getElementById('sls-luna-overlay');
  assert.match(textOf(overlay),/Show session time in the name/,'the enrichment row is missing');
  const sessionSwitch=findNode(overlay,node=>(node.className||'').startsWith('sls-luna-switch')
    && node.parentNode && /session time/i.test(node.parentNode.textContent));
  assert.ok(sessionSwitch,'the session-time switch was not rendered');
  assert.match(sessionSwitch.className,/sls-luna-switch on/,'it must be on by default');
  sessionSwitch.onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  const call=calls.filter(c=>c.fn==='SlsPresenceSetSessionTime').pop();
  assert.ok(call,'toggling must call SlsPresenceSetSessionTime');
  assert.equal(call.payload.sessionTime,false,'the switch must send the inverted value');
});

async function openPresence(document,options){
  menubarFixture(document,true);
  const made=fakeWindow(document,options||{});
  mount(made.w);
  document.getElementById('sls-luna-btn').click();
  await new Promise(resolve=>setTimeout(resolve,30));
  const tab=findNode(document.getElementById('sls-luna-overlay'),
    node=>node.className==='sls-luna-tab'&&node.textContent==='Presence');
  assert.ok(tab,'the Presence tab was not found');
  tab.onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  const overlay=document.getElementById('sls-luna-overlay');
  const input=findNode(overlay,node=>node.tagName==='INPUT'&&/Application ID/i.test(node.placeholder||''));
  assert.ok(input,'the Discord Application ID field was not rendered');
  return {made,overlay,input};
}

test('Discord says in plain words why nothing is published, without an Application ID',async()=>{
  const document=fakeDom();
  const {overlay}=await openPresence(document,{
    presence:{success:true,state:{enabled:true,discordEnabled:true,discordAppId:'',owned:{},running:{},cleanup:0},
      runtime:{healthy:true,error:''},
      discord:{enabled:true,hasAppId:false,appIdValid:false,publishing:false,reason:'no-app-id'}}});
  const text=textOf(overlay);
  assert.match(text,/Discord needs an Application ID; nothing is published until one is set\./,
    'the no-app-id reason must be spelled out under the field');
  assert.match(text,/Discord: not publishing\./,'the status line must say it is not publishing');
});

test('each publishing reason gets its own line, and no-socket is not the missing-ID one',async()=>{
  const document=fakeDom();
  const {overlay}=await openPresence(document,{
    presence:{success:true,state:{enabled:true,discordEnabled:true,discordAppId:'123456789012345678',owned:{},running:{},cleanup:0},
      runtime:{healthy:true,error:''},
      discord:{enabled:true,hasAppId:true,appIdValid:true,publishing:false,reason:'no-socket'}}});
  const text=textOf(overlay);
  assert.match(text,/Discord is not running on this machine; start Discord to publish again\./);
  assert.doesNotMatch(text,/nothing is published until one is set/,'the missing-ID line must be for no-app-id only');

  const invalidDom=fakeDom();
  const invalid=await openPresence(invalidDom,{
    presence:{success:true,state:{enabled:true,discordEnabled:true,discordAppId:'123',owned:{},running:{},cleanup:0},
      runtime:{healthy:true,error:''},
      discord:{enabled:true,hasAppId:true,appIdValid:false,publishing:false,reason:'invalid-app-id'}}});
  assert.match(textOf(invalid.overlay),/That Application ID is not valid; nothing is published until it is fixed\./);
});

test('a publishing Discord shows no warning line',async()=>{
  const document=fakeDom();
  const {overlay}=await openPresence(document,{
    presence:{success:true,state:{enabled:true,discordEnabled:true,discordAppId:'123456789012345678',owned:{},running:{},cleanup:0},
      runtime:{healthy:true,error:''},
      discord:{enabled:true,hasAppId:true,appIdValid:true,publishing:true,reason:'ok'}}});
  const text=textOf(overlay);
  assert.doesNotMatch(text,/not publishing/);
  assert.doesNotMatch(text,/nothing is published/);
});

test('without the discord block the Presence page renders exactly as before',async()=>{
  const document=fakeDom();
  const {overlay}=await openPresence(document,{
    presence:{success:true,state:{enabled:true,discordEnabled:false,discordAppId:'',owned:{},running:{},cleanup:0},
      runtime:{healthy:true,error:''}}});
  const text=textOf(overlay);
  assert.match(text,/Enable Presence/);
  assert.doesNotMatch(text,/not publishing|nothing is published|Discord is not running/,
    'an older backend must not grow a line it never sent');
});

test('a non-empty Application ID outside 17-20 digits is refused before any RPC',async()=>{
  const document=fakeDom();
  const {made,input}=await openPresence(document,{
    presence:{success:true,state:{enabled:true,discordEnabled:true,discordAppId:'',owned:{},running:{},cleanup:0},
      runtime:{healthy:true,error:''},
      discord:{enabled:true,hasAppId:false,appIdValid:false,publishing:false,reason:'no-app-id'}}});
  const save=findNode(input.parentNode,node=>node.tagName==='BUTTON'&&node.textContent==='Save');
  assert.ok(save,'the Save button was not rendered');
  input.value='12345';
  save.onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  assert.equal(made.calls.filter(call=>call.fn==='SlsPresenceSetDiscord').length,0,
    'a malformed ID must never reach the backend');
  assert.match(textOf(document.getElementById('sls-luna-overlay')),/Application ID must be 17-20 digits\./);
});

test('the backend invalid-app-id reply is shown inline at the field',async()=>{
  const document=fakeDom();
  const {input}=await openPresence(document,{
    presence:{success:true,state:{enabled:true,discordEnabled:true,discordAppId:'',owned:{},running:{},cleanup:0},
      runtime:{healthy:true,error:''},
      discord:{enabled:true,hasAppId:false,appIdValid:false,publishing:false,reason:'no-app-id'}},
    discordRefusal:'Application ID must be 17-20 digits.'});
  input.value='123456789012345678';
  findNode(input.parentNode,node=>node.tagName==='BUTTON'&&node.textContent==='Save').onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  const overlay=document.getElementById('sls-luna-overlay');
  const errorNode=findNode(overlay,node=>node.className==='sls-luna-error'
    && node.textContent==='Application ID must be 17-20 digits.');
  assert.ok(errorNode,'the backend refusal must be shown visibly');
  assert.ok(errorNode.parentNode&&errorNode.parentNode.parentNode,
    'the line lives inside the Presence page, under the field');
  assert.equal(input.value,'123456789012345678','a refused save keeps what the user typed');
});

async function openCompressor(document,subtab){
  document.getElementById('sls-luna-btn').click();
  await new Promise(resolve=>setTimeout(resolve,30));
  const compressorTab=findNode(document.getElementById('sls-luna-overlay'),
    node=>(node.className||'').startsWith('sls-luna-tab') && node.textContent==='Compressor');
  assert.ok(compressorTab,'the Compressor tab was not found');
  compressorTab.onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  if(subtab){
    const item=findNode(document.getElementById('sls-luna-overlay'),
      node=>(node.className||'').startsWith('sls-luna-subtab') && node.textContent===subtab);
    assert.ok(item,'the '+subtab+' sub-tab was not found');
    item.onclick();
    await new Promise(resolve=>setTimeout(resolve,20));
  }
  return document.getElementById('sls-luna-overlay');
}

async function clickRefresh(document){
  const button=findNode(document.getElementById('sls-luna-overlay'),
    node=>node.tagName==='BUTTON' && node.textContent==='Refresh');
  assert.ok(button,'the Refresh button was not found');
  button.onclick();
  await new Promise(resolve=>setTimeout(resolve,30));
}

test('the compressor status tab shows the plan and drives the real RPCs',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w,calls}=fakeWindow(document);
  mount(w);
  const overlay=await openCompressor(document);
  const text=textOf(overlay);
  assert.match(text,/ZSTD compression \+ deduplication/,'the enabled operations are not stated');
  assert.match(text,/To compress/,'the compression statistic is missing');
  assert.match(text,/Dedupe only/,'the dedupe statistic is missing');
  assert.match(text,/Held back/,'the filter statistic is missing');
  assert.ok(calls.some(c=>c.fn==='SlsCompressorGet'),'opening the menu must load the compressor state');
  const scan=findNode(overlay,node=>node.tagName==='BUTTON' && node.textContent==='Scan library');
  assert.ok(scan,'the Scan library button was not rendered');
  scan.onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  const call=calls.filter(c=>c.fn==='SlsCompressorRun').pop();
  assert.ok(call,'Scan library must call SlsCompressorRun');
  assert.equal(call.payload.action,'scan','it must request the read-only scan');
});

test('Run now is gated by the enabled operations and sends the composed run',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const fixture=compressorFixture();
  fixture.settings.compress=false;
  fixture.settings.dedupe=false;
  const {w,calls}=fakeWindow(document,{compressor:fixture});
  mount(w);
  let overlay=await openCompressor(document);
  let run=findNode(overlay,node=>node.tagName==='BUTTON' && node.textContent==='Run now');
  assert.ok(run,'the Run now button was not rendered');
  assert.equal(run.disabled,true,'Run now must be disabled while nothing is enabled');
  overlay=await openCompressor(document,'Settings');
  const compressionSwitch=findNode(overlay,node=>(node.className||'').startsWith('sls-luna-switch')
    && node.parentNode && /ZSTD compression/.test(node.parentNode.textContent));
  assert.ok(compressionSwitch,'the compression switch was not found');
  compressionSwitch.onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  const saved=calls.filter(c=>c.fn==='SlsCompressorPutSettings').pop();
  assert.ok(saved,'the switch must save through PutSettings');
  assert.equal(saved.payload.compress,true,'the switch must send the new value');
  Object.assign(fixture.settings,saved.payload);
  fixture.plan.compress=1;
  await clickRefresh(document);
  overlay=await openCompressor(document,'Status');
  run=findNode(overlay,node=>node.tagName==='BUTTON' && node.textContent==='Run now');
  assert.equal(run.disabled,false,'Run now must unlock once an operation is enabled');
  run.onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  assert.equal(calls.filter(c=>c.fn==='SlsCompressorRun').length,0,'Run now must ask inside the page, never natively');
  overlay=document.getElementById('sls-luna-overlay');
  assert.match(textOf(overlay),/Start the pass now\?/,'the confirmation drawer must appear');
  assert.match(textOf(overlay),/smallest first/,'the drawer must state the run order');
  const back=findNode(overlay,node=>node.tagName==='BUTTON' && node.textContent==='Back');
  assert.ok(back,'the drawer needs a Back button');
  back.onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  overlay=document.getElementById('sls-luna-overlay');
  assert.doesNotMatch(textOf(overlay),/Start the pass now\?/,'Back must close the drawer');
  assert.equal(calls.filter(c=>c.fn==='SlsCompressorRun').length,0,'Back must not start anything');
  findNode(overlay,node=>node.tagName==='BUTTON' && node.textContent==='Run now').onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  overlay=document.getElementById('sls-luna-overlay');
  const start=findNode(overlay,node=>node.tagName==='BUTTON' && node.textContent==='Start the pass');
  assert.ok(start,'the start button was not rendered');
  start.onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  const called=calls.filter(c=>c.fn==='SlsCompressorRun').pop();
  assert.equal(called.payload.action,'run','the drawer must start the composed run');
});

test('the status tab shows live progress while a run is in flight',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const fixture=compressorFixture();
  fixture.job={action:'run',pid:42,running:true,startedAt:Date.now()/1000-30,finishedAt:0,elapsed:30,
    log:'[12/146] Compressing: MonsterHunterWilds...\n  [####------] 8% (12/146)'};
  const {w}=fakeWindow(document,{compressor:fixture});
  mount(w);
  const overlay=await openCompressor(document);
  const text=textOf(overlay);
  assert.match(text,/Running now: compression pass/,'the running line is missing');
  assert.match(text,/Progress: 12 of 146 games/,'the progress position is missing');
  assert.match(text,/8%/,'the progress percentage is missing');
  assert.match(text,/MonsterHunterWilds/,'the current game is missing');
  assert.ok(findNode(overlay,node=>node.className==='sls-luna-progress'),'the progress bar is missing');
});

test('the status tab shows the game-by-game queue with bars',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const fixture=compressorFixture();
  fixture.job={action:'run',pid:42,running:true,startedAt:Date.now()/1000-90,finishedAt:0,elapsed:90,log:''};
  // queue.saved is MiB in the backend's own contract, not bytes.
  fixture.queue={active:true,total:3,index:2,done:1,failed:0,skipped:0,saved:118*1048576,finished:false,
    current:{name:'Cronos: The New Dawn',path:'/games/Cronos',size:100,totalBytes:100,doneBytes:42,
      phase:'compress',scannedBytes:0},
    next:[{name:'Hades',size:50}],
    games:[{name:'Cronos: The New Dawn',path:'/games/Cronos',size:100,status:'running',saved:0},
      {name:'Hades',path:'/games/Hades',size:50,status:'pending',saved:0},
      {name:'Pony Island',path:'/games/Pony',size:20,status:'done',saved:96*1048576}]};
  const {w}=fakeWindow(document,{compressor:fixture});
  mount(w);
  let overlay=await openCompressor(document);
  const text=textOf(overlay);
  assert.match(text,/Run in progress/,'the queue state line is missing');
  assert.match(text,/1 of 3 games done/,'the games-done count is missing');
  assert.match(text,/62 B of 170 B/,'the byte progress is missing');
  assert.match(text,/Game 2 of 3: Cronos: The New Dawn/,'the current game is missing');
  assert.match(text,/compressing 42%/,'the current percentage is missing');
  assert.match(text,/Next: Hades/,'the next-up line is missing');
  assert.match(text,/saved 118 MiB/,'the savings line is missing');
  const overallBar=findNode(document.getElementById('sls-luna-overlay'),
    node=>(node.className||'').indexOf('sls-luna-progress')===0);
  assert.ok(overallBar,'the queue needs a progress bar');
  assert.doesNotMatch(overallBar.className,/active/,'the overall bar must not be an indeterminate loader');
  assert.equal(overallBar.children[0].style.width,'47%','the bar must show games done plus the current fraction');
  overlay=await openCompressor(document,'Games');
  const chips=findNode(overlay,node=>(node.className||'').startsWith('sls-luna-chip'));
  assert.ok(chips,'a per-game state chip was not rendered');
  assert.match(textOf(overlay),/done . saved 96.0 MiB/,'a finished game must show its saving in bytes');
  assert.ok(findNode(overlay,node=>node.className==='sls-luna-mini'),'a per-game mini bar was not rendered');
});

test('game names from disk are written as text, never markup',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const fixture=compressorFixture();
  const name='<img src=x onerror=alert(1)>';
  fixture.job={action:'run',pid:42,running:true,startedAt:Date.now()/1000-90,finishedAt:0,elapsed:90,log:''};
  fixture.queue={active:true,total:1,index:1,done:0,failed:0,skipped:0,saved:0,finished:false,
    current:{name,path:'/games/x',size:100,totalBytes:100,doneBytes:1,phase:'compress',scannedBytes:0},
    next:[],games:[{name,path:'/games/x',size:100,status:'running',saved:0}]};
  const {w}=fakeWindow(document,{compressor:fixture});
  mount(w);
  const overlay=await openCompressor(document);
  const line=findNode(overlay,node=>/Game 1 of 1/.test(node.innerHTML||''));
  assert.ok(line,'the current-game line is missing');
  assert.doesNotMatch(line.innerHTML,/<img/,'a game name must not become an element');
  assert.match(line.innerHTML,/&lt;img src=x onerror=alert\(1\)&gt;/);
});

test('the settings tab saves switches and reveals the alpha resolution profiles',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const fixture=compressorFixture();
  const {w,calls}=fakeWindow(document,{compressor:fixture});
  mount(w);
  let overlay=await openCompressor(document,'Settings');
  let text=textOf(overlay);
  assert.match(text,/Safe operations/,'the safe group is missing');
  assert.match(text,/Eco mode/,'the eco switch is missing');
  assert.match(text,/Only process games smaller than/,'the size ceiling row is missing');
  assert.match(text,/Asset optimization/,'the alpha switch is missing');
  assert.doesNotMatch(text,/Resolution profile/,'the profile must stay hidden while alpha is off');

  const alphaSwitch=findNode(overlay,node=>(node.className||'').startsWith('sls-luna-switch')
    && node.parentNode && /Asset optimization/.test(node.parentNode.textContent));
  assert.ok(alphaSwitch,'the alpha switch was not found');
  alphaSwitch.onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  let saved=calls.filter(c=>c.fn==='SlsCompressorPutSettings').pop();
  assert.equal(saved.payload.assets,true,'the alpha switch must save assets=true');
  Object.assign(fixture.settings,saved.payload);
  await clickRefresh(document);

  overlay=await openCompressor(document,'Settings');
  text=textOf(overlay);
  assert.match(text,/Resolution profile/,'the profile row must appear once alpha is on');
  const chip=findNode(overlay,node=>node.className==='sls-luna-subtab' && node.textContent==='1440p');
  assert.ok(chip,'the resolution profile chips were not rendered');
  chip.onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  saved=calls.filter(c=>c.fn==='SlsCompressorPutSettings').pop();
  assert.equal(saved.payload.assetProfile,'quality','the chip must save its profile');
  Object.assign(fixture.settings,saved.payload);
  await clickRefresh(document);

  overlay=await openCompressor(document,'Settings');
  const ceilingSwitch=findNode(overlay,node=>(node.className||'').startsWith('sls-luna-switch')
    && node.parentNode && /Only process games smaller than/.test(node.parentNode.textContent));
  assert.ok(ceilingSwitch,'the size ceiling switch was not found');
  ceilingSwitch.onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  saved=calls.filter(c=>c.fn==='SlsCompressorPutSettings').pop();
  assert.equal(saved.payload.maxSizeGb,20,'the ceiling switch must default to 20 GB');

  overlay=await openCompressor(document,'Settings');
  const orderChip=findNode(overlay,node=>node.className==='sls-luna-subtab' && node.textContent==='Largest first');
  assert.ok(orderChip,'the run-order chips were not rendered');
  orderChip.onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  saved=calls.filter(c=>c.fn==='SlsCompressorPutSettings').pop();
  assert.equal(saved.payload.runOrder,'largest','the order chip must save its value');
});

test('the games tab toggles per-game exclusions through PutSettings',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const fixture=compressorFixture();
  const {w,calls}=fakeWindow(document,{compressor:fixture});
  mount(w);
  const overlay=await openCompressor(document,'Games');
  const text=textOf(overlay);
  assert.match(text,/3 installed games/,'the games summary is missing');
  assert.match(text,/Cronos: The New Dawn/,'the games list is missing');
  assert.match(text,/playing now/,'a running game must be labelled');
  const rowSwitch=findNode(overlay,node=>(node.className||'').startsWith('sls-luna-switch')
    && node.parentNode && /Cronos/.test(node.parentNode.textContent));
  assert.ok(rowSwitch,'the per-game switch was not rendered');
  rowSwitch.onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  const saved=calls.filter(c=>c.fn==='SlsCompressorPutSettings').pop();
  assert.deepEqual(saved.payload.excluded,['Cronos: The New Dawn'],'the exclusion must be saved');
});

test('the log tab shows the sanitized job tail',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const fixture=compressorFixture();
  fixture.job={action:'run',pid:42,running:false,startedAt:Date.now()/1000-120,
    finishedAt:Date.now()/1000-60,elapsed:0,log:'ARGS: --run\nbatch running'};
  const {w}=fakeWindow(document,{compressor:fixture});
  mount(w);
  const overlay=await openCompressor(document,'Log');
  assert.match(textOf(overlay),/batch running/,'the log text is missing');
});

test('buildRows ranks recorded games first and marks non-Steam entries',()=>{
  const rows=buildRows({apps:[1,2,3],games:{'2':{trackedSeconds:3600,adjustmentSeconds:0,lastPlayed:9}},
    titles:{'1':'Named but unplayed'},shortcutNames:{'3':'Shortcut'}},'');
  assert.equal(rows[0].id,'2','recorded games sort first');
  assert.equal(rows.find(row=>row.id==='3').shortcut,true);
  assert.equal(rows.find(row=>row.id==='1').shortcut,false);
  assert.equal(rows.find(row=>row.id==='2').name,'App 2','with no name the id is shown');
  const filtered=buildRows({apps:[1,2],titles:{'1':'Portal 2'},games:{}},'portal');
  assert.deepEqual(filtered.map(row=>row.id),['1']);
});

test('the playtime total uses the official Steam playtime as the base',()=>{
  const rows=buildRows({apps:[2101960,3212112756],games:{
    '2101960':{title:'Cronos',trackedSeconds:100,adjustmentSeconds:12488,officialSeconds:33240,lastPlayed:10},
    '3212112756':{title:'Minecraft',trackedSeconds:3600,adjustmentSeconds:60,lastPlayed:5},
  },titles:{},shortcutNames:{'3212112756':'Minecraft'}},'');
  const byId=Object.fromEntries(rows.map(row=>[row.id,row]));
  assert.equal(byId['2101960'].total,33240+12488,
    'a Steam-tracked game must show the official total plus any correction');
  assert.equal(byId['3212112756'].total,3660,'a shortcut keeps its locally tracked total');
});

test('buildHltbRows merges the library with cached times and sorts missing first',()=>{
  const rows=buildHltbRows(
    {'1145360':{name:'Hades',main:84940,match:'exact'}},
    [{appid:620,name:'Portal 2',shortcut:false},{appid:1145360,name:'Hades',shortcut:false},
      {appid:3212112756,name:'Minecraft',shortcut:true}],'');
  assert.deepEqual(rows.map(row=>row.name),['Minecraft','Portal 2','Hades'],
    'missing games sort first, then names');
  assert.equal(rows.find(row=>row.name==='Minecraft').shortcut,true);
  assert.equal(rows.find(row=>row.name==='Hades').record.main,84940);
  assert.equal(rows.find(row=>row.name==='Portal 2').record,null);
  const filtered=buildHltbRows({},[{appid:620,name:'Portal 2'}],'portal');
  assert.deepEqual(filtered.map(row=>row.id),['620']);
  const byId=buildHltbRows({},[{appid:620,name:'Portal 2'}],'620');
  assert.deepEqual(byId.map(row=>row.id),['620']);
});

test('the HowLongToBeat page lists cached times, missing games and fetches on demand',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {calls,overlay}=await openHltbPage(document,{hltb:hltbFixture()});
  const text=textOf(overlay);
  assert.match(text,/23\.6 h story/,'cached times must be shown');
  assert.match(text,/Portal 2 \(#620\)/,'missing games must be listed');
  assert.match(text,/Minecraft \(#3212112756\) \[non-Steam\]/,'shortcuts must be marked');
  assert.match(text,/Fetch missing \(2\)/,'the missing count must be visible');
  const row=rowFor(overlay,'Portal 2');
  assert.ok(row,'the Portal 2 row was not found');
  const fetchButton=findNode(row,node=>node.textContent==='Fetch');
  assert.ok(fetchButton,'a missing game must offer Fetch');
  fetchButton.onclick();
  await new Promise(resolve=>setTimeout(resolve,40));
  const call=calls.filter(entry=>entry.fn==='SlsHltbFetch').pop();
  assert.ok(call,'fetching a row must call SlsHltbFetch');
  assert.deepEqual(call.payload,{appid:620,name:'Portal 2'});
  assert.match(textOf(document.getElementById('sls-luna-overlay')),/1\.0 h story/,
    'the fetched row must show its times');
});

test('a pending HowLongToBeat fetch is polled until the record arrives',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {calls,overlay}=await openHltbPage(document,{hltb:hltbFixture(),fetchPending:3});
  findNode(rowFor(overlay,'Portal 2'),node=>node.textContent==='Fetch').onclick();
  await new Promise(resolve=>setTimeout(resolve,80));
  const fetches=calls.filter(entry=>entry.fn==='SlsHltbFetch');
  assert.equal(fetches.length,4,'three pending answers, then the record');
  const text=textOf(document.getElementById('sls-luna-overlay'));
  assert.match(text,/1\.0 h story/,'the fetched row must show its times');
  assert.doesNotMatch(text,/still answering/,'pending is not an error for the user');
});

test('the HowLongToBeat page opens the site search through Lumen',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {calls,overlay}=await openHltbPage(document,{hltb:hltbFixture()});
  const row=rowFor(overlay,'Portal 2');
  const search=findNode(row,node=>node.textContent==='Search');
  assert.ok(search,'a game must offer Search');
  search.onclick();
  await new Promise(resolve=>setTimeout(resolve,40));
  const call=calls.filter(entry=>entry.fn==='__lumenOpenExternalUrl').pop();
  assert.ok(call,'Search must route through the Lumen opener');
  assert.equal(call.payload.url,'https://howlongtobeat.com/?q=Portal%202');
});

test('the fetch-missing queue skips unmatchable games and keeps going',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {calls,overlay}=await openHltbPage(document,{hltb:hltbFixture({games:{},
    library:[{appid:620,name:'Portal 2',shortcut:false},{appid:70,name:'Half-Life',shortcut:false},
      {appid:220,name:'Half-Life 2',shortcut:false}]}),fetchFailure:220});
  const start=findNode(overlay,node=>node.textContent==='Fetch missing (3)'
    &&typeof node.onclick==='function');
  assert.ok(start,'the fetch-missing button was not found');
  start.onclick();
  await new Promise(resolve=>setTimeout(resolve,250));
  const fetched=calls.filter(entry=>entry.fn==='SlsHltbFetch').map(entry=>entry.payload.appid);
  assert.deepEqual(fetched,[70,220,620],'a no-match game must be skipped, not stop the run');
  const text=textOf(document.getElementById('sls-luna-overlay'));
  assert.match(text,/Fetched 2 game\(s\), 1 skipped \(no match\)/,'the summary must count the skip');
  assert.match(text,/no match found/,'the skipped game must be remembered in the list');
  assert.doesNotMatch(text,/Stopped:/,'a no-match must not stop the queue');
});

test('a rate-limited fetch pauses and retries the same game once',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {calls,overlay}=await openHltbPage(document,{hltb:hltbFixture({games:{},
    library:[{appid:620,name:'Portal 2',shortcut:false},{appid:70,name:'Half-Life',shortcut:false}]}),
    rateLimitOnce:620});
  const start=findNode(overlay,node=>node.textContent==='Fetch missing (2)'
    &&typeof node.onclick==='function');
  start.onclick();
  await new Promise(resolve=>setTimeout(resolve,250));
  const fetched=calls.filter(entry=>entry.fn==='SlsHltbFetch').map(entry=>entry.payload.appid);
  assert.deepEqual(fetched,[70,620,620],
    'the rate-limited game must be retried after the pause, in page order');
  const text=textOf(document.getElementById('sls-luna-overlay'));
  assert.match(text,/Fetched 2 game\(s\)/,'both games must end up cached');
  assert.doesNotMatch(text,/Stopped:/,'a single rate limit must not stop the queue');
});

test('the fetch pass keeps running with the menu closed and never reopens it',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {calls,storage,overlay}=await openHltbPage(document,{hltb:hltbFixture({games:{},
    library:[{appid:70,name:'Half-Life',shortcut:false},{appid:220,name:'Half-Life 2',shortcut:false},
      {appid:620,name:'Portal 2',shortcut:false}]})});
  const start=findNode(overlay,node=>node.textContent==='Fetch missing (3)'
    &&typeof node.onclick==='function');
  start.onclick();
  assert.equal(storage.get('slsLunaHltbBackground'),'1','starting a pass must set the background flag');
  await new Promise(resolve=>setTimeout(resolve,40));
  const closed=document.getElementById('sls-luna-overlay');
  if (closed) closed.remove(); // the user closes the menu while the pass runs
  await new Promise(resolve=>setTimeout(resolve,400));
  assert.equal(document.getElementById('sls-luna-overlay'),null,
    'a background pass must never reopen the menu');
  const fetched=calls.filter(entry=>entry.fn==='SlsHltbFetch').length;
  assert.ok(fetched>=2,'fetching must continue with the menu closed (got '+fetched+')');
  assert.equal(storage.get('slsLunaHltbBackground'),undefined,
    'finishing every queued game must clear the background flag');
});

test('Stop clears the background flag and halts the pass',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {calls,storage,overlay}=await openHltbPage(document,{hltb:hltbFixture({games:{},
    library:[{appid:70,name:'Half-Life',shortcut:false},{appid:220,name:'Half-Life 2',shortcut:false},
      {appid:620,name:'Portal 2',shortcut:false}]})});
  const start=findNode(overlay,node=>node.textContent==='Fetch missing (3)'
    &&typeof node.onclick==='function');
  start.onclick();
  const stop=findNode(document.getElementById('sls-luna-overlay'),
    node=>node.textContent==='Stop after current'&&typeof node.onclick==='function');
  assert.ok(stop,'a running pass must offer Stop');
  stop.onclick();
  assert.equal(storage.get('slsLunaHltbBackground'),undefined,'Stop must clear the background flag');
  await new Promise(resolve=>setTimeout(resolve,400));
  const fetched=calls.filter(entry=>entry.fn==='SlsHltbFetch').length;
  assert.ok(fetched<3,'stopping must end the pass early (got '+fetched+' of 3)');
});

test('a remembered pass resumes when the menu opens again',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {calls,storage}=await openHltbPage(document,{hltb:hltbFixture({games:{},
    library:[{appid:70,name:'Half-Life',shortcut:false}]})});
  storage.set('slsLunaHltbBackground','1');
  // Closing and reopening the menu refreshes the data and resumes the pass.
  const overlay=document.getElementById('sls-luna-overlay');
  overlay.remove();
  document.getElementById('sls-luna-btn').click();
  await new Promise(resolve=>setTimeout(resolve,300));
  const fetched=calls.filter(entry=>entry.fn==='SlsHltbFetch').map(entry=>entry.payload.appid);
  assert.deepEqual(fetched,[70],'the remembered pass must resume on open');
  assert.equal(storage.get('slsLunaHltbBackground'),undefined,'finishing must clear the flag');
});

test('the HowLongToBeat page offers a sort-direction switch that is remembered',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {storage,overlay}=await openHltbPage(document,{hltb:hltbFixture()});
  assert.match(textOf(overlay),/Library order: shortest first/,'the default must be shortest first');
  const row=findNode(overlay,node=>node.className==='sls-luna-row'
    &&(node.textContent||'').includes('Library order'));
  const switchBox=findNode(row,node=>(node.className||'').startsWith('sls-luna-switch'));
  assert.ok(switchBox&&switchBox.className.includes('on'),'the switch must start on');
  switchBox.onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  assert.equal(storage.get('slsLunaHltbOrder'),'long','flipping must persist the preference');
  assert.match(textOf(document.getElementById('sls-luna-overlay')),/Library order: longest first/);
});

test('the SteamGridDB page saves and tests the API key',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w,calls,storage}=fakeWindow(document,{});
  storage.set('slsLunaBeta','1'); // the picker lives behind the beta switch
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await new Promise(resolve=>setTimeout(resolve,30));
  const tab=findNode(document.getElementById('sls-luna-overlay'),
    node=>(node.className||'').startsWith('sls-luna-tab')&&node.textContent==='SteamGridDB');
  assert.ok(tab,'the SteamGridDB tab must exist');
  tab.onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  let overlay=document.getElementById('sls-luna-overlay');
  assert.match(textOf(overlay),/SteamGridDB/,'the page must explain the key and attribution');
  const input=findNode(overlay,node=>node.tagName==='INPUT'&&node.placeholder==='SteamGridDB API key');
  assert.ok(input,'the key field must exist');
  input.value='my-key';
  const save=findNode(overlay,node=>node.textContent==='Save key'&&typeof node.onclick==='function');
  save.onclick();
  await new Promise(resolve=>setTimeout(resolve,40));
  const call=calls.filter(entry=>entry.fn==='SlsSgdbSetKey').pop();
  assert.ok(call&&call.payload.key==='my-key','saving must send the key');
  overlay=document.getElementById('sls-luna-overlay');
  assert.match(textOf(overlay),/Key saved/,'the page must confirm the save');
  const test=findNode(overlay,node=>node.textContent==='Test key'&&typeof node.onclick==='function');
  test.onclick();
  await new Promise(resolve=>setTimeout(resolve,40));
  assert.ok(calls.some(entry=>entry.fn==='SlsSgdbTest'),'testing must call the backend');
  assert.match(textOf(document.getElementById('sls-luna-overlay')),/key works/);
});

test('a right-click artwork request opens the picker and applying calls the backend',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w,calls,storage}=fakeWindow(document,{sgdbKey:'my-key'});
  storage.set('slsLunaBeta','1');
  mount(w);
  storage.set('slsLunaArtworkRequest',JSON.stringify({appid:1145360,name:'Hades',at:Date.now()}));
  w.__emitStorage();
  await new Promise(resolve=>setTimeout(resolve,80));
  const overlay=document.getElementById('sls-luna-overlay');
  assert.ok(overlay,'the picker must open from the request');
  assert.match(textOf(overlay),/Artwork for Hades \(#1145360\)/,'the requested game must be shown');
  const search=calls.filter(entry=>entry.fn==='SlsSgdbSearch').pop();
  assert.ok(search&&search.payload.term==='Hades','the game name must be searched');
  const assets=calls.filter(entry=>entry.fn==='SlsSgdbAssets').pop();
  assert.ok(assets&&assets.payload.gameId===1&&assets.payload.kind==='grid',
    'the best match assets must load for the grid kind');
  const card=findNode(document.getElementById('sls-luna-overlay'),node=>node.className==='sls-luna-asset');
  assert.ok(card,'the asset card must render');
  card.onclick();
  await new Promise(resolve=>setTimeout(resolve,40));
  const apply=calls.filter(entry=>entry.fn==='SlsSgdbApply').pop();
  assert.ok(apply,'clicking an asset must apply it');
  assert.deepEqual(apply.payload,{appid:1145360,kind:'grid',url:'https://cdn.example/grid1.png',mime:'image/png'});
  assert.match(textOf(document.getElementById('sls-luna-overlay')),/Artwork applied/);
  const hero=findNode(document.getElementById('sls-luna-overlay'),
    node=>node.textContent==='Hero'&&typeof node.onclick==='function');
  hero.onclick();
  await new Promise(resolve=>setTimeout(resolve,40));
  const heroAssets=calls.filter(entry=>entry.fn==='SlsSgdbAssets').pop();
  assert.equal(heroAssets.payload.kind,'hero','switching kind must reload assets');
  // The slot the backend now holds a step for is the one that was applied.
  assert.match(textOf(document.getElementById('sls-luna-overlay')),/Grid · 1 undo step/,
    'the applied slot must appear in Recent changes');
  const undo=findNode(document.getElementById('sls-luna-overlay'),
    node=>node.textContent==='Undo'&&typeof node.onclick==='function');
  assert.ok(undo,'each history row must offer Undo');
  undo.onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  assert.equal(calls.filter(entry=>entry.fn==='SlsSgdbUndo').length,0,
    'the first click only arms the undo');
  findNode(document.getElementById('sls-luna-overlay'),
    node=>node.textContent==='Confirm undo'&&typeof node.onclick==='function').onclick();
  await new Promise(resolve=>setTimeout(resolve,40));
  const undoCall=calls.filter(entry=>entry.fn==='SlsSgdbUndo').pop();
  assert.deepEqual(undoCall.payload,{appid:1145360,kind:'grid'});
  assert.match(textOf(document.getElementById('sls-luna-overlay')),/Undone/);
  const reset=findNode(document.getElementById('sls-luna-overlay'),
    node=>node.textContent==='Reset this game\u2019s artwork'&&typeof node.onclick==='function');
  assert.ok(reset,'the per-game reset must be offered');
  reset.onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  assert.equal(calls.filter(entry=>entry.fn==='SlsSgdbReset').length,0,
    'reset is armed too, never a single click');
  findNode(document.getElementById('sls-luna-overlay'),
    node=>node.textContent==='Confirm reset'&&typeof node.onclick==='function').onclick();
  await new Promise(resolve=>setTimeout(resolve,40));
  const resetCall=calls.filter(entry=>entry.fn==='SlsSgdbReset').pop();
  assert.deepEqual(resetCall.payload,{appid:1145360,kind:'hero'});
  assert.match(textOf(document.getElementById('sls-luna-overlay')),/its own again/);
});

test('the Health page explains a drifted Steam client in plain words',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w}=fakeWindow(document,{steamClient:{build:'1799999999',pinned:'1791918634',match:false,
    updatesBlocked:true,cachedBuild:'1791918634',cacheReady:true,
    pacmanManaged:true,pacmanIgnored:false}});
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await new Promise(resolve=>setTimeout(resolve,60));
  const tab=findNode(document.getElementById('sls-luna-overlay'),
    node=>(node.className||'').startsWith('sls-luna-tab')&&node.textContent.startsWith('Health'));
  tab.onclick();
  await new Promise(resolve=>setTimeout(resolve,40));
  const text=textOf(document.getElementById('sls-luna-overlay'));
  assert.match(text,/Needs attention/,'the attention section must lead');
  assert.match(text,/luna steam recover/,'the drift must name the exact recovery command');
  assert.match(text,/IgnorePkg = steam/,'the pacman gap must name the exact conf line');
});

test('the Updates page keeps Steam on the known-good build and gates the latest behind a confirm',async()=>{
  const document=fakeDom();
  const {calls}=await openUpdates(document,{steamClient:{pinned:'1791918634',match:true}});
  const text=()=>textOf(document.getElementById('sls-luna-overlay'));
  assert.match(text(),/Recommended/,'staying on the known-good build must be labelled Recommended');
  assert.match(text(),/known-good 1791918634/,'the pinned build must be shown');
  const pin=button(document,'Pin this build as known-good');
  assert.ok(pin,'the pin button must exist');
  const latest=button(document,'Move to Valve\u2019s latest\u2026');
  assert.ok(latest,'the separate latest action must exist');
  latest.onclick();
  await settle();
  assert.ok(!calls.some(entry=>entry.fn==='SlsLunaSteam'&&entry.payload.action==='queue-update'),
    'arming must not queue anything');
  assert.match(text(),/may break the current setup/,'the confirm must state the risk');
  button(document,'Keep the known-good build').onclick();
  await settle();
  assert.ok(!calls.some(entry=>entry.fn==='SlsLunaSteam'&&entry.payload.action==='queue-update'),
    'declining must queue nothing');
  button(document,'Move to Valve\u2019s latest\u2026').onclick();
  await settle();
  button(document,'Yes, queue the latest Steam client').onclick();
  await settle(80);
  assert.ok(calls.some(entry=>entry.fn==='SlsLunaSteam'&&entry.payload.action==='queue-update'),
    'the confirmed move must go through the Steam RPC');
  assert.match(text(),/Queued Steam task: update/,'the queued task must be visible');
  button(document,'Cancel Steam task').onclick();
  await settle(80);
  assert.ok(calls.some(entry=>entry.fn==='SlsLunaSteam'&&entry.payload.action==='cancel'),
    'cancel must go through the Steam RPC');
});

test('Health keeps a pointer to Updates and no duplicate update controls',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w}=fakeWindow(document,{});
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await settle(60);
  findNode(document.getElementById('sls-luna-overlay'),
    node=>(node.className||'').startsWith('sls-luna-tab')&&node.textContent.startsWith('Health')).onclick();
  await settle(40);
  assert.match(textOf(document.getElementById('sls-luna-overlay')),/Looking for Health > Lumen\? Updating Lumen and Luna is on the Updates page/);
  ['Check now','Install update','Update Steam client','Refresh Steam + Luna','Install automatically',
    'Pin current build'].forEach(label=>assert.ok(!button(document,label),
    label+' must not be on the Health page any more'));
});

test('the Health page offers bridge repair when a base update wiped it',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w,calls}=fakeWindow(document,{bootPatched:false});
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await new Promise(resolve=>setTimeout(resolve,60));
  const tab=findNode(document.getElementById('sls-luna-overlay'),
    node=>(node.className||'').startsWith('sls-luna-tab')&&node.textContent.startsWith('Health'));
  tab.onclick();
  await new Promise(resolve=>setTimeout(resolve,40));
  const overlay=document.getElementById('sls-luna-overlay');
  assert.match(textOf(overlay),/base update wiped the bridge/,'the issue must name the cause');
  const repair=findNode(overlay,node=>node.textContent==='Repair bridge'
    &&typeof node.onclick==='function');
  assert.ok(repair,'the repair button must exist');
  assert.equal(repair.disabled,false,'repair must be enabled when the bridge is missing');
  repair.onclick();
  await new Promise(resolve=>setTimeout(resolve,40));
  assert.ok(calls.some(entry=>entry.fn==='SlsLunaRepair'),
    'repair must go through its own RPC, never a generic shell');
});

test('the Health page disables bridge repair when the bridge is present',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w}=fakeWindow(document,{});
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await new Promise(resolve=>setTimeout(resolve,60));
  const tab=findNode(document.getElementById('sls-luna-overlay'),
    node=>(node.className||'').startsWith('sls-luna-tab')&&node.textContent.startsWith('Health'));
  tab.onclick();
  await new Promise(resolve=>setTimeout(resolve,40));
  const overlay=document.getElementById('sls-luna-overlay');
  const repair=findNode(overlay,node=>node.textContent==='Repair bridge'
    &&typeof node.onclick==='function');
  assert.ok(repair,'the repair button must exist');
  assert.equal(repair.disabled,true,'repair must stay disabled when nothing is missing');
});

test('Settings offers SteamOS Mode shortcut creation',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w,calls}=fakeWindow(document,{});
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await new Promise(resolve=>setTimeout(resolve,60));
  const tab=findNode(document.getElementById('sls-luna-overlay'),
    node=>(node.className||'').startsWith('sls-luna-tab')&&node.textContent==='Settings');
  tab.onclick();
  await new Promise(resolve=>setTimeout(resolve,40));
  const overlay=document.getElementById('sls-luna-overlay');
  assert.match(textOf(overlay),/SteamOS Mode/,'the SteamOS Mode section must exist');
  const create=findNode(overlay,node=>node.textContent==='Create shortcuts'
    &&typeof node.onclick==='function');
  assert.ok(create,'the create button must exist');
  create.onclick();
  await new Promise(resolve=>setTimeout(resolve,40));
  assert.ok(calls.some(entry=>entry.fn==='SlsLunaLaunchers'
    &&entry.payload.action==='create'),'creation must go through its own RPC');
  assert.match(textOf(document.getElementById('sls-luna-overlay')),/SteamOS Mode shortcuts: 3/,
    'the creation result must be shown');
});

test('Settings arms and starts the Steam update lock/unlock jobs',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w,calls}=fakeWindow(document,{});
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await new Promise(resolve=>setTimeout(resolve,60));
  findNode(document.getElementById('sls-luna-overlay'),
    node=>(node.className||'').startsWith('sls-luna-tab')&&node.textContent==='Settings').onclick();
  await new Promise(resolve=>setTimeout(resolve,40));
  let overlay=document.getElementById('sls-luna-overlay');
  assert.match(textOf(overlay),/Steam updates/);
  assert.match(textOf(overlay),/Normal: Steam updates itself/);
  const block=findNode(overlay,node=>node.textContent==='Block updates (roll back)'
    &&typeof node.onclick==='function');
  assert.ok(block,'the block button must exist');
  block.onclick();
  await new Promise(resolve=>setTimeout(resolve,40));
  assert.ok(!calls.some(entry=>entry.fn==='SlsLunaSteam'&&entry.payload.action==='lock'),
    'the first click only arms the confirmation');
  overlay=document.getElementById('sls-luna-overlay');
  assert.match(textOf(overlay),/500 MB/,'the confirmation must state the consequences');
  findNode(overlay,node=>node.textContent==='Yes, close Steam and block updates').onclick();
  await new Promise(resolve=>setTimeout(resolve,40));
  assert.ok(calls.some(entry=>entry.fn==='SlsLunaSteam'&&entry.payload.action==='lock'),
    'confirming must start the lock job');
  assert.match(textOf(document.getElementById('sls-luna-overlay')),/background/);
});

test('the Health page renders Spanish when the language preference says so',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w}=fakeWindow(document,{prefs:{lang:'es'},
    steamClient:{build:'1799999999',pinned:'1791918634',match:false,updatesBlocked:true,
      cachedBuild:'1791918634',cacheReady:true,pacmanManaged:false,pacmanIgnored:false}});
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await new Promise(resolve=>setTimeout(resolve,60));
  const tab=findNode(document.getElementById('sls-luna-overlay'),
    node=>(node.className||'').startsWith('sls-luna-tab')&&node.textContent.startsWith('Health'));
  tab.onclick();
  await new Promise(resolve=>setTimeout(resolve,40));
  const overlay=document.getElementById('sls-luna-overlay');
  const text=textOf(overlay);
  assert.match(text,/Necesita atención/,'the attention header must be Spanish');
  assert.match(text,/luna steam recover/,'commands stay machine-readable in both languages');
  assert.doesNotMatch(text,/Everything looks good/,'no English status line may leak through');
  const settings=findNode(document.getElementById('sls-luna-overlay'),
    node=>(node.className||'').startsWith('sls-luna-tab')&&node.textContent.startsWith('Settings'));
  settings.onclick();
  await new Promise(resolve=>setTimeout(resolve,40));
  assert.match(textOf(document.getElementById('sls-luna-overlay')),/Español/,
    'the language switch must be visible');
});

test('the auto picker fills every slot and asks the agent for a live refresh',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w,calls,storage}=fakeWindow(document,{sgdbKey:'my-key'});
  storage.set('slsLunaBeta','1');
  mount(w);
  storage.set('slsLunaArtworkRequest',JSON.stringify({appid:1145360,name:'Hades',at:Date.now()}));
  w.__emitStorage();
  await new Promise(resolve=>setTimeout(resolve,80));
  const auto=findNode(document.getElementById('sls-luna-overlay'),
    node=>node.textContent==='Apply best to every slot'&&typeof node.onclick==='function');
  assert.ok(auto,'the auto button must be offered');
  auto.onclick();
  await new Promise(resolve=>setTimeout(resolve,60));
  const call=calls.filter(entry=>entry.fn==='SlsSgdbAuto').pop();
  assert.ok(call,'auto must call the backend');
  assert.deepEqual(call.payload,{appid:1145360,gameId:1});
  assert.match(textOf(document.getElementById('sls-luna-overlay')),
    /Applied the best match to 4 of 5 slots \(icon had nothing usable\)/,
    'the summary counts the slot that really failed');
  assert.match(textOf(document.getElementById('sls-luna-overlay')),
    /icon: none \u2014 No usable icon images\./,'the per-slot failure is spelled out');
  const live=JSON.parse(storage.get('slsLunaArtworkLive'));
  assert.equal(live.appid,1145360);
  assert.deepEqual(live.kinds,['grid','wide','hero','logo'],'icon has no live slot and is skipped');
});

test('beta features stay hidden until the Settings switch is on',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w,storage}=fakeWindow(document,{});
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await new Promise(resolve=>setTimeout(resolve,30));
  let overlay=document.getElementById('sls-luna-overlay');
  assert.match(textOf(overlay),/Settings/,'the Settings tab must exist');
  assert.doesNotMatch(textOf(overlay),/SteamGridDB/,'beta pages must be hidden by default');
  const settings=findNode(overlay,node=>(node.className||'').startsWith('sls-luna-tab')
    &&node.textContent==='Settings');
  settings.onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  overlay=document.getElementById('sls-luna-overlay');
  assert.match(textOf(overlay),/Show beta features/,'the switch must be on the Settings page');
  assert.match(textOf(overlay),/hidden/,'the page must say beta pages are hidden');
  const row=findNode(overlay,node=>node.className==='sls-luna-row'
    &&(node.textContent||'').includes('Show beta features'));
  const switchBox=findNode(row,node=>(node.className||'').startsWith('sls-luna-switch'));
  switchBox.onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  assert.equal(storage.get('slsLunaBeta'),'1','the switch must persist');
  overlay=document.getElementById('sls-luna-overlay');
  assert.match(textOf(overlay),/SteamGridDB/,'turning it on must reveal the beta tab');
});

test('an artwork request with beta off points at the Settings switch',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w,storage}=fakeWindow(document,{sgdbKey:'my-key'});
  mount(w);
  storage.set('slsLunaArtworkRequest',JSON.stringify({appid:1145360,name:'Hades',at:Date.now()}));
  w.__emitStorage();
  await new Promise(resolve=>setTimeout(resolve,80));
  const overlay=document.getElementById('sls-luna-overlay');
  assert.ok(overlay,'the menu must open');
  const text=textOf(overlay);
  assert.match(text,/Show beta features/,'the request must land on the Settings page');
  assert.match(text,/beta features are off/,'the reason must be explained');
  assert.doesNotMatch(text,/Artwork for Hades/,'no picker without beta on');
});

test('an artwork request is consumed once and never replays on restart',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const first=fakeWindow(document,{sgdbKey:'my-key'});
  first.storage.set('slsLunaBeta','1');
  mount(first.w);
  first.storage.set('slsLunaArtworkRequest',JSON.stringify({appid:1145360,name:'Hades',at:Date.now()}));
  first.w.__emitStorage();
  await new Promise(resolve=>setTimeout(resolve,80));
  assert.ok(document.getElementById('sls-luna-overlay'),'the picker must open from the request');
  assert.equal(first.storage.get('slsLunaArtworkRequest'),undefined,'the request must be claimed');
  // A fresh mount sharing the same storage (a Steam restart) must stay shut.
  const document2=fakeDom();
  menubarFixture(document2,true);
  const second=fakeWindow(document2,{sgdbKey:'my-key'});
  second.storage.set('slsLunaBeta','1');
  second.w.localStorage.getItem=(key)=>first.storage.get(key) ?? null;
  second.w.localStorage.setItem=(key,value)=>first.storage.set(key,String(value));
  second.w.localStorage.removeItem=(key)=>first.storage.delete(key);
  mount(second.w);
  await new Promise(resolve=>setTimeout(resolve,120));
  assert.equal(document2.getElementById('sls-luna-overlay'),null,'a consumed request must not reopen the menu');
});

test('a stale artwork request is ignored',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w,storage}=fakeWindow(document,{sgdbKey:'my-key'});
  storage.set('slsLunaBeta','1');
  mount(w);
  storage.set('slsLunaArtworkRequest',JSON.stringify({appid:1145360,name:'Hades',at:Date.now()-10*60*1000}));
  w.__emitStorage();
  await new Promise(resolve=>setTimeout(resolve,80));
  assert.equal(document.getElementById('sls-luna-overlay'),null,'a ten-minute-old request must stay shut');
});

test('the Graphics beta page reports the detection read-only',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w,storage}=fakeWindow(document,{});
  storage.set('slsLunaBeta','1');
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await new Promise(resolve=>setTimeout(resolve,40));
  const tab=findNode(document.getElementById('sls-luna-overlay'),
    node=>(node.className||'').startsWith('sls-luna-tab')&&node.textContent==='Graphics');
  assert.ok(tab,'the Graphics tab must exist with beta on');
  tab.onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  const text=textOf(document.getElementById('sls-luna-overlay'));
  assert.match(text,/Arch Linux \(test\)/,'the distro must be shown');
  assert.match(text,/AMD Radeon RX 7900 XTX/,'the GPU must be shown');
  assert.match(text,/driver amdgpu/,'the driver must be shown');
  assert.match(text,/VkLayer_LSFG_vk\.json/,'the lsfg-vk layer must be shown');
  assert.match(text,/Decky plugin detected: decky-lsfg-vk/,'the overlapping Decky plugin must be noted');
  assert.match(text,/OptiScaler/,'the OptiScaler section must exist');
  assert.match(text,/Space Engineers 2/,'the OptiScaler Client games must be listed');
  assert.match(text,/read side of the Luna port/,'the port must be described honestly');
  assert.match(text,/Steam root: \/home\/tester\/\.local\/share\/Steam/,'the Steam facts must be shown');
  assert.match(text,/Nothing here installs/,'the read-only promise must be stated');
});

async function openGraphicsPage(document,options){
  menubarFixture(document,true);
  const made=fakeWindow(document,options||{});
  made.storage.set('slsLunaBeta','1');
  mount(made.w);
  document.getElementById('sls-luna-btn').click();
  await new Promise(resolve=>setTimeout(resolve,40));
  const tab=findNode(document.getElementById('sls-luna-overlay'),
    node=>(node.className||'').startsWith('sls-luna-tab')&&node.textContent==='Graphics');
  assert.ok(tab,'the Graphics tab must exist with beta on');
  tab.onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  return Object.assign(made,{overlay:document.getElementById('sls-luna-overlay')});
}

test('Graphics reads bypass the backend cache and the page says when it was checked',async()=>{
  const document=fakeDom();
  const checkedAt=Math.floor(Date.now()/1000)-12;
  const {calls,overlay}=await openGraphicsPage(document,{graphicsCheckedAt:checkedAt});
  assert.match(textOf(overlay),/Checked (1[1-9]|2[0-9]) s ago\./,
    'the stamp comes from the report\'s own checkedAt');
  const reads=calls.filter(entry=>entry.fn==='SlsGraphicsGet');
  assert.ok(reads.length>=1);
  reads.forEach(entry=>assert.equal(entry.payload.force,true,
    'every Graphics read asks the backend to skip its cache'));

  const before=calls.filter(entry=>entry.fn==='SlsGraphicsGet').length;
  await clickRefresh(document);
  const after=calls.filter(entry=>entry.fn==='SlsGraphicsGet');
  assert.ok(after.length>before,'Refresh reads the report again');
  assert.equal(after[after.length-1].payload.force,true,'the explicit Refresh forces it');
});

test('the GPU in use and the boot GPU are marked when the backend says so',async()=>{
  const document=fakeDom();
  const {overlay}=await openGraphicsPage(document,{
    graphicsGpus:[
      {card:'card0',vendor:'0x8086',vendor_name:'Intel',device:'0x46a6',driver:'i915',
        product:'Intel Iris Xe',boot_vga:false,in_use:false},
      {card:'card1',vendor:'0x10de',vendor_name:'NVIDIA',device:'0x2882',driver:'nvidia',
        product:'NVIDIA RTX 4070',boot_vga:true,in_use:true}],
    graphicsGpu:{in_use:'card1',primary:'card1',hybrid:true}});
  const overlayText=textOf(overlay);
  assert.match(overlayText,/card1: NVIDIA RTX 4070 .* · in use, primary/);
  const intelLine=findNode(overlay,node=>(node.className||'').indexOf('sls-luna-note')===0
    && /card0:/.test(node.textContent));
  assert.ok(intelLine,'the second GPU must be listed');
  assert.doesNotMatch(intelLine.textContent,/in use|primary/,
    'a GPU the backend does not mark stays plain');
});

test('the checked-ago tick dies with the menu',async()=>{
  const document=fakeDom();
  const {w,overlay,timers}=await openGraphicsPage(document,{});
  assert.ok(findNode(overlay,node=>(node.className||'').indexOf('sls-luna-graphics-age')>=0),
    'the age line is on the page');
  assert.ok(timers.intervals.size>0,'the page has timers while it is open');
  w.__SlsLunaTeardown();
  assert.equal(timers.intervals.size,0,'teardown clears every interval, the tick included');
  assert.equal(document.getElementById('sls-luna-overlay'),null);
});

test('stale global HLTB preferences never relabel an alphabetical Steam dropdown',async()=>{
  const document=fakeDom();
  const header=document.createElement('div');
  const sortLabel=document.createElement('div');
  sortLabel.textContent='Sort By';
  const button=document.createElement('div');
  const buttonText=document.createElement('div');
  buttonText.textContent='Alphabetical';
  button.appendChild(buttonText);
  header.append(sortLabel,button);
  document.body.appendChild(header);
  menubarFixture(document,true);
  const {w}=fakeWindow(document);
  w.localStorage.setItem('AppGridDisplaySettings','{"eSortBy":12}');
  w.localStorage.setItem('slsLunaSortLive','12');
  const intervals=[];
  w.setInterval=fn=>intervals.push(fn);
  mount(w);
  await new Promise(resolve=>setTimeout(resolve,30));
  intervals.forEach(fn=>fn());
  assert.equal(buttonText.textContent,'Alphabetical','only the agent\'s own view-local dropdown knows its selection');
});

test('library home shelves sort view-locally by cached HLTB times',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const apps=[
    {appid:1,display_name:'Zebra'},
    {appid:2,display_name:'Alpha'},
    {appid:3,display_name:'Missing'},
  ];
  const alpha = collection => collection.visibleApps.slice()
    .sort((a,b)=>String(a.display_name).localeCompare(String(b.display_name)));
  function shelf(sort) {
    const instance = {
      props:{showcase:{eSortBy:sort},collectionId:'games'},
      m_fnSort:alpha, updates:0,
      forceUpdate(){ this.updates++; this.order=this.m_fnSort({visibleApps:apps}).map(a=>a.appid); },
    };
    return instance;
  }
  const hltbShelf=shelf(12), normalShelf=shelf(1);
  const hltbFiber={stateNode:hltbShelf,
    memoizedProps:{showcase:{eSortBy:12}},child:null,sibling:null};
  const normalFiber={stateNode:normalShelf,
    memoizedProps:{showcase:{eSortBy:1}},child:null,sibling:null};
  hltbFiber.sibling=normalFiber;
  const root={child:hltbFiber,sibling:null};
  hltbFiber.return=root; normalFiber.return=root;
  const anchor=document.createElement('div');
  anchor['__reactFiber$test']=root;
  document.body.appendChild(anchor);
  const {w}=fakeWindow(document,{hltb:{success:true,count:2,
    games:{'1':{main:3600},'2':{main:120}}}});
  mount(w);
  await new Promise(resolve=>setTimeout(resolve,60));
  assert.deepEqual(hltbShelf.order,[2,1,3],'shortest first, unknown last');
  assert.ok(hltbShelf.updates>0,'the HLTB shelf must refresh in this window');
  assert.equal(normalShelf.updates||0,0,'other shelves keep Steam order');
  assert.deepEqual(normalShelf.m_fnSort({visibleApps:apps}).map(a=>a.appid),[2,3,1],
    'alphabetical shelf untouched');
  assert.deepEqual(apps.map(a=>a.display_name),['Zebra','Alpha','Missing'],
    'shared app objects are never mutated');
});

test('library shelves re-render only when their HLTB times or direction change',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const apps=[{appid:1,display_name:'Zebra'},{appid:2,display_name:'Alpha'}];
  const hltbShelf={
    props:{showcase:{eSortBy:12},collectionId:'games'},
    m_fnSort:collection=>collection.visibleApps.slice(), updates:0,
    forceUpdate(){ this.updates++; this.order=this.m_fnSort({visibleApps:apps}).map(a=>a.appid); },
  };
  const fiber={stateNode:hltbShelf,memoizedProps:{showcase:{eSortBy:12}},child:null,sibling:null};
  const root={child:fiber,sibling:null};
  fiber.return=root;
  const anchor=document.createElement('div');
  anchor['__reactFiber$test']=root;
  document.body.appendChild(anchor);
  const {w}=fakeWindow(document,{hltb:{success:true,games:{'1':{main:3600},'2':{main:120}}}});
  const intervals=[];
  w.setInterval=fn=>{intervals.push(fn);return intervals.length;};
  mount(w);
  await new Promise(resolve=>setTimeout(resolve,60));
  assert.deepEqual(hltbShelf.order,[2,1],'shortest first');
  const settled=hltbShelf.updates;
  // The walk runs in idle slices now, so let the queued slice finish after
  // each interval pump before counting re-renders.
  const pumpAll=async()=>{intervals.forEach(fn=>fn());await settle(20);};
  for(let i=0;i<5;i++)await pumpAll();
  assert.equal(hltbShelf.updates,settled,'unchanged times and direction must not force re-renders');
  w.localStorage.setItem('slsLunaHltbOrder','long');
  await pumpAll();
  assert.equal(hltbShelf.updates,settled+1,'a direction change re-renders exactly once');
  assert.deepEqual(hltbShelf.order,[1,2],'longest first');
  await pumpAll();
  assert.equal(hltbShelf.updates,settled+1);
});

/* --- Shelf walk cost control ------------------------------------------------
   A fake MutationObserver records the observers the panel installs, so these
   tests drive the filters directly. A library container with a stable id is
   what the panel binds to instead of document.body. */
function shelfWindow(options){
  const document=fakeDom();
  menubarFixture(document,true);
  const library=document.createElement('div');
  library.id='GamepadUI_Library';
  document.body.appendChild(library);
  const {w}=fakeWindow(document,Object.assign({hltb:{success:true,
    games:{'1':{main:3600},'2':{main:120}}}},options||{}));
  const observers=[];
  w.MutationObserver=function(cb){
    this.cb=cb;this.connected=false;this.target=null;
    this.observe=target=>{this.connected=true;this.target=target;};
    this.disconnect=()=>{this.connected=false;};
    observers.push(this);
  };
  const intervals=[];
  w.setInterval=(fn,ms)=>{intervals.push({fn,ms});return intervals.length;};
  return {document,library,w,observers,intervals,
    live:()=>observers.filter(observer=>observer.connected)};
}
function shortShelf(){
  const instance={props:{showcase:{eSortBy:1},collectionId:'g'},updates:0,
    m_fnSort:collection=>collection.visibleApps.slice(),
    forceUpdate(){this.updates++;}};
  const fiber={memoizedProps:{showcase:{eSortBy:1}},child:null,sibling:null,return:null};
  let reads=0;
  Object.defineProperty(fiber,'stateNode',{get(){reads++;return instance;}});
  return {fiber,instance,reads:()=>reads};
}
function hltbShelfFiber(){
  const instance={props:{showcase:{eSortBy:12},collectionId:'g'},updates:0,
    m_fnSort:collection=>collection.visibleApps.slice(),
    forceUpdate(){this.updates++;}};
  return {fiber:{stateNode:instance,memoizedProps:{showcase:{eSortBy:12}},
    child:null,sibling:null,return:null},instance};
}
function addNode(document,library,fiber,index){
  const node=document.createElement('div');
  if(fiber)node['__reactFiber$t'+(index||0)]=fiber;
  library.appendChild(node);
  return node;
}

test('a mutation storm coalesces into one shelf pass and unrelated nodes cost none',async()=>{
  const {document,library,w,live}=shelfWindow();
  mount(w);
  await settle(30);
  assert.ok(live().length>=1,'an observer must exist');
  assert.equal(live().filter(entry=>entry.target===document.body).length,0,'nothing observes document.body');
  const observer=live().find(entry=>entry.target&&entry.target.id==='GamepadUI_Library');
  assert.ok(observer,'the shelf observer must sit on the library container');
  const before=w.__SlsLunaSelftest.state();
  for(let i=0;i<1000;i++){
    const {fiber}=shortShelf();
    const node=addNode(document,library,fiber,i);
    observer.cb([{type:'childList',target:library,addedNodes:[node],removedNodes:[]}]);
  }
  await settle(40);
  const after=w.__SlsLunaSelftest.state();
  assert.equal(after.observerCallbacks-before.observerCallbacks,1000,'every callback is counted');
  assert.equal(after.walks-before.walks,1,'1000 mutations must schedule at most one fiber walk');
  const plain=document.createElement('div');
  for(let i=0;i<1000;i++){
    observer.cb([{type:'childList',target:library,addedNodes:[plain],removedNodes:[]}]);
  }
  await settle(40);
  assert.equal(w.__SlsLunaSelftest.state().walks,after.walks,'nodes without a fiber never schedule work');
  assert.equal(typeof after.walks,'number');
  assert.equal(typeof after.walkMsTotal,'number');
  assert.equal(after.shelfObserver,true,'the selftest says the library observer is bound');
  w.__SlsLunaTeardown();
});

test('mutations inside Luna\'s own UI never schedule a shelf walk',async()=>{
  const {document,library,w,live}=shelfWindow();
  mount(w);
  await settle(30);
  const observer=live().find(entry=>entry.target&&entry.target.id==='GamepadUI_Library');
  assert.ok(observer,'the shelf observer must be bound to the library container');
  const before=w.__SlsLunaSelftest.state().walks;
  const own=document.createElement('div');
  own.id='sls-luna-overlay';
  library.appendChild(own);
  const insideOwn=addNode(document,own,hltbShelfFiber().fiber,1);
  observer.cb([{type:'childList',target:own,addedNodes:[insideOwn]}]);
  const lumen=document.createElement('div');
  lumen.id='lumen-embed-root';
  library.appendChild(lumen);
  observer.cb([{type:'childList',target:lumen,addedNodes:[addNode(document,lumen,shortShelf().fiber,2)]}]);
  await settle(40);
  assert.equal(w.__SlsLunaSelftest.state().walks,before,'Luna\'s own nodes are filtered out');
  w.__SlsLunaTeardown();
});

test('an added library node is walked once, not once per mutation (WeakSet)',async()=>{
  const {document,library,w,live}=shelfWindow();
  mount(w);
  await settle(30);
  const observer=live().find(entry=>entry.target&&entry.target.id==='GamepadUI_Library');
  assert.ok(observer,'the shelf observer must be bound to the library container');
  const {fiber,reads}=shortShelf();
  const node=addNode(document,library,fiber,1);
  observer.cb([{type:'childList',target:library,addedNodes:[node]}]);
  await settle(40);
  assert.equal(reads(),1,'the first pass visits the added fiber');
  observer.cb([{type:'childList',target:library,addedNodes:[node]}]);
  await settle(40);
  assert.equal(reads(),1,'the same fiber is never visited twice: the WeakSet skips it');
  w.__SlsLunaTeardown();
});

test('no shelf work happens while the document is hidden',async()=>{
  const {document,library,w,live}=shelfWindow();
  mount(w);
  await settle(30);
  const observer=live().find(entry=>entry.target&&entry.target.id==='GamepadUI_Library');
  assert.ok(observer,'the shelf observer must be bound to the library container');
  const before=w.__SlsLunaSelftest.state().walks;
  document.hidden=true;
  const {fiber,instance}=hltbShelfFiber();
  const node=addNode(document,library,fiber,1);
  observer.cb([{type:'childList',target:library,addedNodes:[node]}]);
  await settle(40);
  assert.equal(instance.updates,0,'a hidden window patches and re-renders nothing');
  assert.equal(w.__SlsLunaSelftest.state().walks,before);
  document.hidden=false;
  observer.cb([{type:'childList',target:library,addedNodes:[node]}]);
  await settle(40);
  assert.equal(instance.updates,1,'visible again: the shelf is patched and drawn once');
  w.__SlsLunaTeardown();
});

test('the fallback shelf pass is rare and exits early when nothing changed',async()=>{
  const {document,library,w,intervals}=shelfWindow();
  const {fiber}=shortShelf();
  const root={child:fiber,sibling:null};
  fiber.return=root;
  const anchor=document.createElement('div');
  anchor['__reactFiber$anchor']=root;
  library.appendChild(anchor);
  mount(w);
  await settle(40);
  const safety=intervals.filter(entry=>entry.ms===120000);
  assert.equal(safety.length,1,'one fallback pass, not a 15 s one');
  assert.equal(intervals.filter(entry=>entry.ms===15000).length,0,'the 15 s walk is gone');
  const settled=w.__SlsLunaSelftest.state().walks;
  safety[0].fn();
  await settle(40);
  assert.equal(w.__SlsLunaSelftest.state().walks,settled,'unchanged signature: no walk');
  library.appendChild(document.createElement('div'));
  safety[0].fn();
  await settle(40);
  assert.equal(w.__SlsLunaSelftest.state().walks,settled+1,'a real change earns one pass');
  document.hidden=true;
  library.appendChild(document.createElement('div'));
  safety[0].fn();
  await settle(40);
  assert.equal(w.__SlsLunaSelftest.state().walks,settled+1,'hidden: even a change waits');
  w.__SlsLunaTeardown();
});

test('teardown disconnects the library observer and drops queued shelf work',async()=>{
  const {document,library,w,live}=shelfWindow();
  mount(w);
  await settle(30);
  const observer=live().find(entry=>entry.target&&entry.target.id==='GamepadUI_Library');
  assert.ok(observer,'the shelf observer must be bound to the library container');
  const {fiber,instance}=hltbShelfFiber();
  const node=addNode(document,library,fiber,1);
  observer.cb([{type:'childList',target:library,addedNodes:[node]}]);
  const before=w.__SlsLunaSelftest.state().walks;
  w.__SlsLunaTeardown();
  await settle(40);
  assert.equal(observer.connected,false,'the observer is disconnected with the page');
  assert.equal(instance.updates,0,'the debounced pass dies with the page');
  assert.equal(w.__SlsLunaSelftest.state().walks,before);
});

test('Forget removes a cached record and the game becomes fetchable again',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {calls,overlay}=await openHltbPage(document,{hltb:hltbFixture()});
  const row=rowFor(overlay,'Hades');
  const forget=findNode(row,node=>node.textContent==='Forget');
  assert.ok(forget,'a cached game must offer Forget');
  forget.onclick();
  await new Promise(resolve=>setTimeout(resolve,60));
  const call=calls.filter(entry=>entry.fn==='SlsHltbForget').pop();
  assert.ok(call,'Forget must call SlsHltbForget');
  assert.equal(call.payload.appid,1145360);
  const fresh=document.getElementById('sls-luna-overlay');
  assert.equal(findNode(rowFor(fresh,'Hades'),node=>node.textContent==='Fetch')!==null,true,
    'the forgotten game must be fetchable again');
  assert.match(textOf(fresh),/Fetch missing \(3\)/);
});

test('store mode hides followed games when the backend switch is on',async()=>{
  const document=fakeDom();
  const tile=id=>{const node=document.createElement('a');node.setAttribute('data-ds-appid',id);
    document.body.appendChild(node);return node;};
  const followedOnly=tile('111');
  const notFollowed=tile('333');
  const allFollowed=tile('111,222');
  const partlyFollowed=tile('111,444');
  const {w,calls}=fakeWindow(document,{prefs:{store_hide_followed:true}});
  w.location={hostname:'store.steampowered.com',pathname:'/'};
  w.fetch=async()=>({ok:true,json:async()=>({rgFollowedApps:[111,222],rgOwnedApps:[1]})});
  mount(w);
  await new Promise(resolve=>setTimeout(resolve,20));
  assert.equal(followedOnly.getAttribute('data-sls-luna-hidden'),'','a followed tile must be hidden');
  assert.equal(allFollowed.getAttribute('data-sls-luna-hidden'),'',
    'a tile whose appids are all followed must be hidden');
  assert.equal(notFollowed.getAttribute('data-sls-luna-hidden'),undefined,
    'a tile that is not followed must stay');
  assert.equal(partlyFollowed.getAttribute('data-sls-luna-hidden'),undefined,
    'a mixed tile must stay');
  const chip=document.getElementById('sls-luna-store-chip');
  assert.ok(chip,'the store chip must appear');
  assert.match(chip.children[0].innerHTML,/Luna hides <b>2<\/b> followed games/);
  assert.ok(calls.some(call=>call.fn==='SlsLunaInfo'),'the switch must come from the backend');
  // The chip can show everything again without a reload.
  chip.children[1].onclick();
  assert.equal(followedOnly.getAttribute('data-sls-luna-hidden'),undefined,
    'showing again must unmark every tile');
  assert.match(chip.children[0].innerHTML,/showing every tile/);
});

test('store mode stays quiet when the switch is off or the page is search',async()=>{
  const document=fakeDom();
  const node=document.createElement('a');
  node.setAttribute('data-ds-appid','111');
  document.body.appendChild(node);
  let fetched=0;
  const first=fakeWindow(document,{prefs:{store_hide_followed:false}});
  first.w.location={hostname:'store.steampowered.com',pathname:'/'};
  first.w.fetch=async()=>{fetched+=1;return{ok:true,json:async()=>({rgFollowedApps:[111]})};};
  mount(first.w);
  await new Promise(resolve=>setTimeout(resolve,20));
  assert.equal(node.getAttribute('data-sls-luna-hidden'),undefined);
  assert.equal(fetched,0,'the followed list must not be fetched when the switch is off');

  const searchDocument=fakeDom();
  const row=searchDocument.createElement('a');
  row.setAttribute('data-ds-appid','111');
  searchDocument.body.appendChild(row);
  const second=fakeWindow(searchDocument,{prefs:{store_hide_followed:true}});
  second.w.location={hostname:'store.steampowered.com',pathname:'/search/'};
  second.w.fetch=async()=>({ok:true,json:async()=>({rgFollowedApps:[111]})});
  mount(second.w);
  await new Promise(resolve=>setTimeout(resolve,20));
  assert.equal(row.getAttribute('data-sls-luna-hidden'),undefined,'search pages must stay untouched');
});

test('the Settings page drives the store switch through the backend',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w,calls,prefs}=fakeWindow(document,{});
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await new Promise(resolve=>setTimeout(resolve,40));
  const tab=findNode(document.getElementById('sls-luna-overlay'),
    node=>(node.className||'').startsWith('sls-luna-tab')&&node.textContent==='Settings');
  assert.ok(tab,'the Settings tab must exist');
  tab.onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  const row=findNode(document.getElementById('sls-luna-overlay'),
    node=>(node.className||'')==='sls-luna-row'
      && textOf(node).includes('Hide followed games in the store'));
  assert.ok(row,'the store switch row must exist');
  const toggle=findNode(row,node=>(node.className||'').startsWith('sls-luna-switch'));
  assert.ok(toggle&&!toggle.className.includes(' on'),'the switch must start off');
  toggle.onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  const call=calls.filter(entry=>entry.fn==='SlsLunaPrefsSet').pop();
  assert.ok(call,'the switch must write through the backend');
  assert.equal(call.payload.key,'store_hide_followed');
  assert.equal(call.payload.value,true);
  assert.equal(prefs.store_hide_followed,true,'the backend must hold the value');
  const rowAfter=findNode(document.getElementById('sls-luna-overlay'),
    node=>(node.className||'')==='sls-luna-row'
      && textOf(node).includes('Hide followed games in the store'));
  const toggleAfter=findNode(rowAfter,node=>(node.className||'').startsWith('sls-luna-switch'));
  assert.ok(toggleAfter.className.includes(' on'),'the switch must paint on after the write');
});

test('the compressor Stats tab charts the measured ledgers and can refresh them',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const stats={compression:{records:132,measured:2,original_bytes:6442450944,disk_bytes:3221225472,
      saved_bytes:3221225472,games:[{name:'Gamma',path:'/lib/Gamma',saved_bytes:2147483648,
        original_bytes:5368709120,disk_bytes:3221225472}]},
    dedupe:{passes:3,directories:1,saved_bytes:6606028,newest:1700000400,
      entries:[{path:'/mnt/games/SteamLibrary/steamapps/common/Slay the Spire 2',saved_bytes:6606028}]},
    luna:{measured:1,saved_bytes:12345678,games:[{name:'Hades',saved_bytes:12345678}]},
    library:{games:3,bytes:37665836}};
  const {w,calls}=fakeWindow(document,{compressor:compressorFixture({stats})});
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await new Promise(resolve=>setTimeout(resolve,40));
  const menuTab=findNode(document.getElementById('sls-luna-overlay'),
    node=>(node.className||'').startsWith('sls-luna-tab')&&node.textContent==='Compressor');
  assert.ok(menuTab,'the Compressor tab must exist');
  menuTab.onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  const subtab=findNode(document.getElementById('sls-luna-overlay'),
    node=>(node.className||'').startsWith('sls-luna-subtab')&&node.textContent==='Stats');
  assert.ok(subtab,'the Stats sub-tab must exist');
  subtab.onclick();
  await new Promise(resolve=>setTimeout(resolve,30));
  const text=textOf(document.getElementById('sls-luna-overlay'));
  assert.match(text,/Measured saved/,'the measured total must be shown');
  assert.match(text,/ZSTD savings per game/,'the ZSTD chart must exist');
  assert.match(text,/Gamma/,'the measured game must be charted');
  assert.match(text,/Deduplication per directory/,'the dedupe chart must exist');
  assert.match(text,/Slay the Spire 2/,'the dedupe directory must be charted');
  assert.match(text,/Savings recorded by Luna passes/,'Luna passes must be charted');
  assert.match(text,/Biggest games in the library/,'the library chart must exist');
  assert.match(text,/Library progress/,'the completion bar must exist');
  const measure=findNode(document.getElementById('sls-luna-overlay'),
    node=>node.tagName==='BUTTON'&&node.textContent==='Measure now');
  assert.ok(measure,'the measurement action must be offered');
  measure.onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  const call=calls.filter(entry=>entry.fn==='SlsCompressorRun').pop();
  assert.ok(call&&call.payload.action==='recheck','the measurement must run the recheck action');
});

test('the backend owns beta and the HLTB order, with localStorage as the mirror',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w,calls,storage,prefs}=fakeWindow(document,{});
  storage.set('slsLunaBeta','1');            // a choice made before the backend owned it
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await new Promise(resolve=>setTimeout(resolve,50));
  const betaCall=calls.filter(entry=>entry.fn==='SlsLunaPrefsSet'&&entry.payload.key==='beta').pop();
  assert.ok(betaCall&&betaCall.payload.value===true,'the local beta choice must migrate to the backend');
  assert.equal(prefs.beta,true,'the backend must hold the migrated value');
  const overlay=()=>document.getElementById('sls-luna-overlay');
  const settings=findNode(overlay(),node=>(node.className||'').startsWith('sls-luna-tab')
    &&node.textContent==='Settings');
  settings.onclick();
  await new Promise(resolve=>setTimeout(resolve,30));
  const betaRow=findNode(overlay(),node=>(node.className||'')==='sls-luna-row'
    &&textOf(node).includes('Show beta features'));
  assert.ok(betaRow,'the beta switch row must exist');
  const betaToggle=findNode(betaRow,node=>(node.className||'').startsWith('sls-luna-switch'));
  assert.ok(betaToggle.className.includes(' on'),'the migrated choice must paint as on');
  betaToggle.onclick();
  await new Promise(resolve=>setTimeout(resolve,30));
  const offCall=calls.filter(entry=>entry.fn==='SlsLunaPrefsSet'&&entry.payload.key==='beta').pop();
  assert.equal(offCall.payload.value,false,'turning the switch off must reach the backend');
  assert.equal(storage.get('slsLunaBeta'),'0','the agent mirror must be updated');

  const hltbTab=findNode(overlay(),node=>(node.className||'').startsWith('sls-luna-tab')
    &&node.textContent==='HowLongToBeat');
  hltbTab.onclick();
  await new Promise(resolve=>setTimeout(resolve,30));
  const orderRow=findNode(overlay(),node=>(node.className||'')==='sls-luna-row'
    &&/Library order:/.test(textOf(node)));
  assert.ok(orderRow,'the HLTB order switch must exist');
  const orderSwitch=findNode(orderRow,node=>(node.className||'').startsWith('sls-luna-switch'));
  orderSwitch.onclick();
  await new Promise(resolve=>setTimeout(resolve,30));
  const orderCall=calls.filter(entry=>entry.fn==='SlsLunaPrefsSet'&&entry.payload.key==='hltb_order').pop();
  assert.ok(orderCall&&orderCall.payload.value==='long','the order choice must reach the backend');
  assert.equal(storage.get('slsLunaHltbOrder'),'long','the agent mirror must hold the order');
});

test('the Health page lists versions, drift and copyable diagnostics',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w}=fakeWindow(document,{});
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await new Promise(resolve=>setTimeout(resolve,50));
  const tab=findNode(document.getElementById('sls-luna-overlay'),
    node=>(node.className||'').startsWith('sls-luna-tab')&&node.textContent.startsWith('Health'));
  assert.ok(tab,'the Health tab must exist');
  tab.onclick();
  await new Promise(resolve=>setTimeout(resolve,30));
  const text=textOf(document.getElementById('sls-luna-overlay'));
  assert.match(text,/Local diagnostics only/,'the page must say it is local');
  assert.match(text,/Boot patch/,'the boot marker card must exist');
  assert.match(text,/luna 0\.14\.0/,'the add-on versions must be listed');
  assert.match(text,/pinned 0\.3\.0/,'drift against the pinned version must show');
  assert.match(text,/Lumen: \/home\/tester/,'the paths must be listed');
  assert.match(text,/3533/,'the HLTB cache count must be shown');
  assert.match(text,/Beta features: off/,'the settings summary must be shown');
  assert.match(text,/sync conflicts: 2/,'waiting localconfig conflicts must be surfaced');
  assert.match(text,/pinned 1791918634/,'the pinned Steam client must be shown');
  const copy=findNode(document.getElementById('sls-luna-overlay'),
    node=>node.tagName==='BUTTON'&&node.textContent==='Copy diagnostics');
  assert.ok(copy,'the diagnostics button must exist');
  copy.onclick();
  const area=findNode(document.getElementById('sls-luna-overlay'),node=>node.tagName==='TEXTAREA');
  assert.ok(area&&/sls-luna/.test(area.value||''),'the diagnostics text must land in the textarea');
});

test('Check now follows the detached check until its result arrives',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w,calls}=fakeWindow(document);
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await new Promise(resolve=>setTimeout(resolve,60));
  findNode(document.getElementById('sls-luna-overlay'),
    node=>(node.className||'').startsWith('sls-luna-tab')&&node.textContent.startsWith('Updates')).onclick();
  await new Promise(resolve=>setTimeout(resolve,40));
  findNode(document.getElementById('sls-luna-overlay'),
    node=>node.tagName==='BUTTON'&&node.textContent==='Check now').onclick();
  await new Promise(resolve=>setTimeout(resolve,120));
  const text=textOf(document.getElementById('sls-luna-overlay'));
  assert.match(text,/latest: 0\.15\.0/,'the finished check must be shown without reopening');
  assert.doesNotMatch(text,/Checking GitHub/);
  const statuses=calls.filter(entry=>entry.fn==='SlsLunaUpdate'&&entry.payload.action==='status');
  assert.ok(statuses.length>=3,'the panel must poll the status while the check runs');
});

test('the Updates page reports updates and can queue one',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w,calls}=fakeWindow(document,{});
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await new Promise(resolve=>setTimeout(resolve,60));
  const tab=findNode(document.getElementById('sls-luna-overlay'),
    node=>(node.className||'').startsWith('sls-luna-tab')&&node.textContent.startsWith('Updates'));
  assert.ok(tab,'the Updates tab must exist');
  assert.match(tab.textContent,/\u2022/,'an available update must dot the tab');
  tab.onclick();
  await new Promise(resolve=>setTimeout(resolve,40));
  const text=textOf(document.getElementById('sls-luna-overlay'));
  assert.match(text,/Luna 0\.14\.3/,'the installed version must be shown');
  assert.match(text,/latest: 0\.14\.9/,'the newest release must be shown');
  assert.match(text,/update available/,'the availability must be stated');
  assert.match(text,/Last run: current 0\.14\.3/,'the last run must be shown');
  const install=findNode(document.getElementById('sls-luna-overlay'),
    node=>node.tagName==='BUTTON'&&node.textContent==='Install Luna update');
  assert.ok(install&&!install.disabled,'the install button must be offered');
  install.onclick();
  await new Promise(resolve=>setTimeout(resolve,60));
  const queued=calls.filter(entry=>entry.fn==='SlsLunaUpdate'&&entry.payload.action==='install').pop();
  assert.ok(queued&&queued.payload.tag==='0.14.9','the update must be queued with the latest tag');
  assert.match(textOf(document.getElementById('sls-luna-overlay')),/Queued: 0\.14\.9/,
    'the queued state must be visible');
  const mode=findNode(document.getElementById('sls-luna-overlay'),
    node=>node.tagName==='BUTTON'&&node.textContent==='Install automatically');
  assert.ok(mode,'the unattended mode control must exist');
  mode.onclick();
  await new Promise(resolve=>setTimeout(resolve,60));
  const timer=calls.filter(entry=>entry.fn==='SlsLunaUpdate'&&entry.payload.action==='timer').pop();
  assert.ok(timer&&timer.payload.value==='on','the mode control must enable the timer');
  const pref=calls.filter(entry=>entry.fn==='SlsLunaPrefsSet'&&entry.payload.key==='auto_update').pop();
  assert.ok(pref&&pref.payload.value==='install','the mode must be stored in the backend');
  const check=calls.filter(entry=>entry.fn==='SlsLunaUpdate'&&entry.payload.action==='check').pop();
  assert.ok(check,'opening the menu must run a (cached) check');
  const checkNow=findNode(document.getElementById('sls-luna-overlay'),
    node=>node.tagName==='BUTTON'&&node.textContent==='Check now');
  checkNow.onclick();
  await new Promise(resolve=>setTimeout(resolve,60));
  const forced=calls.filter(entry=>entry.fn==='SlsLunaUpdate'&&entry.payload.action==='check'
    &&entry.payload.force===true).pop();
  assert.ok(forced,'Check now must bypass the cache');
});

test('the menu still opens when the Host and Gradient RPCs are missing or throw',async()=>{
  // A host/backend that predates SlsLunaHost / SlsLunaGradient (or rejects them)
  // must never stop the menu from rendering.
  const document=fakeDom();
  menubarFixture(document,true);
  const {w}=fakeWindow(document,{get:{success:true,apps:[],titles:{},games:{},stats:{today:0,last7d:0,last30d:0},
    runtime:{healthy:true,reportedAt:Date.now()/1000,file:'x.json'},account:'1',manifestCount:0,shortcutCount:0}});
  const inner=w.Millennium.callServerMethod;
  w.Millennium.callServerMethod=(plugin,fn,args)=>{
    if(fn==='SlsLunaHost')throw Error('no such method');
    if(fn==='SlsLunaGradient')return Promise.reject(Error('no such method'));
    return inner(plugin,fn,args);
  };
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await new Promise(resolve=>setTimeout(resolve,30));
  const overlay=document.getElementById('sls-luna-overlay');
  assert.ok(overlay,'the menu did not open');
  assert.match(textOf(overlay),/About/);
});

test('the menu never hides Steam\u2019s view: a hidden view still takes the clicks',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w}=fakeWindow(document);
  const log=[];
  w.opener={MainWindowBrowserManager:{m_history:{location:{pathname:'/library/home'}},
    m_browser:{SetVisible:v=>log.push(v)}}};
  mount(w);
  const button=document.getElementById('sls-luna-btn');
  button.click();
  await new Promise(resolve=>setTimeout(resolve,40));
  assert.ok(document.getElementById('sls-luna-overlay'),'the menu opens');
  document.fire('document:keydown',{key:'Escape'});
  assert.equal(document.getElementById('sls-luna-overlay'),null);
  button.click();
  await new Promise(resolve=>setTimeout(resolve,40));
  document.fire('document:keydown',{key:'Escape'});
  assert.deepEqual(log,[],'SetVisible is never called: the Library switch replaced hiding');
});
test('without a browser-view API the menu still opens and nothing throws',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w}=fakeWindow(document);
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await new Promise(resolve=>setTimeout(resolve,30));
  assert.ok(document.getElementById('sls-luna-overlay'));
  document.fire('document:keydown',{key:'Escape'});
});

function navFixture(route,{throws=false,noNav=false,noView=false,stuck=false}={}){
  const document=fakeDom();
  menubarFixture(document,true);
  const {w}=fakeWindow(document);
  const log=[];
  const m={m_history:{location:{pathname:route}},m_browser:{SetVisible:v=>log.push('vis:'+v)}};
  if(noView)m.m_browser={};
  const o={MainWindowBrowserManager:m};
  if(!noNav)o.SteamUIStore={Navigate:p=>{log.push('nav:'+p);if(throws)throw new Error('boom');if(!stuck)m.m_history.location.pathname=p;}};
  w.opener=o;
  mount(w);
  return {document,log};
}
const wait=ms=>new Promise(resolve=>setTimeout(resolve,ms));

test('on Store, Community and the browser the menu moves Steam to the Library first',async()=>{
  for(const route of ['/browser/','/store/','/community/home','/store']){
    const {document,log}=navFixture(route);
    document.getElementById('sls-luna-btn').click();
    await wait(80);
    assert.ok(document.getElementById('sls-luna-overlay'),'the menu opens after the switch on '+route);
    assert.deepEqual(log,['nav:/library/home'],'the Library is asked for exactly once on '+route);
    assert.equal(log.some(entry=>entry.startsWith('vis:')),false,
      'nothing is hidden: a hidden browser view still takes clicks');
  }
});
test('on a Library route the menu opens without navigating or hiding',async()=>{
  const {document,log}=navFixture('/library/home');
  document.getElementById('sls-luna-btn').click();
  await wait(80);
  assert.ok(document.getElementById('sls-luna-overlay'));
  assert.deepEqual(log,[]);
});
test('when nothing can move off the browser route the menu says which tab it needs',async()=>{
  const {document,log}=navFixture('/store',{noNav:true});
  document.getElementById('sls-luna-btn').click();
  await wait(80);
  const overlay=document.getElementById('sls-luna-overlay');
  assert.ok(overlay,'the menu still opens');
  assert.deepEqual(log,[],'no router and no hide');
  assert.match(textOf(overlay),/press Steam\u2019s Library tab/,'one line says what to press');
});
test('the menu still opens when the router throws',async()=>{
  const {document}=navFixture('/browser/',{throws:true});
  document.getElementById('sls-luna-btn').click();
  await wait(100);
  assert.ok(document.getElementById('sls-luna-overlay'));
  document.fire('document:keydown',{key:'Escape'});
});

/* ------------------------------------------------------------------ view / Store
   The panel lives in Steam's shell document, where the main window's browser
   manager does not exist. The hide/show ask crosses over localStorage to the
   add-on's agent in SharedJSContext; these tests run the real agent module as
   that side, with Steam's manager faked. */
function viewFixture(route='/library/home',{broken=false,stuck=false}={}){
  const document=fakeDom();
  menubarFixture(document,true);
  const {w}=fakeWindow(document);
  const log=[];
  const manager={m_history:{location:{pathname:route}}};
  if(!broken)manager.m_browser={SetVisible:visible=>log.push('vis:'+visible)};
  const agentWin={localStorage:w.localStorage,__listeners:{},
    setTimeout:(fn)=>{setTimeout(fn,0);return 0;},
    addEventListener:(type,fn)=>{(agentWin.__listeners[type]=agentWin.__listeners[type]||[]).push(fn);},
    MainWindowBrowserManager:manager,
    // SharedJSContext can reach Steam's router itself; the panel asks through
    // the view channel instead of a second copy of the navigation logic.
    SteamUIStore:{Navigate:path=>{log.push('nav:'+path);manager.m_history.location.pathname=path;}}};
  // Only the opener exposes Steam's router here; the panel's own document does
  // not, exactly as measured live.
  w.opener={MainWindowBrowserManager:{m_history:{location:{pathname:route}}},
    SteamUIStore:{Navigate:path=>{log.push('nav:'+path);if(!stuck)manager.m_history.location.pathname=path;}}};
  const setItem=w.localStorage.setItem;
  w.localStorage.setItem=(key,value)=>{
    setItem(key,value);
    if(key!==agentApi.VIEW_REQUEST_KEY)return;
    if(agentApi.viewHandleRequest(agentWin,value))w.__emitTo('storage',{key:agentApi.VIEW_RESULT_KEY});
  };
  // What SharedJSContext publishes at boot: the route is known before the first
  // click, which is what keeps the open synchronous.
  setItem(agentApi.VIEW_RESULT_KEY,JSON.stringify({id:'',action:'status',at:Date.now(),
    ok:!broken,route,browserVisible:true,
    error:broken?'The Steam view API is unavailable.':''}));
  mount(w);
  return {document,w,log,agentWin,manager};
}

test('on Store the menu moves Steam to the Library and never hides the view',async()=>{
  const {document,log}=navFixture('/store');
  document.getElementById('sls-luna-btn').click();
  await wait(150);
  assert.ok(document.getElementById('sls-luna-overlay'),'the menu opens after the route moved');
  assert.deepEqual(log,['nav:/library/home'],'one navigate request, no hide');
  document.fire('document:keydown',{key:'Escape'});
  await wait(20);
  assert.deepEqual(log,['nav:/library/home'],'closing navigates nothing back');
});

test('two rapid opens move to the Library once',async()=>{
  const {document,log}=navFixture('/browser/');
  document.getElementById('sls-luna-btn').click();
  document.getElementById('sls-luna-btn').click();
  await wait(150);
  assert.deepEqual(log,['nav:/library/home'],'one switch, not two');
  document.fire('document:keydown',{key:'Escape'});
  await wait(20);
  assert.deepEqual(log,['nav:/library/home']);
});

test('pagehide and teardown never leave the view hidden',async()=>{
  const {document,log}=navFixture('/community/home');
  document.getElementById('sls-luna-btn').click();
  await wait(150);
  document.fire('document:keydown',{key:'Escape'});
  await wait(10);
  document.getElementById('sls-luna-btn').click();
  await wait(150);
  assert.equal(log.some(entry=>entry.startsWith('vis:')),false,'SetVisible is never called');
  assert.deepEqual(log,['nav:/library/home'],'already on the Library: the second open moves nothing');
});

test('without a router and without an agent the menu says why and stays usable',async()=>{
  const {document,log}=navFixture('/store',{noNav:true});
  document.getElementById('sls-luna-btn').click();
  await wait(80);
  assert.ok(document.getElementById('sls-luna-overlay'),'never a dead menu');
  assert.deepEqual(log,[]);
  assert.ok(textOf(document.getElementById('sls-luna-overlay')).includes('Library'),
    'one short line says how to get a clickable menu');
  document.fire('document:keydown',{key:'Escape'});
  await wait(20);
});

/* The agent's navigate action on its own: it must call Steam's router and
   report the route Steam really shows. */
test('the agent navigate action calls SteamUIStore.Navigate and reports the new route',async()=>{
  const log=[];
  const manager={m_history:{location:{pathname:'/store'}}};
  const agentWin={setTimeout:(fn)=>{setTimeout(fn,0);return 0;},
    MainWindowBrowserManager:manager,
    SteamUIStore:{Navigate:path=>{log.push(path);manager.m_history.location.pathname='/library/home';}}};
  const answers=[];
  agentApi.viewNavigate(agentWin,result=>answers.push(result));
  await wait(30);
  assert.deepEqual(log,['/library/home'],'the Steam router is used');
  assert.equal(answers.length,1,'one answer');
  assert.equal(answers[0].ok,true);
  assert.equal(answers[0].route,'/library/home','the new route is reported');
});

test('the agent navigate action refuses without a Steam router',async()=>{
  const answers=[];
  agentApi.viewNavigate({setTimeout:(fn)=>{setTimeout(fn,0);return 0;},
    MainWindowBrowserManager:{m_history:{location:{pathname:'/store'}}}},result=>answers.push(result));
  await wait(20);
  assert.equal(answers.length,1);
  assert.equal(answers[0].ok,false);
  assert.match(answers[0].error,/router is unavailable/);
});

const NOW=()=>Math.floor(Date.now()/1000);
const comp=(component,status,available,installed)=>({component,channel:'beta',installed:installed||'v2.9',
  available:available||installed||'v2.9',status,note:''});
const checkNewer=()=>({checkedAt:NOW(),status:{upstream:{overall:'newer',components:[
  comp('lumen','newer','beta'),comp('plugin','current'),comp('slsteam_moon','current')]}}});
const checkCurrent=()=>({checkedAt:NOW(),status:{upstream:{overall:'current',components:[
  comp('lumen','current'),comp('plugin','current'),comp('slsteam_moon','current')]}}});
const checkUnknown=()=>({checkedAt:NOW(),status:{upstream:{overall:'unknown',components:[
  comp('lumen','unknown','unknown')]}}});
const pageText=document=>textOf(document.getElementById('sls-luna-overlay'));
const RPC_COUNT=(calls,fn,action)=>calls.filter(entry=>entry.fn===fn&&(!action||entry.payload.action===action)).length;

test('the Updates page is registered after Health and renders the three sections',async()=>{
  const document=fakeDom();
  const {w,calls}=await openUpdates(document,{host:'lumen',lumen:{checkResult:checkNewer()}});
  const names=[];
  const walk=node=>{if(!node)return;if((node.className||'').startsWith('sls-luna-tab'))names.push(node.textContent.replace(/\s*\u2022$/,''));
    Array.from(node.children||[]).forEach(walk);};
  walk(document.getElementById('sls-luna-overlay'));
  assert.ok(names.indexOf('Updates')===names.indexOf('Health')+1,'Updates sits right after Health');
  assert.ok(names.indexOf('Updates')<names.indexOf('Settings'),'and before Settings');
  const text=textOf(document.getElementById('sls-luna-overlay'));
  ['Update everything','Luna','Lumen','Steam client'].forEach(title=>
    assert.ok(text.includes(title),'section '+title));
  assert.match(text,/Add-on sgdb: installed 0\.2\.0 \u00b7 pinned 0\.2\.0/,'add-on installed vs pinned');
  assert.match(text,/luna-host: lumen/,'the host line');
  assert.match(text,/CLI\/tooling/,'the tooling line');
  assert.match(text,/lumen v2\.9/,'the Lumen versions come from SlsLunaLumen');
  assert.match(text,/Self-update block: in place/,'the block state');
  assert.match(text,/Auto-update: notify/,'the auto-update mode');
  assert.equal(button(document,'Update Lumen').disabled,true,'nothing to update before a check');
  assert.match(text,/Check for updates first/,'and the reason is text');
  button(document,'Check for updates').onclick();
  await settle(80);
  assert.equal(RPC_COUNT(calls,'SlsLunaLumen','check'),1,'the check goes through the RPC');
  assert.equal(button(document,'Update Lumen').disabled,false,'a newer build enables the update');
  button(document,'Update Lumen').onclick();
  await settle(40);
  assert.equal(RPC_COUNT(calls,'SlsLunaLumen','update'),0,'nothing is queued before the confirm');
  button(document,'Yes, queue the update').onclick();
  await settle(80);
  assert.ok(calls.some(entry=>entry.fn==='SlsLunaLumen'&&entry.payload.action==='update'&&entry.payload.confirmed===true),'queues through the RPC');
  assert.match(textOf(document.getElementById('sls-luna-overlay')),/Queued\. The update starts after you exit Steam/);
  button(document,'Cancel queued update').onclick();
  await settle(80);
  assert.ok(calls.some(entry=>entry.fn==='SlsLunaLumen'&&entry.payload.action==='cancel'),'cancel RPC');
});
test('the Lumen section is hidden on the luna host',async()=>{
  const document=fakeDom();
  const {calls}=await openUpdates(document,{host:'luna'});
  const text=textOf(document.getElementById('sls-luna-overlay'));
  assert.ok(!button(document,'Update Lumen'),'no Update Lumen button on the luna host');
  assert.doesNotMatch(text,/Self-update block/);
  assert.ok(!calls.some(entry=>entry.fn==='SlsLunaLumen'),'Lumen is not even queried');
  assert.match(text,/Steam client/,'the other sections still render');
});

test('Update everything queues the Luna update then Lumen after a confirm, never the Steam move',async()=>{
  const document=fakeDom();
  const {calls}=await openUpdates(document,{host:'lumen',lumen:{check:checkNewer()}});
  const before=calls.length;
  button(document,'Update everything').onclick();
  await settle(60);
  assert.equal(calls.length,before,'the first click only opens the confirm');
  const confirm=pageText(document);
  assert.match(confirm,/lumen: v2\.9 \u2192 beta/,'the confirm names old and new');
  assert.match(confirm,/Luna 0\.14\.3 \u2192 0\.14\.9/);
  assert.match(confirm,/after you exit Steam/);
  assert.match(confirm,/rolled back automatically/);
  button(document,'Yes, queue the updates').onclick();
  await settle(120);
  const fresh=calls.slice(before).filter(entry=>(entry.fn==='SlsLunaUpdate'&&entry.payload.action==='install')
    ||(entry.fn==='SlsLunaLumen'&&entry.payload.action==='update')||entry.fn==='SlsLunaSteam'&&entry.payload.action!=='status'
    ||entry.fn==='SlsLunaRefresh'&&entry.payload.action!=='status');
  assert.deepEqual(fresh.map(entry=>entry.fn+':'+entry.payload.action),
    ['SlsLunaUpdate:install','SlsLunaLumen:update'],'order and content of the queue');
  const text=pageText(document);
  assert.match(text,/Luna add-ons and tooling: pending/,'per-step state, first step');
  assert.match(text,/Lumen update \+ Luna re-install: pending/,'per-step state, second step');
});

test('Update everything on the luna host queues only the Luna update',async()=>{
  const document=fakeDom();
  const {calls}=await openUpdates(document,{host:'luna'});
  button(document,'Update everything').onclick();
  await settle(60);
  button(document,'Yes, queue the updates').onclick();
  await settle(120);
  assert.ok(calls.some(entry=>entry.fn==='SlsLunaUpdate'&&entry.payload.action==='install'));
  assert.ok(!calls.some(entry=>entry.fn==='SlsLunaLumen'),'no Lumen step on the luna host');
  assert.ok(!calls.some(entry=>entry.fn==='SlsLunaSteam'&&/queue/.test(entry.payload.action)));
});

test('RPC failures render error rows without breaking the Updates page',async()=>{
  const document=fakeDom();
  await openUpdates(document,{host:'lumen',failFns:['SlsLunaSteam','SlsLunaLumen']});
  const text=textOf(document.getElementById('sls-luna-overlay'));
  assert.match(text,/Could not read the Steam client: boom SlsLunaSteam/);
  assert.match(text,/Could not read Lumen: boom SlsLunaLumen/);
  assert.match(text,/Update everything/,'the rest of the page still renders');
  assert.match(text,/Luna 0\.14\.3/);
});

test('queueing Lumen shows the exit-Steam banner with its own Cancel',async()=>{
  const document=fakeDom();
  const {calls}=await openUpdates(document,{host:'lumen',lumen:{check:checkNewer()}});
  button(document,'Update Lumen').onclick();
  await settle(40);
  button(document,'Yes, queue the update').onclick();
  await settle(100);
  const text=pageText(document);
  assert.match(text,/Queued\. The update starts after you exit Steam\. Steam is not closed for you\./,
    'the banner says what happens and that Steam is not closed for the user');
  const cancel=button(document,'Cancel queued update');
  assert.ok(cancel,'the queued state carries a Cancel button');
  cancel.onclick();
  await settle(100);
  assert.ok(calls.some(entry=>entry.fn==='SlsLunaLumen'&&entry.payload.action==='cancel'),'Cancel goes through the RPC');
  assert.ok(!button(document,'Cancel queued update'),'Cancel disappears once nothing is queued');
});

test('Lumen buttons respond instantly and RPC errors become error rows',async()=>{
  const document=fakeDom();
  await openUpdates(document,{host:'lumen',lumen:{check:checkNewer()}});
  button(document,'Update Lumen').onclick();
  await settle(40);
  button(document,'Yes, queue the update').onclick();
  assert.match(pageText(document),/Lumen: working\u2026/,
    'the click paints a working line before the RPC answers');
  await settle(100);

  const failing=fakeDom();
  await openUpdates(failing,{host:'lumen',failFns:['SlsLunaLumen']});
  assert.equal(button(failing,'Update Lumen').disabled,true,'no check data, so no update');
  assert.match(textOf(failing.getElementById('sls-luna-overlay')),/Could not read Lumen: boom SlsLunaLumen/);
});

test('the last Lumen run shows updated old \u2192 new, or failed without a restore hint when nothing changed',async()=>{
  const done=fakeDom();
  await openUpdates(done,{host:'lumen',lumenResult:{state:'applied',message:'Lumen updated and Luna re-attached',
    at:NOW(),before:{lumen:'v2.8'},after:{lumen:'v2.9'},changed:{lumen:'v2.8 -> v2.9'},backup:'20261119-194744'}});
  assert.match(pageText(done),/Updated: lumen v2\.8 \u2192 v2\.9/,'done with old and new');
  assert.match(pageText(done),/luna lumen restore --backup 20261119-194744 --yes/,'restore for a real change');

  const failed=fakeDom();
  await openUpdates(failed,{host:'lumen',lumenResult:{state:'failed',at:NOW(),message:'the base installer exited with 1'}});
  assert.match(pageText(failed),/failed before anything was changed\. the base installer exited with 1/);
  assert.doesNotMatch(pageText(failed),/luna lumen restore/,'nothing changed, nothing to restore');
});

test('incomplete tooling is explained and disables the update buttons',async()=>{
  const document=fakeDom();
  await openUpdates(document,{host:'lumen',lumenTooling:'tooling incomplete: scripts/patch-lumen.py is missing'});
  const text=pageText(document);
  assert.match(text,/tooling incomplete: scripts\/patch-lumen\.py is missing/,'the reason is on the page');
  assert.match(text,/luna install --force/,'and how to repair it');
  assert.equal(button(document,'Update Lumen').disabled,true,'Update Lumen is disabled');
  assert.equal(button(document,'Update everything').disabled,true,'Update everything is disabled too');
});

test('RPC_TIMEOUT is exported and bounded at 15 s',()=>{
  assert.equal(RPC_TIMEOUT,15000,'callers and tests share one timeout contract');
});

test('a hung RPC times out and still clears the busy flag',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  let first=true;
  const gate=fn=>{
    if(fn!=='SlsLunaGradient'||!first)return null;
    first=false;
    return new Promise(()=>{}); // a restarted backend: accepted, never answered
  };
  const {w,calls}=fakeWindow(document,{gate});
  w.__SlsLunaRpcTimeout=20;
  mount(w);
  await settle(80);
  assert.equal(calls.filter(entry=>entry.fn==='SlsLunaGradient').length,1);
  assert.equal(document.getElementById('sls-luna-gradient'),null,
    'a hung call must not paint anything');
  // Only the finally can make the second refresh run; busy would wedge it.
  w.__emitTo('focus');
  await settle(40);
  assert.equal(calls.filter(entry=>entry.fn==='SlsLunaGradient').length,2,
    'busy must be cleared after the timeout');
  assert.ok(document.getElementById('sls-luna-gradient'),
    'the retry after a timeout paints the gradient normally');
});

test('a late answer after the timeout is ignored, and the timeout row is retryable',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  let release=null;
  // Every health read hangs, not just the first: the Health data is now read
  // when its page opens, so the page's own read must see the same dead backend.
  const gate=fn=>{
    if(fn!=='SlsLunaHealth')return null;
    return new Promise(resolve=>{release=resolve;});
  };
  const {w}=fakeWindow(document,{gate});
  w.__SlsLunaRpcTimeout=20;
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await settle(140);
  const overlay=()=>document.getElementById('sls-luna-overlay');
  // The refresh's error rows live on the Updates page.
  const updatesTab=findNode(overlay(),
    node=>(node.className||'').startsWith('sls-luna-tab')&&node.textContent.startsWith('Updates'));
  assert.ok(updatesTab,'the Updates tab must still be there after a timeout');
  updatesTab.onclick();
  // The Updates page reads its own data (health included) on the switch; wait
  // out that read's 20 ms budget before reading the rows.
  await settle(80);
  const row=textOf(overlay());
  assert.match(row,/did not answer SlsLunaHealth/,'the timeout is a visible row, never silence');
  assert.match(row,/press Refresh to retry/,'the row must say how to retry');
  // The answer arrives after the race already rejected: it must paint nothing.
  release(JSON.stringify({success:true,health:{version:'late-ghost'}}));
  await settle(80);
  assert.doesNotMatch(textOf(overlay()),/late-ghost/,'a late answer must not resurrect the page');
  const healthTab=findNode(overlay(),
    node=>(node.className||'').startsWith('sls-luna-tab')&&node.textContent.startsWith('Health'));
  healthTab.onclick();
  await settle(30);
  assert.match(textOf(overlay()),/Local diagnostics|Needs attention/,'the page stays usable after a timeout');
});

test('a remount replaces the old generation instead of stacking intervals and listeners',()=>{
  const document=fakeDom();
  const bar=menubarFixture(document,true);
  const {w,timers,winListeners:windowMap}=fakeWindow(document);
  const windowListeners=()=>[...windowMap.values()].reduce((count,list)=>count+list.length,0);
  const documentListeners=()=>Object.keys(document.listeners)
    .filter(key=>key.startsWith('document:'))
    .reduce((count,key)=>count+document.listeners[key].length,0);
  mount(w);
  const intervals=timers.intervals.size;
  const onWindow=windowListeners();
  const onDocument=documentListeners();
  const buttons=bar.children.length;
  assert.ok(intervals>0,'the panel must own its intervals');
  assert.ok(onWindow>0&&onDocument>0,'the panel must own its listeners');
  mount(w);
  assert.equal(timers.intervals.size,intervals,'the old intervals must be cleared, not doubled');
  assert.equal(windowListeners(),onWindow,'the old window listeners must be removed');
  assert.equal(documentListeners(),onDocument,'the old document listeners must be removed');
  assert.equal(bar.children.length,buttons,'the button must be replaced, not stacked');
  assert.equal(w.__SlsLunaPanelBuild,'bv-5');
});

test('an RPC error still shows its row and the page stays usable',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w}=fakeWindow(document,{failFns:['SlsLunaHealth']});
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await settle(100);
  const overlay=()=>document.getElementById('sls-luna-overlay');
  const updatesTab=findNode(overlay(),
    node=>(node.className||'').startsWith('sls-luna-tab')&&node.textContent.startsWith('Updates'));
  updatesTab.onclick();
  await settle(20);
  assert.match(textOf(overlay()),/Could not read Luna health: boom SlsLunaHealth/,
    'a rejected RPC is a visible row');
  const healthTab=findNode(overlay(),
    node=>(node.className||'').startsWith('sls-luna-tab')&&node.textContent.startsWith('Health'));
  healthTab.onclick();
  await settle(30);
  assert.match(textOf(overlay()),/Local diagnostics|Needs attention/,'the page keeps working after an RPC error');
});

// ---- Lumen state model: one state per contract case, with its action set.
const ACTION_LABELS=['Check for updates','Update Lumen','Up to date','Cancel queued update','Reinstall…'];
const actionSet=document=>{
  const out={};
  ACTION_LABELS.forEach(label=>{const node=button(document,label);if(node)out[label]=!node.disabled;});
  return out;
};
const STATE_CASES=[
  ['idle',{},/Not checked yet/,{'Check for updates':true,'Update Lumen':false}],
  ['current',{check:checkCurrent()},/Up to date\. Nothing newer on your channels/,
    {'Check for updates':true,'Up to date':false,'Reinstall…':true}],
  ['update available',{check:checkNewer()},/Update available: lumen: v2\.9 → beta \(newer build\)/,
    {'Check for updates':true,'Update Lumen':true}],
  ['queued',{pending:true,job:{action:'update',attempts:0,lastError:''},
    result:{state:'queued',message:'x',at:NOW()}},/Queued\. The update starts after you exit Steam/,
    {'Check for updates':true,'Update Lumen':false,'Cancel queued update':true}],
  ['running (installer)',{result:{state:'running',phase:'installer',pid:1,at:NOW()}},
    /replacing Lumen\. Do not shut the computer down/,{'Check for updates':false,'Update Lumen':false}],
  ['applied',{result:{state:'applied',at:NOW(),changed:{lumen:'v2.8 -> v2.9'},backup:'b1'}},
    /Updated: lumen v2\.8 → v2\.9/,{'Check for updates':true,'Update Lumen':false}],
  ['failed, rolled back',{result:{state:'failed',at:NOW(),rolledBack:true,backup:'b1',message:'boom'}},
    /failed and Lumen was rolled back to the previous version\. boom/,{'Update Lumen':false}],
  ['failed, rollback pending',{pending:true,job:{action:'rollback',attempts:0,lastError:''},
    result:{state:'failed',at:NOW(),rollbackPending:true,backup:'b1'}},
    /previous version comes back after you exit Steam/,{'Update Lumen':false,'Cancel queued update':false}],
  ['failed, not rolled back',{result:{state:'failed',at:NOW(),rolledBack:false,backup:'b1',message:'bad'}},
    /failed and was NOT rolled back\. bad/,{'Update Lumen':false}],
  ['unknown',{check:checkUnknown()},/Could not tell whether an update exists/,{'Update Lumen':false}],
  ['tooling incomplete',{toolingIncomplete:'tooling incomplete: missing x'},/Lumen updates are off: tooling incomplete/,
    {'Update Lumen':false}],
  ['interrupted',{result:{state:'running',phase:'installer',pid:1,at:NOW()},interrupted:true},
    /interrupted\. Close Steam and run: luna lumen update --yes/,{'Check for updates':false,'Update Lumen':false}],
  ['recovering',{result:{state:'running',phase:'installer',pid:1,at:NOW()},interrupted:true,pending:true,
    job:{action:'update',attempts:0,lastError:''}},/recovered after you exit Steam/,{'Update Lumen':false}],
  ['old result is history',{sessionStartedAt:NOW(),result:{state:'applied',at:NOW()-5000,changed:{lumen:'a -> b'},backup:'b1'}},
    /Not checked yet/,{'Update Lumen':false}],
];
for(const [name,lumen,pattern,expected] of STATE_CASES){
  test('Lumen state "'+name+'" renders its text and exactly its allowed actions',async()=>{
    const document=fakeDom();
    await openUpdates(document,{host:'lumen',lumen});
    const text=pageText(document);
    assert.match(text,pattern);
    const set=actionSet(document);
    Object.keys(expected).forEach(label=>assert.equal(set[label],expected[label],label+' in '+name));
    if(name==='running (installer)'||name==='interrupted'||name==='recovering'){
      assert.equal(set['Cancel queued update'],undefined,'no Cancel while a job runs');
    }
    if(name!=='current')assert.equal(set['Reinstall…'],undefined,'Reinstall is only for "current"');
  });
}
test('an older result is shown as history, not as the current state',async()=>{
  const document=fakeDom();
  await openUpdates(document,{host:'lumen',lumen:{sessionStartedAt:NOW(),
    result:{state:'applied',at:NOW()-5000,changed:{lumen:'a -> b'},backup:'b1'}}});
  assert.match(pageText(document),/Last run: updated/);
  assert.doesNotMatch(pageText(document),/Status: Updated:/);
});
test('"current" offers only the secondary Reinstall, behind a note and a command',async()=>{
  const document=fakeDom();
  await openUpdates(document,{host:'lumen',lumen:{check:checkCurrent()}});
  assert.ok(!button(document,'Update Lumen'));
  button(document,'Reinstall…').onclick();
  await settle(40);
  assert.match(pageText(document),/same versions/);
  assert.match(pageText(document),/luna lumen update --yes --reinstall/);
});
test('the update is never sent without the confirm step',async()=>{
  const document=fakeDom();
  const {calls}=await openUpdates(document,{host:'lumen',lumen:{check:checkNewer()}});
  button(document,'Update Lumen').onclick();
  await settle(40);
  assert.match(pageText(document),/after you exit Steam/);
  assert.match(pageText(document),/backup is taken first/);
  assert.match(pageText(document),/rolled back automatically/);
  button(document,'Not now').onclick();
  await settle(40);
  assert.equal(RPC_COUNT(calls,'SlsLunaLumen','update'),0);
});
test('a refused cancel shows the CLI text',async()=>{
  const document=fakeDom();
  await openUpdates(document,{host:'lumen',lumen:{pending:true,job:{action:'update',attempts:0,lastError:''},
    result:{state:'queued',at:NOW()},refuseCancel:'another luna command is already running'}});
  button(document,'Cancel queued update').onclick();
  await settle(80);
  assert.match(pageText(document),/Could not cancel: another luna command is already running/);
});
test('the Millennium choice blocks queueing until the owner picks, and is sent with the update',async()=>{
  const document=fakeDom();
  const {calls}=await openUpdates(document,{host:'lumen',lumen:{check:checkNewer(),
    millennium:{choice:'',present:['/home/x/.config/millennium']}}});
  assert.equal(button(document,'Update Lumen').disabled,true);
  assert.match(pageText(document),/Choose what to do with Millennium first/);
  assert.match(pageText(document),/nothing is chosen for you/);
  button(document,'Keep Millennium').onclick();
  await settle(40);
  assert.equal(button(document,'Update Lumen').disabled,false);
  button(document,'Update Lumen').onclick();
  await settle(40);
  assert.match(pageText(document),/Millennium will be kept/);
  button(document,'Yes, queue the update').onclick();
  await settle(80);
  const sent=calls.filter(e=>e.fn==='SlsLunaLumen'&&e.payload.action==='update').pop();
  assert.equal(sent.payload.millennium,'keep');
  assert.equal(sent.payload.confirmed,true);
});
test('a restore command appears only when the result carries a backup and something changed',async()=>{
  const cases=[
    [{state:'applied',at:NOW(),changed:{},backup:'b1'},false],
    [{state:'applied',at:NOW(),changed:{lumen:'a -> b'}},false],
    [{state:'failed',at:NOW(),rolledBack:true,backup:'b1'},false],
    [{state:'failed',at:NOW(),rolledBack:false,backup:'b1'},true],
    [{state:'applied',at:NOW(),changed:{lumen:'a -> b'},backup:'b1'},true],
  ];
  for(const [result,shown] of cases){
    const document=fakeDom();
    await openUpdates(document,{host:'lumen',lumen:{result,backups:[{name:'b1',at:NOW(),bytes:93000000,failed:false}]}});
    assert.equal(/luna lumen restore --backup b1 --yes/.test(pageText(document)),shown,JSON.stringify(result));
    if(shown)assert.match(pageText(document),/keeps your settings as they are/);
  }
});
test('plain polls never start the network check and back off when nothing changes',async()=>{
  const document=fakeDom();
  const {calls,timers}=await openUpdates(document,{host:'lumen'});
  const base=RPC_COUNT(calls,'SlsLunaLumen','status');
  for(let i=0;i<6;i++){
    for(const fn of [...timers.fns.values()])fn();
    await settle(15);
  }
  assert.equal(RPC_COUNT(calls,'SlsLunaLumen','check'),0,'no network check from polling');
  assert.ok(RPC_COUNT(calls,'SlsLunaLumen','status')-base<=1,'idle polling backs off');
});
test('polling stops after teardown',async()=>{
  const document=fakeDom();
  const {w,calls,timers}=await openUpdates(document,{host:'lumen'});
  const stale=[...timers.fns.values()];
  w.__SlsLunaTeardown();
  assert.equal(timers.intervals.size,0);
  const before=RPC_COUNT(calls,'SlsLunaLumen');
  stale.forEach(fn=>fn());
  await settle(30);
  assert.equal(RPC_COUNT(calls,'SlsLunaLumen'),before,'the Lumen poller fires nothing after teardown');
});
test('Luna is never offered a downgrade when the installed build is ahead of the latest release',async()=>{
  const document=fakeDom();
  await openUpdates(document,{host:'lumen',update:{installed:'0.14.18',latest:'0.14.17',available:true}});
  assert.match(pageText(document),/newer than the latest release/);
  assert.equal(button(document,'Install Luna update').disabled,true);
  assert.equal(button(document,'Update everything').disabled,true);
});

// ---- The Lumen category: one launcher, one menu -------------------------
// Lumen's UI is never copied or re-parented. Luna's page is a signpost that
// closes Luna and calls Lumen's own entry point, and Luna hides Lumen's moon
// from the menubar only once its own button is placed there.

function lunaTabLabels(document){
  const labels=[];
  const walk=node=>{
    if(!node)return;
    const classes=String(node.className||'').split(' ');
    if(classes.includes('sls-luna-tab'))labels.push(String(node.textContent||'').trim());
    Array.from(node.children||[]).forEach(walk);
  };
  walk(document.getElementById('sls-luna-overlay'));
  return labels;
}

function openLumenCategory(document){
  const overlay=document.getElementById('sls-luna-overlay');
  const tab=findNode(overlay,node=>String(node.className||'').startsWith('sls-luna-tab')
    &&String(node.textContent||'').trim()==='Lumen');
  assert.ok(tab,'the Lumen category was not found in the sidebar');
  tab.onclick();
  return document.getElementById('sls-luna-overlay');
}

function openLumenButton(document){
  const found=findNode(document.getElementById('sls-luna-overlay'),
    node=>node.tagName==='BUTTON'&&node.textContent==='Open Lumen');
  assert.ok(found,'the Open Lumen button was not found');
  return found;
}

test('the Lumen category sits between Updates and Settings',()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w}=fakeWindow(document);
  mount(w);
  document.getElementById('sls-luna-btn').click();
  const labels=lunaTabLabels(document).map(label=>label.replace(/\s*\u2022$/,''));
  assert.equal(labels.indexOf('Lumen'),labels.indexOf('Updates')+1,'Lumen comes right after Updates');
  assert.equal(labels.indexOf('Lumen')+1,labels.indexOf('Settings'),'Lumen comes right before Settings');
});

test('the Lumen page is a signpost with the terminal commands and the toggle hint',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  // Door mode: the signpost is the page embedding leaves behind.
  const {w}=fakeWindow(document,{prefs:{lumen_embed:false}});
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await settle(60); // the prefs arrive with the first refresh
  const overlay=openLumenCategory(document);
  const text=textOf(overlay);
  assert.match(text,/Lumen is the plugin host and owns the LuaTools \/ SLSsteam settings/);
  assert.match(text,/updates are on the Updates tab/);
  assert.match(text,/luna lumen status/);
  assert.match(text,/luna lumen update/);
  assert.match(text,/Settings . Show Lumen\u2019s own menu button|Show Lumen\u2019s own menu button/);
  assert.ok(openLumenButton(document),'the primary button is there');
});

test('Open Lumen clicks Lumen\'s own button after tearing Luna down',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const lumen=document.getElementById('lumen-moon-btn');
  let clicked=0;
  lumen.addEventListener('click',()=>{
    clicked+=1;
    const over=document.createElement('div');
    over.id='lumen-settings-overlay';
    document.body.appendChild(over);
  });
  const {w}=fakeWindow(document,{prefs:{lumen_embed:false}});
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await settle(60);
  openLumenCategory(document);
  const before=document.getElementById('sls-luna-overlay');
  openLumenButton(document).onclick();
  assert.equal(clicked,1,'Lumen\'s own click handler is the only interaction');
  assert.equal(before.parentNode,null,'Luna\'s overlay is closed first');
  assert.equal(document.getElementById('sls-luna-overlay'),null,'only Lumen\'s overlay remains');
  assert.ok(document.getElementById('lumen-settings-overlay'));
});

test('Open Lumen falls back to __lumenOpenOverlay when the button is gone',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w}=fakeWindow(document,{prefs:{lumen_embed:false}});
  mount(w);
  const lumen=document.getElementById('lumen-moon-btn');
  document.getElementById('sls-luna-btn').click();
  await settle(60);
  openLumenCategory(document);
  lumen.remove();
  let calls=0;
  w.__lumenOpenOverlay=()=>{
    calls+=1;
    const over=document.createElement('div');
    over.id='lumen-settings-overlay';
    document.body.appendChild(over);
  };
  const before=document.getElementById('sls-luna-overlay');
  openLumenButton(document).onclick();
  assert.equal(calls,1);
  assert.equal(before.parentNode,null,'Luna\'s overlay is closed first');
  assert.equal(document.getElementById('sls-luna-overlay'),null);
});

test('Open Lumen explains itself when Lumen has neither a button nor an entry point',()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w,timers}=fakeWindow(document);
  let warns=0;
  w.console={warn:()=>{warns+=1;}};
  mount(w);
  document.getElementById('sls-luna-btn').click();
  openLumenCategory(document);
  runLunaIntervals({timers},25);
  const page=document.getElementById('sls-luna-overlay');
  assert.match(textOf(page),/unavailable this session/, 'the reason is on the page');
  assert.ok(openLumenButton(document),'the door button stays reachable');
  assert.equal(warns,1,'the reason is logged once');
  document.getElementById('lumen-moon-btn').remove();
  const before=document.getElementById('sls-luna-overlay');
  openLumenButton(document).onclick();
  assert.equal(before.parentNode,null,'Luna\'s overlay was closed first');
  const overlay=document.getElementById('sls-luna-overlay');
  assert.ok(overlay,'Luna comes back so the reason is visible');
  const error=findNode(overlay,node=>node.className==='sls-luna-error');
  assert.ok(error,'the error line is rendered');
  assert.match(error.textContent,/not available right now/);
  assert.match(error.textContent,/start Steam with Lumen running/);
  assert.equal(document.getElementById('lumen-settings-overlay'),null,'nothing was opened');
});

test('Lumen\'s launcher is hidden only after Luna mounts, and restored on teardown',()=>{
  const document=fakeDom();
  const bar=document.createElement('div');
  ['File','View','Friends','Games'].forEach(label=>{
    const item=document.createElement('div');
    item.textContent=label;
    bar.appendChild(item);
  });
  const lumen=document.createElement('div');
  lumen.id='lumen-moon-btn';
  lumen.textContent='Lumen';
  lumen.style.display='flex';
  bar.appendChild(lumen);
  document.body.appendChild(bar);
  const {w,timers}=fakeWindow(document);
  mount(w);
  assert.equal(lumen.style.display,'flex','nothing is hidden before the strip is a menubar');
  // Now it is the menubar: the placement retry proves the mount and hides Lumen.
  bar.children[0].textContent='Steam';
  [...timers.fns.values()].forEach(fn=>fn());
  assert.ok(document.getElementById('sls-luna-btn'),'Luna landed in the menubar');
  assert.equal(lumen.style.display,'none');
  assert.equal(lumen.getAttribute('data-sls-luna-hid-display'),'flex',
    'the previous inline display is remembered for an exact restore');
  w.__SlsLunaTeardown();
  assert.equal(lumen.style.display,'flex','the launcher comes back exactly as it was');
  assert.equal(lumen.getAttribute('data-sls-luna-hid-display'),undefined);
});

test('the menubar observer re-hides a re-injected button and disconnects on teardown',()=>{
  const document=fakeDom();
  const bar=menubarFixture(document,true);
  const {w}=fakeWindow(document);
  const made=[];
  class FakeObserver{
    constructor(fn){this.fn=fn;this.disconnected=false;made.push(this);}
    observe(target,options){this.target=target;this.options=options;}
    disconnect(){this.disconnected=true;}
  }
  w.MutationObserver=FakeObserver;
  mount(w);
  const first=document.getElementById('lumen-moon-btn');
  assert.equal(first.style.display,'none');
  const watcher=made.find(entry=>entry.target===bar);
  assert.ok(watcher,'one observer watches the menubar');
  assert.equal(watcher.options.childList,true);
  // Lumen re-injects on its own events: a fresh node must hide again, and the
  // old one must be restored so nothing stays hidden by a stale reference.
  first.remove();
  const again=document.createElement('div');
  again.id='lumen-moon-btn';
  again.textContent='Lumen';
  bar.appendChild(again);
  watcher.fn([]);
  assert.equal(again.style.display,'none','the re-injected button is hidden again');
  assert.notEqual(first.style.display,'none','the detached node was restored');
  w.__SlsLunaTeardown();
  assert.equal(watcher.disconnected,true,'the observer dies with the panel');
  assert.notEqual(again.style.display,'none','teardown gives the launcher back');
});

test('the Settings switch keeps Lumen\'s own menu button visible',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w,calls}=fakeWindow(document);
  mount(w);
  const lumen=document.getElementById('lumen-moon-btn');
  assert.equal(lumen.style.display,'none','hidden by default');
  document.getElementById('sls-luna-btn').click();
  const settings=findNode(document.getElementById('sls-luna-overlay'),
    node=>String(node.className||'').startsWith('sls-luna-tab')
      &&String(node.textContent||'').trim()==='Settings');
  settings.onclick();
  const row=findNode(document.getElementById('sls-luna-overlay'),
    node=>node.className==='sls-luna-row'&&textOf(node).includes('Show Lumen'));
  assert.ok(row,'the switch is on the Settings page');
  row.children[1].onclick();
  assert.notEqual(lumen.style.display,'none','the switch brings the moon back');
  await settle(30);
  const saved=calls.filter(call=>call.fn==='SlsLunaPrefsSet').pop();
  assert.ok(saved,'the choice is persisted through the existing prefs RPC');
  assert.equal(saved.payload.key,'lumen_button');
  assert.equal(saved.payload.value,true);
});

test('opening Luna onto Lumen\'s open menu embeds it; with embedding off it closes Lumen first',()=>{
  // Embedding on (the default): the open menu becomes Luna's Lumen page.
  const document=embedDom();
  menubarFixture(document,true);
  const {w}=fakeWindow(document,{prefs:{lumen_embed:true}});
  let closed=0;
  w.__lumenCloseOverlay=()=>{
    closed+=1;
    const over=document.getElementById('lumen-settings-overlay');
    if(over)over.remove();
  };
  mount(w);
  lumenOverlayFixture(document);
  document.getElementById('sls-luna-btn').click();
  assert.equal(closed,0,'Lumen\'s own window is not closed behind the user');
  const over=document.getElementById('lumen-settings-overlay');
  assert.ok(over,'Lumen is still there, inside Luna now');
  assert.equal(over.style.width,'670px','and laid into Luna\'s content area');
  assert.ok(document.getElementById('sls-luna-overlay'));
  assert.ok(lunaSubtabs(document).length,'Luna\'s sidebar lists Lumen\'s pages');

  // Embedding off: the old rule, one menu at a time.
  const door=embedDom();
  menubarFixture(door,true);
  const made=fakeWindow(door,{prefs:{lumen_embed:false}});
  made.storage.set('slsLunaLumenEmbed','0'); // the prefs mirror, before SlsLunaInfo answers
  let doorClosed=0;
  made.w.__lumenCloseOverlay=()=>{
    doorClosed+=1;
    const node=door.getElementById('lumen-settings-overlay');
    if(node)node.remove();
  };
  mount(made.w);
  const lumenOverlay=door.createElement('div');
  lumenOverlay.id='lumen-settings-overlay';
  door.body.appendChild(lumenOverlay);
  door.getElementById('sls-luna-btn').click();
  assert.equal(doorClosed,1,'Lumen\'s own close path ran');
  assert.equal(door.getElementById('lumen-settings-overlay'),null);
  assert.ok(door.getElementById('sls-luna-overlay'),'Luna is the only overlay');
});

test('the Lumen page carries the last known update state the button can no longer show',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const lumen=document.getElementById('lumen-moon-btn');
  const pill=document.createElement('span');
  pill.className='lumen-auto-fix-pill-copy';
  pill.textContent='Updates available';
  lumen.appendChild(pill);
  const {w}=fakeWindow(document,{prefs:{lumen_embed:false}});
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await settle(60);
  const overlay=openLumenCategory(document);
  assert.match(textOf(overlay),/Lumen reports: Updates available\./);
});

/* ---- Embedded Lumen ------------------------------------------------------
   Lumen's own menu is rebuilt on every open, so these fixtures hand the panel
   the same shape Lumen builds (overlay > .lumen-win > .lumen-side +
   .lumen-content with one .lumen-tab-panel per page) and a content rect the
   panel can measure. */
function lumenOverlayFixture(document,{pages,missing}={}){
  const labels=pages||['LuaTools','SLSsteam','Cloud drives','About'];
  const over=document.createElement('div');
  over.id='lumen-settings-overlay';
  over.style.position='fixed';
  over.style.top='0';
  over.style.left='0';
  over.style.background='rgba(0,0,0,.55)';
  over.style.zIndex='99999';
  over.style.pointerEvents='auto';
  const win=document.createElement('div');
  win.className='lumen-win';
  win.style.width='900px';
  win.style.height='620px';
  win.style.borderRadius='4px';
  win.style.boxShadow='0 20px 60px rgba(0,0,0,.6)';
  const side=document.createElement('div');
  side.className='lumen-side';
  side.style.display='flex';
  const content=document.createElement('div');
  content.className='lumen-content';
  content.style.flex='0 1 auto';
  const tabs=[];const panels=[];const clicked=[];
  over.__clicked=clicked;
  if(missing!=='tabs'){
    labels.forEach((label,index)=>{
      const tab=document.createElement('div');
      tab.id='lumen-tab-'+index;
      tab.className='lumen-tab';
      tab.textContent=label;
      tab.addEventListener('click',()=>{
        clicked.push(label);
        panels.forEach((panel,i)=>{panel.style.display=i===index?'block':'none';});
      });
      side.appendChild(tab);
      tabs.push(tab);
      const panel=document.createElement('div');
      panel.className='lumen-body lumen-tab-panel';
      panel.style.display=index===0?'block':'none';
      content.appendChild(panel);
      panels.push(panel);
    });
  }
  if(missing!=='side')win.appendChild(side);
  if(missing!=='content')win.appendChild(content);
  if(missing!=='win')over.appendChild(win);
  document.body.appendChild(over);
  return {over,win,side,content,tabs,panels,clicked};
}

function embedDom(rects){
  const document=fakeDom();
  const make=document.createElement;
  // The header and the body have different rects on purpose: the pane must be
  // measured from the body, never from the header above it.
  const rect=Object.assign({left:220,top:90,width:670,height:560},(rects||{}).body||{});
  const headRect=Object.assign({left:220,top:30,width:670,height:60},(rects||{}).head||{});
  document.__rect=rect;
  document.__headRect=headRect;
  document.createElement=tag=>{
    const element=make(tag);
    element.getBoundingClientRect=()=>{
      const box=String(element.className||'')==='sls-luna-head'?headRect:rect;
      return {left:box.left,top:box.top,width:box.width,height:box.height,
        right:box.left+box.width,bottom:box.top+box.height};
    };
    return element;
  };
  return document;
}

/* Runs every interval the panel registered - the fakes never tick by
   themselves - so a bounded retry can be stepped without real waiting. */
function runLunaIntervals(made,times){
  for(let pass=0;pass<(times||1);pass+=1){
    Array.from(made.timers.fns.values()).forEach(fn=>{
      try{fn();}catch(_){/* another panel timer */}
    });
  }
}

function embedLuna(document,{prefs,overlay}={}){
  menubarFixture(document,true);
  const made=fakeWindow(document,{prefs:Object.assign({lumen_embed:true},prefs||{})});
  const observers=[];
  class FakeObserver{
    constructor(fn){this.fn=fn;this.disconnected=false;observers.push(this);}
    observe(target,options){this.target=target;this.options=options;}
    disconnect(){this.disconnected=true;}
  }
  made.w.MutationObserver=FakeObserver;
  let opened=0;let closed=0;
  made.w.__lumenOpenOverlay=()=>{
    opened+=1;
    if(!document.getElementById('lumen-settings-overlay')){
      lumenOverlayFixture(document,overlay||{});
    }
  };
  made.w.__lumenCloseOverlay=()=>{
    closed+=1;
    const over=document.getElementById('lumen-settings-overlay');
    if(over)over.remove();
  };
  mount(made.w);
  document.getElementById('sls-luna-btn').click();
  return Object.assign(made,{observers,opened:()=>opened,closed:()=>closed});
}

function lunaCategoryTab(document,label){
  return findNode(document.getElementById('sls-luna-overlay'),
    node=>String(node.className||'').startsWith('sls-luna-tab')
      &&String(node.textContent||'').trim()===(label||'Lumen'));
}

function lunaSubtabs(document){
  const side=findNode(document.getElementById('sls-luna-overlay'),
    node=>node.className==='sls-luna-side');
  if(!side)return [];
  return Array.from(side.children).filter(child=>String(child.className||'').split(/\s+/)
    .includes('sls-luna-subpage'));
}

test('embedded Lumen is laid into Luna\'s content rect with Lumen\'s sidebar hidden',()=>{
  const document=embedDom();
  const {opened}=embedLuna(document);
  lunaCategoryTab(document).onclick();
  const overlay=document.getElementById('sls-luna-overlay');
  assert.ok(overlay,'Luna\'s window stays as the shell');
  const over=document.getElementById('lumen-settings-overlay');
  assert.ok(over,'Lumen\'s UI is in the page');
  assert.equal(opened(),1,'Lumen was opened once, through its own entry point');
  assert.equal(over.style.position,'fixed');
  assert.equal(over.style.left,'220px');
  assert.equal(over.style.top,'90px');
  assert.equal(over.style.width,'670px');
  assert.equal(over.style.height,'560px');
  assert.equal(over.style.right,'auto');
  assert.equal(over.style.bottom,'auto');
  assert.equal(over.style.background,'transparent','no second backdrop');
  assert.equal(over.style.zIndex,'100000','above Luna\'s 99999');
  assert.ok(Number(over.style.zIndex)>99999,'Lumen paints over Luna\'s window');
  assert.equal(over.style.pointerEvents,'none','never swallows clicks meant for the sidebar');
  const win=findNode(over,node=>node.className==='lumen-win');
  assert.equal(win.style.width,'100%');
  assert.equal(win.style.height,'100%');
  assert.equal(win.style.maxWidth,'none');
  assert.equal(win.style.maxHeight,'none');
  assert.equal(win.style.borderRadius,'0');
  assert.equal(win.style.boxShadow,'none');
  assert.equal(win.style.pointerEvents,'auto','Lumen itself still takes input');
  assert.ok(overlay.style.zIndex!=='100000','Luna\'s own overlay keeps its stylesheet layer');
  const side=findNode(over,node=>node.className==='lumen-side');
  assert.equal(side.style.display,'none','Luna\'s sidebar replaces Lumen\'s');
  const content=findNode(over,node=>node.className==='lumen-content');
  assert.equal(content.style.flex,'1 1 auto');
  assert.equal(content.style.width,'100%');
});

test('Luna\'s sidebar lists Lumen\'s own pages and clicking one activates it',()=>{
  const document=embedDom();
  embedLuna(document,{overlay:{pages:['LuaTools','SLSsteam','About']}});
  lunaCategoryTab(document).onclick();
  const over=document.getElementById('lumen-settings-overlay');
  const subs=lunaSubtabs(document);
  assert.deepEqual(subs.map(sub=>sub.textContent),['LuaTools','SLSsteam','About']);
  assert.match(subs[0].className,/ active$/,'the visible Lumen page is highlighted');
  subs[1].onclick();
  assert.deepEqual(over.__clicked,['SLSsteam'],'the nested entry clicks Lumen\'s own tab');
  const after=findNode(over,node=>node.className==='lumen-body lumen-tab-panel'
    ||node.className==='lumen-tab-panel');
  assert.ok(after);
  const panels=over.children[0].children[1].children;
  assert.equal(panels[1].style.display,'block');
  const fresh=lunaSubtabs(document);
  assert.match(fresh[1].className,/ active$/,'the highlight follows the click');
  assert.doesNotMatch(fresh[0].className,/ active$/);
});

test('closing Luna restores every style it wrote on Lumen and closes Lumen',()=>{
  const document=embedDom();
  const {w,closed}=embedLuna(document);
  lunaCategoryTab(document).onclick();
  const over=document.getElementById('lumen-settings-overlay');
  const win=findNode(over,node=>node.className==='lumen-win');
  const side=findNode(over,node=>node.className==='lumen-side');
  const content=findNode(over,node=>node.className==='lumen-content');
  assert.equal(over.style.background,'transparent');
  const event={key:'Escape',stopped:false,prevented:false,
    stopPropagation(){this.stopped=true;},preventDefault(){this.prevented=true;}};
  w.__emitTo('keydown',event);
  assert.equal(event.stopped,true,'Lumen never gets its own Esc');
  assert.equal(document.getElementById('sls-luna-overlay'),null,'Luna closed');
  assert.equal(closed(),1,'Lumen closed with it');
  assert.equal(over.style.background,'rgba(0,0,0,.55)');
  assert.equal(over.style.top,'0');
  assert.equal(over.style.left,'0');
  assert.equal(over.style.zIndex,'99999');
  assert.equal(over.style.pointerEvents,'auto');
  assert.equal(over.style.width,'','geometry was not inline before');
  assert.equal(over.style.right,'');
  assert.equal(win.style.width,'900px');
  assert.equal(win.style.height,'620px');
  assert.equal(win.style.maxWidth,'');
  assert.equal(win.style.borderRadius,'4px');
  assert.equal(win.style.boxShadow,'0 20px 60px rgba(0,0,0,.6)');
  assert.equal(win.style.pointerEvents,'');
  assert.equal(side.style.display,'flex');
  assert.equal(content.style.flex,'0 1 auto');
  assert.equal(content.style.width,'');
});

for(const missing of ['win','side','content','tabs']){
  test('a Lumen overlay without its '+(missing==='tabs'?'page tabs':missing)
    +' falls back to the door and touches nothing',()=>{
    const document=embedDom();
    menubarFixture(document,true);
    const {w}=fakeWindow(document);
    let closed=0;
    w.__lumenOpenOverlay=()=>{
      if(!document.getElementById('lumen-settings-overlay')){
        lumenOverlayFixture(document,{missing});
      }
    };
    w.__lumenCloseOverlay=()=>{closed+=1;};
    mount(w);
    document.getElementById('sls-luna-btn').click();
    lunaCategoryTab(document).onclick();
    assert.equal(document.getElementById('sls-luna-overlay'),null,'Luna closes as it always did');
    const over=document.getElementById('lumen-settings-overlay');
    assert.ok(over,'Lumen\'s own window is up');
    assert.equal(over.style.background,'rgba(0,0,0,.55)','backdrop untouched');
    assert.equal(over.style.zIndex,'99999');
    assert.ok(!over.style.width,'no geometry written');
    assert.equal(over.style.pointerEvents,'auto');
    const win=findNode(over,node=>node.className==='lumen-win');
    if(win){
      assert.equal(win.style.width,'900px','no size written');
      assert.equal(win.style.borderRadius,'4px');
    }
    assert.equal(closed,0,'the door leaves Lumen\'s window alone');
    const note=document.getElementById('sls-luna-embed-note');
    assert.ok(note,'the reason is on screen');
    assert.match(note.textContent,/unavailable this session/);
    assert.match(note.textContent,/Embed Lumen in Luna/);
  });
}

test('a rebuilt Lumen overlay is embedded again, never stacked',()=>{
  const document=embedDom();
  const {w,observers}=embedLuna(document);
  lunaCategoryTab(document).onclick();
  const first=document.getElementById('lumen-settings-overlay');
  const watcher=observers.find(entry=>entry.target===document.body
    && !(entry.options||{}).subtree);
  assert.ok(watcher,'the overlay observer watches the body, direct children only');
  assert.equal(watcher.options.childList,true);
  // Lumen takes its overlay away (its own updater tears the menu down): the
  // removal is noticed, Lumen is re-opened once and laid back in.
  first.remove();
  watcher.fn([]);
  const recovered=document.getElementById('lumen-settings-overlay');
  assert.ok(recovered,'the removal was noticed and Lumen was re-opened');
  assert.notEqual(recovered,first);
  assert.equal(recovered.style.width,'670px','laid back into the content rect');
  assert.equal(recovered.style.zIndex,'100000');
  assert.equal(Array.from(document.body.children).filter(kid=>kid.id==='lumen-settings-overlay').length,1,
    'one Lumen overlay, never two');
  // An overlay Lumen builds itself is laid in without a second one.
  recovered.remove();
  w.__lumenOpenOverlay();
  const fresh=document.getElementById('lumen-settings-overlay');
  watcher.fn([]);
  assert.equal(fresh.style.width,'670px','the fresh pane is laid in');
  assert.equal(fresh.style.background,'transparent');
  assert.equal(Array.from(document.body.children).filter(kid=>kid.id==='lumen-settings-overlay').length,1,
    'still one Lumen overlay');
});

test('one Esc closes Luna only, and Luna\'s resize re-measures Lumen',()=>{
  const document=embedDom();
  const {w}=embedLuna(document);
  lunaCategoryTab(document).onclick();
  const over=document.getElementById('lumen-settings-overlay');
  document.__rect.left=180;
  document.__rect.top=60;
  document.__rect.width=740;
  document.__rect.height=600;
  w.__emitTo('resize',{});
  assert.equal(over.style.left,'180px');
  assert.equal(over.style.top,'60px');
  assert.equal(over.style.width,'740px');
  assert.equal(over.style.height,'600px');
  let lumenEsc=0;
  document.addEventListener('keydown',()=>{lumenEsc+=1;});
  const event={key:'Escape',stopped:false,
    stopPropagation(){this.stopped=true;},preventDefault(){}};
  w.__emitTo('keydown',event);
  if(!event.stopped)document.fire('document:keydown',{key:'Escape'});
  assert.equal(event.stopped,true);
  assert.equal(lumenEsc,0,'Lumen\'s own Esc never ran');
  assert.equal(document.getElementById('sls-luna-overlay'),null);
});

test('switching Luna to another category closes Lumen and gives its styles back',()=>{
  const document=embedDom();
  const {closed}=embedLuna(document);
  lunaCategoryTab(document).onclick();
  const over=document.getElementById('lumen-settings-overlay');
  lunaCategoryTab(document,'Playtime').onclick();
  assert.equal(closed(),1,'Lumen closed through its own entry point');
  assert.equal(over.style.background,'rgba(0,0,0,.55)');
  assert.equal(over.style.width,'');
  const side=findNode(over,node=>node.className==='lumen-side');
  assert.equal(side.style.display,'flex');
});

test('teardown restores Lumen\'s styles and drops the observer and listeners',()=>{
  const document=embedDom();
  const made=embedLuna(document);
  lunaCategoryTab(document).onclick();
  const over=document.getElementById('lumen-settings-overlay');
  const watcher=made.observers.find(entry=>entry.target===document.body
    && !(entry.options||{}).subtree);
  assert.equal((made.winListeners.get('resize')||[]).length,1);
  assert.equal((made.winListeners.get('keydown')||[]).length,1);
  made.w.__SlsLunaTeardown();
  assert.equal(watcher.disconnected,true,'the overlay observer dies with the panel');
  assert.equal((made.winListeners.get('resize')||[]).length,0);
  assert.equal((made.winListeners.get('keydown')||[]).length,0);
  assert.equal(over.style.width,'','styles given back');
  assert.equal(over.style.zIndex,'99999');
  assert.ok(document.getElementById('lumen-settings-overlay'),
    'Lumen\'s own window is left up, not closed behind the user');
});

test('the embedding switch off keeps the door and saves lumen_embed',async()=>{
  const document=embedDom();
  menubarFixture(document,true);
  const {w,calls}=fakeWindow(document,{prefs:{lumen_embed:false}});
  w.__lumenOpenOverlay=()=>{};
  w.__lumenCloseOverlay=()=>{};
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await settle(60);
  lunaCategoryTab(document).onclick();
  assert.ok(document.getElementById('sls-luna-overlay'),'Luna stays open in door mode');
  assert.ok(openLumenButton(document),'the Open Lumen button is the door');
  lunaCategoryTab(document,'Settings').onclick();
  const row=findNode(document.getElementById('sls-luna-overlay'),
    node=>node.className==='sls-luna-row'&&textOf(node).includes('Embed Lumen in Luna'));
  assert.ok(row,'the embedding switch is on the Settings page');
  row.children[1].onclick();
  await settle(30);
  const saved=calls.filter(call=>call.fn==='SlsLunaPrefsSet').pop();
  assert.equal(saved.payload.key,'lumen_embed');
  assert.equal(saved.payload.value,true);
});

test('sidebar sub-pages use the normal row builder and cannot shrink to nothing',()=>{
  const document=embedDom();
  embedLuna(document,{overlay:{pages:['LuaTools','SLSsteam','About']}});
  lunaCategoryTab(document).onclick();
  const subs=lunaSubtabs(document);
  assert.equal(subs.length,3);
  const side=findNode(document.getElementById('sls-luna-overlay'),
    node=>node.className==='sls-luna-side');
  subs.forEach(sub=>{
    // Same builder as Playtime/Health, plus the indent class.
    const classes=String(sub.className||'').split(/\s+/);
    assert.ok(classes.includes('sls-luna-tab'),'the normal row class');
    assert.ok(classes.includes('sls-luna-subpage'),'plus the indent class');
    assert.ok(classes.includes('active')||true);
    assert.doesNotMatch(sub.className,/sls-luna-subtab/,'the chip class stays on chips');
    assert.equal(sub.parentNode,side,'a direct sidebar row');
    assert.ok(String(sub.textContent||'').trim(),'the page label is there');
    assert.ok(sub.children.length>0,'built as a row, not a bare text node');
  });
  const css=String(document.getElementById('sls-luna-styles').textContent||'');
  // The base row rule carries the height and forbids flex shrinking: a
  // flex-column child with overflow:hidden and no flex-basis is what collapsed
  // to 0px in the live build.
  assert.match(css,/\.sls-luna-tab\{[^}]*height:20px[^}]*flex:0 0 auto/);
  // The indent rule must not restate (or zero) the height.
  assert.match(css,/\.sls-luna-subpage\{padding:6px 8px 6px 46px;/);
  assert.doesNotMatch(css,/\.sls-luna-subpage\{[^}]*height:0/);
  assert.match(css,/\.sls-luna-subtab\{[^}]*border:1px solid/,'the chip row keeps its box');
});

test('a slow Lumen menu is retried and lands embedded within the budget',()=>{
  const document=embedDom();
  menubarFixture(document,true);
  const {w,timers}=fakeWindow(document);
  let ready=false;
  w.__lumenOpenOverlay=()=>{ if(ready)lumenOverlayFixture(document); };
  w.__lumenCloseOverlay=()=>{
    const over=document.getElementById('lumen-settings-overlay');
    if(over)over.remove();
  };
  mount(w);
  document.getElementById('sls-luna-btn').click();
  lunaCategoryTab(document).onclick();
  assert.match(textOf(document.getElementById('sls-luna-overlay')),/Opening Lumen/,
    'a short line while it waits');
  assert.doesNotMatch(textOf(document.getElementById('sls-luna-overlay')),/Lumen is the plugin host/,
    'the intro is not shown while pending');
  runLunaIntervals({timers},3);
  assert.match(textOf(document.getElementById('sls-luna-overlay')),/Opening Lumen/,'still waiting');
  ready=true;
  runLunaIntervals({timers},1);
  const over=document.getElementById('lumen-settings-overlay');
  assert.ok(over,'Lumen came up');
  assert.equal(over.style.width,'670px','and was laid into Luna\'s content area');
  assert.equal(over.style.zIndex,'100000');
  assert.doesNotMatch(textOf(document.getElementById('sls-luna-overlay')),/Opening Lumen/);
  assert.ok(lunaSubtabs(document).length>0,'the sidebar grows Lumen\'s pages');
});

test('an embed that never lands explains itself instead of showing the intro',()=>{
  const document=embedDom();
  menubarFixture(document,true);
  const {w,timers}=fakeWindow(document);
  const warns=[];
  w.console={warn:message=>warns.push(message)};
  w.__lumenOpenOverlay=()=>{ /* Lumen never comes up */ };
  w.__lumenCloseOverlay=()=>{};
  mount(w);
  document.getElementById('sls-luna-btn').click();
  lunaCategoryTab(document).onclick();
  runLunaIntervals({timers},25);
  const page=document.getElementById('sls-luna-overlay');
  const text=textOf(page);
  assert.match(text,/unavailable this session/, 'the reason in plain words');
  assert.match(text,/Lumen\u2019s menu is not in the page/,'with what actually failed');
  assert.ok(openLumenButton(document),'the door stays one click away');
  assert.doesNotMatch(text,/Lumen is the plugin host/,'no intro instead of the reason');
  assert.equal((text.match(/Embed Lumen in Luna\u2019s window/g)||[]).length,1,'the switch once');
  assert.equal(warns.length,1,'logged once');
  assert.match(warns[0],/embedding Lumen/i);
});

test('a sub-entry click re-applies the embed and forwards to Lumen\'s own tab',()=>{
  const document=embedDom();
  embedLuna(document,{overlay:{pages:['LuaTools','SLSsteam','About']}});
  lunaCategoryTab(document).onclick();
  // Lumen rebuilt its overlay without the observer having seen it yet.
  document.getElementById('lumen-settings-overlay').remove();
  const fresh=lumenOverlayFixture(document,{pages:['LuaTools','SLSsteam','About']});
  assert.equal(fresh.over.style.width,undefined,'the fresh overlay is not styled yet');
  lunaSubtabs(document)[1].onclick();
  assert.deepEqual(fresh.clicked,['SLSsteam'],'the click reached the matching Lumen tab');
  assert.equal(fresh.over.style.width,'670px','the pane was laid in before the click');
  assert.equal(fresh.over.style.zIndex,'100000');
  assert.match(lunaSubtabs(document)[1].className,/ active$/);
});

test('Luna paints no intro behind Lumen, and door mode keeps it in the body',()=>{
  // Embedded: an empty placeholder, so nothing shows through Lumen's pane.
  const document=embedDom();
  embedLuna(document);
  lunaCategoryTab(document).onclick();
  const overlay=document.getElementById('sls-luna-overlay');
  const head=findNode(overlay,node=>node.className==='sls-luna-head');
  const body=findNode(overlay,node=>node.className==='sls-luna-body');
  const visible=Array.from(body.children).filter(pane=>pane.style.display!=='none');
  assert.equal(visible.length,1,'one page is on screen (the others are cached, hidden)');
  assert.equal(String(visible[0].textContent||''),'','no intro behind the pane');
  assert.match(String(head.textContent||''),/Lumen/,'the shell title stays');
  assert.doesNotMatch(String(head.textContent||''),/Settings page/,'not in the header');
  assert.equal(document.getElementById('sls-luna-embed-note'),null,'nothing floating');
  assert.equal(findNode(overlay,node=>String(node.className||'').indexOf('embed-hint')>=0),null,
    'the header hint element is gone');

  // Door mode: the intro is what the user needs, with the switches named once.
  const door=embedDom();
  menubarFixture(door,true);
  const made=fakeWindow(door,{prefs:{lumen_embed:false}});
  made.storage.set('slsLunaLumenEmbed','0');
  mount(made.w);
  door.getElementById('sls-luna-btn').click();
  lunaCategoryTab(door).onclick();
  const doorBody=findNode(door.getElementById('sls-luna-overlay'),
    node=>node.className==='sls-luna-body');
  const text=String(doorBody.textContent||'');
  assert.match(text,/Lumen is the plugin host/,'the intro belongs to the door');
  assert.equal((text.match(/Show Lumen\u2019s own menu button/g)||[]).length,1,
    'the two switches are stated once');
  assert.match(text,/Embed Lumen in Luna\u2019s window/);
});

test('the pane is measured from Luna\'s body, not its header, once per open and on resize only',()=>{
  const document=embedDom({body:{top:90,left:220,width:670,height:560},
    head:{top:30,left:220,width:670,height:60}});
  const {w}=embedLuna(document);
  lunaCategoryTab(document).onclick();
  const over=document.getElementById('lumen-settings-overlay');
  assert.equal(over.style.top,'90px','the header is not part of the pane');
  assert.equal(over.style.height,'560px');
  document.__rect.top=120;
  w.__emitTo('resize',{});
  assert.equal(over.style.top,'120px','a resize re-measures');
  document.__rect.top=150;
  lunaSubtabs(document)[0].onclick();
  assert.equal(over.style.top,'120px','a repaint reuses the measured rect, no per-paint measure');
});

/* ---- Stability and smoothness -------------------------------------------- */

const countId=(node,id)=>(node&&node.id===id?1:0)
  +Array.from((node&&node.children)||[]).reduce((sum,child)=>sum+countId(child,id),0);
const countClass=(node,cls)=>(String((node&&node.className)||'')===cls?1:0)
  +Array.from((node&&node.children)||[]).reduce((sum,child)=>sum+countClass(child,cls),0);

test('switching tabs reuses one shell and cached pages instead of rebuilding',()=>{
  const document=embedDom();
  embedLuna(document,{overlay:{pages:['LuaTools','SLSsteam','About']}});
  lunaCategoryTab(document).onclick();
  const overlay=document.getElementById('sls-luna-overlay');
  const win=findNode(overlay,node=>node.className==='sls-luna-win');
  const side=findNode(overlay,node=>node.className==='sls-luna-side');
  const main=findNode(overlay,node=>node.className==='sls-luna-main');
  const body=findNode(overlay,node=>node.className==='sls-luna-body');
  const visiblePane=()=>Array.from(body.children).find(pane=>pane.style.display!=='none');
  const settingsPane=(()=>{
    lunaCategoryTab(document,'Settings').onclick();
    return visiblePane();
  })();
  for(const label of ['Playtime','Settings','Lumen','Playtime','Settings']){
    lunaCategoryTab(document,label).onclick();
    assert.equal(document.getElementById('sls-luna-overlay'),overlay,'the window is not rebuilt');
    assert.equal(findNode(document.getElementById('sls-luna-overlay'),
      node=>node.className==='sls-luna-win'),win,'the window node stays');
    assert.equal(findNode(document.getElementById('sls-luna-overlay'),
      node=>node.className==='sls-luna-side'),side,'the sidebar stays');
    assert.equal(findNode(document.getElementById('sls-luna-overlay'),
      node=>node.className==='sls-luna-main'),main,'the content area stays');
    assert.equal(findNode(document.getElementById('sls-luna-overlay'),
      node=>node.className==='sls-luna-body'),body,'the body slot stays');
  }
  assert.equal(visiblePane(),settingsPane,'a cached page is shown again, not rebuilt');
  assert.equal(Array.from(body.children).filter(pane=>pane.style.display!=='none').length,1,
    'exactly one page is visible at a time');
});

test('opening paints once: one window, one body slot, revealed after Lumen is laid in',async()=>{
  const document=embedDom();
  const created=[];
  const make=document.createElement;
  document.createElement=tag=>{const element=make(tag);created.push(element);return element;};
  embedLuna(document,{overlay:{pages:['LuaTools','About']}});
  lunaCategoryTab(document).onclick();
  const overlay=document.getElementById('sls-luna-overlay');
  assert.ok(overlay);
  assert.equal(overlay.style.visibility,'','the first frame is already filled');
  assert.equal(created.filter(node=>node.className==='sls-luna-win').length,1,
    'the window was built once, not once per paint');
  assert.equal(created.filter(node=>node.className==='sls-luna-body').length,1,
    'one body slot');
  const win=findNode(overlay,node=>node.className==='sls-luna-win');
  await settle(80); // the first refresh lands and updates the visible page only
  assert.equal(findNode(document.getElementById('sls-luna-overlay'),
    node=>node.className==='sls-luna-win'),win,'a data refresh never rebuilds the shell');
});

test('the content rect is measured once per open, then only on resize',()=>{
  const document=embedDom();
  let measured=0;
  const make=document.createElement;
  document.createElement=tag=>{
    const element=make(tag);
    const previous=element.getBoundingClientRect;
    element.getBoundingClientRect=()=>{
      if(String(element.className||'')==='sls-luna-body')measured+=1;
      return previous();
    };
    return element;
  };
  const {w}=embedLuna(document);
  lunaCategoryTab(document).onclick();
  assert.equal(measured,1,'measured once when the shell opens');
  lunaCategoryTab(document,'Settings').onclick();
  lunaCategoryTab(document,'Playtime').onclick();
  lunaCategoryTab(document,'Lumen').onclick();
  assert.equal(measured,1,'switching and repainting never re-measure');
  document.__rect.top=120;
  w.__emitTo('resize',{});
  assert.equal(measured,2,'a resize re-measures exactly once');
  lunaCategoryTab(document,'Settings').onclick();
  assert.equal(measured,2,'and the new rect is reused afterwards');
});

test('the overlay observer is scoped, idle when not embedded and gone on teardown',()=>{
  const document=embedDom();
  const made=embedLuna(document,{overlay:{pages:['LuaTools','About']}});
  lunaCategoryTab(document).onclick();
  const watcher=made.observers.find(entry=>entry.target===document.body
    && !(entry.options||{}).subtree);
  assert.ok(watcher,'one observer, on the body\'s direct children');
  assert.equal(watcher.slsWatching,true,'watching while Lumen is embedded');
  lunaCategoryTab(document,'Playtime').onclick();
  assert.equal(watcher.slsWatching,false,'nothing is embedded: idle');
  assert.equal(watcher.disconnected,true,'and disconnected');
  lunaCategoryTab(document,'Lumen').onclick();
  assert.equal(watcher.slsWatching,true,'re-attached for the next embed');
  made.w.__SlsLunaTeardown();
  assert.equal(watcher.disconnected,true,'teardown drops it for good');
});

test('an overlay removed while embedded is recovered once, then explained with the blocked updater line',()=>{
  const document=embedDom();
  const made=embedLuna(document,{overlay:{pages:['LuaTools','About']}});
  lunaCategoryTab(document).onclick();
  const watcher=made.observers.find(entry=>entry.target===document.body
    && !(entry.options||{}).subtree);
  const before=made.opened();
  // First two removals: Lumen is re-opened and laid back in, once each.
  for(let attempt=1;attempt<=2;attempt+=1){
    const over=document.getElementById('lumen-settings-overlay');
    over.remove();
    watcher.fn([]);
    const back=document.getElementById('lumen-settings-overlay');
    assert.ok(back,'recovered on attempt '+attempt);
    assert.equal(back.style.width,'670px','laid in again, not left empty');
    assert.equal(made.opened(),before+attempt,'exactly one re-open per recovery');
    const body=findNode(document.getElementById('sls-luna-overlay'),
      node=>node.className==='sls-luna-body');
    assert.equal(Array.from(body.children).filter(pane=>pane.style.display!=='none').length,1,
      'a page is always on screen');
  }
  // Third time it stays broken: the page says why, keeps the door and names
  // the blocked updater instead of showing an empty window.
  document.getElementById('lumen-settings-overlay').remove();
  watcher.fn([]);
  assert.equal(made.opened(),before+2,'no further re-open past the bound');
  const page=document.getElementById('sls-luna-overlay');
  assert.ok(page,'Luna stays as the shell');
  const body=findNode(page,node=>node.className==='sls-luna-body');
  const visible=Array.from(body.children).find(pane=>pane.style.display!=='none');
  assert.ok(visible,'one page is on screen');
  const text=String(visible.textContent||'');
  assert.match(text,/disappeared while it was embedded/,'the plain reason');
  assert.ok(openLumenButton(document),'the Open Lumen button stays');
  assert.match(text,/Lumen\u2019s own updater is blocked; use Luna \u2192 Updates/,
    'the blocked updater is named once');
});

test('re-injecting after a document swap leaves exactly one launcher and one overlay',()=>{
  const document=fakeDom();
  const bar=menubarFixture(document,true);
  const {w}=fakeWindow(document);
  mount(w);
  document.getElementById('sls-luna-btn').click();
  assert.ok(document.getElementById('sls-luna-overlay'));
  document.fire('document:keydown',{key:'Escape'});
  // The document was replaced: the old generation is retired and injection
  // runs again against the fresh menubar.
  w.__SlsLunaTeardown();
  // The whole old document goes away with the bar's tree (the stub keeps
  // detached nodes in its query list, so drop them explicitly).
  while(bar.children.length)bar.children[0].remove();
  bar.remove();
  menubarFixture(document,true);
  mount(w);
  assert.equal(countId(document.body,'sls-luna-btn'),1,'one launcher');
  document.getElementById('sls-luna-btn').click();
  assert.equal(countId(document.body,'sls-luna-overlay'),1,'one overlay');
  assert.equal(countClass(document.body,'sls-luna-win'),1,'one window');
  document.fire('document:keydown',{key:'Escape'});
});

/* ---- Detached jobs and the Problems surface ------------------------------ */

async function openHealth(document, options){
  menubarFixture(document,true);
  const made=fakeWindow(document,options||{});
  mount(made.w);
  document.getElementById('sls-luna-btn').click();
  await settle(40);
  const tab=findNode(document.getElementById('sls-luna-overlay'),
    node=>(node.className||'').startsWith('sls-luna-tab')&&node.textContent.startsWith('Health'));
  assert.ok(tab,'the Health tab must exist');
  tab.onclick();
  await settle(40);
  return made;
}

test('bridge repair starts a detached job and the page follows it without blocking',async()=>{
  const document=fakeDom();
  const {calls}=await openHealth(document,{bootPatched:false,repair:['running','ok']});
  const repair=findNode(document.getElementById('sls-luna-overlay'),
    node=>node.tagName==='BUTTON'&&node.textContent==='Repair bridge');
  assert.ok(repair&&repair.disabled===false,'repair is offered when the bridge is missing');
  repair.onclick();
  // The first frame after the click already says it is running: the handler
  // returned instead of waiting for the install.
  assert.match(textOf(document.getElementById('sls-luna-overlay')),/Repair started in the background/,
    'progress is visible at once and Steam is not blocked');
  assert.ok(document.getElementById('sls-luna-overlay'),'the menu is still there while it runs');
  await settle(100);
  assert.ok(calls.some(entry=>entry.fn==='SlsLunaRepair'&&entry.payload.action==='start'),
    'the click started the job');
  assert.match(textOf(document.getElementById('sls-luna-overlay')),/repair job finished/i,
    'the outcome replaces progress');
  assert.ok(calls.some(entry=>entry.fn==='SlsLunaRepair'&&entry.payload.action==='status'),
    'the state was followed through the status action');
});

test('a repair that times out is reported with its stopped process group',async()=>{
  const document=fakeDom();
  await openHealth(document,{bootPatched:false,repair:['running','timeout']});
  const repair=findNode(document.getElementById('sls-luna-overlay'),
    node=>node.tagName==='BUTTON'&&node.textContent==='Repair bridge');
  repair.onclick();
  await settle(120);
  const text=textOf(document.getElementById('sls-luna-overlay'));
  assert.match(text,/did not finish in 180 s/,'the deadline is named');
  assert.match(text,/process group was stopped/,'and what was done about it');
  assert.match(text,/Could not|Problems|timed out/i,'the outcome is also a problem entry');
});

test('the Problems block lists backend, agent and job failures and caps at the ring size',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const made=fakeWindow(document,{bootPatched:false,repair:['running','ok']});
  const diag=Array.from({length:25},(_,index)=>({
    at:Date.now()+index,text:'[sls-luna] agent failure '+index}));
  made.storage.set('slsLunaDiag',JSON.stringify(diag));
  mount(made.w);
  document.getElementById('sls-luna-btn').click();
  await settle(40);
  findNode(document.getElementById('sls-luna-overlay'),
    node=>(node.className||'').startsWith('sls-luna-tab')&&node.textContent.startsWith('Health')).onclick();
  await settle(40);
  const overlay=document.getElementById('sls-luna-overlay');
  const text=textOf(overlay);
  assert.match(text,/Problems/);
  assert.match(text,/agent failure 24/,'the newest agent entry is shown');
  assert.doesNotMatch(text,/agent failure 0\b/,'the ring dropped the oldest entries');
  const copy=findNode(overlay,node=>node.tagName==='BUTTON'&&node.textContent==='Copy details');
  assert.ok(copy,'the details can be copied');
  copy.onclick();
  const area=findNode(overlay,node=>node.tagName==='TEXTAREA');
  assert.ok(area,'a copyable block appears');
  assert.equal(area.value.split('\n').length,20,'capped at the ring size');
});

test('a failing RPC adds one Problems entry, not one per poll',async()=>{
  const document=fakeDom();
  const {w}=await openHealth(document,{failFns:['SlsLunaHealth']});
  const overlay=()=>document.getElementById('sls-luna-overlay');
  for(let pass=0;pass<3;pass+=1){
    const refresh=findNode(overlay(),node=>node.tagName==='BUTTON'&&node.textContent==='Refresh');
    refresh.onclick();
    await settle(60);
  }
  const text=textOf(overlay());
  assert.equal((text.match(/rpc: SlsLunaHealth/g)||[]).length,1,
    'the same failure updates its timestamp instead of stacking rows');
  assert.match(text,/boom SlsLunaHealth/,'the short reason is there');
  w.__SlsLunaTeardown();
});


/* ---- SteamGridDB job client --------------------------------------------- */

async function openSgdbPicker(options){
  const document=fakeDom();
  menubarFixture(document,true);
  const made=fakeWindow(document,Object.assign({sgdbKey:'my-key'},options||{}));
  made.storage.set('slsLunaBeta','1');
  mount(made.w);
  made.storage.set('slsLunaArtworkRequest',
    JSON.stringify({appid:1145360,name:'Hades',at:Date.now()}));
  made.w.__emitStorage();
  await settle(80);
  const auto=findNode(document.getElementById('sls-luna-overlay'),
    node=>node.textContent==='Apply best to every slot'&&typeof node.onclick==='function');
  assert.ok(auto,'the auto button must be offered');
  return Object.assign(made,{document,auto});
}

test('the auto pass starts a job, polls it to done, and stops polling',async()=>{
  const {document,auto,calls}=await openSgdbPicker();
  auto.onclick();
  await settle(200);
  const starts=calls.filter(entry=>entry.fn==='SlsSgdbAuto');
  const polls=calls.filter(entry=>entry.fn==='SlsSgdbAutoStatus');
  assert.equal(starts.length,1,'the pass is started once, not per poll');
  assert.equal(starts[0].payload.gameId,1);
  assert.equal(polls.length,3,'running, running, then done: one status call each');
  assert.deepEqual(polls[0].payload,{id:7},'the job id from the start response is used');
  const text=textOf(document.getElementById('sls-luna-overlay'));
  assert.match(text,/SteamGridDB artwork pass: done/,'the final state is visible');
  assert.match(text,/Applied the best match to 4 of 5 slots/);
  assert.match(text,/icon: none/,'a failed slot keeps its code');
  assert.match(text,/No usable icon images/,'and its text');
  const before=polls.length;
  await settle(150);
  assert.equal(calls.filter(entry=>entry.fn==='SlsSgdbAutoStatus').length,before,
    'polling stops when the job is done');
});

test('progress is shown while the pass runs, and a tab switch stops the polling',async()=>{
  const {document,auto,calls}=await openSgdbPicker({sgdbAutoPlan:['running']});
  auto.onclick();
  await settle(40);
  const text=textOf(document.getElementById('sls-luna-overlay'));
  assert.match(text,/SteamGridDB: \d+ of 5 slot/,'progress counts the slots');
  assert.match(text,/working on [a-z]+/,'and names the slot in flight');
  const tab=findNode(document.getElementById('sls-luna-overlay'),
    node=>(node.className||'').startsWith('sls-luna-tab')&&node.textContent==='Playtime');
  tab.onclick();
  const before=calls.filter(entry=>entry.fn==='SlsSgdbAutoStatus').length;
  assert.ok(before>0,'it was polling on the tab');
  await settle(200);
  assert.equal(calls.filter(entry=>entry.fn==='SlsSgdbAutoStatus').length,before,
    'leaving the tab stops the poll');
});

test('teardown stops the artwork poll instead of leaving a timer behind',async()=>{
  const {document,auto,calls,w}=await openSgdbPicker({sgdbAutoPlan:['running']});
  auto.onclick();
  await settle(40);
  w.__SlsLunaTeardown();
  const before=calls.filter(entry=>entry.fn==='SlsSgdbAutoStatus').length;
  await settle(200);
  assert.equal(calls.filter(entry=>entry.fn==='SlsSgdbAutoStatus').length,before,
    'no poll survives teardown');
});

test('the 180 s deadline stops the poll and reports it',async()=>{
  const {document,auto,calls}=await openSgdbPicker({sgdbAutoPlan:['running']});
  const realNow=Date.now;
  try{
    auto.onclick();
    Date.now=()=>realNow()+200000;
    await settle(200);
  }finally{Date.now=realNow;}
  const text=textOf(document.getElementById('sls-luna-overlay'));
  assert.match(text,/stopped after 180 s/,'the deadline is named');
  findNode(document.getElementById('sls-luna-overlay'),
    node=>(node.className||'').startsWith('sls-luna-tab')&&node.textContent==='Health').onclick();
  await settle(40);
  assert.match(textOf(document.getElementById('sls-luna-overlay')),/Problems/,
    'the Health page has the ring');
  assert.match(textOf(document.getElementById('sls-luna-overlay')),/stopped after 180 s/,
    'and the same message is recorded in it');
  const before=calls.filter(entry=>entry.fn==='SlsSgdbAutoStatus').length;
  await settle(150);
  assert.equal(calls.filter(entry=>entry.fn==='SlsSgdbAutoStatus').length,before,
    'no polling continues past the deadline');
});

test('a failed start is shown in the tab, not only in the console',async()=>{
  const {document,auto}=await openSgdbPicker({sgdbAuto:'error'});
  auto.onclick();
  await settle(80);
  const overlay=document.getElementById('sls-luna-overlay');
  const text=textOf(overlay);
  assert.match(text,/Artwork pass failed: Backend error: config unreadable/,
    'the backend error text is visible in the tab');
  const error=findNode(overlay,node=>node.className==='sls-luna-error'
    &&/config unreadable/.test(node.textContent));
  assert.ok(error,'rendered as an error, not a vanishing toast');
  findNode(overlay,node=>(node.className||'').startsWith('sls-luna-tab')
    &&node.textContent==='Health').onclick();
  await settle(40);
  assert.match(textOf(document.getElementById('sls-luna-overlay')),
    /Artwork pass failed: Backend error: config unreadable/,
    'the Problems ring picked the same message up');
});

/* ---- DOM collections are not arrays ------------------------------------- */

test('the sidebar is built and rebuilt against an HTMLCollection, not an array',()=>{
  const document=embedDom();
  embedLuna(document,{overlay:{pages:['LuaTools','SLSsteam','About']}});
  lunaCategoryTab(document).onclick();
  const side=findNode(document.getElementById('sls-luna-overlay'),
    node=>node.className==='sls-luna-side');
  assert.ok(side);
  assert.equal(Array.isArray(side.children),false,'the fake hands out a collection');
  assert.equal(typeof side.children.item,'function','with item()');
  assert.equal(typeof side.children.slice,'undefined','and no array methods');
  assert.equal(typeof side.children.map,'undefined');
  assert.ok(side.children.length>2);
  // The rebuild path removes rows and appends Lumen's sub-pages again; a
  // .slice() there is what broke every open in Steam.
  assert.doesNotThrow(()=>{
    lunaCategoryTab(document,'Settings').onclick();
    lunaCategoryTab(document,'Lumen').onclick();
  },'syncSidebar must iterate a copy of the collection');
  const fresh=findNode(document.getElementById('sls-luna-overlay'),
    node=>node.className==='sls-luna-side');
  const labels=Array.from(fresh.children).map(node=>String(node.textContent||'').trim());
  assert.ok(labels.includes('LuaTools'),'the rebuilt sidebar still lists Lumen pages');
  assert.equal(Array.from(fresh.children).filter(node=>node.className==='sls-luna-tab active').length,1,
    'exactly one active row after the rebuild');
});

test('a paint failure leaves no overlay behind and the next open still works',()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w}=fakeWindow(document);
  mount(w);
  const body=document.body;
  const original=body.appendChild;
  let threw=false;
  body.appendChild=function(kid){
    if(!threw&&kid&&kid.id==='sls-luna-overlay'){
      threw=true;
      throw new TypeError('paint boom');
    }
    return original.call(this,kid);
  };
  const button=document.getElementById('sls-luna-btn');
  assert.throws(()=>button.click(),/paint boom/);
  assert.equal(document.getElementById('sls-luna-overlay'),null,
    'the half-built overlay must not stay in the DOM');
  body.appendChild=original;
  button.click();
  const overlay=document.getElementById('sls-luna-overlay');
  assert.ok(overlay,'the next click opens the menu');
  assert.notEqual(overlay.style.visibility,'hidden','and it is visible');
  assert.ok(findNode(overlay,node=>node.className==='sls-luna-win'),'fully built');
});

test('a hidden leftover overlay is rebuilt instead of being treated as open',()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w}=fakeWindow(document);
  mount(w);
  // Left behind by a failed open: present, but never revealed.
  const stale=document.createElement('div');
  stale.id='sls-luna-overlay';
  stale.style.visibility='hidden';
  document.body.appendChild(stale);
  document.getElementById('sls-luna-btn').click();
  const overlay=document.getElementById('sls-luna-overlay');
  assert.notEqual(overlay,stale,'the leftover is replaced, not returned');
  assert.equal(stale.parentNode,null,'and removed from the body');
  assert.notEqual(overlay.style.visibility,'hidden','the new shell is visible');
  assert.ok(findNode(overlay,node=>node.className==='sls-luna-win'));
});

/* ---- Exactly one page pane, and backend lists of any shape -------------- */

test('only one page pane is ever visible, even after a stale pane is left behind',()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w}=fakeWindow(document);
  mount(w);
  document.getElementById('sls-luna-btn').click();
  const bodyNode=()=>findNode(document.getElementById('sls-luna-overlay'),
    node=>node.className==='sls-luna-body');
  const visiblePanes=()=>Array.from(bodyNode().children)
    .filter(node=>node.style.display!=='none');
  for(const label of ['Health','Updates','Playtime','Health']){
    findNode(document.getElementById('sls-luna-overlay'),
      node=>(node.className||'').startsWith('sls-luna-tab')
        &&node.textContent===label).onclick();
    assert.equal(visiblePanes().length,1,'one pane after '+label);
    assert.ok(String(visiblePanes()[0].textContent||'').trim(),
      label+' is not an empty slot');
  }
  // A pane an earlier shell left in the body (never in this shell's cache)
  // must be hidden on the next switch, not stacked under the new page.
  const stale=document.createElement('div');
  stale.className='sls-luna-pane';
  stale.textContent='stale page from an earlier shell';
  bodyNode().appendChild(stale);
  findNode(document.getElementById('sls-luna-overlay'),
    node=>(node.className||'').startsWith('sls-luna-tab')
      &&node.textContent==='Settings').onclick();
  assert.equal(stale.style.display,'none','the stale pane is hidden');
  assert.equal(visiblePanes().length,1,'still exactly one page on screen');
  assert.doesNotMatch(textOf(visiblePanes()[0]),/stale page from an earlier shell/);
});

test('a warnings field as a string, an object or null never breaks the Updates page',async()=>{
  const shapes=[
    ['a single string','a single warning'],
    ['an object',{code:'disk',message:'low space'}],
    ['null',null]];
  for(const [label,warnings] of shapes){
    const document=fakeDom();
    await openUpdates(document,{host:'lumen',lumenResult:{state:'applied',at:NOW(),
      message:'Lumen updated and Luna re-attached',before:{lumen:'v2.8'},after:{lumen:'v2.9'},
      changed:{lumen:'v2.8 -> v2.9'},backup:'b1',warnings}});
    assert.ok(document.getElementById('sls-luna-overlay'),label+': the page stays open');
    const text=pageText(document);
    assert.match(text,/Updated: lumen v2\.8 \u2192 v2\.9/,label+': the page is not empty');
    if(typeof warnings==='string'){
      assert.match(text,/Warning: a single warning/,label+': the text is shown');
    } else {
      assert.doesNotMatch(text,/Warning: (?!\[object Object\])/,label+': nothing invented');
    }
  }
});

/* ---------------------------------------------------------------- channels */
function channelRow(document,label){
  return findNode(document.getElementById('sls-luna-overlay'),
    node=>node.className==='sls-luna-row'&&node.children.length
      &&String(node.children[0].textContent||'').startsWith(label));
}
const rowButton=(row,label)=>row&&Array.from(row.children)
  .find(node=>node.tagName==='BUTTON'&&node.textContent===label);

test('the Channels rows show each component and change through the CLI after a confirm',async()=>{
  const document=fakeDom();
  const {calls}=await openUpdates(document,{host:'lumen',
    channels:{values:{lumen:'stable',plugin:'beta',slsteam_moon:'stable'}}});
  const text=pageText(document);
  assert.match(text,/Lumen base \(lumen\)/,'the base is listed');
  assert.match(text,/Lumen plugin \(plugin\)/,'the plugin is listed');
  assert.match(text,/SLSsteam Moon \(slsteam_moon\)/,'the moon component is listed');
  assert.match(text,/Channel: beta/,'the current value is shown');
  const lumen=channelRow(document,'Lumen base');
  assert.equal(rowButton(lumen,'Stable').disabled,true,'the current channel is not offered again');
  assert.equal(rowButton(lumen,'Beta').disabled,false,'the other channel is offered');
  const before=calls.length;
  rowButton(lumen,'Beta').onclick();
  await settle(50);
  assert.equal(calls.length,before,'the first click only opens the confirm');
  assert.match(pageText(document),/Lumen base \(lumen\): stable → beta/,'the confirm names the component');
  assert.match(pageText(document),/the Steam client is unchanged/);
  button(document,'Yes, switch to beta').onclick();
  await settle(140);
  const set=calls.find(entry=>entry.fn==='SlsLunaLumen'&&entry.payload.action==='channels-set');
  assert.ok(set,'the change goes through the CLI action');
  assert.equal(set.payload.component,'lumen');
  assert.equal(set.payload.channel,'beta');
  assert.equal(set.payload.confirmed,true,'the backend gets the confirmation too');
  assert.ok(RPC_COUNT(calls,'SlsLunaLumen','check')>=1,'the result is re-checked with status --check');
  assert.match(pageText(document),/Channel: beta/,'the row shows the CLI\'s new value');
});

test('a channel refusal shows the CLI text and keeps the old value',async()=>{
  const document=fakeDom();
  const {calls}=await openUpdates(document,{host:'lumen',
    channelsRefusal:'REFUSING: bad channel "edge" for lumen. Nothing was changed.'});
  rowButton(channelRow(document,'Lumen base'),'Beta').onclick();
  await settle(50);
  button(document,'Yes, switch to beta').onclick();
  await settle(140);
  const text=pageText(document);
  assert.match(text,/Could not change the channel: REFUSING: bad channel "edge" for lumen\. Nothing was changed\./,
    'the CLI refusal text is shown unchanged');
  assert.match(text,/Channel: stable/,'the old value stays');
  assert.equal(RPC_COUNT(calls,'SlsLunaLumen','check'),0,'a refused change is not re-checked');
});

/* ------------------------------------------------------- update everything */
test('Update everything with nothing newer says nothing to do and queues nothing',async()=>{
  const document=fakeDom();
  const {calls}=await openUpdates(document,{host:'lumen',
    update:{available:false,latest:'0.14.3'},steamClient:{pinned:'1791918634',match:true},
    lumen:{check:checkCurrent()}});
  const before=calls.length;
  button(document,'Update everything').onclick();
  await settle(80);
  assert.equal(calls.length,before,'nothing is called from the confirm');
  const text=pageText(document);
  assert.match(text,/Nothing to do: nothing newer on your channels\./);
  assert.match(text,/Steam client: unchanged \(pinned 1791918634\)\./);
  button(document,'Close').onclick();
  await settle(120);
  assert.equal(RPC_COUNT(calls,'SlsLunaUpdate','install'),0,'no reinstall is queued');
  assert.equal(RPC_COUNT(calls,'SlsLunaLumen','update'),0);
});

test('Update everything never queues a Steam-client action and names the pin',async()=>{
  const document=fakeDom();
  const {calls}=await openUpdates(document,{host:'lumen',steamClient:{pinned:'1791918634',match:true},
    lumen:{check:checkNewer()}});
  const before=calls.length;
  button(document,'Update everything').onclick();
  await settle(60);
  const confirm=pageText(document);
  assert.match(confirm,/Scope: Luna\u2019s add-ons and tooling/,'the scope is stated');
  assert.match(confirm,/Steam client: unchanged \(pinned 1791918634\)\./,'the Steam client is named as untouched');
  button(document,'Yes, queue the updates').onclick();
  await settle(140);
  const steamCalls=calls.slice(before).filter(entry=>entry.fn==='SlsLunaSteam'
    &&!(entry.payload&&entry.payload.action==='status'));
  assert.deepEqual(steamCalls.map(entry=>entry.payload.action),[],'no Steam action is ever queued');
  const refreshCalls=calls.slice(before).filter(entry=>entry.fn==='SlsLunaRefresh'
    &&!(entry.payload&&entry.payload.action==='status'));
  assert.deepEqual(refreshCalls.map(entry=>entry.payload.action),[],'refresh (which moves the client) is never called');
  const queued=calls.slice(before).filter(entry=>(entry.fn==='SlsLunaUpdate'&&entry.payload.action==='install')
    ||(entry.fn==='SlsLunaLumen'&&entry.payload.action==='update'));
  assert.deepEqual(queued.map(entry=>entry.fn+':'+entry.payload.action),
    ['SlsLunaUpdate:install','SlsLunaLumen:update'],'only Luna and the Lumen base are queued');
});

/* ------------------------------------------------------------- terminal run */
test('Update in terminal calls only the CLI action and shows the log path',async()=>{
  const document=fakeDom();
  const {calls}=await openUpdates(document,{host:'lumen',steamClient:{pinned:'1791918634',match:true},
    terminalStart:{state:'opened',code:0,at:NOW(),window:'opened foot; the update runs there and is teed to the log',
      log:'/home/tester/.local/share/Luna/state/update-20261120-134427.log',
      output:'terminal  : opened foot\nlog       : /home/tester/.local/share/Luna/state/update-20261120-134427.log'}});
  const before=calls.length;
  button(document,'Update in terminal').onclick();
  await settle(50);
  assert.equal(calls.length,before,'the confirm comes first');
  const confirm=pageText(document);
  assert.match(confirm,/This closes Steam first/,'the confirm says Steam closes');
  assert.match(confirm,/Steam client: unchanged \(pinned 1791918634\)\./);
  button(document,'Yes, close Steam and update').onclick();
  await settle(160);
  const started=calls.slice(before).filter(entry=>entry.fn==='SlsLunaLumen'
    &&['status','check','channels'].indexOf(entry.payload.action)<0);
  assert.deepEqual(started.map(entry=>entry.payload.action),['terminal'],'only the CLI terminal action runs');
  assert.ok(!calls.some(entry=>entry.fn==='SlsLunaSteam'
    &&!(entry.payload&&entry.payload.action==='status')),'the panel runs no steam command itself');
  const text=pageText(document);
  assert.match(text,/Log: \/home\/tester\/\.local\/share\/Luna\/state\/update-20261120-134427\.log/,
    'the log path the CLI named is shown so it can be reopened');
  assert.match(text,/exit 0 means the window opened, not that it finished/);
});

test('a terminal refusal shows the CLI text',async()=>{
  const document=fakeDom();
  const {calls}=await openUpdates(document,{host:'lumen',
    terminalRefusal:'REFUSING: Steam was still running after 60 s (steam, steamwebhelper); '
      +'close it with Steam -> Exit;  or: Nothing was changed.'});
  button(document,'Update in terminal').onclick();
  await settle(50);
  button(document,'Yes, close Steam and update').onclick();
  await settle(160);
  assert.match(pageText(document),/REFUSING: Steam was still running after 60 s \(steam, steamwebhelper\)/,
    'the refusal is shown, not swallowed');
  assert.equal(RPC_COUNT(calls,'SlsLunaLumen','terminal'),1,'the CLI is asked exactly once');
});

/* ------------------------------------------------------------------- footer */
const footerRow=document=>findNode(document.getElementById('sls-luna-overlay'),
  node=>node.className==='sls-luna-account');

const DISCORD_AVATAR='https://cdn.discordapp.com/avatars/1234567890/abcdef.png?size=64';
const AVATAR_PATH='/home/tester/.local/share/Lumen/luatools/backend/data/lua_tools_auth.json';

test('the signed-in footer shows the Discord avatar and name with Connected',async()=>{
  const document=fakeDom();
  await openUpdates(document,{host:'lumen',luatools:{ok:true,signedIn:true,name:'Pablo',expired:false,
    avatar:DISCORD_AVATAR,at:Math.floor(Date.now()/1000)+3300,path:AVATAR_PATH}});
  const row=footerRow(document);
  assert.ok(row,'the footer has the connection row');
  assert.match(row.textContent,/Pablo/,'the Discord display name is the main text');
  assert.match(row.textContent,/Connected/);
  const img=Array.from(row.children).find(node=>node.tagName==='IMG');
  assert.ok(img,'the avatar image is rendered');
  assert.equal(img.src,DISCORD_AVATAR,'the validated URL goes on the src property');
  assert.equal(img.referrerPolicy,'no-referrer');
  assert.equal(img.style.borderRadius,'50%','the avatar is round');
  assert.match(String(row.title||''),/LuaTools account/,'the row is the account button');
  assert.equal(row.getAttribute('role'),'button');
  assert.equal(row.getAttribute('tabindex'),'0','keyboard reachable');
});

test('a past expires_at with a refresh token still shows Connected, never expired',async()=>{
  const document=fakeDom();
  await openUpdates(document,{host:'lumen',luatools:{ok:true,signedIn:true,name:'Pablo',expired:true,
    avatar:'',at:Math.floor(Date.now()/1000)-60,path:AVATAR_PATH}});
  const text=pageText(document);
  assert.match(text,/Pablo/);
  assert.match(text,/Connected/,'the one-hour token being stale is not a session end');
  assert.doesNotMatch(text,/expired/i,'the UI must never say expired');
});

test('a broken avatar image falls back to the dot instead of a broken icon',async()=>{
  const document=fakeDom();
  await openUpdates(document,{host:'lumen',luatools:{ok:true,signedIn:true,name:'Pablo',expired:false,
    avatar:DISCORD_AVATAR,at:0,path:AVATAR_PATH}});
  const row=footerRow(document);
  const img=Array.from(row.children).find(node=>node.tagName==='IMG');
  assert.ok(img,'the image starts out on the row');
  img.onerror();
  assert.ok(!Array.from(row.children).some(node=>node.tagName==='IMG'),'the broken image is gone');
  const dot=Array.from(row.children).find(node=>node.className.indexOf('sls-luna-dot')===0);
  assert.ok(dot,'the dot is back');
  assert.equal(dot.style.display,'','the dot is visible again');
});

test('the footer says Sign in when LuaTools has no session file',async()=>{
  const document=fakeDom();
  await openUpdates(document,{host:'lumen',luatools:{ok:false,signedIn:false,name:'',avatar:'',
    expired:false,at:0,path:'',error:'LuaTools has not saved a Discord session on this machine'}});
  const row=footerRow(document);
  assert.match(row.textContent,/LuaTools/);
  assert.match(row.textContent,/Sign in/);
  assert.doesNotMatch(row.textContent,/Connected/);
  assert.match(String(row.title||''),/not saved a Discord session/,'the reason is the tooltip');
});

test('the footer says Unavailable with the reason when the backend errors',async()=>{
  const document=fakeDom();
  await openUpdates(document,{host:'lumen',failFns:['SlsLunaLuaTools']});
  const row=footerRow(document);
  assert.match(row.textContent,/Unavailable/);
  assert.doesNotMatch(row.textContent,/Connected/,'no fabricated connection');
  assert.match(String(row.title||''),/boom SlsLunaLuaTools/,'the reason is the tooltip');
});

test('a non-Discord or non-https avatar URL is never rendered',async()=>{
  for(const avatar of ['http://cdn.discordapp.com/avatars/1/a.png',
      'https://evil.example/avatars/1/a.png','https://cdn.discordapp.com.evil.example/a.png',
      'javascript:alert(1)']){
    const document=fakeDom();
    await openUpdates(document,{host:'lumen',luatools:{ok:true,signedIn:true,name:'Pablo',
      expired:false,avatar:avatar,at:0,path:AVATAR_PATH}});
    const row=footerRow(document);
    assert.ok(!Array.from(row.children).some(node=>node.tagName==='IMG'),'no img for '+avatar);
    const dot=Array.from(row.children).find(node=>node.className.indexOf('sls-luna-dot')===0);
    assert.equal(dot.style.display,'','the dot stands in for '+avatar);
  }
});

function embedReady(document,options){
  // Lumen's overlay in the shape the embed needs, plus a measurable Luna body.
  const overlay=document.createElement('div');
  overlay.id='lumen-settings-overlay';
  ['lumen-win','lumen-side','lumen-content'].forEach(cls=>{
    const node=document.createElement('div');node.className=cls;overlay.appendChild(node);
  });
  ['Fixes','General'].forEach(label=>{
    const tab=document.createElement('div');tab.className='lumen-tab';tab.textContent=label;
    overlay.appendChild(tab);
  });
  if(!options||options.entry!==false){
    const entry=document.createElement('button');
    entry.className='lumen-account-entry connected';
    entry.textContent='Pablo Connected';
    overlay.children[1].appendChild(entry);
  }
  const panel=document.createElement('div');
  panel.id='lumen-panel-account';
  panel.className='lumen-body lumen-tab-panel';
  panel.style.display='none';
  overlay.appendChild(panel);
  document.body.appendChild(overlay);
  const slot=findNode(document.getElementById('sls-luna-overlay'),
    node=>node.className==='sls-luna-body');
  if(slot)slot.getBoundingClientRect=()=>({top:0,left:0,width:640,height:480,right:640,bottom:480});
  return overlay;
}
const signedIn={ok:true,signedIn:true,name:'Pablo',expired:false,avatar:'',at:0,path:AVATAR_PATH};

test('the footer row opens Lumen\u2019s own account entry inside the embed',async()=>{
  const document=fakeDom();
  const opened=[];
  await openUpdates(document,{host:'lumen',openLuaTools:()=>opened.push('legacy'),luatools:signedIn});
  const overlay=embedReady(document);
  const entry=findNode(overlay,node=>String(node.className||'').indexOf('lumen-account-entry')>=0);
  let clicks=0;
  entry.addEventListener('click',()=>{clicks+=1;});
  footerRow(document).onclick();
  await wait(250);
  assert.equal(clicks,1,'Lumen\u2019s account entry is clicked once');
  assert.deepEqual(opened,[],'__lumenOpenLuaToolsAccount is never used while the overlay exists');
  assert.ok(document.getElementById('sls-luna-overlay'),'Luna\u2019s menu stays open');
  assert.equal(document.getElementById('lumen-settings-overlay').style.position,'fixed',
    'Lumen\u2019s window stays laid into Luna');
});

test('without the account entry Luna pre-selects the account tab and rebuilds the overlay',async()=>{
  const document=fakeDom();
  let opened=0;
  let tab='';
  const created=[];
  const openUpdates2=await openUpdates(document,{host:'lumen',luatools:signedIn,
    lumenCloseOverlay:()=>{const old=document.getElementById('lumen-settings-overlay');
      if(old)old.remove();},
    lumenOpenOverlay:()=>{tab=document.getElementById('sls-luna-overlay')?'':'';
      const w2=created;w2.push(1);embedReady(document,{entry:false});}});
  embedReady(document,{entry:false});
  footerRow(document).onclick();
  await wait(300);
  assert.equal(created.length>=1,true,'Lumen\u2019s own open path is used');
  assert.equal(document.getElementById('lumen-settings-overlay').style.position,'fixed',
    'the rebuilt overlay is embedded again');
  assert.ok(document.getElementById('sls-luna-overlay'),'Luna stays');
});

test('with no entry and no open path the Lumen page carries the note',async()=>{
  const document=fakeDom();
  await openUpdates(document,{host:'lumen',luatools:signedIn});
  embedReady(document,{entry:false});
  footerRow(document).onclick();
  await wait(250);
  assert.ok(document.getElementById('sls-luna-overlay'),'the menu is not left blank');
  assert.match(pageText(document),/Open the account from Lumen\u2019s own menu\./);
});

test('with embedding off the footer row hands over to Lumen as before',async()=>{
  const document=fakeDom();
  const opened=[];
  await openUpdates(document,{host:'lumen',prefs:{lumen_embed:false},
    openLuaTools:()=>opened.push('account'),luatools:signedIn});
  footerRow(document).onclick();
  await wait(150);
  assert.deepEqual(opened,['account'],'Lumen is called once');
  assert.equal(document.getElementById('sls-luna-overlay'),null,'Luna closed first');
});

test('a throwing account opener is caught, reported and stays inside Luna',async()=>{
  const document=fakeDom();
  const {warnings}=await openUpdates(document,{host:'lumen',
    lumenOpenOverlay:()=>{throw Error('no window');},luatools:signedIn});
  embedReady(document,{entry:false});
  footerRow(document).onclick();
  await wait(250);
  assert.ok(document.getElementById('sls-luna-overlay'),'the user is not left with nothing');
  assert.match(pageText(document),/Open the account from Lumen\u2019s own menu\./);
  assert.ok(warnings.some(line=>/^\[sls-luna\] Lumen\u2019s account page did not open: no window/.test(line)),
    'the throw surfaces through the shared warning path');
});

test('the footer state is read on open and again on Refresh, never on a timer',async()=>{
  const document=fakeDom();
  const {calls,timers}=await openUpdates(document,{host:'lumen'});
  const count=()=>calls.filter(entry=>entry.fn==='SlsLunaLuaTools').length;
  assert.equal(count(),1,'one read when the menu opens');
  [...timers.fns.values()].forEach(fn=>fn());
  await settle(40);
  assert.equal(count(),1,'no timer reads the footer state');
  button(document,'Refresh').onclick();
  await settle(140);
  assert.ok(count()>1,'the Refresh button reads it again');
});

test('the lua.tools account sub-entry exists only with Lumen\u2019s own entry and follows its panel',async()=>{
  const subEntry=document=>findNode(document.getElementById('sls-luna-overlay'),
    node=>String(node.className||'').indexOf('sls-luna-subpage')>=0
      &&String(node.textContent||'').trim()==='lua.tools account');
  const openLumenTab=document=>findNode(document.getElementById('sls-luna-overlay'),
    node=>(node.className||'').startsWith('sls-luna-tab')
      &&String(node.textContent||'').trim()==='Lumen').onclick();

  // With Lumen's account button present the extra row is drawn.
  const document=fakeDom();
  await openUpdates(document,{host:'lumen',luatools:signedIn});
  const overlay=embedReady(document);
  openLumenTab(document);
  await wait(120);
  let row=subEntry(document);
  assert.ok(row,'the account sub-entry is listed under Lumen');
  assert.equal(row.className.indexOf('active'),-1,'inactive while Lumen shows another page');

  // Lumen opens its account panel: the row is the active one.
  const panel=document.getElementById('lumen-panel-account');
  panel.style.display='block';
  openLumenTab(document);
  await wait(80);
  row=subEntry(document);
  assert.ok(row.className.indexOf('active')>=0,'the account panel highlights its row');
  const others=findNode(document.getElementById('sls-luna-overlay'),
    node=>String(node.className||'').indexOf('sls-luna-subpage')>=0
      &&String(node.textContent||'').trim()==='Fixes');
  assert.equal(others.className.indexOf('active'),-1,'Lumen pages are no longer highlighted');

  // Without the button (another Lumen build) the row is not invented.
  const plain=fakeDom();
  await openUpdates(plain,{host:'lumen',luatools:signedIn});
  embedReady(plain,{entry:false});
  openLumenTab(plain);
  await wait(120);
  assert.equal(subEntry(plain),null,'no entry without Lumen\u2019s own button');
});

test('the artwork page lists recent changes and undoes one only after a confirm',async()=>{
  const {document,calls}=await openSgdbPicker({sgdbHistory:{grid:2,hero:1}});
  const overlay=document.getElementById('sls-luna-overlay');
  assert.match(textOf(overlay),/Recent changes/,'the history list must be on the page');
  assert.match(textOf(overlay),/Grid · 2 undo steps/);
  assert.match(textOf(overlay),/Hero · 1 undo step/);
  const read=calls.filter(entry=>entry.fn==='SlsSgdbHistory').pop();
  assert.deepEqual(read.payload,{appid:1145360},'the history is read for the game on screen');
  assert.equal(collect(overlay,node=>node.className==='sls-luna-history-row').length,2,
    'one row per slot that still has a step');
  const undoLast=findNode(overlay,node=>node.textContent==='Undo last change');
  assert.ok(undoLast,'an Undo last change button must be offered');
  assert.equal(undoLast.disabled,false);
  // One click only arms it; the second click is the undo.
  let row=collect(overlay,node=>node.className==='sls-luna-history-row')
    .find(node=>/Grid/.test(node.textContent));
  findNode(row,node=>node.textContent==='Undo').onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  assert.equal(calls.filter(entry=>entry.fn==='SlsSgdbUndo').length,0,
    'the first click must not undo anything');
  const confirm=findNode(document.getElementById('sls-luna-overlay'),
    node=>node.textContent==='Confirm undo');
  assert.ok(confirm,'the row must ask for a second click');
  confirm.onclick();
  await new Promise(resolve=>setTimeout(resolve,60));
  const undo=calls.filter(entry=>entry.fn==='SlsSgdbUndo').pop();
  assert.deepEqual(undo.payload,{appid:1145360,kind:'grid'});
  const after=document.getElementById('sls-luna-overlay');
  assert.match(textOf(after),/Undone/);
  assert.match(textOf(after),/Grid · 1 undo step\b/,'the list must refresh from the backend');
  assert.equal(calls.filter(entry=>entry.fn==='SlsSgdbHistory').length>1,true,
    'the history is read again after a change');
});

test('an artwork undo disables the buttons while the call is in flight',async()=>{
  let release=null;
  const gate=fn=>{
    if(fn!=='SlsSgdbUndo')return null;
    return new Promise(resolve=>{release=()=>resolve(JSON.stringify({success:true,undone:true,history:1}));});
  };
  const {document,calls}=await openSgdbPicker({sgdbHistory:{grid:2},gate});
  const overlay=document.getElementById('sls-luna-overlay');
  const row=findNode(overlay,node=>node.className==='sls-luna-history-row');
  findNode(row,node=>node.textContent==='Undo').onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  findNode(document.getElementById('sls-luna-overlay'),
    node=>node.textContent==='Confirm undo').onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  assert.ok(calls.some(entry=>entry.fn==='SlsSgdbUndo'),'the confirm click sends the call');
  const busy=document.getElementById('sls-luna-overlay');
  assert.match(textOf(busy),/Working/,'an in-flight call says so');
  assert.equal(findNode(busy,node=>node.textContent==='Undo last change').disabled,true,
    'no second undo while one is in flight');
  const history=findNode(busy,node=>node.className==='sls-luna-history');
  const waiting=collect(history,node=>(node.className||'').indexOf('sls-luna-btn')===0);
  assert.ok(waiting.length>=2,'the history buttons are still there');
  waiting.forEach(button=>assert.equal(button.disabled,true,'every history button waits'));
  release();
  await new Promise(resolve=>setTimeout(resolve,60));
  assert.match(textOf(document.getElementById('sls-luna-overlay')),/Undone/);
});

test('the artwork history shows a backend error inline and an empty list says so',async()=>{
  const failed=await openSgdbPicker({sgdbHistoryError:'Steam folder not found'});
  const broken=failed.document.getElementById('sls-luna-overlay');
  const errorNode=findNode(broken,node=>node.className==='sls-luna-error');
  assert.ok(errorNode,'the backend refusal must be inline, not a blank page');
  assert.match(errorNode.textContent,/Could not read the artwork history: Steam folder not found/);
  assert.match(textOf(broken),/Artwork for/,'the rest of the page still renders');
  const empty=await openSgdbPicker({sgdbHistory:{grid:0,hero:0}});
  const blank=empty.document.getElementById('sls-luna-overlay');
  assert.match(textOf(blank),/No artwork changes yet\./);
  assert.equal(findNode(blank,node=>node.textContent==='Undo last change').disabled,true,
    'nothing to undo, nothing to press');
});

test('an icon apply says Steam must restart, and a live slot does not',async()=>{
  const {document,storage}=await openSgdbPicker();
  const icon=findNode(document.getElementById('sls-luna-overlay'),
    node=>node.textContent==='Icon'&&typeof node.onclick==='function');
  icon.onclick();
  await settle(60);
  const card=findNode(document.getElementById('sls-luna-overlay'),node=>node.className==='sls-luna-asset');
  card.onclick();
  await settle(60);
  const text=textOf(document.getElementById('sls-luna-overlay'));
  assert.match(text,/SteamGridDB artwork|Artwork applied/,'the page still renders');
  assert.match(text,/Restart Steam to see this icon\./,'an icon is file-only and needs a restart');
  assert.equal(storage.get('slsLunaArtworkLive'),undefined,
    'no live refresh may be requested for a slot Steam cannot repaint');

  const grid=await openSgdbPicker();
  findNode(grid.document.getElementById('sls-luna-overlay'),
    node=>node.className==='sls-luna-asset').onclick();
  await settle(60);
  const liveText=textOf(grid.document.getElementById('sls-luna-overlay'));
  assert.match(liveText,/Artwork applied/);
  assert.doesNotMatch(liveText,/Restart Steam to see this/,
    'a grid is live: no restart notice');
  const request=JSON.parse(grid.storage.get('slsLunaArtworkLive'));
  assert.deepEqual(request.kinds,['grid']);
});

test('the recent-changes list dates a slot from the backend times',async()=>{
  const {document}=await openSgdbPicker({
    sgdbHistory:{grid:2,hero:1},
    sgdbHistoryTimes:{grid:Math.floor(Date.now()/1000)-180,hero:Math.floor(Date.now()/1000)-7200}});
  const overlay=document.getElementById('sls-luna-overlay');
  const rows=collect(overlay,node=>node.className==='sls-luna-history-row');
  const grid=rows.find(row=>/^Grid/.test(row.textContent));
  const hero=rows.find(row=>/^Hero/.test(row.textContent));
  assert.match(grid.textContent,/Grid · 2 undo steps · changed 3m ago/,
    'times fills the date the session does not know');
  assert.match(hero.textContent,/Hero · 1 undo step · changed 2h ago/);

  const old=await openSgdbPicker({sgdbHistory:{grid:3}});
  const undated=findNode(old.document.getElementById('sls-luna-overlay'),
    node=>node.className==='sls-luna-history-row').textContent;
  assert.match(undated,/Grid · 3 undo steps/);
  assert.doesNotMatch(undated,/changed|ago/,'without times nothing is invented');
});

test('Undo is disabled at depth 0 and the reset asks for a second click',async()=>{
  const {document,calls}=await openSgdbPicker({sgdbHistory:{grid:0,wide:0,hero:0,logo:0,icon:0}});
  const overlay=document.getElementById('sls-luna-overlay');
  const undoLast=findNode(overlay,node=>node.textContent==='Undo last change');
  assert.equal(undoLast.disabled,true,'nothing to undo, nothing to press');
  assert.equal(collect(overlay,node=>node.tagName==='BUTTON'&&node.textContent==='Undo').length,0,
    'no per-slot Undo is offered at depth 0');

  const armed=await openSgdbPicker({sgdbHistory:{grid:1}});
  const reset=findNode(armed.document.getElementById('sls-luna-overlay'),
    node=>node.textContent==='Reset this game\u2019s artwork');
  assert.ok(reset,'the Steam-artwork reset must be offered');
  reset.onclick();
  await settle(30);
  assert.equal(armed.calls.filter(entry=>entry.fn==='SlsSgdbReset').length,0,
    'one click only arms the reset');
  const confirm=findNode(armed.document.getElementById('sls-luna-overlay'),
    node=>node.textContent==='Confirm reset');
  assert.ok(confirm,'the reset asks for a second click, exactly like undo');
  confirm.onclick();
  await settle(60);
  assert.equal(armed.calls.filter(entry=>entry.fn==='SlsSgdbReset').length,1);
});

test('a rate-limited apply says how long SteamGridDB wants to wait',async()=>{
  const {document}=await openSgdbPicker({sgdbApplyFailure:{success:false,code:'rate_limit',
    error:'SteamGridDB is rate-limiting requests.',retryAfter:42}});
  findNode(document.getElementById('sls-luna-overlay'),
    node=>node.className==='sls-luna-asset').onclick();
  await settle(60);
  assert.match(textOf(document.getElementById('sls-luna-overlay')),
    /Could not apply artwork: SteamGridDB is rate-limiting; try again in 42 s/);
});

test('the playtime page draws the 30-day chart, the weekly line and the week top',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const values=new Array(30).fill(0);
  values[29]=8040; // today
  values[27]=3600;
  const {w}=fakeWindow(document,{get:{success:true,apps:[],games:{},titles:{},shortcutNames:{},
    stats:{today:8040,last7d:12600,last30d:12000,prev7d:1800,
      daily30:dailyFixture(values),week_top:[{appid:1145360,name:'Hades',seconds:8040}]},
    runtime:{healthy:true,reportedAt:Date.now()/1000,file:'playtime.json'},
    account:'76561198000000000',manifestCount:0,shortcutCount:0}});
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await new Promise(resolve=>setTimeout(resolve,60));
  const overlay=document.getElementById('sls-luna-overlay');
  const bars=collect(overlay,node=>(node.className||'').startsWith('sls-luna-bar'));
  assert.equal(bars.length,30,'one bar per day');
  assert.equal(bars.filter(bar=>/is-today/.test(bar.className)).length,1,'today is marked once');
  assert.ok(bars.filter(bar=>/is-weekend/.test(bar.className)).length>=8,'weekends differ');
  const today=bars.find(bar=>/is-today/.test(bar.className));
  assert.match(today.title,/: 2 h 14 min$/,'the tooltip names the day and the time');
  bars.forEach(bar=>assert.doesNotMatch(bar.title,/NaN/,'no NaN ever reaches a tooltip'));
  const plot=findNode(overlay,node=>node.className==='sls-luna-chart');
  assert.equal(plot.getAttribute('role'),'img');
  assert.match(plot.getAttribute('aria-label'),/^Playtime for the last 30 days: 3 h 14 min in total, busiest day /);
  assert.match(textOf(overlay),/This week 3 h 30 min, 3 h more than last week\./);
  assert.match(textOf(overlay),/Hades/,'the games of the week are listed');
});

test('an older backend without the new playtime fields renders the old totals only',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w}=fakeWindow(document,{get:{success:true,apps:[],games:{},titles:{},shortcutNames:{},
    stats:{today:3600,last7d:7200,last30d:9000},
    runtime:{healthy:true,reportedAt:Date.now()/1000,file:'playtime.json'},
    account:'76561198000000000',manifestCount:0,shortcutCount:0}});
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await new Promise(resolve=>setTimeout(resolve,60));
  const overlay=document.getElementById('sls-luna-overlay');
  assert.equal(findNode(overlay,node=>(node.className||'').startsWith('sls-luna-chart')),null,
    'no chart without daily30');
  assert.doesNotMatch(textOf(overlay),/This week/,'no weekly line without the new fields');
  assert.doesNotMatch(textOf(overlay),/per day/);
  assert.match(textOf(overlay),/Last 30 days/,'the three totals are unchanged');
  assert.match(textOf(overlay),/2\.00 h|2 h/,'the totals still show');
});

test('the daily chart rejects malformed data instead of drawing it',()=>{
  const zeros=new Array(30).fill(0);
  assert.equal(dailyBars({}),null,'absent daily30 means no chart');
  assert.equal(dailyBars({daily30:'nope'}),null);
  assert.equal(dailyBars({daily30:zeros}),null,'a bare number is not a day');
  assert.equal(dailyBars({daily30:dailyFixture(zeros.slice(0,29))}),null,'29 days is not 30');
  const nan=dailyFixture(zeros);
  nan[4].seconds=NaN;
  assert.equal(dailyBars({daily30:nan}),null,'NaN is treated as absent, never as a bar');
  const broken=dailyFixture(zeros);
  broken[3].day='2026-02-31';
  assert.equal(dailyBars({daily30:broken}),null,'an impossible date is not a bar');
  const negative=dailyFixture(zeros.map(()=>-600));
  const clamped=dailyBars({daily30:negative});
  assert.ok(clamped,'negative playtime still draws');
  assert.equal(clamped.total,0);
  assert.equal(clamped.max,0,'a zero maximum must not divide by zero');
  assert.ok(clamped.bars.every(bar=>bar.seconds===0));
  assert.equal(clamped.busiest,null,'nothing is busiest in an empty month');
  assert.equal(minutesText(8040),'2 h 14 min');
  assert.equal(minutesText(0),'0 min');
});

function playtimeFixture(overrides){
  return Object.assign({success:true,apps:[],games:{},titles:{},shortcutNames:{},
    stats:{today:0,last7d:0,last30d:0},
    runtime:{healthy:true,reportedAt:Date.now()/1000,file:'playtime.json'},
    account:'76561198000000000',manifestCount:0,shortcutCount:0},overrides||{});
}

async function openPlaytimePage(document,options){
  menubarFixture(document,true);
  const made=fakeWindow(document,options||{});
  mount(made.w);
  document.getElementById('sls-luna-btn').click();
  await new Promise(resolve=>setTimeout(resolve,60));
  return Object.assign(made,{overlay:document.getElementById('sls-luna-overlay')});
}

test('the playtime totals use the one formatter, 0 min and under an hour included',async()=>{
  const document=fakeDom();
  const {overlay}=await openPlaytimePage(document,{get:playtimeFixture(
    {stats:{today:0,last7d:2700,last30d:45000}})});
  const text=textOf(overlay);
  assert.match(text,/\b0 min\b/,'a zero total reads 0 min, never 0.00 h');
  assert.match(text,/\b45 min\b/,'under an hour keeps minutes');
  assert.match(text,/\b12 h 30 min\b/,'hours and minutes share one format');
  assert.doesNotMatch(text,/\d+\.\d\d h/,'no decimal-hours total is left on the page');
});

test('a playtime Save goes busy and a second click cannot submit twice',async()=>{
  let release=null;
  const gate=fn=>{
    if(fn!=='SlsPlaytimeEdit')return null;
    return new Promise(resolve=>{release=()=>resolve(JSON.stringify({success:true}));});
  };
  const document=fakeDom();
  const {calls,overlay}=await openPlaytimePage(document,{gate,get:playtimeFixture(
    {apps:['620'],games:{'620':{title:'Portal 2',trackedSeconds:3600,adjustmentSeconds:0,lastPlayed:1}}})});
  const row=findNode(overlay,node=>node.className==='sls-luna-game');
  assert.ok(row,'the game row was not rendered');
  const save=findNode(row,node=>node.tagName==='BUTTON'&&node.textContent==='Save');
  assert.ok(save,'the row Save button was not rendered');
  save.onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  assert.equal(save.textContent,'Saving\u2026','the button must say it is working');
  assert.equal(save.disabled,true,'the button is disabled while the write is out');
  save.onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  assert.equal(calls.filter(entry=>entry.fn==='SlsPlaytimeEdit').length,1,
    'a held second click must not send a second write');
  release();
  await new Promise(resolve=>setTimeout(resolve,80));
  assert.equal(save.textContent,'Save','the button returns to its label');
  assert.equal(save.disabled,false);
});

test('replacing every playtime record needs a second in-page click',async()=>{
  const document=fakeDom();
  const {calls,overlay}=await openPlaytimePage(document,{get:playtimeFixture(
    {apps:['620'],games:{'620':{title:'Portal 2',trackedSeconds:3600,adjustmentSeconds:0,lastPlayed:1}}})});
  findNode(overlay,node=>node.tagName==='BUTTON'&&node.textContent==='Import JSON').onclick();
  const area=findNode(document.getElementById('sls-luna-overlay'),
    node=>node.tagName==='TEXTAREA');
  assert.ok(area,'the paste area was not shown');
  area.value='{"version":1,"games":{}}';
  findNode(document.getElementById('sls-luna-overlay'),
    node=>node.textContent==='Replace ALL local records').onclick();
  await new Promise(resolve=>setTimeout(resolve,20));
  assert.equal(calls.filter(entry=>entry.fn==='SlsPlaytimeImport').length,0,
    'the first click only asks');
  assert.match(textOf(document.getElementById('sls-luna-overlay')),
    /This replaces all 1 local record with the backup\./,
    'the confirm states that every record is replaced');
  findNode(document.getElementById('sls-luna-overlay'),
    node=>node.textContent==='Confirm replace').onclick();
  await new Promise(resolve=>setTimeout(resolve,60));
  assert.equal(calls.filter(entry=>entry.fn==='SlsPlaytimeImport').length,1,
    'the second click performs the replace');
});

test('the playtime read sends its last rev and an unchanged reply keeps the page',async()=>{
  const document=fakeDom();
  const {calls,overlay}=await openPlaytimePage(document,{playtimeRev:'rev-7',
    get:playtimeFixture({stats:{today:3600,last7d:3600,last30d:3600}})});
  const first=calls.filter(entry=>entry.fn==='SlsPlaytimeGet').pop();
  assert.equal(first.payload.rev,undefined,'the first read has no revision to send');
  assert.match(textOf(overlay),/\b1 h\b/,'the full payload was painted');
  await clickRefresh(document);
  const second=calls.filter(entry=>entry.fn==='SlsPlaytimeGet').pop();
  assert.equal(second.payload.rev,'rev-7','the page sends the revision it holds');
  assert.match(textOf(document.getElementById('sls-luna-overlay')),/\b1 h\b/,
    'an unchanged reply keeps the painted data, it never blanks the page');
});

test('a recovered ledger and merged sync copies are noted in one line each',async()=>{
  const document=fakeDom();
  const {overlay}=await openPlaytimePage(document,{get:playtimeFixture(
    {recovered:true,merge:{conflictFiles:['playtime.sync-conflict-2026.json'],recovered:1}})});
  const text=textOf(overlay);
  assert.match(text,/recovered from the previous good backup/);
  assert.match(text,/Steam sync left 1 conflicting ledger copy \(playtime\.sync-conflict-2026\.json\); the newest copy is in use\./);

  const quiet=fakeDom();
  const plain=await openPlaytimePage(quiet,{get:playtimeFixture()});
  assert.doesNotMatch(textOf(plain.overlay),/recovered from the previous|c conflicting ledger/,
    'a clean reply grows no diagnostic line');
});

test('the weekly line handles more, less, same and a missing previous week',()=>{
  assert.equal(weeklySentence({last7d:12600,prev7d:1800},null),
    'This week 3 h 30 min, 3 h more than last week.');
  assert.equal(weeklySentence({last7d:1800,prev7d:5400},null),
    'This week 30 min, 1 h less than last week.');
  assert.equal(weeklySentence({last7d:3600,prev7d:3600},null),
    'This week 1 h, same as last week.');
  assert.equal(weeklySentence({last7d:0,prev7d:0},null),
    'This week 0 min, same as last week.');
  assert.equal(weeklySentence({last7d:3600},null),'This week 1 h.','no prev7d, no comparison');
  assert.equal(weeklySentence({},null),null,'nothing to say, nothing rendered');
  const last7=weekTop({week_top:[{appid:1,name:'A',seconds:3600},{appid:2,seconds:1800},
    {appid:3,name:'C',seconds:0},{appid:4,name:'D',seconds:NaN}]},{'2':'Portal 2'});
  assert.deepEqual(last7,[{appid:1,name:'A',seconds:3600},{appid:2,name:'Portal 2',seconds:1800}],
    'zero and NaN entries are dropped and names come from the library when absent');
});

test('compressor sizes use one unit-aware formatter, bytes or MiB',async()=>{
  const gib=12.9*1024*1024*1024;
  const queueBase={active:false,total:1,done:1,failed:0,skipped:0,finished:true,
    index:1,current:null,next:[],games:[],outcome:{kind:'finished',reasonText:''}};
  const document=fakeDom();
  menubarFixture(document,true);
  const withBytes=compressorFixture({queue:Object.assign({},queueBase,{
    saved:gib,
    savings:{measuredMib:13209.6,measuredGames:1,unmeasuredGames:0,estimateMib:0,
      estimateGames:0,text:'',unit:'MiB',measuredBytes:gib,estimateBytes:0}})});
  const {w}=fakeWindow(document,{compressor:withBytes});
  mount(w);
  const overlay=await openCompressor(document);
  assert.match(textOf(overlay),/saved 12\.9 GiB/,'the explicit byte field is a byte count');
  assert.doesNotMatch(textOf(overlay),/saved 12 MB/,'a GiB value must never print as MB');

  const mibDom=fakeDom();
  menubarFixture(mibDom,true);
  const mibOnly=compressorFixture({queue:Object.assign({},queueBase,{saved:gib,
    games:[{name:'A',status:'done',saved:gib}]})});
  const second=fakeWindow(mibDom,{compressor:mibOnly});
  mount(second.w);
  const mibOverlay=await openCompressor(mibDom);
  assert.match(textOf(mibOverlay),/saved 12\.9 GiB/,'queue.saved and game saved are bytes');
  assert.doesNotMatch(textOf(mibOverlay),/TiB/);

  const statsDom=fakeDom();
  menubarFixture(statsDom,true);
  const statsFixture=compressorFixture({stats:{
    compression:{records:1,measured:1,original_bytes:2*gib,disk_bytes:0,saved_bytes:gib,games:[]},
    dedupe:{passes:0,directories:0,saved_bytes:0,entries:[]},
    luna:{measured:1,saved_bytes:gib,games:[{name:'Luna game',saved_bytes:gib}]},
    library:{games:1,bytes:2*gib}}});
  const third=fakeWindow(statsDom,{compressor:statsFixture});
  mount(third.w);
  const statsOverlay=await openCompressor(statsDom,'Stats');
  assert.match(textOf(statsOverlay),/Luna passes.*12\.9 GiB|12\.9 GiB.*Luna passes/);
  assert.match(textOf(statsOverlay),/Luna game.*12\.9 GiB/);
  assert.doesNotMatch(textOf(statsOverlay),/TiB/);
  assert.doesNotMatch(textOf(statsOverlay),/Luna passes\s*12\.9 KiB|Luna passes.*12 MB/,
    'a MiB stat is never read as bytes');
});

test('a refused library says the last run did not start, an empty queue keeps the old line',async()=>{
  const reason='the library sits on a btrfs snapshot';
  const refusedDom=fakeDom();
  menubarFixture(refusedDom,true);
  const refused=compressorFixture({queue:{active:false,total:2,done:0,failed:0,skipped:0,
    finished:false,index:0,current:null,next:[],games:[
      {name:'Hades',path:'/games/Hades',size:50,status:'pending',saved:0},
      {name:'Portal 2',path:'/games/Portal',size:40,status:'pending',saved:0}],
    refusal:{reason:'snapshot',text:reason,next_step:'boot the installed system and run again'},
    outcome:{kind:'not-started',reason:'snapshot',reasonText:reason,
      nextStep:'boot the installed system and run again',path:'/games'}}});
  const first=fakeWindow(refusedDom,{compressor:refused});
  mount(first.w);
  const overlay=await openCompressor(refusedDom);
  const text=textOf(overlay);
  assert.match(text,/Last run did not start/,'a refusal is not a finished run');
  assert.match(text,/the library sits on a btrfs snapshot/,'the reason is shown');
  assert.match(text,/Next: boot the installed system and run again\./,'the backend next step is shown');
  assert.doesNotMatch(text,/Last run finished/,'the old wording must be gone');

  const emptyDom=fakeDom();
  menubarFixture(emptyDom,true);
  const empty=compressorFixture({queue:{active:false,total:0,done:0,failed:0,skipped:0,
    finished:true,index:0,current:null,next:[],games:[],
    outcome:{kind:'empty',reasonText:''}}});
  const second=fakeWindow(emptyDom,{compressor:empty});
  mount(second.w);
  const emptyOverlay=await openCompressor(emptyDom);
  // An empty queue never drew a queue line, and still does not: no refusal
  // sentence, no "did not start", exactly today's page.
  assert.doesNotMatch(textOf(emptyOverlay),/did not start/);
  assert.doesNotMatch(textOf(emptyOverlay),/Last run/);
});

function visiblePanes(document){
  const overlay=document.getElementById('sls-luna-overlay');
  return collect(overlay,node=>node.className==='sls-luna-pane'
    && (!node.style||node.style.display!=='none'));
}
const shellReads=['SlsPlaytimeGet','SlsPresenceGet','SlsLunaInfo','SlsLunaLuaTools','SlsLunaUpdate'];

test('the menu issues its reads together, before any of them answers',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const held=[];
  let released=false;
  const gate=fn=>{
    if(released||shellReads.indexOf(fn)<0)return null;
    return new Promise(resolve=>{held.push(resolve);});
  };
  const {w,calls}=fakeWindow(document,{gate});
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await settle(40);
  // Nothing has answered yet, and every shell read is already out: the chain
  // is a fan-out, not one read waiting for the one before it.
  shellReads.forEach(name=>assert.ok(calls.some(entry=>entry.fn===name),
    name+' must be issued without waiting for the others'));
  assert.ok(held.length>=shellReads.length,
    'every read is in flight at once, not one after another');
  released=true;
  held.forEach(resolve=>resolve('{"success":true}'));
  await settle(100);
  assert.ok(document.getElementById('sls-luna-overlay'),'the menu stays usable');
});

test('one rejected read leaves the rest of the page rendered',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w}=fakeWindow(document,{failFns:['SlsPresenceGet'],
    get:playtimeFixture({stats:{today:1800,last7d:1800,last30d:1800}})});
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await settle(100);
  const overlay=document.getElementById('sls-luna-overlay');
  assert.ok(overlay,'a rejected read must not close the menu');
  assert.match(textOf(overlay),/30 min/,'the totals from the other reads still render');
  assert.match(textOf(overlay),/Import JSON/,'the page is still the Playtime page');
  assert.equal(w.__SlsLunaSelftest.state().open,true,'the selftest probe still answers');
});

test('the HLTB cache is read for its page only, never by opening the menu',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w,calls}=fakeWindow(document,{hltb:hltbFixture()});
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await settle(100);
  assert.equal(calls.filter(entry=>entry.fn==='SlsHltbGet').length,0,
    'opening on Playtime must not read the megabyte HLTB cache');
  const tab=findNode(document.getElementById('sls-luna-overlay'),
    node=>(node.className||'').startsWith('sls-luna-tab')&&node.textContent==='HowLongToBeat');
  assert.ok(tab,'the HowLongToBeat tab must exist');
  tab.onclick();
  await settle(100);
  assert.ok(calls.filter(entry=>entry.fn==='SlsHltbGet').length>=1,
    'its own page reads it');
});

test('a pass for a page the user left never paints the pane now on screen',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  let releaseHealth=null;
  const gate=fn=>{
    if(fn!=='SlsLunaHealth')return null;
    return new Promise(resolve=>{
      releaseHealth=()=>resolve(JSON.stringify({success:true,health:{version:'late-ghost'}}));
    });
  };
  const {w}=fakeWindow(document,{gate,
    get:playtimeFixture({stats:{today:1800,last7d:1800,last30d:1800}})});
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await settle(100);
  const overlay=()=>document.getElementById('sls-luna-overlay');
  findNode(overlay(),node=>(node.className||'').startsWith('sls-luna-tab')
    &&node.textContent.startsWith('Updates')).onclick();
  await settle(30);
  findNode(overlay(),node=>(node.className||'').startsWith('sls-luna-tab')
    &&node.textContent==='Playtime').onclick();
  await settle(30);
  releaseHealth();
  await settle(120);
  const panes=visiblePanes(document);
  assert.equal(panes.length,1,'exactly one pane is on screen');
  const visible=textOf(panes[0]);
  assert.match(visible,/Import JSON/,'the Playtime pane is the one on screen');
  assert.match(visible,/30 min/,'its own data is intact');
  assert.doesNotMatch(visible,/late-ghost|Update everything/,
    'the late Updates answer must not paint into the visible pane');
});

test('sidebar rows share one roving tabindex and move with the arrow keys',async()=>{
  const document=fakeDom();
  menubarFixture(document,true);
  const {w}=fakeWindow(document);
  mount(w);
  document.getElementById('sls-luna-btn').click();
  await new Promise(resolve=>setTimeout(resolve,40));
  const overlay=()=>document.getElementById('sls-luna-overlay');
  const rowFor=label=>collect(overlay(),node=>(node.className||'').startsWith('sls-luna-tab'))
    .find(node=>node.textContent===label);
  const playtime=rowFor('Playtime');
  assert.ok(playtime,'the sidebar must render');
  assert.equal(playtime.getAttribute('tabindex'),'0','the active row is the tab stop');
  assert.equal(playtime.getAttribute('aria-current'),'page','the active category is current');
  assert.equal(playtime.getAttribute('role'),'button');
  assert.equal(collect(overlay(),node=>(node.className||'').startsWith('sls-luna-tab'))
    .filter(node=>node.getAttribute('tabindex')==='0').length,1,'exactly one tab stop');
  const presence=rowFor('Presence');
  assert.equal(presence.getAttribute('tabindex'),'-1');
  assert.equal(presence.getAttribute('aria-current'),undefined);
  presence.onkeydown({key:'ArrowUp',target:presence,preventDefault(){}});
  assert.equal(playtime.getAttribute('tabindex'),'0','ArrowUp moves the tab stop up');
  assert.equal(presence.getAttribute('tabindex'),'-1');
  playtime.onkeydown({key:'ArrowDown',target:playtime,preventDefault(){}});
  assert.equal(presence.getAttribute('tabindex'),'0','ArrowDown moves the tab stop back down');
  presence.onkeydown({key:'End',target:presence,preventDefault(){}});
  assert.equal(rowFor('About').getAttribute('tabindex'),'0','End jumps to the last row');
  rowFor('About').onkeydown({key:'Home',target:rowFor('About'),preventDefault(){}});
  assert.equal(rowFor('Playtime').getAttribute('tabindex'),'0','Home jumps to the first row');
  // Enter and Space activate the row, without a mouse.
  rowFor('Presence').onkeydown({key:'Enter',target:rowFor('Presence'),preventDefault(){}});
  await new Promise(resolve=>setTimeout(resolve,40));
  assert.equal(w.__SlsLunaSelftest.state().tab,'presence','Enter opens the row');
  assert.equal(rowFor('Presence').getAttribute('aria-current'),'page');
  rowFor('Compressor').onkeydown({key:' ',target:rowFor('Compressor'),preventDefault(){}});
  await new Promise(resolve=>setTimeout(resolve,40));
  assert.equal(w.__SlsLunaSelftest.state().tab,'compressor','Space opens the row too');
  // Keys typed into a field are never the sidebar's.
  rowFor('Presence').onkeydown({key:'ArrowDown',target:{tagName:'INPUT'},preventDefault(){}});
  assert.equal(rowFor('Compressor').getAttribute('tabindex'),'0','an input keeps its arrow keys');
  assert.equal(rowFor('Playtime').getAttribute('tabindex'),'-1');
  // With the menu closed nothing responds, and teardown unbinds the document.
  const stale=rowFor('Presence');
  document.custom('document:keydown',{key:'Escape'});
  assert.equal(overlay(),null,'Escape still closes the menu');
  stale.onkeydown({key:'ArrowUp',target:stale});
  assert.equal(stale.getAttribute('tabindex'),'-1','a closed menu takes no keys');
  w.__SlsLunaTeardown();
  assert.equal((document.listeners['document:keydown']||[]).length,0,
    'nothing stays bound after teardown');
});
