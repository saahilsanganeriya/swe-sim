/* Applies the persisted theme before first paint (external, not inline: CSP-friendly). app.js owns the toggle. */
(function () {
  try {
    var t = localStorage.getItem("swesim-theme");
    if (t === "light" || t === "dark") document.documentElement.setAttribute("data-theme", t);
  } catch (e) { /* storage unavailable: follow prefers-color-scheme */ }
})();
