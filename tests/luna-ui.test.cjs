// SLS Luna shell UI: the menubar button and the settings menu.
//
// Runs against a small DOM stub, because what matters is the wiring the client
// depends on: the button lands in the native menubar next to Lumen's, the menu
// opens with a working sidebar, and the settings for both add-ons are driven by
// their real RPC names.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {mount,buildRows,findMenubar}=require('../addons/luna/plugin/public/panel.js');

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
    querySelectorAll:()=>created,
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

function fakeWindow(document,{get,presence,compressor,edit,setEnabled}={}){
  const calls=[];
  const w={
    document,
    setInterval:(fn,ms)=>{return 1;},
    clearInterval:()=>{},
    prompt:()=>null,
    confirm:()=>true,
    Millennium:{callServerMethod:async(plugin,fn,args)=>{
      const payload=args&&args.raw?JSON.parse(args.raw):(args||{});
      calls.push({fn,payload});
      if(fn==='SlsPlaytimeGet')return JSON.stringify(get||{success:true,apps:[],games:{},titles:{},shortcutNames:{},
        stats:{today:0,last7d:0,last30d:0},runtime:{healthy:true,reportedAt:Date.now()/1000,file:'x.json'},account:'76561190000000000',manifestCount:0,shortcutCount:0});
      if(fn==='SlsPresenceGet')return JSON.stringify(presence||{success:true,state:{enabled:true,owned:{},running:{},cleanup:0},runtime:{healthy:true,error:''}});
      if(fn==='SlsPlaytimeEdit')return JSON.stringify({success:true});
      if(fn==='SlsPresenceSetEnabled')return JSON.stringify({success:true,enabled:payload.enabled});
      if(fn==='SlsCompressorGet')return JSON.stringify(compressor||compressorFixture());
      if(fn==='SlsCompressorPutSettings')return JSON.stringify({success:true,settings:payload});
      if(fn==='SlsCompressorRun')return JSON.stringify({success:true,job:{action:payload.action,running:true}});
      if(fn==='SlsCompressorCancel')return JSON.stringify({success:true,cancelled:true});
      return JSON.stringify({success:true});
    }},
  };
  return {w,calls};
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
      runtime:{healthy:true,reportedAt:Date.now()/1000,file:'76561190000000000.json'},account:'76561190000000000',
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
  const called=calls.filter(c=>c.fn==='SlsCompressorRun').pop();
  assert.equal(called.payload.action,'run','Run now must ask for the composed run');
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
