// Print button for lease agreements and receipts.
(function () {
  function wire() {
    var btn = document.getElementById("print-btn");
    if (!btn) return;
    btn.replaceWith(btn.cloneNode(true));
    var fresh = document.getElementById("print-btn");
    fresh.addEventListener("click", function (e) {
      e.preventDefault();
      window.print();
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wire);
  } else {
    wire();
  }
})();
