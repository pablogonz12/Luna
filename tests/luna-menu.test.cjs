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
  collectLibrary,pushLibrary,SORT_VALUE,SORT_LABEL,ARTWORK_REQUEST_KEY,ARTWORK_LIVE_KEY,
  ARTWORK_LIVE_RESULT_KEY,setHltbRecords,compareHltb,syncSortAs,
  isSortDropdown,withSortOption,withHltbSections,hltbGridType,hookHltbShelf,withHltbOverlay,hltbMinutesFor,
  handleArtworkLive,refreshHltbData}=require('../addons/luna/plugin/public/agent.js');

test('home HLTB shelves redraw after asynchronous times load, without remounting',()=>{
  setHltbRecords({});
  const tasks=[];
  const apps=[{appid:1,display_name:'Alpha',sort_as:'alpha'},
    {appid:2,display_name:'Zebra',sort_as:'zebra'},
    {appid:3,display_name:'Missing',sort_as:'missing'}];
  const w={appStore:{allApps:apps},setTimeout:fn=>tasks.push(fn)};
  let unmounted=0;
  class Shelf {
    constructor(sort){this.props={showcase:{eSortBy:sort},collectionId:'games'};this.updates=0;
      this.m_fnSort=collection=>collection.visibleApps.slice().sort((a,b)=>a.sort_as.localeCompare(b.sort_as));}
    ComputeChildSizes(){}
    render(){this.order=this.m_fnSort({visibleApps:apps}).map(a=>a.appid);}
    forceUpdate(){this.updates++;this.render();}
    componentWillUnmount(){unmounted++;}
  }
  const shelf=new Shelf(SORT_VALUE),other=new Shelf(1);
  assert.equal(hookHltbShelf(Shelf,shelf.props,w),true);
  shelf.render();other.render();
  assert.deepEqual(shelf.order,[1,3,2],'no times initially: alphabetical');
  setHltbRecords({'1':{main:3600},'2':{main:180}},w);
  setHltbRecords({'1':{main:3600},'2':{main:180}},w);
  assert.equal(tasks.length,1,'coalesce refreshes outside render');
  tasks.shift()();
  assert.deepEqual(shelf.order,[2,1,3],'shortest first, missing last');
  assert.equal(shelf.updates,1);
  assert.equal(other.updates,0,'normal shelves must not be refreshed');
  const long={...w,localStorage:{getItem:k=>k==='slsLunaHltbOrder'?'long':null}};
  w.localStorage=long.localStorage;
  syncSortAs(w);tasks.shift()();
  assert.deepEqual(shelf.order,[1,2,3],'longest-first preference keeps missing last');
  shelf.componentWillUnmount();other.componentWillUnmount();
  assert.equal(unmounted,2,'preserve upstream cleanup');
  delete w.localStorage;syncSortAs(w);
  assert.equal(tasks.length,0,'unmounted instances are not retained');
  assert.equal(hookHltbShelf(function Other(){},{showcase:{eSortBy:12},collectionId:'x'},w),false);
  assert.deepEqual(apps.map(a=>a.sort_as),['alpha','zebra','missing'],'never mutate shared app names');
});

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

test('normal agent installation automatically hooks shelves before cached HLTB data arrives',async()=>{
  setHltbRecords({});
  const tools=fixture(),tasks=[];
  const apps=[{appid:1,display_name:'Alpha',sort_as:'alpha'},
    {appid:2,display_name:'Zebra',sort_as:'zebra'}];
  const w={appStore:{allApps:apps},setTimeout:fn=>tasks.push(fn),
    localStorage:{getItem:k=>k==='AppGridDisplaySettings'?'{"eSortBy":12}':null,setItem(){}},
    Millennium:{callServerMethod:()=>Promise.resolve({games:{'1':{main:7200},'2':{main:120}}})}};
  class Shelf {
    constructor(){this.props={showcase:{eSortBy:SORT_VALUE},collectionId:'games'};
      this.m_fnSort=collection=>collection.visibleApps.slice().sort((a,b)=>a.sort_as.localeCompare(b.sort_as));}
    ComputeChildSizes(){}
    render(){this.order=this.m_fnSort({visibleApps:apps}).map(a=>a.appid);}
    forceUpdate(){this.render();}
  }
  const menu=installLunaMenu(w,tools),shelf=new Shelf();
  tools.jsx.jsx(Shelf,shelf.props); // Steam's normal JSX path, no manual hook.
  shelf.render();assert.deepEqual(shelf.order,[1,2]);
  await refreshHltbData(w);syncSortAs(w); // The agent's normal async startup/beat.
  tasks.splice(0).forEach(fn=>fn());
  assert.deepEqual(shelf.order,[2,1]);
  shelf.componentWillUnmount();menu.dispose();
});

