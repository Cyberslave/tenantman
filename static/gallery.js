/* Photo viewer for the home page gallery: click a photo to enlarge, arrows or swipe buttons to browse, Esc to close. */
(function () {
  'use strict';
  var links = Array.prototype.slice.call(document.querySelectorAll('[data-gallery] a.shot'));
  if (!links.length) return;
  var box, img, cap, closeBtn, prevBtn, nextBtn, idx = 0, opener = null;

  function el(tag, cls, text) { var e = document.createElement(tag); if (cls) e.className = cls; if (text) e.textContent = text; return e; }

  function build() {
    box = el('div', 'lightbox'); box.setAttribute('role', 'dialog'); box.setAttribute('aria-modal', 'true'); box.setAttribute('aria-label', 'Photo viewer');
    closeBtn = el('button', 'lb-btn lb-close', '×'); closeBtn.type = 'button'; closeBtn.setAttribute('aria-label', 'Close');
    prevBtn = el('button', 'lb-btn lb-prev', '‹'); prevBtn.type = 'button'; prevBtn.setAttribute('aria-label', 'Previous photo');
    nextBtn = el('button', 'lb-btn lb-next', '›'); nextBtn.type = 'button'; nextBtn.setAttribute('aria-label', 'Next photo');
    var fig = el('figure', 'lb-fig'); img = el('img'); cap = el('figcaption', 'lb-cap');
    fig.appendChild(img); fig.appendChild(cap);
    [closeBtn, prevBtn, fig, nextBtn].forEach(function (n) { box.appendChild(n); });
    document.body.appendChild(box);
    closeBtn.addEventListener('click', close);
    prevBtn.addEventListener('click', function () { show(idx - 1); });
    nextBtn.addEventListener('click', function () { show(idx + 1); });
    box.addEventListener('click', function (e) { if (e.target === box) close(); });
  }

  function show(i) {
    idx = (i + links.length) % links.length;
    var a = links[idx], thumb = a.querySelector('img');
    img.src = a.href; img.alt = thumb ? thumb.alt : '';
    cap.textContent = a.dataset.caption || '';
    cap.hidden = !a.dataset.caption;
    var single = links.length < 2; prevBtn.hidden = single; nextBtn.hidden = single;
  }

  function open(i, from) {
    if (!box) build();
    opener = from; box.hidden = false; document.body.classList.add('lb-open');
    show(i); closeBtn.focus();
    document.addEventListener('keydown', onKey);
  }

  function close() {
    box.hidden = true; document.body.classList.remove('lb-open');
    document.removeEventListener('keydown', onKey);
    if (opener) opener.focus();
  }

  function onKey(e) {
    if (e.key === 'Escape') close();
    else if (e.key === 'ArrowLeft') show(idx - 1);
    else if (e.key === 'ArrowRight') show(idx + 1);
    else if (e.key === 'Tab') {   // keep focus inside the viewer
      var f = [closeBtn, prevBtn, nextBtn].filter(function (b) { return !b.hidden; });
      var i = f.indexOf(document.activeElement);
      e.preventDefault();
      f[(i + (e.shiftKey ? -1 : 1) + f.length) % f.length].focus();
    }
  }

  links.forEach(function (a, i) { a.addEventListener('click', function (e) { e.preventDefault(); open(i, a); }); });
})();
