// Applies the saved theme before first paint to avoid a flash of the wrong theme.
(function () {
  var theme = "dark";
  try {
    var saved = localStorage.getItem("md.theme");
    if (saved === "light" || saved === "dark") theme = saved;
  } catch (e) { /* storage unavailable */ }
  document.documentElement.setAttribute("data-theme", theme);
})();
