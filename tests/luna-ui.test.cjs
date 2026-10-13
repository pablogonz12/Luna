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
    // children's, which is what the code and the tests read.
    let ownText='';
    Object.defineProperty(element,'textContent',{
      get:()=>element.children.length
        ? element.children.map(child=>child.textContent).join('')
        : ownText,
      set(value){ownText=String(value);element.children.length=0;},
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

function fakeWindow(document,{get,presence,edit,setEnabled}={}){
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
  ['sls-playtime-panel-button','sls-playtime-panel','sls-presence-panel-button','sls-presence-panel']
    .forEach(id=>{const node=document.createElement('div');node.id=id;document.body.appendChild(node);});
  const {w}=fakeWindow(document);
  mount(w);
  assert.equal(document.getElementById('sls-playtime-panel'),null,'the old floating panel must be gone');
  assert.equal(document.getElementById('sls-presence-panel'),null);
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
    assert.equal(document.created.filter(e=>e.className==='sls-luna-search').length,1);
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
