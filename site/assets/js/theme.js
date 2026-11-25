(function () {
  var root = document.documentElement;
  var button = document.querySelector(".theme-toggle");

  function current() {
    if (root.dataset.theme === "dark" || root.dataset.theme === "light") {
      return root.dataset.theme;
    }
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }

  function paint() {
    var dark = current() === "dark";
    var next = dark ? "light" : "dark";
    if (button) {
      button.setAttribute("aria-label", "Switch to " + next + " theme");
    }
    document.querySelectorAll('meta[name="theme-color"]').forEach(function (meta) {
      meta.setAttribute("content", dark ? "#000000" : "#f7f3ea");
      meta.removeAttribute("media");
    });
  }

  if (button) {
    button.addEventListener("click", function () {
      var next = current() === "dark" ? "light" : "dark";
      root.dataset.theme = next;
      try {
        localStorage.setItem("deckstack-theme", next);
      } catch (error) {}
      paint();
    });
  }

  paint();
})();
