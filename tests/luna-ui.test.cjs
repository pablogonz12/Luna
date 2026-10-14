// SLS Luna shell UI: the menubar button and the settings menu.
//
// Runs against a small DOM stub, because what matters is the wiring the client
// depends on: the button lands in the native menubar next to Lumen's, the menu
// opens with a working sidebar, and the settings for both add-ons are driven by
// their real RPC names.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {mount,buildRows,buildHltbRows,enforceSortLabel,findMenubar}=require('../addons/luna/plugin/public/panel.js');

function fakeDom(){
  const registry={};
  const created=[];
  const listeners={};
  const node=tag=>{
    const element={
      tagName:(tag||'div').toUpperCase(),id:'',className:'',title:'',hidden:false,
      style:{cssText:'',cssText2:''},children:[],parentNode:null,innerHTML:'',
      value:'',placeholder:'',type:'',rows:0,onclick:null,
      append(...kids){kids.forEach(kid=>element.appendChild(kid));},
      appendChild(kid){kid.parentNode=element;element.children.push(kid);register(kid);return kid;},
      insertBefore(kid,ref){
        kid.parentNode=element;
        const index=ref?element.children.indexOf(ref):-1;
        if(index<0)element.children.push(kid);else element.children.splice(index,0,kid);
        register(kid);return kid;
      },
      remove(){const p=element.parentNode;if(p){const i=p.children.indexOf(element);if(i>=0)p.children.splice(i,1);}
        delete registry[element.id];element.parentNode=null;},
      setAttribute(name,value){element['attr_'+name]=value;},
      getAttribute(name){return element['attr_'+name];},
      removeAttribute(name){delete element['attr_'+name];},
      addEventListener(type,fn){(listeners[element.id+':'+type]=listeners[element.id+':'+type]||[]).push(fn);},
      focus(){},select(){},
      click(){(listeners[element.id+':click']||[]).forEach(fn=>
        fn({preventDefault(){},stopPropagation(){}}));},
      get nextSibling(){const p=element.parentNode;if(!p)return null;
        const i=p.children.indexOf(element);return i>=0?p.children[i+1]||null:null;},
      querySelectorAll(){return [];},
    };
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
        element.children.length=0;
      },
    });
    Object.defineProperty(element,'textContent',{
      get:()=>element.children.length
        ? element.children.map(child=>child.textContent).join('')
        : ownText,
      set(value){ownText=String(value);markup='';element.children.length=0;},
    });
    Object.defineProperty(element,'innerText',{get:()=>element.textContent});
    created.push(element);
    return element;
  };
  const register=element=>{
    if(!element||typeof element!=='object')return;
    if(element.id)registry[element.id]=element;
    (element.children||[]).forEach(register);
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
    fire(type,event){const key=type.startsWith('document')?type:type;
      (listeners[type]||[]).forEach(fn=>fn(event||{}));},
    custom(type,event){(listeners[type]||[]).forEach(fn=>fn(event||{}));},
  };
}

