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
  let opened = null;
  const labels = {auto: "System", light: "Light", dark: "Dark"};
  const icons = {
    auto: '<rect x="2" y="3" width="16" height="11" rx="2"/><path d="M7 18h6m-3-4v4"/>',
    light: '<circle cx="10" cy="10" r="3.5"/><path d="M10 1v2m0 14v2M1 10h2m14 0h2M3.6 3.6 5 5m10 10 1.4 1.4M3.6 16.4 5 15M15 5l1.4-1.4"/>',
    dark: '<path d="M16.4 12.6A7 7 0 0 1 7.4 3.6a7 7 0 1 0 9 9Z"/>',
  };
  const svg = content => `<svg viewBox="0 0 20 20" aria-hidden="true">${content}</svg>`;
  function closeMenu(restore = false) {
    if (!opened) return;
    const {button, menu} = opened;
    menu.hidden = true;
    button.setAttribute("aria-expanded", "false");
    opened = null;
    if (restore) button.focus();
  }
  function apply() {
    const dark = mode === "dark" || (mode === "auto" && system.matches);
    document.documentElement.dataset.theme = dark ? "dark" : "light";
    document.documentElement.dataset.themeMode = mode;
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", dark ? "#151515" : "#e9edf1");
    for (const id of ["theme-mode", "auth-theme-mode"]) {
      const select = document.getElementById(id);
      if (select) select.value = mode;
    }
    for (const button of document.querySelectorAll(".theme-trigger")) {
      button.querySelector(".theme-current-icon").innerHTML = icons[mode];
      button.querySelector(".theme-label").textContent = labels[mode];
      button.title = `Appearance: ${labels[mode]}`;
      button.setAttribute("aria-label", button.title);
    }
    for (const item of document.querySelectorAll(".theme-option")) {
      item.setAttribute("aria-checked", String(item.dataset.mode === mode));
    }
  }
  function choose(value) {
    mode = valid(value) ? value : "auto";
    try { localStorage.setItem(key, mode); } catch (_) {}
    apply();
  }
  function mount(select) {
    let container = select.parentElement;
    // Also enhance older cached HTML without leaving a button inside a label.
    if (container.tagName === "LABEL") {
      const replacement = document.createElement("div");
      replacement.className = container.className;
      replacement.append(...container.childNodes);
      container.replaceWith(replacement);
      container = replacement;
    }
    container.querySelector(":scope > svg")?.remove();
    const button = document.createElement("button");
    button.type = "button";
    button.id = `${select.id}-trigger`;
    button.className = "theme-trigger";
    button.setAttribute("aria-haspopup", "menu");
    button.setAttribute("aria-expanded", "false");
    button.setAttribute("aria-controls", `${select.id}-menu`);
    button.innerHTML = '<svg class="theme-current-icon" viewBox="0 0 20 20" aria-hidden="true"></svg><span class="theme-label"></span>' + svg('<path d="m6 8 4 4 4-4"/>');
    button.lastElementChild.classList.add("theme-chevron");
    const menu = document.createElement("div");
    menu.id = `${select.id}-menu`;
    menu.className = "theme-menu";
    menu.setAttribute("role", "menu");
    menu.setAttribute("aria-label", "Appearance");
    menu.hidden = true;
    const items = Object.keys(labels).map(value => {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "theme-option";
      item.dataset.mode = value;
      item.setAttribute("role", "menuitemradio");
      item.tabIndex = -1;
      item.innerHTML = svg(icons[value]) + `<span class="theme-option-label">${labels[value]}</span>` + svg('<path d="m4 10 4 4 8-8"/>');
      item.lastElementChild.classList.add("theme-option-check");
      item.addEventListener("click", () => { choose(value); closeMenu(true); });
      menu.append(item);
      return item;
    });
    function openMenu(last = false) {
      closeMenu();
      menu.hidden = false;
      const rect = button.getBoundingClientRect();
      const width = menu.offsetWidth, height = menu.offsetHeight;
      menu.style.left = `${Math.max(12, Math.min(rect.right - width, innerWidth - width - 12))}px`;
      menu.style.top = `${Math.max(12, rect.bottom + height + 6 <= innerHeight - 12 ? rect.bottom + 6 : rect.top - height - 6)}px`;
      button.setAttribute("aria-expanded", "true");
      opened = {button, menu};
      (last ? items.at(-1) : items.find(item => item.dataset.mode === mode)).focus();
    }
    button.addEventListener("click", () => opened?.button === button ? closeMenu() : openMenu());
    button.addEventListener("keydown", event => {
      if (["ArrowDown", "ArrowUp"].includes(event.key)) {
        event.preventDefault(); openMenu(event.key === "ArrowUp");
      }
    });
    menu.addEventListener("keydown", event => {
      const index = items.indexOf(document.activeElement);
      let next;
      if (event.key === "ArrowDown") next = (index + 1) % items.length;
      if (event.key === "ArrowUp") next = (index + items.length - 1) % items.length;
      if (event.key === "Home") next = 0;
      if (event.key === "End") next = items.length - 1;
      if (next !== undefined) { event.preventDefault(); items[next].focus(); }
      if (event.key === "Escape") { event.preventDefault(); closeMenu(true); }
      if (event.key === "Tab") { closeMenu(); button.focus(); }
    });
    select.hidden = true;
    container.append(button);
    document.body.append(menu);
  }
  function bind() {
    for (const id of ["theme-mode", "auth-theme-mode"]) {
      const select = document.getElementById(id);
      if (select && !select.dataset.bound) {
        mount(select);
        select.addEventListener("change", () => choose(select.value));
        select.dataset.bound = "true";
      }
    }
    apply();
  }
  document.addEventListener("pointerdown", event => {
    if (opened && !opened.menu.contains(event.target) && !opened.button.contains(event.target)) closeMenu();
  });
  document.addEventListener("focusin", event => {
    if (opened && !opened.menu.contains(event.target) && !opened.button.contains(event.target)) closeMenu();
  });
  window.addEventListener("resize", () => closeMenu());
  window.addEventListener("scroll", () => closeMenu(), true);
  system.addEventListener("change", () => { if (mode === "auto") apply(); });
  window.addEventListener("storage", event => {
    if (event.key === key || event.key === null) { mode = read(); apply(); }
  });
  window.AstroLOGTheme = {apply, bind};
  apply();
})();
