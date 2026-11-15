/* Rich-text document editor (contenteditable). */
(function () {
  'use strict';
  const root = document.getElementById('editor');
  if (!root) return;
  const canEdit = root.dataset.edit === '1';
  const printBtn = document.getElementById('print');
  if (printBtn) printBtn.addEventListener('click', () => window.print());
  if (!canEdit) return;

  const titleEl = document.getElementById('title');
  const statusEl = document.getElementById('status');
  const wordsEl = document.getElementById('words');
  if (!root.innerHTML.trim()) root.innerHTML = '<p><br></p>';
  try { document.execCommand('styleWithCSS', false, false); document.execCommand('defaultParagraphSeparator', false, 'p'); } catch (e) { /* ignore */ }

  const saver = createSaver({
    url: root.dataset.saveUrl || ('/api/workspace/' + root.dataset.id + '/save'),
    version: parseInt(root.dataset.version, 10),
    status: statusEl,
    getPayload: () => ({ title: titleEl.value, content: root.innerHTML }),
  });
  document.getElementById('save').addEventListener('click', () => saver.save());

  function countWords() {
    const t = root.innerText.trim();
    wordsEl.textContent = (t ? t.split(/\s+/).length : 0) + ' words';
  }
  root.addEventListener('input', () => { saver.markDirty(); countWords(); });
  titleEl.addEventListener('input', () => saver.markDirty());
  countWords();

  function ensureFocus() {
    const sel = window.getSelection();
    if (!sel.rangeCount || !root.contains(sel.anchorNode)) root.focus();
  }
  function align(cls) {
    ensureFocus();
    const sel = window.getSelection();
    if (!sel.rangeCount) return;
    const range = sel.getRangeAt(0);
    const blocks = Array.from(root.querySelectorAll('p,h1,h2,h3,li,blockquote,div,td,th')).filter((el) => range.intersectsNode(el));
    blocks.forEach((el) => { el.classList.remove('ta-left', 'ta-center', 'ta-right'); el.classList.add(cls); });
    saver.markDirty();
  }
  function insertTable() {
    const r = parseInt(prompt('Rows', '3'), 10), c = parseInt(prompt('Columns', '3'), 10);
    if (!(r > 0 && c > 0 && r <= 30 && c <= 12)) return;
    let h = '<table><tbody>';
    for (let i = 0; i < r; i++) { h += '<tr>'; for (let j = 0; j < c; j++) h += '<td><br></td>'; h += '</tr>'; }
    ensureFocus();
    document.execCommand('insertHTML', false, h + '</tbody></table><p><br></p>');
  }
  const actions = {
    h1: () => document.execCommand('formatBlock', false, 'h1'),
    h2: () => document.execCommand('formatBlock', false, 'h2'),
    p: () => document.execCommand('formatBlock', false, 'p'),
    quote: () => document.execCommand('formatBlock', false, 'blockquote'),
    left: () => align('ta-left'), center: () => align('ta-center'), right: () => align('ta-right'),
    link: () => { const u = prompt('Link address (https://…)'); if (u) document.execCommand('createLink', false, u); },
    table: insertTable,
  };
  document.querySelectorAll('[data-cmd]').forEach((btn) => {
    btn.addEventListener('mousedown', (e) => e.preventDefault());
    btn.addEventListener('click', () => {
      ensureFocus();
      const cmd = btn.dataset.cmd;
      if (actions[cmd]) actions[cmd](); else document.execCommand(cmd, false, null);
      saver.markDirty(); countWords();
    });
  });
})();
