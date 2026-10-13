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
    about: '<circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" stroke-width="2"/>'
      + '<path d="M12 11v5m0-8v.5" stroke="currentColor" stroke-width="2"/>',
  };
  const NAV = [
    { id: 'playtime', label: 'Playtime' },
    { id: 'presence', label: 'Presence' },
    { id: 'about', label: 'About' },
  ];
  // Floating panels the add-ons draw when Luna is not present.
  const FLOATING = ['sls-playtime-panel-button', 'sls-playtime-panel',
                    'sls-presence-panel-button', 'sls-presence-panel'];

  const parse = raw => (typeof raw === 'string' ? JSON.parse(raw) : raw);
  const hours = seconds => ((Number(seconds) || 0) / 3600).toFixed(2);

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
      '.sls-luna-error{color:#ff8080;font-size:13px;margin:8px 0 0;}',
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
    button.title = 'SLS Luna - settings for Playtime and Presence';
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
        if (out && out.success === false) throw Error(out.error || (method + ' failed'));
        return out;
      });
  }

  function mount(w) {
    const document = w.document;
    if (!document || w.__SlsLunaPanel) return;
    w.__SlsLunaPanel = true;
    // Tells the feature add-ons to stay quiet in the shell: their settings live
    // in this menu while it is present.
    w.__SlsLunaUi = true;
    styled(document);
    removeFloating(document);

    let overlay = null;
    let tab = 'playtime';
    let snapshot = null;
    let presence = null;
    let query = '';
    let visible = 50;
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

    const sidebar = () => NAV.map(entry => {
      const item = document.createElement('div');
      item.className = 'sls-luna-tab' + (entry.id === tab ? ' active' : '');
      const icon = document.createElement('span');
      icon.className = 'ico';
      icon.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true">' + ICON[entry.id] + '</svg>';
      const label = document.createElement('span');
      label.textContent = entry.label;
      item.append(icon, label);
      item.onclick = () => { tab = entry.id; paint(); };
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
      importButton.onclick = async () => {
        const raw = w.prompt('Paste a trusted SLS Playtime backup (replaces local records).');
        if (!raw || !w.confirm('Replace ALL locally recorded playtime?')) return;
        try {
          await rpc(w, 'SlsPlaytimeImport', { raw });
          await refresh();
        } catch (error) { showError(body, error); }
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
      cleanup.textContent = 'Disable and remove placeholders';
      cleanup.onclick = async () => {
        if (!w.confirm('Disable Presence and ask it to remove every placeholder it owns?')) return;
        try { await rpc(w, 'SlsPresenceRequestCleanup'); await refresh(); } catch (error) { showError(body, error); }
      };
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

    function aboutBody() {
      const body = document.createElement('div');
      const note = document.createElement('p');
      note.className = 'sls-luna-note';
      note.innerHTML = 'SLS Luna wraps the SLSsteam-Moon add-ons for Lumen: one installer, one boot hook, '
        + 'and this menu. Playtime counts non-Steam games and keeps every manifest editable; Presence '
        + 'publishes what you are playing. Neither one changes official Steam statistics.';
      body.appendChild(note);
      const versions = document.createElement('p');
      versions.className = 'sls-luna-note';
      versions.innerHTML = 'Add-ons loaded: <strong>'
        + ((snapshot && snapshot.manifestCount) ? 'playtime, ' : '') + (presence ? 'presence' : '')
        + '</strong> (from the bridge).';
      body.appendChild(versions);
      const repo = document.createElement('p');
      repo.className = 'sls-luna-note';
      repo.textContent = 'Repository: pgm1207/slsteam-luna (private).';
      body.appendChild(repo);
      return body;
    }

    function showError(body, error) {
      const line = document.createElement('p');
      line.className = 'sls-luna-error';
      line.textContent = String(error && error.message ? error.message : error);
      body.insertBefore(line, body.firstChild ? body.firstChild.nextSibling : null);
    }

    const TITLES = { playtime: 'Playtime', presence: 'Presence', about: 'About SLS Luna' };

    function paint() {
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
      body.appendChild(tab === 'presence' ? presenceBody() : tab === 'about' ? aboutBody() : playtimeBody());

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
      paint();
    }

    function openMenu() {
      if (document.getElementById(OVERLAY_ID)) return;
      paint();
      refresh();
    }

    function closeMenu() {
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
        total: Math.max(0, (record.trackedSeconds || 0) + (record.adjustmentSeconds || 0)),
        known: name !== ('App ' + id),
      };
    }).filter(row => (row.name + ' ' + row.id).toLowerCase().includes(filter))
      .sort((a, b) => (b.total > 0) - (a.total > 0) || (b.known ? 1 : 0) - (a.known ? 1 : 0)
        || (b.record.lastPlayed || 0) - (a.record.lastPlayed || 0) || a.name.localeCompare(b.name));
  }

  return { mount, buildRows, findMenubar, hours };
});
