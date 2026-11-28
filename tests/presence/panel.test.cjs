// Presence panel: standalone it draws its floating button, and it stands aside
// when SLS Luna is hosting the settings menu.
//
// The panel is a plain IIFE with no exports, so it is evaluated in a fresh VM
// context against a small DOM stub - the same code the client gets, unmodified.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');

const SOURCE=path.join(__dirname,'..','..','addons','presence','plugin','public','panel.js');

function fakeWindow({luna=false}={}){
  const registry={};
  const listeners=[];
  const node=tag=>{
    const element={
      tagName:(tag||'div').toUpperCase(),id:'',style:{cssText:''},hidden:false,textContent:'',
      children:[],
      append(...kids){kids.forEach(kid=>{element.children.push(kid);if(kid.id)registry[kid.id]=kid;});},
      appendChild(kid){element.append(kid);return kid;},
      get nextSibling(){return null;},
    };
    return element;
  };
  const document={
    readyState:'complete',
    body:node('body'),
    createElement:node,
    getElementById:id=>registry[id]||null,
    addEventListener(type,fn){listeners.push({type,fn});},
  };
  const w={
    document,
    __SlsLunaUi:luna,
    Millennium:{callServerMethod:async()=>JSON.stringify({success:true,state:{enabled:false,owned:{},running:{}},runtime:{}})},
    setInterval:()=>1,
    clearInterval:()=>{},
  };
  return {w,document,registry};
}

function loadPanel(w){
  const code=fs.readFileSync(SOURCE,'utf8');
  const sandbox={window:w,document:w.document,console,JSON,Date,Error,Promise,
    setTimeout,clearTimeout,setInterval:w.setInterval,clearInterval:w.clearInterval};
  vm.runInNewContext(code,sandbox,{filename:SOURCE});
}

test('standalone, the panel draws its own floating button and window',()=>{
  const {w,document}=fakeWindow();
  loadPanel(w);
  assert.ok(document.getElementById('sls-presence-panel-button'),'the floating button is missing');
  assert.ok(document.getElementById('sls-presence-panel'),'the floating window is missing');
  assert.equal(w.__SlsPresencePanel,true);
});

test('with SLS Luna present, the panel creates nothing',()=>{
  const {w,document}=fakeWindow({luna:true});
  loadPanel(w);
  assert.equal(document.getElementById('sls-presence-panel-button'),null,
    'no floating button may be created while Luna is present');
  assert.equal(document.getElementById('sls-presence-panel'),null);
  assert.equal(w.__SlsPresencePanel,undefined,'the standalone guard must stay untouched');
});

test('corruption notice uses text, lists paths, and acknowledges with a JSON string',async()=>{
  const {w,document}=fakeWindow();
  const calls=[];
  const unsafe='/tmp/<img src=x onerror=alert(1)>';
  let locked=true;
  w.Millennium.callServerMethod=async (_host,method,args)=>{
    calls.push({method,args});
    if(method==='SlsPresenceAcknowledgeCorruption') {
      assert.equal(args.raw,JSON.stringify({acknowledge:true}));
      locked=false;
      return JSON.stringify({success:true});
    }
    return JSON.stringify({success:true,state:{enabled:false,owned:{}},runtime:{},apps:[],
      corruptionRequiresAcknowledgement:locked,quarantineDirectory:unsafe,quarantinePaths:[unsafe+'/presence.json.quarantine.1.1']});
  };
  loadPanel(w);
  document.getElementById('sls-presence-panel-button').onclick();
  await new Promise(resolve=>setImmediate(resolve));
  const notice=document.getElementById('sls-presence-corruption');
  const button=document.getElementById('sls-presence-start-fresh');
  assert.equal(notice.hidden,false);
  assert.match(notice.textContent,/state and its backup could not be read/);
  assert.match(notice.textContent,/ownership changes are paused/);
  assert.ok(notice.textContent.includes(unsafe));
  assert.equal(notice.innerHTML,undefined,'backend text must not be inserted as HTML');
  assert.equal(button.textContent,'Start fresh');
  await button.onclick();
  assert.equal(calls.filter(x=>x.method==='SlsPresenceAcknowledgeCorruption').length,1);
  assert.equal(notice.hidden,true);
});
