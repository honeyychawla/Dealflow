// Runs before first paint so a saved theme never flashes the other one.
(function () {
  try {
    var saved = localStorage.getItem("dealflow-theme");
    if (saved === "light" || saved === "dark") {
      document.documentElement.dataset.theme = saved;
    }
  } catch (e) {
    /* storage blocked: follow the system setting */
  }
})();
