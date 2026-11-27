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
  const PANEL_BUILD = 'bv-5';
  // Every RPC is bounded: a restarted or dead Lumen must never leave a busy
  // flag (or a polling loop) waiting on a promise that never settles.
  const RPC_TIMEOUT = 15000;
  const BTN_ID = 'sls-luna-btn';
  const OVERLAY_ID = 'sls-luna-overlay';
  // Backends differ on whether a list field arrives as an array, a single item
  // or a string; page builders must never throw on the shape.
  const asList = value => (Array.isArray(value) ? value : (value === null || value === undefined ? [] : [value]));
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
    updates: '<path d="M12 4v10m0 0-4-4m4 4 4-4" fill="none" stroke="currentColor" stroke-width="2"/>'
      + '<path d="M5 18h14" stroke="currentColor" stroke-width="2"/>',
    lumen: '<path d="M15.5 4a8 8 0 1 0 4.5 12.5A7 7 0 0 1 15.5 4z" fill="none" '
      + 'stroke="currentColor" stroke-width="2"/>',
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
    { id: 'updates', label: 'Updates' },
    { id: 'lumen', label: 'Lumen' },
    { id: 'settings', label: 'Settings' },
    { id: 'about', label: 'About' },
  ];
  // Floating panels the add-ons draw when Luna is not present.
  const FLOATING = ['sls-playtime-panel-button', 'sls-playtime-panel',
                    'sls-presence-panel-button', 'sls-presence-panel',
                    'sls-compressor-panel-button', 'sls-compressor-panel'];

  const errorText = error => {
    const text = String(error && error.message ? error.message : error);
    // A timeout is retryable: name the retry so the row is actionable.
    return error && error.code === 'timeout' ? text + ' (press Refresh to retry)' : text;
  };
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
  /* Size fields are bytes unless explicitly named as MiB. */
  const sizeToBytes = (value, unit) => {
    const number = Number(value) || 0;
    const name = String(unit || 'bytes').toLowerCase();
    const factor = name === 'mib' || name === 'mb' ? 1024 * 1024
      : name === 'gib' ? 1024 * 1024 * 1024 : 1;
    return Math.max(0, number * factor);
  };
  const formatSize = (value, unit) => formatBytes(sizeToBytes(value, unit));
  const pctText = value => String(Math.max(0, Math.min(100, Math.round(Number(value) || 0))));
  // Game names, paths and backend messages come from disk or the network: any
  // of them placed in innerHTML goes through here, so it stays text.
  const escapeHtml = text => String(text == null ? '' : text)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');

  /* ---- Playtime chart (pure) -------------------------------------------
     The backend may send `stats.daily30` (30 days, oldest first, zero filled)
     and `stats.prev7d` / `stats.week_top`. An older backend sends none of
     them: every reader below then returns null and the page renders exactly
     what it always rendered, with no error. Hostile values are treated as
     absent rather than drawn as NaN widths. */
  const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const finiteSeconds = value => {
    const number = Number(value);
    return Number.isFinite(number) ? Math.max(0, number) : null;
  };
  // "2 h 14 min", "45 min", "0 min": tooltips and the weekly line share it.
  const minutesText = seconds => {
    const total = Math.max(0, Math.round(Number(seconds) || 0));
    const whole = Math.floor(total / 3600);
    const rest = Math.floor((total % 3600) / 60);
    if (!whole) return rest + ' min';
    return whole + ' h' + (rest ? ' ' + rest + ' min' : '');
  };
  const dayParts = day => {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(day == null ? '' : day));
    if (!match) return null;
    const stamp = Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
    const date = new Date(stamp);
    if (date.getUTCFullYear() !== Number(match[1]) || date.getUTCMonth() !== Number(match[2]) - 1
      || date.getUTCDate() !== Number(match[3])) return null;
    return { y: Number(match[1]), m: Number(match[2]), d: Number(match[3]), days: stamp / 86400000 };
  };
  const localDayKey = date => date.getFullYear() + '-'
    + String(date.getMonth() + 1).padStart(2, '0') + '-' + String(date.getDate()).padStart(2, '0');
  function dailyBars(stats, now) {
    const daily = stats && stats.daily30;
    if (!Array.isArray(daily) || daily.length !== 30) return null;
    const today = localDayKey(now instanceof Date ? now : new Date());
    const bars = [];
    for (let index = 0; index < daily.length; index += 1) {
      const entry = daily[index];
      if (!entry || typeof entry !== 'object') return null;
      const parts = dayParts(entry.day);
      const seconds = finiteSeconds(entry.seconds);
      if (!parts || seconds === null) return null;
      const weekday = new Date(parts.days * 86400000).getUTCDay();
      bars.push({
        day: String(entry.day), seconds, weekday,
        weekend: weekday === 0 || weekday === 6,
        today: String(entry.day) === today,
        label: WEEKDAYS[weekday] + ' ' + parts.d + ' ' + MONTHS[parts.m - 1],
      });
    }
    const total = bars.reduce((sum, bar) => sum + bar.seconds, 0);
    const max = bars.reduce((top, bar) => Math.max(top, bar.seconds), 0);
    let busiest = null;
    bars.forEach(bar => { if (bar.seconds > 0 && (!busiest || bar.seconds > busiest.seconds)) busiest = bar; });
    return { bars, total, max, busiest };
  }
  function weeklySentence(stats, chart) {
    const source = stats || {};
    const chartData = chart || dailyBars(source, new Date());
    const direct = finiteSeconds(source.last7d);
    const week = direct !== null ? direct
      : (chartData ? chartData.bars.slice(23).reduce((sum, bar) => sum + bar.seconds, 0) : null);
    if (week === null) return null;
    const head = 'This week ' + minutesText(week);
    const previous = finiteSeconds(source.prev7d);
    if (previous === null) return head + '.';
    const diff = Math.round(week - previous);
    if (Math.abs(diff) < 60) return head + ', same as last week.';
    return head + ', ' + minutesText(Math.abs(diff)) + (diff > 0 ? ' more' : ' less') + ' than last week.';
  }
  function weekTop(stats, names) {
    const list = asList(stats && stats.week_top);
    const known = names || {};
    const rows = [];
    list.forEach(entry => {
      if (!entry || typeof entry !== 'object') return;
      const seconds = finiteSeconds(entry.seconds);
      if (seconds === null || seconds <= 0) return;
      const appid = Number(entry.appid) || 0;
      const name = String(entry.name || known[appid] || known[String(appid)] || '').trim();
      if (!name) return;
      rows.push({ appid, name, seconds });
    });
    return rows.length ? rows.slice(0, 5) : null;
  }

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
      '.sls-luna-side{flex:0 0 200px;background:#2a2d34;display:flex;flex-direction:column;padding-top:8px;',
      'overflow-y:auto;}',
      '.sls-luna-side-title{color:' + ACCENT + ';font-size:17px;font-weight:700;text-transform:uppercase;padding:14px 24px 16px;}',
      // `flex:0 0 auto` matters: the sidebar is a flex column, and without it a
      // row with `overflow:hidden` shrinks to 0px as soon as the list is full.
      '.sls-luna-tab{display:flex;align-items:center;gap:12px;padding:10px 8px 10px 24px;height:20px;',
      'flex:0 0 auto;color:#b8bcbf;font-size:14px;cursor:pointer;}',
      '.sls-luna-tab:hover{background:rgba(255,255,255,.04);}',
      '.sls-luna-tab.active{background:#3d4450;color:#fff;}',
      // Keyboard focus must be visible on a dark sidebar, and the ring sits
      // inside the row so an overflow-hidden row never clips it away.
      '.sls-luna-tab:focus{outline:2px solid #8ab4f8;outline-offset:-2px;}',
      '.sls-luna-tab:focus:not(:focus-visible){outline:none;}',
      '.sls-luna-tab:focus-visible{outline:2px solid #8ab4f8;outline-offset:-2px;}',
      // Nested list under Lumen: Lumen's own pages, one line each, indented
      // under the category and built by the same row builder as the categories
      // (this rule only changes indent, type and colour, never the height).
      '.sls-luna-subpage{padding:6px 8px 6px 46px;color:#9aa4ae;font-size:13px;flex:0 0 auto;',
      'white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}',
      '.sls-luna-subpage:hover{background:rgba(255,255,255,.04);color:#dcdedf;}',
      '.sls-luna-subpage.active{background:rgba(61,68,80,.7);color:#fff;}',
      '.sls-luna-spacer{flex:1 1 auto;min-height:18px;}',
      '.sls-luna-account{display:flex;align-items:center;gap:10px;padding:13px 14px 14px;',
      'border-top:1px solid rgba(255,255,255,.07);color:#e6e9ec;font-size:13px;',
      'background:transparent;border:0;border-top:1px solid rgba(255,255,255,.07);',
      'font:inherit;width:100%;text-align:left;cursor:pointer;}',
      '.sls-luna-account:hover{background:rgba(255,255,255,.04);}',
      '.sls-luna-account:focus-visible{outline:1px solid #1a9fff;outline-offset:-1px;}',
      '.sls-luna-account:focus{outline-color:rgba(26,159,255,.7);}',
      '.sls-luna-account .sls-luna-avatar{width:24px;height:24px;border-radius:50%;flex:0 0 24px;',
      'object-fit:cover;display:block;}',
      '.sls-luna-account .sls-luna-state{margin-left:auto;font-size:12px;}',
      '.sls-luna-account .sls-luna-state.ok{color:#57cb5c;}',
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
      '.sls-luna-view-note{color:#e0b060;font-size:12px;line-height:1.35;max-width:340px;margin-left:10px;}',
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

  /* On Store/Community Steam composites a separate browser view above the
     shell document, so this overlay is visible but receives no clicks. Steam's
     own modals hide that view with SetVisible(false). The manager that owns it
     exists only in SharedJSContext, never in this document (and `opener` is
     unusable there), so the ask travels over localStorage to the add-on's
     agent in that context: one request key, one result key. The request is
     answered by agent.js (viewStatus/viewSetVisible); nothing about a hidden
     view is persisted, so a Steam restart always starts normal. */
  const VIEW_REQUEST_KEY = 'slsLunaViewRequest';
  const VIEW_RESULT_KEY = 'slsLunaViewResult';
  const VIEW_TIMEOUT_MS = 700;
  // Navigating takes longer than a status answer: Steam's router moves a view.
  const VIEW_NAVIGATE_TIMEOUT_MS = 2500;
  const VIEW_LIBRARY_NOTE = 'Steam stayed on the Store/Community page; press Steam\u2019s Library tab '
    + 'once, then open Luna.';
  // Last state SharedJSContext reported, and whether *this* panel hid the view.
  let viewReported = null;
  let viewHiddenByLuna = false;
  let viewNote = '';
  function findMainBrowser(w) {
    const hosts = [w];
    try { if (w && w.opener) hosts.push(w.opener); } catch (_) { /* cross-origin */ }
    for (const h of hosts) {
      try {
        const b = h && h.MainWindowBrowserManager && h.MainWindowBrowserManager.m_browser;
        if (b && typeof b.SetVisible === 'function') return b;
      } catch (_) { /* keep looking */ }
    }
    return null;
  }
  /* Preferred route: Steam's own router. The browser view is only shown on
     /browser/... routes (Store, Community, profiles, web tabs), so the menu
     first moves Steam to the Library with SteamUIStore.Navigate and waits for
     the route to change. Nothing is navigated back on close. */
  function steamHosts(w) {
    const hosts = [w];
    try { if (w && w.opener) hosts.push(w.opener); } catch (_) { /* cross-origin */ }
    return hosts;
  }
  function currentRoute(w) {
    for (const h of steamHosts(w)) {
      try {
        const loc = h && h.MainWindowBrowserManager && h.MainWindowBrowserManager.m_history && h.MainWindowBrowserManager.m_history.location;
        if (loc && typeof loc.pathname === 'string') return loc.pathname;
      } catch (_) { /* keep looking */ }
    }
    return null;
  }
  function findNavigate(w) {
    for (const h of steamHosts(w)) {
      try {
        const ui = h && h.SteamUIStore;
        if (ui && typeof ui.Navigate === 'function') return p => ui.Navigate(p);
      } catch (_) { /* keep looking */ }
    }
    return null;
  }
  function onBrowserRoute(w) {
    return onBrowserRoutePath(currentRoute(w));
  }
  function onBrowserRoutePath(route) {
    return typeof route === 'string' && /^\/(browser|store|community)(\/|$)/.test(route);
  }
  // Resolves true when no navigation was needed or it completed, false when
  // no navigation API exists (caller falls back to hiding the view).
  // Last resort only: the hide/show path above never navigates the user.
  async function leaveBrowserRoute(w) {
    if (!onBrowserRoute(w)) return true;
    const navigate = findNavigate(w);
    if (!navigate) return false;
    try { navigate('/library/home'); } catch (_) { return true; }
    const until = Date.now() + 1500;
    while (onBrowserRoute(w) && Date.now() < until) {
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    return true;
  }

  function viewStorage(w) {
    try { return (w && w.localStorage) || null; } catch (_) { return null; }
  }

  // The last state the agent published, if any. Its route is the only route
  // this document can read at all.
  function rememberView(w) {
    const storage = viewStorage(w);
    if (!storage) { viewReported = null; return null; }
    let raw = null;
    try { raw = storage.getItem(VIEW_RESULT_KEY); } catch (_) { return viewReported; }
    if (!raw) { viewReported = null; return null; }
    let body = null;
    try { body = JSON.parse(raw); } catch (_) { return viewReported; }
    if (!body || typeof body !== 'object') return viewReported;
    viewReported = body.ok === true
      ? { ok: true, route: typeof body.route === 'string' ? body.route : null,
        browserVisible: body.browserVisible !== false }
      : { ok: false, route: null, browserVisible: false };
    return viewReported;
  }

  /* One ask to the SharedJSContext agent, always settled: the storage event
     normally answers it, the poll covers a listener registered after the
     answer, and the timer rejects when nothing in that context is listening. */
  function requestView(w, action, timeoutMs) {
    return new Promise((resolve, reject) => {
      const storage = viewStorage(w);
      if (!storage) { reject(Error('Steam view control is unavailable.')); return; }
      const id = 'v' + Date.now().toString(36) + Math.random().toString(36).slice(2, 8);
      let settled = false;
      let timer = null;
      let poll = null;
      const finish = (error, value) => {
        if (settled) return;
        settled = true;
        if (timer !== null) { try { (w.clearTimeout || clearTimeout)(timer); } catch (_) { /* gone */ } }
        if (poll !== null) { try { (w.clearInterval || clearInterval)(poll); } catch (_) { /* gone */ } }
        try { w.removeEventListener('storage', onStorage); } catch (_) { /* gone */ }
        if (error) reject(error); else resolve(value);
      };
      const read = () => {
        if (settled) return;
        let raw = null;
        try { raw = storage.getItem(VIEW_RESULT_KEY); } catch (_) { return; }
        if (!raw) return;
        let body = null;
        try { body = JSON.parse(raw); } catch (_) { return; }
        if (!body || body.id !== id) return;
        if (body.ok === true) {
          viewReported = { ok: true, route: typeof body.route === 'string' ? body.route : null,
            browserVisible: body.browserVisible !== false };
        }
        finish(null, body);
      };
      const onStorage = event => {
        const key = event && typeof event.key === 'string' ? event.key : '';
        if (key && key !== VIEW_RESULT_KEY) return;
        read();
      };
      try { w.addEventListener('storage', onStorage); } catch (_) { /* no events */ }
      try {
        storage.setItem(VIEW_REQUEST_KEY, JSON.stringify({ id, action, at: Date.now() }));
      } catch (error) {
        finish(error instanceof Error ? error : Error('Steam view control is unavailable.'));
        return;
      }
      // 40 ms only until the answer or VIEW_TIMEOUT_MS; finish() clears it.
      poll = (w.setInterval || setInterval)(read, 40);
      timer = (w.setTimeout || setTimeout)(() => finish(Error('Steam view control did not answer.')),
        Number(timeoutMs) > 0 ? Number(timeoutMs) : VIEW_TIMEOUT_MS);
      read();
    });
  }

  // Synchronous path: the manager happened to be reachable from this document.
  function hideMainView(w) {
    if (viewHiddenByLuna) return true;
    const b = findMainBrowser(w);
    if (!b) return false;
    try { b.SetVisible(false); viewHiddenByLuna = true; return true; } catch (_) { return false; }
  }

  /* Move Steam to the Library before the menu opens. Hiding the browser view
     is not enough on its own: a hidden Store/Community view keeps taking input,
     so clicks never reach this overlay. The agent asks Steam's own router and
     reports the route it actually shows; the in-document router is the fallback
     when no agent answers; a failure is one line in the window, never a menu
     over a page that cannot be clicked. Nothing is navigated back on close. */
  async function prepareView(w) {
    // An older build could have left the view hidden: never leave it that way.
    if (viewHiddenByLuna) restoreMainView(w);
    let status = null;
    try { status = await requestView(w, 'status'); } catch (_) { status = null; }
    if (status && status.ok !== true) status = null;
    const known = rememberView(w);
    const route = status && typeof status.route === 'string' ? status.route
      : (currentRoute(w) || (known && known.ok ? known.route : null));
    if (!onBrowserRoutePath(route)) return true;
    let navigated = false;
    if (status) {
      try {
        const out = await requestView(w, 'navigate', VIEW_NAVIGATE_TIMEOUT_MS);
        navigated = Boolean(out && out.ok === true);
      } catch (_) { navigated = false; }
    }
    if (!navigated) navigated = await leaveBrowserRoute(w).catch(() => false);
    if (!navigated) viewNote = VIEW_LIBRARY_NOTE;
    else viewNote = '';
    return true;
  }

  // Put the view back: once, and only if this panel hid it.
  function restoreMainView(w) {
    if (!viewHiddenByLuna) return;
    viewHiddenByLuna = false;
    if (viewReported) viewReported = { ...viewReported, browserVisible: true };
    const b = findMainBrowser(w);
    if (b) {
      try { b.SetVisible(true); } catch (_) { /* view gone */ }
      return;
    }
    requestView(w, 'show').catch(() => { /* Steam is closing */ });
  }

  /* One cleanup bag per panel generation. Every timer, listener and observer a
     mount installs is recorded here, so replacing a panel (a newer build, or a
     second mount of the same page) leaves no stale closure sweeping the DOM. */
  function createLife(w) {
    const intervals = [];
    const timeouts = [];
    const listeners = [];
    const observers = [];
    const clearIntervalOf = w.clearInterval || clearInterval;
    const clearTimeoutOf = w.clearTimeout || clearTimeout;
    return {
      interval(fn, ms) {
        const id = w.setInterval(fn, ms);
        intervals.push(id);
        return id;
      },
      timeout(fn, ms) {
        const id = (w.setTimeout || setTimeout)(fn, ms);
        timeouts.push(id);
        return id;
      },
      listen(target, type, fn, options) {
        if (!target || typeof target.addEventListener !== 'function') return;
        target.addEventListener(type, fn, options);
        listeners.push([target, type, fn, options]);
      },
      watch(observer) {
        observers.push(observer);
        return observer;
      },
      teardown() {
        intervals.forEach(id => { try { clearIntervalOf(id); } catch (_) { /* already gone */ } });
        timeouts.forEach(id => { try { clearTimeoutOf(id); } catch (_) { /* already fired */ } });
        listeners.forEach(entry => {
          try { entry[0].removeEventListener(entry[1], entry[2], entry[3]); }
          catch (_) { /* the target went away with the page */ }
        });
        observers.forEach(observer => { try { observer.disconnect(); } catch (_) { /* already dropped */ } });
        intervals.length = 0;
        timeouts.length = 0;
        listeners.length = 0;
        observers.length = 0;
      },
    };
  }

  function mountButton(w, onOpen, life, onPlaced) {
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
      // Proven placed: now the Lumen launcher may give way (see hideLumenLauncher).
      if (typeof onPlaced === 'function') onPlaced();
      return true;
    };
    if (place()) return;
    let tries = 0;
    // Retries at most 120 times (one minute after boot) and stops on success.
    const timer = life.interval(() => {
      tries += 1;
      if (place() || tries > 120) { try { (w.clearInterval || clearInterval)(timer); } catch (_) { /* gone */ } }
    }, 500);
    return timer;
  }

  function removeFloating(document) {
    FLOATING.forEach(id => {
      const node = document.getElementById(id);
      if (node) node.remove();
    });
  }

  /* One honest error surface: a small in-memory ring the Health page lists
     and copies. It holds RPC failures, panel warnings and the outcome of the
     last backend job; no file, no network, no cost when nothing fails. */
  const PROBLEM_LIMIT = 20;
  const PROBLEMS = [];

  function addProblem(source, text) {
    const value = String(text == null ? '' : text).replace(/\s+/g, ' ').trim().slice(0, 240);
    if (!value) return;
    const now = Date.now();
    const key = String(source) + '\u0000' + value;
    // The same failure seen again by the next poll updates its time instead of
    // adding a row: one entry per distinct problem, not one per tick.
    const known = PROBLEMS.find(entry => entry.key === key && now - entry.at < 60000);
    if (known) { known.at = now; return; }
    PROBLEMS.push({ key: key, at: now, source: String(source), text: value });
    while (PROBLEMS.length > PROBLEM_LIMIT) PROBLEMS.shift();
  }

  function lunaWarn(text, w) {
    const line = '[sls-luna] ' + String(text == null ? '' : text);
    try {
      const sink = (w && w.console) || (typeof console !== 'undefined' ? console : null);
      if (sink && typeof sink.warn === 'function') sink.warn(line);
    } catch (_) { /* no console */ }
    addProblem('panel', line);
  }

  /* A bounded RPC. Lumen answers in milliseconds when healthy, so a call past
     the budget means it was restarted or died; the promise always settles,
     which is what keeps busy flags and polling loops from wedging for the life
     of the page. A late answer is dropped: the race has already rejected, so a
     response after the timeout can never repaint the page with stale state. */
  function rpc(w, method, args) {
    const configured = Number(w && w.__SlsLunaRpcTimeout);
    const budget = configured > 0 ? configured : RPC_TIMEOUT;
    let timer = null;
    const answer = Promise.resolve()
      .then(() => w.Millennium.callServerMethod('lumen', method, args || {}))
      .then(parse)
      .then(out => {
        if (out && out.success === false) {
          // Keep the backend's code so callers can tell retryable failures
          // (rate limiting) from permanent ones (no confident match).
          const error = Error(out.error || (method + ' failed'));
          error.code = out.code || '';
          // Rate limiting carries its own wait; the page can tell the user how
          // long to hold off instead of guessing.
          if (out.retryAfter !== undefined) error.retryAfter = out.retryAfter;
          throw error;
        }
        return out;
      });
    const expiry = new Promise((resolve, reject) => {
      timer = setTimeout(() => {
        const error = Error('Luna did not answer ' + method + ' within '
          + (budget >= 1000 ? Math.round(budget / 1000) + ' s' : budget + ' ms'));
        error.code = 'timeout';
        error.method = method;
        reject(error);
      }, budget);
    });
    const settle = () => { try { clearTimeout(timer); } catch (_) { /* already fired */ } };
    return Promise.race([answer, expiry])
      .then(out => { settle(); return out; }, error => {
        settle();
        addProblem('rpc', method + ': '
          + String(error && error.message ? error.message : error));
        throw error;
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

  function mountStore(w, life) {
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
      // Hidden store page: nothing to hide on screen, the 4 s pass catches up.
      if (document.hidden === true) return;
      scheduled = true;
      const later = () => { scheduled = false; sweep(); };
      if (typeof w.setTimeout === 'function') life.timeout(later, 350);
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
        life.watch(state.observer);
      }
      // Runs only while store_hide_followed is on and only on store pages; the
      // pass is a querySelectorAll over the tiles, skipped while hidden.
      if (typeof w.setInterval === 'function') state.interval = life.interval(schedule, 4000);
    }

    start();
  }

  // Steam window gradient built from the Noctalia palette. The backend reads
  // Noctalia's generated Steam colour file; this repaints when the file changes
  // (polled, plus on focus) so the gradient follows the desktop theme live.
  function mountGradient(w, life) {
    const document = w.document;
    const state = { sig: null, busy: false };
    function css(colors) {
      const c = name => {
        const v = colors[name];
        return v ? v.join(',') : null;
      };
      const primary = c('primary');
      const secondary = c('secondary') || primary;
      const tertiary = c('tertiary') || primary;
      const base = c('surface-container') || c('surface') || '24,25,42';
      const container = c('primary-container') || primary;
      const bg = 'linear-gradient(135deg, rgba(' + base + ',1) 0%, rgba(' + container + ',0.9) 38%, '
        + 'rgba(' + primary + ',0.55) 68%, rgba(' + secondary + ',0.5) 84%, rgba(' + tertiary + ',0.45) 100%)';
      return 'html, body { background: ' + bg + ' fixed !important; background-color: rgb(' + base + ') !important; }\n'
        // Opaque full-window shells would hide the gradient; make only those transparent.
        + '.fullscreen, .mainbackground, .SteamDesktop, .bodyContainer, .DesktopUI, '
        + '[class*="BasicUI"], [class*="mainbackground"] { background: transparent !important; }';
    }
    function apply(out) {
      let node = document.getElementById('sls-luna-gradient');
      if (!out || !out.success || !out.enabled || !out.available) {
        if (node) node.remove();
        state.sig = null;
        return;
      }
      const sig = JSON.stringify(out.colors);
      if (sig === state.sig && node) return;
      state.sig = sig;
      if (!node) {
        node = document.createElement('style');
        node.id = 'sls-luna-gradient';
        (document.head || document.documentElement).appendChild(node);
      }
      node.textContent = css(out.colors);
    }
    async function refresh() {
      if (state.busy) return;
      state.busy = true;
      try {
        apply(await rpc(w, 'SlsLunaGradient'));
      } catch (_) {
        // Keep the last gradient; a timeout is retried on the next tick.
      } finally {
        state.busy = false;
      }
    }
    refresh();
    // Cheap (one RPC per 30 s) and it only paints; skipped while the window is
    // hidden, and the focus listener refreshes right away when it returns.
    if (typeof w.setInterval === 'function') life.interval(() => {
      if (document.hidden === true) return;
      refresh();
    }, 30000);
    life.listen(w, 'focus', refresh);
    return { refresh };
  }

  function mount(w) {
    const document = w.document;
    if (!document) return;
    // Each panel generation starts with an empty Problems list.
    PROBLEMS.length = 0;
    // One generation owns the page. A newer build (or a second mount) retires
    // the previous generation first, so its interval, listeners and observers
    // cannot stack against a stale closure.
    if (typeof w.__SlsLunaTeardown === 'function') {
      try { w.__SlsLunaTeardown(); } catch (_) { /* the old page is already gone */ }
      w.__SlsLunaTeardown = null;
    }
    for (const id of [BTN_ID, OVERLAY_ID]) {
      const old = document.getElementById(id);
      if (old && typeof old.remove === 'function') old.remove();
    }
    const life = createLife(w);
    w.__SlsLunaPanel = true;
    w.__SlsLunaPanelBuild = PANEL_BUILD;
    const generation = (w.__SlsLunaPanelGen = (w.__SlsLunaPanelGen || 0) + 1);
    const current = () => w.__SlsLunaPanelGen === generation;
    // Tells the feature add-ons to stay quiet in the shell: their settings live
    // in this menu while it is present.
    w.__SlsLunaUi = true;
    // The store is a different origin with no menubar: no menu there, just the
    // followed-games filter when the Settings switch asks for it.
    if (isStoreWindow(w)) {
      w.__SlsLunaTeardown = () => {
        life.teardown();
        const style = document.getElementById('sls-luna-store-style');
        if (style && typeof style.remove === 'function') style.remove();
        removeFloating(document);
      };
      mountStore(w, life);
      return;
    }
    styled(document);
    removeFloating(document);

    let overlay = null;
    let tab = 'playtime';
    let snapshot = null;
    // The revision of the painted playtime payload and the diagnostics the last
    // full reply carried (`recovered`, `merge`). A poll sends `rev`, and an
    // `{unchanged:true}` answer leaves the painted snapshot exactly as it is.
    let snapshotRev = null;
    let playtimeRecovered = false;
    let playtimeMerge = null;
    let playtimeBusy = false;
    let presence = null;
    let compressor = null;
    let hltb = null;
    // Revision of the last cache payload. SlsHltbGet with the same rev answers
    // {unchanged:true, rev} (59 B) instead of ~1 MB; the payload stays as is.
    let hltbRev = null;
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
    let sgdbReadError = '';
    // Undo history for the game on screen: per-slot depths from the backend
    // (SlsSgdbHistory), read once per game and after every change, plus when
    // this session last touched each slot so the list can say "3m ago".
    let sgdbHistory = null;
    let sgdbHistoryFor = '';
    let sgdbHistoryError = '';
    let sgdbHistoryBusy = false;
    let sgdbUndoArmed = null;
    let sgdbResetArmed = 0;
    let sgdbChangedAt = {};
    // Per-slot epochs from SlsSgdbHistory ({grid: <epoch>, ...}), so a change
    // made before this page opened can still say when it happened.
    let sgdbTimes = {};
    // The detached artwork pass: its id, progress and the last per-slot
    // results. Polled only while this tab is on screen.
    let sgdbAuto = null;
    let sgdbAutoTimer = null;
    let artworkRequest = '';
    let graphics = null;
    // When the Graphics report was produced (its own `checkedAt`, else the
    // moment this page received it) and the one text node that counts up.
    let graphicsCheckedAt = 0;
    let graphicsAgeNode = null;
    let lunaInfo = null;
    let lunaHealth = null;
    let lunaUpdate = null;
    let lunaSteam = null;
    let lunaRefresh = null;
    let lunaLumen = null;
    // Lumen's channel state, read through `luna lumen channels` once per page
    // session and refreshed after every change; null means "not read yet".
    let lunaChannels = null;
    let channelsBusy = false;
    let channelsError = '';
    let channelsTried = false;
    // Footer connection state, read from LuaTools' own session through the
    // backend: {signedIn, name, expired, path} or {unavailable, error}.
    let luatools = null;
    let lumenBusy = false;
    // Confirm step in front of every queue ({kind: 'lumen'|'everything'|'reinstall'}),
    // the owner's Millennium pick for this page open, and the status poller's pace.
    let lumenConfirm = null;
    let millenniumPick = '';
    let lumenNextPoll = 0;
    let lumenIdlePolls = 0;
    let tornDown = false;
    // Lumen signpost: the explanation line when its menu cannot be opened, and
    // the launcher node this generation hid (restored exactly on teardown).
    let lumenOpenError = '';
    // One line on the Lumen page after the account row handed over (or failed to).
    let lumenSignInNote = '';
    let hiddenLumenBtn = null;
    let panelMounted = false;
    // Per-RPC failure text for the Updates page (an error row, never a broken page).
    let rpcErrors = {};
    // "Update everything" bookkeeping: what this session queued, and when.
    let everything = null;
    let steamLatestArmed = false;
    let lunaCheckedAt = 0;
    let healthNotice = null;
    // The last backend job's live line and outcome (repair, pin, shortcuts).
    let jobNotice = '';
    let compressorTab = 'status';
    let compressorQuery = '';
    let pendingAction = null;
    let compressorNotice = null;
    // In-page confirmation state: native confirm()/prompt() freeze the Steam
    // shell, so every destructive action is armed with a second click instead.
    let confirmAction = null;
    let steamLockArmed = null;
    let steamJobNotice = null;
    let lunaHost = null;
    const gradient = mountGradient(w, life);
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
      jobTimer = life.interval(async () => {
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

    /* One sidebar row, used by the categories and by Lumen's nested pages: the
       same builder means the same height, spacing and hover behaviour. A nested
       row only adds `.sls-luna-subpage` for its indent and smaller type.
       Roving tabindex: exactly one row is in the tab order (the active one),
       so Tab never has to walk a dozen rows and the arrow keys are what move
       inside the list. `aria-current` marks the category on screen. */
    function sidebarRow({ label, iconId, active, sub, onclick }) {
      const item = document.createElement('div');
      item.className = 'sls-luna-tab' + (sub ? ' sls-luna-subpage' : '') + (active ? ' active' : '');
      item.setAttribute('role', 'button');
      item.setAttribute('tabindex', active ? '0' : '-1');
      if (active) item.setAttribute('aria-current', 'page');
      item.__lunaNav = { sub: Boolean(sub), id: '' };
      if (iconId) {
        const icon = document.createElement('span');
        icon.className = 'ico';
        icon.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true">' + ICON[iconId] + '</svg>';
        item.appendChild(icon);
      }
      const text = document.createElement('span');
      text.textContent = label;
      item.appendChild(text);
      item.onclick = onclick;
      item.onkeydown = event => onSidebarKey(event, item);
      return item;
    }

    // The rows currently in the sidebar, in the order the user sees them.
    function navRows() {
      if (!shell || !shell.side) return [];
      return Array.from(shell.side.children || []).filter(node => node && node.__lunaNav);
    }

    function focusNavRow(node) {
      if (!node) return;
      // Roving: moving focus also moves the single tab stop.
      navRows().forEach(row => row.setAttribute('tabindex', row === node ? '0' : '-1'));
      try { if (typeof node.focus === 'function') node.focus(); } catch (_) { /* no focus here */ }
    }

    // Keys typed into a field, or into Lumen's own page, are never the
    // sidebar's: both would otherwise lose their text or their navigation.
    function editingTarget(node) {
      if (!node || typeof node !== 'object') return false;
      const tag = String(node.tagName || '').toLowerCase();
      if (tag === 'input' || tag === 'textarea' || tag === 'select') return true;
      return node.isContentEditable === true || node.contentEditable === 'true';
    }

    function insideNode(node, root) {
      let walk = node;
      for (let depth = 0; walk && depth < 40; depth += 1) {
        if (walk === root) return true;
        walk = walk.parentNode || null;
      }
      return false;
    }

    function onSidebarKey(event, item) {
      if (!event || !current()) return;
      if (editingTarget(event.target)) return;
      const embed = lumenEmbedState && lumenEmbedState.overlay;
      if (embed && insideNode(event.target, embed)) return;
      const key = String(event.key || event.code || '');
      const rows = navRows();
      const index = rows.indexOf(item);
      if (index < 0) return;
      if (key === 'Enter' || key === ' ' || key === 'Spacebar') {
        if (typeof event.preventDefault === 'function') event.preventDefault();
        if (typeof item.onclick === 'function') item.onclick(event);
        return;
      }
      let next = -1;
      if (key === 'ArrowDown') next = Math.min(rows.length - 1, index + 1);
      else if (key === 'ArrowUp') next = Math.max(0, index - 1);
      else if (key === 'Home') next = 0;
      else if (key === 'End') next = rows.length - 1;
      if (next < 0) return;
      if (typeof event.preventDefault === 'function') event.preventDefault();
      focusNavRow(rows[next]);
    }

    let lumenSubtabsDrawn = false;
    const sidebar = () => {
      lumenSubtabsDrawn = false;
      const items = [];
      visibleNav().forEach(entry => {
        const item = sidebarRow({
          label: entry.label + (entry.id === 'updates' && updateBadge() ? ' \u2022' : ''),
          iconId: entry.id,
          active: entry.id === tab,
          onclick: () => { selectCategory(entry.id); },
        });
        item.__lunaNav.id = entry.id;
        items.push(item);
        if (entry.id === 'lumen' && lumenSubtabsWanted()) {
          const subs = lumenSubEntries();
          if (subs.length) lumenSubtabsDrawn = true;
          items.push(...subs);
        }
      });
      return items;
    };

    /* One footer row, like Lumen's: an avatar (or a dot), the LuaTools/Discord
       name and a short label on the right. The row is the account button: it
       closes this menu and hands over to Lumen's own sign-in UI, because Lumen
       owns the LuaTools session and Luna must not reimplement it.
       "Connected" is only rendered when the backend reports a signed-in
       LuaTools session with a display name. An expires_at in the past is not a
       state: that token lasts an hour and LuaTools refreshes it itself, so a
       past value only means nothing has refreshed it yet. */
    // The backend validates this too; the page repeats it so a stale or
    // hand-edited value can never put a foreign URL on an <img>.
    const DISCORD_AVATAR = /^https:\/\/(?:cdn\.discordapp\.com|media\.discordapp\.net)\//i;

    function footerState(info) {
      const state = info || {};
      if (state.unavailable) {
        return { name: 'LuaTools', label: 'Unavailable', ok: false, avatar: '',
          why: String(state.error || 'the LuaTools state could not be read') };
      }
      if (state.signedIn === true) {
        const name = String(state.name || '');
        if (name) {
          const avatar = String(state.avatar || '');
          return { name: name, label: 'Connected', ok: true,
            avatar: DISCORD_AVATAR.test(avatar) ? avatar : '',
            why: 'Signed in through Discord via LuaTools.' };
        }
      }
      return { name: 'LuaTools', label: 'Sign in', ok: false, avatar: '',
        why: String(state.error || '') };
    }

    function accountRow() {
      const info = footerState(luatools);
      const row = document.createElement('button');
      row.className = 'sls-luna-account';
      row.setAttribute('role', 'button');
      row.setAttribute('tabindex', '0');
      row.title = info.why ? 'LuaTools account \u2014 ' + info.why : 'LuaTools account';
      const dot = document.createElement('span');
      dot.className = 'sls-luna-dot' + (info.ok ? ' ok' : '');
      dot.style.display = '';
      row.appendChild(dot);
      if (info.avatar) {
        // The URL comes validated from the backend; still assigned through the
        // property (never innerHTML) and without a referrer, so the Discord CDN
        // never sees the Steam page and a broken URL degrades to the dot.
        const img = document.createElement('img');
        img.className = 'sls-luna-avatar';
        img.width = 24;
        img.height = 24;
        img.alt = '';
        img.referrerPolicy = 'no-referrer';
        img.style.width = '24px';
        img.style.height = '24px';
        img.style.borderRadius = '50%';
        img.style.objectFit = 'cover';
        img.onerror = () => {
          if (typeof img.remove === 'function') img.remove();
          dot.style.display = '';
        };
        img.src = info.avatar;
        dot.style.display = 'none';
        row.appendChild(img);
      }
      const name = document.createElement('span');
      name.textContent = info.name;
      const state = document.createElement('span');
      state.className = 'sls-luna-state' + (info.ok ? ' ok' : '');
      if (!info.ok) state.style.color = '#8d97a1';
      state.textContent = info.label;
      row.append(name, state);
      row.onclick = openLuaToolsAccount;
      row.onkeydown = event => {
        const key = String((event && (event.key || event.code)) || '');
        if (key === 'Enter' || key === ' ' || key === 'Spacebar') {
          if (event && typeof event.preventDefault === 'function') event.preventDefault();
          openLuaToolsAccount();
        }
      };
      return row;
    }

    /* Lumen's sidebar footer button opens the account page, and a click works
       even while that sidebar is hidden by the embed. `__lumenOpenLuaToolsAccount`
       is NOT used here: it returns at once when the overlay already exists,
       which is always the case once embedded. */
    function lumenAccountButton(overlay) {
      const root = overlay || document.getElementById('lumen-settings-overlay');
      return root ? findClass(root, 'lumen-account-entry') : null;
    }

    function lumenAccountPanel() {
      return document.getElementById('lumen-panel-account') || null;
    }

    /* The account panel counts as a Lumen page: Lumen shows it by display, and
       marks its own sidebar entry, exactly like the other panels. */
    function lumenAccountVisible() {
      const panel = lumenAccountPanel();
      if (!panel) return false;
      const style = panel.style || {};
      if (panel.hidden === true || style.display === 'none') return false;
      if (classTokens(panel).indexOf('hidden') >= 0) return false;
      return (typeof style.display === 'string' && style.display !== '')
        || classTokens(panel).indexOf('active') >= 0;
    }

    /* Open the account page inside the embed: click Lumen's own entry; if it is
       gone, Lumen's supported open path with the account tab pre-selected (its
       overlay has to be rebuilt for that, and the observer re-embeds it). */
    function openLumenAccountPage() {
      const button = lumenAccountButton();
      if (button && typeof button.click === 'function') {
        try { button.click(); } catch (error) {
          lunaWarn('Lumen\u2019s account entry did not open: ' + errorText(error), w);
        }
        return true;
      }
      if (typeof w.__lumenOpenOverlay !== 'function') return false;
      try {
        w.__lumenSettingsInitialTab = 'account';
        if (typeof w.__lumenCloseOverlay === 'function') w.__lumenCloseOverlay();
        w.__lumenOpenOverlay();
        return true;
      } catch (error) {
        lunaWarn('Lumen\u2019s account page did not open: ' + errorText(error), w);
        return false;
      }
    }

    /* The account lives in Lumen, but the click must keep everything inside
       Luna. With embedding on: switch to the Lumen category, wait until the
       pane is laid in, then open Lumen's account page in it. With embedding
       off, or after this session fell back, the old hand-over runs: close Luna
       and let Lumen show its own window. */
    function openLuaToolsAccount() {
      if (!lumenEmbedWanted() || lumenEmbedBlocked) { handOverToLumenAccount(); return; }
      const entered = selectCategory('lumen');
      if (entered === 'door' || !document.getElementById(OVERLAY_ID)) {
        // Embedding fell back to Lumen's own window, which is in front of Luna.
        handOverToLumenAccount();
        return;
      }
      const started = Date.now();
      const step = () => {
        if (tornDown || tab !== 'lumen' || !document.getElementById(OVERLAY_ID)) return;
        if (lumenEmbedState) {
          lumenSignInNote = openLumenAccountPage()
            ? '' : 'Open the account from Lumen\u2019s own menu.';
          repaintIfOpen();
          return;
        }
        if (Date.now() - started > 3000) {
          lumenSignInNote = 'Open the account from Lumen\u2019s own menu.';
          repaintIfOpen();
          return;
        }
        life.timeout(step, 50);
      };
      life.timeout(step, 0);
    }

    // Embedding off (Settings) or blocked this session: the old door.
    function handOverToLumenAccount() {
      closeMenu();
      let open = null;
      try {
        open = typeof w.__lumenOpenLuaToolsAccount === 'function' ? w.__lumenOpenLuaToolsAccount : null;
      } catch (_) { open = null; }
      let note = 'Open the account from Lumen\u2019s own menu.';
      if (open) {
        try {
          open();
          return;
        } catch (error) {
          note = 'Lumen\u2019s account window did not open: ' + errorText(error);
          lunaWarn(note, w);
        }
      }
      showMenu();
      selectCategory('lumen');
      lumenSignInNote = note;
      repaintIfOpen();
    }

    function connected() {
      const live = (snapshot && snapshot.runtime) || {};
      return Boolean(live.healthy && Number(live.reportedAt || 0) > 0
        && Date.now() / 1000 - Number(live.reportedAt) < 45);
    }

    // One playtime read. With a known `rev` the backend answers a 59-byte
    // {unchanged:true} when nothing moved, and the painted snapshot stays; a
    // failed read keeps the last good payload instead of blanking the page.
    // Returns true only when the painted data really changed.
    async function readPlaytime() {
      if (playtimeBusy) return false;
      playtimeBusy = true;
      try {
        const out = await rpc(w, 'SlsPlaytimeGet',
          snapshotRev === null ? {} : { raw: JSON.stringify({ rev: snapshotRev }) });
        if (out && out.unchanged === true) {
          if (out.rev !== undefined) snapshotRev = out.rev;
          return false;
        }
        snapshot = out || {};
        snapshotRev = out && out.rev !== undefined ? out.rev : null;
        playtimeRecovered = Boolean(out && out.recovered === true);
        playtimeMerge = out && out.merge && typeof out.merge === 'object' ? out.merge : null;
        return true;
      } catch (error) {
        snapshot = snapshot || {};
        return false;
      } finally {
        playtimeBusy = false;
      }
    }

    function statsRow() {
      const stats = (snapshot && snapshot.stats) || {};
      const wrap = document.createElement('div');
      wrap.className = 'sls-luna-stat';
      [['Today', stats.today], ['Last 7 days', stats.last7d], ['Last 30 days', stats.last30d]].forEach(pair => {
        const cell = document.createElement('div');
        const value = document.createElement('b');
        // One formatter for the whole page: the totals, the bars and the week
        // top all read "0 min", "45 min" or "12 h 30 min".
        value.textContent = minutesText(pair[1]);
        const label = document.createElement('i');
        label.textContent = pair[0];
        cell.append(value, label);
        wrap.appendChild(cell);
      });
      return wrap;
    }

    /* The 30-day view the newer backend can answer for: 30 plain divs, no
       canvas and no per-pixel work, so a menu repaint stays cheap. An older
       backend sends no `daily30`, which returns null here and the page keeps
       exactly its three totals above: no chart, no warning, no empty box. */
    function playtimeChart() {
      const stats = (snapshot && snapshot.stats) || {};
      const chart = dailyBars(stats);
      if (!chart) return null;
      const wrap = document.createElement('div');
      wrap.className = 'sls-luna-chart-wrap';
      const caption = document.createElement('p');
      caption.className = 'sls-luna-note';
      caption.textContent = 'Playtime per day, last 30 days';
      wrap.appendChild(caption);

      const plot = document.createElement('div');
      plot.className = 'sls-luna-chart';
      plot.style.cssText = 'display:flex;align-items:flex-end;gap:2px;height:64px;'
        + 'padding:2px 0;border-bottom:1px solid rgba(255,255,255,.12);';
      plot.setAttribute('role', 'img');
      plot.setAttribute('aria-label', chart.max > 0
        ? 'Playtime for the last 30 days: ' + minutesText(chart.total) + ' in total, busiest day '
          + chart.busiest.label + ' with ' + minutesText(chart.busiest.seconds) + '.'
        : 'No playtime recorded in the last 30 days.');
      chart.bars.forEach(bar => {
        const node = document.createElement('div');
        node.className = 'sls-luna-bar' + (bar.today ? ' is-today' : '') + (bar.weekend ? ' is-weekend' : '');
        node.title = bar.label + ': ' + minutesText(bar.seconds);
        node.setAttribute('aria-hidden', 'true');
        const share = chart.max > 0 ? Math.round((bar.seconds / chart.max) * 100) : 0;
        node.style.cssText = 'flex:1 1 0;min-width:0;align-self:flex-end;border-radius:2px 2px 0 0;'
          + 'height:' + (bar.seconds > 0 ? Math.max(3, share) : 0) + '%;'
          + 'background:' + (bar.today ? '#8ab4f8'
            : bar.weekend ? 'rgba(255,255,255,.20)' : 'rgba(255,255,255,.38)') + ';';
        plot.appendChild(node);
      });
      wrap.appendChild(plot);

      const sentence = weeklySentence(stats, chart);
      if (sentence) {
        const week = document.createElement('p');
        week.className = 'sls-luna-note sls-luna-week';
        week.textContent = sentence;
        wrap.appendChild(week);
      }
      const names = Object.assign({}, (snapshot && snapshot.titles) || {},
        (snapshot && snapshot.shortcutNames) || {});
      const top = weekTop(stats, names);
      if (top) {
        const list = document.createElement('div');
        list.className = 'sls-luna-weektop';
        top.forEach(entry => {
          const row = document.createElement('div');
          row.className = 'sls-luna-weektop-row';
          row.style.cssText = 'display:flex;gap:8px;justify-content:space-between;';
          const name = document.createElement('span');
          name.className = 'gname';
          name.textContent = entry.name;
          const value = document.createElement('span');
          value.textContent = minutesText(entry.seconds);
          row.append(name, value);
          list.appendChild(row);
        });
        wrap.appendChild(list);
      }
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
      // Two small diagnostics the backend can attach: a ledger recovered from
      // a backup, or sync-conflict copies it had to merge. Both are one line,
      // and an older backend sends neither.
      if (snapshot && playtimeRecovered) {
        const recovered = document.createElement('p');
        recovered.className = 'sls-luna-note sls-luna-playtime-note';
        recovered.textContent = 'The playtime ledger was recovered from the previous good backup.';
        body.appendChild(recovered);
      }
      const conflicts = snapshot && playtimeMerge && Array.isArray(playtimeMerge.conflictFiles)
        ? playtimeMerge.conflictFiles : [];
      if (conflicts.length) {
        const merged = document.createElement('p');
        merged.className = 'sls-luna-note sls-luna-playtime-note';
        merged.textContent = 'Steam sync left ' + conflicts.length + ' conflicting ledger cop'
          + (conflicts.length === 1 ? 'y' : 'ies') + ' ('
          + conflicts.slice(0, 3).map(name => String(name)).join(', ')
          + (conflicts.length > 3 ? '\u2026' : '') + '); the newest copy is in use.';
        body.appendChild(merged);
      }
      if (snapshot) body.appendChild(statsRow());
      const chart = snapshot ? playtimeChart() : null;
      if (chart) body.appendChild(chart);

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
        const confirmLine = document.createElement('p');
        confirmLine.className = 'sls-luna-note';
        let armed = false;
        cancel.onclick = () => {
          area.remove();
          row.remove();
          if (confirmLine.parentNode && typeof confirmLine.remove === 'function') confirmLine.remove();
        };
        replace.onclick = async () => {
          const raw = area.value.trim();
          if (!raw) { showError(body, Error('Paste a backup first.')); return; }
          if (!armed) {
            // Two clicks, in the page: a native confirm() freezes the shell, and
            // replacing every record must never be one stray click away.
            armed = true;
            const total = Object.keys((snapshot && snapshot.games) || {}).length;
            replace.textContent = 'Confirm replace';
            confirmLine.textContent = 'This replaces all ' + total + ' local record'
              + (total === 1 ? '' : 's') + ' with the backup. Click Confirm replace to continue, '
              + 'or Cancel to keep what you have.';
            body.insertBefore(confirmLine, row.nextSibling || null);
            return;
          }
          armed = false;
          replace.textContent = 'Replace ALL local records';
          try {
            await rpc(w, 'SlsPlaytimeImport', { raw });
            area.remove(); row.remove();
            if (confirmLine.parentNode && typeof confirmLine.remove === 'function') confirmLine.remove();
            await refresh();
          } catch (error) {
            armed = true;
            replace.textContent = 'Confirm replace';
            showError(body, error);
          }
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
      let saving = false;
      save.onclick = async () => {
        // One save per row at a time: the button goes busy and a second click
        // (or a held Enter) cannot send a second write.
        if (saving) return;
        saving = true;
        save.disabled = true;
        save.textContent = 'Saving\u2026';
        try {
          await rpc(w, 'SlsPlaytimeEdit', { raw: JSON.stringify({ id: Number(entry.id), hours: Number(input.value) }) });
          await refresh();
        } catch (error) { showError(body, error); }
        finally {
          saving = false;
          save.disabled = false;
          save.textContent = 'Save';
        }
      };
      row.append(name, input, save);
      return row;
    }

    function presenceBody() {
      const body = document.createElement('div');
      const state = (presence && presence.state) || {};
      const runtime = (presence && presence.runtime) || {};
      // The optional `discord` block (newer backend). Absent on an older one:
      // then every line below renders exactly as it always did.
      const discord = presence && presence.discord && typeof presence.discord === 'object'
        ? presence.discord : null;
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

      // Discord Rich Presence: Luna talks to the local Discord client itself.
      const dTxt = (prefValue('lang') === 'es') ? {
        title: 'Discord', sub: 'Muestra el nombre real del juego en Discord mientras juegas.',
        hint: 'Crea una aplicación con el nombre de tu launcher en discord.com/developers y pega su Application ID',
        placeholder: 'Application ID de Discord', save: 'Guardar',
        needId: 'Discord necesita un Application ID; no se publica nada hasta que lo pongas.',
        badId: 'Ese Application ID no es válido; no se publica nada hasta corregirlo.',
        noSocket: 'Discord no está abierto en esta máquina; ábrelo para volver a publicar.',
        notPublishing: 'Discord está conectado pero ahora mismo no publica.',
        appIdRule: 'El Application ID debe tener entre 17 y 20 dígitos.',
      } : {
        title: 'Discord', sub: 'Shows the real game name in Discord while you play.',
        hint: 'Create an application named after your launcher at discord.com/developers and paste its Application ID',
        placeholder: 'Discord Application ID', save: 'Save',
        needId: 'Discord needs an Application ID; nothing is published until one is set.',
        badId: 'That Application ID is not valid; nothing is published until it is fixed.',
        noSocket: 'Discord is not running on this machine; start Discord to publish again.',
        notPublishing: 'Discord is connected but is not publishing right now.',
        appIdRule: 'Application ID must be 17-20 digits.',
      };
      const discordOn = state.discordEnabled !== false;
      const discordRow = document.createElement('div');
      discordRow.className = 'sls-luna-row';
      const discordText = document.createElement('div');
      discordText.className = 'txt';
      const discordName = document.createElement('div');
      discordName.className = 'name';
      discordName.textContent = dTxt.title;
      const discordSub = document.createElement('div');
      discordSub.className = 'sub';
      discordSub.textContent = dTxt.sub;
      discordText.append(discordName, discordSub);
      const discordSwitch = document.createElement('div');
      discordSwitch.className = 'sls-luna-switch' + (discordOn ? ' on' : '');
      discordSwitch.appendChild(document.createElement('span'));
      discordSwitch.onclick = async () => {
        try {
          await rpc(w, 'SlsPresenceSetDiscord', { raw: JSON.stringify({ enabled: !discordOn }) });
          await refresh();
        } catch (error) { showError(body, error); }
      };
      discordRow.append(discordText, discordSwitch);
      body.appendChild(discordRow);
      const discordIdRow = document.createElement('div');
      discordIdRow.className = 'sls-luna-row';
      const discordInput = document.createElement('input');
      discordInput.className = 'sls-luna-search';
      discordInput.placeholder = dTxt.placeholder;
      discordInput.value = String(state.discordAppId || '');
      const discordSave = document.createElement('button');
      discordSave.className = 'sls-luna-btn';
      discordSave.textContent = dTxt.save;
      // A refused ID is a line under the field, not a banner at the top: the
      // field is what the user is looking at. Both a local refusal and the
      // backend's `invalid-app-id` land here.
      let discordError = null;
      const showDiscordError = message => {
        if (discordError && typeof discordError.remove === 'function') discordError.remove();
        discordError = document.createElement('p');
        discordError.className = 'sls-luna-error';
        discordError.textContent = message;
        body.insertBefore(discordError, discordIdRow.nextSibling || null);
      };
      discordSave.onclick = async () => {
        const value = discordInput.value.trim();
        // Discord application IDs are snowflakes: 17-20 digits. An older
        // backend has no `discord` block and keeps the old accepted range.
        if (discord && value !== '' && !/^\d{17,20}$/.test(value)) {
          showDiscordError(dTxt.appIdRule);
          return;
        }
        try {
          await rpc(w, 'SlsPresenceSetDiscord', { raw: JSON.stringify({ appId: value }) });
          await refresh();
        } catch (error) {
          showDiscordError(String(error && error.message ? error.message : error));
        }
      };
      discordIdRow.append(discordInput, discordSave);
      body.appendChild(discordIdRow);
      const discordHint = document.createElement('p');
      discordHint.className = 'sls-luna-note';
      discordHint.textContent = dTxt.hint;
      body.appendChild(discordHint);
      // Why nothing is published, in plain words, only while Discord is on and
      // the backend says it is not publishing.
      if (discord && discordOn && discord.publishing === false) {
        const reason = String(discord.reason || '');
        const why = document.createElement('p');
        why.className = 'sls-luna-note sls-luna-discord-note';
        why.textContent = reason === 'no-app-id' ? dTxt.needId
          : reason === 'invalid-app-id' ? dTxt.badId
          : reason === 'no-socket' ? dTxt.noSocket : dTxt.notPublishing;
        body.appendChild(why);
      }

      const owned = Object.keys(state.owned || {}).length;
      const running = Object.keys(state.running || {}).length;
      const health = document.createElement('p');
      health.className = 'sls-luna-note';
      health.innerHTML = 'Agent ' + (runtime.healthy ? '<strong>connected</strong>' : 'not ready')
        + '. Placeholders owned: <strong>' + owned + '</strong>, in use: <strong>' + running + '</strong>.'
        + (state.account ? ' Account ' + String(state.account).slice(-5) + '.' : '')
        + (runtime.error ? ' ' + runtime.error : '')
        + (discord && discord.publishing === false ? ' Discord: not publishing.' : '')
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
        line.innerHTML = '<strong>Running now:</strong> ' + escapeHtml(ACTION_LABELS[job.action] || job.action)
          + ' \u00b7 ' + age(job.startedAt) + '.';
      } else if (pendingAction) {
        line.innerHTML = '<strong>Starting</strong> '
          + escapeHtml(ACTION_LABELS[pendingAction.action] || pendingAction.action) + '\u2026';
      } else if (job) {
        line.innerHTML = 'Last job: ' + escapeHtml(ACTION_LABELS[job.action] || job.action)
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
      button.disabled = Boolean(compressor && compressor.job && compressor.job.stuck);
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

    // The queue's savings contract: explicit byte fields win; only when
    // they are absent do the legacy MiB
    // counters get used, and then they are labelled MiB, never bytes.
    function savingsSize(savings, key) {
      if (!savings || typeof savings !== 'object') return '';
      if (savings[key + 'Bytes'] !== undefined && savings[key + 'Bytes'] !== null) {
        return formatSize(savings[key + 'Bytes'], 'bytes');
      }
      if (savings[key + 'Mib'] !== undefined && savings[key + 'Mib'] !== null) {
        return formatSize(savings[key + 'Mib'], 'mib');
      }
      return '';
    }

    function compressorQueueBody(body) {
      const queue = (compressor && compressor.queue) || {};
      const current = queue.current;
      const active = queue.active === true;
      const total = Number(queue.total) || 0;
      const done = Number(queue.done) || 0;
      const failed = Number(queue.failed) || 0;
      const skipped = Number(queue.skipped) || 0;
      const savings = queue.savings && typeof queue.savings === 'object' ? queue.savings : null;
      const measuredText = savings && savings.measuredGames != null && Number(savings.measuredGames) === 0
        ? '' : savingsSize(savings, 'measured');
      const estimateText = savingsSize(savings, 'estimate');
      const savedText = measuredText
        || (Number(queue.saved) > 0 ? formatSize(queue.saved, 'bytes') : '')
        || (estimateText ? 'about ' + estimateText + ' (estimate)' : '');
      const outcome = queue.outcome && typeof queue.outcome === 'object' ? queue.outcome : null;
      const outcomeKind = outcome ? String(outcome.kind || '') : '';
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
      // A library refusal is not a finished run: nothing was attempted, so the
      // page says so (and why) instead of "Last run finished".
      head.innerHTML = '<strong>' + (active ? 'Run in progress'
        : outcomeKind === 'not-started' ? 'Last run did not start'
        : queue.finished ? 'Last run finished' : 'Last run stopped') + '</strong> \u00b7 '
        + done + ' of ' + total + ' games done'
        + (overall ? ' \u00b7 ' + formatBytes(overall.doneBytes) + ' of ' + formatBytes(overall.totalBytes) : '')
        + (failed ? ' \u00b7 failed ' + failed : '')
        + (skipped ? ' \u00b7 skipped ' + skipped : '')
        + (savedText ? ' \u00b7 saved ' + savedText : '');
      body.appendChild(head);
      body.appendChild(progressBar(barPct, false));

      if (!active && outcomeKind === 'not-started') {
        const reason = String(outcome.reasonText || outcome.reason || '').trim();
        const nextStep = String(outcome.nextStep || '').trim();
        const why = document.createElement('p');
        why.className = 'sls-luna-note sls-luna-queue-outcome';
        why.textContent = 'The library refused the pass'
          + (reason ? ': ' + reason : '')
          + (outcome.path ? ' (' + outcome.path + ')' : '') + '.'
          + (nextStep ? ' Next: ' + nextStep + (/[.!?]$/.test(nextStep) ? '' : '.') : '');
        body.appendChild(why);
      }

      if (current) {
        const pct = current.phase === 'dedupe'
          ? (Number(current.size) > 0
            ? Math.min(100, (Number(current.scannedBytes) || 0) / Number(current.size) * 100) : 0)
          : (Number(current.totalBytes) > 0
            ? Number(current.doneBytes) / Number(current.totalBytes) * 100 : 0);
        const line = document.createElement('p');
        line.className = 'sls-luna-note';
        line.innerHTML = 'Game ' + (Number(queue.index) || 0) + ' of ' + total + ': <strong>'
          + escapeHtml(current.name) + '</strong> '
          + (current.phase === 'dedupe'
            ? '\u00b7 deduplicating' + (Number(current.scannedBytes) > 0
              ? ' (' + formatBytes(current.scannedBytes) + ' scanned)' : '')
            : '\u00b7 compressing' + (Number(current.totalBytes) > 0 ? ' ' + pctText(pct) + '%' : ''));
        body.appendChild(line);
        body.appendChild(progressBar(pct, false));
      }

      if (!active && !queue.finished && total > 0 && outcomeKind !== 'not-started') {
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
      if (job && job.error) {
        const errorLine = document.createElement('p');
        errorLine.className = 'sls-luna-note';
        errorLine.textContent = job.error;
        body.appendChild(errorLine);
      }
      if (job && job.stuck) {
        const stuckLine = document.createElement('p');
        stuckLine.className = 'sls-luna-note';
        stuckLine.textContent = job.stuck.message;
        body.appendChild(stuckLine);
      }
      if (compressor.rescan) {
        const refreshLine = document.createElement('p');
        refreshLine.className = 'sls-luna-note';
        refreshLine.textContent = compressor.rescan.failed
          ? 'Snapshot refresh failed: ' + compressor.rescan.failed
          : 'Snapshot refresh pending; library results may be out of date.';
        body.appendChild(refreshLine);
      }

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
        line.innerHTML = 'Progress: <strong>' + escapeHtml(position) + '</strong>'
          + (progress && progress.pct ? ' \u00b7 ' + pctText(progress.pct) + '%' : '')
          + (progress && progress.total && progress.label ? ' \u00b7 ' + escapeHtml(progress.label) : '');
        body.appendChild(line);
        body.appendChild(progressBar(progress && progress.pct, !progress || !progress.pct));
      }

      const actions = document.createElement('div');
      actions.style.cssText = 'display:flex;gap:8px;flex-wrap:wrap;margin:10px 0 8px;';
      actions.appendChild(jobButton(body, 'Scan library', 'scan', true, running));
      actions.appendChild(jobButton(body, 'Preview (dry-run)', 'preview', true, running || Boolean(compressor.rescan)));
      if (confirmAction !== 'run') {
        const runButton = document.createElement('button');
        runButton.className = 'sls-luna-btn';
        runButton.textContent = 'Run now';
        runButton.disabled = running || !runs || Boolean(compressor.rescan);
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
      [['Measured saved', formatSize(measuredSaved, 'bytes'), 'ZSTD + deduplication'],
       ['Library on disk', formatSize(library.bytes, 'bytes'), (Number(library.games) || 0) + ' games in the last scan'],
       ['Compacted', (Number(summary.compacted) || 0) + ' / ' + (Number(summary.game_count) || 0), 'games done'],
       ['Luna passes', formatSize(luna.saved_bytes, 'bytes'), (Number(luna.measured) || 0) + ' games with savings']]
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
          value: sizeToBytes(game.saved_bytes, 'bytes'),
          display: formatSize(game.saved_bytes, 'bytes') + ' \u00b7 '
            + pctText(game.original_bytes ? game.saved_bytes * 100 / game.original_bytes : 0) + '%',
        })),
        { empty: Number(compression.measured)
          ? 'Nothing measured yet.'
          : 'No measured ZSTD rows yet \u2014 press Measure now to fill this ledger.' });

      const lunaGames = listOf(luna.games).slice(0, 8);
      compressorChart(body, 'Savings recorded by Luna passes',
        lunaGames.map(game => ({ label: game.name, value: sizeToBytes(game.saved_bytes, 'bytes'),
          display: formatSize(game.saved_bytes, 'bytes') })),
        { empty: 'No per-game savings were recorded yet \u2014 a pass that reclaims nothing records zero.' });

      const dedupeEntries = listOf(dedupe.entries).slice(0, 8);
      compressorChart(body, 'Deduplication per directory',
        dedupeEntries.map(entry => ({
          label: String(entry.path || '').split('/').pop() || String(entry.path || ''),
          title: entry.path || '', value: sizeToBytes(entry.saved_bytes, 'bytes'),
          display: formatSize(entry.saved_bytes, 'bytes'),
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
            ? 'done \u00b7 saved ' + formatSize(queueEntry.saved, 'bytes') : state;
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
    // Local mirror of the Lumen embedding switch, so the choice holds in the
    // first seconds of a session, before SlsLunaInfo has answered.
    const LUMEN_EMBED_KEY = 'slsLunaLumenEmbed';
    let betaNotice = null;
    let steamosNotice = null;
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
      if (prefs.lumen_button === undefined && localItem(LUMEN_BTN_KEY) === '1') patch.lumen_button = true;
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
      if (fresh.lumen_button !== undefined) setLocalItem(LUMEN_BTN_KEY, fresh.lumen_button ? '1' : '0');
      if (fresh.lumen_embed !== undefined) setLocalItem(LUMEN_EMBED_KEY, fresh.lumen_embed ? '1' : '0');
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

    /* One reader for every SlsHltbGet: remembers the revision, keeps the
       previous payload when the backend says unchanged, and forgets the rev
       after any error (the next call asks for the full reply). */
    async function readHltb() {
      try {
        const out = await rpc(w, 'SlsHltbGet', hltbRev === null ? {} : { rev: hltbRev });
        if (out && out.unchanged === true) {
          if (out.rev !== undefined) hltbRev = out.rev;
          return hltb;
        }
        hltbRev = out && out.rev !== undefined ? out.rev : null;
        return out;
      } catch (error) {
        hltbRev = null;
        throw error;
      }
    }
    // A write moves the backend's revision on, so the next read must be full.
    const forgetHltbRev = () => { hltbRev = null; };

    async function refreshHltb() {
      try {
        hltb = await readHltb();
      } catch (error) {
        hltb = hltb || {};
      }
      try { refreshLibraryShelves(); } catch (_) { /* shelves render later */ }
      repaintIfOpen();
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
    // The anchor div is reused while it stays in the document, so a refresh does
    // not copy and key-scan every div in the library on each pass.
    let fiberAnchor = null;
    const fiberKey = el => Object.keys(el).find(key => key.indexOf('__reactFiber') === 0);
    function libraryRootFiber() {
      if (!fiberAnchor || fiberAnchor.isConnected !== true || !fiberKey(fiberAnchor)) {
        fiberAnchor = null;
        const divs = document.querySelectorAll ? document.querySelectorAll('div') : [];
        for (const candidate of divs) {
          if (fiberKey(candidate)) { fiberAnchor = candidate; break; }
        }
      }
      if (!fiberAnchor) return null;
      let fiber = fiberAnchor[fiberKey(fiberAnchor)];
      let depth = 0;
      while (fiber && fiber.return && depth < 500) { fiber = fiber.return; depth += 1; }
      // A node's fiber can be the stale alternate; the root container always
      // points at the tree React rendered last.
      const current = fiber && fiber.stateNode && fiber.stateNode.current;
      return current && typeof current === 'object' ? current : fiber;
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
            shelfWantsHltb = true;
            // A shelf sorted by HowLongToBeat needs the cached times even if
            // the page was never opened: one read, then one re-walk so the
            // shelf redraws with real times instead of name order.
            if (!hltb && !hltbKick) {
              hltbKick = true;
              load('hltb').then(() => {
                hltbKick = false;
                if (hltb) { resetShelfWalk(); refreshLibraryShelves(); }
              });
            }
            return collection.visibleApps.slice().sort(compareHltbHere);
          }
        } catch (_) { /* fall through to Steam's sorter */ }
        return original.call(this, collection, comparator);
      };
      return true;
    }

    /* --- Library shelf refresh -------------------------------------------
       What the walk is for: Steam's home/library shelves are React class
       instances with their own m_fnSort. A shelf whose sort is 12
       ("HowLongToBeat") is patched to sort by the cached times, and it is
       forced to re-render only when it was just patched or the times/direction
       it last drew with changed. Every other shelf keeps Steam's own sorter.

       Cost control, because the menu is normally closed while the user browses:
       - the observer is bound to the library view container, not document.body,
         and is re-bound only when that container appears or is replaced;
       - a mutation only schedules work when Steam added element nodes outside
         Luna's own UI, and only the fibers under those nodes are visited (a
         WeakSet skips repeats), so a storm of unrelated mutations costs one
         cheap filter pass;
       - one pass runs in idle slices of <= 8 ms;
       - the fallback rescan runs every 2 min, only while visible and focused,
         and bails out when a cheap signature says nothing changed. */
    const SHELF_SLICE_MS = 8;
    const SHELF_DEBOUNCE_MS = 800;
    const SHELF_SAFETY_MS = 120000;
    const SHELF_QUIET_MS = 1500;
    const SHELF_ROOT_CAP = 64;
    const SHELF_QUEUE_CAP = 4096;
    // Steam has no stable id for the library; the desktop shell's container
    // class carries the stable "LibraryDisplaySize" suffix, the Deck/BPM shell
    // uses this id. Either is the smallest element the shelves live under.
    const LIBRARY_ROOT_IDS = ['GamepadUI_Library', 'GamepadUI_LibraryHome'];
    const LIBRARY_ROOT_HINT = '[class*="LibraryDisplaySize"]';
    let shelfObserverRoot = null;
    let shelfObserver = null;
    let shelfTimer = null;
    let shelfWalkTimer = null;
    let shelfIdleHandle = null;
    let shelfQueue = [];
    let shelfScheduled = false;
    let shelfPendingRoots = [];
    let shelfPassRunning = false;
    let shelfPassStart = 0;
    let shelfSeen = new WeakSet();
    let shelfEpoch = { data: null, order: '' };
    // Set once a shelf is really drawn with sort 12 ("HowLongToBeat"): only then
    // is the HLTB cache worth reading outside its own page.
    let shelfWantsHltb = false;
    let hltbKick = false;
    let shelfQuietUntil = 0;
    let shelfSig = null;
    const shelfStats = { walks: 0, walkMsTotal: 0, observerCallbacks: 0 };

    function shelfNow() {
      const perf = w.performance;
      return perf && typeof perf.now === 'function' ? perf.now() : Date.now();
    }
    function libraryObserverRoot() {
      for (const id of LIBRARY_ROOT_IDS) {
        const node = document.getElementById ? document.getElementById(id) : null;
        if (node) return node;
      }
      try {
        return document.querySelector ? document.querySelector(LIBRARY_ROOT_HINT) : null;
      } catch (_) {
        return null;
      }
    }
    // Luna's own UI (the menu, its button, the Lumen overlay) never contains a
    // Steam shelf, so mutations inside it are never worth a walk.
    function lunaOwned(node) {
      let current = node;
      for (let depth = 0; current && depth < 64; depth += 1) {
        const id = typeof current.id === 'string' ? current.id : '';
        if (id.indexOf('sls-luna') === 0 || id.indexOf('lumen') === 0) return true;
        current = current.parentNode || null;
      }
      return false;
    }
    function fiberOfNode(node) {
      if (!node || typeof node !== 'object' || node.nodeType === 3) return null;
      const key = fiberKey(node);
      if (!key) return null;
      const fiber = node[key];
      return fiber && typeof fiber === 'object' ? fiber : null;
    }
    function shelfEpochCurrent() {
      return shelfEpoch.data === hltb && shelfEpoch.order === hltbOrder();
    }
    function resetShelfWalk() {
      shelfSeen = new WeakSet();
      shelfEpoch = { data: hltb, order: hltbOrder() };
    }
    // Cheap "did anything change" check for the fallback pass: direction, route
    // and a bounded node count of the container. No fiber access.
    function shelfSignature() {
      const root = shelfObserverRoot && shelfObserverRoot.isConnected !== false
        ? shelfObserverRoot : libraryObserverRoot();
      let count = 0;
      if (root) {
        const stack = [root];
        while (stack.length && count < 4000) {
          const node = stack.pop();
          count += 1;
          try {
            const kids = node.children;
            if (kids) for (let index = 0; index < kids.length; index += 1) {
              if (kids[index]) stack.push(kids[index]);
            }
          } catch (_) { /* a live collection that changed under us */ }
        }
      }
      return String(currentRoute(w) || '') + '|' + hltbOrder() + '|' + count;
    }
    function visitShelfFiber(node, data, order) {
      const instance = node.stateNode;
      const props = node.memoizedProps;
      if (!instance || !props || !props.showcase) return;
      if (props.showcase.eSortBy !== 12) {
        if (instance.__slsHltbShelf && instance.__slsOrigSort) {
          try { instance.m_fnSort = instance.__slsOrigSort; } catch (_) {}
          instance.__slsHltbShelf = false;
          instance.__slsHltbDrawn = null;
        }
        return;
      }
      const patched = patchShelfInstance(instance);
      if (!patched && !instance.__slsHltbShelf) return;
      const drawn = instance.__slsHltbDrawn;
      if (!patched && drawn && drawn.data === data && drawn.order === order) return;
      if (typeof instance.forceUpdate !== 'function') return;
      try {
        instance.forceUpdate();
        instance.__slsHltbDrawn = { data, order };
        // Those re-renders come back as mutations; they need no second pass.
        shelfQuietUntil = shelfNow() + SHELF_QUIET_MS;
      } catch (_) {}
    }
    function scheduleShelfPump() {
      if (shelfScheduled || !shelfQueue.length) return;
      shelfScheduled = true;
      if (typeof w.requestIdleCallback === 'function') {
        try {
          shelfIdleHandle = w.requestIdleCallback(pumpShelfWalk, { timeout: 400 });
          return;
        } catch (_) { /* fall through to a plain timeout */ }
      }
      shelfWalkTimer = (w.setTimeout || setTimeout)(pumpShelfWalk, 16);
    }
    // One idle slice: <= 8 ms of fiber visiting, then re-arm. The pass counter
    // and its wall time are what `luna selftest` reads.
    function pumpShelfWalk() {
      shelfScheduled = false;
      shelfWalkTimer = null;
      shelfIdleHandle = null;
      if (tornDown === true) { shelfQueue = []; return; }
      if (!shelfQueue.length) return;
      if (!shelfPassRunning) {
        shelfPassRunning = true;
        shelfPassStart = shelfNow();
        shelfStats.walks += 1;
      }
      const started = shelfNow();
      const data = hltb;
      const order = hltbOrder();
      let visited = 0;
      while (shelfQueue.length) {
        const node = shelfQueue.pop();
        if (!node) continue;
        // A fiber already visited still has unseen descendants (a shelf can be
        // added under an old container), so its children are queued anyway.
        if (shelfSeen.has(node)) {
          try { if (node.sibling) shelfQueue.push(node.sibling); } catch (_) {}
          try { if (node.child) shelfQueue.push(node.child); } catch (_) {}
          continue;
        }
        shelfSeen.add(node);
        try { if (node.sibling) shelfQueue.push(node.sibling); } catch (_) {}
        try { if (node.child) shelfQueue.push(node.child); } catch (_) {}
        visitShelfFiber(node, data, order);
        visited += 1;
        if (visited >= 32 && shelfNow() - started >= SHELF_SLICE_MS) break;
      }
      if (shelfQueue.length) { scheduleShelfPump(); return; }
      shelfPassRunning = false;
      shelfStats.walkMsTotal += shelfNow() - shelfPassStart;
      shelfSig = shelfSignature();
    }
    // Roots are fibers from nodes Steam just added; without them the whole tree
    // from the library root is visited (a new cache object or direction always
    // takes this path, since earlier passes drew with the old one).
    function requestShelfWalk(roots) {
      if (tornDown === true) return;
      if (!shelfEpochCurrent()) { resetShelfWalk(); roots = null; }
      const list = roots && roots.length ? roots : [libraryRootFiber()];
      for (const fiber of list) if (fiber) shelfQueue.push(fiber);
      if (shelfQueue.length > SHELF_QUEUE_CAP) {
        shelfQueue = shelfQueue.slice(-SHELF_QUEUE_CAP);
        shelfSeen = new WeakSet();
      }
      scheduleShelfPump();
    }
    function refreshLibraryShelves() {
      requestShelfWalk(null);
    }
    function scheduleShelfRefresh(delay) {
      if (shelfTimer) return;
      shelfTimer = (w.setTimeout || setTimeout)(() => {
        shelfTimer = null;
        if (tornDown === true) { shelfPendingRoots.length = 0; return; }
        // A hidden window has nothing to show; the fallback pass catches up.
        if (document.hidden === true) { shelfPendingRoots.length = 0; return; }
        requestShelfWalk(shelfPendingRoots.splice(0));
      }, delay == null ? 0 : delay);
    }
    /* Mutation filter: only element nodes Steam added outside Luna's UI can
       bring new shelves, and only their fibers are queued. A thousand unrelated
       records therefore cost one loop and no walk. */
    function onLibraryMutations(records) {
      shelfStats.observerCallbacks += 1;
      if (tornDown === true || document.hidden === true) return;
      if (shelfNow() < shelfQuietUntil) return;
      let relevant = false;
      for (let index = 0; index < records.length; index += 1) {
        const record = records[index];
        if (!record || record.type !== 'childList') continue;
        const added = record.addedNodes;
        if (!added || !added.length) continue;
        if (lunaOwned(record.target)) continue;
        for (let child = 0; child < added.length; child += 1) {
          const node = added[child];
          if (!node || node.nodeType === 3) continue;
          if (lunaOwned(node)) continue;
          const fiber = fiberOfNode(node);
          if (!fiber) continue;
          if (shelfPendingRoots.length < SHELF_ROOT_CAP) shelfPendingRoots.push(fiber);
          relevant = true;
        }
        if (shelfPendingRoots.length >= SHELF_ROOT_CAP) break;
      }
      if (!relevant) return;
      scheduleShelfRefresh(SHELF_DEBOUNCE_MS);
    }
    /* Binds the observer to the library container that is mounted now. Steam
       fires no route event in this document, so the probe below calls this on
       arrival and departure of the library view: nothing observes the shell
       while another page is up. */
    function syncShelfObserver() {
      if (tornDown === true) return;
      const root = libraryObserverRoot();
      if (root === shelfObserverRoot) return;
      if (shelfObserver) {
        try { shelfObserver.disconnect(); } catch (_) { /* already dropped */ }
        shelfObserver = null;
      }
      shelfObserverRoot = root;
      resetShelfWalk();
      if (!root || typeof w.MutationObserver !== 'function') return;
      try {
        shelfObserver = new w.MutationObserver(onLibraryMutations);
        shelfObserver.observe(root, { childList: true, subtree: true });
        life.watch(shelfObserver);
      } catch (_) {
        shelfObserver = null;
        return;
      }
      // The library just mounted, so its shelves are new: visit them once.
      scheduleShelfRefresh(0);
    }

    async function forgetHltb(row) {
      hltbNotice = null;
      try {
        await rpc(w, 'SlsHltbForget', { appid: Number(row.id) });
        forgetHltbRev();
        hltbEdit = null;
        await refreshHltb();
      } catch (error) {
        hltbNotice = String(error && error.message ? error.message : error);
        repaintIfOpen();
      }
    }

    // The backend answers 'pending' while HowLongToBeat is still responding:
    // its requests never block Steam's UI thread, so ask again until the
    // answer is in (the backend's own request timeouts bound the wait).
    async function fetchHltbRpc(row) {
      for (let attempt = 0; ; attempt += 1) {
        try {
          const out = await rpc(w, 'SlsHltbFetch', { appid: Number(row.id), name: row.name });
          forgetHltbRev();
          return out;
        } catch (error) {
          if (!error || error.code !== 'pending' || attempt >= 300) throw error;
          await sleep(250);
        }
      }
    }

    async function fetchHltbOne(row) {
      hltbNotice = null;
      try {
        const out = await fetchHltbRpc(row);
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
          const out = await fetchHltbRpc(row);
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
          const fresh = await readHltb().catch(() => null);
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
      const library = asList(hltb && hltb.library);
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
            forgetHltbRev();
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
      const library = asList(hltb && hltb.library);
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
          line.innerHTML = '<strong>Stopped:</strong> ' + escapeHtml(hltbQueue.error);
        } else if (hltbQueue.wait) {
          line.innerHTML = '<strong>Rate-limited.</strong> Waiting ' + (Number(hltbQueue.wait) || 0)
            + 's before retrying \u201c' + escapeHtml(hltbQueue.label) + '\u201d\u2026';
        } else if (hltbQueue.running) {
          line.innerHTML = '<strong>Fetching ' + (hltbQueue.done + hltbQueue.skipped + 1)
            + ' of ' + hltbQueue.total + '</strong> \u00b7 ' + escapeHtml(hltbQueue.label) + '\u2026'
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
        sgdbReadError = '';
      } catch (error) {
        sgdb = sgdb || {};
        sgdbReadError = String(error && error.message ? error.message : error);
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
        sgdbNotice = 'Could not save the key: '
          + String(error && error.message ? error.message : error);
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
        sgdbNotice = 'Key test failed: '
          + String(error && error.message ? error.message : error);
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
        sgdbNotice = 'Search failed: ' + sgdbErrorText(error);
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
        sgdbNotice = 'Could not load artwork: ' + sgdbErrorText(error);
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
        sgdbChangedAt = Object.assign({}, sgdbChangedAt, { [sgdbKind]: Date.now() });
        // The kind decides whether Steam can repaint over the running client;
        // an icon never can, so say plainly that a restart is needed.
        const liveKinds = sgdbLive(sgdbKind) && (!out || out.live !== false) ? [sgdbKind] : [];
        if (!liveKinds.length) sgdbNotice += ' Restart Steam to see this ' + sgdbKind + '.';
        const at = liveKinds.length ? requestLiveArtwork(sgdbGame.appid, liveKinds) : null;
        sgdbBusy = false;
        await refreshSgdbHistory();
        repaintIfOpen();
        if (at) finishLiveNotice(at, 'Applied and refreshed in Steam right away.');
      } catch (error) {
        sgdbNotice = 'Could not apply artwork: ' + sgdbErrorText(error);
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

    /* The auto pass is a backend job now: SlsSgdbAuto only creates it, and
       each SlsSgdbAutoStatus call advances it by one bounded request. Follow
       it while this tab is visible; stop on a tab switch, on close, on
       teardown and at the job's own 180 s deadline. */
    const SGDB_AUTO_POLL = 700;
    const SGDB_AUTO_TTL = 180000;

    function stopSgdbAuto() {
      if (sgdbAutoTimer === null) return;
      const id = sgdbAutoTimer;
      sgdbAutoTimer = null;
      try { (w.clearTimeout || clearTimeout)(id); } catch (_) { /* already fired */ }
    }

    function normalizeSgdbAuto(out) {
      const job = (out && out.job) || {};
      return {
        id: job.id != null ? job.id : (sgdbAuto && sgdbAuto.id),
        state: String((out && out.state) || job.state || 'running'),
        done: Number(job.done) || 0,
        total: Number(job.total) || 0,
        results: Array.isArray(out && out.results) ? out.results : [],
        startedAt: job.startedAt || (sgdbAuto && sgdbAuto.startedAt) || 0,
        begunAt: (sgdbAuto && sgdbAuto.begunAt) || Date.now(),
      };
    }

    function sgdbAutoResults() {
      const list = asList(sgdbAuto && sgdbAuto.results);
      return { settled: list.filter(entry => entry && !entry.pending),
        pending: list.filter(entry => entry && entry.pending) };
    }

    function finishSgdbAuto() {
      const { settled } = sgdbAutoResults();
      const ok = settled.filter(entry => entry.ok);
      const failed = settled.filter(entry => !entry.ok);
      sgdbBusy = false;
      sgdbNotice = 'Applied the best match to ' + ok.length + ' of ' + settled.length + ' slots'
        + (failed.length ? ' (' + failed.map(entry => entry.kind).join(', ')
          + ' had nothing usable)' : '') + '.';
      if (sgdbGame) {
        const liveKinds = ok.filter(entry => entry.live).map(entry => entry.kind);
        const at = liveKinds.length ? requestLiveArtwork(sgdbGame.appid, liveKinds) : null;
        ok.forEach(entry => {
          sgdbChangedAt = Object.assign({}, sgdbChangedAt, { [entry.kind]: Date.now() });
        });
        refreshSgdbHistory().then(() => { if (current()) repaintIfOpen(); });
        repaintIfOpen();
        if (at) finishLiveNotice(at, 'Steam refreshed every slot live.');
        return;
      }
      repaintIfOpen();
    }

    function scheduleSgdbAuto(step) {
      stopSgdbAuto();
      const schedule = w.setTimeout || setTimeout;
      sgdbAutoTimer = schedule(() => { sgdbAutoTimer = null; step(); }, SGDB_AUTO_POLL);
    }

    function pollSgdbAuto() {
      if (!sgdbAuto || sgdbAuto.state !== 'running') return;
      if (tab !== 'steamgrid' || !document.getElementById(OVERLAY_ID)) { stopSgdbAuto(); return; }
      if (Date.now() - (sgdbAuto.begunAt || Date.now()) > SGDB_AUTO_TTL) {
        stopSgdbAuto();
        sgdbAuto.state = 'timeout';
        sgdbBusy = false;
        sgdbNotice = 'The artwork pass stopped after 180 s; nothing else was changed.';
        addProblem('SteamGridDB', sgdbNotice);
        repaintIfOpen();
        return;
      }
      scheduleSgdbAuto(async () => {
        if (!sgdbAuto || sgdbAuto.state !== 'running') return;
        if (tab !== 'steamgrid' || !document.getElementById(OVERLAY_ID)) { stopSgdbAuto(); return; }
        try {
          const out = await rpc(w, 'SlsSgdbAutoStatus',
            { raw: JSON.stringify({ id: sgdbAuto.id }) });
          sgdbAuto = normalizeSgdbAuto(out);
          repaintIfOpen();
          if (sgdbAuto.state === 'running') pollSgdbAuto();
          else finishSgdbAuto();
        } catch (error) {
          const message = String(error && error.message ? error.message : error);
          sgdbNotice = 'Artwork pass failed: ' + message;
          sgdbBusy = false;
          if (sgdbAuto) sgdbAuto.state = 'failed';
          stopSgdbAuto();
          repaintIfOpen();
        }
      });
    }

    async function autoApplySgdb() {
      if (!sgdbGame || !sgdbGame.sgdbId) return;
      stopSgdbAuto();
      sgdbNotice = null;
      sgdbBusy = true;
      sgdbAuto = { id: null, state: 'starting', done: 0, total: 0, results: [], begunAt: Date.now() };
      repaintIfOpen();
      try {
        const out = await rpc(w, 'SlsSgdbAuto', {
          raw: JSON.stringify({ appid: Number(sgdbGame.appid), gameId: sgdbGame.sgdbId }),
        });
        if (!out || out.success === false) {
          throw Error((out && out.error) || 'The artwork pass did not start.');
        }
        sgdbAuto = normalizeSgdbAuto(out);
        sgdbBusy = sgdbAuto.state === 'running';
        repaintIfOpen();
        if (sgdbAuto.state === 'running') pollSgdbAuto();
        else finishSgdbAuto();
      } catch (error) {
        sgdbNotice = 'Artwork pass failed: '
          + String(error && error.message ? error.message : error);
        addProblem('SteamGridDB', sgdbNotice);
        sgdbAuto = null;
        sgdbBusy = false;
        stopSgdbAuto();
        repaintIfOpen();
      }
    }

    /* Undo and reset replace or remove a file, so both are armed with a second
       click in the page (native confirm() freezes the Steam shell, and a
       mis-click must not cost the user their artwork). The arm expires after
       4 s: a stale "Confirm undo" is never a hidden loaded gun. */
    const SGDB_CONFIRM_MS = 4000;
    const SGDB_KINDS = [['grid', 'Grid'], ['wide', 'Wide'], ['hero', 'Hero'], ['logo', 'Logo'], ['icon', 'Icon']];
    // Mirrors the backend's LIVE_TYPES: the four slots Steam can repaint over
    // its running client. Icons are file-only, so an applied icon needs a
    // restart before Steam shows it.
    const SGDB_LIVE_TYPES = ['grid', 'wide', 'hero', 'logo'];
    const sgdbLive = kind => SGDB_LIVE_TYPES.indexOf(String(kind)) >= 0;
    // SteamGridDB answers 429 with a wait; that is worth a sentence of its own
    // instead of the raw backend string.
    const sgdbErrorText = error => {
      const wait = Number(error && error.retryAfter);
      if (error && error.code === 'rate_limit' && Number.isFinite(wait) && wait > 0) {
        return 'SteamGridDB is rate-limiting; try again in ' + Math.round(wait) + ' s';
      }
      return String(error && error.message ? error.message : error);
    };

    function forgetSgdbHistory() {
      sgdbHistory = null;
      sgdbHistoryFor = '';
      sgdbHistoryError = '';
      sgdbHistoryBusy = false;
      sgdbUndoArmed = null;
      sgdbResetArmed = 0;
      sgdbChangedAt = {};
      sgdbTimes = {};
    }

    // {success:true, history:{grid:N, hero:N, ...}} or a structured refusal.
    // One read per game, never while the menu is closed.
    async function refreshSgdbHistory() {
      if (!sgdbGame) { sgdbHistory = null; sgdbHistoryFor = ''; return; }
      if (document.hidden === true) return;
      const appid = Number(sgdbGame.appid);
      if (!Number.isFinite(appid) || appid <= 0) { sgdbHistory = null; return; }
      sgdbHistoryFor = String(appid);
      sgdbHistoryBusy = true;
      try {
        const out = await rpc(w, 'SlsSgdbHistory', { raw: JSON.stringify({ appid: appid }) });
        if (!sgdbGame || Number(sgdbGame.appid) !== appid) return;
        sgdbHistory = out && out.history && typeof out.history === 'object' ? out.history : {};
        sgdbTimes = out && out.times && typeof out.times === 'object' ? out.times : {};
        sgdbHistoryError = '';
      } catch (error) {
        if (!sgdbGame || Number(sgdbGame.appid) !== appid) return;
        sgdbHistoryError = String(error && error.message ? error.message : error);
      }
      sgdbHistoryBusy = false;
    }

    const sgdbDepth = kind => {
      const value = Number(sgdbHistory && sgdbHistory[kind]);
      return Number.isFinite(value) && value > 0 ? Math.floor(value) : 0;
    };

    // "3m ago" for a slot this session changed; nothing is invented for a
    // change made in an earlier session or by another tool.
    const sgdbAgo = kind => {
      const at = Number(sgdbChangedAt[kind]);
      if (at > 0) return ' \u00b7 ' + age(at / 1000) + ' ago';
      // Nothing is invented: without a session change or a backend `times`
      // stamp the row simply shows its depth.
      const stamped = Number(sgdbTimes && sgdbTimes[kind]);
      return Number.isFinite(stamped) && stamped > 0 ? ' \u00b7 changed ' + age(stamped) + ' ago' : '';
    };

    function armSgdbUndo(kind) {
      const slot = kind || sgdbKind;
      const now = Date.now();
      if (sgdbUndoArmed && sgdbUndoArmed.kind === slot
        && now - sgdbUndoArmed.at <= SGDB_CONFIRM_MS) {
        undoSgdbAsset(slot);
        return;
      }
      sgdbUndoArmed = { kind: slot, at: now };
      sgdbResetArmed = 0;
      repaintIfOpen();
    }

    function armSgdbReset() {
      const now = Date.now();
      if (sgdbResetArmed && now - sgdbResetArmed <= SGDB_CONFIRM_MS) {
        resetSgdbAsset();
        return;
      }
      sgdbResetArmed = now;
      sgdbUndoArmed = null;
      repaintIfOpen();
    }

    async function undoSgdbAsset(kind) {
      if (!sgdbGame) return;
      const slot = kind || sgdbKind;
      sgdbNotice = null;
      sgdbUndoArmed = null;
      sgdbResetArmed = 0;
      sgdbBusy = true;
      repaintIfOpen();
      const appid = Number(sgdbGame.appid);
      try {
        const out = await rpc(w, 'SlsSgdbUndo', {
          raw: JSON.stringify({ appid: appid, kind: slot }),
        });
        sgdbChangedAt = Object.assign({}, sgdbChangedAt, { [slot]: Date.now() });
        sgdbNotice = 'Undone: the previous artwork is back'
          + (out && typeof out.history === 'number' && out.history > 0
            ? ' (' + out.history + ' step' + (out.history === 1 ? '' : 's') + ' left)' : '')
          + '.';
        sgdbBusy = false;
        await refreshSgdbHistory();
        const at = requestLiveArtwork(appid, [slot]);
        repaintIfOpen();
        if (at) finishLiveNotice(at, '');
      } catch (error) {
        sgdbNotice = 'Could not undo: '
          + String(error && error.message ? error.message : error);
        sgdbBusy = false;
        repaintIfOpen();
      }
    }

    async function resetSgdbAsset() {
      if (!sgdbGame) return;
      sgdbNotice = null;
      sgdbResetArmed = 0;
      sgdbUndoArmed = null;
      sgdbBusy = true;
      repaintIfOpen();
      try {
        const out = await rpc(w, 'SlsSgdbReset', {
          raw: JSON.stringify({ appid: Number(sgdbGame.appid), kind: sgdbKind }),
        });
        sgdbNotice = out && out.removed
          ? 'Custom artwork removed; Steam shows its own again.'
          : 'Nothing custom in this slot; Steam already shows its own.';
        sgdbBusy = false;
        await refreshSgdbHistory();
      } catch (error) {
        sgdbNotice = 'Could not remove the custom artwork: '
          + String(error && error.message ? error.message : error);
        sgdbBusy = false;
      }
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
      forgetSgdbHistory();
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
        where.innerHTML = 'Writing to <strong>' + escapeHtml(sgdb.gridDir) + '</strong>';
        body.appendChild(where);
      } else if (hasKey && sgdb && sgdb.gridError) {
        const broken = document.createElement('p');
        broken.className = 'sls-luna-error';
        broken.textContent = 'Steam artwork folder unavailable: ' + sgdb.gridError;
        body.appendChild(broken);
      }

      if (sgdbNotice) {
        const notice = document.createElement('p');
        notice.className = /(fail|reject|unavailable|error|could not)/i.test(sgdbNotice)
          ? 'sls-luna-error' : 'sls-luna-note';
        notice.textContent = sgdbNotice;
        body.appendChild(notice);
      }
      if (sgdbReadError) {
        const readError = document.createElement('p');
        readError.className = 'sls-luna-error';
        readError.textContent = 'Could not read SteamGridDB settings: ' + sgdbReadError;
        body.appendChild(readError);
      }
      if (sgdbBusy) {
        const busy = document.createElement('p');
        busy.className = 'sls-luna-note';
        busy.textContent = 'Working\u2026';
        body.appendChild(busy);
      }

      // The pass now runs as a job: counts and the slot in flight while it
      // runs, then one row per slot with what really happened.
      if (sgdbAuto) {
        const { settled, pending } = sgdbAutoResults();
        const running = sgdbAuto.state === 'running' || sgdbAuto.state === 'starting';
        const total = Number(sgdbAuto.total) || 0;
        const done = Number(sgdbAuto.done) || 0;
        const progress = document.createElement('p');
        progress.className = 'sls-luna-note';
        progress.textContent = running
          ? 'SteamGridDB: ' + done + ' of ' + (total || '?') + ' slot(s)'
            + (pending.length ? ' \u00b7 working on ' + pending[0].kind : '') + '\u2026'
          : 'SteamGridDB artwork pass: ' + String(sgdbAuto.state || 'done') + '.';
        body.appendChild(progress);
        if (running && total) body.appendChild(progressBar((done / total) * 100, true));
        if (settled.length) {
          const list = document.createElement('div');
          settled.forEach(entry => {
            const row = document.createElement('p');
            const failed = !entry.ok;
            row.className = failed ? 'sls-luna-error' : 'sls-luna-note';
            row.textContent = String(entry.kind || '?') + ': ' + (failed
              ? (entry.code ? entry.code + ' \u2014 ' : '') + (entry.error || 'nothing usable')
              : 'applied' + (entry.live ? ' (live)' : ''));
            list.appendChild(row);
          });
          body.appendChild(list);
        }
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
      const esc = escapeHtml;
      head.innerHTML = 'Artwork for <strong>' + esc(sgdbGame.name) + '</strong> (#' + sgdbGame.appid + ')'
        + (sgdbGame.sgdbName && sgdbGame.sgdbName !== sgdbGame.name
          ? ' \u00b7 matched as &quot;' + esc(sgdbGame.sgdbName) + '&quot;' : '')
        + (sgdbGame.sgdbId ? '' : ' \u00b7 <span style="color:#8d97a1">searching\u2026</span>');
      body.appendChild(head);

      // One history read per game, only on this page and only while it is on
      // screen. Nothing here undoes anything by itself: the list only offers.
      if (sgdbHistoryFor !== String(sgdbGame.appid) && !sgdbHistoryBusy) {
        refreshSgdbHistory().then(() => { if (current()) repaintIfOpen(); });
      }

      // Recent changes, straight from the backend's undo history: one row per
      // slot that still has a step to go back to, each with its own Undo.
      const history = document.createElement('div');
      history.className = 'sls-luna-history';
      const historyHead = document.createElement('p');
      historyHead.className = 'sls-luna-note';
      historyHead.innerHTML = '<strong>Recent changes</strong> for ' + esc(sgdbGame.name);
      history.appendChild(historyHead);
      const steps = SGDB_KINDS.filter(pair => sgdbDepth(pair[0]) > 0);
      if (sgdbHistoryError) {
        const bad = document.createElement('p');
        bad.className = 'sls-luna-error';
        bad.textContent = 'Could not read the artwork history: ' + sgdbHistoryError;
        history.appendChild(bad);
      } else if (!steps.length && sgdbHistory) {
        const empty = document.createElement('p');
        empty.className = 'sls-luna-note';
        empty.textContent = 'No artwork changes yet.';
        history.appendChild(empty);
      }
      steps.forEach(pair => {
        const kind = pair[0];
        const depth = sgdbDepth(kind);
        const row = document.createElement('div');
        row.className = 'sls-luna-history-row';
        row.style.cssText = 'display:flex;align-items:center;gap:8px;justify-content:space-between;';
        const text = document.createElement('span');
        text.textContent = pair[1] + ' \u00b7 ' + depth + ' undo step' + (depth === 1 ? '' : 's')
          + sgdbAgo(kind);
        const armed = Boolean(sgdbUndoArmed && sgdbUndoArmed.kind === kind
          && Date.now() - sgdbUndoArmed.at <= SGDB_CONFIRM_MS);
        const button = document.createElement('button');
        button.className = 'sls-luna-btn secondary';
        button.textContent = armed ? 'Confirm undo' : 'Undo';
        button.title = 'Undo the last ' + kind + ' change for this game';
        button.disabled = sgdbBusy;
        button.onclick = () => armSgdbUndo(kind);
        row.append(text, button);
        history.appendChild(row);
      });
      const historyActions = document.createElement('div');
      historyActions.style.cssText = 'display:flex;gap:8px;flex-wrap:wrap;margin:8px 0 0;';
      const undoLast = document.createElement('button');
      undoLast.className = 'sls-luna-btn secondary';
      undoLast.textContent = 'Undo last change';
      undoLast.title = 'Step back the most recent artwork change for this game';
      undoLast.disabled = sgdbBusy || !steps.length;
      undoLast.onclick = () => {
        // Newest first by when this session changed it, then by kind order.
        const sorted = steps.slice().sort((a, b) =>
          (Number(sgdbChangedAt[b[0]]) || 0) - (Number(sgdbChangedAt[a[0]]) || 0));
        if (sorted.length) armSgdbUndo(sorted[0][0]);
      };
      const resetGame = document.createElement('button');
      resetGame.className = 'sls-luna-btn secondary';
      resetGame.textContent = sgdbResetArmed ? 'Confirm reset' : 'Reset this game\u2019s artwork';
      resetGame.title = 'Remove the custom ' + sgdbKind
        + ' file so Steam shows its own artwork again (the history is kept)';
      resetGame.disabled = sgdbBusy;
      resetGame.onclick = () => armSgdbReset();
      historyActions.append(undoLast, resetGame);
      history.appendChild(historyActions);
      body.appendChild(history);

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
      const close = document.createElement('button');
      close.className = 'sls-luna-btn secondary';
      close.textContent = 'Close picker';
      close.onclick = () => {
        stopSgdbAuto();
        sgdbAuto = null;
        sgdbGame = null; sgdbAssets = null; sgdbResults = null; sgdbNotice = null;
        forgetSgdbHistory();
        repaintIfOpen();
      };
      actions.append(auto, close);
      body.appendChild(actions);
      return body;
    }

    // ---- Graphics page (beta): read-only detection -------------------------

    // "Checked 4 s ago." from the report's `checkedAt` (or the receipt time on
    // an older backend). Spaces keep the unit readable at a glance.
    function graphicsAge() {
      if (!graphicsCheckedAt) return 'Not checked yet.';
      return 'Checked ' + age(graphicsCheckedAt).replace(/(\d)([smh])$/, '$1 $2') + ' ago.';
    }

    function graphicsBody() {
      const body = document.createElement('div');
      const report = graphics || {};
      const system = report.system || {};
      const lsfg = report.lsfg || {};
      const layers = Array.isArray(lsfg.layers) ? lsfg.layers : [];
      const gpus = Array.isArray(report.gpus) ? report.gpus : [];
      const opti = Array.isArray(report.optiscaler) ? report.optiscaler : [];
      const steamInfo = report.steam || {};
      const gpuSummary = report.gpu && typeof report.gpu === 'object' ? report.gpu : null;

      const note = document.createElement('p');
      note.className = 'sls-luna-note';
      note.innerHTML = 'Luna only <strong>detects</strong> what this machine has for frame generation and '
        + 'upscaling. Nothing here installs, downloads or changes files - the installers come later, '
        + 'each behind its own gates.';
      body.appendChild(note);
      // The report's age, from its own `checkedAt`. Only the tick below touches
      // this node; a page rebuild makes a fresh one.
      const ageLine = document.createElement('p');
      ageLine.className = 'sls-luna-note sls-luna-graphics-age';
      ageLine.textContent = graphicsAge();
      graphicsAgeNode = ageLine;
      body.appendChild(ageLine);

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
        // "in use" and "primary" come from the backend fields; an older report
        // has neither and the line stays exactly as it was.
        const marks = [];
        if (gpu.in_use === true || (gpuSummary && gpuSummary.in_use && gpuSummary.in_use === gpu.card)) {
          marks.push('in use');
        }
        if (gpu.boot_vga === true
          || (gpuSummary && gpuSummary.primary && gpuSummary.primary === gpu.card)) {
          marks.push('primary');
        }
        line(gpu.card + ': ' + (gpu.product || gpu.vendor_name) + ' \u00b7 driver ' + (gpu.driver || '?')
          + ' (' + gpu.vendor + ':' + (gpu.device || '?') + ')'
          + (marks.length ? ' \u00b7 ' + marks.join(', ') : ''));
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
    /* Health page: local diagnostics only. Paths, versions, marker counts and
       file sizes, so a problem can be described without a shell. */
    const HEALTH_TEXT = {
      en: {
        intro: 'Local diagnostics only: paths, versions, marker counts and file sizes. '
          + 'Nothing leaves this machine - use <strong>Copy diagnostics</strong> when reporting a problem.',
        empty: 'No health report yet - the next Refresh loads it.',
        needsAttention: n => 'Needs attention (' + n + ')',
        statusOk: 'Status',
        allGood: 'Everything looks good.',
        addons: 'Add-ons',
        paths: 'Paths',
        settings: 'Settings',
        dataFiles: 'Data files',
        updates: 'Updates',
        steamClient: 'Steam client (update only from here)',
        steamUpdates: 'Steam updates',
        steamUpdNormal: 'Normal: Steam updates itself',
        steamUpdSoft: 'Blocked by a pin (soft): this build is pinned and the bootstrapper is told not to update',
        steamUpdHard: 'Hard-locked on the ubuntu12 rollback: updates blocked, package files read-only',
        steamUpdBuild: 'Pinned build',
        steamUpdJob: 'A lock/unlock job is running in the background; Steam will close and reopen by itself.',
        blockUpdates: 'Block updates (roll back)',
        unblockUpdates: 'Unblock & update Steam',
        blockConfirm: 'This rolls Steam back to the ubuntu12 client and locks it there. Steam will close now '
          + '(anything unsaved is lost), about 500 MB is downloaded (it can take several minutes), and Steam '
          + 'reopens by itself when it is done. You will get a desktop notification with the result.',
        unblockConfirm: 'This removes the lock and the pin. Steam will close and restart, and it updates itself '
          + 'to the latest client on that start.',
        confirmBlock: 'Yes, close Steam and block updates',
        confirmUnblock: 'Yes, restart Steam and update',
        cancel: 'Cancel',
        steamJobStarted: 'Started. Steam will close and reopen by itself; progress is in the Luna steam job log.',
        hostTitle: 'Plugin host',
        hostRunning: 'Running on',
        hostPending: 'switches after a Steam restart (luna host cutover / rollback)',
        gradientTitle: 'Steam gradient',
        gradientLabel: 'Desktop-colour gradient',
        gradientSub: 'Paints a soft gradient behind Steam from your Noctalia palette and follows it when the '
          + 'desktop theme changes. Needs the Noctalia Steam template.',
        issueUpdateAvailable: (latest, installed) => 'Luna update available: ' + latest + ' (you have '
          + installed + '). Press Install update below, then restart Steam.',
        issueUpdateQueued: tag => 'Luna update queued (' + tag + '): it lands the next time Steam is closed.',
        issueSteamDrifted: (build, pinned) => 'Steam updated itself past the pin (' + build + ' vs pinned '
          + pinned + '). Close Steam and run: luna steam recover',
        issuePacman: 'Steam comes from pacman and is NOT ignored: a system update can replace the '
          + 'launcher. Add "IgnorePkg = steam" under [options] in /etc/pacman.conf (needs sudo).',
        issueUpdatesAllowed: 'A Steam build is pinned but updates are NOT blocked. Close Steam and run: '
          + 'luna steam recover (it re-applies steam.cfg).',
        issueConflicts: n => 'Steam sync conflicts: ' + n + ' localconfig cop'
          + (Number(n) === 1 ? 'y' : 'ies')
          + ' waiting - close Steam on both machines, let Syncthing settle, then run: luna reconcile',
        issueBridgeMissing: 'Add-ons are installed but boot.lua is not patched (a base update wiped the bridge). '
          + 'Close Steam and press Repair bridge below, or run: python3 scripts/luna.py install --keep-steam-client',
        issueRefreshQueued: 'Steam+Luna refresh queued: Steam updates, pins and caches, then Luna reinstalls - '
          + 'applies the next time Steam is closed (timer).',
        issueDrift: (first, more) => 'Installed files differ from the pinned bundle: ' + first
          + (more ? ' (+' + more + ' more)' : '') + '. Run: python3 scripts/luna.py verify',
        issueNoTimer: 'Unattended updates are on but this machine has no timer. Pick the mode again to enable it.',
        checkNow: 'Check now',
        checking: 'Checking\u2026',
        installUpdate: 'Install update',
        queued: 'Queued',
        cancelQueued: 'Cancel queued',
        repairBridge: 'Repair bridge',
        repairTitle: 'Reinstalls the Lumen bridge when a base update wiped it (Steam must be closed)',
        pinBuild: 'Pin current build',
        updateClient: 'Update Steam client',
        cacheNow: 'Cache client now',
        cacheTitle: 'Queues a package snapshot for the next closed-Steam window',
        cancelSteamTask: 'Cancel Steam task',
        refreshBoth: 'Refresh Steam + Luna',
        refreshTitle: 'Update Steam to Valve\u2019s latest, pin and cache it, then reinstall Luna - applies when Steam is closed',
        cancelRefresh: 'Cancel refresh',
        copyDiagnostics: 'Copy diagnostics',
        applyNeedsClosed: 'Applying an update needs Steam closed, so a queued one waits for the next exit - '
          + 'or turn on unattended updates and it applies by itself.',
        noTimerYet: 'An update is queued but this machine has no timer worker yet: '
          + 'it lands the next time the timer runs, so keep this page open or pick a mode to enable it.',
        timerReady: 'This machine is set up: the timer checks every 30 minutes and applies while Steam is closed.',
        timerMissing: 'This machine is not set up yet: pick the mode again to enable its timer.',
        notPinned: 'not pinned',
        updatesBlocked: 'blocked everywhere else',
        updatesAllowed: 'ALLOWED elsewhere - pin to block',
        cacheReady: 'cache ready',
        pinnedOk: 'pinned',
        drifted: 'drifted',
        queuedTask: task => 'Queued Steam task: ' + task
          + ' - applies the next time Steam is closed (timer), then updates are blocked again.',
        queuedRefresh: 'Queued refresh: Steam+Luna - applies the next time Steam is closed (timer).',
        lastNoticed: drift => 'Last unattended run noticed: ' + drift,
        refreshExplainer: 'Refresh is the whole flow in one button: it never downgrades, never touches games or '
          + 'accounts, and refuses while Steam runs instead of closing anything.',
        steamosMode: 'SteamOS Mode',
        steamosDesc: 'Big-Picture shortcuts, one per detected screen (SteamOS (1080p 60), SteamOS (4K 60), ...), '
          + 'with the SDL mouse compatibility this machine uses. Creating them again is safe: unchanged '
          + 'Luna shortcuts are deduplicated, custom launchers are never touched.',
        createShortcuts: 'Create shortcuts',
      },
      es: {
        intro: 'Solo diagnósticos locales: rutas, versiones, recuentos y tamaños. '
          + 'Nada sale de esta máquina; usa <strong>Copiar diagnóstico</strong> para informar de un problema.',
        empty: 'Aún no hay informe de salud; la próxima actualización lo cargará.',
        needsAttention: n => 'Necesita atención (' + n + ')',
        statusOk: 'Estado',
        allGood: 'Todo está bien.',
        addons: 'Complementos',
        paths: 'Rutas',
        settings: 'Ajustes',
        dataFiles: 'Archivos de datos',
        updates: 'Actualizaciones',
        steamClient: 'Cliente Steam (actualizar solo desde aquí)',
        steamUpdates: 'Actualizaciones de Steam',
        steamUpdNormal: 'Normal: Steam se actualiza solo',
        steamUpdSoft: 'Bloqueado por un pin (suave): esta versión está fijada y el bootstrapper no actualiza',
        steamUpdHard: 'Bloqueo duro en el rollback ubuntu12: actualizaciones bloqueadas, archivos de package de solo lectura',
        steamUpdBuild: 'Versión fijada',
        steamUpdJob: 'Hay una tarea de bloqueo/desbloqueo en segundo plano; Steam se cerrará y reabrirá solo.',
        blockUpdates: 'Bloquear actualizaciones (rollback)',
        unblockUpdates: 'Desbloquear y actualizar Steam',
        blockConfirm: 'Esto devuelve Steam al cliente ubuntu12 y lo bloquea ahí. Steam se cerrará ahora '
          + '(se pierde lo no guardado), se descargan unos 500 MB (puede tardar varios minutos) y Steam '
          + 'se reabre solo al terminar. Recibirás una notificación con el resultado.',
        unblockConfirm: 'Esto quita el bloqueo y el pin. Steam se cerrará y reiniciará, y en ese inicio '
          + 'se actualizará al último cliente.',
        confirmBlock: 'Sí, cerrar Steam y bloquear',
        confirmUnblock: 'Sí, reiniciar Steam y actualizar',
        cancel: 'Cancelar',
        steamJobStarted: 'Iniciado. Steam se cerrará y reabrirá solo; el progreso está en el log de la tarea de Luna.',
        hostTitle: 'Host de plugins',
        hostRunning: 'Funcionando con',
        hostPending: 'cambia tras reiniciar Steam (luna host cutover / rollback)',
        gradientTitle: 'Degradado de Steam',
        gradientLabel: 'Degradado con los colores del escritorio',
        gradientSub: 'Pinta un degradado suave detrás de Steam con tu paleta de Noctalia y lo sigue cuando '
          + 'cambia el tema del escritorio. Requiere la plantilla de Steam de Noctalia.',
        issueUpdateAvailable: (latest, installed) => 'Actualización de Luna disponible: ' + latest + ' (tienes '
          + installed + '). Pulsa Instalar actualización abajo y reinicia Steam.',
        issueUpdateQueued: tag => 'Actualización de Luna en cola (' + tag + '): se aplicará con Steam cerrado.',
        issueSteamDrifted: (build, pinned) => 'Steam se actualizó por encima de la versión fijada (' + build
          + ' frente a fijada ' + pinned + '). Cierra Steam y ejecuta: luna steam recover',
        issuePacman: 'Steam viene de pacman y NO está ignorado: una actualización del sistema puede reemplazar el '
          + 'lanzador. Añade "IgnorePkg = steam" bajo [options] en /etc/pacman.conf (necesita sudo).',
        issueUpdatesAllowed: 'Hay una versión de Steam fijada pero las actualizaciones NO están bloqueadas. Cierra Steam y ejecuta: '
          + 'luna steam recover (reaplica steam.cfg).',
        issueConflicts: n => 'Conflictos de sincronización de Steam: ' + n + ' copia(s) de localconfig '
          + 'en espera; cierra Steam en ambas máquinas, deja que Syncthing se sincronice y ejecuta: luna reconcile',
        issueBridgeMissing: 'Los complementos están instalados pero boot.lua no tiene el parche (una actualización de la base borró el puente). '
          + 'Cierra Steam y pulsa Reparar puente abajo, o ejecuta: python3 scripts/luna.py install --keep-steam-client',
        issueRefreshQueued: 'Actualización de Steam+Luna en cola: Steam se actualiza, fija y guarda en caché, y Luna se reinstala; '
          + 'se aplica con Steam cerrado (temporizador).',
        issueDrift: (first, more) => 'Los archivos instalados difieren del paquete fijado: ' + first
          + (more ? ' (+' + more + ' más)' : '') + '. Ejecuta: python3 scripts/luna.py verify',
        issueNoTimer: 'Las actualizaciones desatendidas están activadas pero esta máquina no tiene temporizador. Elige el modo de nuevo para activarlo.',
        checkNow: 'Comprobar ahora',
        checking: 'Comprobando\u2026',
        installUpdate: 'Instalar actualización',
        queued: 'En cola',
        cancelQueued: 'Cancelar cola',
        repairBridge: 'Reparar puente',
        repairTitle: 'Reinstala el puente de Lumen cuando una actualización de la base lo borró (Steam debe estar cerrado)',
        pinBuild: 'Fijar versión actual',
        updateClient: 'Actualizar cliente Steam',
        cacheNow: 'Guardar caché ahora',
        cacheTitle: 'Pone en cola una instantánea de paquetes para la próxima ventana con Steam cerrado',
        cancelSteamTask: 'Cancelar tarea Steam',
        refreshBoth: 'Actualizar Steam + Luna',
        refreshTitle: 'Actualiza Steam a lo último de Valve, lo fija y lo guarda en caché, y reinstala Luna; se aplica con Steam cerrado',
        cancelRefresh: 'Cancelar actualización',
        copyDiagnostics: 'Copiar diagnóstico',
        applyNeedsClosed: 'Aplicar una actualización necesita Steam cerrado: lo encolado espera a la próxima salida, '
          + 'o activa las actualizaciones desatendidas y se aplica solo.',
        noTimerYet: 'Hay una actualización en cola pero esta máquina aún no tiene temporizador: '
          + 'se aplicará cuando se ejecute el temporizador; deja esta página abierta o elige un modo para activarlo.',
        timerReady: 'Esta máquina está lista: el temporizador revisa cada 30 minutos y aplica con Steam cerrado.',
        timerMissing: 'Esta máquina aún no está lista: elige el modo de nuevo para activar su temporizador.',
        notPinned: 'sin fijar',
        updatesBlocked: 'bloqueado en el resto',
        updatesAllowed: 'PERMITIDO en el resto: fija para bloquear',
        cacheReady: 'caché lista',
        pinnedOk: 'fijada',
        drifted: 'cambiada',
        queuedTask: task => 'Tarea Steam en cola: ' + task
          + '; se aplica con Steam cerrado (temporizador) y luego se bloquean las actualizaciones.',
        queuedRefresh: 'Actualización en cola: Steam+Luna; se aplica con Steam cerrado (temporizador).',
        lastNoticed: drift => 'La última ejecución desatendida detectó: ' + drift,
        refreshExplainer: 'Actualizar es todo el flujo en un botón: nunca baja de versión, nunca toca juegos ni '
          + 'cuentas, y se niega con Steam abierto en lugar de cerrar nada.',
        steamosMode: 'SteamOS Mode',
        steamosDesc: 'Accesos directos Big Picture, uno por pantalla detectada (SteamOS (1080p 60), SteamOS (4K 60), ...), '
          + 'con la compatibilidad de ratón SDL que usa esta máquina. Volver a crearlos es seguro: los accesos '
          + 'de Luna sin cambios se deduplican y los personalizados nunca se tocan.',
        createShortcuts: 'Crear accesos directos',
      },
    };
    function healthBody() {
      const body = document.createElement('div');
      const health = lunaHealth || null;
      const healthPrefs = ((health && health.data && health.data.prefs) || {});
      const T = HEALTH_TEXT[healthPrefs.lang] || HEALTH_TEXT.en;
      if (jobNotice) {
        const running = document.createElement('p');
        running.className = 'sls-luna-note';
        running.textContent = jobNotice;
        body.appendChild(running);
      }
      if (healthNotice) {
        const notice = document.createElement('p');
        notice.className = 'sls-luna-error';
        notice.textContent = healthNotice;
        body.appendChild(notice);
        healthNotice = null;
      }
      const note = document.createElement('p');
      note.className = 'sls-luna-note';
      note.innerHTML = T.intro;
      body.appendChild(note);
      if (!health) {
        const empty = document.createElement('p');
        empty.className = 'sls-luna-note';
        empty.textContent = T.empty;
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
        issues.push(T.issueUpdateAvailable(upd.latest || '?', upd.installed || '?'));
      }
      if (upd.pending) {
        issues.push(T.issueUpdateQueued(upd.pendingTag || 'latest'));
      }
      if (steamClient.pinned && !steamClient.match) {
        issues.push(T.issueSteamDrifted(steamClient.build || '?', steamClient.pinned));
      }
      if (steamClient.pinned && steamClient.pacmanManaged && !steamClient.pacmanIgnored) {
        issues.push(T.issuePacman);
      }
      if (steamClient.pinned && !steamClient.updatesBlocked) {
        issues.push(T.issueUpdatesAllowed);
      }
      if (Number(steam.localconfigConflicts || 0) > 0) {
        issues.push(T.issueConflicts(steam.localconfigConflicts));
      }
      if ((lumen.addons || []).length && !boot.patched) {
        issues.push(T.issueBridgeMissing);
      }
      if (lunaRefresh && lunaRefresh.refreshPending) {
        issues.push(T.issueRefreshQueued);
      }
      if (asList(install.drift).length) {
        issues.push(T.issueDrift(asList(install.drift).slice(0, 2).join('; '),
          install.drift.length > 2 ? install.drift.length - 2 : 0));
      }
      if ((upd.autoUpdate || 'off') !== 'off' && !upd.timerUnits && !upd.pending) {
        issues.push(T.issueNoTimer);
      }
      section(issues.length ? T.needsAttention(issues.length) : T.statusOk);
      if (!issues.length) line(T.allGood);
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

      section(T.addons);
      const pinned = install.versions || {};
      asList(lumen.addons).forEach(entry => {
        const want = pinned[entry.name];
        line(entry.name + ' ' + (entry.version || '?')
          + (want && String(want) !== String(entry.version) ? ' \u00b7 pinned ' + want : '')
          + ' \u00b7 ' + (entry.files || 0) + ' entries');
      });
      if (asList(install.drift).length) line('Drift: ' + asList(install.drift).join('; '));

      section(T.paths);
      line('Lumen: ' + (lumen.root || '?') + (lumen.binary ? ' (binary present)' : ' (binary missing)')
        + (lumen.luatools ? ' \u00b7 LuaTools present' : ' \u00b7 LuaTools missing')
        + (lumen.bridge ? ' \u00b7 bridge present' : ' \u00b7 bridge missing'));
      const hostInfo = health.host || {};
      line('Host: ' + (hostInfo.running || '?')
        + (hostInfo.running === 'luna' ? ' (standalone)' : '')
        + (hostInfo.configured && hostInfo.configured !== hostInfo.running
        ? ' (configured: ' + hostInfo.configured + ')' : ''));
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
          + (install.generated ? ' \u00b7 bundle built ' + String(install.generated).slice(0, 10) : '')
          + (install.source ? ' \u00b7 ' + install.source : ''));
      } else {
        line('Last install: not recorded by this installer version (run install --force once).');
      }

      section(T.settings);
      const prefs = data.prefs || {};
      line('Beta features: ' + (prefs.beta ? 'on' : 'off')
        + ' \u00b7 HLTB order: ' + (prefs.hltb_order || 'short')
        + ' \u00b7 Store filter: ' + (prefs.store_hide_followed ? 'on' : 'off')
        + ' \u00b7 SteamGridDB key: ' + (data.sgdb && data.sgdb.hasKey ? 'stored' : 'not set'));

      section(T.dataFiles);
      asList(data.files).forEach(entry => {
        line(entry.name + (entry.directory
          ? '/ (' + (entry.files != null ? entry.files : 0) + ' entries)'
          : ' \u00b7 ' + formatBytes(entry.size || 0)));
      });

      /* Updates, the Steam client controls and the auto-update switch live on
         the Updates page now; Health keeps the diagnostics and the repair. */
      section(T.updates);
      const repairRow = document.createElement('div');
      repairRow.className = 'sls-luna-row';
      const repairButton = document.createElement('button');
      repairButton.className = 'sls-luna-btn secondary';
      repairButton.textContent = T.repairBridge;
      repairButton.title = T.repairTitle;
      repairButton.disabled = !((lumen.addons || []).length && !boot.patched);
      repairButton.onclick = () => runUpdateAction({ action: 'repair' });
      repairRow.appendChild(repairButton);
      body.appendChild(repairRow);
      line('Looking for Health > Lumen? Updating Lumen and Luna is on the Updates page, the next tab.');

      /* Problems: the last few failures from every layer (RPCs, this panel's
         own warnings, the agent's ring and the last backend job), compact and
         copyable. Read from memory and one localStorage key: no RPC, so the
         page opens as fast as before. */
      const problemEntries = () => {
        const list = PROBLEMS.slice();
        try {
          const raw = w.localStorage && w.localStorage.getItem('slsLunaDiag');
          const agent = raw ? JSON.parse(raw) : [];
          if (Array.isArray(agent)) {
            agent.forEach(item => {
              if (item && item.text) {
                list.push({ at: Number(item.at) || 0, source: 'agent', text: String(item.text) });
              }
            });
          }
        } catch (_) { /* a malformed diag is not itself a problem */ }
        // The repair job's last outcome comes from the state file the Health
        // RPC already returned, so it survives a reopen without another call.
        const repairJob = (lunaHealth && lunaHealth.repair) || null;
        if (repairJob && repairJob.message && repairJob.state !== 'idle') {
          const already = list.some(entry => entry.text === repairJob.message);
          if (!already) {
            list.push({ at: (repairJob.result && repairJob.result.at) * 1000
              || (repairJob.startedAt || 0) * 1000, source: 'repair', text: repairJob.message });
          }
        }
        list.sort((a, b) => (a.at || 0) - (b.at || 0));
        return list.slice(-PROBLEM_LIMIT);
      };
      section('Problems');
      const problems = problemEntries();
      if (!problems.length) {
        line('Nothing recorded this session.');
      } else {
        const clock = at => {
          try { return new Date(at || Date.now()).toLocaleTimeString(); } catch (_) { return ''; }
        };
        problems.slice(-6).forEach(entry => {
          line(clock(entry.at) + ' \u00b7 ' + entry.source + ': ' + entry.text);
        });
        if (problems.length > 6) line('and ' + (problems.length - 6) + ' older entries in Copy details.');
        const copyProblems = document.createElement('button');
        copyProblems.className = 'sls-luna-btn secondary';
        copyProblems.textContent = 'Copy details';
        copyProblems.onclick = () => {
          const area = document.createElement('textarea');
          area.rows = 10;
          area.style.width = '100%';
          area.value = problems.map(entry =>
            clock(entry.at) + ' \u00b7 ' + entry.source + ': ' + entry.text).join('\n');
          body.insertBefore(area, body.firstChild ? body.firstChild.nextSibling : null);
          area.focus();
          area.select();
          try { if (document.execCommand) document.execCommand('copy'); } catch (_) { /* select-only */ }
        };
        const problemRow = document.createElement('div');
        problemRow.className = 'sls-luna-row';
        problemRow.appendChild(copyProblems);
        body.appendChild(problemRow);
      }

      const copy = document.createElement('button');
      copy.className = 'sls-luna-btn secondary';
      copy.textContent = T.copyDiagnostics;
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
    /* The backend runs the update check detached so GitHub never stalls
       Steam's UI thread; follow it while the menu is open so the result
       appears without reopening. */
    let followingCheck = false;
    function followUpdateCheck() {
      if (followingCheck || !(lunaUpdate && lunaUpdate.checking)) return;
      followingCheck = true;
      let tries = 0;
      const schedule = w.setTimeout || setTimeout;
      const step = async () => {
        tries += 1;
        try {
          const out = await rpc(w, 'SlsLunaUpdate', { raw: JSON.stringify({ action: 'status' }) });
          if (out && out.update) lunaUpdate = out.update;
        } catch (_) { /* keep the last known state */ }
        if (lunaUpdate && lunaUpdate.checking && tries < 45 && document.getElementById(OVERLAY_ID)) {
          schedule(step, 2000);
          return;
        }
        followingCheck = false;
        repaintIfOpen();
      };
      schedule(step, 2000);
    }

    /* Backend jobs run detached, so the start call returns in milliseconds.
       This loop follows the job's small state file through the status action
       the page already has: a cheap poll, a live line in the page, and the
       outcome in the Problems block. Closing the menu leaves the job running. */
    function jobText(job, label) {
      const state = String((job && job.state) || '');
      const message = String((job && job.message) || '').replace(/\s+/g, ' ').trim();
      if (state === 'ok' || state === 'done') return message || (label + ' finished.');
      if (state === 'timeout') {
        return message || (label + ' timed out at ' + ((job && job.limit) || 180)
          + ' s; its process group was stopped.');
      }
      if (state === 'failed') return message || (label + ' failed.');
      if (state === 'idle') return message || (label + ' has not run yet.');
      return label + ' is still running in the background; it continues if this menu closes.';
    }

    async function followJob(label, start, status) {
      const first = await start();
      if (!first || first.success === false) {
        throw Error((first && first.error) || (label + ' did not start'));
      }
      let job = first.job || { state: 'running' };
      for (let tries = 0; tries < 320; tries += 1) {
        jobNotice = (job.state === 'running' || job.state === 'finishing' || job.state === 'starting')
          ? label + ' in progress' + (job.age ? ' (' + job.age + ' s)' : '') + '\u2026'
          : jobText(job, label);
        repaintIfOpen();
        if (job.state !== 'running' && job.state !== 'finishing' && job.state !== 'starting') break;
        if (!document.getElementById(OVERLAY_ID)) return job; // it keeps running
        await sleep(600);
        try {
          const out = await status();
          if (out && out.job) job = out.job;
        } catch (error) {
          addProblem('job', label + ' status: '
            + String(error && error.message ? error.message : error));
          break;
        }
      }
      jobNotice = jobText(job, label);
      addProblem('job', jobNotice);
      repaintIfOpen();
      return job;
    }

    async function runUpdateAction(payload) {
      try {
        if (payload.action === 'repair') {
          healthNotice = 'Repair started in the background; Steam stays responsive.';
          repaintIfOpen();
          const job = await followJob('Repair',
            () => rpc(w, 'SlsLunaRepair', { raw: JSON.stringify({ action: 'start' }) }),
            () => rpc(w, 'SlsLunaRepair', { raw: JSON.stringify({ action: 'status' }) }));
          healthNotice = jobText(job, 'Repair');
          await refresh();
          repaintIfOpen();
          return;
        }
        if (payload.action === 'mode') {
          await rpc(w, 'SlsLunaPrefsSet',
            { raw: JSON.stringify({ key: 'auto_update', value: payload.value }) });
          const job = await followJob('Auto-update timer',
            () => rpc(w, 'SlsLunaUpdate', { raw: JSON.stringify({ action: 'timer',
              value: payload.value === 'off' ? 'off' : 'on' }) }),
            () => rpc(w, 'SlsLunaUpdate', { raw: JSON.stringify({ action: 'status' }) })
              .then(out => { if (out && out.update) lunaUpdate = out.update; return out; }));
          healthNotice = jobText(job, 'Auto-update timer');
          await refresh();
          repaintIfOpen();
          return;
        }
        const request = payload.action === 'check' ? { action: 'check', force: true } : payload;
        const out = await rpc(w, 'SlsLunaUpdate', { raw: JSON.stringify(request) });
        if (out && out.update) lunaUpdate = out.update;
        if (payload.action === 'check') followUpdateCheck();
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
       then re-blocks the bootstrapper. Refresh queues the whole flow. */
    async function runRefreshAction(payload) {
      try {
        const out = await rpc(w, 'SlsLunaRefresh', { raw: JSON.stringify(payload) });
        if (out) lunaRefresh = out;
        healthNotice = null;
        await refresh();
        repaintIfOpen();
      } catch (error) {
        healthNotice = 'Refresh action failed: '
          + String(error && error.message ? error.message : error);
        repaintIfOpen();
      }
    }
    async function runSteamAction(payload) {
      try {
        if (payload.action === 'pin') {
          healthNotice = 'Pinning in the background; Steam stays responsive.';
          repaintIfOpen();
          const job = await followJob('Steam pin',
            () => rpc(w, 'SlsLunaSteam', { raw: JSON.stringify(payload) }),
            () => rpc(w, 'SlsLunaSteam', { raw: JSON.stringify({ action: 'status' }) })
              .then(out => { if (out && out.steam) lunaSteam = out.steam; return out; }));
          healthNotice = jobText(job, 'Steam pin');
          await refresh();
          repaintIfOpen();
          return;
        }
        const out = await rpc(w, 'SlsLunaSteam', { raw: JSON.stringify(payload) });
        if (out && out.steam) lunaSteam = out.steam;
        await refresh();
        healthNotice = null;
        repaintIfOpen();
      } catch (error) {
        healthNotice = 'Steam action failed: '
          + String(error && error.message ? error.message : error);
        repaintIfOpen();
      }
    }

    /* ------------------------------------------------------------ Updates
       The one place for versions and updating. Nothing here runs an update
       while Steam is open: every action either queues a job the
       `luna autoupdate` timer applies with Steam closed, or is read-only.
       Each RPC failure shows as its own error row; the rest still renders. */
    /* ---- One state for the Lumen job. Everything the page shows about Lumen
       comes from deriveLumen(): a single key, its sentence, and the allowed
       actions with a reason for each disabled one. */
    const LUMEN_FINAL = ['applied', 'failed', 'current', 'unknown', 'cancelled', 'restored'];
    const LUMEN_RESTORE_CMD = name => 'luna lumen restore --backup ' + name + ' --yes';
    const LUMEN_REINSTALL_CMD = 'luna lumen update --yes --reinstall';

    function versionParts(value) {
      const found = String(value || '').replace(/^v/i, '').match(/\d+/g);
      return found ? found.map(Number) : null;
    }
    // -1, 0, 1, or null when either side is not a version number.
    function compareVersions(a, b) {
      const x = versionParts(a);
      const y = versionParts(b);
      if (!x || !y) return null;
      for (let i = 0; i < Math.max(x.length, y.length); i += 1) {
        const d = (x[i] || 0) - (y[i] || 0);
        if (d) return d < 0 ? -1 : 1;
      }
      return 0;
    }
    // The Luna release job: an update is offered only when the latest release is
    // really newer; an installed build that is ahead of it is never "updated".
    function lunaRelease(update) {
      const relation = compareVersions(update.installed, update.latest);
      const newer = update.available === true && relation !== 0 && relation !== 1;
      return { newer: newer, ahead: relation === 1 && Boolean(update.latest),
        known: Boolean(update.latest) };
    }
    function arrowText(text) { return String(text || '').replace(/\s*->\s*/g, ' → '); }
    function changedEntries(result) {
      const out = [];
      const changed = result && result.changed;
      if (changed && typeof changed === 'object') {
        Object.keys(changed).sort().forEach(name => out.push([name, arrowText(changed[name])]));
      } else {
        const before = (result && result.before) || {};
        const after = (result && result.after) || {};
        Object.keys(after).sort().forEach(name => {
          if (before[name] && String(before[name]) !== String(after[name])) {
            out.push([name, before[name] + ' → ' + after[name]]);
          }
        });
      }
      return out;
    }
    function componentText(item) {
      const installed = String(item.installed || '?');
      const available = String(item.available || '?');
      if (item.status === 'newer') {
        return item.component + ': ' + installed + ' → ' + available
          + (available === installed || available === 'beta' ? ' (newer build)' : '');
      }
      return item.component + ': ' + installed + (item.status === 'current' ? ' (current)'
        : ' (unknown' + (item.note ? ': ' + item.note : '') + ')');
    }

    function deriveLumen(lumen) {
      const result = lumen.result || {};
      const rs = String(result.state || '');
      const at = Number(result.at) || 0;
      const started = Number(lumen.sessionStartedAt) || 0;
      const pending = Boolean(lumen.pending);
      const job = lumen.job || {};
      const history = at > 0 && started > 0 && at < started && LUMEN_FINAL.indexOf(rs) >= 0;
      const fresh = Boolean(rs) && !history;
      const check = lumen.check || {};
      const upstream = (check.status || {}).upstream || null;
      const checkedAt = Number(check.checkedAt) || 0;
      const checkUsable = Boolean(upstream) && (!fresh || LUMEN_FINAL.indexOf(rs) < 0
        || rs === 'current' || rs === 'unknown' || checkedAt >= at);
      const view = { key: 'idle', text: '', components: [], checkedAt: checkedAt,
        checkFailed: Boolean(check.failed || (check.status && check.status.upstreamError)),
        checking: Boolean(check.checking) };
      view.components = (checkUsable ? asList(upstream.components)
        : (fresh && (rs === 'current' || rs === 'unknown') ? asList(result.compared) : []));
      const phases = { backup: 'Updating: taking a backup. Nothing has changed yet.',
        installer: 'Updating: replacing Lumen. Do not shut the computer down.',
        reattach: 'Updating: attaching Luna again.',
        rollback: 'Rolling back: putting the backup back.' };
      const say = (key, text) => { view.key = key; view.text = text; return view; };
      if (rs === 'running' && lumen.interrupted) {
        return pending
          ? say('recovering', 'An update was interrupted. It will be recovered after you exit Steam.')
          : say('interrupted', 'An update was interrupted. Close Steam and run: luna lumen update --yes '
            + '(it recovers first).');
      }
      if (rs === 'running') return say('running', phases[result.phase] || 'Updating…');
      if (rs === 'failed' && result.rollbackPending) {
        return say('failed-rollback-pending', 'The update failed. The previous version comes back '
          + 'after you exit Steam; until then Lumen stays as it is.');
      }
      if (pending) {
        return say('queued', job.action === 'reattach'
          ? 'Waiting for you to exit Steam to finish attaching Luna.'
          : job.action === 'rollback' ? 'Waiting for you to exit Steam to put the previous version back.'
          : 'Queued. The update starts after you exit Steam.');
      }
      if (lumen.toolingIncomplete) {
        return say('tooling', 'Lumen updates are off: ' + lumen.toolingIncomplete
          + '. Repair with: luna install --force');
      }
      if (fresh && rs === 'applied') {
        const changes = changedEntries(result);
        return say('applied', changes.length
          ? 'Updated: ' + changes.map(pair => pair[0] + ' ' + pair[1]).join(', ')
          : 'Reinstalled. Nothing changed.');
      }
      if (fresh && rs === 'failed') {
        const why = result.message ? ' ' + result.message : '';
        if (result.rolledBack === true) {
          return say('failed-rolled-back', 'The update failed and Lumen was rolled back to the '
            + 'previous version.' + why);
        }
        if (result.backup) {
          return say('failed-not-rolled-back', 'The update failed and was NOT rolled back.' + why);
        }
        return say('failed', 'The update failed before anything was changed.' + why);
      }
      if (checkUsable) {
        if (upstream.overall === 'newer') {
          return say('update-available', 'Update available: ' + (view.components
            .filter(item => item.status === 'newer').map(componentText).join(', ') || 'newer build'));
        }
        if (upstream.overall === 'current') return say('current', 'Up to date. Nothing newer on your channels.');
        return say('unknown', 'Could not tell whether an update exists. Nothing was changed.');
      }
      if (fresh && rs === 'current') return say('current', 'Up to date. Nothing newer on your channels.');
      if (fresh && rs === 'unknown') {
        return say('unknown', 'Could not check for updates. ' + (result.message || '') + ' Nothing was changed.');
      }
      if (check.failed) return say('unknown', 'Could not check for updates. Nothing was changed.');
      return say('idle', 'Not checked yet. Press "Check for updates" to see what an update would change.');
    }

    // What the owner may do now. A disabled action carries the reason as text.
    function lumenActions(lumen, view, pick) {
      const present = asList((lumen.millennium || {}).present);
      const choice = (lumen.millennium || {}).choice || '';
      const millenniumNeeded = present.length > 0 && !choice && !pick;
      const result = lumen.result || {};
      const busy = view.key === 'running' || view.key === 'recovering' || view.key === 'interrupted';
      const actions = {
        check: { enabled: !busy && !view.checking && !lumenBusy,
          reason: busy ? 'An update is in progress.' : view.checking ? 'Checking…' : '' },
        update: { label: view.key === 'current' ? 'Up to date' : 'Update Lumen', enabled: false, reason: '' },
        cancel: { show: false, enabled: false, reason: '' },
        reinstall: { show: view.key === 'current' && !lumen.toolingIncomplete && !lumen.pending },
        restore: { show: false, name: '', command: '' },
        choice: { show: millenniumNeeded || (present.length > 0 && !choice),
          picked: pick || choice || '' },
      };
      const reasons = {
        idle: 'Check for updates first.', current: 'Everything is current.',
        unknown: 'Check again when you are online.', tooling: 'Repair the Lumen tooling first.',
        running: 'An update is running.', recovering: 'An interrupted update is waiting to be recovered.',
        interrupted: 'Recover the interrupted update first.', queued: 'An update is already queued.',
        'failed-rollback-pending': 'A rollback is waiting for you to exit Steam.',
      };
      if (view.key === 'update-available' || view.key === 'applied' || view.key.indexOf('failed') === 0) {
        // After a final result only a fresh check can say something is newer.
        actions.update.reason = view.key === 'update-available' ? '' : 'Check for updates first.';
      } else {
        actions.update.reason = reasons[view.key] || '';
      }
      if (view.key === 'update-available' && millenniumNeeded) {
        actions.update.reason = 'Choose what to do with Millennium first.';
      }
      actions.update.enabled = view.key === 'update-available' && !millenniumNeeded && !lumenBusy;
      if (view.key === 'queued' || view.key === 'failed-rollback-pending') {
        const updateJob = view.key === 'queued' && ((lumen.job || {}).action || 'update') === 'update';
        actions.cancel = { show: true, enabled: updateJob && !lumenBusy,
          reason: updateJob ? '' : 'This step finishes the work already started and cannot be cancelled.' };
      }
      const changes = changedEntries(result);
      const wanted = result.backup && result.rolledBack !== true && view.key !== 'running'
        && !busy && ((view.key === 'applied' && changes.length > 0) || view.key === 'failed-not-rolled-back');
      if (wanted) {
        const known = asList(lumen.backups).find(item => item.name === result.backup) || {};
        actions.restore = { show: true, name: String(result.backup), at: known.at || 0,
          bytes: known.bytes || 0, command: LUMEN_RESTORE_CMD(result.backup) };
      }
      return actions;
    }

    function lumenView() {
      return deriveLumen(lunaLumen || {});
    }

    async function runLumenAction(action, extra) {
      // Instant visible response: the page repaints with a working line and
      // disabled buttons before the RPC round-trip, and an RPC error always
      // lands as an error row instead of silence.
      lumenBusy = true;
      lumenConfirm = null;
      delete rpcErrors.lumenAction;
      repaintIfOpen();
      try {
        const out = await rpc(w, 'SlsLunaLumen',
          { raw: JSON.stringify(Object.assign({ action: action }, extra || {})) });
        if (out && out.lumen) lunaLumen = out.lumen;
        if (out && out.success === false) throw new Error(out.error || 'refused');
        delete rpcErrors.lumenAction;
      } catch (error) {
        rpcErrors.lumenAction = (action === 'cancel' ? 'Could not cancel: '
          : action === 'update' ? 'Could not queue the Lumen update: '
          : action === 'check' ? 'Could not check for updates: ' : 'Lumen action failed: ')
          + errorText(error);
      }
      lumenBusy = false;
      lumenNextPoll = 0;
      lumenIdlePolls = 0;
      await refresh();
      repaintIfOpen();
    }

    /* A channel change goes through `luna lumen channels set`, which owns
       update-channels.json and refuses bad values with its own text. The re-read
       afterwards plus a fresh `status --check` is what updates the available
       column: this page never writes the channels file itself. */
    async function runChannelSet(component, channel) {
      lumenConfirm = null;
      channelsBusy = true;
      delete rpcErrors.lumenAction;
      repaintIfOpen();
      try {
        const out = await rpc(w, 'SlsLunaLumen', { raw: JSON.stringify({ action: 'channels-set',
          component: component, channel: channel, confirmed: true }) });
        if (out && out.channels) lunaChannels = out.channels;
        if (out && out.lumen) lunaLumen = out.lumen;
        if (out && out.success === false) throw new Error(out.error || 'refused');
        try {
          const check = await rpc(w, 'SlsLunaLumen', { raw: JSON.stringify({ action: 'check' }) });
          if (check && check.lumen) lunaLumen = check.lumen;
        } catch (_) { /* the channel stands; the page says the check failed */ }
      } catch (error) {
        rpcErrors.lumenAction = 'Could not change the channel: ' + errorText(error);
      }
      channelsBusy = false;
      lumenNextPoll = 0;
      lumenIdlePolls = 0;
      await refresh();
      repaintIfOpen();
    }

    /* The visible-terminal update. Only `update-all --terminal` runs: the CLI
       asks Steam itself to quit, opens the window and writes the log. Exit 0
       only means the window opened, so the page says that and shows the log. */
    async function runTerminalUpdate() {
      lumenConfirm = null;
      lumenBusy = true;
      delete rpcErrors.lumenAction;
      repaintIfOpen();
      try {
        const out = await rpc(w, 'SlsLunaLumen', { raw: JSON.stringify({ action: 'terminal',
          confirmed: true }) });
        if (out && out.lumen) lunaLumen = out.lumen;
        if (out && out.success === false) throw new Error(out.error || 'refused');
      } catch (error) {
        rpcErrors.lumenAction = 'Could not start the terminal update: ' + errorText(error);
      }
      lumenBusy = false;
      lumenNextPoll = 0;
      lumenIdlePolls = 0;
      await refresh();
      repaintIfOpen();
    }

    // The status RPC reads files only, so following it is cheap; the network
    // check is never started from here.
    async function pollLumen() {
      try {
        const out = await rpc(w, 'SlsLunaLumen', { raw: JSON.stringify({ action: 'status' }) });
        if (out && out.lumen && !lumenBusy) {
          const before = JSON.stringify(lunaLumen);
          lunaLumen = out.lumen;
          lumenIdlePolls = JSON.stringify(lunaLumen) === before ? lumenIdlePolls + 1 : 0;
          repaintIfOpen();
        }
      } catch (_) { /* keep the last known state */ }
    }

    /* Queue, in order: the Luna add-ons/tooling update, then (Lumen host only)
       the Lumen update, whose re-attach installs that Luna on top. Never Steam. */
    async function runEverything() {
      lumenConfirm = null;
      const queuedAt = Math.floor(Date.now() / 1000);
      const state = { queuedAt: queuedAt, luna: {}, lumen: null };
      everything = state;
      const update = lunaUpdate || {};
      const release = lunaRelease(update);
      // Only what is really newer is queued: a current Luna is not reinstalled.
      if (release.newer) {
        try {
          const out = await rpc(w, 'SlsLunaUpdate',
            { raw: JSON.stringify({ action: 'install', tag: update.latest || '' }) });
          if (out && out.success === false) throw new Error(out.error || 'refused');
          if (out && out.update) lunaUpdate = out.update;
          if (!(lunaUpdate && lunaUpdate.timerUnits)) {
            try {
              await rpc(w, 'SlsLunaUpdate', { raw: JSON.stringify({ action: 'timer', value: 'on' }) });
            } catch (_) { /* the queue stands; the page explains the timer */ }
          }
        } catch (error) {
          state.luna.error = errorText(error);
        }
      } else {
        state.luna.skipped = true;
      }
      const onLumen = ((lunaHost || {}).configured || (lunaHost || {}).running) === 'lumen';
      const wantLumen = onLumen && lumenView().key === 'update-available';
      if (wantLumen && !state.luna.error) {
        state.lumen = {};
        try {
          const choice = ((lunaLumen || {}).millennium || {}).choice || millenniumPick;
          const out = await rpc(w, 'SlsLunaLumen', { raw: JSON.stringify(Object.assign(
            { action: 'update', confirmed: true }, choice ? { millennium: choice } : {})) });
          if (out && out.lumen) lunaLumen = out.lumen;
          if (out && out.success === false) throw new Error(out.error || 'refused');
        } catch (error) {
          state.lumen.error = errorText(error);
        }
      }
      await refresh();
      repaintIfOpen();
    }

    function everythingSteps() {
      if (!everything) return [];
      const update = lunaUpdate || {};
      const lumen = lunaLumen || {};
      const steps = [];
      const luna = everything.luna;
      const last = update.last || {};
      let lunaStep;
      if (luna.error) lunaStep = ['failed', luna.error];
      else if (luna.skipped) lunaStep = ['skipped', 'already current'];
      else if (update.pending) lunaStep = ['pending', 'queued; lands when Steam is closed'];
      else if (last.at && Number(last.at) >= everything.queuedAt) {
        lunaStep = /error|fail/.test(String(last.result || ''))
          ? ['failed', String(last.reason || last.result)]
          : ['done', String(last.result || 'done') + (last.version ? ' ' + last.version : '')];
      } else lunaStep = ['running', 'applying'];
      steps.push({ label: 'Luna add-ons and tooling', state: lunaStep[0], message: lunaStep[1] });
      if (everything.lumen) {
        let lumenStep;
        const view = deriveLumen(lumen);
        if (everything.lumen.error) lumenStep = ['failed', everything.lumen.error];
        else if (view.key === 'running') lumenStep = ['running', view.text];
        else if (lumen.pending) lumenStep = ['pending', 'queued after the Luna update'];
        else if (view.key === 'applied') lumenStep = ['done', view.text];
        else if (view.key.indexOf('failed') === 0) lumenStep = ['failed', view.text];
        else lumenStep = ['pending', 'waiting'];
        steps.push({ label: 'Lumen update + Luna re-install', state: lumenStep[0], message: lumenStep[1] });
      }
      return steps;
    }

    function updatesBody() {
      const body = document.createElement('div');
      const text = (className, value) => {
        const node = document.createElement('p');
        node.className = className;
        node.textContent = value;
        body.appendChild(node);
        return node;
      };
      const line = value => text('sls-luna-note', value);
      const section = title => {
        const node = document.createElement('p');
        node.className = 'sls-luna-note';
        const strong = document.createElement('strong');
        strong.textContent = title;
        node.appendChild(strong);
        body.appendChild(node);
      };
      const button = (label, onclick, secondary, disabled) => {
        const node = document.createElement('button');
        node.className = 'sls-luna-btn' + (secondary ? ' secondary' : '');
        node.textContent = label;
        node.disabled = Boolean(disabled);
        node.onclick = onclick;
        return node;
      };
      const row = (...buttons) => {
        const node = document.createElement('div');
        node.className = 'sls-luna-row';
        buttons.forEach(item => node.appendChild(item));
        body.appendChild(node);
      };
      // The shared confirm step: what changes, when it runs, what protects it.
      // `notes` replaces the queue-specific lines for actions that do something
      // else (a channel switch, the visible-terminal update).
      const confirmBlock = (changes, yesLabel, onYes, notes) => {
        const box = document.createElement('div');
        box.className = 'sls-luna-confirm';
        box.style.cssText = 'padding:10px 12px;margin:6px 0;border:1px solid #d9a441;border-radius:8px';
        const add = value => {
          const node = document.createElement('p');
          node.className = 'sls-luna-note';
          node.textContent = value;
          box.appendChild(node);
        };
        changes.forEach(add);
        (notes || ['It runs after you exit Steam. Steam is not closed for you.',
          'A backup is taken first. If the update fails, it is rolled back automatically.'])
          .forEach(add);
        const kept = (notes ? '' : (((lunaLumen || {}).millennium || {}).choice || millenniumPick));
        if (kept && (((lunaLumen || {}).millennium || {}).present || []).length) {
          add('Millennium will be ' + (kept === 'keep' ? 'kept.' : 'removed.'));
        }
        const buttons = document.createElement('div');
        buttons.className = 'sls-luna-row';
        buttons.appendChild(button(yesLabel, onYes, false, lumenBusy));
        buttons.appendChild(button('Not now', () => { lumenConfirm = null; repaintIfOpen(); }, true, false));
        box.appendChild(buttons);
        body.appendChild(box);
      };
      if (healthNotice) { line(healthNotice); healthNotice = null; }
      const labels = { info: 'Luna info', health: 'Luna health', update: 'Luna updates',
        steam: 'the Steam client', host: 'the plugin host', lumen: 'Lumen', refresh: 'refresh state',
        lumenAction: '' };
      Object.keys(rpcErrors).forEach(key => {
        if (!rpcErrors[key]) return;
        text('sls-luna-error', key === 'lumenAction' ? rpcErrors[key]
          : 'Could not read ' + labels[key] + ': ' + rpcErrors[key]);
      });

      const update = lunaUpdate || {};
      const steam = lunaSteam || {};
      const hostInfo = lunaHost || {};
      const health = lunaHealth || {};
      const lumen = lunaLumen || {};
      const onLumen = (hostInfo.configured || hostInfo.running) === 'lumen';
      const mode = update.autoUpdate || 'off';
      const lumenView_ = onLumen ? deriveLumen(lumen) : null;
      const acts = onLumen ? lumenActions(lumen, lumenView_, millenniumPick) : null;
      const release = lunaRelease(update);
      const lumenIncomplete = String(lumen.toolingIncomplete || '');
      if (lumenBusy) line('Lumen: working…');
      if (lumenView_ && (lumenView_.key === 'queued' || lumenView_.key === 'failed-rollback-pending'
          || lumenView_.key === 'running' || lumenView_.key === 'recovering')) {
        const banner = document.createElement('div');
        banner.className = 'sls-luna-banner';
        banner.style.cssText = 'padding:10px 12px;margin:0 0 10px;border:1px solid #d9a441;'
          + 'border-radius:8px;background:rgba(217,164,65,.16)';
        const head = document.createElement('p');
        head.className = 'sls-luna-note';
        const strong = document.createElement('strong');
        strong.textContent = lumenView_.text + (lumenView_.key === 'queued'
          ? ' Steam is not closed for you.' : '');
        head.appendChild(strong);
        banner.appendChild(head);
        body.insertBefore(banner, body.firstChild);
      }

      // Everything that is newer, in one go: what would change, then one confirm.
      // Scope is fixed: Luna's add-ons/tooling plus the Lumen base, and the
      // Steam client is named as untouched in both the description and the
      // confirm so nobody can read this as "update Steam".
      const everythingLumen = Boolean(lumenView_) && lumenView_.key === 'update-available';
      const newerComponents = everythingLumen
        ? lumenView_.components.filter(item => item.status === 'newer') : [];
      const newerItems = (release.newer ? ['Luna ' + (update.installed || '?') + ' → ' + update.latest] : [])
        .concat(newerComponents.map(componentText));
      const steamPin = String(steam.pinned || 'not recorded');
      const steamScope = 'Steam client: unchanged (pinned ' + steamPin + ').';
      const busyLumen = Boolean(lumenView_ && ['running', 'recovering', 'interrupted', 'queued',
        'failed-rollback-pending', 'tooling'].indexOf(lumenView_.key) >= 0);
      const everythingKnown = (release.known && !release.ahead) || Boolean(lumenView_
        && (lumenView_.key === 'current' || lumenView_.key === 'update-available'));
      const everythingReady = !update.pending && !lumenBusy && !busyLumen && !lumenIncomplete
        && !(lumenView_ && acts.choice.show && !acts.choice.picked) && everythingKnown;
      section('Update everything');
      line('Queues Luna\u2019s add-ons and tooling' + (onLumen
        ? ', then the Lumen base update with Luna re-installed on top' : '')
        + ', only for what is newer on your channels. Both wait for you to exit Steam. '
        + steamScope);
      row(button('Update everything', () => { lumenConfirm = { kind: 'everything' }; repaintIfOpen(); },
        false, !everythingReady));
      if (!everythingReady) {
        line(update.pending ? 'Reason: a Luna update is already queued.'
          : lumenIncomplete ? 'Reason: ' + lumenIncomplete + '. Repair with: luna install --force'
          : busyLumen && lumenView_ ? 'Reason: ' + (acts.update.reason || lumenView_.text)
          : lumenView_ && acts.choice.show && !acts.choice.picked ? 'Reason: choose what to do with Millennium first.'
          : !everythingKnown ? 'Reason: not checked yet; press "Check now" or "Check for updates".' : '');
      }
      if (lumenConfirm && lumenConfirm.kind === 'everything') {
        const scope = ['Scope: Luna\u2019s add-ons and tooling'
          + (onLumen ? ', then the Lumen base (it is re-verified, not replaced by a newer installer)' : '')
          + '.', steamScope];
        if (!newerItems.length) {
          // A full reinstall when nothing is newer is exactly what this button
          // must not do: say so, and queue nothing.
          confirmBlock(['Nothing to do: nothing newer on your channels.'].concat(scope),
            'Close', () => { lumenConfirm = null; repaintIfOpen(); }, []);
        } else {
          confirmBlock(['This will update:'].concat(newerItems, scope),
            'Yes, queue the updates', () => runEverything());
        }
      }
      everythingSteps().forEach(step => {
        text(step.state === 'failed' ? 'sls-luna-error' : 'sls-luna-note',
          step.label + ': ' + step.state + (step.message ? ' · ' + step.message : ''));
      });

      // The visible-terminal twin of the queue above. It is the only path that
      // closes Steam: the CLI asks Steam itself to quit, so this page runs no
      // steam/lock command and kills nothing.
      const terminalJob = (lunaLumen || {}).terminal || {};
      const terminalStarting = terminalJob.state === 'starting';
      section('Update in terminal');
      line('Runs luna update-all --terminal: Luna\u2019s add-ons and the Lumen base, live in a terminal '
        + 'window that writes a log. This closes Steam first \u2014 the CLI asks Steam to quit itself and '
        + 'waits up to 60 s \u2014 then opens the window. ' + steamScope);
      row(button('Update in terminal', () => { lumenConfirm = { kind: 'terminal' }; repaintIfOpen(); },
        false, terminalStarting || lumenBusy));
      if (terminalStarting) {
        line('Starting: the CLI is asking Steam to quit and opening the window\u2026');
      }
      if (terminalJob.state === 'opened') {
        line('Window opened. The update runs there: exit 0 means the window opened, not that it finished.');
        if (terminalJob.log) line('Log: ' + terminalJob.log);
        if (terminalJob.window) line('Terminal: ' + terminalJob.window);
      }
      if (terminalJob.state === 'refused') {
        line('The CLI refused: ' + (String(terminalJob.output || '').replace(/\s+/g, ' ').trim()
          || 'no text; Steam was probably still running'));
        if (terminalJob.log) line('Log: ' + terminalJob.log);
      }
      if (lumenConfirm && lumenConfirm.kind === 'terminal') {
        confirmBlock(['Luna\u2019s add-ons and tooling and the Lumen base (the pinned installer is re-verified, not replaced).',
          steamScope,
          'This closes Steam first: the CLI asks Steam itself to quit and waits up to 60 s for steam and '
            + 'steamwebhelper before it opens the window.',
          'The window stays open at the end; the log path is shown here so you can reopen it.'],
          'Yes, close Steam and update', () => runTerminalUpdate(), []);
      }

      line('Auto-update: ' + mode + (update.timerUnits ? '' : ' (timer not set up on this machine)'));
      const modeRow = document.createElement('div');
      modeRow.className = 'sls-luna-subtabs';
      [['off', 'Off'], ['notify', 'Notify me'], ['install', 'Install automatically']].forEach(pair => {
        const item = document.createElement('button');
        item.className = 'sls-luna-subtab' + (mode === pair[0] ? ' active' : '');
        item.textContent = pair[1];
        item.onclick = () => runUpdateAction({ action: 'mode', value: pair[0] });
        modeRow.appendChild(item);
      });
      body.appendChild(modeRow);

      section('Luna');
      line('Luna ' + (update.installed || '?') + ' · latest: ' + (update.latest || 'unknown')
        + (release.newer ? ' · update available'
          : release.ahead ? ' · newer than the latest release'
          : update.latest ? ' · up to date' : '')
        + (update.checkedAt ? ' · checked ' + age(update.checkedAt) + ' ago' : ''));
      if (update.checking) line('Checking GitHub for a newer release…');
      else if (update.checkError) line('Check: ' + update.checkError);
      if (update.pending) line('Queued: ' + (update.pendingTag || 'latest') + ' - it lands the next time Steam is closed.');
      if (update.last) {
        line('Last run: ' + (update.last.result || '?')
          + (update.last.version ? ' ' + update.last.version : '')
          + (update.last.reason ? ' · ' + update.last.reason : '')
          + (update.last.at ? ' · ' + age(update.last.at) + ' ago' : ''));
      }
      const pinned = update.pinned || ((health.install || {}).versions) || {};
      const installedAddons = asList((health.lumen || {}).addons);
      ['luna', 'sgdb', 'graphics', 'playtime', 'presence', 'compressor'].forEach(name => {
        const entry = installedAddons.find(item => item.name === name);
        const want = pinned[name];
        const have = entry ? entry.version : (name === 'luna' ? update.installed : '');
        line('Add-on ' + name + ': installed ' + (have || 'not installed') + ' · pinned ' + (want || '?')
          + (have && want && String(have) !== String(want) ? ' — differs' : ''));
      });
      line('luna-host: ' + (hostInfo.running || '?')
        + (hostInfo.configured && hostInfo.configured !== hostInfo.running
          ? ' (configured: ' + hostInfo.configured + ')' : '')
        + ' · binary ' + (hostInfo.binary ? 'installed' : 'not installed') + ' (no separate version)');
      line('CLI/tooling: ' + (update.toolsInstalled ? 'installed' : 'not installed')
        + (update.source ? ' · from ' + update.source : '')
        + (update.installedAt ? ' · installed ' + age(update.installedAt) + ' ago' : ''));
      row(button(update.checking ? 'Checking…' : 'Check now', () => runUpdateAction({ action: 'check' }),
          true, update.checking),
        button(update.pending ? 'Queued' : 'Install Luna update',
          () => runUpdateAction({ action: 'install', tag: update.latest }), false,
          !release.newer || update.pending),
        button('Cancel queued', () => runUpdateAction({ action: 'cancel' }), true, !update.pending));

      if (onLumen) {
        section('Lumen');
        const versions = lumen.versions || {};
        line('Versions: ' + (Object.keys(versions).sort().map(key => key + ' ' + versions[key]).join(' · ')
          || 'unknown'));
        line('Self-update block: ' + (lumen.blockInPlace ? 'in place' : 'NOT in place')
          + ' · Luna bridge: ' + (lumen.bridge ? 'installed' : 'missing'));
        const result = lumen.result || {};
        const view = lumenView_;
        const chan = lumen.channels || {};
        const flag = (view.key.indexOf('failed') === 0 || view.key === 'interrupted') ? 'sls-luna-error'
          : 'sls-luna-note';
        text(flag, 'Status: ' + view.text);
        asList(result.warnings).forEach(item => { if (view.key === 'applied') line('Warning: ' + item); });
        // Older or superseded results are history, never the current state.
        const shownAsState = ['applied', 'failed', 'failed-rolled-back', 'failed-not-rolled-back',
          'failed-rollback-pending', 'running', 'recovering', 'interrupted'].indexOf(view.key) >= 0
          || (view.key === 'queued' && result.state === 'queued');
        if (result.state && result.at && !shownAsState) {
          line('Last run: ' + arrowText(result.state === 'applied' ? 'updated'
            : result.state) + (result.message ? ' · ' + result.message : '')
            + ' · ' + age(result.at) + ' ago');
        }
        // What an update would do: installed vs available per component.
        if (view.components.length) {
          view.components.forEach(item => line('  ' + componentText(item)
            + (item.channel ? ' · channel ' + item.channel : '')));
          if (view.checkedAt) line('Checked ' + age(view.checkedAt) + ' ago.');
        } else if (Object.keys(lumen.channels || {}).length) {
          line('Channels: ' + Object.keys(chan).sort().map(name => name + ' ' + chan[name]).join(' · '));
        }
        if (view.checkFailed && !view.components.length) {
          line('The last check did not finish. Try again when you are online.');
        }
        if (acts.choice.show) {
          text('sls-luna-error', 'Millennium is installed on this machine. An update would remove it '
            + 'unless you keep it. Choose before updating (nothing is chosen for you):');
          const pickRow = (value, label) => button(label + (acts.choice.picked === value ? ' ✓' : ''),
            () => { millenniumPick = value; repaintIfOpen(); }, acts.choice.picked !== value, false);
          row(pickRow('keep', 'Keep Millennium'), pickRow('remove', 'Remove Millennium'));
        }
        const buttons = [
          button(view.checking ? 'Checking…' : 'Check for updates', () => runLumenAction('check'), true,
            !acts.check.enabled),
          button(acts.update.label, () => { lumenConfirm = { kind: 'lumen' }; repaintIfOpen(); }, false,
            !acts.update.enabled),
        ];
        if (acts.cancel.show) {
          buttons.push(button('Cancel queued update', () => runLumenAction('cancel'), true, !acts.cancel.enabled));
        }
        row(...buttons);
        if (!acts.update.enabled && acts.update.reason) line(acts.update.label + ' is off. ' + acts.update.reason);
        if (acts.cancel.show && !acts.cancel.enabled && acts.cancel.reason) line('Cancel is off. ' + acts.cancel.reason);
        if (!acts.check.enabled && acts.check.reason && !view.checking) line('Check is off. ' + acts.check.reason);
        if (lumenConfirm && lumenConfirm.kind === 'lumen' && acts.update.enabled) {
          confirmBlock(['This will update:'].concat(
            view.components.filter(item => item.status === 'newer').map(componentText)),
            'Yes, queue the update', () => runLumenAction('update', Object.assign({ confirmed: true },
              acts.choice.picked ? { millennium: acts.choice.picked } : {})));
        }
        if (acts.reinstall.show) {
          row(button('Reinstall…', () => { lumenConfirm = { kind: 'reinstall' }; repaintIfOpen(); }, true, false));
          if (lumenConfirm && lumenConfirm.kind === 'reinstall') {
            const same = Object.keys(lumen.versions || {}).sort()
              .map(name => name + ' ' + lumen.versions[name]).join(', ');
            line('Reinstall puts the same versions back (' + (same || 'unchanged') + '). Nothing newer is installed. '
              + 'It cannot be queued from here: close Steam completely, then run in a terminal:');
            line(LUMEN_REINSTALL_CMD);
            row(button('Close', () => { lumenConfirm = null; repaintIfOpen(); }, true, false));
          }
        }
        if (acts.restore.show) {
          const when = acts.restore.at ? clock(acts.restore.at) : '';
          line('Go back: backup ' + acts.restore.name + (when ? ' (' + when + (acts.restore.bytes
            ? ', ' + formatBytes(acts.restore.bytes) : '') + ')' : '')
            + '. Restoring needs Steam closed and keeps your settings as they are. Close Steam, then run:');
          line(acts.restore.command);
        }
        if (view.key === 'update-available' || view.key === 'idle') {
          line('An update backs Lumen up first, runs the installer and attaches Luna again. '
            + 'If it fails, the backup is put back automatically.');
        }
      }

      // Channels: which release each Lumen part follows. Read and written only
      // through the CLI, which owns update-channels.json; the page never writes
      // that file and never guesses a value it has not read.
      if (onLumen) {
        section('Channels');
        const labelsByName = { lumen: 'Lumen base', plugin: 'Lumen plugin',
          slsteam_moon: 'SLSsteam Moon' };
        const values = (lunaChannels && lunaChannels.values) || null;
        line('Which channel each Lumen part follows. Changes go through luna lumen channels set; '
          + 'the Steam client is never touched by a channel change.');
        if (!values) {
          line(channelsError ? 'Could not read the channels: ' + channelsError
            : 'Reading the channels\u2026');
          if (channelsError) {
            row(button('Retry', () => { channelsTried = false; repaintIfOpen(); refresh(); },
              true, channelsBusy));
          }
        } else {
          ['lumen', 'plugin', 'slsteam_moon'].forEach(name => {
            const current = String(values[name] || 'stable');
            const item = document.createElement('div');
            item.className = 'sls-luna-row';
            const cell = document.createElement('div');
            cell.className = 'txt';
            const title = document.createElement('div');
            title.className = 'name';
            title.textContent = labelsByName[name] + ' (' + name + ')';
            const sub = document.createElement('div');
            sub.className = 'sub';
            sub.textContent = 'Channel: ' + current;
            cell.append(title, sub);
            item.appendChild(cell);
            [['stable', 'Stable'], ['beta', 'Beta']].forEach(pair => {
              item.appendChild(button(pair[1], () => {
                lumenConfirm = { kind: 'channel', component: name, channel: pair[0] };
                repaintIfOpen();
              }, pair[0] === current, channelsBusy || pair[0] === current));
            });
            body.appendChild(item);
          });
          if (lunaChannels && lunaChannels.file) line('Channels file: ' + lunaChannels.file);
          if (lumenConfirm && lumenConfirm.kind === 'channel') {
            const pick = lumenConfirm;
            const current = String(values[pick.component] || 'stable');
            confirmBlock([labelsByName[pick.component] + ' (' + pick.component + '): ' + current
              + ' → ' + pick.channel,
              'This only changes which channel the installer follows for that component. Nothing is installed '
                + 'now and the Steam client is unchanged.'],
            'Yes, switch to ' + pick.channel,
            () => runChannelSet(pick.component, pick.channel), []);
          }
        }
      }

      section('Steam client');
      line('Build ' + (steam.build || '?')
        + (steam.pinned ? ' · known-good ' + steam.pinned + (steam.match ? ' ✓' : ' — drifted') : ' · no known-good build pinned')
        + ' · updates ' + (steam.updatesBlocked ? 'blocked (steam.cfg lock)' : 'allowed'));
      if (steam.pending) line('Queued Steam task: ' + steam.pending);
      line('Stay on the known-good build — Recommended. This is the default; nothing moves Steam unless you choose below.');
      row(button('Pin this build as known-good', () => runSteamAction({ action: 'pin' }), true,
          Boolean(steam.pinned && steam.match)),
        button('Cancel Steam task', () => runSteamAction({ action: 'cancel' }), true, !steam.pending));
      if (!steamLatestArmed) {
        row(button('Move to Valve’s latest…', () => { steamLatestArmed = true; repaintIfOpen(); },
          true, Boolean(steam.pending)));
      } else {
        text('sls-luna-error', 'Risk: Valve’s newest client may break the current setup until Luna supports '
          + 'that build. It is queued and applied when Steam is closed.');
        row(button('Yes, queue the latest Steam client', () => {
            steamLatestArmed = false;
            runSteamAction({ action: 'queue-update' });
          }, false, Boolean(steam.pending)),
          button('Keep the known-good build', () => { steamLatestArmed = false; repaintIfOpen(); }, true));
      }
      return body;
    }

    function settingsBody() {
      const body = document.createElement('div');
      const settingsT = HEALTH_TEXT[((lunaInfo && lunaInfo.prefs) || {}).lang] || HEALTH_TEXT.en;
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
      if (steamosNotice) {
        const notice = document.createElement('p');
        notice.className = 'sls-luna-note';
        notice.textContent = steamosNotice;
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

      const lumenHeader = document.createElement('p');
      lumenHeader.className = 'sls-luna-note';
      lumenHeader.innerHTML = '<strong>Lumen</strong>';
      body.appendChild(lumenHeader);
      switchRow(body, 'Embed Lumen in Luna\u2019s window',
        'Lumen\u2019s pages open inside this window, with Lumen\u2019s own pages listed in the sidebar and '
        + 'one Esc closing both. Turn this off to send the Lumen page back to Lumen\u2019s own window; the '
        + 'moon button below is a separate switch.',
        lumenEmbedWanted(), setLumenEmbed);
      switchRow(body, 'Show Lumen\u2019s own menu button',
        'Luna\u2019s menu has a Lumen page, so the moon beside Luna is hidden by default: one launcher, '
        + 'one menu. Turn this on to keep both; the Lumen page reaches Lumen either way.',
        lumenButtonWanted(), setLumenOwnButton);

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
      const gradHeader = document.createElement('p');
      gradHeader.className = 'sls-luna-note';
      gradHeader.innerHTML = '<strong>' + settingsT.gradientTitle + '</strong>';
      body.appendChild(gradHeader);
      switchRow(body, settingsT.gradientLabel, settingsT.gradientSub,
        !(lunaInfo && lunaInfo.prefs && lunaInfo.prefs.steam_gradient === false), setGradient);
      const langHeader = document.createElement('p');
      langHeader.className = 'sls-luna-note';
      langHeader.innerHTML = '<strong>Language / Idioma</strong>';
      body.appendChild(langHeader);
      const langRow = document.createElement('div');
      langRow.className = 'sls-luna-subtabs';
      [['en', 'English'], ['es', 'Español']].forEach(pair => {
        const button = document.createElement('button');
        button.className = 'sls-luna-subtab'
          + (((lunaInfo && lunaInfo.prefs && lunaInfo.prefs.lang) || 'en') === pair[0] ? ' active' : '');
        button.textContent = pair[1];
        button.onclick = () => setLang(pair[0]);
        langRow.appendChild(button);
      });
      body.appendChild(langRow);
      const steamosHeader = document.createElement('p');
      steamosHeader.className = 'sls-luna-note';
      steamosHeader.innerHTML = '<strong>' + settingsT.steamosMode + '</strong>';
      body.appendChild(steamosHeader);
      const steamosDesc = document.createElement('p');
      steamosDesc.className = 'sls-luna-note';
      steamosDesc.textContent = settingsT.steamosDesc;
      body.appendChild(steamosDesc);
      const steamosRow = document.createElement('div');
      steamosRow.className = 'sls-luna-row';
      const steamosButton = document.createElement('button');
      steamosButton.className = 'sls-luna-btn';
      steamosButton.textContent = settingsT.createShortcuts;
      steamosButton.onclick = () => createSteamosShortcuts();
      steamosRow.appendChild(steamosButton);
      body.appendChild(steamosRow);

      // Steam updates: block (rollback + hard lock) or unblock. Both kill or
      // restart Steam, so the backend runs them as a detached job and the
      // click is armed in the page first (a native confirm() freezes the shell).
      const sc = lunaSteam || {};
      const updHeader = document.createElement('p');
      updHeader.className = 'sls-luna-note';
      updHeader.innerHTML = '<strong>' + settingsT.steamUpdates + '</strong>';
      body.appendChild(updHeader);
      const updState = document.createElement('p');
      updState.className = 'sls-luna-note';
      updState.textContent = (sc.hardLock ? settingsT.steamUpdHard
        : (sc.pinned && sc.updatesBlocked) ? settingsT.steamUpdSoft : settingsT.steamUpdNormal)
        + (sc.pinned ? ' \u00b7 ' + settingsT.steamUpdBuild + ' ' + sc.pinned : '');
      body.appendChild(updState);
      if (sc.job) {
        const busy = document.createElement('p');
        busy.className = 'sls-luna-note';
        busy.textContent = settingsT.steamUpdJob;
        body.appendChild(busy);
      }
      if (steamJobNotice) {
        const notice = document.createElement('p');
        notice.className = 'sls-luna-note';
        notice.textContent = steamJobNotice;
        body.appendChild(notice);
      }
      if (steamLockArmed) {
        const box = document.createElement('div');
        box.className = 'sls-luna-confirm';
        const detail = document.createElement('div');
        detail.className = 'sub';
        detail.textContent = steamLockArmed === 'lock' ? settingsT.blockConfirm : settingsT.unblockConfirm;
        const confirmRow = document.createElement('div');
        confirmRow.style.cssText = 'display:flex;gap:8px;margin-top:10px;';
        const yes = document.createElement('button');
        yes.className = 'sls-luna-btn';
        yes.textContent = steamLockArmed === 'lock' ? settingsT.confirmBlock : settingsT.confirmUnblock;
        yes.onclick = () => runSteamLockJob(steamLockArmed);
        const no = document.createElement('button');
        no.className = 'sls-luna-btn secondary';
        no.textContent = settingsT.cancel;
        no.onclick = () => { steamLockArmed = null; repaintIfOpen(); };
        confirmRow.append(yes, no);
        box.append(detail, confirmRow);
        body.appendChild(box);
      }
      const updRow = document.createElement('div');
      updRow.className = 'sls-luna-row';
      const blockButton = document.createElement('button');
      blockButton.className = 'sls-luna-btn';
      blockButton.textContent = settingsT.blockUpdates;
      blockButton.disabled = Boolean(sc.job) || Boolean(sc.hardLock);
      blockButton.onclick = () => { steamLockArmed = 'lock'; steamJobNotice = null; repaintIfOpen(); };
      const unblockButton = document.createElement('button');
      unblockButton.className = 'sls-luna-btn secondary';
      unblockButton.textContent = settingsT.unblockUpdates;
      unblockButton.disabled = Boolean(sc.job) || !(sc.hardLock || sc.pinned || sc.updatesBlocked);
      unblockButton.onclick = () => { steamLockArmed = 'unlock'; steamJobNotice = null; repaintIfOpen(); };
      updRow.append(blockButton, unblockButton);
      body.appendChild(updRow);

      // Which plugin host runs Luna (read-only; switching is `luna host`).
      const hs = lunaHost || {};
      const hostHeader = document.createElement('p');
      hostHeader.className = 'sls-luna-note';
      hostHeader.innerHTML = '<strong>' + settingsT.hostTitle + '</strong>';
      body.appendChild(hostHeader);
      const hostLine = document.createElement('p');
      hostLine.className = 'sls-luna-note';
      hostLine.textContent = settingsT.hostRunning + ' ' + (hs.running || '?')
        + (hs.configured && hs.running && hs.configured !== hs.running
          ? ' \u00b7 ' + hs.configured + ' ' + settingsT.hostPending : '');
      body.appendChild(hostLine);
      return body;
    }

    async function setGradient(value) {
      try {
        const out = await rpc(w, 'SlsLunaPrefsSet',
          { raw: JSON.stringify({ key: 'steam_gradient', value: Boolean(value) }) });
        lunaInfo = Object.assign({}, lunaInfo || {}, { prefs: out.prefs });
      } catch (error) {
        betaNotice = 'Could not save the gradient preference: '
          + String(error && error.message ? error.message : error);
      }
      gradient.refresh();
      repaintIfOpen();
    }

    async function runSteamLockJob(action) {
      const settingsT = HEALTH_TEXT[((lunaInfo && lunaInfo.prefs) || {}).lang] || HEALTH_TEXT.en;
      steamLockArmed = null;
      try {
        const out = await rpc(w, 'SlsLunaSteam', { raw: JSON.stringify({ action }) });
        if (out && out.steam) lunaSteam = out.steam;
        steamJobNotice = settingsT.steamJobStarted;
      } catch (error) {
        steamJobNotice = 'Steam action failed: ' + String(error && error.message ? error.message : error);
      }
      repaintIfOpen();
    }

    // SteamOS Mode shortcuts, one per detected screen. Creation only writes
    // .desktop entries and the managed helper, so it never needs Steam closed.
    async function createSteamosShortcuts() {
      try {
        steamosNotice = 'Creating shortcuts in the background\u2026';
        repaintIfOpen();
        const job = await followJob('Shortcuts',
          () => rpc(w, 'SlsLunaLaunchers', { raw: JSON.stringify({ action: 'create' }) }),
          () => rpc(w, 'SlsLunaLaunchers', { raw: JSON.stringify({ action: 'status' }) }));
        steamosNotice = jobText(job, 'Shortcuts');
      } catch (error) {
        steamosNotice = 'Could not create SteamOS Mode shortcuts: '
          + String(error && error.message ? error.message : error);
      }
      repaintIfOpen();
    }

    // The Health page language lives with the other prefs in the backend so
    // the choice survives cache resets on every machine sharing the data dir.
    async function setLang(value) {
      try {
        const out = await rpc(w, 'SlsLunaPrefsSet',
          { raw: JSON.stringify({ key: 'lang', value: String(value) }) });
        lunaInfo = Object.assign({}, lunaInfo || {}, { prefs: out.prefs });
      } catch (error) {
        betaNotice = 'Could not save the language preference: '
          + String(error && error.message ? error.message : error);
      }
      repaintIfOpen();
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

    /* ---- Lumen: one window, one menu -----------------------------------
       Lumen is the plugin host; its settings (LuaTools, SLSsteam) stay in
       Lumen's own UI, and Luna neither copies nor re-parents it. The Lumen
       page opens Lumen through Lumen's own public entry point and, by default,
       lays it into Luna's content area (see "Embedded Lumen" below) so Lumen's
       pages look like Luna's pages. With embedding switched off the page is a
       signpost that closes Luna and leaves Lumen's own window up. The one thing
       Luna takes over either way is the launcher: once its own button is placed
       in the menubar, Lumen's moon is hidden (a Settings switch brings it
       back), so there is one menu and one launcher. Also the only place Lumen's
       pending-update pill is still readable when its button is hidden. */
    const LUMEN_BTN_KEY = 'slsLunaLumenButton';
    const LUMEN_BTN_PREF = 'lumen_button';
    const LUMEN_HID_ATTR = 'data-sls-luna-hid-display';

    function lumenButtonWanted() {
      const stored = prefValue(LUMEN_BTN_PREF);
      if (typeof stored === 'boolean') return stored;
      return localItem(LUMEN_BTN_KEY) === '1';
    }
    function restoreLumenLauncher() {
      const btn = hiddenLumenBtn;
      hiddenLumenBtn = null;
      if (!btn || typeof btn.getAttribute !== 'function') return;
      const previous = btn.getAttribute(LUMEN_HID_ATTR);
      try { btn.removeAttribute(LUMEN_HID_ATTR); } catch (_) { /* gone */ }
      if (btn.style) btn.style.display = previous || '';
    }
    /* Idempotent: Lumen re-injects its button after menu opens and route
       changes, so the observer calls this again on every menubar mutation. A
       re-injected node is a new element, so the old reference is restored
       first (it is detached, which is a no-op) and the new one recorded. */
    function hideLumenLauncher() {
      if (tornDown || !panelMounted) return;
      const btn = document.getElementById('lumen-moon-btn');
      if (!btn) { restoreLumenLauncher(); return; }
      if (lumenButtonWanted()) { restoreLumenLauncher(); return; }
      if (hiddenLumenBtn && hiddenLumenBtn !== btn) restoreLumenLauncher();
      if (hiddenLumenBtn !== btn) {
        if (typeof btn.setAttribute !== 'function') return;
        hiddenLumenBtn = btn;
        btn.setAttribute(LUMEN_HID_ATTR, (btn.style && btn.style.display) || '');
      }
      if (btn.style) btn.style.display = 'none';
    }
    function watchLumenLauncher() {
      if (typeof w.MutationObserver !== 'function') return;
      const bar = findMenubar(document);
      if (!bar) return;
      // Menubar only (never body): Lumen's own button is inserted there, and the
      // callback is a no-op unless Luna is meant to hide it.
      try {
        const observer = new w.MutationObserver(() => { hideLumenLauncher(); });
        observer.observe(bar, { childList: true, subtree: true });
        life.watch(observer);
      } catch (_) { /* without an observer the button is still hidden once */ }
    }
    function setLumenOwnButton(on) {
      setLocalItem(LUMEN_BTN_KEY, on ? '1' : '0');
      savePref(LUMEN_BTN_PREF, Boolean(on));
      hideLumenLauncher();
      repaintIfOpen();
    }
    // Lumen's overlay and Luna's share a z-index; only one may be up. Close
    // Lumen through its own entry point so its focus trap and listeners run
    // down with it.
    function closeLumenMenu() {
      if (!document.getElementById('lumen-settings-overlay')) return;
      if (typeof w.__lumenCloseOverlay === 'function') {
        try { w.__lumenCloseOverlay(); } catch (_) { /* already gone */ }
        return;
      }
      // No public closer (an old Lumen): leave its DOM alone. Luna's overlay is
      // appended after it and paints on top, and Esc still closes Luna first.
    }
    /* Lumen builds its menu on demand and rebuilds it on every open, so Luna
       opens Lumen through Lumen's own entry points and never reparents it.
       The launcher is only clicked on the first attempt: it is a toggle in some
       Lumen builds, and a retry loop must not flick it shut again. */
    function openLumenOverlay(allowButton) {
      if (document.getElementById('lumen-settings-overlay')) return true;
      if (allowButton !== false) {
        const btn = document.getElementById('lumen-moon-btn');
        if (btn && typeof btn.click === 'function') {
          try { btn.click(); } catch (_) { /* try the entry point below */ }
        }
        if (document.getElementById('lumen-settings-overlay')) return true;
      }
      if (typeof w.__lumenOpenOverlay === 'function') {
        try { w.__lumenOpenOverlay(); } catch (_) { /* reported by the caller */ }
      }
      return Boolean(document.getElementById('lumen-settings-overlay'));
    }
    /* Door mode: Luna's teardown first (its close path restores the hidden
       Steam view), then Lumen's own window - its own element, its own handler.
       Nothing is re-parented, resized or restyled. Used when embedding is
       switched off, when the switch flipped this session, and by "Open Lumen". */
    function openLumenMenu() {
      lumenOpenError = '';
      closeMenu();
      if (openLumenOverlay()) return;
      // Neither door opened: say so in Luna rather than leaving a blank page.
      lumenOpenError = "Lumen's menu is not available right now \u2014 start Steam with Lumen running";
      showMenu();
    }

    /* ---- Embedded Lumen: Luna's window is the shell ---------------------
       The Lumen page no longer closes Luna. Lumen's overlay is opened once and
       then restyled into Luna's content rect: its sidebar hidden (Luna's
       sidebar lists Lumen's pages instead), its window filling the area, its
       backdrop gone and its z-index under Luna's. Every inline style this code
       writes is recorded first and restored on the way out, and the whole
       contract (overlay, .lumen-win, .lumen-side, .lumen-content, one
       .lumen-tab) is checked before a single style changes, so Lumen is never
       left half-restyled: any mismatch falls back to the door and says why. */
    const LUMEN_EMBED_PREF = 'lumen_embed';
    const LUMEN_EMBED_NOTE_ID = 'sls-luna-embed-note';
    const LUMEN_EMBED_HINT = 'Embed Lumen in Luna\u2019s window and Show Lumen\u2019s own menu button are both on the Settings page.';
    let lumenEmbedState = null;
    let lumenEmbedBlocked = '';
    let lumenEmbedObserver = null;
    let lumenEmbedProbe = '';
    let lumenLastTabNode = null;
    let lumenEmbedPending = false;
    let lumenEmbedRetry = null;
    let lumenEmbedRetryTries = 0;
    let lumenEmbedLogged = false;
    // Lumen's overlay can go away after it was embedded: Lumen tears its own
    // menu down when an action inside it throws, and its self-updater is
    // patched shut by Luna. Recovery is bounded per visit, never a loop.
    let lumenEmbedRecoveries = 0;
    let lumenEmbedUpdateHint = false;
    // Luna's content rect: measured once per open and again only after a
    // resize (rAF-throttled), never on every paint.
    let lumenRectCache = null;
    let lumenRectFrame = null;
    // Lumen opens its menu a moment after the click (it injects styles first),
    // so the embed gets ~2 s of short attempts before the page explains itself.
    const LUMEN_EMBED_TRIES = 20;
    const LUMEN_EMBED_EVERY = 100;
    const LUMEN_EMBED_RECOVERIES = 2;
    const LUMEN_UPDATER_NOTE = 'Lumen\u2019s own updater is blocked; use Luna \u2192 Updates';

    function lumenEmbedWanted() {
      const stored = prefValue(LUMEN_EMBED_PREF);
      if (typeof stored === 'boolean') return stored;
      return localItem(LUMEN_EMBED_KEY) !== '0';
    }

    /* One short line over Lumen's window: why embedding was skipped this
       session, and where the switch lives. Not a modal, not clickable, and it
       removes itself. */
    function showLumenEmbedNote(text) {
      const body = document.body;
      if (!body) return;
      const old = document.getElementById(LUMEN_EMBED_NOTE_ID);
      if (old && typeof old.remove === 'function') old.remove();
      const note = document.createElement('div');
      note.id = LUMEN_EMBED_NOTE_ID;
      note.className = 'sls-luna-embed-note';
      note.textContent = text;
      note.style.position = 'fixed';
      note.style.left = '50%';
      note.style.bottom = '28px';
      note.style.transform = 'translateX(-50%)';
      note.style.zIndex = '100001';
      note.style.maxWidth = '620px';
      note.style.padding = '10px 14px';
      note.style.borderRadius = '4px';
      note.style.background = '#26313c';
      note.style.color = '#e6e9ec';
      note.style.font = '13px/1.45 "Motiva Sans",Arial,Helvetica,sans-serif';
      note.style.boxShadow = '0 12px 32px rgba(0,0,0,.45)';
      note.style.pointerEvents = 'none';
      body.appendChild(note);
      life.timeout(() => {
        if (note.parentNode && typeof note.remove === 'function') note.remove();
      }, 9000);
    }

    const classTokens = node => String((node && node.className) || '').split(/\s+/).filter(Boolean);
    function findClasses(root, cls) {
      const hits = [];
      const walk = node => {
        if (!node) return;
        if (classTokens(node).indexOf(cls) >= 0) hits.push(node);
        const kids = node.children || [];
        for (let index = 0; index < kids.length; index += 1) walk(kids[index]);
      };
      walk(root);
      return hits;
    }
    const findClass = (root, cls) => {
      const hits = findClasses(root, cls);
      return hits.length ? hits[0] : null;
    };

    /* The shape embedding needs from Lumen. A reason means "do not touch":
       the caller takes the door instead. */
    function lumenShape() {
      const overlay = document.getElementById('lumen-settings-overlay');
      if (!overlay) return { reason: 'Lumen\u2019s menu is not in the page' };
      const win = findClass(overlay, 'lumen-win');
      if (!win) return { reason: 'Lumen\u2019s window (.lumen-win) is missing' };
      const side = findClass(overlay, 'lumen-side');
      if (!side) return { reason: 'Lumen\u2019s sidebar (.lumen-side) is missing' };
      const content = findClass(overlay, 'lumen-content');
      if (!content) return { reason: 'Lumen\u2019s content area (.lumen-content) is missing' };
      const tabs = findClasses(overlay, 'lumen-tab');
      if (!tabs.length) return { reason: 'Lumen\u2019s pages (.lumen-tab) are missing' };
      return { overlay: overlay, win: win, side: side, content: content, tabs: tabs };
    }

    const dashed = prop => String(prop).replace(/[A-Z]/g, ch => '-' + ch.toLowerCase());
    function styleOf(node, prop) {
      const style = node && node.style;
      if (!style) return '';
      if (typeof style.getPropertyValue === 'function') return style.getPropertyValue(dashed(prop)) || '';
      return style[prop] == null ? '' : String(style[prop]);
    }
    /* Records the original once per node and property, then writes ours. */
    function embedStyle(state, node, prop, value) {
      if (!node || !node.style) return;
      let bag = state.records.get(node);
      if (!bag) { bag = new Map(); state.records.set(node, bag); }
      if (!bag.has(prop)) bag.set(prop, styleOf(node, prop));
      try { node.style[prop] = value; } catch (_) { /* detached node */ }
    }

    /* Luna's content area (its scrolling body) in viewport coordinates. Cached:
       the shell is measured once when it opens, and again only after a resize.
       A paint never measures - it reuses the rect it was given. */
    function measureContentRect(force) {
      if (lumenRectCache && !force) return lumenRectCache;
      lumenRectCache = lumenContentRect();
      return lumenRectCache;
    }

    function lumenContentRect() {
      const luna = document.getElementById(OVERLAY_ID);
      if (!luna) return null;
      const slot = findClass(luna, 'sls-luna-body');
      if (!slot || typeof slot.getBoundingClientRect !== 'function') return null;
      let rect = null;
      try { rect = slot.getBoundingClientRect(); } catch (_) { rect = null; }
      if (!rect) return null;
      const width = Number(rect.width) || (Number(rect.right) - Number(rect.left)) || 0;
      const height = Number(rect.height) || (Number(rect.bottom) - Number(rect.top)) || 0;
      if (!(width > 0) || !(height > 0)) return null;
      return { top: Number(rect.top) || 0, left: Number(rect.left) || 0, width: width, height: height };
    }

    /* Apply (or re-apply) the embedded layout. Idempotent: calling it twice
       never re-records an original, and never stacks a second overlay. */
    function applyLumenEmbed() {
      if (!lumenEmbedWanted() || lumenEmbedBlocked) return false;
      const shape = lumenShape();
      if (!shape.overlay || shape.reason) return false;
      const rect = measureContentRect(false);
      if (!rect) return false; // Luna is not painted yet; paint() calls again
      if (lumenEmbedState && lumenEmbedState.overlay !== shape.overlay) restoreLumenEmbed();
      if (!lumenEmbedState) lumenEmbedState = { overlay: shape.overlay, records: new Map() };
      const state = lumenEmbedState;
      const px = value => value + 'px';
      embedStyle(state, shape.overlay, 'position', 'fixed');
      embedStyle(state, shape.overlay, 'top', px(rect.top));
      embedStyle(state, shape.overlay, 'left', px(rect.left));
      embedStyle(state, shape.overlay, 'right', 'auto');
      embedStyle(state, shape.overlay, 'bottom', 'auto');
      embedStyle(state, shape.overlay, 'width', px(rect.width));
      embedStyle(state, shape.overlay, 'height', px(rect.height));
      embedStyle(state, shape.overlay, 'boxSizing', 'border-box');
      embedStyle(state, shape.overlay, 'padding', '0');
      embedStyle(state, shape.overlay, 'margin', '0');
      embedStyle(state, shape.overlay, 'border', '0');
      embedStyle(state, shape.overlay, 'background', 'transparent');
      embedStyle(state, shape.overlay, 'boxShadow', 'none');
      // Above Luna's window: Lumen's pane is the content, Luna's frame is the
      // shell around it (Luna's own overlay stays at the stylesheet's 99999).
      embedStyle(state, shape.overlay, 'zIndex', '100000');
      // Only Lumen's window takes input: a click on the rest of the rect (or a
      // stale rect) reaches Luna's sidebar instead of dying on a backdrop.
      embedStyle(state, shape.overlay, 'pointerEvents', 'none');
      embedStyle(state, shape.win, 'width', '100%');
      embedStyle(state, shape.win, 'height', '100%');
      embedStyle(state, shape.win, 'maxWidth', 'none');
      embedStyle(state, shape.win, 'maxHeight', 'none');
      embedStyle(state, shape.win, 'borderRadius', '0');
      embedStyle(state, shape.win, 'boxShadow', 'none');
      embedStyle(state, shape.win, 'pointerEvents', 'auto');
      // Luna's sidebar replaces Lumen's: one list, one window.
      embedStyle(state, shape.side, 'display', 'none');
      embedStyle(state, shape.content, 'flex', '1 1 auto');
      embedStyle(state, shape.content, 'width', '100%');
      embedStyle(state, shape.content, 'minWidth', '0');
      state.rect = rect;
      attachLumenOverlayWatch();
      return true;
    }

    function restoreLumenEmbed() {
      const state = lumenEmbedState;
      lumenEmbedState = null;
      if (!state) return;
      state.records.forEach((bag, node) => {
        bag.forEach((value, prop) => {
          try {
            const style = node && node.style;
            if (!style) return;
            if (typeof style.removeProperty === 'function') {
              if (value) style.setProperty(dashed(prop), value);
              else style.removeProperty(dashed(prop));
            } else style[prop] = value || '';
          } catch (_) { /* the node went away with Lumen's overlay */ }
        });
      });
      state.records.clear();
    }

    /* Refuse to half-restyle. Sets the session flag so later visits take the
       door immediately, explains it once, and then does what the page did
       before embedding existed: close Luna and leave Lumen's own window up. */
    function fallBackFromLumenEmbed(reason) {
      lumenEmbedBlocked = String(reason || 'Lumen\u2019s menu did not match the embedded layout');
      lumenLastTabNode = null;
      restoreLumenEmbed();
      showLumenEmbedNote('Embedded Lumen is unavailable this session: ' + lumenEmbedBlocked
        + '. Lumen opens in its own window; the switch is Settings \u2192 Embed Lumen in Luna\u2019s window.');
      if (current() && document.getElementById(OVERLAY_ID)) closeMenu();
    }

    /* Leaving the page: stop the retry, put every style back, then close Lumen
       through its own entry point (that also runs its listeners and focus trap
       down). A pending open that Luna started is closed too: it was ours. */
    function leaveLumenEmbed() {
      const had = Boolean(lumenEmbedState) || lumenEmbedPending;
      stopLumenEmbedRetry();
      lumenEmbedPending = false;
      lumenLastTabNode = null;
      restoreLumenEmbed();
      detachLumenOverlayWatch();
      if (had) closeLumenMenu();
    }

    function stopLumenEmbedRetry() {
      if (lumenEmbedRetry === null) return;
      const id = lumenEmbedRetry;
      lumenEmbedRetry = null;
      try { (w.clearInterval || clearInterval)(id); } catch (_) { /* already gone */ }
    }

    /* Nothing could be embedded inside the budget: say why in the page (not a
       floating note), keep the door button, and log the reason once. */
    function embedNever(reason) {
      const why = String(reason || 'Lumen\u2019s menu did not settle into Luna\u2019s window');
      lumenEmbedPending = false;
      lumenEmbedBlocked = why;
      detachLumenOverlayWatch();
      if (!lumenEmbedLogged) {
        lumenEmbedLogged = true;
        lunaWarn('embedding Lumen is unavailable this session: ' + why, w);
      }
      if (tab === 'lumen') repaintIfOpen();
    }

    /* Lumen's menu appears a beat after it is asked for. Try the embed for a
       bounded time; the first success repaints so the sidebar grows Lumen's
       pages, and running out of attempts explains itself in the page. */
    function startLumenEmbedRetry() {
      if (lumenEmbedRetry !== null || lumenEmbedState || lumenEmbedBlocked) return;
      lumenEmbedPending = true;
      lumenEmbedRetryTries = 0;
      lumenEmbedRetry = life.interval(() => {
        lumenEmbedRetryTries += 1;
        if (lumenEmbedBlocked || !lumenEmbedWanted()) { stopLumenEmbedRetry(); lumenEmbedPending = false; return; }
        if (!document.getElementById('lumen-settings-overlay')) {
          openLumenOverlay(lumenEmbedRetryTries <= 1);
        }
        const shape = lumenShape();
        if (shape.overlay && shape.reason) {
          // Lumen is there but not the shape embedding needs. Give it the rest
          // of the budget in case it is still building, then take the door.
          if (lumenEmbedRetryTries >= LUMEN_EMBED_TRIES) {
            stopLumenEmbedRetry();
            lumenEmbedPending = false;
            fallBackFromLumenEmbed(shape.reason);
          }
          return;
        }
        if (applyLumenEmbed()) {
          stopLumenEmbedRetry();
          lumenEmbedPending = false;
          lumenOpenError = '';
          if (tab === 'lumen') repaintIfOpen();
          return;
        }
        if (lumenEmbedRetryTries >= LUMEN_EMBED_TRIES) {
          stopLumenEmbedRetry();
          embedNever(shape.reason || 'Luna could not measure the space for it');
        }
      }, LUMEN_EMBED_EVERY);
    }

    const lumenSubtabsWanted = () => tab === 'lumen' && lumenEmbedWanted() && !lumenEmbedBlocked
      && Boolean(document.getElementById('lumen-settings-overlay'));

    /* Which Lumen page is showing: the one last clicked here, else the first
       visible panel (Lumen builds all panels and toggles them by display). */
    function lumenActiveIndex(shape) {
      const tabs = (shape && shape.tabs) || [];
      if (!tabs.length) return -1;
      if (lumenLastTabNode) {
        const known = tabs.indexOf(lumenLastTabNode);
        if (known >= 0) return known;
      }
      const panels = findClasses(shape.overlay, 'lumen-tab-panel');
      for (let index = 0; index < panels.length && index < tabs.length; index += 1) {
        const style = panels[index].style || {};
        if ((style.display && style.display !== 'none')
          || classTokens(panels[index]).indexOf('active') >= 0) return index;
      }
      return -1;
    }

    /* Select a Lumen page from Luna's sidebar: highlight it, make sure the pane
       is actually laid into Luna's window (retrying like the entry does), then
       forward the click to Lumen's own tab - by index, because Lumen may have
       rebuilt its overlay since this sub-entry was drawn. */
    function selectLumenSubPage(tabNode, index) {
      lumenLastTabNode = tabNode || lumenLastTabNode;
      if (!lumenEmbedState && !lumenEmbedBlocked && lumenEmbedWanted()) {
        if (!document.getElementById('lumen-settings-overlay')) openLumenOverlay();
        applyLumenEmbed();
        if (!lumenEmbedState) { lumenEmbedPending = true; startLumenEmbedRetry(); }
      }
      const shape = lumenShape();
      const tabs = shape.tabs || [];
      const target = tabs[index] || (tabNode && tabs.indexOf(tabNode) >= 0 ? tabNode : null);
      if (target) {
        lumenLastTabNode = target;
        if (typeof target.click === 'function') { try { target.click(); } catch (_) { /* gone */ } }
        else if (typeof target.onclick === 'function') { try { target.onclick(); } catch (_) { /* gone */ } }
      }
      repaintIfOpen();
    }

    /* Lumen's own page names, live from its sidebar, one nested entry each.
       Built with the same row builder as the categories: the only difference is
       `.sls-luna-subpage` (indent, smaller type). */
    function lumenSubEntries() {
      const shape = lumenShape();
      if (!shape.overlay || shape.reason) return [];
      const accountActive = lumenAccountVisible();
      const active = accountActive ? -1 : lumenActiveIndex(shape);
      const rows = shape.tabs.map((tabNode, index) => {
        const sub = sidebarRow({
          label: String(tabNode.textContent || '').trim() || ('Page ' + (index + 1)),
          active: index === active,
          sub: true,
          onclick: event => {
            if (event && typeof event.stopPropagation === 'function') event.stopPropagation();
            selectLumenSubPage(tabNode, index);
          },
        });
        return sub;
      });
      // Lumen's account page is the sixth panel and has no tab of its own; the
      // entry exists only while Lumen's sidebar carries that button.
      if (lumenAccountButton(shape.overlay)) {
        const account = sidebarRow({
          label: 'lua.tools account',
          active: accountActive,
          sub: true,
          onclick: event => {
            if (event && typeof event.stopPropagation === 'function') event.stopPropagation();
            openLumenAccountPage();
            repaintIfOpen();
          },
        });
        rows.push(account);
      }
      return rows;
    }

    /* Lumen rebuilds its overlay on every open and removes it on close, and it
       takes the overlay away when something inside it acts (its updater is
       patched shut). While Lumen is *our* content area, watch the direct
       children of the document body - the only place Lumen's overlay is added
       or removed - and nothing else. The observer is disconnected whenever
       nothing is embedded, so an idle Luna costs no callbacks at all. */
    function attachLumenOverlayWatch() {
      if (!lumenEmbedState || lumenEmbedBlocked || !lumenEmbedWanted()) return;
      if (tornDown || tab !== 'lumen' || !document.getElementById(OVERLAY_ID)) return;
      if (typeof w.MutationObserver !== 'function') return;
      const body = document.body;
      if (!body) return;
      if (!lumenEmbedObserver) {
        try {
          lumenEmbedObserver = new w.MutationObserver(() => onLumenOverlayMutations());
          life.watch(lumenEmbedObserver);
        } catch (_) { lumenEmbedObserver = null; return; }
      }
      if (lumenEmbedObserver.slsWatching) return;
      try {
        lumenEmbedObserver.observe(body, { childList: true });
        lumenEmbedObserver.slsWatching = true;
      } catch (_) { /* without the observer the retry below still runs */ }
    }

    function detachLumenOverlayWatch() {
      if (!lumenEmbedObserver || !lumenEmbedObserver.slsWatching) return;
      lumenEmbedObserver.slsWatching = false;
      try { lumenEmbedObserver.disconnect(); } catch (_) { /* already gone */ }
    }

    function onLumenOverlayMutations() {
      if (tornDown || !current()) return;
      if (tab !== 'lumen' || lumenEmbedBlocked || !lumenEmbedWanted()) {
        detachLumenOverlayWatch();
        return;
      }
      if (!document.getElementById(OVERLAY_ID)) { detachLumenOverlayWatch(); return; }
      const live = document.getElementById('lumen-settings-overlay');
      if (live) {
        // A fresh overlay (Lumen re-opens itself): lay it in, no recovery.
        if (!lumenEmbedState || lumenEmbedState.overlay !== live) applyLumenEmbed();
        return;
      }
      if (!lumenEmbedState) return;
      recoverLumenEmbed();
    }

    /* The embedded overlay disappeared. Re-open Lumen and lay it in again (a
       bounded number of times per visit); past that, say in plain words what
       happened, keep the Open Lumen button and name the blocked updater. The
       content area is never left empty. */
    function recoverLumenEmbed() {
      lumenLastTabNode = null;
      restoreLumenEmbed();
      lumenEmbedRecoveries += 1;
      if (!lumenEmbedLogged) {
        lumenEmbedLogged = true;
        lunaWarn('Lumen\u2019s menu disappeared while it was embedded; re-opening it', w);
      }
      if (lumenEmbedRecoveries > LUMEN_EMBED_RECOVERIES) {
        lumenEmbedUpdateHint = true;
        embedNever('Lumen\u2019s menu disappeared while it was embedded');
        return;
      }
      lumenEmbedPending = true;
      if (tab === 'lumen') repaintIfOpen(); // "Opening Lumen…", never an empty slot
      if (!openLumenOverlay(false)) { startLumenEmbedRetry(); return; }
      if (!applyLumenEmbed()) { startLumenEmbedRetry(); return; }
      lumenEmbedPending = false;
      if (tab === 'lumen') repaintIfOpen();
    }

    /* Steam's focus trap was registered against Lumen's overlay when Lumen
       opened it and is a closure that cannot be unbound from here. Ask Luna's
       own sidebar to take focus; if focus lands inside Lumen's window instead,
       embedding would leave the sidebar unreachable, so this session falls
       back to the door and says so. */
    function probeLumenTrap() {
      if (lumenEmbedProbe || !lumenEmbedState || !document || document.activeElement === undefined) return;
      lumenEmbedProbe = 'probing';
      const overlay = lumenEmbedState.overlay;
      life.timeout(() => {
        lumenEmbedProbe = 'done';
        if (!lumenEmbedState || lumenEmbedState.overlay !== overlay) return;
        const luna = document.getElementById(OVERLAY_ID);
        const target = luna && (findClass(luna, 'sls-luna-subpage')
          || findClass(luna, 'sls-luna-tab active') || findClass(luna, 'sls-luna-tab'));
        if (!target || typeof target.focus !== 'function') return;
        try { target.focus(); } catch (_) { /* repainted away */ }
        const active = document.activeElement;
        if (!active) return;
        if (active === overlay || (typeof overlay.contains === 'function' && overlay.contains(active))) {
          fallBackFromLumenEmbed('Steam\u2019s focus trap keeps focus inside Lumen');
        }
      }, 150);
    }

    /* Returns 'embedded' (Lumen is in Luna's window), 'door' (this session fell
       back; Luna is closed and Lumen's own window is up), 'off' (the switch is
       off), 'pending' (no measurable slot yet; paint() re-applies) or
       'missing' (Lumen is not running; the page explains it). */
    function enterLumenPage() {
      if (!lumenEmbedWanted() || lumenEmbedBlocked) return 'off';
      // The door was just tried and Lumen was not there: keep that line and the
      // button instead of starting another quiet retry behind the user's back.
      if (lumenOpenError) return 'missing';
      if (!openLumenOverlay()) {
        // Lumen often opens a moment later (it injects styles first); retry
        // instead of declaring it missing on the first look.
        lumenOpenError = '';
        lumenEmbedPending = true;
        startLumenEmbedRetry();
        return 'pending';
      }
      const shape = lumenShape();
      if (shape.reason) { fallBackFromLumenEmbed(shape.reason); return 'door'; }
      if (!applyLumenEmbed()) {
        startLumenEmbedRetry();
        return 'pending';
      }
      lumenEmbedPending = false;
      probeLumenTrap();
      return 'embedded';
    }

    /* The switch. Off restores Lumen's own window at once; on embeds the page
       that is already open without a repaint first. */
    function setLumenEmbed(on) {
      setLocalItem(LUMEN_EMBED_KEY, on ? '1' : '0');
      savePref(LUMEN_EMBED_PREF, Boolean(on));
      if (!on) leaveLumenEmbed();
      else if (tab === 'lumen' && document.getElementById(OVERLAY_ID)) enterLumenPage();
      repaintIfOpen();
    }
    /* Lumen's own button carries its last known update state in a pill; while
       that button is hidden this line is the only place it shows. Read-only
       DOM and state the page already has: no new RPC, no network call. */
    function lumenUpdateLine() {
      const findPill = node => {
        if (!node) return null;
        if (String(node.className || '').indexOf('lumen-auto-fix-pill-copy') >= 0) return node;
        for (const kid of (node.children || [])) {
          const hit = findPill(kid);
          if (hit) return hit;
        }
        return null;
      };
      const pill = findPill(document.getElementById('lumen-moon-btn'));
      const pillText = pill && String(pill.textContent || '').trim();
      const lumen = lunaLumen || {};
      const result = lumen.result || {};
      let text = '';
      if (pillText) text = 'Lumen reports: ' + pillText + '.';
      else if (lumen.job) text = 'A Lumen update is queued; it applies when Steam is closed.';
      else if (lumen.interrupted) text = 'An earlier Lumen update was interrupted.';
      else if (result.state === 'failed') text = 'The last Lumen update failed.';
      if (!text) return null;
      const line = document.createElement('p');
      line.className = 'sls-luna-note';
      line.textContent = text;
      return line;
    }

    const lumenOpenButton = () => {
      const open = document.createElement('button');
      open.className = 'sls-luna-btn';
      open.textContent = 'Open Lumen';
      open.onclick = openLumenMenu;
      return open;
    };

    function lumenBody() {
      const body = document.createElement('div');
      // The account hand-over line comes first, so it is visible even while the
      // embedded Lumen page is still opening.
      if (lumenSignInNote) {
        const note = document.createElement('p');
        note.className = 'sls-luna-note';
        note.textContent = lumenSignInNote;
        body.appendChild(note);
      }
      // Lumen's menu is on its way in: one short line, not the whole intro.
      if (lumenEmbedPending) {
        const opening = document.createElement('p');
        opening.className = 'sls-luna-note';
        opening.textContent = 'Opening Lumen\u2026';
        body.appendChild(opening);
        return body;
      }
      // Embedding could not land: the reason in plain words, then the door.
      if (lumenEmbedBlocked) {
        const blocked = document.createElement('p');
        blocked.className = 'sls-luna-note';
        blocked.textContent = 'Embedded Lumen is unavailable this session: ' + lumenEmbedBlocked
          + '. Luna could not put Lumen\u2019s menu inside this window.';
        body.appendChild(blocked);
        if (lumenOpenError) {
          const failed = document.createElement('p');
          failed.className = 'sls-luna-error';
          failed.textContent = lumenOpenError;
          body.appendChild(failed);
        }
        const row = document.createElement('div');
        row.className = 'sls-luna-row';
        row.appendChild(lumenOpenButton());
        body.appendChild(row);
        if (lumenEmbedUpdateHint) {
          // One line: the likely cause, and where the working update lives.
          const hint = document.createElement('p');
          hint.className = 'sls-luna-note';
          hint.textContent = LUMEN_UPDATER_NOTE;
          body.appendChild(hint);
        }
        const settings = document.createElement('p');
        settings.className = 'sls-luna-note';
        settings.textContent = LUMEN_EMBED_HINT;
        body.appendChild(settings);
        return body;
      }

      // Embedded: Lumen's pane covers this area, so Luna paints nothing behind
      // it. No intro, no flash, and nothing shows through Lumen's transparent
      // areas. The intro belongs to the door and to the blocked states above.
      if (lumenEmbedState) return body;

      const note = document.createElement('p');
      note.className = 'sls-luna-note';
      note.textContent = 'Lumen is the plugin host and owns the LuaTools / SLSsteam settings; '
        + 'updates are on the Updates tab.';
      body.appendChild(note);

      const row = document.createElement('div');
      row.className = 'sls-luna-row';
      row.appendChild(lumenOpenButton());
      body.appendChild(row);

      if (lumenOpenError) {
        const failed = document.createElement('p');
        failed.className = 'sls-luna-error';
        failed.textContent = lumenOpenError;
        body.appendChild(failed);
      }
      const state = lumenUpdateLine();
      if (state) body.appendChild(state);

      const terminal = document.createElement('p');
      terminal.className = 'sls-luna-note';
      terminal.textContent = 'The same is available in a terminal: luna lumen status to see what is '
        + 'installed, luna lumen update to update.';
      body.appendChild(terminal);

      // One line for both switches; no second settings sentence in the page.
      const settings = document.createElement('p');
      settings.className = 'sls-luna-note';
      settings.textContent = LUMEN_EMBED_HINT;
      body.appendChild(settings);
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
      health: 'Health (local diagnostics)', updates: 'Updates', lumen: 'Lumen', settings: 'Settings', about: 'About SLS Luna' };

    /* ---- Shell and page cache -------------------------------------------
       The window (overlay, sidebar, header, body slot) is built once per open
       and reused for every tab switch, so switching never destroys the DOM the
       user is looking at and never flashes. Each category's page is built the
       first time it is shown and cached; Luna's content rect is measured once,
       when the shell opens, and only again after a resize. */
    let shell = null;
    let shellBodyCache = new Map();
    let shellDataRev = 0;
    let shellSideSig = '';
    let pendingReveal = false;

    function buildShell() {
      if (shell && document.getElementById(OVERLAY_ID)) return shell;
      shellBodyCache = new Map();
      shellSideSig = '';
      const node = document.createElement('div');
      node.id = OVERLAY_ID;
      // Laid out but not shown until showMenu's first frame: Lumen can be
      // measured and embedded before anything flashes.
      if (pendingReveal && node.style) node.style.visibility = 'hidden';
      const win = document.createElement('div');
      win.className = 'sls-luna-win';
      const side = document.createElement('div');
      side.className = 'sls-luna-side';
      const title = document.createElement('div');
      title.className = 'sls-luna-side-title';
      title.textContent = 'Luna';
      side.appendChild(title);
      const main = document.createElement('div');
      main.className = 'sls-luna-main';
      const head = document.createElement('div');
      head.className = 'sls-luna-head';
      const heading = document.createElement('h3');
      // Only shown when the view could not be hidden and Steam had to be moved
      // to the Library instead: one short line, never a permanent banner.
      const viewLine = document.createElement('div');
      viewLine.className = 'sls-luna-view-note';
      if (viewNote) viewLine.textContent = viewNote;
      else if (viewLine.style) viewLine.style.display = 'none';
      const refreshButton = document.createElement('button');
      refreshButton.className = 'sls-luna-btn secondary';
      refreshButton.textContent = 'Refresh';
      refreshButton.onclick = refresh;
      const close = document.createElement('button');
      close.className = 'sls-luna-x';
      close.textContent = '\u2715';
      close.onclick = closeMenu;
      head.append(heading, viewLine, refreshButton, close);
      const body = document.createElement('div');
      body.className = 'sls-luna-body';
      main.append(head, body);
      win.append(side, main);
      node.appendChild(win);
      node.addEventListener('click', event => { if (event.target === node) closeMenu(); });
      document.body.appendChild(node);
      shell = { overlay: node, win: win, side: side, main: main, head: head, heading: heading,
        viewNote: viewLine, refreshButton: refreshButton, body: body, title: title,
        spacer: null, account: null };
      overlay = node;
      return shell;
    }

    function revealShell() {
      pendingReveal = false;
      if (!shell || !shell.overlay.style) return;
      shell.overlay.style.visibility = '';
    }

    // The fallback line can arrive after the shell is painted (the agent
    // answered late), so it is refreshed on every open rather than only built.
    function syncViewNote() {
      if (!shell || !shell.viewNote) return;
      if (viewNote) shell.viewNote.textContent = viewNote;
      else if (shell.viewNote.style) shell.viewNote.style.display = 'none';
    }

    function bodyNode(id) {
      return id === 'presence' ? presenceBody()
        : id === 'compressor' ? compressorBody()
        : id === 'hltb' ? hltbBody()
        : id === 'steamgrid' ? steamgridBody()
        : id === 'graphics' ? graphicsBody()
        : id === 'health' ? healthBody()
        : id === 'updates' ? updatesBody()
        : id === 'lumen' ? lumenBody()
        : id === 'settings' ? settingsBody()
        : id === 'about' ? aboutBody() : playtimeBody();
    }

    /* The sidebar changes only when its labels, the active row or Lumen's page
       list change. Rows are rebuilt then; a plain tab switch only moves the
       `active` class, so the same DOM stays on screen. */
    function sidebarSignature() {
      const parts = visibleNav().map(entry => entry.id + ':' + entry.label
        + (entry.id === 'updates' && updateBadge() ? ' *' : ''));
      parts.push('>' + tab);
      if (tab === 'lumen' && lumenSubtabsWanted()) {
        const shape = lumenShape();
        if (shape.overlay && !shape.reason) {
          const active = lumenActiveIndex(shape);
          parts.push('~' + shape.tabs.map((tabNode, index) =>
            (index === active ? '[' : '') + String(tabNode.textContent || '').trim()).join('|')
            + '^' + (lumenAccountVisible() ? 'account' : '')
            + (lumenAccountButton(shape.overlay) ? '+entry' : ''));
        }
      }
      const account = (snapshot && snapshot.account) || '';
      parts.push('@' + account + (connected() ? ' ok' : ''));
      parts.push('#' + (luatools ? String(luatools.labelKey || '') + ':' + String(luatools.name || '')
        + ':' + String(luatools.avatar || '') : 'unknown'));
      return parts.join(';');
    }

    function syncSidebar() {
      const sig = sidebarSignature();
      if (sig === shellSideSig) return;
      shellSideSig = sig;
      // HTMLCollection has no slice(): iterate a copy so removals are safe.
      for (const node of Array.from(shell.side.children || [])) {
        if (node === shell.title) continue;
        if (typeof node.remove === 'function') node.remove();
      }
      sidebar().forEach(item => shell.side.appendChild(item));
      shell.spacer = document.createElement('div');
      shell.spacer.className = 'sls-luna-spacer';
      shell.side.appendChild(shell.spacer);
      shell.account = accountRow();
      shell.side.appendChild(shell.account);
    }

    /* Show a category. `fresh` rebuilds that page from current state (a paint
       after data landed); without it, a cached page is shown by toggling
       display and only the first visit builds anything. */
    function syncShell(fresh) {
      syncSidebar();
      shell.heading.textContent = TITLES[tab] || tab;
      let entry = shellBodyCache.get(tab);
      if (!entry || fresh || entry.rev !== shellDataRev || !entry.node.parentNode) {
        const node = bodyNode(tab);
        const pane = document.createElement('div');
        pane.className = 'sls-luna-pane';
        pane.appendChild(node);
        if (entry && entry.node && typeof entry.node.remove === 'function') entry.node.remove();
        shell.body.appendChild(pane);
        entry = { node: pane, rev: shellDataRev };
        shellBodyCache.set(tab, entry);
      }
      // Exactly one pane on screen: hide every other child of the body slot, so
      // a stale or replaced pane can never stack under the active one.
      Array.from(shell.body.children || []).forEach(node => {
        if (node !== entry.node && node.style) node.style.display = 'none';
      });
      if (entry.node.style) entry.node.style.display = '';
    }

    function selectCategory(id) {
      if (!document.getElementById(OVERLAY_ID)) return;
      // The artwork pass is polled only while its tab is on screen.
      if (id !== 'steamgrid') stopSgdbAuto();
      const leaving = tab === 'lumen' && id !== 'lumen';
      tab = id;
      confirmAction = null;
      cancelArmed = false;
      cleanupArmed = false;
      if (leaving) leaveLumenEmbed();
      if (id === 'lumen') {
        const entered = enterLumenPage();
        // 'door': the page fell back to Lumen's own window, which closed Luna.
        // Returned so a caller can tell whether the menu is still there.
        if (entered === 'door') return 'door';
      }
      if (!shell || !document.getElementById(OVERLAY_ID)) { paint(); return; }
      syncShell(false);
      if (tab === 'lumen' && lumenEmbedState) applyLumenEmbed();
      // The page on screen decides what is read: a switch starts that page's
      // own pass (one per page, so a fast second click joins the first). The
      // shell set is already in memory from the open, so a switch never
      // re-reads the footer or the sidebar.
      refreshPage(tab);
      // Back on the tab: resume following the same job, no second start.
      if (tab === 'steamgrid' && sgdbAuto && sgdbAuto.state === 'running') pollSgdbAuto();
    }

    function paint() {
      const nav = visibleNav();
      if (!nav.some(entry => entry.id === tab)) tab = 'settings';
      // The "Lumen is not running" line belongs to one visit; leaving the page
      // drops it, so a later open never shows a stale failure.
      if (tab !== 'lumen') { lumenOpenError = ''; lumenSignInNote = ''; }
      // Lumen's UI only lives in this window while its page is on screen.
      if (tab !== 'lumen') leaveLumenEmbed();
      if (tab !== 'steamgrid') stopSgdbAuto();
      const restoreSearch = document.activeElement === search && tab === 'playtime';
      const selection = [search.selectionStart, search.selectionEnd];
      buildShell();
      syncShell(true);
      // The rect is cached: this re-lays Lumen into it, it never measures.
      if (tab === 'lumen' && lumenEmbedState) applyLumenEmbed();
      if (restoreSearch) {
        search.focus();
        if (typeof search.setSelectionRange === 'function') search.setSelectionRange(...selection);
      }
    }

    /* ---- Page data ------------------------------------------------------
       Every read below is one promise. A page asks for the shell set plus what
       it actually shows, and the scheduler starts them together: a tab switch
       then waits for the slowest read instead of for their sum. `load()` keeps
       one call per name in flight, so a switch, a poll and a write racing each
       other never send the same RPC twice. */
    const SHELL_LOADS = ['playtime', 'presence', 'info', 'luatools', 'update'];

    // What a page really shows. The HowLongToBeat cache is megabytes on a cold
    // read: it is requested for its own page, and otherwise only once a library
    // shelf is actually sorted by it (the walk below sees sort 12).
    function pageLoads(id) {
      let names;
      if (id === 'hltb') names = ['hltb'];
      else if (id === 'compressor') names = ['compressor'];
      else if (id === 'steamgrid') names = ['sgdb', 'steam'];
      else if (id === 'graphics') names = ['graphics'];
      else if (id === 'health') names = ['health', 'steam', 'host', 'lumen', 'refresh'];
      else if (id === 'updates') names = ['health', 'steam', 'host', 'lumen', 'refresh'];
      else if (id === 'lumen') names = ['host', 'lumen', 'steam'];
      else if (id === 'settings') names = ['host', 'lumen', 'steam', 'refresh'];
      else names = [];
      if (shelfWantsHltb && names.indexOf('hltb') < 0) names = names.concat('hltb');
      return names;
    }

    const loads = {
      playtime: () => readPlaytime(),
      presence: async () => {
        try {
          presence = await rpc(w, 'SlsPresenceGet');
        } catch (error) {
          presence = presence || {};
        }
      },
      compressor: async () => {
        try {
          // The per-game list and the statistics are only needed by their own
          // tabs; every other view asks for the light shape, which leaves both
          // out.
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
      },
      hltb: async () => {
        try {
          hltb = await readHltb();
        } catch (error) {
          hltb = hltb || {};
        }
      },
      sgdb: async () => {
        try {
          sgdb = await rpc(w, 'SlsSgdbGet');
          sgdbReadError = '';
        } catch (error) {
          sgdb = sgdb || {};
          sgdbReadError = String(error && error.message ? error.message : error);
        }
      },
      graphics: async () => {
        try {
          // Refresh is an explicit request: bypass the backend's report cache so
          // a newly attached GPU or lsfg config shows up now, not 30 s later.
          graphics = await rpc(w, 'SlsGraphicsGet', { raw: JSON.stringify({ force: true }) });
          graphicsCheckedAt = Number(graphics && graphics.checkedAt) || Math.floor(Date.now() / 1000);
        } catch (error) {
          graphics = graphics || {};
        }
      },
      info: async () => {
        try {
          lunaInfo = await rpc(w, 'SlsLunaInfo');
          delete rpcErrors.info;
        } catch (error) {
          rpcErrors.info = errorText(error);
          lunaInfo = lunaInfo || {};
        }
        await migratePrefs();
      },
      health: async () => {
        try {
          const report = await rpc(w, 'SlsLunaHealth');
          lunaHealth = (report && report.health) || report || {};
          delete rpcErrors.health;
        } catch (error) {
          rpcErrors.health = errorText(error);
          lunaHealth = lunaHealth || {};
        }
      },
      update: async () => {
        try {
          const out = await rpc(w, 'SlsLunaUpdate', { raw: JSON.stringify({ action: 'status' }) });
          lunaUpdate = (out && out.update) || lunaUpdate || {};
          delete rpcErrors.update;
        } catch (error) {
          rpcErrors.update = errorText(error);
          lunaUpdate = lunaUpdate || {};
        }
        // A real check talks to GitHub through the updater; the Python side
        // caches it for six hours, and opening the menu throttles it anyway.
        // "Off" means never check: Health then only shows the last known status.
        if ((lunaUpdate.autoUpdate || 'off') !== 'off' && Date.now() - lunaCheckedAt > 30 * 60 * 1000) {
          lunaCheckedAt = Date.now();
          try {
            const out = await rpc(w, 'SlsLunaUpdate', { raw: JSON.stringify({ action: 'check' }) });
            if (out && out.update) lunaUpdate = out.update;
            followUpdateCheck();
          } catch (_) { /* offline or gh missing; the status stays */ }
        }
      },
      steam: async () => {
        try {
          const steamOut = await rpc(w, 'SlsLunaSteam', { raw: JSON.stringify({ action: 'status' }) });
          lunaSteam = (steamOut && steamOut.steam) || lunaSteam || {};
          delete rpcErrors.steam;
        } catch (error) {
          rpcErrors.steam = errorText(error);
          lunaSteam = lunaSteam || {};
        }
      },
      host: async () => {
        try {
          const hostOut = await rpc(w, 'SlsLunaHost', {});
          lunaHost = (hostOut && hostOut.host) || lunaHost || {};
          delete rpcErrors.host;
        } catch (error) {
          rpcErrors.host = errorText(error);
          lunaHost = lunaHost || {};
        }
      },
      lumen: async () => {
        // The host decides whether Lumen's own status is worth reading at all.
        await load('host');
        if ((lunaHost.configured || lunaHost.running) === 'lumen') {
          try {
            const lumenOut = await rpc(w, 'SlsLunaLumen', { raw: JSON.stringify({ action: 'status' }) });
            lunaLumen = (lumenOut && lumenOut.lumen) || lunaLumen || {};
            delete rpcErrors.lumen;
          } catch (error) {
            rpcErrors.lumen = errorText(error);
            lunaLumen = lunaLumen || {};
          }
          // The channels come from `luna lumen channels`, which starts a bounded
          // CLI read: once per page session, not on every status poll.
          if (lunaChannels === null && !channelsBusy && !channelsTried) {
            channelsBusy = true;
            channelsTried = true;
            try {
              const chanOut = await rpc(w, 'SlsLunaLumen', { raw: JSON.stringify({ action: 'channels' }) });
              if (chanOut && chanOut.channels) lunaChannels = chanOut.channels;
              channelsError = '';
            } catch (error) {
              channelsError = errorText(error);
            }
            channelsBusy = false;
          }
        }
      },
      // Footer connection: LuaTools' own signed-in session, read by the
      // backend. A failed RPC is "unavailable", never "connected".
      luatools: async () => {
        try {
          const out = await rpc(w, 'SlsLunaLuaTools', {});
          if (out && out.success === false) throw new Error(out.error || 'not available');
          const state = (out && out.luatools) || {};
          luatools = { signedIn: state.signedIn === true, name: String(state.name || ''),
            avatar: String(state.avatar || ''), expired: state.expired === true,
            path: String(state.path || ''),
            error: String(state.error || ''),
            labelKey: state.signedIn === true ? 'signedIn' : 'out' };
        } catch (error) {
          luatools = { unavailable: true, signedIn: false, name: '',
            error: 'the LuaTools state is not readable here: ' + errorText(error), labelKey: 'unavailable' };
        }
      },
      refresh: async () => {
        try {
          const refreshOut = await rpc(w, 'SlsLunaRefresh', { raw: JSON.stringify({ action: 'status' }) });
          lunaRefresh = refreshOut || lunaRefresh || {};
          delete rpcErrors.refresh;
        } catch (error) {
          rpcErrors.refresh = errorText(error);
          lunaRefresh = lunaRefresh || {};
        }
      },
    };

    const loading = new Map();
    function load(name) {
      const existing = loading.get(name);
      if (existing) return existing;
      const fn = loads[name];
      if (!fn) return Promise.resolve(false);
      const promise = Promise.resolve().then(fn).then(() => true, () => false)
        .then(done => { loading.delete(name); return done; });
      loading.set(name, promise);
      return promise;
    }

    /* One pass per page at a time, and one repaint at the end of it: a pass
       that began for a page the user has already left never paints the pane
       now on screen. */
    function runPass(wanted, names) {
      return Promise.allSettled(names.map(name => load(name)))
        .then(() => {
          try { refreshLibraryShelves(); } catch (_) { /* shelves render later */ }
          maybeAutoFetch();
          if (!current() || tab !== wanted) return false;
          if (!document.getElementById(OVERLAY_ID)) return false;
          // New data for this page: its cached pane is stale and is rebuilt now.
          shellDataRev += 1;
          repaintIfOpen();
          return true;
        });
    }

    const refreshing = new Map();
    // Shell + page: the open, the Refresh button and every write go through
    // this, so what the user just changed is read again right away.
    function refresh() {
      const wanted = tab;
      const running = refreshing.get(wanted);
      if (running) return running;
      const pass = runPass(wanted, SHELL_LOADS.concat(pageLoads(wanted)))
        .then(result => { refreshing.delete(wanted); return result; },
          () => { refreshing.delete(wanted); return false; });
      refreshing.set(wanted, pass);
      return pass;
    }

    // A tab switch fetches only what that page shows.
    const pageRefreshing = new Map();
    function refreshPage(id) {
      const names = pageLoads(id);
      if (!names.length) return Promise.resolve(false);
      const running = pageRefreshing.get(id);
      if (running) return running;
      const pass = runPass(id, names)
        .then(result => { pageRefreshing.delete(id); return result; },
          () => { pageRefreshing.delete(id); return false; });
      pageRefreshing.set(id, pass);
      return pass;
    }

    let opening = false;
    function showMenu() {
      const existing = document.getElementById(OVERLAY_ID);
      if (existing) {
        // A hidden shell is a half-built one from a failed open: rebuild it
        // instead of returning early and leaving the menu dead for the session.
        if (existing.style && existing.style.visibility === 'hidden') {
          existing.remove();
          shell = null;
          overlay = null;
          shellBodyCache = new Map();
          shellSideSig = '';
        } else {
          return;
        }
      }
      lumenEmbedRecoveries = 0;
      lumenEmbedUpdateHint = false;
      lumenRectCache = null;
      // If Lumen's own menu is already up and embedding is on, Luna opens on
      // its Lumen page with that menu inside it instead of closing it.
      const lumenAlreadyOpen = Boolean(document.getElementById('lumen-settings-overlay'));
      if (lumenAlreadyOpen && lumenEmbedWanted() && !lumenEmbedBlocked) tab = 'lumen';
      // One menu at a time. Lumen's overlay shares Luna's z-index, so it is
      // closed through its own entry point before Luna paints - unless the
      // Lumen page is about to embed it, in which case it stays up and is
      // restyled after Luna paints.
      const embedding = tab === 'lumen' && lumenEmbedWanted() && !lumenEmbedBlocked;
      if (!embedding) closeLumenMenu();
      // Hide the view when still on a browser route, or when the route is
      // unknown: the overlay lives in Steam's shell document, and Store,
      // Community and the built-in browser are drawn above it.
      try {
        // Build the window hidden, fill the first page, embed Lumen into the
        // measured rect, and only then show the first frame. No flash, no
        // empty content area and no second paint.
        pendingReveal = true;
        paint();
        syncViewNote();
        if (embedding) {
          const entered = enterLumenPage();
          if (entered === 'door') { pendingReveal = false; return; } // Lumen's own window is up
          // Lumen's pane now covers this area (or the retry is on): fill the
          // page slot once, still hidden, so the first frame is never the intro.
          syncShell(true);
        }
        revealShell();
        refresh();
      } catch (err) {
        pendingReveal = false;
        restoreMainView(w);
        // Never leave a hidden, half-built shell behind: it would make every
        // later click a no-op.
        const stale = document.getElementById(OVERLAY_ID);
        if (stale) stale.remove();
        shell = null;
        overlay = null;
        shellBodyCache = new Map();
        shellSideSig = '';
        throw err;
      }
    }

    // Where the shell document is ours, the menu opens in place - Library,
    // Store, Community, Profile: no navigation, nothing to wait for. The
    // browser view is hidden first (through SharedJSContext), which is what
    // makes the overlay clickable on Store and Community; navigation to the
    /* Store/Community first: a browser view over the shell eats every click,
       even when it is hidden, so Steam has to move to the Library before the
       menu can be used. When the route is already known to be a browser one the
       switch happens before the paint; when the only evidence is a cached
       answer the menu opens at once and the live route confirms it - a stale
       cache is corrected within a beat instead of leaving a dead menu. */
    function openMenu() {
      if (opening) return;
      const existing = document.getElementById(OVERLAY_ID);
      // A hidden leftover is a failed open, not a menu: showMenu rebuilds it.
      if (existing && !(existing.style && existing.style.visibility === 'hidden')) return;
      const direct = currentRoute(w);
      const known = rememberView(w);
      const route = direct !== null ? direct : (known && known.ok ? known.route : null);
      if (onBrowserRoutePath(route)) {
        opening = true;
        const done = () => {
          opening = false;
          if (tornDown) return;
          showMenu();
          syncViewNote();
        };
        // prepareView navigates (never hides) and only then is the menu painted.
        prepareView(w).then(done, done);
        return;
      }
      showMenu();
      followView(w);
    }

    /* The menu is on screen: ask the live route and, on a browser page, ask
       Steam to show the Library. Never hides the view, never navigates back. */
    function followView(w) {
      requestView(w, 'status').then(status => {
        if (!status || status.ok !== true) return;
        if (!onBrowserRoutePath(status.route)) return;
        return requestView(w, 'navigate', VIEW_NAVIGATE_TIMEOUT_MS).then(out => {
          viewNote = out && out.ok === true ? '' : VIEW_LIBRARY_NOTE;
          syncViewNote();
        }).catch(() => {
          viewNote = VIEW_LIBRARY_NOTE;
          syncViewNote();
        });
      }).catch(() => { /* no agent: nothing can be moved from here */ });
    }

    function closeMenu() {
      stopJobPolling();
      confirmAction = null;
      cancelArmed = false;
      cleanupArmed = false;
      pendingReveal = false;
      stopSgdbAuto();
      // An armed "Confirm undo" never survives a close: reopening the picker
      // starts from a plain Undo again.
      sgdbUndoArmed = null;
      sgdbResetArmed = 0;
      // Lumen goes back to its own window (and every style Luna wrote on it is
      // restored) before Luna's overlay goes away.
      leaveLumenEmbed();
      detachLumenOverlayWatch();
      // A fetch pass deliberately keeps running in the background; the menu
      // shows its progress again the next time it is opened.
      const node = document.getElementById(OVERLAY_ID);
      if (node) node.remove();
      shell = null;
      overlay = null;
      shellBodyCache = new Map();
      shellSideSig = '';
      lumenRectCache = null;
      lumenEmbedRecoveries = 0;
      lumenEmbedUpdateHint = false;
      viewNote = '';
      restoreMainView(w);
    }

    // Read-only probe for `luna selftest` and `luna doctor --report`: the
    // categories that exist and what is on screen right now. It cannot click,
    // switch, write or navigate anything - the selftest does its own clicking
    // through the DOM (`element.click()`), so this only has to answer.
    w.__SlsLunaSelftest = {
      tabs: () => visibleNav().map(entry => ({ id: entry.id, label: entry.label })),
      state: () => ({
        tab,
        open: Boolean(document.getElementById(OVERLAY_ID)),
        build: PANEL_BUILD,
        embedding: Boolean(lumenEmbedState && lumenEmbedState.overlay),
        hiddenView: Boolean(viewHiddenByLuna),
        route: currentRoute(w) || '',
        problems: PROBLEMS.slice(0),
        // Shelf-walk cost, so `luna selftest` (and a human) can measure it live:
        // passes started, their summed wall time, and mutation callbacks seen.
        walks: shelfStats.walks,
        walkMsTotal: Math.round(shelfStats.walkMsTotal * 10) / 10,
        observerCallbacks: shelfStats.observerCallbacks,
        shelfObserver: Boolean(shelfObserverRoot),
      }),
    };

    // Retiring this generation: every handle below is registered with `life`,
    // so the next mount (or a page reload) starts from a clean page.
    w.__SlsLunaTeardown = () => {
      tornDown = true;
      // Give Lumen back its own inline styles and drop the overlay observer;
      // Lumen's window itself is left where the user can see it.
      lumenLastTabNode = null;
      stopLumenEmbedRetry();
      stopSgdbAuto();
      lumenEmbedPending = false;
      detachLumenOverlayWatch();
      lumenEmbedObserver = null;
      restoreLumenEmbed();
      shell = null;
      overlay = null;
      shellBodyCache = new Map();
      lumenRectCache = null;
      pendingReveal = false;
      // Give Lumen's launcher back before the observer that watches it goes.
      restoreLumenLauncher();
      life.teardown();
      // The shelf refresh keeps a timeout and an observer of its own; drop both
      // with the page, so a retired panel never watches or walks again.
      if (shelfTimer) {
        try { (w.clearTimeout || clearTimeout)(shelfTimer); } catch (_) { /* already fired */ }
        shelfTimer = null;
      }
      if (shelfWalkTimer) {
        try { (w.clearTimeout || clearTimeout)(shelfWalkTimer); } catch (_) { /* already fired */ }
        shelfWalkTimer = null;
      }
      if (shelfIdleHandle !== null) {
        try { if (typeof w.cancelIdleCallback === 'function') w.cancelIdleCallback(shelfIdleHandle); } catch (_) {}
        shelfIdleHandle = null;
      }
      if (shelfObserver) {
        try { shelfObserver.disconnect(); } catch (_) { /* already dropped */ }
        shelfObserver = null;
      }
      shelfObserverRoot = null;
      shelfQueue = [];
      shelfPendingRoots = [];
      shelfSeen = new WeakSet();
      shelfScheduled = false;
      shelfPassRunning = false;
      try { stopJobPolling(); } catch (_) { /* never started */ }
      const node = document.getElementById(OVERLAY_ID);
      if (node && typeof node.remove === 'function') node.remove();
      viewNote = '';
      restoreMainView(w);
    };

    life.listen(w, 'pagehide', () => { restoreMainView(w); });

    life.listen(document, 'keydown', event => {
      if (event.key === 'Escape' && current()) closeMenu();
    });

    /* Lumen closes itself on a capture-phase document keydown. This one sits on
       the window's capture phase, so it runs before Lumen's and can stop the
       event there: while Lumen is embedded, one Esc closes Luna (and Lumen with
       it), never Lumen alone. Lumen's own handler and Steam's focus trap stay
       registered; nothing here tries to unbind them. */
    life.listen(w, 'keydown', event => {
      if (event.key !== 'Escape' || !lumenEmbedState || !current()) return;
      if (typeof event.preventDefault === 'function') event.preventDefault();
      if (typeof event.stopPropagation === 'function') event.stopPropagation();
      closeMenu();
    }, true);

    // A resize is the only thing besides opening that re-measures Luna's
    // content rect. One rAF at most is queued; nothing is measured per paint.
    function onLunaResize() {
      if (!shell) return;
      const raf = typeof w.requestAnimationFrame === 'function'
        ? w.requestAnimationFrame.bind(w) : null;
      if (!raf) {
        lumenRectCache = null;
        if (lumenEmbedState) applyLumenEmbed();
        return;
      }
      if (lumenRectFrame !== null) return;
      lumenRectFrame = raf(() => {
        lumenRectFrame = null;
        if (tornDown || !shell) return;
        lumenRectCache = null;
        if (lumenEmbedState) applyLumenEmbed();
      });
    }
    life.listen(w, 'resize', onLunaResize);

    mountButton(w, openMenu, life, () => {
      if (panelMounted) { hideLumenLauncher(); return; }
      panelMounted = true;
      hideLumenLauncher();
      watchLumenLauncher();
    });
    // Safety net: an add-on that mounted its floating panel before the flag was
    // set is removed on the next pass.
    // Skipped while hidden: no floating panel can appear on a window nobody is
    // looking at, the next visible pass gets it.
    life.interval(() => {
      if (tornDown === true || document.hidden === true) return;
      removeFloating(document);
    }, 4000);

    /* The Playtime page follows the ledger while it is the page on screen:
       readPlaytime sends the last `rev`, so a quiet backend answers 59 bytes and
       nothing is rebuilt. A poll that races the page's own load is skipped by
       the busy flag, and one that started for another page never paints. */
    /* The Graphics stamp follows the clock only while that page is on screen
       and visible. It writes one text node: no rebuild, no RPC, and the tick
       dies with the menu through the same `life` bookkeeping. */
    life.interval(() => {
      if (tornDown || document.hidden === true) return;
      if (tab !== 'graphics' || !document.getElementById(OVERLAY_ID)) return;
      if (graphicsAgeNode && graphicsAgeNode.parentNode) graphicsAgeNode.textContent = graphicsAge();
    }, 5000);

    life.interval(() => {
      if (tornDown || document.hidden === true) return;
      if (tab !== 'playtime' || !document.getElementById(OVERLAY_ID)) return;
      readPlaytime().then(changed => {
        if (!changed || !current() || tab !== 'playtime') return;
        if (!document.getElementById(OVERLAY_ID)) return;
        shellDataRev += 1;
        repaintIfOpen();
      });
    }, 5000);

    /* The Lumen job follows itself only while the Updates page is open and
       visible. Fast while a job runs or a check is out, then slower and slower
       when nothing changes; never the network check, only the status files. */
    life.interval(() => {
      // Returns on the first line while the menu is closed (no Updates tab, no
      // overlay), so the 2 s tick costs one property read.
      if (tornDown || lumenBusy || document.hidden === true) return;
      if (tab !== 'updates' || !document.getElementById(OVERLAY_ID)) return;
      const terminalStarting = Boolean(lunaLumen && lunaLumen.terminal
        && lunaLumen.terminal.state === 'starting');
      if ((lunaHost && (lunaHost.configured || lunaHost.running)) !== 'lumen' && !terminalStarting) return;
      const now = Date.now();
      if (now < lumenNextPoll) return;
      const view = lumenView();
      const active = view.key === 'running' || view.key === 'recovering' || view.checking
        || terminalStarting;
      lumenNextPoll = now + (active ? 2000 : Math.min(60000, 10000 * (1 + lumenIdlePolls)));
      pollLumen();
    }, 2000);
    // The agent's "Change artwork" right-click lands here as a storage event
    // (SharedJSContext writes it; the panel owns the window and opens the UI).
    // Only artwork keys wake the consumer; anything else is unrelated traffic.
    life.listen(w, 'storage', event => {
      try {
        if (event && typeof event.key === 'string') {
          if (event.key === 'slsLunaArtworkRequest') { consumeArtworkRequest(); return; }
          if (event.key === 'slsLunaHltbOrder') { scheduleShelfRefresh(0); return; }
          return;
        }
      } catch (_) { /* a bare emitter without a key still consumes */ }
      consumeArtworkRequest();
    });
    life.timeout(() => { consumeArtworkRequest(); }, 2500);
    // Home-shelf HLTB order lives in this window: re-scan after navigation and
    // re-apply after the cache or direction changes. No shared objects change.
    // Two early passes cover Steam's late shelf render; the observer and the
    // fallback pass below cover everything after that.
    life.timeout(() => { refreshLibraryShelves(); }, 3000);
    life.timeout(() => { refreshLibraryShelves(); }, 9000);
    /* Presence probe: Steam fires no route event in this document, so the
       observer's target is checked here. One getElementById (or one
       querySelector over the stable library class) per 2 s, nothing while the
       window is hidden; it re-binds only when the container actually changed. */
    life.interval(() => {
      if (tornDown === true || document.hidden === true) return;
      syncShelfObserver();
    }, 2000);
    /* Fallback pass for a shelf that arrived without a mutation record (a
       restored page, a Steam re-render): every 2 min, visible and focused only,
       and skipped outright unless the cheap signature changed. */
    life.interval(() => {
      if (tornDown === true || document.hidden === true) return;
      if (typeof document.hasFocus === 'function' && document.hasFocus() === false) return;
      if (shelfSignature() === shelfSig) return;
      if (!shelfEpochCurrent()) resetShelfWalk();
      requestShelfWalk([libraryRootFiber()]);
    }, SHELF_SAFETY_MS);
    life.listen(document, 'visibilitychange', () => {
      if (document.hidden !== true) { syncShelfObserver(); scheduleShelfRefresh(0); }
    });
    life.listen(w, 'focus', () => { syncShelfObserver(); });
    syncShelfObserver();
    // Resume a background fetch pass after a Steam restart without making the
    // user open the menu: load the local times and continue where it left off.
    life.timeout(async () => {
      // The local times are needed by a pass the user left running, and by a
      // library shelf actually sorted by HowLongToBeat. Without either, the
      // megabyte cache stays unread until its own page asks for it.
      if (!hltbBackground() && !shelfWantsHltb) return;
      try {
        hltb = await readHltb();
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

  return { mount, RPC_TIMEOUT, buildRows, buildHltbRows, dailyBars, weeklySentence, weekTop,
    minutesText, findMenubar, hours, findMainBrowser,
    hideMainView, restoreMainView, leaveBrowserRoute, prepareView, requestView, rememberView,
    onBrowserRoutePath, VIEW_REQUEST_KEY, VIEW_RESULT_KEY };
});
