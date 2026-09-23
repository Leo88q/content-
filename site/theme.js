/* Тема: «CRT-ночь» (тёмная) и «бумажная аркада» (светлая).
   ---------------------------------------------------------------------------
   Решение применено до отрисовки (скрипт в <head>) — иначе на загрузке мигает
   чужая тема. Выбор запоминается в localStorage, по умолчанию — системная
   настройка prefers-color-scheme.

   ВАЖНО для тестов: scripts/smoke_narrative.js выполняет app.js в минималистичной
   DOM-заглушке, где нет document.documentElement и window.matchMedia. Поэтому
   каждое обращение к DOM здесь — через проверку, а не напрямую: падение
   телеметрии/темы не должно ронять терминал.
*/
(function () {
  "use strict";
  var KEY = "tc_theme";

  function root() {
    try {
      if (typeof document === "undefined") return null;
      return document.documentElement || document.body || null;
    } catch (e) { return null; }
  }

  function readStored() {
    try {
      if (typeof localStorage === "undefined") return null;
      var v = localStorage.getItem(KEY);
      return v === "light" || v === "dark" ? v : null;
    } catch (e) { return null; }
  }

  function systemTheme() {
    try {
      if (typeof window !== "undefined" && typeof window.matchMedia === "function") {
        if (window.matchMedia("(prefers-color-scheme: light)").matches) return "light";
        if (window.matchMedia("(prefers-color-scheme: dark)").matches) return "dark";
      }
    } catch (e) {}
    return "dark";
  }

  function current() {
    var node = root();
    if (node && node.getAttribute) {
      var attr = node.getAttribute("data-theme");
      if (attr === "light" || attr === "dark") return attr;
    }
    return readStored() || systemTheme();
  }

  function labels(theme) {
    return theme === "light"
      ? { icon: "🌙", title: "Тёмная тема", text: "НОЧЬ" }
      : { icon: "☀️", title: "Светлая тема", text: "ДЕНЬ" };
  }

  function paintButtons(theme) {
    var l = labels(theme);
    try {
      if (typeof document === "undefined" || !document.querySelectorAll) return;
      var nodes = document.querySelectorAll("[data-theme-toggle]");
      for (var i = 0; i < nodes.length; i++) {
        var n = nodes[i];
        if (n.setAttribute) {
          n.setAttribute("title", l.title);
          n.setAttribute("aria-label", l.title);
          n.setAttribute("aria-pressed", theme === "light" ? "true" : "false");
        }
        if ("textContent" in n) n.textContent = l.icon + " " + l.text;
      }
    } catch (e) {}
  }

  function apply(theme, persist) {
    var node = root();
    if (node && node.setAttribute) node.setAttribute("data-theme", theme);
    if (persist) {
      try { if (typeof localStorage !== "undefined") localStorage.setItem(KEY, theme); } catch (e) {}
    }
    paintButtons(theme);
    return theme;
  }

  function toggle() {
    return apply(current() === "light" ? "dark" : "light", true);
  }

  // Применяем сразу: скрипт подключён в <head> до body.
  apply(readStored() || systemTheme(), false);

  function bind() {
    try {
      if (typeof document === "undefined" || !document.querySelectorAll) return;
      var nodes = document.querySelectorAll("[data-theme-toggle]");
      for (var i = 0; i < nodes.length; i++) {
        nodes[i].addEventListener("click", function (e) {
          if (e && e.preventDefault) e.preventDefault();
          toggle();
        });
      }
    } catch (e) {}
    paintButtons(current());
  }

  if (typeof document !== "undefined" && document.addEventListener) {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", bind);
    } else {
      bind();
    }
  }

  // Следуем за системной темой, пока пользователь не выбрал явно.
  try {
    if (typeof window !== "undefined" && typeof window.matchMedia === "function"
        && typeof window.addEventListener === "function") {
      var mq = window.matchMedia("(prefers-color-scheme: dark)");
      var onChange = function () { if (!readStored()) apply(systemTheme(), false); };
      if (mq.addEventListener) mq.addEventListener("change", onChange);
      else if (mq.addListener) mq.addListener(onChange);
    }
  } catch (e) {}

  window.__TC_THEME__ = { apply: apply, toggle: toggle, current: current };
})();
