"use strict";
(() => {
  const buttons = document.querySelectorAll("[data-language-toggle]");
  let language = "en";
  try { language = localStorage.getItem("cobot-language") === "zh" ? "zh" : "en"; } catch {}
  function setLanguage(next) {
    language = next;
    document.documentElement.lang = next === "zh" ? "zh-CN" : "en";
    document.querySelectorAll("[data-language]").forEach(element => {
      element.hidden = element.dataset.language !== next;
    });
    buttons.forEach(button => {
      button.textContent = next === "en" ? "中文" : "English";
      button.setAttribute("aria-label", next === "en" ? "Switch to Chinese" : "切换到英文");
    });
    const title = document.documentElement.dataset[next === "en" ? "titleEn" : "titleZh"];
    if (title) document.title = title;
    try { localStorage.setItem("cobot-language", next); } catch {}
  }
  buttons.forEach(button => button.addEventListener("click", () => setLanguage(language === "en" ? "zh" : "en")));
  setLanguage(language);
  document.querySelectorAll("[data-tab-group]").forEach(group => {
    const tabs = [...group.querySelectorAll('[role="tab"]')];
    function activate(tab, focus) {
      tabs.forEach(item => {
        const selected = item === tab;
        item.setAttribute("aria-selected", String(selected));
        item.tabIndex = selected ? 0 : -1;
        document.getElementById(item.getAttribute("aria-controls")).hidden = !selected;
      });
      if (focus) tab.focus();
    }
    tabs.forEach((tab, index) => {
      tab.addEventListener("click", () => activate(tab, false));
      tab.addEventListener("keydown", event => {
        let target;
        if (event.key === "ArrowRight") target = tabs[(index + 1) % tabs.length];
        if (event.key === "ArrowLeft") target = tabs[(index - 1 + tabs.length) % tabs.length];
        if (event.key === "Home") target = tabs[0];
        if (event.key === "End") target = tabs[tabs.length - 1];
        if (target) { event.preventDefault(); activate(target, true); }
      });
    });
  });
})();
