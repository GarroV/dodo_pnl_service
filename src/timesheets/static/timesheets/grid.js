/*
 * Островок табеля. Ровно три вещи, которых нет у браузера и у htmx.
 *
 * 1. Стрелки между ячейками. Tab браузер даёт сам, но в таблице 35×6 табом
 *    доходить до нужной строки невозможно — нужен переход вниз.
 * 2. Видимое состояние ячейки: ушло / сохранено / отказ. Без него человек не
 *    отличает сохранённое от набранного, а на этом экране цена ошибки — часы.
 * 3. Досылка несохранённого при уходе со страницы. Обычный путь (htmx на
 *    change) срабатывает при потере фокуса, но если вкладку закрывают прямо из
 *    поля, браузер вправе оборвать незавершённый запрос. sendBeacon — запрос,
 *    который браузер обязан доставить уже после ухода.
 *
 * Никакого состояния сетки в памяти здесь нет: источник истины — база, на
 * экране только то, что она подтвердила.
 */
(function () {
  "use strict";

  const table = document.getElementById("timesheet-grid");
  if (!table) return;

  const errorBox = document.getElementById("cell-error");
  const cells = () => Array.from(table.querySelectorAll("input.cell"));

  // --- переход между ячейками ------------------------------------------------

  function move(from, rowStep, colStep) {
    const cell = from.closest("td");
    const row = from.closest("tr");
    const column = Array.prototype.indexOf.call(row.children, cell);
    const rows = Array.from(table.tBodies[0].rows);
    const rowIndex = rows.indexOf(row);

    const target = rows[rowIndex + rowStep];
    if (!target) return false;
    const next = target.children[column + colStep];
    const input = next && next.querySelector("input.cell");
    if (!input) return false;

    input.focus();
    // Курсор в конец, а не выделение целиком: следующим нажатием человек чаще
    // дописывает, чем стирает.
    input.setSelectionRange(input.value.length, input.value.length);
    return true;
  }

  table.addEventListener("keydown", function (event) {
    const input = event.target;
    if (!input.classList || !input.classList.contains("cell")) return;

    // Стрелки влево-вправо внутри набранного текста — обычное перемещение
    // курсора; забирать их себе значило бы сломать правку числа.
    const atStart = input.selectionStart === 0 && input.selectionEnd === 0;
    const atEnd =
      input.selectionStart === input.value.length &&
      input.selectionEnd === input.value.length;

    let handled = false;
    if (event.key === "ArrowDown" || event.key === "Enter") {
      handled = move(input, 1, 0);
    } else if (event.key === "ArrowUp") {
      handled = move(input, -1, 0);
    } else if (event.key === "ArrowRight" && atEnd) {
      handled = move(input, 0, 1);
    } else if (event.key === "ArrowLeft" && atStart) {
      handled = move(input, 0, -1);
    } else if (event.key === "Escape") {
      // Отмена правки: вернуть то, что подтвердил сервер.
      input.value = input.dataset.saved !== undefined ? input.dataset.saved : input.defaultValue;
      input.blur();
      handled = true;
    }
    if (handled) event.preventDefault();
  });

  // --- состояние ячейки ------------------------------------------------------

  function mark(input, state) {
    input.classList.remove("pending", "saved", "failed");
    if (state) input.classList.add(state);
  }

  document.body.addEventListener("htmx:beforeRequest", function (event) {
    const input = event.detail.elt;
    if (!input.classList.contains("cell")) return;
    mark(input, "pending");
    errorBox.hidden = true;
  });

  document.body.addEventListener("htmx:afterRequest", function (event) {
    const input = event.detail.elt;
    if (!input.classList.contains("cell")) return;
    const xhr = event.detail.xhr;

    if (xhr.status === 200) {
      const stored = xhr.getResponseHeader("X-Cell-Value");
      if (stored !== null) {
        const canonical = Number(stored) === 0 ? "" : stored;
        // Поле под курсором не трогаем: человек уже мог начать в нём печатать
        // заново, и подмена стёрла бы набранное.
        if (document.activeElement !== input) input.value = canonical;
        input.dataset.saved = canonical;
      }
      mark(input, "saved");
      return;
    }

    mark(input, "failed");
    errorBox.textContent =
      xhr.status === 422
        ? xhr.responseText
        : "Не удалось сохранить: " + (xhr.status || "нет связи с сервером");
    errorBox.hidden = false;
    input.focus();
  });

  // --- досылка при уходе со страницы ----------------------------------------

  const token = document.querySelector("input[name=csrfmiddlewaretoken]");

  window.addEventListener("pagehide", function () {
    if (!token) return;
    cells().forEach(function (input) {
      const saved = input.dataset.saved !== undefined ? input.dataset.saved : input.defaultValue;
      if (input.value === saved) return;

      // Ячейка изменена, но подтверждения от сервера нет: либо запрос ещё
      // летит, либо его не было вовсе (уход прямо из поля). Повтор безвреден —
      // запись ячейки идемпотентна.
      const body = new FormData();
      body.append("row", input.dataset.row);
      body.append("kind", input.dataset.kind);
      body.append("hours", input.value);
      body.append("csrfmiddlewaretoken", token.value);
      navigator.sendBeacon(table.dataset.url, body);
    });
  });
})();
