/* End-to-end lifecycle tests for the injected agent.
 *
 * These drive the real bootAgent() against a fake SteamClient + a fake Lumen
 * backend, so the wire contract (JSON strings over callServerMethod, the {raw}
 * argument shape, the 5 s sync loop) is exercised rather than mocked away.
 * They still prove nothing about the real client; see docs/TEST_PLAN.md.
 */
const {test} = require('node:test');
const assert = require('node:assert/strict');
const agent = require('../../addons/presence/plugin/public/agent.js');
const {PresenceController, shortcutGameId, probeCapabilities, probeContracts,
  makeHidingStrategy, readAccountKey, readAccountIdentity, planReconciliation,
  fnv1a, sessionLabel, validId, HELPER} = agent;

const GAME = 2101960;              // "Cronos: The New Dawn": manifest-managed, installed
const GAME_TITLE = 'Cronos: The New Dawn';
const OTHER_GAME = 620;            // a normal owned game: never touched

// ---------------------------------------------------------------- fake Steam
function fakeWorld(opts = {}) {
  const log = [];
  const overviews = new Map([[GAME, {display_name: GAME_TITLE, appid: GAME}]]);
  const hidden = new Set();
  const running = new Set();
  const realGames = new Set();
  const launchOptions = new Map();
  const selectionOrder = opts.selection ? [GAME] : [];
  let nextShortcut = opts.firstShortcutId || 3000000000;
  const state = {
    enabled: false, sessionTime: true, owned: {}, running: {}, cleanup: 0, account: null,
  };
  const titles = {};
  const reports = [];
  let stored = JSON.stringify(state);

  const apps = {
    // Models the verified client behaviour: the returned AppID is a unique
    // allocation, the visible name initially comes from the executable, and the
    // 3rd argument does NOT become the helper's runtime arguments.
    AddShortcut: async (name, exe, args, dir) => {
      log.push({op: 'add', name, exe, args, dir});
      if (opts.failAdd) throw new Error('AddShortcut rejected');
      const id = nextShortcut++;
      overviews.set(id, {display_name: String(exe).split('/').pop(), appid: id});
      return id;
    },
    SetShortcutName: (id, name) => {
      log.push({op: 'setName', id, name});
      if (opts.failRename) throw new Error('SetShortcutName rejected');
      const ov = overviews.get(id) || {appid: id};
      overviews.set(id, {...ov, display_name: name});
    },
    SetShortcutLaunchOptions: (id, value) => {
      log.push({op: 'setOptions', id, value});
      if (opts.failOptions) throw new Error('SetShortcutLaunchOptions rejected');
      launchOptions.set(id, value);
    },
    RemoveShortcut: async id => {
      log.push({op: 'remove', id});
      if (opts.failRemove) throw new Error('RemoveShortcut rejected');
      overviews.delete(id);
      hidden.delete(id);
    },
    RunGame: async (...args) => {
      log.push({op: 'run', gameid: args[0]});
      if (opts.failRun) throw new Error('RunGame rejected');
      running.add(args[0]);
      if (opts.selection) {
        const id = Number(BigInt(args[0]) >> 32n);
        selectionOrder.unshift(id);
      }
    },
    TerminateApp: async (...args) => {
      log.push({op: 'terminate', gameid: args[0]});
      if (opts.failTerminate) throw new Error('Steam busy');
      running.delete(args[0]);
    },
  };
  if (opts.hideApi !== 'none') {
    apps.SetShortcutHidden = async (id, value) => {
      log.push({op: 'setShortcutHidden', id, value});
      if (opts.failHide) throw new Error('cannot hide');
      if (value) hidden.add(id); else hidden.delete(id);
    };
  }
  const collections = {
    SetAppsAsHidden: async (ids, value) => {
      const list = Array.isArray(ids) ? ids : [ids];
      log.push({op: 'setAppsAsHidden', ids: list, value});
      if (opts.failHide) throw new Error('cannot hide');
      for (const id of list) { if (value) hidden.add(id); else hidden.delete(id); }
    },
    BIsHidden: id => hidden.has(id),
  };
  const steamId = opts.steamId === undefined ? '76561198000000000' : opts.steamId;

  const registry = {
    SlsPresenceGet: () => JSON.stringify({
      success: true, state: JSON.parse(stored), apps: opts.apps || [GAME], titles,
      session: opts.session,
      runtime: reports.at(-1) || {active: 0, healthy: false},
    }),
    SlsPresencePut: raw => {
      const obj = JSON.parse(raw);
      const current = JSON.parse(stored);
      // The real backend lets the UI own enabled/sessionTime/cleanup while the
      // agent only replaces the ledger; mirror that here so a persist cannot
      // reset a setting the test just changed.
      stored = JSON.stringify({...obj,
        enabled: current.enabled, sessionTime: current.sessionTime, cleanup: current.cleanup});
      return JSON.stringify({success: true});
    },
    SlsPresenceSetEnabled: value => {
      const parsed = JSON.parse(stored);
      stored = JSON.stringify({...parsed, enabled: value});
      return JSON.stringify({success: true, enabled: value});
    },
    SlsPresenceReport: raw => { reports.push(JSON.parse(raw)); return JSON.stringify({success: true}); },
    SlsPresenceProbeProcesses: () => {
      const helpers = [...running].map(gid => Number(BigInt(gid) >> 32n));
      return JSON.stringify({success: true, complete: true, games: [...realGames, ...helpers], helpers});
    },
    SlsPresenceSetTitles: raw => { Object.assign(titles, JSON.parse(raw)); return JSON.stringify({success: true}); },
    SlsPresenceRequestCleanup: () => JSON.stringify({success: true, cleanup: 1}),
    SlsPresenceAcknowledgeCleanup: () => JSON.stringify({success: true}),
  };
  const timers = [];
  const win = {
    SteamClient: {
      Apps: apps,
      GameSessions: {RegisterForAppLifetimeNotifications: cb => { win.lifetime = cb; return 1; }},
      User: {GetSteamID: typeof steamId === 'function' ? steamId : () => steamId},
    },
    appStore: {GetAppOverviewByAppID: id => {
      const o = overviews.get(id);
      return o ? {...o, app_type: o.app_type ?? (id >= 2147483648 ? 1073741824 : 1)} : null;
    }},
    collectionStore: opts.collectionStore === false ? undefined : collections,
    Millennium: {callServerMethod: async (_plugin, method, params = {}) => {
      if (!registry[method]) return JSON.stringify({success: false, error: 'unknown method: ' + method});
      return registry[method](params.raw, params);
    }},
    setInterval: (fn, ms) => { timers.push({fn, ms}); return timers.length; },
  };
  if (opts.selection) win.SteamUIStore = {
    get MainRunningAppID() { return selectionOrder[0]; },
    get RunningApps() { return selectionOrder.map(appid => overviews.get(appid) || {appid}); },
    SetRunningApp(id) {
      log.push({op: 'select', id});
      const index = selectionOrder.indexOf(id);
      if (index < 0) return;
      selectionOrder.splice(index, 1);
      selectionOrder.unshift(id);
    },
  };
  return {
    win, log, overviews, hidden, running, realGames, reports, timers, launchOptions, selectionOrder,
    get state() { return JSON.parse(stored); },
    set state(value) { stored = JSON.stringify(value); },
    deliver: notification => {
      if (notification.bRunning) realGames.add(notification.unAppID);
      else realGames.delete(notification.unAppID);
      return win.lifetime(notification);
    },
    // One periodic sync plus a bounded drain: bootAgent() already kicks a sync
    // that may still be in flight, and the storage writes are promise-chained.
    tick: async (rounds = 10) => {
      for (const t of timers) await t.fn();
      for (let i = 0; i < rounds; i++) await flushed();
    },
    gameId: id => shortcutGameId(id),
  };
}

