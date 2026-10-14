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
 * Actions: HowLongToBeat (opens the game's search page in the browser through
 * Steam's own URL handlers) and Fetch HowLongToBeat times (asks Luna's backend
 * for a user-triggered lookup and caches it locally; no library data leaves the
 * machine, and nothing is fetched automatically).
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
      id: 'hltb-fetch',
      label: 'Fetch HowLongToBeat times',
      isEnabled: app => Boolean(nameOf(app)) && valid(app && app.appid),
      run: (app, w) => rpc(w, 'SlsHltbFetch', { appid: Number(app.appid), name: nameOf(app) })
        .then(out => {
          if (!out || out.success !== true) {
            throw Error((out && out.error) || 'The HowLongToBeat fetch failed.');
          }
          return out.record;
        }),
    }];
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
     standalone and keeps working whatever else is installed. */
  function discoverMenuTools(w) {
    const chunks = w.webpackChunksteamui;
    if (!chunks || !chunks.push || !w.SP_REACT) return null;
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
    let jsx, item;
    for (const id of new Set(dependencies)) {
      const exports = require(id);
      if (exports && exports.jsx && exports.jsxs) jsx = exports;
      for (const value of Object.values(exports || {})) {
        if (typeof value !== 'function') continue;
        const source = String(value);
        if (value.prototype && value.prototype.OnClick && source.includes('contextMenuItem') && source.includes('onSelected')) item = value;
      }
    }
    return jsx && item ? { React: w.SP_REACT, jsx, item } : null;
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

    for (const key of ['jsx', 'jsxs']) {
      originals[key] = jsx[key];
      wrappers[key] = function (type, ...args) {
        const props = args[0];
        const proto = type && type.prototype;
        if (isOuter(proto) && props && props.overview) {
          // Fresh build of the game menu: remember the exact target before its
          // contents elements are created inside the same render pass.
          slot = { app: props.overview, instance: props.instance, ts: Date.now() };
        } else if (isInner(proto) && props && props.children != null) {
          const target = targetFor(props);
          if (target && target.app && valid(target.app.appid)) {
            const items = actionItems(target.app);
            if (items.length) args[0] = { ...props, children: insertActions(props.children, items) };
          }
        }
        return originals[key].call(this, type, ...args);
      };
      jsx[key] = wrappers[key];
    }

    const api = {
      version: 1,
      register,
      unregister,
      list: () => actions.map(entry => ({ id: entry.id, label: entry.label })),
      get attached() { return jsx.jsx === wrappers.jsx || jsx.jsxs === wrappers.jsxs; },
      ensure() {
        // The library UI can rebuild its bundle and replace the jsx functions
        // we hooked. Rebind if the module changed and re-arm on top of the
        // current chain, so Luna's actions cannot silently disappear.
        if (api.attached) return false;
        try {
          const fresh = discoverMenuTools(w);
          if (fresh && fresh.jsx !== jsx) jsx = fresh.jsx;
        } catch (_) { /* keep the previous module */ }
        restore();
        for (const key of ['jsx', 'jsxs']) {
          originals[key] = jsx[key];
          jsx[key] = wrappers[key];
        }
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
     leave the menu without Luna's actions. */
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
    if (typeof w.setInterval === 'function') w.setInterval(arm, 30000);
  }

  return { HLTB_SEARCH, discoverMenuTools, openExternalUrl, searchUrlFor, nameOf, defaultActions, installLunaMenu, bootLunaMenu };
});
