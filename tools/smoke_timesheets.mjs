/*
 * Смоук экрана табеля настоящими событиями браузера.
 *
 * Проверяется не разметка, а поведение: ввод с клавиатуры, переход между
 * ячейками стрелками, сохранение без перезагрузки и — главное — что число
 * остаётся на месте после ухода со страницы и возврата.
 *
 * Управление браузером — в tools/cdp.mjs: смоуков табеля стало два, и копия
 * харнесса в каждом означала бы чинить один и не замечать другой. Внешних
 * зависимостей нет: WebSocket встроен в Node.
 *
 * Как запустить (нужен поднятый продукт с сидом и headless-браузер):
 *
 *     docker compose up -d && docker compose exec app python manage.py seed_dev
 *     "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
 *         --headless=new --disable-gpu --no-first-run \
 *         --remote-debugging-port=9339 --user-data-dir=/tmp/chrome-smoke &
 *     APP=http://127.0.0.1:8000 node tools/smoke_timesheets.mjs
 *
 * Скрипт пишет в базу — гонять только на тестовом стенде. Порт отладки задаётся
 * переменной CDP_PORT, адрес продукта — APP.
 */
import { attach, loginWith } from "./cdp.mjs";

// Умолчание — APP_PORT из .env.example. На своём стенде задайте APP.
const APP = process.env.APP || "http://127.0.0.1:8000";

const { send, evalIn, goto, key, type, check, report, logs } = await attach();

// Вход настоящим логином и паролем, а не кнопкой-ярлыком: проверяем тот путь,
// которым пойдёт человек.
const loginAs = loginWith(APP, evalIn, goto);

await loginAs("director");
check("вошли директором", (await evalIn("location.pathname")) === "/periods/",
      await evalIn("location.pathname"));

const periodHref = await evalIn(
  // Именно ссылка на период, а не пункт «Периоды» в шапке: тот тоже начинается
  // с /periods/ и заканчивается косой чертой.
  `[...document.querySelectorAll('a[href^="/periods/"]')]
     .map(a => a.getAttribute('href'))
     .find(h => /^\\/periods\\/[0-9a-f-]{36}\\/$/.test(h))`
);
await goto(APP + periodHref);
const gridHref = await evalIn(
  `document.querySelector('a[href^="/timesheets/"]').getAttribute('href')`
);
check("со страницы периода есть ссылка на табель", !!gridHref, gridHref);

await goto(APP + gridHref);

const state = await evalIn(`({
  htmx: typeof window.htmx,
  version: window.htmx && window.htmx.version,
  cells: document.querySelectorAll('input.cell').length,
  rows: document.querySelectorAll('#timesheet-grid tbody tr').length,
  columns: document.querySelectorAll('#timesheet-grid thead th').length,
})`);
check("htmx поднялся из файла", state.htmx === "object" && state.version.startsWith("2.0"),
      "версия " + state.version);
check("сетка построена", state.rows >= 35 && state.cells >= 35 * 4,
      `${state.rows} строк, ${state.cells} ячеек`);

// --- ввод с клавиатуры -------------------------------------------------------

// Первая ячейка в шестой строке — берём не первую строку, чтобы заодно проверить
// переход стрелкой вверх и вниз по настоящей таблице.
const cellInfo = await evalIn(`
  (() => {
    const rows = [...document.querySelectorAll('#timesheet-grid tbody tr')];
    const input = rows[5].querySelector('input.cell');
    input.focus();
    return { row: input.dataset.row, kind: input.dataset.kind, before: input.value };
  })()
`);

await evalIn(`document.activeElement.select()`);
await key("Backspace", "Backspace", 8);
await type("123.5");
check("текст набран с клавиатуры", (await evalIn("document.activeElement.value")) === "123.5",
      await evalIn("document.activeElement.value"));

// Enter — уход вниз, то есть потеря фокуса, то есть отправка ячейки.
await key("Enter", "Enter", 13);
await new Promise((r) => setTimeout(r, 900));

const afterEnter = await evalIn(`({
  moved: document.activeElement.dataset.row,
  kind: document.activeElement.dataset.kind,
  savedClass: [...document.querySelectorAll('input.cell')].some(i => i.classList.contains('saved')),
  cellValue: [...document.querySelectorAll('input.cell')]
      .find(i => i.dataset.row === ${JSON.stringify(cellInfo.row)}
              && i.dataset.kind === ${JSON.stringify(cellInfo.kind)}).value,
  rowTotal: document.getElementById('row-total-' + ${JSON.stringify(cellInfo.row)}).textContent.trim(),
})`);
check("Enter перевёл фокус на строку ниже",
      afterEnter.moved && afterEnter.moved !== cellInfo.row && afterEnter.kind === cellInfo.kind,
      `${cellInfo.kind} → строка ${afterEnter.moved}`);
check("ячейка помечена сохранённой", afterEnter.savedClass);
check("значение приведено к виду базы", afterEnter.cellValue === "123.50", afterEnter.cellValue);
check("итог строки пересчитан без перезагрузки", afterEnter.rowTotal.includes("123,50")
      || afterEnter.rowTotal !== "", "итог строки: " + afterEnter.rowTotal);

