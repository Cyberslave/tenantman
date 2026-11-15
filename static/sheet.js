/* Spreadsheet editor: grid, formulas (via formula.js), CSV export. */
(function () {
  'use strict';
  const holder = document.getElementById('grid');
  if (!holder) return;
  const F = window.TenantmanFormula;
  const canEdit = holder.dataset.edit === '1';
  const data = JSON.parse(document.getElementById('sheet-data').textContent);
  const COLS = 26;
  let rows = data.rows || 60;
  const cells = Object.assign({}, data.cells);
  const inputs = {};
  let focused = null;

  const titleEl = document.getElementById('title');
  const statusEl = document.getElementById('status');
  const cellLabel = document.getElementById('cell-label');
  const saver = canEdit ? createSaver({
    url: '/api/workspace/' + holder.dataset.id + '/save',
    version: parseInt(holder.dataset.version, 10),
    status: statusEl,
    getPayload: () => ({ title: titleEl.value, content: { rows, cols: COLS, cells } }),
  }) : null;
  if (canEdit) {
    document.getElementById('save').addEventListener('click', () => saver.save());
    titleEl.addEventListener('input', () => saver.markDirty());
  }

  const table = document.createElement('table');
  table.className = 'sheet';
  const head = table.createTHead().insertRow();
  head.appendChild(document.createElement('th'));
  for (let c = 1; c <= COLS; c++) { const th = document.createElement('th'); th.textContent = F.numToCol(c); head.appendChild(th); }
  const tbody = table.createTBody();
  holder.appendChild(table);

  function key(c, r) { return F.numToCol(c) + r; }
  function move(c, r) { const el = inputs[key(c, r)]; if (el) el.focus(); }

  function addRows(from, to) {
    for (let r = from; r <= to; r++) {
      const tr = tbody.insertRow();
      const rh = document.createElement('th'); rh.textContent = r; tr.appendChild(rh);
      for (let c = 1; c <= COLS; c++) {
        const k = key(c, r), td = tr.insertCell(), inp = document.createElement('input');
        inp.dataset.key = k; inp.dataset.c = c; inp.dataset.r = r; inp.autocomplete = 'off'; inp.spellcheck = false;
        inp.setAttribute('aria-label', 'Cell ' + k);
        if (!canEdit) inp.readOnly = true;
        inp.addEventListener('focus', () => { focused = k; cellLabel.textContent = k; inp.value = cells[k] || ''; inp.select(); });
        inp.addEventListener('blur', () => { focused = null; refresh(); });
        inp.addEventListener('input', () => {
          if (inp.value === '') delete cells[k]; else cells[k] = inp.value;
          saver.markDirty(); refresh();
        });
        inp.addEventListener('keydown', (e) => {
          if (e.key === 'Enter') { e.preventDefault(); move(c, r + (e.shiftKey ? -1 : 1)); }
          else if (e.key === 'ArrowDown') { e.preventDefault(); move(c, r + 1); }
          else if (e.key === 'ArrowUp') { e.preventDefault(); move(c, r - 1); }
        });
        td.appendChild(inp); inputs[k] = inp;
      }
    }
  }

  function refresh() {
    const sheet = new F.Sheet(cells);
    for (const k in inputs) {
      if (k === focused) continue;
      const inp = inputs[k];
      if (cells[k] === undefined) { if (inp.value !== '') inp.value = ''; inp.className = ''; continue; }
      const v = sheet.get(k);
      inp.value = F.format(v);
      inp.className = (typeof v === 'number' ? 'num ' : '') + (v instanceof F.Err ? 'err' : '');
    }
  }

  addRows(1, rows);
  refresh();

  const addBtn = document.getElementById('add-rows');
  if (addBtn && canEdit) addBtn.addEventListener('click', () => {
    if (rows >= 1000) return;
    const to = Math.min(rows + 25, 1000);
    addRows(rows + 1, to); rows = to; refresh(); saver.markDirty();
  });

  document.getElementById('csv').addEventListener('click', () => {
    const sheet = new F.Sheet(cells);
    let last = 0;
    Object.keys(cells).forEach((k) => { const p = F.parseRef(k); if (p && p.r > last) last = p.r; });
    const lines = [];
    for (let r = 1; r <= last; r++) {
      const row = [];
      for (let c = 1; c <= COLS; c++) {
        const k = key(c, r);
        let s = cells[k] === undefined ? '' : F.format(sheet.get(k));
        if (/^[=+\-@\t\r]/.test(s) && Number.isNaN(Number(s))) s = "'" + s;
        row.push(/[",\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s);
      }
      while (row.length && row[row.length - 1] === '') row.pop();
      lines.push(row.join(','));
    }
    const blob = new Blob(['\ufeff' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = ((titleEl.value || 'spreadsheet').replace(/[^\w\- ]+/g, '').trim() || 'spreadsheet') + '.csv';
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  });
})();
