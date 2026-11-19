// «Все игры» в карточке ведёт туда, откуда открыли игру: страница, фильтры и поиск.
// Адрес каталога запоминается при переходе в карточку, HTMX держит его в location.
// Без JS и в новой вкладке ссылка остаётся на «/»
(function () {
  "use strict";
  var KEY = "catalog-url";

  function read() {
    try { return sessionStorage.getItem(KEY); } catch (e) { return null; }
  }

  function save(url) {
    try { sessionStorage.setItem(KEY, url); } catch (e) { /* приватный режим */ }
  }

  document.addEventListener("click", function (event) {
    var link = event.target.closest && event.target.closest("a.card__link");
    if (link && location.pathname === "/") save(location.pathname + location.search);
  });

  document.addEventListener("DOMContentLoaded", function () {
    var back = document.querySelector("a[data-catalog-back]");
    var url = read();
    // Только свой каталог: значение из хранилища не должно увести на чужой адрес
    if (back && url && /^\/(\?|$)/.test(url)) back.href = url;
  });
})();