function webpackFixture(requireName){
  const React={createElement:(type,props,...children)=>{
    const element={type,props:{...props,children}};
    if(props&&'key' in props)element.key=props.key;
    return element;},
    isValidElement:e=>!!e?.props,
    useState:()=>[],
    Fragment:{}};
  const calls=[];
  const jsxs=(t,p)=>{calls.push(p);return {type:t,props:p};};
  const jsx={jsx:(t,p)=>{calls.push(p);return {type:t,props:p};},jsxs};
  const MenuItem=new Function('return function contextMenuItem(){return "onSelected"}')();
  MenuItem.prototype.OnClick=function(){};
  const moduleExports={...jsx,MenuItem};
  const exports={1:moduleExports,2:React};
  const require=id=>exports[id];
  require.m={
    1:new Function('return function(module,exports,'+requireName+'){' +
      'const nativeName="BuildManageSubmenu";'+requireName+'(1);}')(),
    2:new Function('return function(module,exports){const a="react"}')(),
  };
  return {SP_REACT:React,__expectedJsx:moduleExports,__calls:calls,
    webpackChunksteamui:{push(chunk){chunk[2](require);}}};
}

const SORT_ENUM=[{data:1,label:'Alphabetical'},{data:10,label:'Friends Playing'},
  {data:2,label:'% of Achievements Complete'},{data:4,label:'Hours Played'},{data:5,label:'Last Played'},
  {data:6,label:'Release Date'},{data:7,label:'Date Added to Library'},{data:8,label:'Size on Disk'},
  {data:9,label:'Metacritic Score'},{data:11,label:'Steam Review'}];

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

