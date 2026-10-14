// SLS Luna game-menu agent: the shared right-click registry and its first
// action (HowLongToBeat).
//
// The Steam context menu is driven through a small fake of the library's jsx
// layer, the same shape the Playtime add-on tests use: what matters is that
// Luna's actions land after the primary action exactly when a game is
// resolved, that other add-ons can register through the registry, and that
// selecting an action routes the URL through Steam's own handlers.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const {discoverMenuTools,installLunaMenu,bootLunaMenu,openExternalUrl,searchUrlFor,nameOf,
  collectLibrary,pushLibrary}=require('../addons/luna/plugin/public/agent.js');

test('search URL trims and encodes the game name',()=>{
  assert.equal(searchUrlFor('  Hades  '),'https://howlongtobeat.com/?q=Hades');
  assert.equal(searchUrlFor('Ori & the Blind Forest'),'https://howlongtobeat.com/?q=Ori%20%26%20the%20Blind%20Forest');
  assert.equal(searchUrlFor('Pokémon Legends: Arceus'),'https://howlongtobeat.com/?q=Pok%C3%A9mon%20Legends%3A%20Arceus');
  assert.equal(searchUrlFor(''),'https://howlongtobeat.com/?q=');
});

test('game names come from display_name, then name',()=>{
  assert.equal(nameOf({display_name:'Hades'}),'Hades');
  assert.equal(nameOf({name:'Celeste'}),'Celeste');
  assert.equal(nameOf({display_name:'  '}),'');
  assert.equal(nameOf(null),'');
});

test('external URLs prefer the Steam client handlers and fall back to the Lumen relay',async()=>{
  const opened=[];
  assert.equal(await openExternalUrl({SteamClient:{System:{OpenInSystemBrowser:u=>opened.push(['system',u])}}},'https://example.com/x'),true);
  assert.deepEqual(opened,[['system','https://example.com/x']]);
  opened.length=0;
  assert.equal(await openExternalUrl({SteamClient:{URL:{ExecuteSteamURL:u=>opened.push(['url',u])}}},'https://example.com/x'),true);
  assert.deepEqual(opened,[['url','steam://openurl_external/https://example.com/x']]);
  const calls=[];
  const relay={Millennium:{callServerMethod:(plugin,fn,args)=>{
    calls.push([plugin,fn,args]);
    return Promise.resolve(JSON.stringify({ok:true}));
  }}};
  assert.equal(await openExternalUrl(relay,'https://example.com/x'),true);
  assert.deepEqual(calls,[['lumen','__lumenOpenExternalUrl',{url:'https://example.com/x'}]]);
  assert.equal(await openExternalUrl({},'https://example.com/x'),false);
  assert.equal(await openExternalUrl({},''),false);
});

function fixture(){
  const React={createElement:(type,props,...children)=>{
    const element={type,props:{...props,children}};
    if(props&&'key' in props)element.key=props.key;
    return element;},
    isValidElement:e=>!!e?.props};
  const jsx={jsx:(t,p)=>({type:t,props:p}),jsxs:(t,p)=>({type:t,props:p})};
  class MenuItem{}
  class Outer{GetTargetApps(){} BuildManageSubmenu(){} render(){}}
  class Inner{HideIfSubmenu(){} HideMenu(){} render(){}}
  return {React,jsx,item:MenuItem,Outer,Inner,originalJsx:jsx.jsx,originalJsxs:jsx.jsxs};
}

function webpackFixture(requireName){
  const React={createElement(){},isValidElement:e=>!!e?.props};
  const calls=[];
  const jsxs=(t,p)=>{calls.push(p);return {type:t,props:p};};
  const jsx={jsx:(t,p)=>{calls.push(p);return {type:t,props:p};},jsxs};
  const MenuItem=new Function('return function contextMenuItem(){return "onSelected"}')();
  MenuItem.prototype.OnClick=function(){};
  const moduleExports={...jsx,MenuItem};
  const exports={1:moduleExports};
  const require=id=>exports[id];
  require.m={
    main:new Function('return function(module,exports,'+requireName+'){' +
      'const nativeName="BuildManageSubmenu";'+requireName+'(1);}')(),
  };
  return {SP_REACT:React,__expectedJsx:moduleExports,__calls:calls,webpackChunksteamui:{push(chunk){chunk[2](require);}}};
}

