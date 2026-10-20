// Fills static shell text from window.I18N[<html lang>]: data-i18n -> textContent, data-i18n-label -> aria-label.
(() => {
  const T = window.I18N[document.documentElement.lang || "ko"];
  document.querySelectorAll("[data-i18n]").forEach((el) => { el.textContent = T[el.dataset.i18n]; });
  document.querySelectorAll("[data-i18n-label]").forEach((el) => { el.setAttribute("aria-label", T[el.dataset.i18nLabel]); });
})();
