// Время в часовом поясе пользователя. Сервер отдаёт <time datetime="…UTC" data-format="…">
// с запасным текстом в своём поясе, здесь он переписывается в поясе браузера.
// Блок мониторинга приходит через SSE целиком, поэтому новые узлы ловит MutationObserver
(() => {
  const two = { day: "2-digit", month: "2-digit" };
  const clock = { hour: "2-digit", minute: "2-digit", hourCycle: "h23" };
  const formats = {
    datetime: new Intl.DateTimeFormat("ru-RU", { ...two, ...clock, second: "2-digit" }),
    time: new Intl.DateTimeFormat("ru-RU", clock),
  };
  const full = new Intl.DateTimeFormat("ru-RU", { dateStyle: "long", timeStyle: "long" });

  const localize = (root) => {
    for (const el of root.querySelectorAll("time[data-format]:not([data-local])")) {
      const moment = new Date(el.dateTime);
      const format = formats[el.dataset.format];
      if (!format || Number.isNaN(moment.getTime())) continue;
      // «09.11, 09:57:40» → «09.11 09:57:40», как в запасном тексте
      el.textContent = format.format(moment).replace(", ", " ");
      el.title = full.format(moment);
      el.dataset.local = "";
    }
  };

  const start = () => {
    localize(document);
    new MutationObserver((records) => {
      for (const record of records) {
        for (const node of record.addedNodes) {
          if (node.nodeType === Node.ELEMENT_NODE) localize(node.parentNode ?? node);
        }
      }
    }).observe(document.body, { childList: true, subtree: true });
  };

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
