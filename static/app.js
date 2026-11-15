/* Shared helpers: CSRF-aware fetch, notification badges, toasts, desktop alerts. */
(function () {
  'use strict';
  const meta = document.querySelector('meta[name=csrf]');
  const csrf = meta ? meta.content : '';

  async function api(url, opts) {
    opts = opts || {};
    const headers = { 'X-CSRF-Token': csrf };
    if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
    try {
      const r = await fetch(url, {
        method: opts.method || 'GET', credentials: 'same-origin', headers,
        body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
      });
      let data = null;
      try { data = await r.json(); } catch (e) { /* not JSON */ }
      return { ok: r.ok, status: r.status, data };
    } catch (e) {
      return { ok: false, status: 0, data: null };
    }
  }
  window.Tenantman = { csrf, api };

  const body = document.body;
  if (body.dataset.signedIn !== '1') return;
  let lastId = parseInt(body.dataset.lastNotif || '0', 10);
  let lastMsgs = parseInt(body.dataset.msgs || '0', 10);
  const onThread = body.dataset.page === 'thread';

  function setBadge(id, n) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = n > 99 ? '99+' : String(n);
    el.hidden = n <= 0;
  }

  function toast(title, text, href) {
    let box = document.getElementById('toasts');
    if (!box) { box = document.createElement('div'); box.id = 'toasts'; box.setAttribute('aria-live', 'polite'); document.body.appendChild(box); }
    const el = document.createElement(href ? 'a' : 'div');
    el.className = 'toast';
    if (href) el.href = href;
    const t = document.createElement('strong'); t.textContent = title;
    el.appendChild(t);
    if (text) { const p = document.createElement('span'); p.textContent = text; el.appendChild(p); }
    box.appendChild(el);
    setTimeout(() => el.remove(), 9000);
    if (window.Notification && Notification.permission === 'granted' && document.hidden) {
      try { new Notification(title, { body: text || '' }); } catch (e) { /* ignore */ }
    }
  }

  async function poll() {
    if (document.hidden) return;
    const res = await api('/api/summary?after=' + lastId);
    if (!res.ok || !res.data) return;
    const d = res.data;
    setBadge('badge-notifications', d.notifications);
    setBadge('badge-messages', d.messages);
    d.new.forEach((n) => toast(n.title, n.body, '/notifications/' + n.id + '/open'));
    if (d.last_id > lastId) lastId = d.last_id;
    if (d.messages > lastMsgs && !onThread) toast('New message', 'You have ' + d.messages + ' unread message' + (d.messages === 1 ? '' : 's') + '.', '/messages');
    lastMsgs = d.messages;
  }
  setTimeout(poll, 1500);
  setInterval(poll, 45000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });

  const enable = document.getElementById('enable-alerts');
  if (enable) {
    if (!window.Notification) { enable.hidden = true; }
    else if (Notification.permission === 'granted') { enable.textContent = 'Desktop alerts are on'; enable.disabled = true; }
    enable.addEventListener('click', async () => {
      const p = await Notification.requestPermission();
      if (p === 'granted') { enable.textContent = 'Desktop alerts are on'; enable.disabled = true; }
    });
  }
})();
