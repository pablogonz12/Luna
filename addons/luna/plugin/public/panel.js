/* SLS Luna: the shell entry point for the SLSsteam-Moon add-ons.
 *
 * Adds one half-moon "Luna" button to Steam's native menubar, next to Lumen's
 * own button, and one settings menu behind it. The menu is modelled on Lumen's
 * (same window size, sidebar, accent and account row) so the two read as one
 * product, and it hosts the settings of the add-ons instead of each add-on
 * drawing its own floating panel.
 *
 * The add-ons still work standalone: each one only skips its floating UI when
 * window.__SlsLunaUi is set, and this file sets it. */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  if (root && root.document) api.mount(root);
})(typeof window !== 'undefined' ? window : null, function () {
  'use strict';
  const BTN_ID = 'sls-luna-btn';
  const OVERLAY_ID = 'sls-luna-overlay';
  const STYLE_ID = 'sls-luna-styles';
  const ACCENT = '#1a9fff';
  // Inline 16px glyphs, matching the size of Lumen's sidebar icons.
  const ICON = {
    playtime: '<circle cx="12" cy="12" r="8" fill="none" stroke="currentColor" stroke-width="2"/>'
      + '<path d="M12 8v4l3 2" fill="none" stroke="currentColor" stroke-width="2"/>',
    presence: '<circle cx="12" cy="12" r="3" fill="currentColor"/>'
      + '<path d="M5 12a7 7 0 0 1 7-7m7 7a7 7 0 0 1-7 7" fill="none" stroke="currentColor" stroke-width="2"/>',
    compressor: '<path d="M4 8h16v11H4z" fill="none" stroke="currentColor" stroke-width="2"/>'
      + '<path d="M4 8l2-4h12l2 4M10 12h4" fill="none" stroke="currentColor" stroke-width="2"/>',
    hltb: '<circle cx="12" cy="12" r="8" fill="none" stroke="currentColor" stroke-width="2"/>'
      + '<path d="M12 7v5l3.5 2" fill="none" stroke="currentColor" stroke-width="2"/>'
      + '<path d="M4 5.5L6.5 3M20 5.5L17.5 3" stroke="currentColor" stroke-width="2"/>',
    steamgrid: '<rect x="4" y="4" width="7" height="7" rx="1" fill="none" stroke="currentColor" stroke-width="2"/>'
      + '<rect x="13" y="4" width="7" height="7" rx="1" fill="none" stroke="currentColor" stroke-width="2"/>'
      + '<rect x="4" y="13" width="7" height="7" rx="1" fill="none" stroke="currentColor" stroke-width="2"/>'
      + '<path d="M16.5 13.5v6M13.5 16.5h6" stroke="currentColor" stroke-width="2"/>',
    settings: '<circle cx="12" cy="12" r="3" fill="none" stroke="currentColor" stroke-width="2"/>'
      + '<path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1" stroke="currentColor" stroke-width="2"/>',
    graphics: '<rect x="3" y="4" width="18" height="12" rx="1" fill="none" stroke="currentColor" stroke-width="2"/>'
      + '<path d="M9 20h6M12 16v4M7 8h4M7 11h8" stroke="currentColor" stroke-width="2"/>',
    health: '<circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/>'
      + '<path d="M6.5 12h2.5l1.5-3 2.5 6 2-4 1 1h1.5" fill="none" stroke="currentColor" stroke-width="2"/>',
    about: '<circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/>'
      + '<path d="M12 11v5m0-8v.5" stroke="currentColor" stroke-width="2"/>',
  };
  // Beta pages stay hidden until the Settings switch is on.
  const NAV = [
    { id: 'playtime', label: 'Playtime' },
    { id: 'presence', label: 'Presence' },
    { id: 'compressor', label: 'Compressor' },
    { id: 'hltb', label: 'HowLongToBeat' },
    { id: 'steamgrid', label: 'SteamGridDB', beta: true },
    { id: 'graphics', label: 'Graphics', beta: true },
    { id: 'health', label: 'Health' },
    { id: 'settings', label: 'Settings' },
    { id: 'about', label: 'About' },
  ];
  // Floating panels the add-ons draw when Luna is not present.
  const FLOATING = ['sls-playtime-panel-button', 'sls-playtime-panel',
                    'sls-presence-panel-button', 'sls-presence-panel',
                    'sls-compressor-panel-button', 'sls-compressor-panel'];

  const parse = raw => (typeof raw === 'string' ? JSON.parse(raw) : raw);
  const hours = seconds => ((Number(seconds) || 0) / 3600).toFixed(2);
  /* The lifetime total the playtime backend publishes: for games Steam tracks
     itself the official playtime (localconfig.vdf, read-only) is the base and a
     manual edit is a correction on top of it; shortcuts, which Steam never
     records, keep the locally tracked count. */
  const lifetimeSeconds = record => {
    const official = Number(record && record.officialSeconds) || 0;
    const base = official > 0 ? official : (Number(record && record.trackedSeconds) || 0);
    return Math.max(0, base + (Number(record && record.adjustmentSeconds) || 0));
  };
  // Compact "how long ago" and "at what time" text for scan and job stamps.
  const age = seconds => {
    const value = Math.max(0, Math.floor(Date.now() / 1000 - (Number(seconds) || 0)));
    if (value < 90) return value + 's';
    if (value < 5400) return Math.round(value / 60) + 'm';
    return Math.round(value / 3600) + 'h';
  };
  const clock = seconds => {
    const date = new Date((Number(seconds) || 0) * 1000);
    return String(date.getHours()).padStart(2, '0') + ':' + String(date.getMinutes()).padStart(2, '0');
  };
  // Compressor page vocabulary: in-page sub-tabs, the alpha resolution
  // profiles, and plain-language names for actions and game states.
  const COMPRESSOR_TABS = [
    { id: 'status', label: 'Status' },
    { id: 'stats', label: 'Stats' },
    { id: 'settings', label: 'Settings' },
    { id: 'games', label: 'Games' },
    { id: 'log', label: 'Log' },
  ];
  const PROFILE_LABELS = [
    ['ultra-performance', '480p'],
    ['performance', '720p'],
    ['balanced', '1080p'],
    ['quality', '1440p'],
    ['ultra-quality', '4K'],
    ['lossless', 'Lossless'],
  ];
  const ACTION_LABELS = {
    scan: 'library scan', preview: 'preview', run: 'compression pass',
    recheck: 'measurement refresh', assets: 'asset inventory',
  };
  const STATUS_LABELS = {
    UNCOMPRESSED: 'uncompressed', UPDATED: 'changed since last pass', COMPRESSED: 'dedupe pending',
    COMPACTED: 'compacted', 'LOW YIELD': 'low yield', RUNNING: 'playing now',
  };
  // Lists can arrive as a real array or as an object keyed by index (an empty
  // Lua table and a dense one differ only in marking), so normalise both.
  const listOf = value => Array.isArray(value) ? value
    : value && typeof value === 'object' ? Object.keys(value).map(key => value[key]) : [];
  const formatBytes = bytes => {
    const value = Number(bytes) || 0;
    if (value < 1024) return value + ' B';
    const units = ['KiB', 'MiB', 'GiB', 'TiB'];
    let size = value;
    let unit = -1;
    while (size >= 1024 && unit < units.length - 1) { size /= 1024; unit += 1; }
    return (size >= 100 ? size.toFixed(0) : size.toFixed(1)) + ' ' + units[unit];
  };
  const pctText = value => String(Math.max(0, Math.min(100, Math.round(Number(value) || 0))));

  function styled(document) {
    if (document.getElementById(STYLE_ID)) return;
    const style = document.createElement('style');
    style.id = STYLE_ID;
    style.textContent = [
      '#' + BTN_ID + '{display:inline-flex;align-items:center;gap:6px;height:22px;padding:0 9px 0 0;margin:0 2px;',
      'cursor:pointer;font-size:11px;font-weight:700;line-height:1;color:#fff;opacity:.8;',
      'background:' + ACCENT + ';border-radius:999px;white-space:nowrap;overflow:hidden;',
      '-webkit-app-region:no-drag;user-select:none;',
      'transition:opacity .18s cubic-bezier(.4,0,.2,1),box-shadow .18s;}',
      '#' + BTN_ID + ':hover{opacity:1;box-shadow:0 0 0 1px rgba(102,192,244,.55);}',
      '#' + BTN_ID + ':focus-visible{opacity:1;outline:2px solid #66c0f4;outline-offset:1px;}',
      '.sls-luna-glyph{display:inline-flex;align-items:center;justify-content:center;width:22px;height:22px;flex:0 0 22px;}',
      '.sls-luna-glyph svg{display:block;width:15px;height:15px;}',
      // window: same proportions as Lumen's settings overlay
      '#' + OVERLAY_ID + '{position:fixed;inset:0;z-index:99999;display:flex;align-items:center;',
      'justify-content:center;background:rgba(0,0,0,.55);font-family:"Motiva Sans",Arial,Helvetica,sans-serif;}',
      '.sls-luna-win{display:flex;width:900px;max-width:94vw;height:620px;max-height:88vh;border-radius:4px;',
      'overflow:hidden;box-shadow:0 20px 60px rgba(0,0,0,.6);border:1px solid rgba(0,0,0,.5);}',
      '.sls-luna-side{flex:0 0 200px;background:#2a2d34;display:flex;flex-direction:column;padding-top:8px;}',
      '.sls-luna-side-title{color:' + ACCENT + ';font-size:17px;font-weight:700;text-transform:uppercase;padding:14px 24px 16px;}',
      '.sls-luna-tab{display:flex;align-items:center;gap:12px;padding:10px 8px 10px 24px;height:20px;color:#b8bcbf;',
      'font-size:14px;cursor:pointer;}',
      '.sls-luna-tab:hover{background:rgba(255,255,255,.04);}',
      '.sls-luna-tab.active{background:#3d4450;color:#fff;}',
      '.sls-luna-spacer{flex:1 1 auto;min-height:18px;}',
      '.sls-luna-account{display:flex;align-items:center;gap:10px;padding:13px 14px 14px;',
      'border-top:1px solid rgba(255,255,255,.07);color:#e6e9ec;font-size:13px;}',
      '.sls-luna-dot{width:8px;height:8px;border-radius:50%;flex:0 0 8px;background:#6b7280;}',
      '.sls-luna-dot.ok{background:#57cb5c;}',
      '.sls-luna-main{flex:1 1 auto;background:#1b1f24;color:#dcdedf;display:flex;flex-direction:column;min-width:0;}',
      '.sls-luna-head{display:flex;align-items:center;gap:12px;padding:18px 24px 12px;}',
      '.sls-luna-head h3{margin:0;font-size:22px;font-weight:400;color:#fff;flex:1 1 auto;}',
      '.sls-luna-x{background:rgba(255,255,255,.06);border:0;color:#cfd3d7;width:28px;height:28px;border-radius:3px;',
      'cursor:pointer;font-size:15px;line-height:1;}',
      '.sls-luna-btn{background:#3cb0ff;border:1px solid #3cb0ff;color:#fff;border-radius:3px;padding:7px 12px;',
      'cursor:pointer;font-size:13px;}',
      '.sls-luna-btn.secondary{background:rgba(255,255,255,.08);border-color:rgba(255,255,255,.15);}',
      '.sls-luna-body{flex:1 1 auto;overflow-y:auto;padding:0 24px 18px;}',
      '.sls-luna-note{color:#9aa4ae;font-size:13px;line-height:1.5;margin:0 0 12px;}',
      '.sls-luna-note strong{color:#dcdedf;}',
      '.sls-luna-row{display:flex;align-items:flex-start;gap:14px;padding:12px 0;border-bottom:1px solid rgba(255,255,255,.06);}',
      '.sls-luna-row .txt{flex:1 1 auto;min-width:0;}',
      '.sls-luna-row .txt .name{color:#fff;font-size:14px;}',
      '.sls-luna-row .txt .sub{color:#8d97a1;font-size:12px;margin-top:3px;line-height:1.45;}',
      '.sls-luna-input{width:96px;background:#263d50;color:#fff;border:1px solid #66849d;border-radius:3px;padding:5px;}',
      '.sls-luna-search{width:100%;box-sizing:border-box;background:#203447;color:#fff;border:1px solid #3b4b5c;',
      'border-radius:3px;padding:8px;margin:8px 0;}',
      '.sls-luna-switch{position:relative;width:44px;height:24px;border-radius:12px;background:#4c5560;cursor:pointer;',
      'flex:0 0 44px;transition:background .15s;}',
      '.sls-luna-switch.on{background:' + ACCENT + ';}',
      '.sls-luna-switch span{position:absolute;top:3px;left:3px;width:18px;height:18px;border-radius:50%;background:#fff;',
      'transition:left .15s;}',
      '.sls-luna-switch.on span{left:23px;}',
      '.sls-luna-stat{display:flex;gap:22px;flex-wrap:wrap;margin:6px 0 14px;}',
      '.sls-luna-stat div{min-width:96px;}',
      '.sls-luna-stat b{display:block;color:#fff;font-size:19px;font-weight:400;}',
      '.sls-luna-stat i{color:#8d97a1;font-size:12px;font-style:normal;}',
      '.sls-luna-game{display:flex;align-items:center;gap:9px;padding:8px 0;border-bottom:1px solid rgba(255,255,255,.05);}',
      '.sls-luna-game .gname{flex:1 1 auto;min-width:0;overflow-wrap:anywhere;font-size:13px;color:#e6e9ec;}',
      '.sls-luna-subtabs{display:flex;gap:6px;margin:0 0 14px;}',
      '.sls-luna-subtab{background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12);color:#cfd3d7;',
      'border-radius:3px;padding:5px 11px;font-size:12px;cursor:pointer;font-family:inherit;}',
      '.sls-luna-subtab:hover{background:rgba(255,255,255,.12);}',
      '.sls-luna-subtab.active{background:#3cb0ff;border-color:#3cb0ff;color:#fff;}',
      '.sls-luna-log{margin:0;max-height:44vh;overflow:auto;background:#10161c;color:#aeb8c2;font-size:11px;',
      'line-height:1.45;padding:10px;border-radius:3px;white-space:pre-wrap;}',
      '.sls-luna-confirm{background:#233140;border:1px solid #3b4b5c;border-radius:4px;padding:12px 14px;margin:0 0 10px;}',
      '.sls-luna-confirm .name{color:#fff;font-size:14px;margin-bottom:6px;}',
      '.sls-luna-confirm .sub{color:#9aa4ae;font-size:12px;line-height:1.5;}',
      '.sls-luna-progress{height:6px;border-radius:3px;background:rgba(255,255,255,.08);margin:6px 0 12px;overflow:hidden;}',
      '.sls-luna-progress div{height:100%;background:#3cb0ff;transition:width .4s;}',
      '.sls-luna-progress.active{background:rgba(60,176,255,.15);}',
      '.sls-luna-progress.active div{background-image:linear-gradient(90deg,rgba(60,176,255,.25),#3cb0ff,rgba(60,176,255,.25));',
      'animation:sls-slide 1.2s linear infinite;}',
      '@keyframes sls-slide{0%{transform:translateX(-30%);}100%{transform:translateX(30%);}}',
      '.sls-luna-chip{font-size:11px;padding:2px 8px;border-radius:9px;background:rgba(255,255,255,.08);',
      'color:#b8bcbf;white-space:nowrap;}',
      '.sls-luna-chip.done{background:rgba(87,203,92,.18);color:#7be07f;}',
      '.sls-luna-chip.running{background:rgba(60,176,255,.2);color:#7cc7ff;}',
      '.sls-luna-chip.failed{background:rgba(255,128,128,.16);color:#ff9a9a;}',
      '.sls-luna-chip.pending,.sls-luna-chip.skipped{opacity:.7;}',
      '.sls-luna-mini{width:64px;height:6px;border-radius:3px;background:rgba(255,255,255,.08);overflow:hidden;flex:0 0 64px;}',
      '.sls-luna-mini i{display:block;height:100%;background:#3cb0ff;}',
      '.sls-luna-mini i.done{background:#57cb5c;}',
      '.sls-luna-mini i.failed{background:#ff8080;}',
      '.sls-luna-chart{margin:0 0 16px;}',
      '.sls-luna-chart-row{display:flex;align-items:center;gap:10px;padding:3px 0;}',
      '.sls-luna-chart-label{flex:0 0 180px;color:#cfd3d7;font-size:12px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}',
      '.sls-luna-chart-track{flex:1 1 auto;height:12px;border-radius:3px;background:rgba(255,255,255,.06);overflow:hidden;}',
      '.sls-luna-chart-fill{height:100%;background:linear-gradient(90deg,#2b7fb8,#3cb0ff);}',
      '.sls-luna-chart-fill.alt{background:linear-gradient(90deg,#3d6b4a,#57cb5c);}',
      '.sls-luna-chart-value{flex:0 0 118px;text-align:right;color:#9aa4ae;font-size:11px;}',
      '.sls-luna-error{color:#ff8080;font-size:13px;margin:8px 0 0;}',
      '.sls-luna-assets{display:grid;grid-template-columns:repeat(auto-fill,minmax(118px,1fr));gap:8px;margin:10px 0 0;}',
      '.sls-luna-asset{cursor:pointer;border:1px solid rgba(255,255,255,.09);border-radius:3px;overflow:hidden;background:#10161c;}',
      '.sls-luna-asset:hover{border-color:#3cb0ff;}',
      '.sls-luna-asset img{display:block;width:100%;height:auto;}',
      '.sls-luna-asset .who{font-size:11px;color:#8d97a1;padding:3px 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}',
    ].join('');
    (document.head || document.body).appendChild(style);
  }

  /* Steam's menubar has hashed class names and localized labels, so it is found
     by shape: a short row whose direct children are menu items, one of which is
     the untranslated brand "Steam". Same approach as Lumen's, deliberately. */
  function findMenubar(document) {
    const isItem = node => {
      const text = (node.textContent || '').trim();
      return text.length > 0 && text.length <= 24 && text.indexOf('\n') === -1;
    };
    const nodes = document.querySelectorAll ? document.querySelectorAll('div,span,button,a') : [];
    for (let i = 0; i < nodes.length; i += 1) {
      const bar = nodes[i];
      const kids = bar.children;
      if (!kids || kids.length < 4 || kids.length > 12) continue;
      let items = 0;
      let hasSteam = false;
      for (let j = 0; j < kids.length; j += 1) {
        if (!isItem(kids[j])) continue;
        items += 1;
        if ((kids[j].textContent || '').trim() === 'Steam') hasSteam = true;
      }
      if (hasSteam && items >= 4) return bar;
    }
    return null;
  }

  function glyph(document) {
    const span = document.createElement('span');
    span.className = 'sls-luna-glyph';
    // Half moon: a circle with the right half in shadow, drawn inline so no
    // image request or font is involved.
    span.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true">'
      + '<defs><clipPath id="slsLunaHalf"><rect x="0" y="0" width="12" height="24"/></clipPath></defs>'
      + '<circle cx="12" cy="12" r="9" fill="#ffffff"/>'
      + '<circle cx="12" cy="12" r="9" fill="#123a5c" clip-path="url(#slsLunaHalf)"/></svg>';
    return span;
  }

  function buildButton(document, onOpen) {
    const button = document.createElement('div');
    button.id = BTN_ID;
    button.title = 'SLS Luna - settings for Playtime, Presence and Compressor';
    button.setAttribute('role', 'button');
    button.setAttribute('tabindex', '0');
    button.appendChild(glyph(document));
    const copy = document.createElement('span');
    copy.textContent = 'Luna';
    button.appendChild(copy);
    button.addEventListener('click', event => {
      event.preventDefault();
      event.stopPropagation();
      onOpen();
    });
    button.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        onOpen();
      }
    });
    return button;
  }

  function mountButton(w, onOpen) {
    const document = w.document;
    const place = () => {
      const existing = document.getElementById(BTN_ID);
      if (existing) return true;
      const bar = findMenubar(document);
      if (!bar) return false;
      const button = buildButton(document, onOpen);
      const lumen = document.getElementById('lumen-moon-btn');
      // Next to Lumen when it is there, otherwise at the end of the menubar.
      if (lumen && lumen.parentNode === bar) bar.insertBefore(button, lumen.nextSibling);
      else bar.appendChild(button);
      return true;
    };
    if (place()) return;
    let tries = 0;
    const clear = w.clearInterval || (() => {});
    const timer = w.setInterval(() => {
      tries += 1;
      if (place() || tries > 120) clear(timer);
    }, 500);
    return timer;
  }

  function removeFloating(document) {
    FLOATING.forEach(id => {
      const node = document.getElementById(id);
      if (node) node.remove();
    });
  }

  function rpc(w, method, args) {
    return Promise.resolve(w.Millennium.callServerMethod('lumen', method, args || {}))
      .then(parse)
      .then(out => {
        if (out && out.success === false) {
          // Keep the backend's code so callers can tell retryable failures
          // (rate limiting) from permanent ones (no confident match).
          const error = Error(out.error || (method + ' failed'));
          error.code = out.code || '';
          throw error;
        }
        return out;
      });
  }

  function isStoreWindow(w) {
    try { return /(^|\.)store\.steampowered\.com$/.test(String(w.location && w.location.hostname || '')); }
    catch (_) { return false; }
  }

  /* Store mode. The store runs on its own origin, so it can neither read the
     shell's localStorage nor be seen by the Settings page: the switch comes from
     the backend over RPC, and the followed list comes from the store's own
     session. The response is only ever used to hide tiles - nothing is stored
     and nothing leaves this machine. */
  const STORE_HIDDEN_ATTR = 'data-sls-luna-hidden';
  // Followed tiles are hidden on the store's browsing surfaces; search results
  // and the game pages themselves stay untouched on purpose.
  const STORE_QUIET = /^\/(search|app|account|login|join|checkout|cart|wishlist|points)(\/|$)/;

  function mountStore(w) {
    const document = w.document;
    const state = { followed: null, show: false, interval: null, observer: null };
    let scheduled = false;

    const style = document.createElement('style');
    style.id = 'sls-luna-store-style';
    style.textContent = [
      '[' + STORE_HIDDEN_ATTR + ']{display:none !important;}',
      '#sls-luna-store-chip{position:fixed;left:12px;bottom:12px;z-index:2147483000;display:flex;align-items:center;',
      'gap:8px;background:rgba(27,31,36,.92);color:#dcdedf;border:1px solid rgba(255,255,255,.16);border-radius:999px;',
      'padding:6px 12px;font:12px "Motiva Sans",Arial,sans-serif;box-shadow:0 4px 14px rgba(0,0,0,.4);}',
      '#sls-luna-store-chip b{color:#66c0f4;font-weight:700;}',
      '#sls-luna-store-chip button{background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.2);color:#fff;',
      'border-radius:999px;padding:3px 10px;font:11px "Motiva Sans",Arial,sans-serif;cursor:pointer;}',
    ].join('');
    if (document.head) document.head.appendChild(style);

    const markedTiles = () => Array.prototype.slice.call(
      document.querySelectorAll('[' + STORE_HIDDEN_ATTR + ']'));

    function paintChip(count) {
      let chip = document.getElementById('sls-luna-store-chip');
      if (!chip) {
        chip = document.createElement('div');
        chip.id = 'sls-luna-store-chip';
        const label = document.createElement('span');
        const toggle = document.createElement('button');
        toggle.onclick = () => {
          if (state.show) {
            state.show = false;
            sweep();
            return;
          }
          state.show = true;
          markedTiles().forEach(node => node.removeAttribute(STORE_HIDDEN_ATTR));
          paintChip(0);
        };
        chip.append(label, toggle);
        (document.body || document.documentElement).appendChild(chip);
      }
      const label = chip.children[0];
      const toggle = chip.children[1];
      if (state.show) {
        label.innerHTML = 'Luna: showing every tile';
        toggle.textContent = 'hide again';
      } else {
        label.innerHTML = 'Luna hides <b>' + count + '</b> followed game' + (count === 1 ? '' : 's');
        toggle.textContent = 'show';
      }
    }

    /** Marks every tile whose appid(s) are all followed. */
    function sweep() {
      if (state.show || !state.followed) return;
      let count = 0;
      for (const tile of document.querySelectorAll('[data-ds-appid]')) {
        const attr = String(tile.getAttribute('data-ds-appid') || '');
        const ids = attr.split(',').map(part => part.trim()).filter(part => /^\d+$/.test(part));
        if (!ids.length || !ids.every(id => state.followed.has(id))) continue;
        if (!tile.getAttribute(STORE_HIDDEN_ATTR)) tile.setAttribute(STORE_HIDDEN_ATTR, '');
        count += 1;
      }
      if (count > 0 || document.getElementById('sls-luna-store-chip')) paintChip(count);
    }

    function schedule() {
      if (scheduled || state.show) return;
      scheduled = true;
      const later = () => { scheduled = false; sweep(); };
      if (typeof w.setTimeout === 'function') w.setTimeout(later, 350);
      else later();
    }

    async function start() {
      if (STORE_QUIET.test(String(w.location && w.location.pathname || ''))) return;
      let info = null;
      try { info = await rpc(w, 'SlsLunaInfo'); } catch (_) { return; }
      if (!info || !info.prefs || info.prefs.store_hide_followed !== true) return;
      if (typeof w.fetch !== 'function') return;
      let followed = null;
      try {
        const response = await w.fetch('/dynamicstore/userdata/', { credentials: 'include' });
        if (!response || response.ok === false) return;
        const data = await response.json();
        followed = new Set((data && Array.isArray(data.rgFollowedApps) ? data.rgFollowedApps : []).map(String));
      } catch (_) { return; }
      if (!followed.size) return;
      state.followed = followed;
      sweep();
      const root = document.body || document.documentElement;
      if (typeof w.MutationObserver === 'function' && root) {
        state.observer = new w.MutationObserver(schedule);
        state.observer.observe(root, { childList: true, subtree: true });
      }
      if (typeof w.setInterval === 'function') state.interval = w.setInterval(schedule, 4000);
    }

    start();
  }

  function mount(w) {
    const document = w.document;
    if (!document || w.__SlsLunaPanel) return;
    w.__SlsLunaPanel = true;
    // Tells the feature add-ons to stay quiet in the shell: their settings live
    // in this menu while it is present.
    w.__SlsLunaUi = true;
    // The store is a different origin with no menubar: no menu there, just the
    // followed-games filter when the Settings switch asks for it.
    if (isStoreWindow(w)) { mountStore(w); return; }
    styled(document);
    removeFloating(document);

    let overlay = null;
    let tab = 'playtime';
    let snapshot = null;
    let presence = null;
    let compressor = null;
    let hltb = null;
    let hltbQuery = '';
    let hltbQueue = null;
    let hltbEdit = null;
    let hltbNotice = null;
    let sgdb = null;
    let sgdbGame = null;
    let sgdbKind = 'grid';
    let sgdbResults = null;
    let sgdbAssets = null;
    let sgdbBusy = false;
    let sgdbNotice = null;
    let artworkRequest = '';
    let graphics = null;
    let lunaInfo = null;
    let lunaHealth = null;
    let lunaUpdate = null;
    let lunaSteam = null;
    let lunaCheckedAt = 0;
    let healthNotice = null;
    let compressorTab = 'status';
    let compressorQuery = '';
    let pendingAction = null;
    let compressorNotice = null;
    // In-page confirmation state: native confirm()/prompt() freeze the Steam
    // shell, so every destructive action is armed with a second click instead.
    let confirmAction = null;
    let cancelArmed = false;
    let cleanupArmed = false;
    let query = '';
    let visible = 50;
    let jobTimer = null;
    // Keep the actual input node across paints (including asynchronous refreshes).
    // Recreating it after every keystroke drops focus and the caret.
    const search = document.createElement('input');
    search.className = 'sls-luna-search';
    search.placeholder = 'Filter game or AppID';
    search.oninput = () => {
      query = search.value; visible = 50;
      const start = search.selectionStart, end = search.selectionEnd;
      paint();
      search.focus();
      if (typeof search.setSelectionRange === 'function') search.setSelectionRange(start, end);
    };

    // Same idea for the compressor Games tab: one node, caret kept across paints.
    const compressSearch = document.createElement('input');
    compressSearch.className = 'sls-luna-search';
    compressSearch.placeholder = 'Filter games';
    compressSearch.oninput = () => {
      compressorQuery = compressSearch.value;
      const start = compressSearch.selectionStart, end = compressSearch.selectionEnd;
      paint();
      compressSearch.focus();
      if (typeof compressSearch.setSelectionRange === 'function') compressSearch.setSelectionRange(start, end);
    };

    // And for the HowLongToBeat page: same node, caret kept across paints.
    const hltbSearch = document.createElement('input');
    hltbSearch.className = 'sls-luna-search';
    hltbSearch.placeholder = 'Filter by name or AppID';
    hltbSearch.oninput = () => {
      hltbQuery = hltbSearch.value;
      const start = hltbSearch.selectionStart, end = hltbSearch.selectionEnd;
      paint();
      hltbSearch.focus();
      if (typeof hltbSearch.setSelectionRange === 'function') hltbSearch.setSelectionRange(start, end);
    };

    // SteamGridDB page: the API key field and the game search keep their nodes
    // across paints, like the other search boxes.
    const sgdbKeyInput = document.createElement('input');
    sgdbKeyInput.className = 'sls-luna-search';
    sgdbKeyInput.placeholder = 'SteamGridDB API key';
    const sgdbSearch = document.createElement('input');
    sgdbSearch.className = 'sls-luna-search';
    sgdbSearch.placeholder = 'Find a game on SteamGridDB';
    sgdbSearch.oninput = () => { /* read on submit, the node survives paints */ };

    // A running compressor job is polled while the menu stays open; the timer
    // dies with the menu so a closed window never keeps talking to the backend.
    function stopJobPolling() {
      if (jobTimer) { (w.clearInterval || (() => {}))(jobTimer); jobTimer = null; }
    }
    function startJobPolling() {
      if (jobTimer) return;
      jobTimer = w.setInterval(async () => {
        if (!document.getElementById(OVERLAY_ID)) { stopJobPolling(); return; }
        await refresh();
        const job = compressor && compressor.job;
        if (!job || !job.running) stopJobPolling();
      }, 1000);
    }

    /* A dot next to Health when an update is available or already queued, so it
       is visible without opening the page. */
    function updateBadge() {
      const update = lunaUpdate || {};
      return Boolean(update.available || update.pending);
    }

    const sidebar = () => visibleNav().map(entry => {
      const item = document.createElement('div');
      item.className = 'sls-luna-tab' + (entry.id === tab ? ' active' : '');
      const icon = document.createElement('span');
      icon.className = 'ico';
      icon.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true">' + ICON[entry.id] + '</svg>';
      const label = document.createElement('span');
      label.textContent = entry.label + (entry.id === 'health' && updateBadge() ? ' \u2022' : '');
      item.append(icon, label);
      item.onclick = () => {
        tab = entry.id;
        confirmAction = null;
        cancelArmed = false;
        cleanupArmed = false;
        paint();
      };
      return item;
    });

    function accountRow() {
      const row = document.createElement('div');
      row.className = 'sls-luna-account';
      const dot = document.createElement('span');
      dot.className = 'sls-luna-dot' + (connected() ? ' ok' : '');
      const label = document.createElement('span');
      const account = (snapshot && snapshot.account) || '';
      label.textContent = account ? ('Account ' + account.slice(-5)) : 'No ledger yet';
      const state = document.createElement('span');
      state.style.color = '#8d97a1';
      state.textContent = connected() ? 'Connected' : 'Offline';
      row.append(dot, label, state);
      return row;
    }

    function connected() {
      const live = (snapshot && snapshot.runtime) || {};
      return Boolean(live.healthy && Number(live.reportedAt || 0) > 0
        && Date.now() / 1000 - Number(live.reportedAt) < 45);
    }

    function statsRow() {
      const stats = (snapshot && snapshot.stats) || {};
      const wrap = document.createElement('div');
      wrap.className = 'sls-luna-stat';
      [['Today', stats.today], ['Last 7 days', stats.last7d], ['Last 30 days', stats.last30d]].forEach(pair => {
        const cell = document.createElement('div');
        const value = document.createElement('b');
        value.textContent = hours(pair[1]) + ' h';
        const label = document.createElement('i');
        label.textContent = pair[0];
        cell.append(value, label);
        wrap.appendChild(cell);
      });
      return wrap;
    }

    function playtimeBody() {
      const body = document.createElement('div');
      const live = (snapshot && snapshot.runtime) || {};
      const info = document.createElement('p');
      info.className = 'sls-luna-note';
      const active = Object.keys((snapshot && snapshot.active) || {});
      info.innerHTML = '<strong>' + (connected() ? 'Agent connected' : 'Agent not ready') + '</strong>. '
        + (snapshot ? (snapshot.manifestCount || 0) + ' Steam manifests (edit-only) + '
          + (snapshot.shortcutCount || 0) + ' non-Steam shortcuts (counted while they play). '
          + 'Ledger ' + ((live.file) || 'playtime.json') + '.' : 'Waiting for the backend.')
        + (active.length ? ' <strong>Recording now:</strong> ' + active.join(', ') + '.' : '')
        + (live.error ? ' ' + live.error : '');
      body.appendChild(info);
      if (snapshot) body.appendChild(statsRow());

      search.value = query;
      body.appendChild(search);

      const actions = document.createElement('div');
      actions.style.cssText = 'display:flex;gap:8px;margin:0 0 8px;';
      const exportButton = document.createElement('button');
      exportButton.className = 'sls-luna-btn';
      exportButton.textContent = 'Export JSON';
      exportButton.onclick = async () => {
        try {
          const out = await rpc(w, 'SlsPlaytimeExport');
          const area = document.createElement('textarea');
          area.rows = 8; area.style.width = '100%';
          area.value = JSON.stringify(out.data, null, 2);
          body.insertBefore(area, body.children[1] || null);
          area.focus(); area.select();
        } catch (error) { showError(body, error); }
      };
      const importButton = document.createElement('button');
      importButton.className = 'sls-luna-btn secondary';
      importButton.textContent = 'Import JSON';
      importButton.onclick = () => {
        // In-page flow: native prompt/confirm freeze the Steam shell, so the
        // paste area and the replace button live in the panel itself.
        const area = document.createElement('textarea');
        area.rows = 6;
        area.style.width = '100%';
        area.placeholder = 'Paste a trusted SLS Playtime backup (JSON) here';
        const row = document.createElement('div');
        row.style.cssText = 'display:flex;gap:8px;margin:6px 0;';
        const replace = document.createElement('button');
        replace.className = 'sls-luna-btn';
        replace.textContent = 'Replace ALL local records';
        const cancel = document.createElement('button');
        cancel.className = 'sls-luna-btn secondary';
        cancel.textContent = 'Cancel';
        cancel.onclick = () => { area.remove(); row.remove(); };
        replace.onclick = async () => {
          const raw = area.value.trim();
          if (!raw) { showError(body, Error('Paste a backup first.')); return; }
          try {
            await rpc(w, 'SlsPlaytimeImport', { raw });
            area.remove(); row.remove();
            await refresh();
          } catch (error) { showError(body, error); }
        };
        row.append(replace, cancel);
        body.insertBefore(area, body.children[1] || null);
        body.insertBefore(row, body.children[2] || null);
        area.focus();
      };
      actions.append(exportButton, importButton);
      body.appendChild(actions);

      if (!snapshot) return body;
      const rows = buildRows(snapshot, query);
      const list = document.createElement('div');
      rows.slice(0, visible).forEach(entry => list.appendChild(gameRow(entry, body)));
      body.appendChild(list);
      if (rows.length > visible) {
        const more = document.createElement('button');
        more.className = 'sls-luna-btn secondary';
        more.style.marginTop = '10px';
        more.textContent = 'Show 50 more (' + (rows.length - visible) + ' left)';
        more.onclick = () => { visible += 50; paint(); };
        body.appendChild(more);
      } else if (rows.length === 0) {
        const empty = document.createElement('p');
        empty.className = 'sls-luna-note';
        empty.textContent = 'Nothing matches that filter.';
        body.appendChild(empty);
      }
      return body;
    }

    function gameRow(entry, body) {
      const row = document.createElement('div');
      row.className = 'sls-luna-game';
      const name = document.createElement('span');
      name.className = 'gname';
      name.textContent = entry.name + ' (#' + entry.id + ')' + (entry.shortcut ? ' [non-Steam]' : '');
      const input = document.createElement('input');
      input.className = 'sls-luna-input';
      input.type = 'number';
      input.min = '0';
      input.step = '0.01';
      input.value = hours(entry.total);
      const save = document.createElement('button');
      save.className = 'sls-luna-btn';
      save.textContent = 'Save';
      save.onclick = async () => {
        try {
          await rpc(w, 'SlsPlaytimeEdit', { raw: JSON.stringify({ id: Number(entry.id), hours: Number(input.value) }) });
          await refresh();
        } catch (error) { showError(body, error); }
      };
      row.append(name, input, save);
      return row;
    }

    function presenceBody() {
      const body = document.createElement('div');
      const state = (presence && presence.state) || {};
      const runtime = (presence && presence.runtime) || {};
      const note = document.createElement('p');
      note.className = 'sls-luna-note';
      note.innerHTML = 'Publishes a hidden placeholder shortcut so friends see the game you are playing. '
        + 'Requires SLSsteam-Moon and at least one numeric manifest.';
      body.appendChild(note);

      const toggle = document.createElement('div');
      toggle.className = 'sls-luna-row';
      const text = document.createElement('div');
      text.className = 'txt';
      const name = document.createElement('div');
      name.className = 'name';
      name.textContent = 'Enable Presence';
      const sub = document.createElement('div');
      sub.className = 'sub';
      sub.textContent = 'Off by default. Turning it off removes its placeholder shortcut on the next game start.';
      text.append(name, sub);
      const switchBox = document.createElement('div');
      switchBox.className = 'sls-luna-switch' + (state.enabled ? ' on' : '');
      switchBox.appendChild(document.createElement('span'));
      switchBox.onclick = async () => {
        try {
          await rpc(w, 'SlsPresenceSetEnabled', { enabled: !state.enabled });
          await refresh();
        } catch (error) { showError(body, error); }
      };
      toggle.append(text, switchBox);
      body.appendChild(toggle);

      const sessionRow = document.createElement('div');
      sessionRow.className = 'sls-luna-row';
      const sessionText = document.createElement('div');
      sessionText.className = 'txt';
      const sessionName = document.createElement('div');
      sessionName.className = 'name';
      sessionName.textContent = 'Show session time in the name';
      const sessionSub = document.createElement('div');
      sessionSub.className = 'sub';
      sessionSub.textContent = 'While a game runs, its placeholder name gains a suffix like \u201c(42m)\u201d after the first five minutes.';
      sessionText.append(sessionName, sessionSub);
      const sessionSwitch = document.createElement('div');
      const sessionOn = state.sessionTime !== false;
      sessionSwitch.className = 'sls-luna-switch' + (sessionOn ? ' on' : '');
      sessionSwitch.appendChild(document.createElement('span'));
      sessionSwitch.onclick = async () => {
        try {
          await rpc(w, 'SlsPresenceSetSessionTime', { sessionTime: !sessionOn });
          await refresh();
        } catch (error) { showError(body, error); }
      };
      sessionRow.append(sessionText, sessionSwitch);
      body.appendChild(sessionRow);

      const owned = Object.keys(state.owned || {}).length;
      const running = Object.keys(state.running || {}).length;
      const health = document.createElement('p');
      health.className = 'sls-luna-note';
      health.innerHTML = 'Agent ' + (runtime.healthy ? '<strong>connected</strong>' : 'not ready')
        + '. Placeholders owned: <strong>' + owned + '</strong>, in use: <strong>' + running + '</strong>.'
        + (state.account ? ' Account ' + String(state.account).slice(-5) + '.' : '')
        + (runtime.error ? ' ' + runtime.error : '')
        + (runtime.accountMismatch ? ' Account mismatch detected; nothing is being published.' : '');
      body.appendChild(health);

      if (Number(state.cleanup || 0) > 0) {
        const pending = document.createElement('p');
        pending.className = 'sls-luna-note';
        pending.innerHTML = 'A cleanup was requested and is not acknowledged yet.';
        body.appendChild(pending);
      }
      const actions = document.createElement('div');
      actions.style.cssText = 'display:flex;gap:8px;flex-wrap:wrap;';
      const cleanup = document.createElement('button');
      cleanup.className = 'sls-luna-btn';
      cleanup.textContent = cleanupArmed ? 'Confirm removal' : 'Disable and remove placeholders';
      cleanup.onclick = async () => {
        // Two-step in the page: a native confirm() would freeze the shell.
        if (!cleanupArmed) { cleanupArmed = true; paint(); return; }
        cleanupArmed = false;
        try { await rpc(w, 'SlsPresenceRequestCleanup'); await refresh(); } catch (error) { showError(body, error); }
      };
      if (cleanupArmed) {
        const warning = document.createElement('p');
        warning.className = 'sls-luna-note';
        warning.textContent = 'Click Confirm removal to disable Presence and remove every placeholder it owns.';
        body.appendChild(warning);
      }
      const ack = document.createElement('button');
      ack.className = 'sls-luna-btn secondary';
      ack.textContent = 'Acknowledge cleanup';
      ack.onclick = async () => {
        try { await rpc(w, 'SlsPresenceAcknowledgeCleanup'); await refresh(); } catch (error) { showError(body, error); }
      };
      actions.append(cleanup, ack);
      body.appendChild(actions);
      return body;
    }

    // ---- Compressor page: Status / Settings / Games / Log ------------------

    function compressorRunsText() {
      const settings = (compressor && compressor.settings) || {};
      const parts = [];
      if (settings.compress) parts.push('ZSTD compression');
      if (settings.dedupe) parts.push('deduplication');
      return parts.join(' + ');
    }

    function compressorBusyLine() {
      const job = compressor && compressor.job;
      const line = document.createElement('p');
      line.className = 'sls-luna-note';
      if (job && job.running) {
        line.innerHTML = '<strong>Running now:</strong> ' + (ACTION_LABELS[job.action] || job.action)
          + ' \u00b7 ' + age(job.startedAt) + '.';
      } else if (pendingAction) {
        line.innerHTML = '<strong>Starting</strong> ' + (ACTION_LABELS[pendingAction.action] || pendingAction.action) + '\u2026';
      } else if (job) {
        line.innerHTML = 'Last job: ' + (ACTION_LABELS[job.action] || job.action)
          + ' (started at ' + clock(job.startedAt) + ').';
      } else {
        line.textContent = 'No job has run from here yet.';
      }
      return line;
    }

    async function saveSettings(patch, body) {
      const current = (compressor && compressor.settings) || {};
      const next = Object.assign({}, current, patch);
      try {
        await rpc(w, 'SlsCompressorPutSettings', { raw: JSON.stringify(next) });
        await refresh();
      } catch (error) { showError(body, error); }
    }

    async function startJob(body, action) {
      confirmAction = null;
      cancelArmed = false;
      pendingAction = { action: action, at: Date.now() / 1000 };
      paint();
      try {
        await rpc(w, 'SlsCompressorRun', { raw: JSON.stringify({ action: action }) });
        startJobPolling();
        await refresh();
      } catch (error) {
        // The body painted before the request is gone by now, so the failure
        // travels through the notice the next paint renders.
        pendingAction = null;
        compressorNotice = String(error && error.message ? error.message : error);
        paint();
      }
    }

    function jobButton(body, label, action, secondary, disabled) {
      const button = document.createElement('button');
      button.className = 'sls-luna-btn' + (secondary ? ' secondary' : '');
      button.textContent = label;
      button.disabled = Boolean(disabled);
      button.onclick = () => startJob(body, action);
      return button;
    }

    function cancelButton(body) {
      const button = document.createElement('button');
      button.className = 'sls-luna-btn' + (cancelArmed ? '' : ' secondary');
      button.textContent = cancelArmed ? 'Confirm cancel' : 'Cancel job';
      button.onclick = async () => {
        const job = compressor && compressor.job;
        if (!job || !job.running) return;
        // Two-step in the page; a native confirm() would freeze the shell.
        if (!cancelArmed) { cancelArmed = true; paint(); return; }
        cancelArmed = false;
        try { await rpc(w, 'SlsCompressorCancel'); await refresh(); }
        catch (error) {
          compressorNotice = String(error && error.message ? error.message : error);
          paint();
        }
      };
      return button;
    }

    function switchRow(body, label, sub, on, onToggle) {
      const row = document.createElement('div');
      row.className = 'sls-luna-row';
      const text = document.createElement('div');
      text.className = 'txt';
      const name = document.createElement('div');
      name.className = 'name';
      name.textContent = label;
      const info = document.createElement('div');
      info.className = 'sub';
      info.textContent = sub;
      text.append(name, info);
      const box = document.createElement('div');
      box.className = 'sls-luna-switch' + (on ? ' on' : '');
      box.appendChild(document.createElement('span'));
      box.onclick = () => onToggle(!on);
      row.append(text, box);
      body.appendChild(row);
      return row;
    }

    function runConfirmText(plan) {
      const settings = (compressor && compressor.settings) || {};
      const target = settings.compress && settings.dedupe ? 'ZSTD compression + deduplication'
        : settings.compress ? 'ZSTD compression (deduplication stays for a later pass)'
        : 'a whole-library deduplication pass (the exclude list does not apply to this mode)';
      const count = settings.compress && settings.dedupe
        ? (Number(plan.compress || 0) + Number(plan.dedupe || 0))
        : settings.compress ? Number(plan.compress || 0) : 0;
      const ceilingGb = Number(settings.maxSizeGb) || 0;
      const excluded = listOf(settings.excluded).length;
      const order = settings.runOrder === 'largest' ? 'largest first'
        : settings.runOrder === 'library' ? 'in library order' : 'smallest first';
      return 'This runs <strong>' + target + '</strong>'
        + (count > 0 ? ' on about <strong>' + count + '</strong> game(s)' : '')
        + ', ' + order
        + '. Filters: <strong>' + excluded + '</strong> excluded'
        + (ceilingGb > 0 ? ', only games under <strong>' + ceilingGb + ' GB</strong>' : ', no size ceiling')
        + '. Games that are playing are skipped; you can cancel from here while it runs.';
    }

    // The confirmation is a drawer inside the page: a native confirm() freezes
    // the whole Steam shell, which is what made Run now look stuck.
    function runConfirm(body, plan) {
      const box = document.createElement('div');
      box.className = 'sls-luna-confirm';
      const title = document.createElement('div');
      title.className = 'name';
      title.textContent = 'Start the pass now?';
      const detail = document.createElement('div');
      detail.className = 'sub';
      detail.innerHTML = runConfirmText(plan);
      const row = document.createElement('div');
      row.style.cssText = 'display:flex;gap:8px;margin-top:10px;';
      const start = document.createElement('button');
      start.className = 'sls-luna-btn';
      start.textContent = 'Start the pass';
      start.onclick = () => startJob(body, 'run');
      const back = document.createElement('button');
      back.className = 'sls-luna-btn secondary';
      back.textContent = 'Back';
      back.onclick = () => { confirmAction = null; paint(); };
      row.append(start, back);
      box.append(title, detail, row);
      return box;
    }

    // What the tool last printed, turned into position and, when the line
    // carries it, a percentage for the bar. The percentage line and the game
    // name usually come from different lines, so both are collected.
    function jobProgress(job) {
      if (!job || !job.log) return null;
      const lines = job.log.split('\n').reverse();
      const out = {};
      for (const line of lines) {
        let match = line.match(/(\d+)% \((\d+)\/(\d+)\)/);
        if (match) {
          if (out.pct == null) out.pct = Number(match[1]);
          if (out.current == null) out.current = Number(match[2]);
          if (out.total == null) out.total = Number(match[3]);
          continue;
        }
        match = line.match(/\[(\d+)\/(\d+)\]\s*Compressing:\s*(.+)\.\.\./);
        if (match) {
          if (out.current == null) out.current = Number(match[1]);
          if (out.total == null) out.total = Number(match[2]);
          if (out.label == null) out.label = match[3];
          continue;
        }
        match = line.match(/Deduplicating\s+(.+)\.\.\./);
        if (match && out.label == null) out.label = 'deduplicating ' + match[1];
      }
      if (out.pct == null && out.current == null && out.label == null) {
        const last = lines.find(entry => entry.trim());
        return last ? { label: last.trim().slice(0, 120) } : null;
      }
      return out;
    }

    function progressBar(pct, indeterminate) {
      const bar = document.createElement('div');
      bar.className = 'sls-luna-progress' + (indeterminate ? ' active' : '');
      const fill = document.createElement('div');
      fill.style.width = indeterminate ? '100%' : Math.max(2, Number(pctText(pct))) + '%';
      bar.appendChild(fill);
      return bar;
    }

    // Byte-level overall progress, when the full queue is loaded; the light poll
    // only carries counts, so callers fall back to the game count.
    function queueOverall() {
      const queue = (compressor && compressor.queue) || {};
      let doneBytes = 0;
      let totalBytes = 0;
      listOf(queue.games).forEach(game => {
        const size = Number(game.size) || 0;
        totalBytes += size;
        if (game.status === 'done' || game.status === 'skipped') doneBytes += size;
        else if (game.status === 'running' && queue.current) {
          doneBytes += Math.min(size, Number(queue.current.doneBytes) || 0);
        }
      });
      if (totalBytes <= 0) return null;
      return { doneBytes, totalBytes };
    }

    function compressorQueueBody(body) {
      const queue = (compressor && compressor.queue) || {};
      const current = queue.current;
      const active = queue.active === true;
      const total = Number(queue.total) || 0;
      const done = Number(queue.done) || 0;
      const failed = Number(queue.failed) || 0;
      const skipped = Number(queue.skipped) || 0;
      const saved = Number(queue.saved) || 0;
      const overall = queueOverall();
      // The overall bar is the queue's own unit: games finished. The current
      // game's fraction is included so the bar moves within a game instead of
      // sitting still between completions.
      const currentFraction = () => {
        if (!current) return 0;
        if (current.phase === 'dedupe') {
          const size = Number(current.size) || 0;
          return size > 0 ? Math.min(1, (Number(current.scannedBytes) || 0) / size) : 0;
        }
        const totalBytes = Number(current.totalBytes) || 0;
        return totalBytes > 0 ? Math.min(1, (Number(current.doneBytes) || 0) / totalBytes) : 0;
      };
      const barPct = total > 0 ? Math.min(100, ((done + currentFraction()) / total) * 100) : 0;
      const head = document.createElement('p');
      head.className = 'sls-luna-note';
      head.innerHTML = '<strong>' + (active ? 'Run in progress'
        : queue.finished ? 'Last run finished' : 'Last run stopped') + '</strong> \u00b7 '
        + done + ' of ' + total + ' games done'
        + (overall ? ' \u00b7 ' + formatBytes(overall.doneBytes) + ' of ' + formatBytes(overall.totalBytes) : '')
        + (failed ? ' \u00b7 failed ' + failed : '')
        + (skipped ? ' \u00b7 skipped ' + skipped : '')
        + (saved > 0 ? ' \u00b7 saved ' + formatBytes(saved) : '');
      body.appendChild(head);
      body.appendChild(progressBar(barPct, false));

      if (current) {
        const pct = current.phase === 'dedupe'
          ? (Number(current.size) > 0
            ? Math.min(100, (Number(current.scannedBytes) || 0) / Number(current.size) * 100) : 0)
          : (Number(current.totalBytes) > 0
            ? Number(current.doneBytes) / Number(current.totalBytes) * 100 : 0);
        const line = document.createElement('p');
        line.className = 'sls-luna-note';
        line.innerHTML = 'Game ' + (Number(queue.index) || 0) + ' of ' + total + ': <strong>'
          + current.name + '</strong> '
          + (current.phase === 'dedupe'
            ? '\u00b7 deduplicating' + (Number(current.scannedBytes) > 0
              ? ' (' + formatBytes(current.scannedBytes) + ' scanned)' : '')
            : '\u00b7 compressing' + (Number(current.totalBytes) > 0 ? ' ' + pctText(pct) + '%' : ''));
        body.appendChild(line);
        body.appendChild(progressBar(pct, false));
      }

      if (!active && !queue.finished && total > 0) {
        const stopped = document.createElement('p');
        stopped.className = 'sls-luna-note';
        stopped.textContent = 'The queue stopped before it finished; the game in progress is picked up again by the next run.';
        body.appendChild(stopped);
      }
      const next = listOf(queue.next);
      if (active && next.length) {
        const upcoming = document.createElement('p');
        upcoming.className = 'sls-luna-note';
        upcoming.textContent = 'Next: ' + next.map(entry => entry.name + ' (' + formatBytes(entry.size) + ')').join(', ') + '.';
        body.appendChild(upcoming);
      }
      return true;
    }

    function compressorStatus(body) {
      const settings = (compressor && compressor.settings) || {};
      const plan = (compressor && compressor.plan) || {};
      const tool = (compressor && compressor.tool) || {};
      const runs = compressorRunsText();
      const job = compressor.job;
      const running = Boolean(job && job.running);
      const ceilingGb = Number(settings.maxSizeGb) || 0;
      const excludedCount = listOf(settings.excluded).length;

      const enabled = document.createElement('p');
      enabled.className = 'sls-luna-note';
      enabled.innerHTML = runs
        ? 'A run does: <strong>' + runs + '</strong>. Asset changes are never applied by a run, and a '
          + 'game that is playing is skipped.'
        : '<strong>Nothing is enabled.</strong> Turn on compression or deduplication in Settings first.';
      body.appendChild(enabled);

      const stats = document.createElement('div');
      stats.className = 'sls-luna-stat';
      [['Pending', plan.pending, 'games not compacted yet'],
        ['To compress', plan.compress, 'waiting for ZSTD'],
        ['Dedupe only', plan.dedupe, 'compression is current'],
        ['Held back', Number(plan.excluded || 0) + Number(plan.overSize || 0), 'excluded or over the ceiling']]
        .forEach(pair => {
          const cell = document.createElement('div');
          const value = document.createElement('b');
          value.textContent = String(pair[1] == null ? 0 : pair[1]);
          const label = document.createElement('i');
          label.textContent = pair[0];
          cell.append(value, label);
          stats.appendChild(cell);
        });
      body.appendChild(stats);

      const filters = document.createElement('p');
      filters.className = 'sls-luna-note';
      filters.textContent = 'Filters: ' + excludedCount + ' game(s) excluded'
        + (ceilingGb > 0 ? ', only games under ' + ceilingGb + ' GB' : ', no size ceiling')
        + '. Pending counts come from the last scan.';
      body.appendChild(filters);

      const scanLine = document.createElement('p');
      scanLine.className = 'sls-luna-note';
      const when = Number(compressor.statusAt || 0);
      scanLine.textContent = 'Tool: ' + (tool.path || '?') + (tool.version ? ' v' + tool.version : '') + '. '
        + (when ? 'Scanned ' + age(when) + ' ago.' : 'No scan yet \u2014 one starts by itself on first open.');
      body.appendChild(scanLine);

      body.appendChild(compressorBusyLine());
      if (compressor && compressor.queue && (Number(compressor.queue.total) > 0 || compressor.queue.active)) {
        compressorQueueBody(body);
      } else if (running) {
        const progress = jobProgress(job);
        const line = document.createElement('p');
        line.className = 'sls-luna-note';
        const position = progress && progress.total
          ? progress.current + ' of ' + progress.total + ' games'
          : (progress && progress.label) || 'working';
        line.innerHTML = 'Progress: <strong>' + position + '</strong>'
          + (progress && progress.pct ? ' \u00b7 ' + progress.pct + '%' : '')
          + (progress && progress.total && progress.label ? ' \u00b7 ' + progress.label : '');
        body.appendChild(line);
        body.appendChild(progressBar(progress && progress.pct, !progress || !progress.pct));
      }

      const actions = document.createElement('div');
      actions.style.cssText = 'display:flex;gap:8px;flex-wrap:wrap;margin:10px 0 8px;';
      actions.appendChild(jobButton(body, 'Scan library', 'scan', true, running));
      actions.appendChild(jobButton(body, 'Preview (dry-run)', 'preview', true, running));
      if (confirmAction !== 'run') {
        const runButton = document.createElement('button');
        runButton.className = 'sls-luna-btn';
        runButton.textContent = 'Run now';
        runButton.disabled = running || !runs;
        runButton.onclick = () => { confirmAction = 'run'; paint(); };
        actions.appendChild(runButton);
      }
      if (running) actions.appendChild(cancelButton(body));
      body.appendChild(actions);
      if (confirmAction === 'run') body.appendChild(runConfirm(body, plan));
      if (cancelArmed && running) {
        const stopHint = document.createElement('p');
        stopHint.className = 'sls-luna-note';
        stopHint.textContent = 'Click Confirm cancel again to stop the pass; it keeps its progress.';
        body.appendChild(stopHint);
      }
      if (!runs) {
        const unlockHint = document.createElement('p');
        unlockHint.className = 'sls-luna-note';
        unlockHint.textContent = 'Run now unlocks once compression or deduplication is enabled.';
        body.appendChild(unlockHint);
      }

      if (running) startJobPolling();
      return body;
    }

    /* Horizontal bars for the Stats tab. Values are plain byte counts; the
       widest entry fills the track so small libraries still read clearly. */
    function compressorChart(body, title, entries, options) {
      const opts = options || {};
      const wrap = document.createElement('div');
      const head = document.createElement('p');
      head.className = 'sls-luna-note';
      head.innerHTML = '<strong>' + title + '</strong>';
      wrap.appendChild(head);
      if (!entries.length) {
        const empty = document.createElement('p');
        empty.className = 'sls-luna-note';
        empty.textContent = opts.empty || 'Nothing measured yet.';
        wrap.appendChild(empty);
        body.appendChild(wrap);
        return;
      }
      const list = document.createElement('div');
      list.className = 'sls-luna-chart';
      const peak = entries.reduce((max, entry) => Math.max(max, Number(entry.value) || 0), 0) || 1;
      entries.forEach(entry => {
        const row = document.createElement('div');
        row.className = 'sls-luna-chart-row';
        const label = document.createElement('span');
        label.className = 'sls-luna-chart-label';
        label.textContent = entry.label;
        if (entry.title) label.title = entry.title;
        const track = document.createElement('div');
        track.className = 'sls-luna-chart-track';
        const fill = document.createElement('div');
        fill.className = 'sls-luna-chart-fill' + (opts.alt ? ' alt' : '');
        fill.style.width = Math.max(2, Math.round((Number(entry.value) || 0) * 100 / peak)) + '%';
        track.appendChild(fill);
        const value = document.createElement('span');
        value.className = 'sls-luna-chart-value';
        value.textContent = entry.display;
        row.append(label, track, value);
        list.appendChild(row);
      });
      wrap.appendChild(list);
      body.appendChild(wrap);
    }

    /* The Stats tab: everything comes from the compressor's own ledgers (read
       by the backend), Luna's recorded passes and the last scan. Nothing here
       triggers a measurement - the button does that explicitly. */
    function compressorStats(body) {
      const stats = (compressor && compressor.stats) || null;
      const job = compressor && compressor.job;
      const note = document.createElement('p');
      note.className = 'sls-luna-note';
      note.innerHTML = 'Savings measured by the compressor itself: <strong>ZSTD compression</strong> (original vs '
        + 'on-disk bytes per game) and <strong>deduplication</strong> (physical extent deltas, latest measurement '
        + 'per directory). The categories overlap by design and are never added into one grand total.';
      body.appendChild(note);

      const actions = document.createElement('div');
      actions.className = 'sls-luna-row';
      const button = jobButton(body, 'Measure now', 'recheck', true, Boolean(job && job.running));
      const hint = document.createElement('div');
      hint.className = 'sub';
      hint.textContent = 'Runs the compressor\u2019s own --recheck: it remeasures the installed games and '
        + 'refreshes its ledger. Nothing is recompressed and no game file is touched.';
      actions.append(button, hint);
      body.appendChild(actions);

      if (!stats) {
        const empty = document.createElement('p');
        empty.className = 'sls-luna-note';
        empty.textContent = 'No statistics yet \u2014 the next Refresh loads them.';
        body.appendChild(empty);
        return;
      }
      const compression = stats.compression || {};
      const dedupe = stats.dedupe || {};
      const luna = stats.luna || {};
      const library = stats.library || {};
      const summary = (compressor && compressor.status && compressor.status.summary) || {};
      const measuredSaved = (Number(compression.saved_bytes) || 0) + (Number(dedupe.saved_bytes) || 0);

      const cards = document.createElement('div');
      cards.className = 'sls-luna-stat';
      [['Measured saved', formatBytes(measuredSaved), 'ZSTD + deduplication'],
       ['Library on disk', formatBytes(library.bytes), (Number(library.games) || 0) + ' games in the last scan'],
       ['Compacted', (Number(summary.compacted) || 0) + ' / ' + (Number(summary.game_count) || 0), 'games done'],
       ['Luna passes', formatBytes(luna.saved_bytes), (Number(luna.measured) || 0) + ' games with savings']]
        .forEach(pair => {
          const cell = document.createElement('div');
          const value = document.createElement('b');
          value.textContent = String(pair[1]);
          const label = document.createElement('i');
          label.textContent = pair[0] + ' \u00b7 ' + pair[2];
          cell.append(value, label);
          cards.appendChild(cell);
        });
      body.appendChild(cards);

      const compressionGames = listOf(compression.games).slice(0, 8);
      compressorChart(body, 'ZSTD savings per game',
        compressionGames.map(game => ({
          label: game.name, title: game.path || '',
          value: game.saved_bytes,
          display: formatBytes(game.saved_bytes) + ' \u00b7 '
            + pctText(game.original_bytes ? game.saved_bytes * 100 / game.original_bytes : 0) + '%',
        })),
        { empty: Number(compression.measured)
          ? 'Nothing measured yet.'
          : 'No measured ZSTD rows yet \u2014 press Measure now to fill this ledger.' });

      const lunaGames = listOf(luna.games).slice(0, 8);
      compressorChart(body, 'Savings recorded by Luna passes',
        lunaGames.map(game => ({ label: game.name, value: game.saved_bytes,
          display: formatBytes(game.saved_bytes) })),
        { empty: 'No per-game savings were recorded yet \u2014 a pass that reclaims nothing records zero.' });

      const dedupeEntries = listOf(dedupe.entries).slice(0, 8);
      compressorChart(body, 'Deduplication per directory',
        dedupeEntries.map(entry => ({
          label: String(entry.path || '').split('/').pop() || String(entry.path || ''),
          title: entry.path || '', value: entry.saved_bytes,
          display: formatBytes(entry.saved_bytes),
        })),
        { empty: 'No deduplication passes recorded yet.', alt: true });

      const libraryGames = listOf(compressor && compressor.status && compressor.status.games)
        .slice()
        .sort((a, b) => (Number(b.size_bytes) || 0) - (Number(a.size_bytes) || 0))
        .slice(0, 8);
      compressorChart(body, 'Biggest games in the library',
        libraryGames.map(game => ({ label: game.name, value: game.size_bytes,
          display: game.size_display || formatBytes(game.size_bytes) })),
        { empty: 'No scan yet.', alt: true });

      const total = Number(summary.game_count) || 0;
      const done = Number(summary.compacted) || 0;
      if (total > 0) {
        const wrap = document.createElement('div');
        const head = document.createElement('p');
        head.className = 'sls-luna-note';
        head.innerHTML = '<strong>Library progress</strong> \u00b7 ' + done + ' of ' + total + ' compacted ('
          + pctText(done * 100 / total) + '%)';
        const bar = document.createElement('div');
        bar.className = 'sls-luna-progress';
        const fill = document.createElement('div');
        fill.style.width = pctText(done * 100 / total) + '%';
        bar.appendChild(fill);
        wrap.append(head, bar);
        body.appendChild(wrap);
      }
    }

    function compressorSettings(body) {
      const settings = (compressor && compressor.settings) || {};
      const group = title => {
        const line = document.createElement('p');
        line.className = 'sls-luna-note';
        line.innerHTML = '<strong>' + title + '</strong>';
        body.appendChild(line);
      };

      group('Safe operations');
      switchRow(body, 'ZSTD compression',
        'Rewrites each game through zstd when it is new or changed. The slow part, and safe to interrupt.',
        settings.compress !== false, value => saveSettings({ compress: value }, body));
      switchRow(body, 'Deduplication',
        'Merges duplicate extents after compression. Touches no file contents.',
        settings.dedupe !== false, value => saveSettings({ dedupe: value }, body));
      switchRow(body, 'Eco mode',
        'Runs the pass with nice/ionice so the machine stays responsive while it works.',
        settings.eco === true, value => saveSettings({ eco: value }, body));

      group('Limits');
      const ceiling = Number(settings.maxSizeGb) || 0;
      const sizeRow = document.createElement('div');
      sizeRow.className = 'sls-luna-row';
      const sizeText = document.createElement('div');
      sizeText.className = 'txt';
      const sizeName = document.createElement('div');
      sizeName.className = 'name';
      sizeName.textContent = 'Only process games smaller than';
      const sizeSub = document.createElement('div');
      sizeSub.className = 'sub';
      sizeSub.textContent = 'Handy for a first test: set it low, watch one small game go through, then lift it. Off means every game.';
      sizeText.append(sizeName, sizeSub);
      const sizeInput = document.createElement('input');
      sizeInput.className = 'sls-luna-input';
      sizeInput.type = 'number';
      sizeInput.min = '0';
      sizeInput.step = '1';
      sizeInput.placeholder = 'GB';
      sizeInput.value = ceiling > 0 ? String(ceiling) : '';
      sizeInput.disabled = ceiling <= 0;
      sizeInput.onchange = () => saveSettings({ maxSizeGb: Math.max(0, Number(sizeInput.value) || 0) }, body);
      const sizeSwitch = document.createElement('div');
      sizeSwitch.className = 'sls-luna-switch' + (ceiling > 0 ? ' on' : '');
      sizeSwitch.appendChild(document.createElement('span'));
      sizeSwitch.onclick = () => saveSettings({
        maxSizeGb: ceiling > 0 ? 0 : Math.max(1, Number(sizeInput.value) || 20),
      }, body);
      sizeRow.append(sizeText, sizeInput, sizeSwitch);
      body.appendChild(sizeRow);

      const orderRow = document.createElement('div');
      orderRow.className = 'sls-luna-row';
      const orderText = document.createElement('div');
      orderText.className = 'txt';
      const orderName = document.createElement('div');
      orderName.className = 'name';
      orderName.textContent = 'Run order';
      const orderSub = document.createElement('div');
      orderSub.className = 'sub';
      orderSub.textContent = 'Which game the queue takes first. Smallest first shows finished games quickly.';
      orderText.append(orderName, orderSub);
      const orderChips = document.createElement('div');
      orderChips.style.cssText = 'display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end;max-width:320px;';
      [['smallest', 'Smallest first'], ['largest', 'Largest first'], ['library', 'Library order']]
        .forEach(pair => {
          const chip = document.createElement('button');
          chip.className = 'sls-luna-subtab' + ((settings.runOrder || 'smallest') === pair[0] ? ' active' : '');
          chip.textContent = pair[1];
          chip.onclick = () => saveSettings({ runOrder: pair[0] }, body);
          orderChips.appendChild(chip);
        });
      orderRow.append(orderText, orderChips);
      body.appendChild(orderRow);

      const excluded = listOf(settings.excluded).length;
      const excludedRow = document.createElement('div');
      excludedRow.className = 'sls-luna-row';
      const excludedText = document.createElement('div');
      excludedText.className = 'txt';
      const excludedName = document.createElement('div');
      excludedName.className = 'name';
      excludedName.textContent = 'Excluded games';
      const excludedSub = document.createElement('div');
      excludedSub.className = 'sub';
      excludedSub.textContent = excluded
        ? excluded + ' game(s) will never be touched by a pass. The status view still reports them.'
        : 'Pick games a pass must never touch; the status view still reports them.';
      excludedText.append(excludedName, excludedSub);
      const manage = document.createElement('button');
      manage.className = 'sls-luna-btn secondary';
      manage.textContent = 'Manage';
      manage.onclick = () => { compressorTab = 'games'; paint(); };
      excludedRow.append(excludedText, manage);
      body.appendChild(excludedRow);

      group('Alpha');
      switchRow(body, 'Asset optimization',
        'ALPHA. Changes supported game assets (images, DDS textures, audio). While off, the resolution profile is pinned to Native so nothing can change assets.',
        settings.assets === true, value => saveSettings({ assets: value }, body));
      if (settings.assets === true) {
        const profileRow = document.createElement('div');
        profileRow.className = 'sls-luna-row';
        const profileText = document.createElement('div');
        profileText.className = 'txt';
        const profileName = document.createElement('div');
        profileName.className = 'name';
        profileName.textContent = 'Resolution profile';
        const profileSub = document.createElement('div');
        profileSub.className = 'sub';
        profileSub.textContent = 'Longest edge assets are downsized to. Lossless only recompresses Hades LZ4 packages; decoded assets stay identical.';
        profileText.append(profileName, profileSub);
        const chips = document.createElement('div');
        chips.style.cssText = 'display:flex;gap:6px;flex-wrap:wrap;justify-content:flex-end;max-width:340px;';
        PROFILE_LABELS.forEach(pair => {
          const chip = document.createElement('button');
          chip.className = 'sls-luna-subtab' + (settings.assetProfile === pair[0] ? ' active' : '');
          chip.textContent = pair[1];
          chip.onclick = () => saveSettings({ assetProfile: pair[0] }, body);
          chips.appendChild(chip);
        });
        profileRow.append(profileText, chips);
        body.appendChild(profileRow);

        const warning = document.createElement('p');
        warning.className = 'sls-luna-note';
        warning.innerHTML = '<strong>Alpha:</strong> the run buttons never apply assets. This sets the '
          + 'profile for the tool and the read-only inventory below; applying assets stays a per-game '
          + 'action in the tool for now.';
        body.appendChild(warning);
        const previewRow = document.createElement('div');
        previewRow.style.cssText = 'display:flex;gap:8px;margin:0 0 8px;';
        previewRow.appendChild(jobButton(body, 'Preview asset inventory (read-only)', 'assets', true,
          Boolean(compressor.job && compressor.job.running)));
        body.appendChild(previewRow);
      }
      return body;
    }

    function compressorGames(body) {
      const status = compressor && compressor.status;
      const games = (status && Array.isArray(status.games) ? status.games : []);
      const settings = (compressor && compressor.settings) || {};
      const excluded = new Set(listOf(settings.excluded));
      const ceilingGb = Number(settings.maxSizeGb) || 0;
      const queue = (compressor && compressor.queue) || null;
      const queueByPath = {};
      const queueByName = {};
      listOf(queue && queue.games).forEach(entry => {
        if (entry.path) queueByPath[entry.path] = entry;
        if (entry.name) queueByName[entry.name] = entry;
      });

      const summary = document.createElement('p');
      summary.className = 'sls-luna-note';
      summary.innerHTML = '<strong>' + games.length + ' installed games</strong>. '
        + excluded.size + ' excluded, ' + (ceilingGb > 0 ? 'only under ' + ceilingGb + ' GB' : 'no size ceiling')
        + '. Toggling a game here never writes to it.';
      body.appendChild(summary);

      compressSearch.value = compressorQuery;
      body.appendChild(compressSearch);

      const needle = compressorQuery.trim().toLowerCase();
      const rows = games.filter(game => (String(game.name) + ' ' + String(game.status)).toLowerCase().includes(needle));
      if (!rows.length) {
        const empty = document.createElement('p');
        empty.className = 'sls-luna-note';
        empty.textContent = games.length ? 'Nothing matches that filter.' : 'No scan yet: run Scan library first.';
        body.appendChild(empty);
        return body;
      }
      rows.slice(0, 80).forEach(game => {
        const row = document.createElement('div');
        row.className = 'sls-luna-row';
        const text = document.createElement('div');
        text.className = 'txt';
        const name = document.createElement('div');
        name.className = 'name';
        name.textContent = game.name;
        const sub = document.createElement('div');
        sub.className = 'sub';
        sub.textContent = [
          STATUS_LABELS[game.status] || String(game.status || '').toLowerCase(),
          game.size_display || '',
          game.dedupe_pending ? 'dedupe pending' : '',
          game.community_hint_pct ? 'hint ~' + game.community_hint_pct + '%' : '',
        ].filter(Boolean).join(' \u00b7 ');
        text.append(name, sub);
        // When a queue exists, each row carries its own state and mini bar:
        // green when done, blue while running (with the live percentage), red
        // when it failed, dim when still pending.
        const queueEntry = queue ? (queueByPath[game.path] || queueByName[game.name]) : null;
        let mini = null;
        let chip = null;
        if (queueEntry) {
          const state = queueEntry.status || 'pending';
          const isCurrent = queue.current && queueEntry.path === queue.current.path;
          let pct = state === 'done' || state === 'failed' ? 100 : 0;
          if (isCurrent && Number(queue.current.totalBytes) > 0) {
            pct = Number(queue.current.doneBytes) / Number(queue.current.totalBytes) * 100;
          }
          mini = document.createElement('span');
          mini.className = 'sls-luna-mini';
          const fill = document.createElement('i');
          fill.className = state === 'done' ? 'done' : state === 'failed' ? 'failed' : '';
          fill.style.width = Math.max(2, Number(pctText(pct))) + '%';
          mini.appendChild(fill);
          chip = document.createElement('span');
          chip.className = 'sls-luna-chip ' + state;
          chip.textContent = state === 'done' && Number(queueEntry.saved) > 0
            ? 'done \u00b7 saved ' + formatBytes(queueEntry.saved) : state;
        }
        const excludedNow = excluded.has(game.name);
        const box = document.createElement('div');
        box.className = 'sls-luna-switch' + (excludedNow ? ' on' : '');
        box.appendChild(document.createElement('span'));
        box.onclick = () => {
          const next = listOf(settings.excluded).filter(entry => entry !== game.name);
          if (!excludedNow) next.push(game.name);
          saveSettings({ excluded: next }, body);
        };
        const parts = [text];
        if (mini) parts.push(mini, chip);
        parts.push(box);
        row.append(...parts);
        body.appendChild(row);
      });
      if (rows.length > 80) {
        const more = document.createElement('p');
        more.className = 'sls-luna-note';
        more.textContent = '\u2026and ' + (rows.length - 80) + ' more; filter to narrow the list.';
        body.appendChild(more);
      }
      return body;
    }

    function compressorLog(body) {
      const job = compressor && compressor.job;
      body.appendChild(compressorBusyLine());
      if (!job) return body;
      const actions = document.createElement('div');
      actions.style.cssText = 'display:flex;gap:8px;margin:10px 0;';
      const reload = document.createElement('button');
      reload.className = 'sls-luna-btn secondary';
      reload.textContent = 'Reload log';
      reload.onclick = () => refresh();
      actions.appendChild(reload);
      if (job.running) actions.appendChild(cancelButton(body));
      body.appendChild(actions);
      const pre = document.createElement('pre');
      pre.className = 'sls-luna-log';
      pre.textContent = job.log || 'No output yet.';
      body.appendChild(pre);
      const note = document.createElement('p');
      note.className = 'sls-luna-note';
      note.textContent = 'The last 40 lines; colour codes and progress redraws are removed.';
      body.appendChild(note);
      if (job.running) startJobPolling();
      return body;
    }

    function compressorBody() {
      const body = document.createElement('div');
      // One-shot notice: a failed request paints here instead of into a body
      // that the repaint has already replaced.
      if (compressorNotice) {
        const notice = document.createElement('p');
        notice.className = 'sls-luna-error';
        notice.textContent = compressorNotice;
        body.appendChild(notice);
        compressorNotice = null;
      }
      const tool = (compressor && compressor.tool) || {};
      if (!tool.found) {
        const missing = document.createElement('p');
        missing.className = 'sls-luna-note';
        missing.innerHTML = '<strong>btrfs-game-compressor was not found.</strong> Run its install.sh, or '
          + 'point SLS_COMPRESSOR_BIN at the binary and restart Steam.';
        body.appendChild(missing);
        return body;
      }
      const strip = document.createElement('div');
      strip.className = 'sls-luna-subtabs';
      COMPRESSOR_TABS.forEach(entry => {
        const item = document.createElement('button');
        item.className = 'sls-luna-subtab' + (entry.id === compressorTab ? ' active' : '');
        item.textContent = entry.label;
        item.onclick = () => {
          compressorTab = entry.id;
          paint();
          // The full game list and the statistics arrive with the next refresh,
          // so entering those tabs fills them immediately.
          if (entry.id === 'games' || entry.id === 'stats') refresh();
        };
        strip.appendChild(item);
      });
      body.appendChild(strip);
      // The section builders append into the body they are handed; appending
      // their return value again would make the body its own child.
      if (compressorTab === 'settings') compressorSettings(body);
      else if (compressorTab === 'stats') compressorStats(body);
      else if (compressorTab === 'games') compressorGames(body);
      else if (compressorTab === 'log') compressorLog(body);
      else compressorStatus(body);
      return body;
    }

    // The remembered direction of the library sort; the agent picks it up
    // through a storage event and rewrites the keys right away. The choice is
    // owned by the backend (it syncs across machines and survives a CEF cache
    // reset); localStorage stays the fast mirror the agent reads.
    const HLTB_ORDER_KEY = 'slsLunaHltbOrder';
    const BETA_KEY = 'slsLunaBeta';
    let betaNotice = null;
    function localItem(key) {
      try { return w.localStorage ? w.localStorage.getItem(key) : null; } catch (_) { return null; }
    }
    function setLocalItem(key, value) {
      try { if (w.localStorage) w.localStorage.setItem(key, value); } catch (_) { /* private mode */ }
    }
    function clearLocalItem(key) {
      try { if (w.localStorage) w.localStorage.removeItem(key); } catch (_) { /* private mode */ }
    }
    function prefValue(key) {
      if (!lunaInfo || !lunaInfo.prefs) return undefined;
      return lunaInfo.prefs[key];
    }
    function savePref(key, value) {
      if (lunaInfo) {
        lunaInfo.prefs = Object.assign({}, lunaInfo.prefs || {});
        lunaInfo.prefs[key] = value;
      }
      rpc(w, 'SlsLunaPrefsSet', { raw: JSON.stringify({ key: key, value: value }) })
        .catch(() => { /* the local mirror keeps working this session */ });
    }
    function hltbOrder() {
      const stored = prefValue('hltb_order');
      if (stored === 'long' || stored === 'short') return stored;
      return localItem(HLTB_ORDER_KEY) === 'long' ? 'long' : 'short';
    }
    function setHltbOrder(order) {
      const value = order === 'long' ? 'long' : 'short';
      setLocalItem(HLTB_ORDER_KEY, value);
      savePref('hltb_order', value);
    }
    // Beta pages (artwork picker, Graphics) stay hidden until this switch on the
    // Settings page is on.
    function betaFeatures() {
      const stored = prefValue('beta');
      if (typeof stored === 'boolean') return stored;
      return localItem(BETA_KEY) === '1';
    }
    function setBetaFeatures(on) {
      setLocalItem(BETA_KEY, on ? '1' : '0');
      savePref('beta', Boolean(on));
    }
    function visibleNav() {
      return NAV.filter(entry => !entry.beta || betaFeatures());
    }
    /* One-time: a choice that only existed locally (or on the other machine's
       localStorage) is pushed up once the backend has no value yet; afterwards
       the backend value is mirrored down so the agent keeps reading
       localStorage. Idempotent: refresh() runs it on every open. */
    async function migratePrefs() {
      const prefs = (lunaInfo && lunaInfo.prefs) || {};
      const patch = {};
      if (prefs.beta === undefined && localItem(BETA_KEY) === '1') patch.beta = true;
      if (prefs.hltb_order === undefined && localItem(HLTB_ORDER_KEY) === 'long') patch.hltb_order = 'long';
      for (const key of Object.keys(patch)) {
        try {
          const out = await rpc(w, 'SlsLunaPrefsSet',
            { raw: JSON.stringify({ key: key, value: patch[key] }) });
          if (out && out.prefs) lunaInfo = Object.assign({}, lunaInfo, { prefs: out.prefs });
        } catch (_) { /* the local mirror still works */ }
      }
      const fresh = (lunaInfo && lunaInfo.prefs) || {};
      if (fresh.beta !== undefined) setLocalItem(BETA_KEY, fresh.beta ? '1' : '0');
      if (fresh.hltb_order !== undefined) {
        setLocalItem(HLTB_ORDER_KEY, fresh.hltb_order === 'long' ? 'long' : 'short');
      }
    }

    // ---- HowLongToBeat page: cached times, missing games, on-demand fetch --

    const hltbHours = seconds => (Math.max(0, Number(seconds) || 0) / 3600).toFixed(1) + ' h';

    function hltbTimes(record) {
      if (record.match === 'none') return 'no match found';
      const parts = [];
      if (record.main) parts.push(hltbHours(record.main) + ' story');
      if (record.extra) parts.push(hltbHours(record.extra) + ' +extra');
      if (record.completionist) parts.push(hltbHours(record.completionist) + ' 100%');
      return parts.join(' \u00b7 ') || 'no times';
    }

    const sleep = ms => new Promise(resolve => {
      const schedule = w.setTimeout || setTimeout;
      schedule(resolve, ms);
    });

    // The background flag remembers that the user asked for a full pass, so it
    // survives closing the menu and even a Steam restart. Stopping is explicit.
    const HLTB_BACKGROUND_KEY = 'slsLunaHltbBackground';
    function hltbBackground() {
      try {
        return Boolean(w.localStorage && w.localStorage.getItem(HLTB_BACKGROUND_KEY) === '1');
      } catch (_) { return false; }
    }
    function setHltbBackground(on) {
      try {
        if (!w.localStorage) return;
        if (on) w.localStorage.setItem(HLTB_BACKGROUND_KEY, '1');
        else w.localStorage.removeItem(HLTB_BACKGROUND_KEY);
      } catch (_) { /* private mode or quota; the run itself still works */ }
    }

    const repaintIfOpen = () => {
      if (document.getElementById(OVERLAY_ID)) paint();
    };

    async function refreshHltb() {
      try {
        hltb = await rpc(w, 'SlsHltbGet');
      } catch (error) {
        hltb = hltb || {};
      }
      try { refreshLibraryShelves(); } catch (_) { /* shelves render later */ }
      repaintIfOpen();
      refreshLibraryShelves();
    }

    /* View-local HLTB sorting for this library window. Home shelves are class
       instances with their own m_fnSort(collection, comparator); patching the
       instance keeps every shelf independent: an HLTB shelf sorts numerically
       by cached times (unknown last, either direction), any other shelf keeps
       Steam's own sorter. Shared app objects are never mutated. */
    function hltbSecondsHere(appid) {
      const games = (hltb && hltb.games) || {};
      const record = games[String(appid)];
      if (!record || record.match === 'none') return null;
      const value = Number(record.main) || Number(record.all) || Number(record.extra) || 0;
      return value > 0 ? value : null;
    }
    function compareHltbHere(a, b) {
      const order = hltbOrder();
      const left = hltbSecondsHere(a && a.appid), right = hltbSecondsHere(b && b.appid);
      if (left == null && right != null) return 1;
      if (right == null && left != null) return -1;
      if (left != null && right != null && left !== right) {
        return order === 'long' ? right - left : left - right;
      }
      const an = (a && (a.display_name || a.name)) || '', bn = (b && (b.display_name || b.name)) || '';
      return String(an).localeCompare(String(bn)) || Number(a && a.appid) - Number(b && b.appid);
    }
    function eachFiber(root, visit) {
      const stack = [root];
      while (stack.length) {
        const node = stack.pop();
        if (!node) continue;
        try { visit(node); } catch (_) { /* keep scanning */ }
        try { if (node.sibling) stack.push(node.sibling); } catch (_) {}
        try { if (node.child) stack.push(node.child); } catch (_) {}
      }
    }
    function libraryRootFiber() {
      const divs = (document.querySelectorAll ? [...document.querySelectorAll('div')] : []);
      const el = divs.find(candidate =>
        Object.keys(candidate).some(key => key.indexOf('__reactFiber') === 0));
      if (!el) return null;
      let fiber = el[Object.keys(el).find(key => key.indexOf('__reactFiber') === 0)];
      let depth = 0;
      while (fiber && fiber.return && depth < 500) { fiber = fiber.return; depth += 1; }
      return fiber;
    }
    function patchShelfInstance(instance) {
      if (!instance || instance.__slsHltbShelf) return false;
      if (typeof instance.m_fnSort !== 'function') return false;
      const original = instance.m_fnSort;
      instance.__slsHltbShelf = true;
      instance.__slsOrigSort = original;
      instance.m_fnSort = function (collection, comparator) {
        try {
          if (this.props && this.props.showcase && this.props.showcase.eSortBy === 12
            && collection && Array.isArray(collection.visibleApps)) {
            return collection.visibleApps.slice().sort(compareHltbHere);
          }
        } catch (_) { /* fall through to Steam's sorter */ }
        return original.call(this, collection, comparator);
      };
      return true;
    }
    function refreshLibraryShelves() {
      let root = null;
      try { root = libraryRootFiber(); } catch (_) { root = null; }
      if (!root) return 0;
      let refreshed = 0;
      eachFiber(root, node => {
        const instance = node.stateNode;
        const props = node.memoizedProps;
        if (!instance || !props || !props.showcase) return;
        if (props.showcase.eSortBy !== 12) {
          if (instance.__slsHltbShelf && instance.__slsOrigSort) {
            try { instance.m_fnSort = instance.__slsOrigSort; } catch (_) {}
            instance.__slsHltbShelf = false;
          }
          return;
        }
        if (patchShelfInstance(instance) && typeof instance.forceUpdate === 'function') {
          try { instance.forceUpdate(); refreshed += 1; } catch (_) {}
        } else if (instance.__slsHltbShelf && typeof instance.forceUpdate === 'function') {
          try { instance.forceUpdate(); refreshed += 1; } catch (_) {}
        }
      });
      return refreshed;
    }
    let shelfTimer = null;
    function scheduleShelfRefresh(delay) {
      if (shelfTimer) return;
      const schedule = w.setTimeout || setTimeout;
      shelfTimer = schedule(() => {
        shelfTimer = null;
        refreshLibraryShelves();
      }, delay == null ? 0 : delay);
    }

    async function forgetHltb(row) {
      hltbNotice = null;
      try {
        await rpc(w, 'SlsHltbForget', { appid: Number(row.id) });
        hltbEdit = null;
        await refreshHltb();
      } catch (error) {
        hltbNotice = String(error && error.message ? error.message : error);
        repaintIfOpen();
      }
    }

    async function fetchHltbOne(row) {
      hltbNotice = null;
      try {
        const out = await rpc(w, 'SlsHltbFetch', { appid: Number(row.id), name: row.name });
        if (!out || out.success !== true) throw Error((out && out.error) || 'The fetch failed.');
        await refreshHltb();
      } catch (error) {
        hltbNotice = String(error && error.message ? error.message : error);
        repaintIfOpen();
      }
    }

    async function openHltbSearch(row) {
      hltbNotice = null;
      const url = 'https://howlongtobeat.com/?q=' + encodeURIComponent(String(row.name || '').trim());
      try {
        const out = await rpc(w, '__lumenOpenExternalUrl', { url });
        if (!out || out.ok !== true) throw Error('Steam could not open the browser.');
      } catch (error) {
        hltbNotice = String(error && error.message ? error.message : error);
        paint();
      }
    }

    // Fetch one game with the queue's policy: a game the site cannot match is
    // reported as skipped (the backend remembers it, so it stops counting as
    // missing); rate limiting pauses a minute and retries once; anything else
    // is fatal for the run.
    async function fetchHltbWithPolicy(row, queue) {
      let retried = false;
      for (;;) {
        try {
          const out = await rpc(w, 'SlsHltbFetch', { appid: Number(row.id), name: row.name });
          if (!out || out.success !== true) {
            const error = Error((out && out.error) || 'The fetch failed.');
            error.code = (out && out.code) || '';
            throw error;
          }
          return { skipped: false };
        } catch (error) {
          const code = error && error.code;
          if (code === 'no_match') return { skipped: true };
          if ((code === 'rate_limit' || code === 'throttled') && !retried) {
            retried = true;
            queue.wait = 60;
            queue.label = row.name;
            paint();
            await sleep(60000);
            queue.wait = 0;
            continue;
          }
          throw error;
        }
      }
    }

    // Sequentially fetch every missing game, keeping the site's own spacing.
    // Unmatchable games are skipped and remembered; rate limiting pauses and
    // retries once, then stops and says why. The run keeps going with the menu
    // closed - the overlay is never re-opened by it - and it resumes after a
    // Steam restart while the background flag is set.
    async function runHltbQueue(rows) {
      if (!rows.length || (hltbQueue && hltbQueue.running)) return;
      setHltbBackground(true);
      hltbQueue = { running: true, done: 0, skipped: 0, total: rows.length,
        label: rows[0].name, error: '', wait: 0 };
      repaintIfOpen();
      for (const row of rows) {
        if (!hltbQueue || !hltbQueue.running) break;
        hltbQueue.label = row.name;
        repaintIfOpen();
        try {
          const result = await fetchHltbWithPolicy(row, hltbQueue);
          if (result.skipped) hltbQueue.skipped += 1;
          else hltbQueue.done += 1;
          const fresh = await rpc(w, 'SlsHltbGet').catch(() => null);
          if (fresh && fresh.success !== false) hltb = fresh;
          hltbNotice = null;
          repaintIfOpen();
        } catch (error) {
          hltbQueue.running = false;
          hltbQueue.error = String(error && error.message ? error.message : error);
          repaintIfOpen();
          return;
        }
        await sleep(2200);
      }
      if (hltbQueue) {
        hltbQueue.running = false;
        hltbQueue.label = '';
        // Everything queued was processed (user stops clear the flag first).
        setHltbBackground(false);
        repaintIfOpen();
      }
    }

    // A background pass resumes whenever fresh data arrives (menu open, the
    // boot refresh, or the Refresh button) and the user has not stopped it.
    function maybeAutoFetch() {
      if (hltbQueue && hltbQueue.running) return;
      if (!hltbBackground()) return;
      const records = (hltb && hltb.games) || {};
      const library = (hltb && hltb.library) || [];
      const missing = buildHltbRows(records, library, '').filter(row => !row.record);
      if (!missing.length) {
        setHltbBackground(false);
        return;
      }
      runHltbQueue(missing);
    }

    function hltbSmallButton(label, handler) {
      const button = document.createElement('button');
      button.className = 'sls-luna-btn secondary';
      button.style.cssText = 'padding:4px 9px;font-size:12px;white-space:nowrap;';
      button.textContent = label;
      button.onclick = () => { Promise.resolve(handler()).catch(() => {}); };
      return button;
    }

    function hltbRow(row) {
      const wrap = document.createElement('div');
      const line = document.createElement('div');
      line.className = 'sls-luna-game';
      const name = document.createElement('span');
      name.className = 'gname';
      name.textContent = row.name + ' (#' + row.id + ')' + (row.shortcut ? ' [non-Steam]' : '');
      line.appendChild(name);
      if (row.record) {
        const times = document.createElement('span');
        times.style.cssText = 'color:#9aa4ae;font-size:12px;white-space:nowrap;';
        times.textContent = hltbTimes(row.record);
        const chip = document.createElement('span');
        const manual = row.record.match === 'manual';
        const none = row.record.match === 'none';
        chip.className = 'sls-luna-chip' + (row.record.match === 'exact' || manual ? ' done' : '');
        chip.textContent = none ? 'no match' : manual ? 'manual' : row.record.match === 'exact' ? 'match' : 'approx';
        line.append(times, chip);
        line.append(
          hltbSmallButton('Refresh', () => fetchHltbOne(row)),
          hltbSmallButton('Search', () => openHltbSearch(row)),
          hltbSmallButton('Edit', () => {
            hltbEdit = {
              appid: row.id,
              main: Number(((Number(row.record.main) || 0) / 3600).toFixed(2)),
              extra: Number(((Number(row.record.extra) || 0) / 3600).toFixed(2)),
              completionist: Number(((Number(row.record.completionist) || 0) / 3600).toFixed(2)),
            };
            paint();
          }),
          hltbSmallButton('Forget', () => forgetHltb(row)));
      } else {
        line.append(
          hltbSmallButton('Fetch', () => fetchHltbOne(row)),
          hltbSmallButton('Search', () => openHltbSearch(row)));
      }
      wrap.appendChild(line);
      if (hltbEdit && hltbEdit.appid === row.id) {
        const editor = document.createElement('div');
        editor.style.cssText = 'display:flex;gap:8px;align-items:center;margin:2px 0 10px;flex-wrap:wrap;';
        const fields = [['main', 'story'], ['extra', 'extra'], ['completionist', '100%']];
        const inputs = {};
        fields.forEach(pair => {
          const label = document.createElement('span');
          label.style.cssText = 'color:#9aa4ae;font-size:12px;';
          label.textContent = pair[1] + ' (h)';
          const input = document.createElement('input');
          input.className = 'sls-luna-input';
          input.type = 'number';
          input.min = '0';
          input.step = '0.5';
          input.value = String(hltbEdit[pair[0]] || 0);
          input.oninput = () => { hltbEdit[pair[0]] = Number(input.value) || 0; };
          inputs[pair[0]] = input;
          editor.append(label, input);
        });
        const save = document.createElement('button');
        save.className = 'sls-luna-btn';
        save.textContent = 'Save hours';
        save.onclick = async () => {
          hltbNotice = null;
          try {
            await rpc(w, 'SlsHltbPut', { raw: JSON.stringify({
              appid: Number(row.id),
              name: row.name,
              main: Number(inputs.main.value) || 0,
              extra: Number(inputs.extra.value) || 0,
              completionist: Number(inputs.completionist.value) || 0,
            }) });
            hltbEdit = null;
            await refreshHltb();
          } catch (error) {
            hltbNotice = String(error && error.message ? error.message : error);
            paint();
          }
        };
        const cancel = document.createElement('button');
        cancel.className = 'sls-luna-btn secondary';
        cancel.textContent = 'Cancel';
        cancel.onclick = () => { hltbEdit = null; paint(); };
        editor.append(save, cancel);
        wrap.appendChild(editor);
      }
      return wrap;
    }

    function hltbBody() {
      const body = document.createElement('div');
      const records = (hltb && hltb.games) || {};
      const library = (hltb && hltb.library) || [];
      const all = buildHltbRows(records, library, '');
      const missing = all.filter(row => !row.record);
      const noMatch = all.filter(row => row.record && row.record.match === 'none');

      const info = document.createElement('p');
      info.className = 'sls-luna-note';
      info.innerHTML = 'Times come from HowLongToBeat, fetched <strong>on demand</strong> (never automatically) and '
        + 'stored locally. Fetching waits between games because the site rate-limits searches; games it cannot '
        + 'match are skipped and remembered. A started pass <strong>keeps running in the background</strong> - with '
        + 'this window closed, and across Steam restarts - until it finishes or you stop it. '
        + 'You can correct any match with Edit; nothing here changes Steam files.';
      body.appendChild(info);

      const stats = document.createElement('div');
      stats.className = 'sls-luna-stat';
      [['Cached', all.length - noMatch.length - missing.length, 'games with times'],
        ['No match', noMatch.length, 'skipped, set them by hand'],
        ['Missing', missing.length, 'not fetched yet'],
        ['Library', library.length || '\u2014', 'games seen by Luna']].forEach(pair => {
        const cell = document.createElement('div');
        const value = document.createElement('b');
        value.textContent = String(pair[1]);
        const label = document.createElement('i');
        label.textContent = pair[0] + ' \u00b7 ' + pair[2];
        cell.append(value, label);
        stats.appendChild(cell);
      });
      body.appendChild(stats);

      const orderRow = document.createElement('div');
      orderRow.className = 'sls-luna-row';
      const orderText = document.createElement('div');
      orderText.className = 'txt';
      const orderName = document.createElement('div');
      orderName.className = 'name';
      const orderShort = hltbOrder() === 'short';
      orderName.textContent = orderShort ? 'Library order: shortest first' : 'Library order: longest first';
      const orderSub = document.createElement('div');
      orderSub.className = 'sub';
      orderSub.textContent = 'Applies to the library\u2019s HowLongToBeat sort. Games without times always stay at the end. '
        + 'The library refreshes the new order by itself within a moment.';
      orderText.append(orderName, orderSub);
      const orderSwitch = document.createElement('div');
      orderSwitch.className = 'sls-luna-switch' + (orderShort ? ' on' : '');
      orderSwitch.appendChild(document.createElement('span'));
      orderSwitch.onclick = () => {
        setHltbOrder(orderShort ? 'long' : 'short');
        repaintIfOpen();
      };
      orderRow.append(orderText, orderSwitch);
      body.appendChild(orderRow);

      if (!library.length) {
        const note = document.createElement('p');
        note.className = 'sls-luna-note';
        note.textContent = 'Luna has not received the game list from Steam yet. It arrives at startup; '
          + 'restart Steam or reopen this page if the list stays empty.';
        body.appendChild(note);
      }

      hltbSearch.value = hltbQuery;
      body.appendChild(hltbSearch);

      const busy = Boolean(hltbQueue && hltbQueue.running);
      const missingFiltered = buildHltbRows(records, library, hltbQuery).filter(row => !row.record);
      const actions = document.createElement('div');
      actions.style.cssText = 'display:flex;gap:8px;flex-wrap:wrap;margin:0 0 10px;';
      const fetchMissing = document.createElement('button');
      fetchMissing.className = 'sls-luna-btn';
      fetchMissing.textContent = busy ? 'Fetching\u2026' : 'Fetch missing (' + missingFiltered.length + ')';
      fetchMissing.disabled = busy || missingFiltered.length === 0;
      fetchMissing.onclick = () => runHltbQueue(missingFiltered);
      actions.appendChild(fetchMissing);
      if (busy) {
        const stop = document.createElement('button');
        stop.className = 'sls-luna-btn secondary';
        stop.textContent = 'Stop after current';
        stop.onclick = () => {
          if (hltbQueue) hltbQueue.running = false;
          setHltbBackground(false);
          repaintIfOpen();
        };
        actions.appendChild(stop);
      }
      body.appendChild(actions);

      if (hltbQueue) {
        const line = document.createElement('p');
        line.className = 'sls-luna-note';
        const skipText = hltbQueue.skipped ? ', ' + hltbQueue.skipped + ' skipped (no match)' : '';
        if (hltbQueue.error) {
          line.innerHTML = '<strong>Stopped:</strong> ' + hltbQueue.error;
        } else if (hltbQueue.wait) {
          line.innerHTML = '<strong>Rate-limited.</strong> Waiting ' + hltbQueue.wait
            + 's before retrying \u201c' + (hltbQueue.label || '') + '\u201d\u2026';
        } else if (hltbQueue.running) {
          line.innerHTML = '<strong>Fetching ' + (hltbQueue.done + hltbQueue.skipped + 1)
            + ' of ' + hltbQueue.total + '</strong> \u00b7 ' + (hltbQueue.label || '') + '\u2026'
            + (hltbQueue.skipped ? ' (skipped ' + hltbQueue.skipped + ')' : '')
            + ' <span style="color:#8d97a1">\u00b7 keeps running with this window closed</span>';
        } else {
          line.textContent = 'Fetched ' + hltbQueue.done + ' game(s)' + skipText + '.';
        }
        body.appendChild(line);
        body.appendChild(progressBar(
          hltbQueue.total ? ((hltbQueue.done + hltbQueue.skipped) / hltbQueue.total) * 100 : 0,
          hltbQueue.running || Boolean(hltbQueue.wait)));
      }

      if (hltbNotice) {
        const error = document.createElement('p');
        error.className = 'sls-luna-error';
        error.textContent = hltbNotice;
        body.appendChild(error);
      }

      const rows = buildHltbRows(records, library, hltbQuery);
      if (!rows.length) {
        const empty = document.createElement('p');
        empty.className = 'sls-luna-note';
        empty.textContent = library.length || Object.keys(records).length
          ? 'Nothing matches that filter.' : 'No games known yet.';
        body.appendChild(empty);
        return body;
      }
      const list = document.createElement('div');
      rows.slice(0, visible).forEach(row => list.appendChild(hltbRow(row)));
      body.appendChild(list);
      if (rows.length > visible) {
        const more = document.createElement('button');
        more.className = 'sls-luna-btn secondary';
        more.style.marginTop = '10px';
        more.textContent = 'Show 50 more (' + (rows.length - visible) + ' left)';
        more.onclick = () => { visible += 50; paint(); };
        body.appendChild(more);
      }
      return body;
    }

    // ---- SteamGridDB page: key, search, assets, apply, restore -------------

    async function refreshSgdb() {
      try {
        sgdb = await rpc(w, 'SlsSgdbGet');
      } catch (error) {
        sgdb = sgdb || {};
      }
      repaintIfOpen();
    }

    async function saveSgdbKey() {
      // Read the field before any repaint: the backend never hands the key
      // back (masked only), so the input is write-only and cleared on save.
      const key = sgdbKeyInput.value.trim();
      sgdbNotice = null;
      sgdbBusy = true;
      repaintIfOpen();
      try {
        await rpc(w, 'SlsSgdbSetKey', { raw: JSON.stringify({ key }) });
        await refreshSgdb();
        sgdbKeyInput.value = '';
        sgdbNotice = key ? 'Key saved.' : 'Key cleared.';
      } catch (error) {
        sgdbNotice = String(error && error.message ? error.message : error);
      }
      sgdbBusy = false;
      repaintIfOpen();
    }

    async function testSgdbKey() {
      sgdbNotice = null;
      sgdbBusy = true;
      repaintIfOpen();
      try {
        await rpc(w, 'SlsSgdbTest');
        sgdbNotice = 'SteamGridDB is reachable and the key works.';
      } catch (error) {
        sgdbNotice = String(error && error.message ? error.message : error);
      }
      sgdbBusy = false;
      repaintIfOpen();
    }

    async function searchSgdb(term) {
      if (!term) { sgdbNotice = 'Type a game name first.'; repaintIfOpen(); return; }
      sgdbNotice = null;
      sgdbBusy = true;
      sgdbResults = null;
      repaintIfOpen();
      try {
        const out = await rpc(w, 'SlsSgdbSearch', { raw: JSON.stringify({ term }) });
        sgdbResults = Array.isArray(out.games) ? out.games : [];
        if (!sgdbResults.length) {
          sgdbNotice = 'No SteamGridDB game matched "' + term + '".';
        } else if (sgdbGame && !sgdbGame.sgdbId) {
          // Coming from a right-click request: use the best match but keep the
          // list visible so a different game can be picked.
          sgdbGame.sgdbId = Number(sgdbResults[0].id);
          sgdbGame.sgdbName = sgdbResults[0].name;
          loadSgdbAssets();
        }
      } catch (error) {
        sgdbNotice = String(error && error.message ? error.message : error);
      }
      sgdbBusy = false;
      repaintIfOpen();
    }

    function pickSgdbGame(game) {
      if (!game) return;
      sgdbGame = Object.assign({}, sgdbGame || {}, { sgdbId: Number(game.id), sgdbName: game.name });
      sgdbResults = null;
      loadSgdbAssets();
    }

    async function loadSgdbAssets() {
      if (!sgdbGame || !sgdbGame.sgdbId) return;
      sgdbAssets = null;
      sgdbNotice = null;
      sgdbBusy = true;
      repaintIfOpen();
      try {
        const out = await rpc(w, 'SlsSgdbAssets', {
          raw: JSON.stringify({ gameId: sgdbGame.sgdbId, kind: sgdbKind }),
        });
        sgdbAssets = Array.isArray(out.assets) ? out.assets : [];
        if (!sgdbAssets.length) {
          sgdbNotice = 'No usable ' + sgdbKind + ' images for this game (PNG or JPEG only).';
        }
      } catch (error) {
        sgdbNotice = String(error && error.message ? error.message : error);
      }
      sgdbBusy = false;
      repaintIfOpen();
    }

    async function applySgdbAsset(asset) {
      if (!sgdbGame) return;
      sgdbNotice = null;
      sgdbBusy = true;
      repaintIfOpen();
      try {
        const out = await rpc(w, 'SlsSgdbApply', {
          raw: JSON.stringify({ appid: Number(sgdbGame.appid), kind: sgdbKind,
            url: asset.url, mime: asset.mime }),
        });
        sgdbNotice = 'Artwork applied' + (out && out.backedUp ? ' (the previous file is backed up)' : '') + '.';
        const liveKinds = out && out.live ? [sgdbKind] : [];
        const at = liveKinds.length ? requestLiveArtwork(sgdbGame.appid, liveKinds) : null;
        sgdbBusy = false;
        repaintIfOpen();
        if (at) finishLiveNotice(at, 'Applied and refreshed in Steam right away.');
      } catch (error) {
        sgdbNotice = String(error && error.message ? error.message : error);
        sgdbBusy = false;
        repaintIfOpen();
      }
    }

    // The live refresh runs in the agent (only SharedJSContext has
    // SteamClient.Apps). The panel writes the request and reads the agent's
    // result a moment later so the page can report what really happened.
    function requestLiveArtwork(appid, kinds) {
      try {
        if (!w.localStorage) return null;
        const at = Date.now();
        w.localStorage.setItem('slsLunaArtworkLive',
          JSON.stringify({ appid: Number(appid), kinds, at }));
        return at;
      } catch (_) { return null; }
    }

    function readLiveResult(at) {
      try {
        const raw = w.localStorage && w.localStorage.getItem('slsLunaArtworkLiveResult');
        if (!raw) return null;
        const result = JSON.parse(raw);
        return result && result.at === at ? result : null;
      } catch (_) { return null; }
    }

    function finishLiveNotice(at, successText) {
      const schedule = w.setTimeout || setTimeout;
      schedule(() => {
        const result = readLiveResult(at);
        if (!result) {
          sgdbNotice = (sgdbNotice || '') + ' Steam will show it after the next refresh.';
          repaintIfOpen();
          return;
        }
        const failed = result.results.filter(entry => !entry.ok);
        sgdbNotice = failed.length
          ? 'Applied, but Steam could not refresh live for ' + failed.map(entry => entry.kind).join(', ')
            + ' - restart Steam to see it.'
          : successText;
        repaintIfOpen();
      }, 2500);
    }

    async function autoApplySgdb() {
      if (!sgdbGame || !sgdbGame.sgdbId) return;
      sgdbNotice = null;
      sgdbBusy = true;
      repaintIfOpen();
      try {
        const out = await rpc(w, 'SlsSgdbAuto', {
          raw: JSON.stringify({ appid: Number(sgdbGame.appid), gameId: sgdbGame.sgdbId }),
        });
        const results = Array.isArray(out.results) ? out.results : [];
        const ok = results.filter(entry => entry.ok);
        const failed = results.filter(entry => !entry.ok);
        sgdbNotice = 'Applied the best match to ' + ok.length + ' of ' + results.length + ' slots'
          + (failed.length ? ' (' + failed.map(entry => entry.kind).join(', ') + ' had nothing usable)' : '') + '.';
        const liveKinds = ok.filter(entry => entry.live).map(entry => entry.kind);
        const at = liveKinds.length ? requestLiveArtwork(sgdbGame.appid, liveKinds) : null;
        sgdbBusy = false;
        repaintIfOpen();
        if (at) finishLiveNotice(at, 'Steam refreshed every slot live.');
      } catch (error) {
        sgdbNotice = String(error && error.message ? error.message : error);
        sgdbBusy = false;
        repaintIfOpen();
      }
    }

    async function undoSgdbAsset() {
      if (!sgdbGame) return;
      sgdbNotice = null;
      sgdbBusy = true;
      repaintIfOpen();
      try {
        const out = await rpc(w, 'SlsSgdbUndo', {
          raw: JSON.stringify({ appid: Number(sgdbGame.appid), kind: sgdbKind }),
        });
        sgdbNotice = 'Undone: the previous artwork is back'
          + (out && typeof out.history === 'number' && out.history > 0
            ? ' (' + out.history + ' step' + (out.history === 1 ? '' : 's') + ' left)' : '')
          + '.';
        const at = requestLiveArtwork(sgdbGame.appid, [sgdbKind]);
        sgdbBusy = false;
        repaintIfOpen();
        if (at) finishLiveNotice(at, '');
      } catch (error) {
        sgdbNotice = String(error && error.message ? error.message : error);
        sgdbBusy = false;
        repaintIfOpen();
      }
    }

    async function resetSgdbAsset() {
      if (!sgdbGame) return;
      sgdbNotice = null;
      sgdbBusy = true;
      repaintIfOpen();
      try {
        const out = await rpc(w, 'SlsSgdbReset', {
          raw: JSON.stringify({ appid: Number(sgdbGame.appid), kind: sgdbKind }),
        });
        sgdbNotice = out && out.removed
          ? 'Custom artwork removed; Steam shows its own again.'
          : 'Nothing custom in this slot; Steam already shows its own.';
      } catch (error) {
        sgdbNotice = String(error && error.message ? error.message : error);
      }
      sgdbBusy = false;
      repaintIfOpen();
    }

    // The agent's "Change artwork" action writes a request into localStorage
    // (it has no DOM); the panel owns the window, so it opens the picker here.
    // One-shot: the key is claimed (removed) on first read so a restart never
    // replays an old request. Stale requests (older than the TTL) are ignored.
    const ARTWORK_REQUEST_TTL_MS = 5 * 60 * 1000;
    function consumeArtworkRequest() {
      let raw = null;
      try { raw = w.localStorage && w.localStorage.getItem('slsLunaArtworkRequest'); } catch (_) {}
      if (!raw || raw === artworkRequest) return false;
      artworkRequest = raw;
      try { if (w.localStorage) w.localStorage.removeItem('slsLunaArtworkRequest'); } catch (_) {}
      let request = null;
      try { request = JSON.parse(raw); } catch (_) { return false; }
      if (!request || !request.appid) return false;
      if (typeof request.at === 'number' && Number.isFinite(request.at)
        && Date.now() - request.at > ARTWORK_REQUEST_TTL_MS) return false;
      if (!betaFeatures()) {
        // The picker is a beta page; point the user at the switch instead of
        // silently doing nothing.
        tab = 'settings';
        betaNotice = 'A game asked for the artwork picker, but beta features are off. '
          + 'Turn them on to use SteamGridDB artwork.';
        openMenu();
        return true;
      }
      betaNotice = null;
      sgdbGame = { appid: Number(request.appid), name: String(request.name || ('App ' + request.appid)) };
      sgdbResults = null;
      sgdbAssets = null;
      sgdbNotice = null;
      sgdbKind = 'grid';
      tab = 'steamgrid';
      openMenu();
      // The status (key, grid folder) may not be loaded yet: refresh first,
      // then search for the requested game's artwork.
      refreshSgdb().then(() => {
        if (sgdb && sgdb.hasKey) searchSgdb(sgdbGame.name);
        else { sgdbNotice = 'Add your SteamGridDB API key below, then press Find game.'; repaintIfOpen(); }
      });
      return true;
    }

    function steamgridBody() {
      const body = document.createElement('div');
      const hasKey = Boolean(sgdb && sgdb.hasKey);
      const info = document.createElement('p');
      info.className = 'sls-luna-note';
      info.innerHTML = 'Artwork comes from <strong>SteamGridDB</strong> with your own free API key '
        + '(steamgriddb.com \u2192 profile \u2192 API). The key is stored owner-only on this machine '
        + 'and never shown again. Chosen images are written to Steam\u2019s custom '
        + 'artwork folder with undo history, and Steam artwork removes the custom one.';
      body.appendChild(info);

      sgdbKeyInput.placeholder = hasKey
        ? 'Key stored (' + ((sgdb && sgdb.keyMasked) || 'stored') + ') \u2014 type a new one to replace it'
        : 'SteamGridDB API key';
      body.appendChild(sgdbKeyInput);
      const keyRow = document.createElement('div');
      keyRow.style.cssText = 'display:flex;gap:8px;flex-wrap:wrap;margin:0 0 4px;';
      const save = document.createElement('button');
      save.className = 'sls-luna-btn';
      save.textContent = 'Save key';
      save.disabled = sgdbBusy;
      save.onclick = () => saveSgdbKey();
      const test = document.createElement('button');
      test.className = 'sls-luna-btn secondary';
      test.textContent = 'Test key';
      test.disabled = sgdbBusy || !hasKey;
      test.onclick = () => testSgdbKey();
      keyRow.append(save, test);
      body.appendChild(keyRow);

      if (hasKey && sgdb && sgdb.gridDir) {
        const where = document.createElement('p');
        where.className = 'sls-luna-note';
        where.innerHTML = 'Writing to <strong>' + sgdb.gridDir + '</strong>';
        body.appendChild(where);
      } else if (hasKey && sgdb && sgdb.gridError) {
        const broken = document.createElement('p');
        broken.className = 'sls-luna-error';
        broken.textContent = 'Steam artwork folder unavailable: ' + sgdb.gridError;
        body.appendChild(broken);
      }

      if (sgdbNotice) {
        const notice = document.createElement('p');
        notice.className = /(fail|reject|unavailable|error)/i.test(sgdbNotice) ? 'sls-luna-error' : 'sls-luna-note';
        notice.textContent = sgdbNotice;
        body.appendChild(notice);
      }
      if (sgdbBusy) {
        const busy = document.createElement('p');
        busy.className = 'sls-luna-note';
        busy.textContent = 'Working\u2026';
        body.appendChild(busy);
      }

      if (!hasKey) return body;

      const searchRow = document.createElement('div');
      searchRow.style.cssText = 'display:flex;gap:8px;margin:10px 0 4px;';
      const find = document.createElement('button');
      find.className = 'sls-luna-btn';
      find.textContent = 'Find game';
      find.disabled = sgdbBusy;
      find.onclick = () => searchSgdb(sgdbSearch.value.trim());
      searchRow.append(sgdbSearch, find);
      body.appendChild(searchRow);

      if (sgdbResults && sgdbResults.length) {
        const list = document.createElement('div');
        list.style.cssText = 'display:flex;flex-wrap:wrap;gap:6px;margin:6px 0 10px;';
        sgdbResults.forEach(game => {
          const chip = document.createElement('button');
          chip.className = 'sls-luna-subtab';
          chip.textContent = game.name;
          chip.onclick = () => pickSgdbGame(game);
          list.appendChild(chip);
        });
        body.appendChild(list);
      }

      if (!sgdbGame) {
        const hint = document.createElement('p');
        hint.className = 'sls-luna-note';
        hint.innerHTML = 'Right-click any game in the library \u2192 <strong>Change artwork</strong>, or search '
          + 'above. Nothing is written until you pick an image.';
        body.appendChild(hint);
        return body;
      }

      const head = document.createElement('p');
      head.className = 'sls-luna-note';
      // Names come from the local library and SteamGridDB: text, never HTML.
      const esc = text => String(text == null ? '' : text)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
      head.innerHTML = 'Artwork for <strong>' + esc(sgdbGame.name) + '</strong> (#' + sgdbGame.appid + ')'
        + (sgdbGame.sgdbName && sgdbGame.sgdbName !== sgdbGame.name
          ? ' \u00b7 matched as &quot;' + esc(sgdbGame.sgdbName) + '&quot;' : '')
        + (sgdbGame.sgdbId ? '' : ' \u00b7 <span style="color:#8d97a1">searching\u2026</span>');
      body.appendChild(head);

      const kinds = [['grid', 'Grid 600\u00d7900'], ['wide', 'Wide 920\u00d7430'], ['hero', 'Hero'],
        ['logo', 'Logo'], ['icon', 'Icon']];
      const chips = document.createElement('div');
      chips.className = 'sls-luna-subtabs';
      kinds.forEach(pair => {
        const chip = document.createElement('button');
        chip.className = 'sls-luna-subtab' + (sgdbKind === pair[0] ? ' active' : '');
        chip.textContent = pair[1];
        chip.onclick = () => { sgdbKind = pair[0]; loadSgdbAssets(); };
        chips.appendChild(chip);
      });
      body.appendChild(chips);

      if (sgdbAssets && sgdbAssets.length) {
        const grid = document.createElement('div');
        grid.className = 'sls-luna-assets';
        sgdbAssets.forEach(asset => {
          const card = document.createElement('div');
          card.className = 'sls-luna-asset';
          card.title = (asset.style || '') + (asset.author ? ' \u00b7 ' + asset.author : '');
          const img = document.createElement('img');
          img.src = asset.thumb || asset.url;
          img.alt = '';
          const who = document.createElement('div');
          who.className = 'who';
          who.textContent = (asset.width && asset.height ? asset.width + '\u00d7' + asset.height : '')
            + (asset.author ? ' \u00b7 ' + asset.author : '');
          card.append(img, who);
          card.onclick = () => applySgdbAsset(asset);
          grid.appendChild(card);
        });
        body.appendChild(grid);
      }

      const actions = document.createElement('div');
      actions.style.cssText = 'display:flex;gap:8px;flex-wrap:wrap;margin:10px 0 0;';
      const auto = document.createElement('button');
      auto.className = 'sls-luna-btn';
      auto.textContent = 'Apply best to every slot';
      auto.disabled = sgdbBusy || !sgdbGame.sgdbId;
      auto.onclick = () => autoApplySgdb();
      const undo = document.createElement('button');
      undo.className = 'sls-luna-btn secondary';
      undo.textContent = 'Undo ' + sgdbKind;
      undo.title = 'Step back through Luna-applied artwork for this slot';
      undo.disabled = sgdbBusy;
      undo.onclick = () => undoSgdbAsset();
      const reset = document.createElement('button');
      reset.className = 'sls-luna-btn secondary';
      reset.textContent = 'Steam artwork';
      reset.title = 'Remove the custom file so Steam shows its own artwork';
      reset.disabled = sgdbBusy;
      reset.onclick = () => resetSgdbAsset();
      const close = document.createElement('button');
      close.className = 'sls-luna-btn secondary';
      close.textContent = 'Close picker';
      close.onclick = () => {
        sgdbGame = null; sgdbAssets = null; sgdbResults = null; sgdbNotice = null;
        repaintIfOpen();
      };
      actions.append(auto, undo, reset, close);
      body.appendChild(actions);
      return body;
    }

    // ---- Graphics page (beta): read-only detection -------------------------

    function graphicsBody() {
      const body = document.createElement('div');
      const report = graphics || {};
      const system = report.system || {};
      const lsfg = report.lsfg || {};
      const layers = Array.isArray(lsfg.layers) ? lsfg.layers : [];
      const gpus = Array.isArray(report.gpus) ? report.gpus : [];
      const opti = Array.isArray(report.optiscaler) ? report.optiscaler : [];
      const steamInfo = report.steam || {};

      const note = document.createElement('p');
      note.className = 'sls-luna-note';
      note.innerHTML = 'Luna only <strong>detects</strong> what this machine has for frame generation and '
        + 'upscaling. Nothing here installs, downloads or changes files - the installers come later, '
        + 'each behind its own gates.';
      body.appendChild(note);

      const stat = document.createElement('div');
      stat.className = 'sls-luna-stat';
      [['GPU', gpus.length || '\u2014', gpus.map(gpu => gpu.vendor_name).join(' + ') || 'not detected'],
        ['lsfg-vk', layers.length ? 'present' : 'not detected', layers.length ? 'Vulkan layer found' : 'no layer found'],
        ['OptiScaler', opti.length ? 'present' : 'not detected', opti.length ? 'management files' : 'nothing found']]
        .forEach(pair => {
          const cell = document.createElement('div');
          const value = document.createElement('b');
          value.textContent = String(pair[1]);
          const label = document.createElement('i');
          label.textContent = pair[0] + ' \u00b7 ' + pair[2];
          cell.append(value, label);
          stat.appendChild(cell);
        });
      body.appendChild(stat);

      const section = title => {
        const line = document.createElement('p');
        line.className = 'sls-luna-note';
        line.innerHTML = '<strong>' + title + '</strong>';
        body.appendChild(line);
      };
      const line = text => {
        const node = document.createElement('p');
        node.className = 'sls-luna-note';
        node.textContent = text;
        body.appendChild(node);
      };

      section('System');
      line((system.distro || 'unknown distro') + (system.kernel ? ' \u00b7 kernel ' + system.kernel : ''));

      section('GPU');
      if (!gpus.length) line('No GPU was detected through sysfs.');
      gpus.forEach(gpu => {
        line(gpu.card + ': ' + (gpu.product || gpu.vendor_name) + ' \u00b7 driver ' + (gpu.driver || '?')
          + ' (' + gpu.vendor + ':' + (gpu.device || '?') + ')');
      });

      section('Frame generation (lsfg-vk)');
      if (!layers.length) {
        line('No lsfg-vk Vulkan layer found. Luna does not install it yet.');
      } else {
        layers.forEach(layer => {
          line('Layer: ' + layer.path + (layer.version ? ' (v' + layer.version + ')' : ''));
        });
        line(lsfg.config
          ? 'User config: ' + lsfg.config.path
          : 'No lsfg-vk user config found (it appears after the first configuration).');
      }
      if (Array.isArray(lsfg.decky_plugins) && lsfg.decky_plugins.length) {
        line('Decky plugin detected: ' + lsfg.decky_plugins.map(path => path.split('/').pop()).join(', ')
          + ' (separate project; Luna is not using or removing it).');
      }

      section('OptiScaler');
      if (!opti.length) {
        line('No OptiScaler or OptiScaler Client install detected in the usual user locations.');
      } else {
        opti.forEach(entry => {
          line((entry.kind === 'client' ? 'Client: ' : 'Files: ') + entry.path);
        });
      }
      line('Per-game OptiScaler management is planned; today Luna only reports what exists.');

      section('OptiScaler Client (Luna port, read-only)');
      const client = report.optiscaler_client;
      if (!client) {
        line('No OptiScaler Client configuration found.');
      } else {
        line('Config: ' + client.config + (client.app_dir ? ' \u00b7 app: ' + client.app_dir : '')
          + ' \u00b7 ' + (Number(client.managed) || 0) + ' game(s) managed, '
          + (Number(client.versions_cached) || 0) + ' OptiScaler version(s) cached.');
        (Array.isArray(client.games) ? client.games : []).forEach(game => {
          const bits = [];
          if (game.installed) bits.push('OptiScaler installed');
          else if (Array.isArray(game.files) && game.files.length) bits.push('files found');
          if (game.fsr_version) bits.push('FSR ' + game.fsr_version + (game.fsr_native ? ' native' : ''));
          if (game.fsr_swapped) bits.push('FSR4 swapped');
          if (game.dlss_via) bits.push('DLSS via OptiScaler');
          if (game.xess_via) bits.push('XeSS via OptiScaler');
          if (game.exists === false) bits.push('path missing');
          line(game.name + ' \u00b7 ' + (bits.join(', ') || 'nothing detected'));
        });
        line('This is the read side of the Luna port: it only reports what the client already did, without '
          + 'touching its files. Deployment and per-game configuration come later, behind their own gate.');
      }

      section('Steam');
      line((steamInfo.root ? 'Steam root: ' + steamInfo.root : 'No bootstrapped Steam installation found.')
        + (Array.isArray(steamInfo.runtimes) && steamInfo.runtimes.length
          ? ' \u00b7 runtimes: ' + steamInfo.runtimes.join(', ') : '')
        + (steamInfo.gamescope ? ' \u00b7 gamescope present' : ''));
      return body;
    }

    /* Health page: local diagnostics only. Paths, versions, marker counts and
       file sizes, so a problem can be described without a shell. */
    function healthBody() {
      const body = document.createElement('div');
      const health = lunaHealth || null;
      if (healthNotice) {
        const notice = document.createElement('p');
        notice.className = 'sls-luna-error';
        notice.textContent = healthNotice;
        body.appendChild(notice);
        healthNotice = null;
      }
      const note = document.createElement('p');
      note.className = 'sls-luna-note';
      note.innerHTML = 'Local diagnostics only: paths, versions, marker counts and file sizes. '
        + 'Nothing leaves this machine - use <strong>Copy diagnostics</strong> when reporting a problem.';
      body.appendChild(note);
      if (!health) {
        const empty = document.createElement('p');
        empty.className = 'sls-luna-note';
        empty.textContent = 'No health report yet - the next Refresh loads it.';
        body.appendChild(empty);
        return body;
      }
      const line = text => {
        const node = document.createElement('p');
        node.className = 'sls-luna-note';
        node.textContent = text;
        body.appendChild(node);
      };
      const section = title => {
        const node = document.createElement('p');
        node.className = 'sls-luna-note';
        node.innerHTML = '<strong>' + title + '</strong>';
        body.appendChild(node);
      };
      const lumen = health.lumen || {};
      const boot = lumen.boot || {};
      const steam = health.steam || {};
      const data = health.data || {};
      const install = health.install || {};
      const steamClient = steam.client || {};
      const upd = lunaUpdate || {};
      // Plain-language attention list first: every entry says what is wrong
      // and the exact next step. The details below stay for bug reports.
      const issues = [];
      if (upd.available && !upd.pending) {
        issues.push('Luna update available: ' + (upd.latest || '?') + ' (you have '
          + (upd.installed || '?') + '). Press Install update below, then restart Steam.');
      }
      if (upd.pending) {
        issues.push('Luna update queued (' + (upd.pendingTag || 'latest')
          + '): it lands the next time Steam is closed.');
      }
      if (steamClient.pinned && !steamClient.match) {
        issues.push('Steam updated itself past the pin (' + (steamClient.build || '?') + ' vs pinned '
          + steamClient.pinned + '). Close Steam and run: luna steam recover');
      }
      if (steamClient.pinned && steamClient.pacmanManaged && !steamClient.pacmanIgnored) {
        issues.push('Steam comes from pacman and is NOT ignored: a system update can replace the '
          + 'launcher. Add "IgnorePkg = steam" under [options] in /etc/pacman.conf (needs sudo).');
      }
      if (steamClient.pinned && !steamClient.updatesBlocked) {
        issues.push('A Steam build is pinned but updates are NOT blocked. Close Steam and run: '
          + 'luna steam recover (it re-applies steam.cfg).');
      }
      if (Number(steam.localconfigConflicts || 0) > 0) {
        issues.push('Steam sync conflicts: ' + steam.localconfigConflicts + ' localconfig cop'
          + (Number(steam.localconfigConflicts) === 1 ? 'y' : 'ies')
          + ' waiting - close Steam on both machines, let Syncthing settle, then run: luna reconcile');
      }
      if ((lumen.addons || []).length && !boot.patched) {
        issues.push('Add-ons are installed but boot.lua is not patched. Run: scripts/install-local.sh');
      }
      if ((install.drift || []).length) {
        issues.push('Installed files differ from the pinned bundle: '
          + install.drift.slice(0, 2).join('; ')
          + (install.drift.length > 2 ? ' (+' + (install.drift.length - 2) + ' more)' : '')
          + '. Run: python3 scripts/luna.py verify');
      }
      if ((upd.autoUpdate || 'off') !== 'off' && !upd.timerUnits && !upd.pending) {
        issues.push('Unattended updates are on but this machine has no timer. Pick the mode again to enable it.');
      }
      section(issues.length ? 'Needs attention (' + issues.length + ')' : 'Status');
      if (!issues.length) line('Everything looks good.');
      issues.forEach(item => {
        const node = document.createElement('p');
        node.className = 'sls-luna-error';
        node.textContent = item;
        body.appendChild(node);
      });
      const cards = document.createElement('div');
      cards.className = 'sls-luna-stat';
      [['Add-ons', (lumen.addons || []).length, 'under sls-addons'],
       ['Boot patch', boot.patched ? 'yes' : 'no', (boot.markers || 0) + ' marker(s)'],
       ['Backup', boot.backup ? 'kept' : 'absent', 'boot.lua original'],
       ['Steam client', (steamClient.build || '?'), (steamClient.pinned
         ? (steamClient.match ? 'pinned ' + steamClient.pinned + ' \u2713' : 'PINNED ' + steamClient.pinned + ' \u2014 drifted')
         : 'not pinned')],
       ['HLTB cache', (data.hltb && data.hltb.cached) || 0, 'cached games']]
        .forEach(pair => {
          const cell = document.createElement('div');
          const value = document.createElement('b');
          value.textContent = String(pair[1]);
          const label = document.createElement('i');
          label.textContent = pair[0] + ' \u00b7 ' + pair[2];
          cell.append(value, label);
          cards.appendChild(cell);
        });
      body.appendChild(cards);

      section('Add-ons');
      const pinned = install.versions || {};
      (lumen.addons || []).forEach(entry => {
        const want = pinned[entry.name];
        line(entry.name + ' ' + (entry.version || '?')
          + (want && String(want) !== String(entry.version) ? ' \u00b7 pinned ' + want : '')
          + ' \u00b7 ' + (entry.files || 0) + ' entries');
      });
      if ((install.drift || []).length) line('Drift: ' + install.drift.join('; '));

      section('Paths');
      line('Lumen: ' + (lumen.root || '?') + (lumen.binary ? ' (binary present)' : ' (binary missing)')
        + (lumen.luatools ? ' \u00b7 LuaTools present' : ' \u00b7 LuaTools missing')
        + (lumen.bridge ? ' \u00b7 bridge present' : ' \u00b7 bridge missing'));
      line('Boot: ' + (boot.path || '?'));
      line('Steam: ' + (steam.root || '?') + (steam.exists ? '' : ' (steam.sh missing)'));
      if (steamClient.build || steamClient.pinned) {
        line('Steam client: ' + (steamClient.build || '?')
          + (steamClient.pinned ? (steamClient.match ? ' \u00b7 pinned ' + steamClient.pinned + ' \u2713' : ' \u00b7 PINNED ' + steamClient.pinned + ' \u2014 drifted') : ' \u00b7 not pinned')
          + ' \u00b7 updates ' + (steamClient.updatesBlocked ? 'blocked' : 'allowed')
          + (steamClient.cacheReady ? ' \u00b7 cache ready (' + (steamClient.cachedBuild || '?') + ')' : '')
          + (steamClient.pacmanManaged ? ' \u00b7 pacman-managed' + (steamClient.pacmanIgnored ? ' (ignored)' : ' (NOT ignored)') : ''));
      }
      if (Number(steam.localconfigConflicts || 0) > 0) {
        line('Steam sync conflicts: ' + steam.localconfigConflicts + ' localconfig cop'
          + (Number(steam.localconfigConflicts) === 1 ? 'y' : 'ies')
          + ' waiting - close Steam on both machines, let Syncthing settle, then run: luna reconcile');
      }
      line('Data: ' + (data.dir || '?'));
      if (install.installedAt || install.generated) {
        line('Last install: ' + (install.installedAt ? clock(install.installedAt) : '?')
          + (install.generated ? ' \u00b7 manifest ' + String(install.generated).slice(0, 10) : '')
          + (install.source ? ' \u00b7 ' + install.source : ''));
      } else {
        line('Last install: not recorded by this installer version (run install --force once).');
      }

      section('Settings');
      const prefs = data.prefs || {};
      line('Beta features: ' + (prefs.beta ? 'on' : 'off')
        + ' \u00b7 HLTB order: ' + (prefs.hltb_order || 'short')
        + ' \u00b7 Store filter: ' + (prefs.store_hide_followed ? 'on' : 'off')
        + ' \u00b7 SteamGridDB key: ' + (data.sgdb && data.sgdb.hasKey ? 'stored' : 'not set'));

      section('Data files');
      (data.files || []).forEach(entry => {
        line(entry.name + (entry.directory
          ? '/ (' + (entry.files != null ? entry.files : 0) + ' entries)'
          : ' \u00b7 ' + formatBytes(entry.size || 0)));
      });

      section('Updates');
      const update = lunaUpdate || {};
      line('Installed: ' + (update.installed || '?') + ' \u00b7 latest: ' + (update.latest || 'unknown')
        + (update.available ? ' \u00b7 update available' : (update.latest ? ' \u00b7 up to date' : ''))
        + (update.checkedAt ? ' \u00b7 checked ' + age(update.checkedAt) + ' ago' : ''));
      if (update.checkError) line('Check: ' + update.checkError);
      if (update.pending) line('Queued: ' + (update.pendingTag || 'latest')
        + ' - it lands the next time Steam is closed.');
      if (update.last) {
        line('Last unattended run: ' + (update.last.result || '?')
          + (update.last.version ? ' ' + update.last.version : '')
          + (update.last.reason ? ' \u00b7 ' + update.last.reason : '')
          + (update.last.at ? ' \u00b7 ' + age(update.last.at) + ' ago' : ''));
      }
      const updateActions = document.createElement('div');
      updateActions.className = 'sls-luna-row';
      const checkButton = document.createElement('button');
      checkButton.className = 'sls-luna-btn secondary';
      checkButton.textContent = 'Check now';
      checkButton.onclick = () => runUpdateAction({ action: 'check' });
      const installButton = document.createElement('button');
      installButton.className = 'sls-luna-btn';
      installButton.textContent = update.pending ? 'Queued' : 'Install update';
      installButton.disabled = !update.available || update.pending;
      installButton.onclick = () => runUpdateAction({ action: 'install', tag: update.latest });
      const cancelButton = document.createElement('button');
      cancelButton.className = 'sls-luna-btn secondary';
      cancelButton.textContent = 'Cancel queued';
      cancelButton.disabled = !update.pending;
      cancelButton.onclick = () => runUpdateAction({ action: 'cancel' });
      updateActions.append(checkButton, installButton, cancelButton);
      body.appendChild(updateActions);
      line('Applying an update needs Steam closed, so a queued one waits for the next exit - '
        + 'or turn on unattended updates and it applies by itself.');
      const modeRow = document.createElement('div');
      modeRow.className = 'sls-luna-subtabs';
      [['off', 'Off'], ['notify', 'Notify me'], ['install', 'Install automatically']]
        .forEach(pair => {
          const button = document.createElement('button');
          button.className = 'sls-luna-subtab' + ((update.autoUpdate || 'off') === pair[0] ? ' active' : '');
          button.textContent = pair[1];
          button.onclick = () => runUpdateAction({ action: 'mode', value: pair[0] });
          modeRow.appendChild(button);
        });
      body.appendChild(modeRow);
      if (update.pending && !update.timerUnits) {
        line('An update is queued but this machine has no timer worker yet: '
          + 'it lands the next time the timer runs, so keep this page open or pick a mode to enable it.');
      } else if ((update.autoUpdate || 'off') !== 'off') {
        line(update.timerUnits
          ? 'This machine is set up: the timer checks every 30 minutes and applies while Steam is closed.'
          : 'This machine is not set up yet: pick the mode again to enable its timer.');
      }

      /* Steam client, updated only from here. The bootstrapper is blocked
         everywhere else, so these are the only sanctioned paths: pin the
         working build now, queue an update or a fresh cache for the next
         closed-Steam window, or cancel the queue. */
      section('Steam client (update only from here)');
      const steamCtl = lunaSteam || (health.steam && health.steam.client) || {};
      line('Client: ' + (steamCtl.build || '?')
        + (steamCtl.pinned ? (steamCtl.match ? ' \u00b7 pinned ' + steamCtl.pinned + ' \u2713' : ' \u00b7 PINNED ' + steamCtl.pinned + ' \u2014 drifted') : ' \u00b7 not pinned')
        + ' \u00b7 updates ' + (steamCtl.updatesBlocked ? 'blocked everywhere else' : 'ALLOWED elsewhere - pin to block')
        + (steamCtl.cacheReady ? ' \u00b7 cache ready' : ''));
      if (steamCtl.pending) {
        line('Queued Steam task: ' + steamCtl.pending
          + ' - applies the next time Steam is closed (timer), then updates are blocked again.');
      }
      if (update.last && update.last.steamDrift) {
        line('Last unattended run noticed: ' + update.last.steamDrift);
      }
      const steamActions = document.createElement('div');
      steamActions.className = 'sls-luna-row';
      const pinButton = document.createElement('button');
      pinButton.className = 'sls-luna-btn secondary';
      pinButton.textContent = 'Pin current build';
      pinButton.onclick = () => runSteamAction({ action: 'pin' });
      const steamUpdateButton = document.createElement('button');
      steamUpdateButton.className = 'sls-luna-btn';
      steamUpdateButton.textContent = steamCtl.pending ? 'Queued' : 'Update Steam client';
      steamUpdateButton.disabled = Boolean(steamCtl.pending);
      steamUpdateButton.onclick = () => runSteamAction({ action: 'queue-update' });
      const steamCacheButton = document.createElement('button');
      steamCacheButton.className = 'sls-luna-btn secondary';
      steamCacheButton.textContent = 'Cache client now';
      steamCacheButton.title = 'Queues a package snapshot for the next closed-Steam window';
      steamCacheButton.disabled = Boolean(steamCtl.pending);
      steamCacheButton.onclick = () => runSteamAction({ action: 'queue-cache' });
      const steamCancelButton = document.createElement('button');
      steamCancelButton.className = 'sls-luna-btn secondary';
      steamCancelButton.textContent = 'Cancel Steam task';
      steamCancelButton.disabled = !steamCtl.pending;
      steamCancelButton.onclick = () => runSteamAction({ action: 'cancel' });
      steamActions.append(pinButton, steamUpdateButton, steamCacheButton, steamCancelButton);
      body.appendChild(steamActions);

      const copy = document.createElement('button');
      copy.className = 'sls-luna-btn secondary';
      copy.textContent = 'Copy diagnostics';
      copy.onclick = () => {
        const area = document.createElement('textarea');
        area.rows = 10;
        area.style.width = '100%';
        area.value = JSON.stringify({ report: health, prefs: (lunaInfo && lunaInfo.prefs) || {} }, null, 2);
        body.insertBefore(area, body.firstChild ? body.firstChild.nextSibling : null);
        area.focus();
        area.select();
        try { if (document.execCommand) document.execCommand('copy'); } catch (_) { /* select-only */ }
      };
      body.appendChild(copy);
      return body;
    }

    /* One place for the update actions: the mode switch also turns the user
       timer on or off, because that is what makes an unattended apply possible
       while Steam is closed. Queuing an update also ensures the timer worker
       exists (without changing the mode), or the queue would sit forever. */
    async function runUpdateAction(payload) {
      try {
        if (payload.action === 'mode') {
          await rpc(w, 'SlsLunaPrefsSet',
            { raw: JSON.stringify({ key: 'auto_update', value: payload.value }) });
          await rpc(w, 'SlsLunaUpdate', { raw: JSON.stringify({ action: 'timer',
            value: payload.value === 'off' ? 'off' : 'on' }) });
          await refresh();
          repaintIfOpen();
          return;
        }
        const request = payload.action === 'check' ? { action: 'check', force: true } : payload;
        const out = await rpc(w, 'SlsLunaUpdate', { raw: JSON.stringify(request) });
        if (out && out.update) lunaUpdate = out.update;
        if (payload.action === 'install' && out && out.queued && !lunaUpdate.timerUnits) {
          try {
            await rpc(w, 'SlsLunaUpdate', { raw: JSON.stringify({ action: 'timer', value: 'on' }) });
          } catch (_) { /* the queue stands; the Health page explains the setup */ }
        }
        await refresh();
        repaintIfOpen();
      } catch (error) {
        healthNotice = 'Update action failed: '
          + String(error && error.message ? error.message : error);
        repaintIfOpen();
      }
    }

    /* Steam client actions. Pinning is immediate and safe while Steam runs;
       update/cache only queue here - the timer applies them with Steam closed,
       then re-blocks the bootstrapper. */
    async function runSteamAction(payload) {
      try {
        const out = await rpc(w, 'SlsLunaSteam', { raw: JSON.stringify(payload) });
        if (out && out.steam) lunaSteam = out.steam;
        await refresh();
        if (payload.action === 'pin') {
          healthNotice = (out && out.message) ? String(out.message).slice(0, 200) : 'Pinned.';
        } else {
          healthNotice = null;
        }
        repaintIfOpen();
      } catch (error) {
        healthNotice = 'Steam action failed: '
          + String(error && error.message ? error.message : error);
        repaintIfOpen();
      }
    }

    function settingsBody() {
      const body = document.createElement('div');
      const note = document.createElement('p');
      note.className = 'sls-luna-note';
      note.innerHTML = 'Luna hides in-development pages behind the switch below. Everything else in the '
        + 'menu is considered stable.';
      body.appendChild(note);

      if (betaNotice) {
        const notice = document.createElement('p');
        notice.className = 'sls-luna-note';
        notice.textContent = betaNotice;
        body.appendChild(notice);
      }

      const on = betaFeatures();
      const row = document.createElement('div');
      row.className = 'sls-luna-row';
      const text = document.createElement('div');
      text.className = 'txt';
      const name = document.createElement('div');
      name.className = 'name';
      name.textContent = 'Show beta features';
      const sub = document.createElement('div');
      sub.className = 'sub';
      sub.textContent = 'Reveals pages that are still being built: the artwork picker (SteamGridDB) '
        + 'and, as they land, frame generation (lsfg-vk) and OptiScaler. They can be rough or incomplete.';
      text.append(name, sub);
      const box = document.createElement('div');
      box.className = 'sls-luna-switch' + (on ? ' on' : '');
      box.appendChild(document.createElement('span'));
      box.onclick = () => {
        setBetaFeatures(!on);
        betaNotice = null;
        repaintIfOpen();
      };
      row.append(text, box);
      body.appendChild(row);

      const state = document.createElement('p');
      state.className = 'sls-luna-note';
      state.innerHTML = on
        ? '<strong>Beta pages visible now:</strong> SteamGridDB artwork below the stable tabs.'
        : '<strong>Beta pages are hidden</strong> - turn the switch on to reach SteamGridDB artwork.';
      body.appendChild(state);

      const planned = document.createElement('p');
      planned.className = 'sls-luna-note';
      planned.textContent = 'Planned beta pages: frame generation (lsfg-vk) and OptiScaler. They will '
        + 'appear in the menu under this same switch once their detection and installers are ready.';
      body.appendChild(planned);

      const storeHeader = document.createElement('p');
      storeHeader.className = 'sls-luna-note';
      storeHeader.innerHTML = '<strong>Store</strong>';
      body.appendChild(storeHeader);
      const storeOn = Boolean(lunaInfo && lunaInfo.prefs && lunaInfo.prefs.store_hide_followed);
      switchRow(body, 'Hide followed games in the store',
        'Store home and discovery lists stop showing games you already follow. Search and game pages stay '
        + 'untouched, and the followed list is read from the store session on this machine. Takes effect on '
        + 'the next store page load.',
        storeOn, setStoreHide);
      return body;
    }

    // The store switch is stored in the backend, not localStorage: the store
    // page runs on its own origin and can only read it over RPC.
    async function setStoreHide(value) {
      try {
        const out = await rpc(w, 'SlsLunaPrefsSet',
          { raw: JSON.stringify({ key: 'store_hide_followed', value: Boolean(value) }) });
        lunaInfo = Object.assign({}, lunaInfo || {}, { prefs: out.prefs });
      } catch (error) {
        betaNotice = 'Could not save the store preference: '
          + String(error && error.message ? error.message : error);
      }
      repaintIfOpen();
    }

    function aboutBody() {      const body = document.createElement('div');
      const note = document.createElement('p');
      note.className = 'sls-luna-note';
      note.innerHTML = 'SLS Luna wraps the SLSsteam-Moon add-ons for Lumen: one installer, one boot hook, '
        + 'and this menu. Playtime counts non-Steam games and keeps every manifest editable; Presence '
        + 'publishes what you are playing; Compressor watches and drives the Btrfs compressor. The first '
        + 'two never change official Steam statistics.';
      body.appendChild(note);
      const versions = document.createElement('p');
      versions.className = 'sls-luna-note';
      const loaded = [];
      if (snapshot && snapshot.manifestCount) loaded.push('playtime');
      if (presence) loaded.push('presence');
      if (compressor) loaded.push('compressor');
      versions.innerHTML = 'Add-ons loaded: <strong>' + (loaded.join(', ') || 'none') + '</strong> (from the bridge).';
      body.appendChild(versions);
      const repo = document.createElement('p');
      repo.className = 'sls-luna-note';
      repo.textContent = 'Repository: pgm1207/Luna (private).';
      body.appendChild(repo);
      return body;
    }

    function showError(body, error) {
      const line = document.createElement('p');
      line.className = 'sls-luna-error';
      line.textContent = String(error && error.message ? error.message : error);
      body.insertBefore(line, body.firstChild ? body.firstChild.nextSibling : null);
    }

    const TITLES = { playtime: 'Playtime', presence: 'Presence', compressor: 'Compressor',
      hltb: 'HowLongToBeat', steamgrid: 'SteamGridDB artwork', graphics: 'Graphics (read-only)',
      health: 'Health (local diagnostics)', settings: 'Settings', about: 'About SLS Luna' };

    function paint() {
      const nav = visibleNav();
      if (!nav.some(entry => entry.id === tab)) tab = 'settings';
      const restoreSearch = document.activeElement === search && tab === 'playtime';
      const selection = [search.selectionStart, search.selectionEnd];
      const existing = document.getElementById(OVERLAY_ID);
      if (existing) existing.remove();
      overlay = document.createElement('div');
      overlay.id = OVERLAY_ID;
      const win = document.createElement('div');
      win.className = 'sls-luna-win';
      const side = document.createElement('div');
      side.className = 'sls-luna-side';
      const title = document.createElement('div');
      title.className = 'sls-luna-side-title';
      title.textContent = 'Luna';
      side.appendChild(title);
      sidebar().forEach(item => side.appendChild(item));
      side.appendChild(Object.assign(document.createElement('div'), { className: 'sls-luna-spacer' }));
      side.appendChild(accountRow());

      const main = document.createElement('div');
      main.className = 'sls-luna-main';
      const head = document.createElement('div');
      head.className = 'sls-luna-head';
      const heading = document.createElement('h3');
      heading.textContent = TITLES[tab] || tab;
      const close = document.createElement('button');
      close.className = 'sls-luna-x';
      close.textContent = '\u2715';
      close.onclick = closeMenu;
      const refreshButton = document.createElement('button');
      refreshButton.className = 'sls-luna-btn secondary';
      refreshButton.textContent = 'Refresh';
      refreshButton.onclick = refresh;
      head.append(heading, refreshButton, close);

      const body = document.createElement('div');
      body.className = 'sls-luna-body';
      body.appendChild(tab === 'presence' ? presenceBody()
        : tab === 'compressor' ? compressorBody()
        : tab === 'hltb' ? hltbBody()
        : tab === 'steamgrid' ? steamgridBody()
        : tab === 'graphics' ? graphicsBody()
        : tab === 'health' ? healthBody()
        : tab === 'settings' ? settingsBody()
        : tab === 'about' ? aboutBody() : playtimeBody());

      main.append(head, body);
      win.append(side, main);
      overlay.appendChild(win);
      overlay.addEventListener('click', event => { if (event.target === overlay) closeMenu(); });
      document.body.appendChild(overlay);
      if (restoreSearch) {
        search.focus();
        if (typeof search.setSelectionRange === 'function') search.setSelectionRange(...selection);
      }
    }

    async function refresh() {
      try {
        snapshot = await rpc(w, 'SlsPlaytimeGet');
      } catch (error) {
        snapshot = snapshot || {};
      }
      try {
        presence = await rpc(w, 'SlsPresenceGet');
      } catch (error) {
        presence = presence || {};
      }
      try {
        // The per-game list and the statistics are only needed by their own
        // tabs; every other view (and the one-second poll) asks for the light
        // shape, which leaves both out.
        const needFull = tab === 'compressor'
          && (compressorTab === 'games' || compressorTab === 'stats');
        compressor = await rpc(w, 'SlsCompressorGet', { raw: JSON.stringify({ light: !needFull }) });
        // The optimistic "Starting..." line gives way once the backend reports
        // this exact job (running, or already finished) from our request.
        const job = compressor && compressor.job;
        if (pendingAction && job
          && (job.running || (Number(job.startedAt) >= pendingAction.at - 1))) {
          pendingAction = null;
        }
        if (!(job && job.running)) cancelArmed = false;
        if (job && job.running) confirmAction = null;
      } catch (error) {
        compressor = compressor || {};
      }
      try {
        hltb = await rpc(w, 'SlsHltbGet');
      } catch (error) {
        hltb = hltb || {};
      }
      try { refreshLibraryShelves(); } catch (_) { /* shelves render later */ }
      try {
        sgdb = await rpc(w, 'SlsSgdbGet');
      } catch (error) {
        sgdb = sgdb || {};
      }
      try {
        graphics = await rpc(w, 'SlsGraphicsGet');
      } catch (error) {
        graphics = graphics || {};
      }
      try {
        lunaInfo = await rpc(w, 'SlsLunaInfo');
      } catch (error) {
        lunaInfo = lunaInfo || {};
      }
      await migratePrefs();
      try {
        const report = await rpc(w, 'SlsLunaHealth');
        lunaHealth = (report && report.health) || report || {};
      } catch (error) {
        lunaHealth = lunaHealth || {};
      }
      try {
        const out = await rpc(w, 'SlsLunaUpdate', { raw: JSON.stringify({ action: 'status' }) });
        lunaUpdate = (out && out.update) || lunaUpdate || {};
      } catch (error) {
        lunaUpdate = lunaUpdate || {};
      }
      try {
        const steamOut = await rpc(w, 'SlsLunaSteam', { raw: JSON.stringify({ action: 'status' }) });
        lunaSteam = (steamOut && steamOut.steam) || lunaSteam || {};
      } catch (error) {
        lunaSteam = lunaSteam || {};
      }
      // A real check talks to GitHub through the updater; the Python side caches
      // it for six hours, and opening the menu throttles the call anyway.
      // "Off" means never check, even from the menu: the Health page then
      // only shows the last known status.
      if ((lunaUpdate.autoUpdate || 'off') !== 'off' && Date.now() - lunaCheckedAt > 30 * 60 * 1000) {
        lunaCheckedAt = Date.now();
        try {
          const out = await rpc(w, 'SlsLunaUpdate', { raw: JSON.stringify({ action: 'check' }) });
          if (out && out.update) lunaUpdate = out.update;
        } catch (_) { /* offline or gh missing; the status stays */ }
      }
      maybeAutoFetch();
      repaintIfOpen();
    }

    function openMenu() {
      if (document.getElementById(OVERLAY_ID)) return;
      paint();
      refresh();
    }

    function closeMenu() {
      stopJobPolling();
      confirmAction = null;
      cancelArmed = false;
      cleanupArmed = false;
      // A fetch pass deliberately keeps running in the background; the menu
      // shows its progress again the next time it is opened.
      const node = document.getElementById(OVERLAY_ID);
      if (node) node.remove();
    }

    document.addEventListener('keydown', event => {
      if (event.key === 'Escape') closeMenu();
    });

    mountButton(w, openMenu);
    // Safety net: an add-on that mounted its floating panel before the flag was
    // set is removed on the next pass.
    w.setInterval(() => { removeFloating(document); }, 4000);
    // The agent's "Change artwork" right-click lands here as a storage event
    // (SharedJSContext writes it; the panel owns the window and opens the UI).
    // Only artwork keys wake the consumer; anything else is unrelated traffic.
    if (typeof w.addEventListener === 'function') {
      w.addEventListener('storage', event => {
        try {
          if (event && typeof event.key === 'string') {
            if (event.key === 'slsLunaArtworkRequest') { consumeArtworkRequest(); return; }
            if (event.key === 'slsLunaHltbOrder') { scheduleShelfRefresh(0); return; }
            return;
          }
        } catch (_) { /* a bare emitter without a key still consumes */ }
        consumeArtworkRequest();
      });
    }
    (w.setTimeout || setTimeout)(() => { consumeArtworkRequest(); }, 2500);
    // Home-shelf HLTB order lives in this window: re-scan after navigation and
    // re-apply after the cache or direction changes. No shared objects change.
    (w.setTimeout || setTimeout)(() => { refreshLibraryShelves(); }, 3000);
    (w.setTimeout || setTimeout)(() => { refreshLibraryShelves(); }, 9000);
    w.setInterval(() => { refreshLibraryShelves(); }, 15000);
    if (typeof w.MutationObserver === 'function') {
      try {
        const seen = new w.MutationObserver(() => { scheduleShelfRefresh(800); });
        seen.observe(document.body || document, { childList: true, subtree: true });
      } catch (_) { /* a static view simply keeps its current order */ }
    }
    // Resume a background fetch pass after a Steam restart without making the
    // user open the menu: load the local times and continue where it left off.
    (w.setTimeout || setTimeout)(async () => {
      try {
        hltb = await rpc(w, 'SlsHltbGet');
      } catch (_) { /* the backend may still be starting */ }
      refreshLibraryShelves();
      maybeAutoFetch();
    }, 6000);
  }

  /* Merged view of the agent-reported game list and the cached HowLongToBeat
     times. Pure so it can be tested without a DOM: missing games sort first,
     then names; rows without a record carry record=null. */
  function buildHltbRows(games, library, needle) {
    const filter = String(needle || '').trim().toLowerCase();
    const records = games || {};
    const byId = new Map();
    (Array.isArray(library) ? library : []).forEach(entry => {
      const id = String((entry && entry.appid) || '');
      if (!id) return;
      byId.set(id, { id, name: entry.name || ('App ' + id), shortcut: entry.shortcut === true });
    });
    Object.keys(records).forEach(id => {
      if (!byId.has(String(id))) {
        byId.set(String(id), { id: String(id), name: (records[id] && records[id].name) || ('App ' + id), shortcut: false });
      }
    });
    return [...byId.values()].map(row => ({ ...row, record: records[row.id] || null }))
      .filter(row => (row.name + ' ' + row.id).toLowerCase().includes(filter))
      .sort((a, b) => (a.record ? 1 : 0) - (b.record ? 1 : 0) || a.name.localeCompare(b.name));
  }

  /* List building for the Playtime tab. Kept pure so it can be tested without a
     DOM; the rules match the standalone panel. */
  function buildRows(snapshot, needle) {
    const filter = String(needle || '').trim().toLowerCase();
    const titles = (snapshot && snapshot.titles) || {};
    const shortcuts = (snapshot && snapshot.shortcutNames) || {};
    const games = (snapshot && snapshot.games) || {};
    const ids = new Set([...((snapshot && Array.isArray(snapshot.apps)) ? snapshot.apps : []).map(String),
      ...Object.keys(games)]);
    return [...ids].map(id => {
      const record = games[id] || { trackedSeconds: 0, adjustmentSeconds: 0, lastPlayed: 0 };
      const name = record.title || titles[id] || shortcuts[id] || ('App ' + id);
      return {
        id,
        record,
        name,
        shortcut: Boolean(shortcuts[id]),
        total: lifetimeSeconds(record),
        known: name !== ('App ' + id),
      };
    }).filter(row => (row.name + ' ' + row.id).toLowerCase().includes(filter))
      .sort((a, b) => (b.total > 0) - (a.total > 0) || (b.known ? 1 : 0) - (a.known ? 1 : 0)
        || (b.record.lastPlayed || 0) - (a.record.lastPlayed || 0) || a.name.localeCompare(b.name));
  }

  return { mount, buildRows, buildHltbRows, findMenubar, hours };
});