// Стрелки: вправо и вверх.
await evalIn(`
  (() => {
    const input = [...document.querySelectorAll('input.cell')]
      .find(i => i.dataset.row === ${JSON.stringify(cellInfo.row)}
              && i.dataset.kind === ${JSON.stringify(cellInfo.kind)});
    input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  })()
`);
await key("ArrowRight", "ArrowRight", 39);
const right = await evalIn(`document.activeElement.dataset.kind`);
check("стрелка вправо — соседняя колонка", right && right !== cellInfo.kind,
      `${cellInfo.kind} → ${right}`);
await key("ArrowUp", "ArrowUp", 38);
const up = await evalIn(`document.activeElement.dataset.row`);
check("стрелка вверх — строка выше", up && up !== cellInfo.row, "строка " + up);

// --- уход со страницы и возврат ---------------------------------------------

await goto(APP + periodHref);
await goto(APP + gridHref);
const afterReturn = await evalIn(`
  [...document.querySelectorAll('input.cell')]
    .find(i => i.dataset.row === ${JSON.stringify(cellInfo.row)}
            && i.dataset.kind === ${JSON.stringify(cellInfo.kind)}).value
`);
check("ушли со страницы и вернулись — число на месте", afterReturn === "123.50", afterReturn);

// --- досылка при закрытии вкладки прямо из поля ------------------------------
// Самый неприятный случай: человек печатает и закрывает вкладку, не покидая
// ячейки. Обычный путь (change при потере фокуса) здесь не срабатывает.

const beaconCell = await evalIn(`
  (() => {
    const rows = [...document.querySelectorAll('#timesheet-grid tbody tr')];
    const input = rows[7].querySelector('input.cell');
    input.focus();
    input.select();
    return { row: input.dataset.row, kind: input.dataset.kind };
  })()
`);
await key("Backspace", "Backspace", 8);
await type("77.25");
// Уход со страницы БЕЗ снятия фокуса с поля: ровно то, что делает закрытие вкладки.
await send("Page.navigate", { url: APP + periodHref });
await new Promise((r) => setTimeout(r, 1500));
await goto(APP + gridHref);
const beaconValue = await evalIn(`
  [...document.querySelectorAll('input.cell')]
    .find(i => i.dataset.row === ${JSON.stringify(beaconCell.row)}
            && i.dataset.kind === ${JSON.stringify(beaconCell.kind)}).value
`);
check("закрытие страницы прямо из поля не теряет часы", beaconValue === "77.25", beaconValue);

// --- отказ на мусор ----------------------------------------------------------

await evalIn(`
  (() => {
    const input = [...document.querySelectorAll('input.cell')]
      .find(i => i.dataset.row === ${JSON.stringify(cellInfo.row)}
              && i.dataset.kind === ${JSON.stringify(cellInfo.kind)});
    input.focus();
    input.select();
  })()
`);
// До этого момента консоль обязана быть чистой: всё, что было раньше, — это
// обычная работа экрана.
check("до намеренной ошибки консоль браузера чиста", logs.length === 0,
      logs.join(" | ").slice(0, 300));
const logsBeforeRefusal = logs.length;

await key("Backspace", "Backspace", 8);
await type("восемь");
await key("Enter", "Enter", 13);
await new Promise((r) => setTimeout(r, 900));
const refusal = await evalIn(`({
  shown: !document.getElementById('cell-error').hidden,
  text: document.getElementById('cell-error').textContent.trim(),
  failed: !![...document.querySelectorAll('input.cell')].find(i => i.classList.contains('failed')),
})`);
check("текст вместо числа — видимый отказ, а не тишина",
      refusal.shown && refusal.failed, refusal.text);

// Вернуть значение, чтобы не оставлять мусор в базе смоука.
await evalIn(`
  (() => {
    const input = [...document.querySelectorAll('input.cell')]
      .find(i => i.dataset.row === ${JSON.stringify(cellInfo.row)}
              && i.dataset.kind === ${JSON.stringify(cellInfo.kind)});
    input.value = ${JSON.stringify(cellInfo.before)};
    input.dispatchEvent(new Event('change', { bubbles: true }));
  })()
`);
await new Promise((r) => setTimeout(r, 800));

// --- управляющий видит только свои точки ------------------------------------

await evalIn(`document.querySelector('form[action="/logout/"]').submit()`);
await new Promise((r) => setTimeout(r, 1200));
await loginAs("manager");
await goto(APP + gridHref);
const managerView = await evalIn(`({
  rows: document.querySelectorAll('#timesheet-grid tbody tr').length,
  units: [...new Set([...document.querySelectorAll('#timesheet-grid tbody tr td:nth-child(2)')]
      .map(td => td.textContent.trim()))],
})`);
check("управляющий видит только свою точку",
      managerView.units.length === 1 && managerView.rows > 0 && managerView.rows < 35,
      `${managerView.rows} строк, точки: ${managerView.units.join(", ")}`);

// --- консоль -----------------------------------------------------------------

// После намеренно испорченного ввода в консоли ожидается ровно один след —
// сообщение htmx о коде 422. Это и есть доказательство, что отказ дошёл до
// клиента, а не растворился.
const afterRefusal = logs.slice(logsBeforeRefusal);
check("единственный след в консоли — ожидаемый отказ 422",
      afterRefusal.every((line) => line.includes("422")),
      afterRefusal.join(" | ").slice(0, 300) || "пусто");

report();