function labelsAfterPrimary(el){
  return (el.props.children||[]).filter(child=>child&&child.props&&Array.isArray(child.props.children)
    &&typeof child.props.children[0]==='string'&&child.props.children[0]!=='Play')
    .map(child=>child.props.children[0]);
}

test('discovers the native menu dependencies when Steam minifies the require parameter',()=>{
  for(const requireName of ['r','t']){
    const w=webpackFixture(requireName);
    const found=discoverMenuTools(w);
    assert.ok(found,'menu tools were not discovered for require alias '+requireName);
    assert.equal(found.jsx,w.__expectedJsx);
    assert.equal(found.item.name,'contextMenuItem');
  }
});

test('the focused game gains the HowLongToBeat action after its primary action',()=>{
  const tools=fixture();
  const w={appStore:{GetAppOverviewByAppID:id=>({appid:id,display_name:'Hades'})}};
  const menu=installLunaMenu(w,tools);
  assert.ok(menu,'the menu must install with provided tools');
  tools.jsx.jsx(tools.Outer,{overview:{appid:1145360,display_name:'Hades'},instance:{}});
  const el=tools.jsx.jsx(tools.Inner,{label:'Hades',labelId:'l',children:[
    {type:'play',props:{}},{type:'rg',props:{rgApps:[{appid:1145360}]}},{type:'fav',props:{}}]});
  assert.deepEqual(labelsAfterPrimary(el),['HowLongToBeat']);
  assert.equal(menu.list().some(entry=>entry.id==='hltb'),true);
  menu.dispose();
  assert.equal(w.__SlsLunaMenu,undefined);
});

test('bulk menus and menus without a resolvable game get no Luna actions',()=>{
  const tools=fixture();
  const w={appStore:{GetAppOverviewByAppID:id=>({appid:id,display_name:'App '+id})}};
  const menu=installLunaMenu(w,tools);
  const bulk=tools.jsx.jsx(tools.Inner,{label:'',labelId:'l',children:[
    {type:'play',props:{}},{type:'rg',props:{rgApps:[{appid:1},{appid:2}]}}]});
  assert.equal(bulk.props.children.length,2);
  tools.jsx.jsx(tools.Outer,{overview:{appid:3,display_name:'Some Game'},instance:{}});
  const submenu=tools.jsx.jsx(tools.Inner,{label:'Add to',labelId:'l',children:[{type:'collection',props:{}}]});
  assert.equal(submenu.props.children.length,1,'a late submenu must not inherit the game target');
  menu.dispose();
});

test('a focused label authorizes the game even when child actions mention other apps',()=>{
  const tools=fixture();
  const app={appid:3240220,display_name:'Grand Theft Auto V Enhanced'};
  const w={appStore:{GetAppOverviewByAppID:id=>id===3240220?app:null}};
  const menu=installLunaMenu(w,tools);
  tools.jsx.jsx(tools.Outer,{overview:app,instance:{}});
  const el=tools.jsx.jsx(tools.Inner,{label:'Grand Theft Auto V Enhanced',labelId:'l',children:[
    {type:'play',props:{}},{type:'rg',props:{rgApps:[app]}},{type:'dlc',props:{app:{appid:999}}}]});
  assert.deepEqual(labelsAfterPrimary(el),['HowLongToBeat']);
  menu.dispose();
});

test('rendering the same menu again keeps exactly one Luna item',()=>{
  const tools=fixture();
  const w={appStore:{GetAppOverviewByAppID:id=>({appid:id,display_name:'Hades'})}};
  const menu=installLunaMenu(w,tools);
  const children=[{type:'play',props:{}},{type:'rg',props:{rgApps:[{appid:1145360}]}}];
  const once=tools.jsx.jsx(tools.Inner,{label:'Hades',labelId:'l',children});
  tools.jsx.jsx(tools.Outer,{overview:{appid:1145360,display_name:'Hades'},instance:{}});
  const twice=tools.jsx.jsx(tools.Inner,{label:'Hades',labelId:'l',children:once.props.children});
  const count=(twice.props.children||[]).filter(child=>child&&child.props
    &&child.props.children&&child.props.children[0]==='HowLongToBeat').length;
  assert.equal(count,1);
  menu.dispose();
});

