(() => {
  const monitor = document.getElementById("monitor");
  const live = document.getElementById("live");
  const button = document.getElementById("run-now");
  const message = document.getElementById("run-message");
  if (!monitor || !button) return;

  const { streamUrl, runUrl, loginUrl, intervalMs } = monitor.dataset;
  const csrfToken = document.querySelector('meta[name="csrf-token"]')?.content ?? "";
  live.title = `Обновление каждые ${Math.round(Number(intervalMs) / 1000)} с`;

  const syncButton = () => {
    button.disabled = monitor.querySelector("[data-running]") !== null;
  };
  syncButton();

  const source = new EventSource(streamUrl);
  source.addEventListener("update", (event) => {
    monitor.innerHTML = event.data;
    live.classList.remove("live--off");
    live.textContent = "● онлайн";
    syncButton();
  });
  const sessionExpired = () => {
    source.close();
    live.classList.add("live--off");
    live.textContent = "● сессия завершена";
    button.disabled = true;
    message.textContent = "Сессия истекла или отозвана, ";
    const link = document.createElement("a");
    link.href = loginUrl;
    link.textContent = "войдите заново";
    message.append(link);
  };
  source.addEventListener("logout", sessionExpired);
  source.addEventListener("error", () => {
    if (source.readyState === EventSource.CLOSED) {
      sessionExpired();
      return;
    }
    live.classList.add("live--off");
    live.textContent = "● переподключение";
  });

  button.addEventListener("click", async () => {
    button.disabled = true;
    message.textContent = "Отправляю запрос…";
    try {
      const response = await fetch(runUrl, {
        method: "POST",
        headers: { "X-CSRF-Token": csrfToken },
      });
      const body = await response.json().catch(() => ({}));
      message.textContent = response.ok
        ? "Запрос принят, воркер начнёт проход в течение пары секунд"
        : body.detail || `Ошибка ${response.status}`;
    } catch {
      message.textContent = "Сервер недоступен";
    }
    setTimeout(syncButton, 3000);
  });
})();
