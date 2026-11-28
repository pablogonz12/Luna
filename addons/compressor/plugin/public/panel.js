/* SLS Compressor lightweight panel for Lumen desktop shell + store webview. */
(function (w) {
  if (!w.document || w.__SlsCompressorPanel) return;
  // SLS Luna hosts this add-on's settings in its own menubar menu; the floating
  // panel is only for a standalone install.
  if (w.__SlsLunaUi) return;
  // No host (Lumen stopped or restarting) must reject, never throw a TypeError
  // from the refresh interval. Same guard the add-on agents use.
  const offline = e => /bridge unavailable/i.test(String((e && e.message) || e));
  const atend = (status, detail, e) => {
    if (offline(e)) {
      status.textContent = 'Waiting for the host (Lumen bridge unavailable).';
      detail.textContent = '';
      return;
    }
    status.textContent = String(e);
  };
  const rpc = async (method, args = {}) => {
    if (!w.Millennium || typeof w.Millennium.callServerMethod !== 'function') throw Error('bridge unavailable');
    const raw = await w.Millennium.callServerMethod('lumen', method, args);
    const out = typeof raw === 'string' ? JSON.parse(raw) : raw;
    if (out?.success === false) throw Error(out.error || 'RPC failed');
    return out;
  };
  const BTN = 'position:fixed;right:205px;bottom:16px;z-index:99999;background:#224f6b;color:white;padding:8px;border:1px solid #62829a;border-radius:6px;cursor:pointer';
  const PANE = 'position:fixed;right:14px;bottom:56px;z-index:99999;width:420px;max-height:70vh;overflow:auto;padding:14px;border-radius:8px;background:#192734;color:white;box-shadow:0 7px 20px #000b';
  const NOTE = 'font-size:11px;margin:4px 0';
  // Returns true when the panel is (or already was) in the DOM; the published
  // flag only exists after a successful mount, so a retry stays possible.
  function mount() {
    if (!document.body) return false;
    if (document.getElementById('sls-compressor-panel-button')) return true;
    const btn = document.createElement('button');
    btn.id = 'sls-compressor-panel-button';
    btn.textContent = 'Compressor';
    btn.style.cssText = BTN;
    const pane = document.createElement('section');
    pane.id = 'sls-compressor-panel';
    pane.hidden = true;
    pane.style.cssText = PANE;
    const header = document.createElement('h3');
    header.textContent = 'SLS Compressor';
    // Per-library capabilities, the snapshot banner and the pass outcome sit
    // here, above the status lines and the game list.
    const info = document.createElement('div');
    const status = document.createElement('p');
    const detail = document.createElement('p');
    detail.style.cssText = 'font-size:11px;opacity:.85;white-space:pre-wrap';
    const note = document.createElement('p');
    note.style.cssText = 'font-size:11px;opacity:.75';
    note.textContent = 'Checks here are read-only. A compaction is irreversible and can take hours; '
      + 'it never touches a running game.';
    const check = document.createElement('button');
    check.textContent = 'Check now';
    const preview = document.createElement('button');
    preview.textContent = 'Preview (dry-run)';
    for (const b of [check, preview]) {
      b.style.cssText = 'padding:7px;margin:4px;background:#315a75;color:white;border:0;border-radius:5px;cursor:pointer';
    }
    pane.append(header, info, status, detail, note, check, preview);
    document.body.append(btn, pane);

    const mib = n => (Number(n) || 0).toLocaleString() + ' MiB';
    const secondsText = value => {
      const total = Math.max(0, Number(value) || 0);
      if (total < 90) return Math.round(total) + 's';
      if (total < 3600) return Math.round(total / 60) + ' min';
      return (total / 3600).toFixed(1) + ' h';
    };
    function line(text, style) {
      const out = document.createElement('p');
      out.style.cssText = style || NOTE;
      out.textContent = text;
      return out;
    }
    function paintLibraries(result) {
      const libraries = Array.isArray(result.libraries) ? result.libraries : [];
      if (!libraries.length) return;
      const table = document.createElement('div');
      table.style.cssText = 'font-size:11px;margin:6px 0;border-top:1px solid #2f4a5e';
      libraries.forEach(lib => {
        const row = document.createElement('div');
        row.style.cssText = 'display:flex;gap:6px;padding:3px 0;border-bottom:1px solid #223646';
        const label = document.createElement('strong');
        label.style.cssText = 'flex:0 0 38%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap';
        label.textContent = (lib.mount || lib.path || 'library') + (lib.fstype ? ' (' + lib.fstype + ')' : '');
        label.title = lib.path || '';
        const notes = document.createElement('span');
        notes.style.cssText = 'opacity:.9';
        notes.textContent = (lib.notes && lib.notes.length ? lib.notes.join(' \u00b7 ') : 'usable')
          + (lib.compressionText ? ' \u00b7 ' + lib.compressionText : '');
        row.append(label, notes);
        table.appendChild(row);
      });
      info.appendChild(table);
    }
    let snapshotPending = null;
    let snapshotAcknowledged = false;
    let restoreSnapshotFocus = false;
    let snapshotButton = null;
    function paintSnapshots(result) {
      snapshotButton = null;
      const snapshots = result.snapshots || null;
      if (!snapshots) return;
      if (snapshots.detected) info.appendChild(line(snapshots.detected.text));
      const choice = snapshots.choice;
      if (!choice) return;
      const box = document.createElement('div');
      box.style.cssText = 'font-size:11px;margin:6px 0;padding:8px;border:1px solid #8a6d2f;border-radius:6px;background:#2a2417';
      box.appendChild(line(choice.title, 'margin:0'));
      box.appendChild(line(choice.text, 'margin:3px 0;opacity:.9'));
      if (choice.enabled === true) {
        box.appendChild(line('Allowed now: ' + choice.setting + ' = true.', 'margin:3px 0;opacity:.85'));
      }
      // The engine reports its own view of the setting, so this toggle always
      // shows the current choice: allow, or turn it back off.
      let allow = null;
      if (choice.enabledKnown === true) {
        allow = document.createElement('button');
        allow.textContent = choice.button;
        allow.disabled = snapshotPending !== null;
        allow.style.cssText = 'margin-top:6px;padding:6px;background:#6b5422;color:white;border:0;border-radius:5px;cursor:pointer';
        allow.onclick = async () => {
          if (document.activeElement === allow) restoreSnapshotFocus = true;
          snapshotPending = choice.enabled !== true;
          snapshotAcknowledged = false;
          allow.disabled = true;
          try {
            const out = await rpc('SlsCompressorAcceptSnapshots',
              { raw: JSON.stringify({ enabled: snapshotPending }) });
            snapshotAcknowledged = true;
            // The backend only starts a detached read-only scan; the next
            // refresh shows the engine's own answer, not an assumption.
            status.textContent = out.message || 'Snapshot setting written.';
            await refresh();
          } catch (e) {
            snapshotPending = null;
            snapshotAcknowledged = false;
            if (snapshotButton) {
              snapshotButton.disabled = false;
              if (restoreSnapshotFocus && (document.activeElement === document.body
                || document.activeElement === document.documentElement
                || document.activeElement === snapshotButton)) {
                snapshotButton.focus();
                restoreSnapshotFocus = false;
              }
            }
            atend(status, detail, e);
          }
        };
        box.appendChild(allow);
      }
      info.appendChild(box);
      snapshotButton = allow;
      if (restoreSnapshotFocus && allow && !allow.disabled) {
        allow.focus();
        restoreSnapshotFocus = false;
      }
    }
    function paintQueue(result) {
      const queue = result.queue || {};
      const outcome = queue.outcome;
      if (outcome && outcome.kind === 'not-started') {
        info.appendChild(line('Run not started: ' + outcome.reasonText + '. Next: ' + outcome.nextStep + '.'));
      } else if (outcome && (outcome.kind === 'interrupted' || outcome.kind === 'stopped')) {
        info.appendChild(line(outcome.reasonText + (outcome.nextStep ? '. ' + outcome.nextStep : '') + '.'));
      }
      const savings = queue.savings;
      if (savings && savings.text) {
        info.appendChild(line((savings.estimateGames > 0 ? 'Estimate, not a measurement: ' : '')
          + savings.text + '.'));
      }
      if (queue.active && queue.eta && queue.eta.seconds != null) {
        info.appendChild(line('Now: ' + (queue.eta.game || 'a game') + ' \u00b7 about '
          + secondsText(queue.eta.seconds) + ' left.'));
      }
    }
    function paintHealth(result) {
      const health = result.health || null;
      if (!health) return;
      info.appendChild(line('Run log: ' + (Number(health.logBytes) / 1048576).toFixed(1) + ' MiB '
        + '(rotates at 8 MiB; the previous pass is kept as run.log.1).', 'font-size:11px;opacity:.75;margin:4px 0'));
    }
    async function refresh() {
      if (pane.hidden) return;
      try {
        const result = await rpc('SlsCompressorGet');
        if (pane.hidden) return;
        const summary = (result.status && result.status.summary) || {};
        const job = result.job || {};
        const focusedSnapshot = info.contains(document.activeElement)
          && document.activeElement.tagName === 'BUTTON';
        if (focusedSnapshot) restoreSnapshotFocus = true;
        else if (document.activeElement && document.activeElement !== document.body
          && document.activeElement !== document.documentElement) restoreSnapshotFocus = false;
        if (snapshotPending !== null && snapshotAcknowledged
          && (result.rescan?.failed || (!result.rescan
            && !(job.running && job.action === 'scan')
            && result.status && result.status.accept_snapshots_unknown === snapshotPending))) {
          snapshotPending = null;
          snapshotAcknowledged = false;
        }
        info.textContent = '';
        if (result.rescan) {
          info.appendChild(line(result.rescan.failed
            ? 'Snapshot refresh failed: ' + result.rescan.failed
            : 'Snapshot refresh pending; library results may be out of date.'));
        }
        paintLibraries(result);
        paintSnapshots(result);
        paintQueue(result);
        paintHealth(result);
        status.textContent = result.tool.found
          ? `${summary.pending || 0} pending, ${summary.compacted || 0} compacted.`
          : 'btrfs-game-compressor was not found.';
        detail.textContent = [
          job.running ? `Running ${job.action}.` : (job.action ? `Last job: ${job.action}.` : 'No job yet.'),
          job.stuck?.message || '',
          job.error || '',
          job.log ? job.log.split('\n').slice(-6).join('\n') : '',
        ].filter(Boolean).join('\n');
      } catch (e) { atend(status, detail, e); }
    }
    const start = action => async () => {
      try { await rpc('SlsCompressorRun', { raw: JSON.stringify({ action }) }); await refresh(); }
      catch (e) { atend(status, detail, e); }
    };
    check.onclick = start('scan');
    preview.onclick = start('preview');
    btn.onclick = () => { pane.hidden = !pane.hidden; if (!pane.hidden) refresh(); };
    // Poll only while the page is visible: a hidden panel pays nothing.
    w.setInterval(() => { if (!pane.hidden) refresh(); }, 6000);
    w.__SlsCompressorPanel = true;
    return true;
  }
  // A fast Steam start can run this script before document.body exists. Retry a
  // few times (bounded, then give up quietly) instead of losing the panel.
  let attempts = 0;
  function attempt() {
    if (w.__SlsCompressorPanel || mount()) return;
    if (++attempts > 20) return;
    w.setTimeout(attempt, attempts < 10 ? 100 : 500);
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', attempt, {once: true});
  else attempt();
})(window);