const flushed = () => new Promise(resolve => setImmediate(resolve));
// ------------------------------------------------- 18. poll cadence (perf)
test('the poll backs off while idle, speeds up on a running-app event, and reports it at once', async () => {
  const w = fakeWorld();
  w.state = {...w.state, enabled: true};
  agent.bootAgent(w.win);
  await w.tick();
  // The 60 s name refresh is a separate timer; this is the sync one.
  const syncTimer = () => w.timers.filter(t => t.ms === 40000 || t.ms === 5000).at(-1);
  assert.equal(syncTimer().ms, 40000, 'idle polling must be slow');
  const before = w.reports.length;
  await w.deliver({unAppID: GAME, bRunning: true});
  assert.equal(syncTimer().ms, 5000, 'a running game is polled every five seconds');
  assert.ok(w.reports.length > before, 'the start is reported without waiting for a poll');
  assert.equal(w.reports.at(-1).active, 1);
  await w.deliver({unAppID: GAME, bRunning: false});
  assert.equal(syncTimer().ms, 40000, 'a stop returns the poll to the idle cadence');
  // Only the newest timer is live: a host that cannot clear an interval must
  // not end up syncing once per obsolete cadence.
  const stale = w.timers.length - 1;
  const inner = w.win.Millennium.callServerMethod;
  let gets = 0;
  w.win.Millennium.callServerMethod = async (...args) => {
    if (args[1] === 'SlsPresenceGet') gets++;
    return inner(...args);
  };
  for (const timer of w.timers.slice(0, stale)) await timer.fn();
  assert.equal(gets, 0, 'superseded timers are inert');
  await syncTimer().fn();
  assert.equal(gets, 1, 'the live timer still syncs');
});

test('the poll pauses while the document is hidden and resumes on visibility', async () => {
  const w = fakeWorld();
  let onVisibility = null;
  w.win.document = {hidden: true, addEventListener: (type, fn) => { if (type === 'visibilitychange') onVisibility = fn; }};
  w.state = {...w.state, enabled: true};
  agent.bootAgent(w.win);
  await w.tick();
  const inner = w.win.Millennium.callServerMethod;
  let gets = 0;
  w.win.Millennium.callServerMethod = async (...args) => {
    if (args[1] === 'SlsPresenceGet') gets++;
    return inner(...args);
  };
  await w.timers.find(t => t.ms === 40000).fn();
  assert.equal(gets, 0, 'a hidden document does no polling');
  w.win.document.hidden = false;
  assert.equal(typeof onVisibility, 'function', 'visibility must be observed');
  await onVisibility();
  assert.equal(gets, 1, 'becoming visible syncs immediately');
  // Two overlapping fires share one RPC: never two syncs in flight.
  const timer = w.timers.filter(t => t.ms === 40000).at(-1);
  const first = timer.fn();
  const second = timer.fn();
  await Promise.all([first, second]);
  assert.equal(gets, 2, 'concurrent fires share a single sync');
});