test('still discovers the menu when Steam stops exposing window.SP_REACT',()=>{
  // The 2026-10 client update removed SP_REACT from SharedJSContext; requiring
  // it silently killed the sort option and every jsx-based hook.
  for(const requireName of ['r','t']){
    const w=webpackFixture(requireName);
    delete w.SP_REACT;
    const found=discoverMenuTools(w);
    assert.ok(found,'the menu tools must come from the module graph instead');
    assert.equal(typeof found.React.createElement,'function');
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

test('ensure stays quiet while another add-on wraps on top, and re-arms after a rebuild',()=>{
  const w=webpackFixture('t');
  w.appStore={GetAppOverviewByAppID:id=>({appid:id,display_name:'Hades'})};
  const menu=installLunaMenu(w);
  assert.ok(menu,'the menu must install from the live module');
  const module=w.__expectedJsx;
  const ourWrapper=module.jsx;
  assert.equal(menu.attached,true);

  // Another add-on (Playtime's agent) wraps on top of Luna's wrapper, calling
  // through. That is NOT detachment: the probe stamps Luna as present and
  // nothing re-wraps - which is what stops the two agents fighting.
  const foreign=function(type,...args){return ourWrapper.call(this,type,...args);};
  const foreignXs=function(type,...args){return module.jsxs.call(this,type,...args);};
  module.jsx=foreign;
  module.jsxs=foreignXs;
  assert.equal(menu.ensure(),false,'a call-through wrapper on top is not detachment');
  assert.equal(module.jsx,foreign,'Luna must not re-wrap');
  assert.ok(module.__slsSeen&&Date.now()-module.__slsSeen.luna<5000,
    'the probe must have stamped Luna as present in the chain');
  class InnerProbe{HideIfSubmenu(){} HideMenu(){} render(){}}
  const el=module.jsx(InnerProbe,{label:'Hades',labelId:'l',children:[
    {type:'play',props:{}},{type:'rg',props:{rgApps:[{appid:1145360}]}}]});
  assert.deepEqual(labelsAfterPrimary(el),['HowLongToBeat'],
    'the chain through the other wrapper must still insert Luna items');

  // Steam rebuilds the bundle in place: fresh functions, no wrapper, and no
  // stamp since. The probe sees the stale stamp and re-arms on the same object.
  module.jsx=(t,p)=>({type:t,props:p});
  module.jsxs=(t,p)=>({type:t,props:p});
  module.__slsSeen.luna=Date.now()-60000;
  assert.equal(menu.ensure(),true,'a real rebuild must re-arm');
  assert.equal(module.jsx,ourWrapper,'the wrapper must be back on the module');
  const again=module.jsx(InnerProbe,{label:'Hades',labelId:'l',children:[
    {type:'play',props:{}},{type:'rg',props:{rgApps:[{appid:1145360}]}}]});
  assert.deepEqual(labelsAfterPrimary(again),['HowLongToBeat'],
    'the re-armed hook must insert the item');
  assert.equal(menu.ensure(),false,'a healthy hook must not re-arm again');
  menu.dispose();
  assert.equal(w.__SlsLunaMenu,undefined);
});

test('a foreign wrapper that calls through still terminates and inserts once',()=>{
  const w=webpackFixture('t');
  w.appStore={GetAppOverviewByAppID:id=>({appid:id,display_name:'Hades'})};
  const menu=installLunaMenu(w);
  const module=w.__expectedJsx;
  const ourWrapper=module.jsx;
  let depth=0;
  module.jsx=function(type,...args){
    depth+=1;
    if(depth>10)throw Error('infinite wrapper recursion');
    try{return ourWrapper.call(this,type,...args);}finally{depth-=1;}
  };
  class InnerProbe{HideIfSubmenu(){} HideMenu(){} render(){}}
  const el=module.jsx(InnerProbe,{label:'Hades',labelId:'l',children:[
    {type:'play',props:{}},{type:'rg',props:{rgApps:[{appid:1145360}]}}]});
  assert.ok(depth<=1,'the chain must not recurse');
  assert.deepEqual(labelsAfterPrimary(el),['HowLongToBeat']);
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
    assert.equal(w.__SlsLunaMenu.sort.value,SORT_VALUE);
    w.__SlsLunaMenu.dispose();
  }finally{console.warn=originalWarn;}
});

test('fetching HowLongToBeat times is not a right-click action: the Luna page owns it',async()=>{
  const tools=fixture();
  const w={appStore:{GetAppOverviewByAppID:id=>({appid:id,display_name:'Hades'})},
    Millennium:{callServerMethod:()=>JSON.stringify({success:true,record:{name:'Hades'}})}};
  const menu=installLunaMenu(w,tools);
  assert.deepEqual(menu.list().map(entry=>entry.id),['hltb','artwork'],
    'both actions are registered; the beta switch only gates rendering');
  tools.jsx.jsx(tools.Outer,{overview:{appid:1145360,display_name:'Hades'},instance:{}});
  const el=tools.jsx.jsx(tools.Inner,{label:'Hades',labelId:'l',children:[
    {type:'play',props:{}},{type:'rg',props:{rgApps:[{appid:1145360}]}}]});
  assert.deepEqual(labelsAfterPrimary(el),['HowLongToBeat']);
  menu.dispose();
});

test('the artwork action is hidden until beta features are on',()=>{
  const tools=fixture();
  const store=new Map();
  const w={appStore:{GetAppOverviewByAppID:id=>({appid:id,display_name:'Hades'})},
    localStorage:{getItem:key=>store.has(key)?store.get(key):null,
      setItem:(key,value)=>store.set(key,String(value)),removeItem:key=>store.delete(key)}};
  const menu=installLunaMenu(w,tools);
  const render=()=>{
    tools.jsx.jsx(tools.Outer,{overview:{appid:1145360,display_name:'Hades'},instance:{}});
    const el=tools.jsx.jsx(tools.Inner,{label:'Hades',labelId:'l',children:[
      {type:'play',props:{}},{type:'rg',props:{rgApps:[{appid:1145360}]}}]});
    return labelsAfterPrimary(el);
  };
  assert.deepEqual(render(),['HowLongToBeat'],'beta off hides Change artwork');
  store.set('slsLunaBeta','1');
  assert.deepEqual(render(),['HowLongToBeat','Change artwork'],'beta on reveals Change artwork');
  store.delete('slsLunaBeta');
  assert.deepEqual(render(),['HowLongToBeat'],'turning beta off hides it again');
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

test('the numeric comparator orders known times first in both directions',()=>{
  setHltbRecords({'1':{main:3600},'2':{main:7200},'3':{main:1800}});
  const apps=[{appid:1},{appid:2},{appid:3},{appid:99}];
  assert.deepEqual(apps.slice().sort((a,b)=>compareHltb(a,b,'short')).map(a=>a.appid),[3,1,2,99]);
  assert.deepEqual(apps.slice().sort((a,b)=>compareHltb(a,b,'long')).map(a=>a.appid),[2,1,3,99]);
  setHltbRecords({'4':{match:'none'}});
  assert.equal(compareHltb({appid:4,name:'A'},{appid:99,name:'B'}),-1);
  setHltbRecords({});
});

function sortableWindow(stored,order){
  const store=new Map();
  if(stored!==undefined) store.set('AppGridDisplaySettings',JSON.stringify({eSortBy:stored}));
  if(order!==undefined) store.set('slsLunaHltbOrder',order);
  return {
    appStore:{allApps:[
      {appid:1,display_name:'Alpha',sort_as:'alpha'},
      {appid:2,display_name:'Beta'},
      {appid:3,display_name:'Gamma',sort_as:'gamma'},
    ]},
    localStorage:{getItem:key=>store.has(key)?store.get(key):null,
      setItem:(key,value)=>store.set(key,String(value)),
      removeItem:key=>store.delete(key)},
    storage:store,
  };
}

test('view-local sections sort without mutating shared overviews or input arrays',()=>{
  setHltbRecords({'1':{main:3600},'2':{main:7200}});
  const w=sortableWindow();
  const apps=w.appStore.allApps.map(Object.freeze);
  const children=Object.freeze([apps[2],apps[1],apps[0]].map(app=>Object.freeze({m_overview:app})));
  const props={eSortBy:SORT_VALUE,childSections:[{children}],strCollectionId:'c'};
  const sorted=withHltbSections(props,w);
  assert.deepEqual(sorted.childSections[0].children.map(c=>c.m_overview.appid),[1,2,3]);
  assert.deepEqual(children.map(c=>c.m_overview.appid),[3,2,1]);
  assert.deepEqual(apps.map(a=>a.sort_as),['alpha',undefined,'gamma']);
  assert.equal(withHltbSections({...props,eSortBy:1},w),null);
  setHltbRecords({});
});

test('the Change artwork action hands the game to the Luna picker',async()=>{
  const tools=fixture();
  const store=new Map();
  store.set('slsLunaBeta','1'); // the picker lives behind the beta switch
  const w={appStore:{GetAppOverviewByAppID:id=>({appid:id,display_name:'Hades'})},
    localStorage:{getItem:key=>store.has(key)?store.get(key):null,
      setItem:(key,value)=>store.set(key,String(value)),removeItem:key=>store.delete(key)}};
  const menu=installLunaMenu(w,tools);
  tools.jsx.jsx(tools.Outer,{overview:{appid:1145360,display_name:'Hades'},instance:{}});
  const el=tools.jsx.jsx(tools.Inner,{label:'Hades',labelId:'l',children:[
    {type:'play',props:{}},{type:'rg',props:{rgApps:[{appid:1145360}]}}]});
  const item=(el.props.children||[]).find(child=>child&&child.props&&child.props.children
    &&child.props.children[0]==='Change artwork');
  assert.ok(item,'the artwork action must be offered');
  await item.props.onSelected();
  const request=JSON.parse(store.get(ARTWORK_REQUEST_KEY));
  assert.equal(request.appid,1145360);
  assert.equal(request.name,'Hades');
  assert.ok(request.at>0,'the request must be timestamped');
  menu.dispose();
});

test('a stale global sort preference must not override another view selection',()=>{
  const scheduled=[];
  const store=new Map();
  store.set('AppGridDisplaySettings',JSON.stringify({eSortBy:SORT_VALUE}));
  const w={appStore:{allApps:[{appid:1,display_name:'A',sort_as:'a'}]},
    localStorage:{getItem:key=>store.has(key)?store.get(key):null,
      setItem:(key,value)=>store.set(key,String(value)),removeItem:key=>store.delete(key)},
    setTimeout:fn=>{scheduled.push(fn);return 0;}};
  setHltbRecords({'1':{main:3600}});
  const changes=[];
  withSortOption({rgOptions:SORT_ENUM.slice(),selectedOption:4,onChange:value=>changes.push(value)},w);
  assert.equal(scheduled.length,0,'never force a global sort onto a local view');
  assert.deepEqual(changes,[]);
  assert.equal(store.get('slsLunaSortLive'),'4','the live value must be published for the panel');
  withSortOption({rgOptions:SORT_ENUM.slice(),selectedOption:4,onChange:value=>changes.push(value)},w);
  assert.equal(scheduled.length,0);
  store.set('AppGridDisplaySettings',JSON.stringify({eSortBy:4}));
  withSortOption({rgOptions:SORT_ENUM.slice(),selectedOption:1,onChange:value=>changes.push(value)},w);
  assert.equal(scheduled.length,0,'another stored choice must never be fought');
  setHltbRecords({});
});

test('live artwork requests are applied through Steam own API',async()=>{
  const store=new Map();
  const applied=[];
  const w={
    SteamClient:{Apps:{SetCustomArtworkForApp:(appid,base64,extension,type)=>{
      applied.push([appid,base64,extension,type]);
      return Promise.resolve();
    }}},
    localStorage:{getItem:key=>store.has(key)?store.get(key):null,
      setItem:(key,value)=>store.set(key,String(value)),removeItem:key=>store.delete(key)},
    Millennium:{callServerMethod:(plugin,fn,args)=>{
      assert.equal(fn,'SlsSgdbLive');
      const payload=JSON.parse(args.raw);
      if(payload.kind==='icon'){
        return Promise.resolve(JSON.stringify({success:false,code:'no_live',error:'no icon slot'}));
      }
      return Promise.resolve(JSON.stringify({success:true,base64:'AAA',extension:'png',
        type:payload.kind==='grid'?0:3}));
    }},
  };
  store.set(ARTWORK_LIVE_KEY,JSON.stringify({appid:1145360,kinds:['grid','wide','icon'],at:Date.now()}));
  assert.equal(await handleArtworkLive(w),true,'the request must be consumed');
  assert.deepEqual(applied,[[1145360,'AAA','png',0],[1145360,'AAA','png',3]]);
  const result=JSON.parse(store.get(ARTWORK_LIVE_RESULT_KEY));
  assert.ok(Math.abs(Date.now()-result.at)<5*60*1000);
  assert.deepEqual(result.results.map(entry=>[entry.kind,entry.ok]),
    [['grid',true],['wide',true],['icon',false]]);
  assert.equal(await handleArtworkLive(w),false,'an unchanged request is not replayed');
  assert.equal(await handleArtworkLive({localStorage:{getItem:()=>null}}),false,'no storage, no crash');
});

test('the dropdown only adds an option and delegates selection without shared mutations',()=>{
  setHltbRecords({'1':{main:3600}});
  const w=sortableWindow();
  const changes=[];
  const props={rgOptions:SORT_ENUM.slice(),onChange:value=>changes.push(value),selectedOption:4};
  const result=withSortOption(props,w);
  assert.equal(result.rgOptions.length,11);
  assert.equal(result.rgOptions[10].data,SORT_VALUE);
  assert.equal(w.appStore.allApps[0].sort_as,'alpha','no keys before the option is chosen');
  result.onChange(SORT_VALUE);
  assert.deepEqual(changes,[SORT_VALUE],'the original onChange must still run');
  assert.equal(w.appStore.allApps[0].sort_as,'alpha','the view sorts itself, not the app store');
  result.onChange(1);
  assert.equal(w.appStore.allApps[0].sort_as,'alpha','another choice must restore them');
  // The persisted choice applies as soon as the dropdown reports it.
  const selected={rgOptions:SORT_ENUM.slice(),selectedOption:SORT_VALUE};
  withSortOption(selected,w);
  assert.equal(w.appStore.allApps[0].sort_as,'alpha');
  setHltbRecords({});
});

test('other dropdowns cannot reset HLTB sorting in an independent view',()=>{
  setHltbRecords({'1':{main:3600}});
  const w=sortableWindow(SORT_VALUE);
  const before=w.appStore.allApps[0].sort_as;
  // The same option list renders as a right-click/context menu without a
  // selectedOption, and other views render it with their own local selection.
  const menuProps={rgOptions:SORT_ENUM.slice()};
  const result=withSortOption(menuProps,w);
  assert.ok(result,'the menu still gains the option');
  assert.equal(w.appStore.allApps[0].sort_as,before,'the keys must survive the menu rendering');
  withSortOption({rgOptions:SORT_ENUM.slice(),selectedOption:4},w);
  assert.equal(w.appStore.allApps[0].sort_as,before,'a foreign selection must not restore either');
  // Even an explicit pick affects only Steam's view-local selection.
  const picked=withSortOption({rgOptions:SORT_ENUM.slice(),selectedOption:SORT_VALUE,onChange:()=>{}},w);
  picked.onChange(4);
  assert.equal(w.appStore.allApps[0].sort_as,'alpha','an explicit pick restores');
  picked.onChange(SORT_VALUE);
  setHltbRecords({});
});

test('periodic sync ignores stored sort selection and only watches direction',()=>{
  setHltbRecords({'1':{main:3600}});
  const w=sortableWindow(SORT_VALUE);
  assert.equal(syncSortAs(w),false);
  assert.equal(w.appStore.allApps[0].sort_as,'alpha');
  w.storage.set('AppGridDisplaySettings',JSON.stringify({eSortBy:4}));
  assert.equal(syncSortAs(w),false);
  assert.equal(w.appStore.allApps[0].sort_as,'alpha');
  assert.equal(syncSortAs(w),false,'nothing left to do when no keys are active');
  setHltbRecords({});
});

test('missing persisted sort and interleaved dropdown renders do not affect shared games',()=>{
  setHltbRecords({'1':{main:3600}});
  const w=sortableWindow();  // no AppGridDisplaySettings at all, as after the client update
  // Picking Luna's option in a view applies the keys and publishes the live value.
  const picked=withSortOption({rgOptions:SORT_ENUM.slice(),onChange(){},selectedOption:SORT_VALUE},w);
  assert.ok(picked,'the sort dropdown must be wrapped');
  assert.equal(w.appStore.allApps[0].sort_as,'alpha');
  // The periodic sync must not undo a live Luna choice just because this client
  // stopped persisting the setting.
  assert.equal(syncSortAs(w),false);
  assert.equal(w.appStore.allApps[0].sort_as,'alpha');
  // An explicit switch away still restores the originals through onChange.
  const other=withSortOption({rgOptions:SORT_ENUM.slice(),onChange(){},selectedOption:4},w);
  other.onChange(4);
  assert.equal(w.appStore.allApps[0].sort_as,'alpha','switching away restores the originals');
  setHltbRecords({});
});

test('collection grids sort their rendered sections without a persisted choice',()=>{
  setHltbRecords({'1':{main:3600}});
  const tools=fixture();
  const w=sortableWindow();                 // no AppGridDisplaySettings at all
  const api=installLunaMenu(w,tools);
  assert.ok(api,'the menu must install');
  // A section's dropdown only renders while its header is hovered, but its grid
  // renders as Luna-sorted - that is the signal the sync must trust.
  const result=tools.jsx.jsx(function Grid(){},{eSortBy:SORT_VALUE,
    childSections:[{children:[{m_overview:{appid:2}},{m_overview:{appid:1}}]}]});
  assert.deepEqual(result.props.childSections[0].children.map(c=>c.m_overview.appid),[1,2]);
  assert.equal(syncSortAs(w),false);
  assert.equal(w.appStore.allApps[0].sort_as,'alpha');
  const diag=JSON.parse(w.storage.get('slsLunaSortDiag')||'{}');
  assert.equal(diag.event,'collection');
  assert.equal(diag.engine,'view-local');
  api.dispose();
  setHltbRecords({});
});

test('mounted functional grids refresh on cache changes, direction changes and clear subscriptions',()=>{
  setHltbRecords({});
  const w=sortableWindow(),tasks=[];
  w.setTimeout=fn=>tasks.push(fn);
  let updates=0,effect,cleanup;
  const ref={};
  const React={
    useReducer:()=>[0,()=>updates++],useRef:()=>ref,
    useEffect:fn=>{effect=fn;},createElement:(type,props)=>({type,props}),
  };
  const Original=()=>{};
  const props={eSortBy:SORT_VALUE,childSections:[{children:[
    {m_overview:{appid:1,name:'Alpha'}},{m_overview:{appid:2,name:'Zebra'}}]}]};
  const Grid=hltbGridType(Original,props,w,React);
  assert.notEqual(Grid,Original);
  assert.equal(hltbGridType(Original,{...props,eSortBy:1},w,React),Grid,'stable type across sorts');
  Grid(props);cleanup=effect();updates=0;
  setHltbRecords({'1':{main:3600},'2':{main:60}},w);
  tasks.splice(0).forEach(fn=>fn());
  assert.equal(updates,1);
  const rendered=Grid(props);
  assert.equal(rendered.type,Original);
  assert.deepEqual(rendered.props.childSections[0].children.map(c=>c.m_overview.appid),[2,1]);
  setHltbRecords({'1':{main:3600},'2':{main:60}},w);
  assert.equal(tasks.length,0,'identical poll results must not redraw');
  w.storage.set('slsLunaHltbOrder','long');syncSortAs(w);
  tasks.splice(0).forEach(fn=>fn());
  assert.deepEqual(Grid(props).props.childSections[0].children.map(c=>c.m_overview.appid),[1,2]);
  Grid({...props,eSortBy:1});updates=0;
  setHltbRecords({'2':{main:90}},w);tasks.splice(0).forEach(fn=>fn());
  assert.equal(updates,0,'do not redraw another sort');
  cleanup();
  w.storage.delete('slsLunaHltbOrder');syncSortAs(w);
  assert.equal(tasks.length,0,'unmount removes subscriptions');
});

test('home sort stays view-local across several shelves and restores Steam sorter after errors',()=>{
  setHltbRecords({'1':{main:7200},'2':{main:120}});
  const w=sortableWindow(),apps=[{appid:1,sort_as:'a'},{appid:2,sort_as:'z'}];
  const alphabetic=c=>c.visibleApps.slice().sort((a,b)=>a.sort_as.localeCompare(b.sort_as));
  class Shelf {
    constructor(sort){this.props={showcase:{eSortBy:sort},collectionId:'x'};this.m_fnSort=alphabetic;}
    ComputeChildSizes(){}
    render(){const result=this.m_fnSort({visibleApps:apps});if(this.fail)throw Error('test');return result;}
    forceUpdate(){}
  }
  const hltb=new Shelf(12),normal=new Shelf(1);
  hookHltbShelf(Shelf,hltb.props,w);
  assert.deepEqual(hltb.render().map(a=>a.appid),[2,1]);
  assert.deepEqual(normal.render().map(a=>a.appid),[1,2]);
  withSortOption({rgOptions:SORT_ENUM,selectedOption:1,onChange(){}},w).onChange(1);
  w.storage.set('AppGridDisplaySettings','{"eSortBy":1}');syncSortAs(w);
  assert.deepEqual(hltb.render().map(a=>a.appid),[2,1],'foreign sort cannot undo HLTB');
  assert.equal(hltb.m_fnSort,alphabetic);
  hltb.fail=true;assert.throws(()=>hltb.render(),/test/);
  assert.equal(hltb.m_fnSort,alphabetic,'restore even on render failure');
  hltb.componentWillUnmount();normal.componentWillUnmount();
});

test('invalid and removed times stay last and merged sections keep every game once',()=>{
  setHltbRecords({'1':{main:Infinity},'2':{main:0,all:120},'3':{main:60,match:'none'}});
  const props={eSortBy:12,childSections:[{children:[{appid:1,name:'A'},{appid:2,name:'Z'}]},
    {children:[{appid:3,name:'B'}]}]};
  assert.deepEqual(withHltbSections(props,sortableWindow()).childSections[0].children.map(c=>c.appid),[2,1,3]);
  assert.deepEqual(withHltbSections(props,sortableWindow(undefined,'long')).childSections[0].children.map(c=>c.appid),[2,1,3]);
  assert.equal(withHltbSections({...props,childSections:[{children:[{}]}]}),null,'unknown shape fails closed');
});

test('only the real sort dropdown gains the HowLongToBeat option',()=>{
  const patched=withSortOption({rgOptions:SORT_ENUM.slice(),onChange(){},selectedOption:4});
  assert.ok(patched,'the sort dropdown must be recognized by its option values');
  assert.equal(patched.rgOptions.length,11);
  assert.deepEqual(patched.rgOptions[10],{data:SORT_VALUE,label:SORT_LABEL});
  assert.equal(withSortOption({rgOptions:patched.rgOptions}),null,'never appended twice and nothing to wrap');
  assert.equal(withSortOption({rgOptions:ENUMStrings()}),null,'string values are a different dropdown');
  assert.equal(withSortOption({rgOptions:SORT_ENUM.slice(0,3)}),null,'a short dropdown is not the sort one');
  assert.equal(withSortOption({rgOptions:SORT_ENUM.map(o=>({data:o.data+100,label:o.label}))}),null,
    'the numeric fingerprint is required');
  assert.equal(isSortDropdown({rgOptions:SORT_ENUM.slice()}),true);
  function ENUMStrings(){return [{data:'a',label:'x'},{data:'b',label:'y'}];}
});

test('the capsule overlay shows cached hours while the HLTB sort is active',()=>{
  setHltbRecords({'2':{main:7200},'3':{match:'none'}});
  class Overview{}
  Overview.prototype.GetCanonicalReleaseDate=()=>123;
  const app=Object.assign(new Overview(),{appid:2,minutes_playtime_forever:0});
  const result=withHltbOverlay({app,sortByType:SORT_VALUE,context:{},id:'row-1'});
  assert.ok(result,'the overlay must be re-pointed');
  assert.equal(result.sortByType,4,"Steam's hours-played branch must render it");
  assert.equal(result.app.minutes_playtime_forever,120,'the cached main story in minutes');
  assert.equal(result.app.GetCanonicalReleaseDate(),123,'the clone must keep the prototype');
  assert.equal(app.minutes_playtime_forever,0,'the original overview must stay untouched');
  assert.equal(hltbMinutesFor(2),120);
  assert.equal(withHltbOverlay({app:{appid:3,minutes_playtime_forever:0},sortByType:SORT_VALUE,id:'x'}),null,
    'a refused match must show nothing');
  assert.equal(withHltbOverlay({app:{appid:99},sortByType:SORT_VALUE,id:'x'}),null,
    'a game without times must show nothing');
  assert.equal(withHltbOverlay({app,sortByType:4,id:'x'}),null,'other sorts must be untouched');
  assert.equal(withHltbOverlay({app,sortByType:SORT_VALUE,id:'x',strCollectionId:'c'}),null,
    'the capsule itself must not be rewritten, only its overlay');
  assert.equal(withHltbOverlay({app,sortByType:SORT_VALUE,id:'x'})!==null,true);
  setHltbRecords({});
});

test('rendering the capsule overlay under the HLTB sort shows hours',()=>{
  setHltbRecords({'2':{main:7200}});
  const tools=fixture();
  const w={appStore:{GetAppOverviewByAppID:id=>({appid:id,display_name:'Beta'})}};
  const menu=installLunaMenu(w,tools);
  class Overlay{}
  const el=tools.jsx.jsx(Overlay,{app:{appid:2,minutes_playtime_forever:0},sortByType:SORT_VALUE,
    context:{},id:'row-1'});
  assert.equal(el.props.sortByType,4,'the hook must re-point the overlay');
  assert.equal(el.props.app.minutes_playtime_forever,120);
  const untouched=tools.jsx.jsx(Overlay,{app:{appid:2,minutes_playtime_forever:0},sortByType:4,
    context:{},id:'row-2'});
  assert.equal(untouched.props.app.minutes_playtime_forever,0,'normal sorts are untouched');
  menu.dispose();
  setHltbRecords({});
});

test('the rendered library dropdown carries the HowLongToBeat option',()=>{
  const tools=fixture();
  const w={appStore:{GetAppOverviewByAppID:id=>({appid:id,display_name:'Hades'})}};
  const menu=installLunaMenu(w,tools);
  class Dropdown{}
  const seenBefore=menu.sort.seen;
  const el=tools.jsx.jsx(Dropdown,{rgOptions:SORT_ENUM.slice(),onChange(){},selectedOption:4});
  assert.equal(el.props.rgOptions.length,11,'the rendered dropdown must gain the option');
  assert.equal(el.props.rgOptions[10].label,'HowLongToBeat');
  assert.equal(menu.sort.seen,seenBefore+1,'the sighting must be reported for verification');
  const game=tools.jsx.jsx(tools.Inner,{label:'Hades',labelId:'l',children:[
    {type:'play',props:{}},{type:'rg',props:{rgApps:[{appid:1145360}]}}]});
  assert.deepEqual(labelsAfterPrimary(game),['HowLongToBeat'],'game menus keep their items');
  menu.dispose();
});