test('selecting an action opens the browser through the Steam client',async()=>{
  const tools=fixture();
  const opened=[];
  const w={appStore:{GetAppOverviewByAppID:id=>({appid:id,display_name:'Ori & the Blind Forest'})},
    SteamClient:{System:{OpenInSystemBrowser:url=>opened.push(url)}}};
  const menu=installLunaMenu(w,tools);
  tools.jsx.jsx(tools.Outer,{overview:{appid:387290,display_name:'Ori & the Blind Forest'},instance:{}});
  const el=tools.jsx.jsx(tools.Inner,{label:'Ori & the Blind Forest',labelId:'l',children:[
    {type:'play',props:{}},{type:'rg',props:{rgApps:[{appid:387290}]}}]});
  const item=(el.props.children||[]).find(child=>child&&child.props&&child.props.children
    &&child.props.children[0]==='HowLongToBeat');
  assert.ok(item,'the HowLongToBeat item must be present');
  await item.props.onSelected();
  assert.deepEqual(opened,['https://howlongtobeat.com/?q=Ori%20%26%20the%20Blind%20Forest']);
  menu.dispose();
});

test('a failing action is contained and never breaks the menu',async()=>{
  const tools=fixture();
  const errors=[];
  const originalError=console.error;
  console.error=(...args)=>errors.push(args.join(' '));
  try{
    const w={appStore:{GetAppOverviewByAppID:id=>({appid:id,display_name:'Hades'})}};
    const menu=installLunaMenu(w,tools);
    menu.register({id:'broken',label:'Broken',run:()=>Promise.reject(Error('boom'))});
    tools.jsx.jsx(tools.Outer,{overview:{appid:1145360,display_name:'Hades'},instance:{}});
    const el=tools.jsx.jsx(tools.Inner,{label:'Hades',labelId:'l',children:[
      {type:'play',props:{}},{type:'rg',props:{rgApps:[{appid:1145360}]}}]});
    const item=(el.props.children||[]).find(child=>child&&child.props&&child.props.children
      &&child.props.children[0]==='Broken');
    await item.props.onSelected();
    assert.equal(errors.some(line=>line.includes('broken')&&line.includes('boom')),true);
    menu.dispose();
  }finally{console.error=originalError;}
});

test('other add-ons can register and remove their own game actions through the registry',()=>{
  const tools=fixture();
  const w={appStore:{GetAppOverviewByAppID:id=>({appid:id,display_name:'Hades'})}};
  const menu=installLunaMenu(w,tools);
  assert.equal(menu.register({id:'artwork',label:'Change artwork',run:()=>{}}),true);
  assert.equal(menu.register({id:'',label:'x',run:()=>{}}),false);
  assert.equal(menu.register({id:'x',label:'',run:()=>{}}),false);
  assert.equal(menu.register({id:'x',label:'x'}),false);
  assert.equal(w.__SlsLunaMenu,menu,'the registry is published on the window');
  menu.register({id:'gated',label:'Gated',run:()=>{},isEnabled:()=>false});
  tools.jsx.jsx(tools.Outer,{overview:{appid:1145360,display_name:'Hades'},instance:{}});
  const el=tools.jsx.jsx(tools.Inner,{label:'Hades',labelId:'l',children:[
    {type:'play',props:{}},{type:'rg',props:{rgApps:[{appid:1145360}]}}]});
  assert.deepEqual(labelsAfterPrimary(el),['HowLongToBeat','Change artwork']);
  assert.equal(menu.unregister('hltb'),true);
  assert.equal(menu.unregister('missing'),false);
  const el2=tools.jsx.jsx(tools.Inner,{label:'Hades',labelId:'l',children:[
    {type:'play',props:{}},{type:'rg',props:{rgApps:[{appid:1145360}]}}]});
  assert.deepEqual(labelsAfterPrimary(el2),['Change artwork']);
  menu.dispose();
});

test('an action whose eligibility throws is treated as disabled',()=>{
  const tools=fixture();
  const w={appStore:{GetAppOverviewByAppID:id=>({appid:id,display_name:'Hades'})}};
  const originalWarn=console.warn;
  console.warn=()=>{};
  try{
    const menu=installLunaMenu(w,tools);
    menu.register({id:'throwing',label:'Throwing',run:()=>{},isEnabled:()=>{throw Error('nope');}});
    tools.jsx.jsx(tools.Outer,{overview:{appid:1145360,display_name:'Hades'},instance:{}});
    const el=tools.jsx.jsx(tools.Inner,{label:'Hades',labelId:'l',children:[
      {type:'play',props:{}},{type:'rg',props:{rgApps:[{appid:1145360}]}}]});
    assert.deepEqual(labelsAfterPrimary(el),['HowLongToBeat']);
    menu.dispose();
  }finally{console.warn=originalWarn;}
});

