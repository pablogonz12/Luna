/* Lumen SharedJSContext playtime agent. Uses Steam's lifetime notifications. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root && root.SteamClient) api.bootAgent(root);
})(typeof window !== 'undefined' ? window : null, function () {
  'use strict';
  const valid = id => Number.isSafeInteger(Number(id)) && Number(id) > 0 && Number(id) <= 4294967295;
  const parseRpc = raw => typeof raw === 'string' ? JSON.parse(raw) : raw;
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
  return {SessionAgent,parseRpc,bootAgent};
});
