/* Live message thread: send without reloading, poll for replies. */
(function () {
  'use strict';
  const box = document.getElementById('thread');
  if (!box) return;
  const ref = box.dataset.ref;
  let lastIn = parseInt(box.dataset.lastIn || '0', 10);
  const list = document.getElementById('msglist');
  const form = document.getElementById('msgform');
  const ta = form.querySelector('textarea');
  const btn = form.querySelector('button');
  const err = document.getElementById('msgerr');
  const empty = document.getElementById('empty');

  function add(m) {
    if (empty) empty.remove();
    const d = document.createElement('div');
    d.className = 'msg ' + (m.mine ? 'mine' : 'theirs');
    const p = document.createElement('p'); p.textContent = m.body;
    const s = document.createElement('span'); s.className = 'small muted'; s.textContent = m.at;
    d.append(p, s); list.appendChild(d);
    list.scrollTop = list.scrollHeight;
  }
  list.scrollTop = list.scrollHeight;

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const body = ta.value.trim();
    if (!body) return;
    btn.disabled = true; err.textContent = '';
    const res = await Tenantman.api('/api/messages/' + ref, { method: 'POST', body: { body } });
    btn.disabled = false;
    if (res.ok) { add(res.data); ta.value = ''; ta.focus(); }
    else err.textContent = (res.data && res.data.error) || 'Could not send. Check your connection and try again.';
  });
  ta.addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); form.requestSubmit(); } });

  setInterval(async () => {
    if (document.hidden) return;
    const res = await Tenantman.api('/api/messages/' + ref + '?after=' + lastIn);
    if (res.ok && res.data) res.data.messages.forEach((m) => { lastIn = Math.max(lastIn, m.id); add(m); });
  }, 6000);
})();