function textOf(node){
  if(!node)return '';
  const own=node.children.length===0?(node.textContent||''):'';
  return own+node.children.map(textOf).join(' ');
}
function findNode(node,predicate){
  if(!node)return null;
  if(predicate(node))return node;
  for(const child of node.children||[]){const hit=findNode(child,predicate);if(hit)return hit;}
  return null;
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

function fakeWindow(document,{get,presence,compressor,hltb,fetchFailure,rateLimitOnce,sgdbKey,edit,setEnabled,prefs}={}){
  const calls=[];
  const hltbState=Object.assign({success:true,count:0,games:{},library:[]},hltb||{});
  hltbState.count=Object.keys(hltbState.games||{}).length;
  const rateLimited=new Set();
  const storage=new Map();
  const storageListeners=[];
  const prefsState=Object.assign({version:1,store_hide_followed:false},prefs||{});
  const sgdbState={apiKey:sgdbKey||'',gridDir:'/home/tester/.local/share/Steam/userdata/1/config/grid',gridError:''};
  const w={
    document,
    localStorage:{getItem:key=>storage.has(key)?storage.get(key):null,
      setItem:(key,value)=>storage.set(key,String(value)),
      removeItem:key=>storage.delete(key)},
    addEventListener:(type,fn)=>{if(type==='storage')storageListeners.push(fn);},
    __emitStorage:()=>{storageListeners.forEach(fn=>fn({key:'slsLunaArtworkRequest'}));},
    setInterval:(fn,ms)=>{return 1;},
    clearInterval:()=>{},
    // The fetch-missing queue spaces requests out; tests skip the waiting.
    setTimeout:(fn,ms)=>{setTimeout(fn,0);return 0;},
    prompt:()=>{throw Error('window.prompt must not be used: it freezes the Steam shell');},
    confirm:()=>{throw Error('window.confirm must not be used: it freezes the Steam shell');},
    Millennium:{callServerMethod:async(plugin,fn,args)=>{
      const payload=args&&args.raw?JSON.parse(args.raw):(args||{});
      calls.push({fn,payload});
      if(fn==='SlsPlaytimeGet')return JSON.stringify(get||{success:true,apps:[],games:{},titles:{},shortcutNames:{},
        stats:{today:0,last7d:0,last30d:0},runtime:{healthy:true,reportedAt:Date.now()/1000,file:'x.json'},account:'76561199026823325',manifestCount:0,shortcutCount:0});
      if(fn==='SlsPresenceGet')return JSON.stringify(presence||{success:true,state:{enabled:true,owned:{},running:{},cleanup:0},runtime:{healthy:true,error:''}});
      if(fn==='SlsPlaytimeEdit')return JSON.stringify({success:true});
      if(fn==='SlsPresenceSetEnabled')return JSON.stringify({success:true,enabled:payload.enabled});
      if(fn==='SlsCompressorGet')return JSON.stringify(compressor||compressorFixture());
      if(fn==='SlsCompressorPutSettings')return JSON.stringify({success:true,settings:payload});
      if(fn==='SlsCompressorRun')return JSON.stringify({success:true,job:{action:payload.action,running:true}});
      if(fn==='SlsCompressorCancel')return JSON.stringify({success:true,cancelled:true});
      if(fn==='SlsHltbGet')return JSON.stringify(hltbState);
      if(fn==='SlsHltbFetch'){
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
      if(fn==='SlsLunaInfo')return JSON.stringify({success:true,version:'0.13.0',
        addons:{luna:true},prefs:Object.assign({},prefsState)});
      if(fn==='SlsLunaPrefsSet'){prefsState[payload.key]=payload.value;
        return JSON.stringify({success:true,prefs:Object.assign({},prefsState)});}
      if(fn==='SlsSgdbGet')return JSON.stringify({success:true,apiKey:sgdbState.apiKey,
        gridDir:sgdbState.gridDir,gridError:sgdbState.gridError});
      if(fn==='SlsSgdbSetKey'){sgdbState.apiKey=(payload.key||'').trim();
        return JSON.stringify({success:true,hasKey:sgdbState.apiKey!==''});}
      if(fn==='SlsSgdbTest')return JSON.stringify(sgdbState.apiKey
        ?{success:true}:{success:false,code:'no_key',error:'Add your SteamGridDB API key first.'});
      if(fn==='SlsSgdbSearch')return JSON.stringify({success:true,
        games:[{id:1,name:'Hades'},{id:2,name:'Hades II'}]});
      if(fn==='SlsSgdbAssets')return JSON.stringify({success:true,total:1,assets:[
        {id:11,url:'https://cdn.example/grid1.png',thumb:'https://cdn.example/t1.png',
          width:600,height:900,style:'alternate',mime:'image/png',author:'artist1'}]});
      if(fn==='SlsSgdbApply')return JSON.stringify({success:true,path:'/grid/out.png',
        kind:payload.kind,backedUp:true,live:payload.kind!=='icon'});
      if(fn==='SlsSgdbRestore')return JSON.stringify({success:true,restored:true});
      if(fn==='SlsSgdbAuto')return JSON.stringify({success:true,results:[
        {kind:'grid',ok:true,live:true},{kind:'wide',ok:true,live:true},
        {kind:'hero',ok:true,live:true},{kind:'logo',ok:true,live:true},
        {kind:'icon',ok:true,live:false}]});
      if(fn==='SlsGraphicsGet')return JSON.stringify({success:true,version:'0.2.0',
        system:{distro:'Arch Linux (test)',kernel:'6.12.4-arch1'},
        gpus:[{card:'card0',vendor:'0x1002',vendor_name:'AMD',device:'0x744c',driver:'amdgpu',
          product:'AMD Radeon RX 7900 XTX'}],
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
  return {w,calls,storage,prefs:prefsState};
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
  const order=bar.children.map(child=>child.id||child.textContent);
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
      runtime:{healthy:true,reportedAt:Date.now()/1000,file:'76561199026823325.json'},account:'76561199026823325',
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
  assert.match(text,/Connected/,'the account row must show the connection state');
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
  fixture.queue={active:true,total:3,index:2,done:1,failed:0,skipped:0,saved:123456789,finished:false,
    current:{name:'Cronos: The New Dawn',path:'/games/Cronos',size:100,totalBytes:100,doneBytes:42,
      phase:'compress',scannedBytes:0},
    next:[{name:'Hades',size:50}],
    games:[{name:'Cronos: The New Dawn',path:'/games/Cronos',size:100,status:'running',saved:0},
      {name:'Hades',path:'/games/Hades',size:50,status:'pending',saved:0},
      {name:'Pony Island',path:'/games/Pony',size:20,status:'done',saved:123456789}]};
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
  assert.match(textOf(overlay),/done . saved 118 MiB/,'a finished game must show its saving');
  assert.ok(findNode(overlay,node=>node.className==='sls-luna-mini'),'a per-game mini bar was not rendered');
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
  const restore=findNode(document.getElementById('sls-luna-overlay'),
    node=>node.textContent==='Restore previous hero'&&typeof node.onclick==='function');
  restore.onclick();
  await new Promise(resolve=>setTimeout(resolve,40));
  const restoreCall=calls.filter(entry=>entry.fn==='SlsSgdbRestore').pop();
  assert.deepEqual(restoreCall.payload,{appid:1145360,kind:'hero'});
  assert.match(textOf(document.getElementById('sls-luna-overlay')),/previous artwork is back/);
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
  assert.match(textOf(document.getElementById('sls-luna-overlay')),/Applied the best match to 5 of 5 slots/);
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

test('the SORT BY label is restored only while the HLTB sort is stored',()=>{
  const document=fakeDom();
  const header=document.createElement('div');
  const sortLabel=document.createElement('div');
  sortLabel.textContent='Sort By';
  const button=document.createElement('div');
  const buttonText=document.createElement('div');
  button.appendChild(buttonText);
  header.append(sortLabel,button);
  document.body.appendChild(header);
  assert.equal(enforceSortLabel(document,4),false,'other sorts keep Steam labels');
  assert.equal(enforceSortLabel(document,12),true,'the stored HLTB choice must label itself');
  assert.equal(buttonText.textContent,'HowLongToBeat');
  assert.equal(enforceSortLabel(document,12),false,'already correct labels are left alone');
  buttonText.textContent='';
  assert.equal(enforceSortLabel(document,1),false,'leaving the sort lets Steam own the label');
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
      entries:[{path:'/mnt/storage/Games/SteamLibrary/steamapps/common/Slay the Spire 2',saved_bytes:6606028}]},
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
