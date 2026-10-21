/* SLS Luna shared game-menu agent (SharedJSContext).
 *
 * Adds Luna's game actions to Steam's native right-click menu and publishes a
 * small registry (`window.__SlsLunaMenu`) so other Luna add-ons can contribute
 * their own per-game actions (artwork, graphics, ...) without each one having
 * to hook Steam's jsx layer on its own.
 *
 * The hook technique is the one already proven by the Playtime add-on: watch
 * the game context menu prototype while it renders, remember the focused game
 * from the outer submenu, and insert real menu-item children into the inner
 * menu. Nothing here touches game files; every action decides its own behavior
 * through the backend contract.
 *
 * Action: HowLongToBeat (opens the game's search page in the browser through
 * Steam's own URL handlers). Fetching and the cached times live in the Luna
 * menu's HowLongToBeat page; this agent only reports the local game inventory
 * so that page can show what is still missing. Nothing is fetched
 * automatically and no library data leaves the machine.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root && root.SteamClient) api.bootLunaMenu(root);
})(typeof window !== 'undefined' ? window : null, function () {
  'use strict';
  const ITEM_PREFIX = 'sls-luna-';
  const HLTB_SEARCH = 'https://howlongtobeat.com/?q=';
  const valid = id => Number.isSafeInteger(Number(id)) && Number(id) > 0 && Number(id) <= 4294967295;

  // A search query, never a scrape: HowLongToBeat has no supported public API,
  // so the browser does the lookup in the user's own session.
  function searchUrlFor(name) {
    return HLTB_SEARCH + encodeURIComponent(String(name == null ? '' : name).trim());
  }

  // Luna backend call through the Lumen bridge.
  function rpc(w, method, args) {
    if (!w || !w.Millennium || typeof w.Millennium.callServerMethod !== 'function') {
      return Promise.reject(Error('The Lumen bridge is unavailable.'));
    }
    return Promise.resolve(w.Millennium.callServerMethod('lumen', method, args || {}))
      .then(raw => (typeof raw === 'string' ? JSON.parse(raw) : raw));
  }

  // A per-game artwork request: the Luna panel (which owns the window's DOM)
  // listens for this localStorage key and opens the SteamGridDB picker on the
  // game. SharedJSContext has no DOM access, so localStorage is the hand-off.
  const ARTWORK_REQUEST_KEY = 'slsLunaArtworkRequest';
  // The panel asks for a live refresh after applying artwork; the agent is the
  // only context with SteamClient.Apps, so it performs the call and reports the
  // outcome back through another key.
  const ARTWORK_LIVE_KEY = 'slsLunaArtworkLive';
  const ARTWORK_LIVE_RESULT_KEY = 'slsLunaArtworkLiveResult';
  const SORT_LIVE_KEY = 'slsLunaSortLive';
  let lastArtworkLive = '';

  function publishArtworkRequest(w, app) {
    const appid = Number(app && app.appid);
    if (!valid(appid)) return false;
    const payload = JSON.stringify({ appid, name: nameOf(app), at: Date.now(),
      nonce: Math.floor(Math.random() * 1e9) });
    try {
      const storage = storageOf(w);
      if (!storage) return false;
      storage.setItem(ARTWORK_REQUEST_KEY, payload);
      return true;
    } catch (_) {
      return false;
    }
  }

  // Apply the already-written artwork through Steam's own API so the library
  // updates without a restart: the backend hands over the file's bytes, and
  // SetCustomArtworkForApp (confirmed live for types 0..3) does the rest.
  // One-shot: the live key is claimed (removed) on first read so a restart
  // never replays it; stale requests past the TTL are ignored.
  const ARTWORK_LIVE_TTL_MS = 5 * 60 * 1000;
  async function handleArtworkLive(w) {
    const storage = storageOf(w);
    if (!storage) return false;
    let raw = null;
    try { raw = storage.getItem(ARTWORK_LIVE_KEY); } catch (_) { return false; }
    if (!raw || raw === lastArtworkLive) return false;
    lastArtworkLive = raw;
    try { storage.removeItem(ARTWORK_LIVE_KEY); } catch (_) {}
    let request = null;
    try { request = JSON.parse(raw); } catch (_) { return false; }
    if (request && typeof request.at === 'number' && Number.isFinite(request.at)
      && Date.now() - request.at > ARTWORK_LIVE_TTL_MS) return false;
    const appid = Number(request && request.appid);
    const kinds = Array.isArray(request && request.kinds) ? request.kinds : [];
    if (!valid(appid) || !kinds.length) return false;
    const apps = w.SteamClient && w.SteamClient.Apps;
    const results = [];
    for (const kind of kinds) {
      try {
        if (!apps || typeof apps.SetCustomArtworkForApp !== 'function') {
          throw Error('Steam artwork API unavailable');
        }
        const live = await rpc(w, 'SlsSgdbLive', { raw: JSON.stringify({ appid, kind }) });
        if (!live || live.success !== true) {
          throw Error((live && live.error) || 'No live payload for this slot.');
        }
        await apps.SetCustomArtworkForApp(appid, live.base64, live.extension, Number(live.type));
        results.push({ kind, ok: true });
      } catch (error) {
        results.push({ kind, ok: false, error: String((error && error.message) || error) });
      }
    }
    try {
      storage.setItem(ARTWORK_LIVE_RESULT_KEY,
        JSON.stringify({ at: request.at || Date.now(), appid, results }));
    } catch (_) { /* the panel just will not see the outcome */ }
    return true;
  }

  // Beta pages live behind the Luna Settings switch; the same localStorage key
  // keeps the context menu honest (no artwork action while beta is off).
  function betaEnabled(w) {
    try {
      const storage = storageOf(w);
      return Boolean(storage && storage.getItem('slsLunaBeta') === '1');
    } catch (_) {
      return false;
    }
  }

  function defaultActions() {
    return [{
      id: 'hltb',
      label: 'HowLongToBeat',
      isEnabled: app => Boolean(nameOf(app)),
      run: (app, w) => openExternalUrl(w, searchUrlFor(nameOf(app))).then(opened => {
        if (!opened) throw Error('Steam could not open the browser for the HowLongToBeat search.');
        return true;
      }),
    }, {
      id: 'artwork',
      label: 'Change artwork',
      isEnabled: (app, w) => valid(app && app.appid) && betaEnabled(w),
      run: (app, w) => {
        if (!publishArtworkRequest(w, app)) {
          throw Error('Could not open the Luna artwork picker.');
        }
        return true;
      },
    }];
  }

  /* The owned games and non-Steam shortcuts this context can see, for the
     Luna panel's HowLongToBeat page. Only appid, display name and a shortcut
     flag leave this function; the list stays local. */
  function collectLibrary(w) {
    const store = w && (w.appStore || (w.SteamUIStore && w.SteamUIStore.AppStore));
    let overviews = [];
    try {
      if (store && Array.isArray(store.allApps)) overviews = store.allApps;
      else if (store && store.m_mapApps && typeof store.m_mapApps.values === 'function') {
        overviews = [...store.m_mapApps.values()];
      }
    } catch (_) { overviews = []; }
    const out = [];
    const seen = new Set();
    for (const overview of overviews) {
      if (!overview) continue;
      const appid = Number(overview.appid);
      if (!valid(appid) || seen.has(appid)) continue;
      const type = Number(overview.app_type);
      if (type !== 1 && type !== 1073741824) continue;
      const name = nameOf(overview);
      if (!name) continue;
      seen.add(appid);
      out.push({ appid, name: name.slice(0, 200), shortcut: type === 1073741824 });
      if (out.length >= 5000) break;
    }
    out.sort((a, b) => a.name.localeCompare(b.name));
    return out;
  }

  let lastLibraryPayload = '';

  function pushLibrary(w) {
    let payload;
    try {
      const games = collectLibrary(w);
      if (!games.length) return Promise.resolve(false);
      payload = JSON.stringify({ games });
    } catch (error) {
      console.warn('[SLS Luna] library inventory failed', error);
      return Promise.resolve(false);
    }
    if (payload === lastLibraryPayload) return Promise.resolve(false);
    return rpc(w, 'SlsHltbLibrary', { raw: payload })
      .then(out => {
        if (out && out.success === true) {
          lastLibraryPayload = payload;
          return true;
        }
        return false;
      })
      .catch(error => {
        console.warn('[SLS Luna] library inventory push failed', error);
        return false;
      });
  }

  function nameOf(app) {
    const name = app && (app.display_name || app.name);
    return typeof name === 'string' ? name.trim() : '';
  }

  /* Open an external URL in the user's default browser. Steam itself is the
     focused GUI app, so routing through its handlers (as Lumen's own cloud tab
     does) raises the browser under Wayland too. Tries the client APIs first and
     the Lumen relay last. Resolves true when a handler accepted the URL. */
  function openExternalUrl(w, url) {
    const target = String(url || '');
    if (!target) return Promise.resolve(false);
    try {
      const client = w && w.SteamClient;
      if (client && client.System && typeof client.System.OpenInSystemBrowser === 'function') {
        client.System.OpenInSystemBrowser(target);
        return Promise.resolve(true);
      }
      if (client && client.URL && typeof client.URL.ExecuteSteamURL === 'function') {
        client.URL.ExecuteSteamURL('steam://openurl_external/' + target);
        return Promise.resolve(true);
      }
    } catch (error) {
      console.warn('[SLS Luna] Steam browser handler failed', error);
    }
    if (w && w.Millennium && typeof w.Millennium.callServerMethod === 'function') {
      return Promise.resolve(w.Millennium.callServerMethod('lumen', '__lumenOpenExternalUrl', { url: target }))
        .then(raw => {
          const out = typeof raw === 'string' ? JSON.parse(raw) : raw;
          return Boolean(out && out.ok === true);
        })
        .catch(error => {
          console.warn('[SLS Luna] external URL relay failed', error);
          return false;
        });
    }
    return Promise.resolve(false);
  }

  /* Same discovery contract as Playtime's agent: push into the library webpack
     chunk, read the third (minified) require parameter of the native game-menu
     module, and inspect only that module's already-loaded dependencies. The
     lookups are independent per add-on on purpose: each payload stays
     standalone and keeps working whatever else is installed. Steam used to
     expose React as `window.SP_REACT`; a client update stopped doing that in
     SharedJSContext, which silently killed every jsx-based hook. When it is
     missing, React is resolved from the same module graph. */
  function reactFromModules(require) {
    try {
      for (const id of Object.keys(require.m)) {
        let exports;
        try { exports = require(id); } catch (_) { continue; }
        if (exports && typeof exports === 'object' && typeof exports.createElement === 'function'
            && typeof exports.isValidElement === 'function' && typeof exports.useState === 'function') {
          return exports;
        }
      }
    } catch (_) { /* no usable module graph */ }
    return null;
  }
  function discoverMenuTools(w) {
    const chunks = w.webpackChunksteamui;
    if (!chunks || !chunks.push) return null;
    let require;
    chunks.push([['sls-luna-menu-' + Date.now()], {}, r => { require = r; }]);
    if (!require || !require.m) return null;
    const factory = Object.values(require.m).find(f => String(f).includes('BuildManageSubmenu'));
    if (!factory) return null;
    const factorySource = String(factory);
    const signature = factorySource.match(/^\s*(?:function(?:\s+[\w$]+)?\s*)?\(([^)]*)\)/);
    const requireName = signature && signature[1].split(',')[2] && signature[1].split(',')[2].trim();
    if (!requireName || !/^[A-Za-z_$][\w$]*$/.test(requireName)) return null;
    const escapedRequire = requireName.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const dependencyPattern = new RegExp('\\b' + escapedRequire + '\\s*\\(\\s*(\\d+(?:e\\d+)?)\\s*\\)', 'g');
    const dependencies = [...factorySource.matchAll(dependencyPattern)].map(m => Number(m[1]));
    let jsx, item, React = w.SP_REACT;
    for (const id of new Set(dependencies)) {
      const exports = require(id);
      if (exports && exports.jsx && exports.jsxs) jsx = exports;
      for (const value of Object.values(exports || {})) {
        if (typeof value !== 'function') continue;
        const source = String(value);
        if (value.prototype && value.prototype.OnClick && source.includes('contextMenuItem') && source.includes('onSelected')) item = value;
      }
    }
    if (!React) React = reactFromModules(require);
    return jsx && item && React ? { React, jsx, item } : null;
  }

  /* ---- HowLongToBeat as a library sort option --------------------------
   * Steam builds "SORT BY" from a fixed enum and sorts through a comparator
   * factory whose exports are non-configurable webpack getters, so it cannot
   * be replaced. Unknown sort values fall back to Steam's alphabetical
   * comparator, which compares each game's `sort_as` field - and that field is
   * a plain writable property. Luna therefore appends its own option (value
   * 12) to the rendered dropdown and, while that option is active, rewrites
   * `sort_as` to a zero-padded inverted-time key; alphabetical comparison then
   * yields longest-main-story first, games without times last. Originals are
   * restored the moment another sort is chosen. If anything fails, Steam keeps
   * working with its normal sorts. */
  const SORT_VALUE = 12;
  const SORT_LABEL = 'HowLongToBeat';
  const SORT_ENUM_FINGERPRINT = [1, 4, 9, 11]; // alphabetical, hours, metacritic, review
  const HLTB_ORDER_KEY = 'slsLunaHltbOrder';   // 'short' (default) or 'long'
  let hltbRecords = {};
  let sortAsPatched = null; // [overview, originalSortAs] pairs while active
  let sortAsSeen = null;    // overviews already captured in this patch session
  let currentOrder = 'short';
  let sortSeen = 0;
  let liveSortValue;
  let hltbGridSeenAt = 0;   // last time a grid rendered as HLTB-sorted
  let lastHealAt = 0;

  function readStoredSort(w) {
    const storage = storageOf(w);
    try {
      return storage ? JSON.parse(storage.getItem('AppGridDisplaySettings') || '{}').eSortBy : null;
    } catch (_) {
      return null;
    }
  }

  // One small record of the last decision, published for the Health page and
  // for diagnosing "the order looks wrong" reports without a debugger.
  const SORT_DIAG_KEY = 'slsLunaSortDiag';
  function noteSortDiag(w, event) {
    try {
      const storage = storageOf(w);
      if (!storage) return;
      storage.setItem(SORT_DIAG_KEY, JSON.stringify({
        event: event,
        at: Math.round(Date.now() / 1000),
        seen: sortSeen,
        live: liveSortValue === undefined ? null : liveSortValue,
        gridAge: hltbGridSeenAt ? Math.round((Date.now() - hltbGridSeenAt) / 1000) : null,
        patched: Array.isArray(sortAsPatched) ? sortAsPatched.length : 0,
      }));
    } catch (_) { /* diagnostics only */ }
  }

  function setHltbRecords(records) {
    hltbRecords = records || {};
  }

  function hltbSecondsFor(appid) {
    const record = hltbRecords[String(appid)];
    if (!record || record.match === 'none') return null;
    const main = Number(record.main) || 0;
    const fallback = Number(record.all) || Number(record.extra) || 0;
    const value = main || fallback;
    return value > 0 ? value : null;
  }

  function hltbMinutesFor(appid) {
    const seconds = hltbSecondsFor(appid);
    return seconds ? Math.round(seconds / 60) : 0;
  }

  // Main-story seconds first, or longest first when the user prefers it;
  // games without times always sit at the end of either order.
  function hltbSortKey(appid, name) {
    const seconds = hltbSecondsFor(appid);
    const value = seconds == null ? 9999999999
      : (currentOrder === 'long' ? 9999999999 - seconds : seconds);
    return String(value).padStart(10, '0') + ':' + String(name || '');
  }

  function overviewsOf(w) {
    const store = w && (w.appStore || (w.SteamUIStore && w.SteamUIStore.AppStore));
    try {
      if (store && Array.isArray(store.allApps)) return store.allApps;
      if (store && store.m_mapApps && typeof store.m_mapApps.values === 'function') {
        return [...store.m_mapApps.values()];
      }
    } catch (_) { /* no store yet */ }
    return [];
  }

  function applyHltbSortAs(w) {
    currentOrder = readHltbOrder(w);
    const overviews = overviewsOf(w);
    if (!overviews.length) return false;
    if (!sortAsPatched) {
      sortAsPatched = [];
      sortAsSeen = new WeakSet();
    }
    let changed = false;
    for (const overview of overviews) {
      if (!overview || overview.appid == null) continue;
      const appid = Number(overview.appid);
      if (!valid(appid)) continue;
      const key = hltbSortKey(appid, overview.display_name || overview.name || '');
      // A second pass must update the key without capturing the patched value
      // as if it were the original.
      if (sortAsSeen.has(overview)) {
        if (overview.sort_as !== key) {
          try { overview.sort_as = key; changed = true; } catch (_) { /* fine */ }
        }
        continue;
      }
      let original;
      try {
        original = overview.sort_as;
        overview.sort_as = key;
      } catch (_) { continue; }
      sortAsSeen.add(overview);
      sortAsPatched.push([overview, original]);
      changed = true;
    }
    if (changed) keysVersion += 1;
    noteSortDiag(w, 'apply');
    return sortAsPatched.length > 0;
  }

  function restoreHltbSortAs() {
    if (!sortAsPatched) return false;
    for (const [overview, original] of sortAsPatched) {
      try {
        if (original === undefined) delete overview.sort_as;
        else overview.sort_as = original;
      } catch (_) { /* the overview moved on; leave it */ }
    }
    sortAsPatched = null;
    sortAsSeen = null;
    keysVersion += 1;
    return true;
  }

  function storageOf(w) {
    try {
      if (w && w.localStorage) return w.localStorage;
      if (typeof localStorage !== 'undefined') return localStorage;
    } catch (_) { /* blocked */ }
    return null;
  }

  function readHltbOrder(w) {
    const storage = storageOf(w);
    try {
      return storage && storage.getItem(HLTB_ORDER_KEY) === 'long' ? 'long' : 'short';
    } catch (_) {
      return 'short';
    }
  }

  // Keep the keys in step with the persisted sort choice: applied while
  // how-long-to-beat is selected, removed otherwise. Runs at boot, on every
  // beat (to catch newly loaded games) and when the dropdown reports a change.
  function syncSortAs(w) {
    const storage = storageOf(w);
    let current = null;
    try {
      current = storage ? JSON.parse(storage.getItem('AppGridDisplaySettings') || '{}').eSortBy : null;
    } catch (_) { current = null; }
    if (current === SORT_VALUE) return applyHltbSortAs(w);
    if (current === null || current === undefined) {
      // A 2026-10 Steam client update stopped persisting AppGridDisplaySettings
      // in localStorage, so "nothing stored" now means "no opinion", not "not
      // Luna". Re-apply while a view is still actually sorted by cached times
      // (the dropdown value the wrapper saw, or a grid rendering with Luna's
      // sort - the grid is the reliable signal, since a section's dropdown is
      // only rendered while its header is hovered) and otherwise leave the keys
      // exactly as they are: restoring them under a view that is still sorted by
      // cached times is what silently dropped the order. An explicit change
      // still restores them through the dropdown's onChange wrapper.
      const live = liveSortValue === SORT_VALUE
        || (hltbGridSeenAt > 0 && Date.now() - hltbGridSeenAt < 300000);
      if (live) return applyHltbSortAs(w);
      noteSortDiag(w, 'sync-keep');
      return false;
    }
    noteSortDiag(w, 'sync-restore');
    return restoreHltbSortAs();
  }

  // The sort dropdown is recognized by its option values, not its labels, so
  // this works in every language. Other dropdowns never match the fingerprint.
  function isSortDropdown(props) {
    if (!props || !Array.isArray(props.rgOptions) || props.rgOptions.length < 8) return false;
    const seen = new Set();
    for (const option of props.rgOptions) {
      const data = option && option.data;
      if (typeof data !== 'number') return false;
      seen.add(data);
    }
    return SORT_ENUM_FINGERPRINT.every(value => seen.has(value));
  }

  function withSortOption(props, w) {
    if (!isSortDropdown(props)) return null;
    sortSeen += 1;
    // Publish Steam's live sort value so the panel's label work reflects the
    // truth (Steam's own label lookup has no case for Luna's value, and other
    // views keep their own local sort state).
    if (props.selectedOption !== undefined && props.selectedOption !== liveSortValue) {
      liveSortValue = props.selectedOption;
      try {
        const storage = storageOf(w);
        if (storage) storage.setItem(SORT_LIVE_KEY, String(liveSortValue));
      } catch (_) { /* diagnostics only */ }
    }
    // Steam's state can drift from the stored choice (a view with its own sort
    // state, or a reset). When the stored setting is Luna's but the live value
    // is not, put the stored choice back - a throttled single call, never a
    // fight: if the user picks another sort, the stored setting changes with it
    // and this stops firing.
    if (props.selectedOption !== undefined && props.selectedOption !== SORT_VALUE
        && readStoredSort(w) === SORT_VALUE && typeof props.onChange === 'function'
        && Date.now() - lastHealAt > 10000) {
      lastHealAt = Date.now();
      const change = props.onChange;
      const schedule = (w && w.setTimeout) || setTimeout;
      schedule(() => { try { change(SORT_VALUE); } catch (_) { /* the next render retries */ } }, 400);
    }
    // Only the explicit paths may change the keys: the stored choice (kept in
    // step by syncSortAs on beats and storage events) and a pick through
    // onChange below.
    if (props.selectedOption === SORT_VALUE && !sortAsPatched) applyHltbSortAs(w);
    const appended = props.rgOptions.some(option => option && option.data === SORT_VALUE)
      ? props.rgOptions
      : props.rgOptions.concat([{ data: SORT_VALUE, label: SORT_LABEL }]);
    const onChange = typeof props.onChange === 'function'
      ? value => {
        if (value === SORT_VALUE) { noteSortDiag(w, 'change-apply'); applyHltbSortAs(w); }
        else { noteSortDiag(w, 'change-restore'); restoreHltbSortAs(); }
        return props.onChange(value);
      }
      : props.onChange;
    if (appended === props.rgOptions && onChange === props.onChange) return null;
    return { ...props, rgOptions: appended, onChange };
  }

  // Steam's grid memoizes the sorted order and `sort_as` is not observable.
  // Handing the grid a fresh array makes its memo recompute; each input array
  // is sliced once per key change (not once per render), so the order follows
  // the current keys without rebuilding the grid on every paint.
  let keysVersion = 0;
  let slicedVersion = 0;
  let slicedArrays = new WeakSet();

  function withFreshOverviews(props) {
    if (!props || !Array.isArray(props.appOverviews) || props.eSortBy === undefined) return null;
    if (slicedVersion !== keysVersion) {
      slicedVersion = keysVersion;
      slicedArrays = new WeakSet();
    }
    if (slicedArrays.has(props.appOverviews)) return null;
    slicedArrays.add(props.appOverviews);
    return { ...props, appOverviews: props.appOverviews.slice() };
  }

  // Steam's capsule overlay (the little value box on each library game) shows
  // the active sort's value, but returns null for unknown sort values. When
  // HowLongToBeat is active, re-point that overlay at Steam's own hours-played
  // rendering with the cached minutes, so each capsule reads like
  // "23.6 hours". Games without times keep Steam's default (nothing shown).
  // Diagnostics (which decisions were taken, and why one was skipped) land in
  // localStorage so a live client can be inspected without guessing.
  const OVERLAY_DIAG_KEY = 'slsLunaOverlayDiag';
  const overlayDiag = { rewrites: 0, misses: 0, seen: {}, reasons: {}, samples: [] };
  let overlayDiagAt = 0;

  function noteOverlayDiag(hit, props, reason) {
    if (hit) overlayDiag.rewrites += 1;
    else {
      overlayDiag.misses += 1;
      overlayDiag.reasons[reason] = (overlayDiag.reasons[reason] || 0) + 1;
      if (overlayDiag.samples.length < 5) {
        overlayDiag.samples.push(Object.keys(props || {}).sort().join(','));
      }
    }
    const key = String(props && props.sortByType);
    overlayDiag.seen[key] = (overlayDiag.seen[key] || 0) + 1;
    const now = Date.now();
    if (now - overlayDiagAt > 2000) {
      overlayDiagAt = now;
      try {
        const storage = storageOf();
        if (storage) storage.setItem(OVERLAY_DIAG_KEY, JSON.stringify(overlayDiag));
      } catch (_) { /* diagnostics only */ }
    }
  }

  function withHltbOverlay(props) {
    const decision = hltbOverlayDecision(props);
    noteOverlayDiag(decision.ok, props, decision.reason);
    return decision.ok ? decision.props : null;
  }

  function hltbOverlayDecision(props) {
    if (!props || props.sortByType !== SORT_VALUE) return { ok: false, reason: 'other_sort' };
    if (!props.app) return { ok: false, reason: 'no_app' };
    if (props.id === undefined) return { ok: false, reason: 'no_id' };
    if (props.strCollectionId !== undefined) return { ok: false, reason: 'has_collection' };
    const minutes = hltbMinutesFor(props.app.appid);
    if (!minutes) return { ok: false, reason: 'no_minutes' };
    const clone = Object.assign(Object.create(Object.getPrototypeOf(props.app)), props.app);
    clone.minutes_playtime_forever = minutes;
    return { ok: true, props: { ...props, sortByType: 4, app: clone } }; // 4 = Steam's hours-played sort
  }

  function refreshHltbData(w) {
    return rpc(w, 'SlsHltbGet').then(out => {
      if (out && out.success !== false) setHltbRecords(out.games || {});
      return true;
    }).catch(() => false);
  }

  /* Install the Luna game-menu hook. Returns the registry (or null while the
     library module is still loading). `providedTools` lets tests inject the
     jsx/item pair instead of a live webpack module. */
  function installLunaMenu(w, providedTools) {
    if (w.__SlsLunaMenu) return w.__SlsLunaMenu;
    const tools = providedTools || discoverMenuTools(w);
    if (!tools) return null;
    const React = tools.React;
    const MenuItem = tools.item;
    const h = React.createElement;
    let jsx = tools.jsx;
    let slot = null;
    const actions = [];
    const originals = {};
    const wrappers = {};

    function register(action) {
      if (!action || typeof action.id !== 'string' || action.id === '') return false;
      if (typeof action.label !== 'string' || action.label === '') return false;
      if (typeof action.run !== 'function') return false;
      const entry = { id: action.id, label: action.label, run: action.run, isEnabled: action.isEnabled };
      const index = actions.findIndex(existing => existing.id === action.id);
      if (index >= 0) actions[index] = entry; else actions.push(entry);
      return true;
    }

    function unregister(id) {
      const index = actions.findIndex(existing => existing.id === id);
      if (index < 0) return false;
      actions.splice(index, 1);
      return true;
    }

    const overviewOf = id => (w.appStore || (w.SteamUIStore && w.SteamUIStore.AppStore))
      ?.GetAppOverviewByAppID?.(Number(id)) || { appid: Number(id) };
    const isOuter = proto => Boolean(proto && proto.GetTargetApps && proto.BuildManageSubmenu && proto.render);
    const isInner = proto => Boolean(proto && proto.HideIfSubmenu && proto.HideMenu && proto.render);

    function scanIds(children, depth) {
      const ids = new Set();
      if ((depth || 0) > 12 || children == null) return ids;
      if (Array.isArray(children)) {
        for (const child of children) for (const id of scanIds(child, (depth || 0) + 1)) ids.add(id);
        return ids;
      }
      if (!React.isValidElement(children) || !children.props) return ids;
      const props = children.props;
      if (props.app && props.app.appid) ids.add(Number(props.app.appid));
      if (Array.isArray(props.rgApps)) for (const app of props.rgApps) if (app && app.appid) ids.add(Number(app.appid));
      for (const id of scanIds(props.children, (depth || 0) + 1)) ids.add(id);
      return ids;
    }

    function targetFor(props) {
      const ids = [...scanIds(props && props.children)];
      // A focused game names itself in the menu label; that is authoritative
      // even when child actions mention DLC or other apps. Bulk menus carry no
      // label and more than one app, so they get no per-game actions.
      const labeled = slot && Date.now() - slot.ts < 10000 && props && props.label && slot.app
        && slot.app.display_name === props.label;
      if (labeled) return slot;
      if (ids.length === 1) return { app: overviewOf(ids[0]), instance: slot && slot.instance };
      return null;
    }

    function enabledActions(app) {
      return actions.filter(action => {
        if (typeof action.isEnabled !== 'function') return true;
        try {
          return action.isEnabled(app, w) === true;
        } catch (error) {
          console.warn('[SLS Luna] action eligibility failed: ' + action.id, error);
          return false;
        }
      });
    }

    function itemKey(child) {
      if (!child) return '';
      if (typeof child.key === 'string') return child.key;
      if (child.props && typeof child.props.key === 'string') return child.props.key;
      return '';
    }

    function actionItems(app) {
      return enabledActions(app).map(action => h(MenuItem, {
        key: ITEM_PREFIX + action.id,
        onSelected: () => Promise.resolve()
          .then(() => action.run(app, w))
          .catch(error => console.error('[SLS Luna] action failed: ' + action.id, error)),
      }, action.label));
    }

    function insertActions(children, items) {
      const clean = (Array.isArray(children) ? children : [children])
        .filter(child => !itemKey(child).startsWith(ITEM_PREFIX));
      let placed = false;
      function afterPrimary(value) {
        if (Array.isArray(value)) {
          const out = [];
          for (const child of value) {
            if (!placed && Array.isArray(child)) out.push(afterPrimary(child));
            else {
              out.push(child);
              if (!placed && React.isValidElement(child)) {
                for (const item of items) out.push(item);
                placed = true;
              }
            }
          }
          return out;
        }
        if (React.isValidElement(value)) {
          placed = true;
          return [value].concat(items);
        }
        return value;
      }
      const ordered = afterPrimary(clean);
      return placed ? ordered : items.concat(Array.isArray(clean) ? clean : [clean]);
    }

    function restore() {
      for (const key of ['jsx', 'jsxs']) if (jsx[key] === wrappers[key]) jsx[key] = originals[key];
    }

    // Two add-ons hooking the same jsx module must not fight, and a Steam
    // rebuild may replace the hooked functions in place or with a new module.
    // The wrapper therefore stamps the module it wrapped (`__slsSeen.luna`)
    // every time it runs; ensure() probes the live chain with an inert element
    // and treats a fresh stamp as "we are still in the chain" (someone else may
    // sit on top - fine) while a stale one means a real rebuild. `base` is the
    // leaf function of the chain at install time; the re-entry guard uses it so
    // a wrapper cycle can never recurse without bound.
    let wrappedModule = null;
    let inRender = false;
    const base = {};

    function stampSeen() {
      if (!jsx || typeof jsx !== 'object') return;
      const seen = jsx.__slsSeen || (jsx.__slsSeen = {});
      seen.luna = Date.now();
    }

    for (const key of ['jsx', 'jsxs']) {
      originals[key] = jsx[key];
      base[key] = jsx[key];
      wrappers[key] = function (type, ...args) {
        stampSeen();
        // Re-entry means another wrapper called us while we were already
        // rendering (a cycle created by two add-ons re-arming). Fall back to
        // the leaf so rendering still completes, without inserting twice.
        const next = inRender && base[key] ? base[key] : originals[key];
        if (!inRender) {
          const props = args[0];
          // The library's SORT BY dropdown gains the cached-times option, and
          // the grid gets a fresh overviews array (so it re-sorts with the
          // current keys).
          if (props && Array.isArray(props.rgOptions)) {
            const withSort = withSortOption(props, w);
            if (withSort) args[0] = withSort;
          }
          if (props && Array.isArray(props.appOverviews)) {
            const fresh = withFreshOverviews(props);
            if (fresh) args[0] = fresh;
          }
          // While that option is active, each capsule shows its cached hours.
          if (args[0] && args[0].sortByType === SORT_VALUE) {
            hltbGridSeenAt = Date.now();
            const overlay = withHltbOverlay(args[0]);
            if (overlay) args[0] = overlay;
          }
          const proto = type && type.prototype;
          if (isOuter(proto) && props && props.overview) {
            // Fresh build of the game menu: remember the exact target before
            // its contents elements are created inside the same render pass.
            slot = { app: props.overview, instance: props.instance, ts: Date.now() };
          } else if (isInner(proto) && props && props.children != null) {
            const target = targetFor(props);
            if (target && target.app && valid(target.app.appid)) {
              const items = actionItems(target.app);
              if (items.length) args[0] = { ...props, children: insertActions(props.children, items) };
            }
          }
        }
        inRender = true;
        try {
          return next.call(this, type, ...args);
        } finally {
          inRender = false;
        }
      };
      jsx[key] = wrappers[key];
    }
    wrappedModule = jsx;

    const api = {
      version: 1,
      register,
      unregister,
      list: () => actions.map(entry => ({ id: entry.id, label: entry.label })),
      get sort() {
        return { value: SORT_VALUE, seen: sortSeen, games: Object.keys(hltbRecords).length,
          applied: Boolean(sortAsPatched), patched: sortAsPatched ? sortAsPatched.length : 0,
          order: currentOrder };
      },
      get attached() { return wrappedModule !== null && jsx === wrappedModule; },
      ensure() {
        // The library UI can rebuild its bundle: sometimes with a new jsx
        // module object, sometimes by replacing the hooked functions in place
        // on the same object. Probe the live chain with an inert element: if
        // Luna's wrapper runs it stamps the module, proving it is still in the
        // chain (possibly under another add-on's wrapper - that is fine). A
        // stale stamp means the functions were replaced and we must re-arm.
        let fresh = null;
        try {
          fresh = discoverMenuTools(w);
        } catch (_) { /* keep waiting for the next beat */ }
        if (!fresh) return false;
        const module = fresh.jsx;
        if (module.jsx === wrappers.jsx || module.jsxs === wrappers.jsxs) return false;
        try {
          module.jsx(function SlsLunaProbe() {}, {});
        } catch (_) { /* a broken chain still means we are not in it */ }
        const seen = module.__slsSeen && Number(module.__slsSeen.luna || 0);
        if (seen && Date.now() - seen < 5000) return false;
        jsx = module;
        restore();
        for (const key of ['jsx', 'jsxs']) {
          if (jsx[key] === wrappers[key]) jsx[key] = originals[key];
          originals[key] = jsx[key];
          base[key] = jsx[key];
          jsx[key] = wrappers[key];
        }
        wrappedModule = jsx;
        stampSeen();
        return true;
      },
      dispose() {
        restore();
        delete w.__SlsLunaMenu;
      },
    };
    for (const action of defaultActions()) register(action);
    w.__SlsLunaMenu = api;
    return api;
  }

  /* Boot in SharedJSContext: arm the menu now, retry while the library module
     is still loading, and re-arm on a slow beat so a Steam UI reload cannot
     leave the menu without Luna's actions. The game inventory is reported to
     the backend at boot (again after a minute, so a late Steam start still
     fills it) and refreshed on a slow beat. */
  function bootLunaMenu(w) {
    if (w.__SlsLunaAgent) return;
    w.__SlsLunaAgent = true;
    const arm = () => {
      try {
        if (!w.__SlsLunaMenu) installLunaMenu(w);
        if (w.__SlsLunaMenu) w.__SlsLunaMenu.ensure();
      } catch (error) {
        console.warn('[SLS Luna] menu install failed', error);
      }
    };
    arm();
    const beat = () => {
      arm();
      pushLibrary(w);
      refreshHltbData(w).then(() => syncSortAs(w));
    };
    if (typeof w.setInterval === 'function') w.setInterval(beat, 30000);
    if (typeof w.setTimeout === 'function') w.setTimeout(() => pushLibrary(w), 60000);
    // The panel writes the sort order and artwork requests in localStorage;
    // the storage event tells this context to react right away.
    if (typeof w.addEventListener === 'function') {
      w.addEventListener('storage', () => {
        syncSortAs(w);
        handleArtworkLive(w);
      });
    }
    pushLibrary(w);
    refreshHltbData(w).then(() => syncSortAs(w));
    handleArtworkLive(w);
  }

  return { HLTB_SEARCH, SORT_VALUE, SORT_LABEL, ARTWORK_REQUEST_KEY, ARTWORK_LIVE_KEY,
    ARTWORK_LIVE_RESULT_KEY, SORT_LIVE_KEY, discoverMenuTools, openExternalUrl,
    searchUrlFor, nameOf, betaEnabled, defaultActions, publishArtworkRequest, handleArtworkLive,
    collectLibrary, pushLibrary, rpc, setHltbRecords, hltbSecondsFor, hltbMinutesFor, hltbSortKey,
    readHltbOrder, applyHltbSortAs, restoreHltbSortAs, syncSortAs, isSortDropdown, withSortOption,
    withFreshOverviews, withHltbOverlay, refreshHltbData, installLunaMenu, bootLunaMenu };
});
