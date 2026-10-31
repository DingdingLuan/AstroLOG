"use strict";

(() => {
  const key = "logpresent.theme";
  const valid = value => ["auto", "light", "dark"].includes(value);
  const system = matchMedia("(prefers-color-scheme: dark)");
  const read = () => {
    try { const value = localStorage.getItem(key); return valid(value) ? value : "auto"; }
    catch (_) { return "auto"; }
  };
  let mode = read();
  function apply() {
    const dark = mode === "dark" || (mode === "auto" && system.matches);
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    document.documentElement.dataset.themeMode = mode;
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", dark ? "#151515" : "#e9edf1");
    for (const id of ["theme-mode", "auth-theme-mode"]) {
      const select = document.getElementById(id);
      if (select) select.value = mode;
    }
    const icon = document.getElementById("theme-icon");
    if (icon) icon.innerHTML = mode === "auto"
      ? '<rect x="2" y="3" width="16" height="11" rx="2"/><path d="M7 18h6m-3-4v4"/>'
      : dark
        ? '<path d="M16.4 12.6A7 7 0 0 1 7.4 3.6a7 7 0 1 0 9 9Z"/>'
        : '<circle cx="10" cy="10" r="3.5"/><path d="M10 1v2m0 14v2M1 10h2m14 0h2M3.6 3.6 5 5m10 10 1.4 1.4M3.6 16.4 5 15M15 5l1.4-1.4"/>';
    const label = mode === "auto" ? "System" : mode === "dark" ? "Dark" : "Light";
    for (const element of document.querySelectorAll(".theme-toggle,.auth-theme-toggle")) element.title = `Appearance: ${label}`;
  }
  function choose(value) {
    mode = valid(value) ? value : "auto";
    try { localStorage.setItem(key, mode); } catch (_) {}
    apply();
  }
  function bind() {
    for (const id of ["theme-mode", "auth-theme-mode"]) {
      const select = document.getElementById(id);
      if (select && !select.dataset.bound) {
        select.addEventListener("change", () => choose(select.value));
        select.dataset.bound = "true";
      }
    }
    apply();
  }
  system.addEventListener("change", () => { if (mode === "auto") apply(); });
  window.addEventListener("storage", event => {
    if (event.key === key || event.key === null) { mode = read(); apply(); }
  });
  window.AstroLOGTheme = {apply, bind};
  apply();
})();
