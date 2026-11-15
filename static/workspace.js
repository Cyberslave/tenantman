/* Shared save logic for the document and spreadsheet editors. */
(function () {
  'use strict';
  window.createSaver = function (opts) {
    let version = opts.version, seq = 0, savedSeq = 0, saving = false, conflict = false;
    const status = opts.status;
    function setStatus(text, cls) { status.textContent = text; status.className = 'small ' + (cls || 'muted'); }

    async function save() {
      while (saving) await new Promise((r) => setTimeout(r, 100));
      if (conflict) return false;
      if (seq === savedSeq) return true;
      saving = true; setStatus('Saving…');
      const sent = seq;
      const payload = opts.getPayload(); payload.base_version = version;
      const res = await Tenantman.api(opts.url, { method: 'POST', body: payload });
      saving = false;
      if (res.ok && res.data) {
        version = res.data.version; savedSeq = sent;
        setStatus(seq === savedSeq ? 'Saved at ' + res.data.saved_at : 'Unsaved changes');
        return true;
      } else if (res.status === 409) {
        conflict = true;
        setStatus('Not saved: someone else saved a newer version. Copy your work, then reload this page.', 'err');
      } else {
        setStatus((res.data && res.data.error) || 'Could not save. Check your connection; your work is still on screen.', 'err');
      }
      return false;
    }
    setInterval(save, 20000);
    window.addEventListener('beforeunload', (e) => { if (seq !== savedSeq && !conflict) { e.preventDefault(); e.returnValue = ''; } });
    document.addEventListener('keydown', (e) => { if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') { e.preventDefault(); save(); } });
    // Downloads use the saved copy, so save first and only continue if that worked.
    const exp = document.getElementById('export');
    if (exp) exp.addEventListener('click', async (e) => { e.preventDefault(); if (await save()) window.location.href = exp.href; });
    return { save, markDirty() { seq++; if (!conflict && !saving) setStatus('Unsaved changes'); } };
  };
})();