test('ensure re-arms only when Steam rebuilds the module, not when another add-on wraps on top',()=>{
  const tools=fixture();
  const w={appStore:{GetAppOverviewByAppID:id=>({appid:id,display_name:'Hades'})}};
  const menu=installLunaMenu(w,tools);
  assert.equal(menu.attached,true,'installing must wrap the current module');
  assert.equal(menu.ensure(),false,'a healthy hook must not re-arm');

  // Another add-on (Playtime's agent) wraps on top of Luna's wrapper on the
  // SAME module object. Luna is still in the chain and must stay quiet, or the
  // two agents would re-wrap each other forever.
  const previousJsx=tools.jsx.jsx;
  const previousJsxs=tools.jsx.jsxs;
  tools.jsx.jsx=function(type,...args){return previousJsx.call(this,type,...args);};
  tools.jsx.jsxs=function(type,...args){return previousJsxs.call(this,type,...args);};
  assert.equal(menu.attached,true,'another wrapper on top is not detachment');
  assert.equal(menu.ensure(),false,'an unchanged module object must never re-wrap');
  const el=tools.jsx.jsx(tools.Outer,{overview:{appid:1145360,display_name:'Hades'},instance:{}});
  const el2=tools.jsx.jsx(tools.Inner,{label:'Hades',labelId:'l',children:[
    {type:'play',props:{}},{type:'rg',props:{rgApps:[{appid:1145360}]}}]});
  assert.deepEqual(labelsAfterPrimary(el2),['HowLongToBeat'],
    'the chain through the other wrapper must still insert Luna items');
  assert.ok(el,'render must still flow through the chain');

  // Steam rebuilds the library bundle: a new module object appears and the
  // menu must re-arm on it.
  Object.assign(w,webpackFixture('t'));
  assert.equal(menu.ensure(),true,'a replaced module object must re-arm');
  assert.equal(menu.attached,true);
  w.__calls.length=0;
  const fresh=w.__expectedJsx;
  fresh.jsx(tools.Outer,{overview:{appid:1145360,display_name:'Hades'},instance:{}});
  fresh.jsx(tools.Inner,{label:'Hades',labelId:'l',children:[
    {type:'play',props:{}},{type:'rg',props:{rgApps:[{appid:1145360}]}}]});
  const rendered=(w.__calls[w.__calls.length-1]||{}).children||[];
  const labels=rendered.filter(child=>child&&child.props&&Array.isArray(child.props.children)
    &&typeof child.props.children[0]==='string'&&child.props.children[0]!=='play')
    .map(child=>child.props.children[0]);
  assert.deepEqual(labels,['HowLongToBeat'],
    'the re-armed hook on the rebuilt module must insert the item');
  assert.equal(menu.ensure(),false,'a healthy re-armed hook must not keep wrapping');

  menu.dispose();
  assert.equal(w.__SlsLunaMenu,undefined);
});

test('a wrapper cycle between two add-ons cannot recurse without bound',()=>{
  const tools=fixture();
  const w={appStore:{GetAppOverviewByAppID:id=>({appid:id,display_name:'Hades'})}};
  const menu=installLunaMenu(w,tools);
  // Simulate the worst case a hostile ordering could produce: the foreign
  // wrapper that Luna captured as its originals calls back into Luna's own
  // wrapper. The re-entry guard must fall back to the leaf function.
  const previousJsx=tools.jsx.jsx;
  tools.jsx.jsx=function(type,...args){return previousJsx.call(this,type,...args);};
  menu.ensure();
  let renders=0;
  const original=tools.jsx.jsx;
  tools.jsx.jsx=function(type,...args){
    renders+=1;
    if(renders>10)throw Error('infinite wrapper recursion');
    return original.call(this,type,...args);
  };
  const el=tools.jsx.jsx(tools.Inner,{label:'Hades',labelId:'l',children:[
    {type:'play',props:{}},{type:'rg',props:{rgApps:[{appid:1145360}]}}]});
  assert.ok(renders<10,'rendering must terminate');
  assert.ok(el,'rendering must return an element');
  menu.dispose();
});

