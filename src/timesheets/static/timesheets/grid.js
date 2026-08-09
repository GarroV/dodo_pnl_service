/*
 * Островок табеля. Ровно три вещи, которых нет у браузера и у htmx.
 *
 * 1. Стрелки между ячейками. Tab браузер даёт сам, но в таблице 35×6 табом
 *    доходить до нужной строки невозможно — нужен переход вниз.
 * 2. Видимое состояние ячейки: ушло / сохранено / отказ. Без него человек не
 *    отличает сохранённое от набранного, а на этом экране цена ошибки — часы.
 *    Объяснение отказа держится у самой ячейки: внизу документа его при 35
 *    строках просто не видно, а невидимый отказ — это молчаливый отказ.
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

  // --- слова этого файла --------------------------------------------------
  //
  // Тексты отказов приезжают из разметки, а не написаны здесь (T017). Причина
  // не в стиле: этот файл отдаётся статикой, его не проходит ни `makemessages`,
  // ни движок шаблонов, и написанная тут строка осталась бы русской на
  // английском и сербском экране — молча, потому что видна она только в момент
  // отказа сохранения ячейки.
  //
  // Пропущенный ключ показывается как `[ключ]`, а не пустотой: отказ без слов
  // — это молчаливый отказ, ровно то, ради чего эта подсказка и заводилась.
  function text(name) {
    return table.dataset[name] || "[" + name + "]";
  }

  // Подстановка в переведённую фразу. Функцией замены, а не строкой: в строке
  // замены `$&` и `$1` для JS — управляющие последовательности, а сюда
  // подставляется то, что человек набрал в ячейке.
  function fill(pattern, values) {
    return pattern.replace(/%\((\w+)\)s/g, function (whole, name) {
      return name in values ? String(values[name]) : whole;
    });
  }

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
      hideRefusal();
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

  // --- объяснение отказа у самой ячейки --------------------------------------
  //
  // Внизу документа его не видно: 35 строк уводят низ страницы за экран, и
  // отказ, о котором человек не узнал, ничем не лучше молчаливого. Поэтому
  // подсказка держится у ячейки — фиксированной, чтобы попадать в окно всегда,
  // а не только при удачной прокрутке.

  let anchor = null;

  function place() {
    if (!anchor) return;
    const cell = anchor.getBoundingClientRect();
    const box = errorBox.getBoundingClientRect();
    const margin = 8;

    // Снизу, если снизу есть место; иначе сверху — но всегда внутри окна.
    let top = cell.bottom + 6;
    if (top + box.height > window.innerHeight - margin) {
      top = Math.min(cell.top - box.height - 6, window.innerHeight - box.height - margin);
    }
    let left = cell.left;
    if (left + box.width > window.innerWidth - margin) {
      left = window.innerWidth - box.width - margin;
    }
    errorBox.style.top = Math.max(margin, top) + "px";
    errorBox.style.left = Math.max(margin, left) + "px";
  }

  function showRefusal(input, text) {
    errorBox.textContent = text;
    errorBox.hidden = false;
    anchor = input;
    place();
  }

  function hideRefusal() {
    errorBox.hidden = true;
    anchor = null;
  }

  // Прокрутка бывает и у окна, и у таблицы — слушаем в фазе перехвата.
  window.addEventListener("scroll", place, true);
  window.addEventListener("resize", place);

  // Человек начал исправлять — объяснение больше не нужно, а красная пометка
  // снимается только ответом сервера: пока он не ответил, ячейка не сохранена.
  table.addEventListener("input", function (event) {
    if (event.target.classList.contains("cell")) hideRefusal();
  });

  document.body.addEventListener("htmx:beforeRequest", function (event) {
    const input = event.detail.elt;
    if (!input.classList.contains("cell")) return;
    mark(input, "pending");
    input.removeAttribute("aria-invalid");
    hideRefusal();
  });

  document.body.addEventListener("htmx:afterRequest", function (event) {
    const input = event.detail.elt;
    if (!input.classList.contains("cell")) return;
    const xhr = event.detail.xhr;
    const stored = xhr.getResponseHeader("X-Cell-Value");
    const canonical = stored === null ? null : Number(stored) === 0 ? "" : stored;

    if (xhr.status === 200) {
      if (canonical !== null) {
        // Поле под курсором не трогаем: человек уже мог начать в нём печатать
        // заново, и подмена стёрла бы набранное.
        if (document.activeElement !== input) input.value = canonical;
        input.dataset.saved = canonical;
      }
      mark(input, "saved");
      return;
    }

    // Отказ: в базу ничего не легло, и на экране не должно остаться того, чего
    // в ней нет. Возвращаем то, что сервер назвал сохранённым, — иначе человек
    // читал бы отказ и видел рядом своё непринятое число.
    const previous =
      canonical !== null
        ? canonical
        : input.dataset.saved !== undefined
          ? input.dataset.saved
          : input.defaultValue;
    const rejected = input.value;
    input.value = previous;
    input.dataset.saved = previous;

    mark(input, "failed");
    input.setAttribute("aria-invalid", "true");
    // Коды, при которых сервер прислал текст для человека: 422 — не принято
    // значение, 403 — не хватает права роли, 409 — правила страны на этот месяц
    // больше не действуют. Показывать надо этот текст, а не номер кода, и
    // рядом с ячейкой: сообщение внизу документа при 35 строках почти всегда
    // оказывается за экраном.
    //
    // 409 идёт отдельной строкой не из аккуратности: при нём в базе может
    // остаться и прежнее значение (правил не было уже на записи), и новое
    // (запись прошла, а пересборка сетки — нет). Врать про «не принято» нельзя
    // ни в ту, ни в другую сторону, поэтому сказано то, что верно всегда: в
    // ячейке стоит то, что в базе, — сервер прислал это значение заголовком.
    const spoken = xhr.status === 422 || xhr.status === 403;
    showRefusal(
      input,
      spoken
        ? xhr.responseText + " " + text("rejected")
        : xhr.status === 409
          ? xhr.responseText + " " + text("current")
          : fill(text("failed"), {
              value: rejected,
              reason: xhr.status || text("offline"),
            }),
    );
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
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
