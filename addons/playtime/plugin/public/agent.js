/* Lumen SharedJSContext playtime agent. Uses Steam's lifetime notifications. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root && root.SteamClient) api.bootAgent(root);
})(typeof window !== 'undefined' ? window : null, function () {
  'use strict';
  const valid = id => Number.isSafeInteger(Number(id)) && Number(id) > 0 && Number(id) <= 4294967295;
  const parseRpc = raw => typeof raw === 'string' ? JSON.parse(raw) : raw;
  // Publish local totals into the same model Steam uses for its library cards.
  // Steam replaces these records on overview updates, so reapply after updates.
  function libraryAdapter(w) {
    const store=w.appStore||w.SteamUIStore?.AppStore;
    let games={},editable=new Set();
    const detailsByApp=new Map();
    const patchedDetails=new WeakSet();
    const eligible=o=>o&&(o.app_type===1073741824||editable.has(Number(o.appid)));
    const patch=o=>{
      const record=games[String(o?.appid)];
      if(!record||!eligible(o))return;
      o.minutes_playtime_forever=Math.max(0,(Number(record.trackedSeconds)||0)+(Number(record.adjustmentSeconds)||0))/60;
      // Only actual per-game history can populate the recent bucket.
      if(Number(record.lastPlayed)>0){
        o.rt_last_time_locally_played=Number(record.lastPlayed);
        o.rt_last_time_played=Number(record.lastPlayed);
        o.rt_last_time_played_or_installed=Number(record.lastPlayed);
      }
    };
    const apply=()=>{
      for(const id of Object.keys(games)){
        const o=store?.GetAppOverviewByAppID?.(Number(id));
        if(!eligible(o))continue;
        patch(o);
        store.m_mapApps?.set(o.appid,o);
      }
    };
    // Steam binds UpdateAppOverview as a sealed, read-only MobX action. Do not
    // assign to it: that aborts agent startup in strict mode. Hook the mutable
    // map insertion and AppInfoStore notification instead (as Decky does).
    const info=w.appInfoStore;
    if(store?.m_mapApps?.set){
      const map=store.m_mapApps,originalSet=map.set;
      map.set=function(id,overview){patch(overview);return originalSet.call(this,id,overview);};
    }
    if(info?.OnAppOverviewChange){
      const original=info.OnAppOverviewChange;
      info.OnAppOverviewChange=function(...args){
        const result=original.apply(this,args);
        // Steam calls this BEFORE updating its overview instances. A microtask
        // reapplies values after the complete synchronous update, including
        // in-place updates that never call Map.set.
        (w.queueMicrotask||queueMicrotask)(apply);
        return result;
      };
    }else if(store?.UpdateAppOverview&&Object.getOwnPropertyDescriptor(store,'UpdateAppOverview')?.writable!==false){
      const original=store.UpdateAppOverview;
      store.UpdateAppOverview=function(...args){const result=original.apply(this,args);apply();return result;};
    }
    function patchDetails(overview,details){
      if(!details||!eligible(overview))return;
      const record=games[String(overview.appid)];
      if(!record)return;
      detailsByApp.set(Number(overview.appid),{overview,details});
      // Manifest pages read this separate model, not the card overview.
      // Steam may refill this plain object after JSX creation. Preserve those
      // native writes, but resolve the local override each time it is read.
      if(!patchedDetails.has(details)&&Object.getOwnPropertyDescriptor(details,'nPlaytimeForever')?.configurable!==false){
        let native=details.nPlaytimeForever;
        Object.defineProperty(details,'nPlaytimeForever',{configurable:true,enumerable:true,
          get(){const local=games[String(overview.appid)];return local&&eligible(overview)?Math.max(0,(Number(local.trackedSeconds)||0)+(Number(local.adjustmentSeconds)||0))/60:native;},
          set(value){native=value;}});
        patchedDetails.add(details);
      }
      if(!patchedDetails.has(details))details.nPlaytimeForever=Math.max(0,(Number(record.trackedSeconds)||0)+(Number(record.adjustmentSeconds)||0))/60;
    }
    return {patchDetails,update(snapshot){games=snapshot.games||{};editable=new Set(Array.isArray(snapshot.apps)?snapshot.apps.map(Number):[]);apply();for(const {overview,details} of detailsByApp.values())patchDetails(overview,details);}};
  }
  function canSetPlaytime(app,snapshot) {
    // The backend's inventory is numeric stplug-in manifests + shortcuts, NOT
    // the Steam library/installed list (which also contains official games).
    return valid(app?.appid)&&Array.isArray(snapshot?.apps)&&snapshot.apps.some(id=>Number(id)===Number(app.appid));
  }
  function parseHours(text) {
    if(String(text).trim()==='')throw Error('Enter the total hours.');
    const hours=Number(text);
    if(!Number.isFinite(hours)||hours<0||hours>10000000)throw Error('Use a number between 0 and 10,000,000 hours.');
    return hours;
  }
  function discoverMenuTools(w) {
    const chunks=w.webpackChunksteamui;
    if(!chunks?.push||!w.SP_REACT)return null;
    let require;
    chunks.push([['sls-playtime-menu-'+Date.now()],{},r=>{require=r;}]);
    if(!require?.m)return null;
    const factory=Object.values(require.m).find(f=>String(f).includes('BuildManageSubmenu'));
    if(!factory)return null;
    // Only inspect dependencies of the already-loaded native game menu. Avoid
    // executing arbitrary webpack modules and avoid version-specific module ids.
    const dependencies=[...String(factory).matchAll(/\br\((\d+(?:e\d+)?)\)/g)].map(m=>Number(m[1]));
    let jsx,item,showModal;
    for(const id of new Set(dependencies)){
      const exports=require(id);
      if(exports?.jsx&&exports?.jsxs)jsx=exports;
      for(const value of Object.values(exports||{})){
        if(typeof value!=='function')continue;
        const source=String(value);
        if(value.prototype?.OnClick&&source.includes('contextMenuItem')&&source.includes('onSelected'))item=value;
        if(source.includes('RequestModalMeasure')&&source.includes('bForcePopOut'))showModal=value;
      }
    }
    return jsx&&item&&showModal?{React:w.SP_REACT,jsx,item,showModal}:null;
  }
  function installPlaytimeMenu(w,rpc,refreshLibrary,providedTools) {
    if(w.__SlsPlaytimeMenu)return w.__SlsPlaytimeMenu;
    let snapshot={apps:[]};
    const tools=providedTools||discoverMenuTools(w);
    if(!tools)return null;
    const {React,jsx,item:MenuItem,showModal}=tools;
    const h=React.createElement;
    async function open(app,instance){
      const latest=parseRpc(await rpc('SlsPlaytimeGet'));
      if(!latest?.success||!canSetPlaytime(app,latest))throw Error('This game is not eligible for local playtime editing.');
      snapshot=latest;
      const record=latest.games?.[String(app.appid)];
      const total=record?Math.max(0,(Number(record.trackedSeconds)||0)+(Number(record.adjustmentSeconds)||0))/3600:(Number(app.minutes_playtime_forever)||0)/60;
      function Dialog({closeModal}){
        const [value,setValue]=React.useState(String(Number(total.toFixed(4))));
        const [error,setError]=React.useState('');
        const [busy,setBusy]=React.useState(false);
        async function save(event){
          event.preventDefault();if(busy)return;
          try{
            const hours=parseHours(value);setBusy(true);setError('');
            const result=parseRpc(await rpc('SlsPlaytimeEdit',{raw:JSON.stringify({id:Number(app.appid),hours})}));
            if(!result?.success)throw Error(result?.error||'Playtime could not be saved.');
            const fresh=parseRpc(await rpc('SlsPlaytimeGet'));
            if(!fresh?.success)throw Error('Saved, but the library could not be refreshed.');
            snapshot=fresh;refreshLibrary(fresh);closeModal?.();
          }catch(e){setError(String(e.message||e));setBusy(false);}
        }
        const buttonStyle={padding:'9px 20px',border:0,borderRadius:3,color:'#fff',background:'#35465a',cursor:'pointer'};
        return h('form',{onSubmit:save,style:{padding:28,width:380,boxSizing:'border-box',color:'#dce7ef',background:'#1b2838',fontFamily:'Arial,sans-serif'}},
          h('h2',{style:{margin:'0 0 12px',color:'#fff'}},'Set Playtime'),
          h('div',{style:{marginBottom:20}},app.display_name||app.name||'App '+app.appid),
          h('label',{htmlFor:'sls-playtime-hours'},'Total hours'),
          h('input',{id:'sls-playtime-hours',type:'number',min:0,max:10000000,step:'any',autoFocus:true,disabled:busy,value,onChange:e=>setValue(e.target.value),style:{display:'block',boxSizing:'border-box',width:'100%',margin:'8px 0 14px',padding:10,color:'#fff',background:'#101822',border:'1px solid #65839c'}}),
          h('div',{style:{fontSize:13,color:'#9fb2c4',marginBottom:16}},'Sets the local lifetime total. Recorded sessions and recent activity are preserved.'),
          error&&h('div',{role:'alert',style:{color:'#ff9b9b',marginBottom:16}},error),
          h('div',{style:{display:'flex',justifyContent:'flex-end',gap:10}},
            h('button',{type:'button',disabled:busy,onClick:()=>closeModal?.(),style:buttonStyle},'Cancel'),
            h('button',{type:'submit',disabled:busy,style:{...buttonStyle,background:'#1a75b8'}},busy?'Saving…':'Save')));
      }
      const owner=instance?.BrowserWindow||w.SteamUIStore?.WindowStore?.MainWindowInstance?.BrowserWindow||w;
      return showModal(h(Dialog),owner,{strTitle:'Set Playtime',bForcePopOut:true,popupWidth:420,popupHeight:350});
    }
    // Both grid and sidebar use the native game menu. Patch its final contents
    // as well as updates: inserting only into the outer result can briefly
    // appear and then disappear when Steam refreshes/reuses the menu.
    const originals={},wrappers={};
    const patches=new Map();
    function target(props){
      const ids=new Set();
      function walk(value,depth=0){
        if(!value||depth>12)return;
        if(Array.isArray(value)){value.forEach(v=>walk(v,depth+1));return;}
        const p=value.props;
        if(!p)return;
        if(p.app?.appid)ids.add(Number(p.app.appid));
        if(Array.isArray(p.rgApps))p.rgApps.forEach(a=>ids.add(Number(a.appid)));
        walk(p.children,depth+1);
      }
      walk(props?.children);
      if(ids.size===1){const id=[...ids][0];return (w.appStore||w.SteamUIStore?.AppStore)?.GetAppOverviewByAppID?.(id)||{appid:id};}
      return ids.size===0?props?.__slsPlaytimeTarget?.app:null;
    }
    function insert(result,props){
      const app=target(props);
      if(!canSetPlaytime(app,snapshot)||!React.isValidElement(result))return result;
      const action=h(MenuItem,{key:'sls-set-playtime',onSelected:()=>{
        open(app,props.__slsPlaytimeTarget?.instance).catch(e=>console.error('[SLS Playtime] editor',e));
      }},'Set Playtime');
      const children=result.props.children;
      function remove(value){
        if(Array.isArray(value))return value.filter(v=>v?.key!=='sls-set-playtime').map(remove);
        return value;
      }
      const clean=remove(children);
      let placed=false;
      function afterPrimary(value){
        if(Array.isArray(value)){
          const out=[];
          for(const child of value){
            if(!placed&&Array.isArray(child))out.push(afterPrimary(child));
            else {
              out.push(child);
              if(!placed&&React.isValidElement(child)){out.push(action);placed=true;}
            }
          }
          return out;
        }
        if(React.isValidElement(value)){placed=true;return [value,action];}
        return value;
      }
      const ordered=afterPrimary(clean);
      return React.cloneElement(result,{children:placed?ordered:[action,clean]});
    }
    function hook(proto,kind){
      if(patches.has(proto))return;
      const originalRender=proto.render;
      const wrappedRender=function(...a){
        const result=originalRender.apply(this,a);
        if(kind==='outer'){
          if(!React.isValidElement(result))return result;
          const apps=this.GetTargetApps();
          return React.cloneElement(result,{__slsPlaytimeTarget:apps.length===1?{app:apps[0],instance:this.props.instance}:null});
        }
        return insert(result,this.props);
      };
      const originalUpdate=proto.shouldComponentUpdate;
      const wrappedUpdate=function(nextProps,...a){
        const decision=originalUpdate?originalUpdate.call(this,nextProps,...a):true;
        return canSetPlaytime(target(nextProps),snapshot)||decision;
      };
      proto.render=wrappedRender;
      if(kind==='inner')proto.shouldComponentUpdate=wrappedUpdate;
      patches.set(proto,{originalRender,wrappedRender,originalUpdate,wrappedUpdate,kind});
    }
    function restoreDiscovery(){for(const key of ['jsx','jsxs'])if(jsx[key]===wrappers[key])jsx[key]=originals[key];}
    for(const key of ['jsx','jsxs']){
      originals[key]=jsx[key];
      wrappers[key]=function(type,...args){
        const props=args[0];
        if(props?.details){
          const overview=props.overview||(w.appStore||w.SteamUIStore?.AppStore)?.GetAppOverviewByAppID?.(Number(props.details.unAppID));
          if(overview)w.__SlsPlaytimeLibrary?.patchDetails?.(overview,props.details);
        }
        const proto=type?.prototype;
        if(proto?.GetTargetApps&&proto?.BuildManageSubmenu&&proto?.render){
          hook(proto,'outer');
        }else if(proto?.HideIfSubmenu&&proto?.HideMenu&&proto?.render){
          hook(proto,'inner');
        }
        return originals[key].call(this,type,...args);
      };
      jsx[key]=wrappers[key];
    }
    const api={update(value){snapshot=value;},open,get attached(){return [...patches.values()].some(p=>p.kind==='inner');},dispose(){restoreDiscovery();for(const [proto,p]of patches){if(proto.render===p.wrappedRender)proto.render=p.originalRender;if(proto.shouldComponentUpdate===p.wrappedUpdate){if(p.originalUpdate)proto.shouldComponentUpdate=p.originalUpdate;else delete proto.shouldComponentUpdate;}}delete w.__SlsPlaytimeMenu;}};
    w.__SlsPlaytimeMenu=api;
    return api;
  }
  let sessionCounter = 0;
  /* Heartbeat protocol: every session carries an opaque id and its requests
     carry a sequence number that only advances once the backend acknowledged
     it. A lost response therefore retries the SAME sequence, and the backend
     answers success without counting the interval again. */
  class SessionAgent {
    constructor({rpc, now = () => Date.now(), mono = () => performance.now(), name = () => '',
                 newSessionId = id => `${id}-${Date.now().toString(36)}-${(++sessionCounter).toString(36)}-${Math.random().toString(36).slice(2, 8)}`}) {
      this.rpc=rpc;this.now=now;this.mono=mono;this.name=name;this.newSessionId=newSessionId;
      this.allowed=new Set();this.active=new Map();this.errors=[];this.lastError='';
    }
    setAllowed(ids) {this.allowed=new Set((Array.isArray(ids)?ids:[]).filter(valid).map(Number));}
    async event(notification) {
      const id=Number(notification?.unAppID);
      if(!valid(id))return;
      if(notification.bRunning===true && this.allowed.has(id))return this.start(id);
      if(notification.bRunning===false)return this.stop(id);
    }
    title(id){return String(this.name(id)||'').slice(0,160);}
    fail(error){
      const text=String(error?.message||error);
      this.lastError=text;
      this.errors.push(text);
      if(this.errors.length>20)this.errors.shift();
    }
    async send(data, entry) {
      const payload={...data};
      // A start opens the session; pulses and the stop consume sequence numbers.
      if(payload.kind!=='start')payload.seq=entry.ack+1;
      const response=parseRpc(await this.rpc('SlsPlaytimeHeartbeat',{raw:JSON.stringify(payload)}));
      if(response?.success!==true){
        const error=Error(response?.error||'Heartbeat rejected');
        error.code=response?.code||'';
        throw error;
      }
      // Only a committed sequence is advanced. A failed attempt keeps the same
      // number so the retry is recognised as a replay instead of new time.
      if(payload.kind!=='start')entry.ack=payload.seq;
      return response;
    }
    async start(id) {
      if(this.active.has(id))return false;
      const entry={sid:this.newSessionId(id),wall:this.now(),mono:this.mono(),started:false,busy:Promise.resolve(),closing:false,ack:0};
      this.active.set(id,entry);
      entry.busy=this.send({kind:'start',id,sid:entry.sid,title:this.title(id)},entry)
        .then(result => {entry.started=true;return result;});
      try {
        await entry.busy;
        return true;
      } catch(e){
        // Do not forget a running game after a transient RPC/disk failure.
        // The next pulse or stop retries the same session handshake.
        entry.busy=Promise.resolve();this.fail(e);return false;
      }
    }
    async record(id,kind) {
      const entry=this.active.get(id);if(!entry)return false;
      if (kind==='pulse' && entry.closing) return false;
      if (kind==='stop') entry.closing=true;
      const run=async()=>{
        // A pulse already queued behind Stop must never resurrect the session.
        if (this.active.get(id)!==entry) return false;
        if (!entry.started) {
          await this.send({kind:'start',id,sid:entry.sid,title:this.title(id)},entry);
          entry.started=true;
          // No time is counted before a start was acknowledged.
          entry.wall=this.now();entry.mono=this.mono();
        }
        const wall=Math.max(0,(this.now()-entry.wall)/1000);
        const mono=(this.mono()-entry.mono)/1000;
        const elapsed=Math.max(0,Math.min(30,wall,Number.isFinite(mono)&&mono>=0?mono:wall));
        await this.send({kind,id,sid:entry.sid,elapsed},entry);
        entry.wall=this.now();entry.mono=this.mono();
        if(kind==='stop')this.active.delete(id);
        return true;
      };
      entry.busy=entry.busy.catch(()=>{}).then(run);
      try {return await entry.busy;}
      catch(e){
        // The backend authoritatively has no such session, so there is nothing
        // left to stop and retrying forever would only collect errors.
        if(kind==='stop'&&e?.code==='no_session'){this.active.delete(id);return true;}
        this.fail(e);
        // A failed stop RPC remains retryable by a later stop event. A failed
        // pulse does not mark the session stopped.
        if(kind==='stop') entry.closing=false;
        return false;
      }
    }
    async pulse(){for(const id of [...this.active.keys()])await this.record(id,'pulse');}
    async stop(id){return this.record(id,'stop');}
  }
  function bootAgent(w) {
    if(w.__SlsPlaytimeAgent)return;
    const client=w.SteamClient;
    const rpc=async (method,args={}) => {
      if(!w.Millennium?.callServerMethod)throw Error('Lumen bridge unavailable');
      return w.Millennium.callServerMethod('lumen',method,args);
    };
    if(!client?.GameSessions?.RegisterForAppLifetimeNotifications ||
       !w.Millennium?.callServerMethod){
      console.warn('[SLS Playtime] Steam lifetime notifications or Lumen RPC unavailable');
      if(w.Millennium?.callServerMethod) {
        Promise.resolve(rpc('SlsPlaytimeReport',{raw:JSON.stringify({healthy:false,
          error:'Steam lifetime notifications unavailable'})})).catch(()=>{});
      }
      return;
    }
    w.__SlsPlaytimeAgent=true;
    const overview=id=>w.appStore?.GetAppOverviewByAppID?.(id)||w.SteamUIStore?.AppStore?.GetAppOverviewByAppID?.(id);
    // The Steam shell window has no appStore, so the panel can never resolve a
    // game name there. This context can, so it reports the names it finds and
    // the backend hands them to the panel.
    const canResolveNames=!!(w.appStore?.GetAppOverviewByAppID||w.SteamUIStore?.AppStore?.GetAppOverviewByAppID);
    const agent=new SessionAgent({rpc,name:id=>overview(id)?.display_name||overview(id)?.name||''});
    const library=libraryAdapter(w);
    w.__SlsPlaytimeLibrary=library;
    let menu=null;
    try{menu=installPlaytimeMenu(w,rpc,value=>library.update(value));}
    catch(e){console.warn('[SLS Playtime] context menu unavailable',e);}
    function resolveTitles(ids){
      const titles={};
      if(!canResolveNames)return titles;
      for(const id of (Array.isArray(ids)?ids:[]).slice(0,2000)){
        const found=overview(Number(id))?.display_name;
        if(typeof found==='string'&&found!=='')titles[String(id)]=found.slice(0,160);
      }
      return titles;
    }
    let refreshPromise=null;
    // Surface repeated heartbeat failures in the panel instead of retrying
    // forever with no visible reason.
    let consecutiveErrors=0,reportedError='';
    function noteResult(ok,error){
      if(ok){consecutiveErrors=0;return;}
      consecutiveErrors++;
      const text=String(error||'heartbeat failed').slice(0,250);
      if(text===reportedError&&consecutiveErrors!==3)return;
      reportedError=text;
      Promise.resolve(rpc('SlsPlaytimeReport',{raw:JSON.stringify({healthy:false,error:text,
        consecutiveErrors})})).catch(()=>{});
    }
    async function refresh(){
      if(refreshPromise)return refreshPromise;
      refreshPromise=(async()=>{
      try{
        const value=parseRpc(await rpc('SlsPlaytimeGet'));
        if(!value?.success)throw Error(value?.error||'Manifest inventory unavailable');
        // trackedApps is the set nothing else records: non-Steam shortcuts.
        // Manifest games stay editable but are not auto-counted.
        agent.setAllowed(value.trackedApps??value.apps);
        library.update(value);
        menu?.update(value);
        refreshTitles(value.apps,Object.keys(value.titles||{}).length);
        await rpc('SlsPlaytimeReport',{raw:JSON.stringify({healthy:consecutiveErrors===0,
          error:consecutiveErrors===0?'':reportedError})});
      } catch(e){
        console.warn('[SLS Playtime] manifest refresh failed',e);
        noteResult(false,e);
      } finally{refreshPromise=null;}
      })();
      return refreshPromise;
    }
    // Display names are cosmetic: a failure here must never mark the tracker
    // unhealthy or interrupt recording. They are re-sent whenever the ledger
    // holds fewer names than this context can resolve, so a ledger reset, an
    // import or a re-injected page heals itself.
    let lastTitlesJson='';
    async function refreshTitles(ids,known){
      if(!canResolveNames)return;
      try{
        const titles=resolveTitles(ids);
        const count=Object.keys(titles).length;
        if(count===0)return;
        // Nothing to add when the ledger already holds at least as many names
        // as this context can resolve; a missing cache is what triggers a resend.
        if(Number(known||0)>=count)return;
        const encoded=JSON.stringify(titles);
        if(encoded===lastTitlesJson)return;
        // Recorded before the await so two overlapping refreshes cannot both
        // decide to resend the same list.
        lastTitlesJson=encoded;
        await rpc('SlsPlaytimeTitles',{raw:JSON.stringify({titles})});
      } catch(e){
        lastTitlesJson='';
        console.warn('[SLS Playtime] game names could not be reported',e);
      }
    }
    client.GameSessions.RegisterForAppLifetimeNotifications(n=>{
      const id=Number(n?.unAppID);
      const tracked=valid(id)&&(agent.active.has(id)||agent.allowed.has(id));
      agent.event(n).then(ok=>{if(tracked)noteResult(true);},e=>{noteResult(false,e);console.warn('[SLS Playtime] event error',e);});
    });
    refresh();
    w.setInterval(refresh,30000);
    w.setInterval(()=>{agent.pulse().then(()=>{if(agent.active.size)noteResult(true);},e=>{noteResult(false,e);console.warn('[SLS Playtime] pulse failed',e);});},10000);
  }
  return {SessionAgent,parseRpc,bootAgent,libraryAdapter,canSetPlaytime,parseHours,discoverMenuTools,installPlaytimeMenu};
});