test('booting retries until the library module appears and only arms once',()=>{
  const tools=fixture();
  const timers=[];
  const w={setInterval:(fn,ms)=>{timers.push(fn);return timers.length;}};
  const originalWarn=console.warn;
  console.warn=()=>{};
  try{
    bootLunaMenu(w);
    assert.equal(w.__SlsLunaMenu,undefined,'no module yet, nothing to install');
    assert.equal(timers.length,1,'after boot one retry beat must be scheduled');
    assert.equal(w.__SlsLunaAgent,true);
    bootLunaMenu(w);
    assert.equal(timers.length,1,'a second boot must not schedule another beat');
    Object.assign(w,webpackFixture('t'));
    timers[0]();
    assert.ok(w.__SlsLunaMenu,'the retry beat must install the menu once the module exists');
    w.__SlsLunaMenu.dispose();
  }finally{console.warn=originalWarn;}
});

test('fetching HowLongToBeat times is not a right-click action: the Luna page owns it',async()=>{
  const tools=fixture();
  const w={appStore:{GetAppOverviewByAppID:id=>({appid:id,display_name:'Hades'})},
    Millennium:{callServerMethod:()=>JSON.stringify({success:true,record:{name:'Hades'}})}};
  const menu=installLunaMenu(w,tools);
  assert.deepEqual(menu.list().map(entry=>entry.id),['hltb'],
    'only the search link stays on the context menu');
  tools.jsx.jsx(tools.Outer,{overview:{appid:1145360,display_name:'Hades'},instance:{}});
  const el=tools.jsx.jsx(tools.Inner,{label:'Hades',labelId:'l',children:[
    {type:'play',props:{}},{type:'rg',props:{rgApps:[{appid:1145360}]}}]});
  assert.deepEqual(labelsAfterPrimary(el),['HowLongToBeat']);
  menu.dispose();
});

test('collectLibrary reports owned games and shortcuts, never tools or duplicates',()=>{
  const w={appStore:{allApps:[
    {appid:1145360,app_type:1,display_name:'Hades'},
    {appid:620,app_type:1,display_name:'Portal 2'},
    {appid:3212112756,app_type:1073741824,display_name:'Minecraft'},
    {appid:228980,app_type:4,display_name:'Steamworks Common Redistributables'},
    {appid:1145360,app_type:1,display_name:'Hades again'},
    {appid:999,app_type:1,display_name:''},
  ]}};
  const games=collectLibrary(w);
  assert.deepEqual(games.map(game=>game.name),['Hades','Minecraft','Portal 2']);
  assert.equal(games.find(game=>game.name==='Minecraft').shortcut,true);
  assert.equal(games.find(game=>game.name==='Hades').shortcut,false);
});

test('collectLibrary also reads a Map-backed store and tolerates nonsense',()=>{
  const map=new Map();
  map.set(1,{appid:10,app_type:1,display_name:'Alpha'});
  map.set(2,{appid:20,app_type:1,display_name:'Beta'});
  const games=collectLibrary({appStore:{m_mapApps:map}});
  assert.deepEqual(games.map(game=>game.name),['Alpha','Beta']);
  assert.deepEqual(collectLibrary({}),[]);
  assert.deepEqual(collectLibrary({appStore:{allApps:[null,{appid:0,app_type:1,display_name:'x'}]}}),[]);
});

test('pushLibrary sends the inventory once and skips identical payloads',async()=>{
  const tools=fixture();
  const calls=[];
  const w={appStore:{allApps:[{appid:1145360,app_type:1,display_name:'Hades'}]},
    Millennium:{callServerMethod:(plugin,fn,args)=>{calls.push([plugin,fn,args]);
      return JSON.stringify({success:true,count:1});}}};
  assert.equal(await pushLibrary(w),true);
  assert.equal(calls.length,1);
  assert.equal(calls[0][0],'lumen');
  assert.equal(calls[0][1],'SlsHltbLibrary');
  const payload=JSON.parse(calls[0][2].raw);
  assert.deepEqual(payload.games,[{appid:1145360,name:'Hades',shortcut:false}]);
  assert.equal(await pushLibrary(w),false,'an unchanged list must not be re-sent');
  assert.equal(calls.length,1);
  w.appStore.allApps.push({appid:620,app_type:1,display_name:'Portal 2'});
  assert.equal(await pushLibrary(w),true,'a changed list must be sent');
  assert.equal(calls.length,2);
  const failing={appStore:w.appStore,Millennium:{callServerMethod:()=>Promise.reject(Error('down'))}};
  const quiet=await pushLibrary(failing);
  assert.equal(quiet,false,'a failed push must not throw');
});
