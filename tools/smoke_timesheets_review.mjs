/*
 * Смоук починок, найденных сверкой со спекой (T061, T062, T066).
 *
 * Проверяется поведение в живом браузере настоящими событиями, а не разметка:
 * дефекты были именно поведенческими — база взносов молча отставала от часов,
 * табель падал 500-й, объяснение отказа рисовалось за экраном.
 *
 * Три этапа, потому что два из них требуют состояния, которого из браузера не
 * создать. Этап задаётся переменной STAGE:
 *
 *   base      — база взносов идёт за часами; отказ виден у ячейки (T061, T066)
 *   mismatch  — расхождение базы помечено и не даёт посчитать период (T061)
 *   norules   — правил на месяц нет: табель объясняет, а не падает (T062)
 *
 * Как запустить (нужен поднятый продукт с сидом и headless-браузер):
 *
 *     "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
 *         --headless=new --disable-gpu --no-first-run --window-size=1440,900 \
 *         --remote-debugging-port=9339 --user-data-dir=/tmp/chrome-smoke &
 * Стенд смоук приводит к сиду сам — но только на базовой ступени: ступени
 * `mismatch` и `norules` работают на стенде, подготовленном снаружи (см.
 * договор в шапке `cdp.mjs`). `COMPOSE_PROJECT_NAME` обязателен.
 *
 *     COMPOSE_PROJECT_NAME=<стенд> APP=http://127.0.0.1:8043 STAGE=base \\
 *         node tools/smoke_timesheets_review.mjs
 *
 * Скрипт пишет в базу — гонять только на тестовом стенде.
 */
import { attach, findPeriodAndGrid, loginWith, standFromSeed } from "./cdp.mjs";

const APP = process.env.APP || "http://127.0.0.1:8000";
const STAGE = process.env.STAGE || "base";

const { send, evalIn, goto, key, type, check, report, logs } = await attach();
const loginAs = loginWith(APP, evalIn, goto);

// Стенд к эталону — только на базовой ступени (issue #76). Ступени `mismatch` и
// `norules` работают на стенде, подготовленном снаружи: там нарочно разведены
// часы и база взносов или сдвинуты правила месяца. Сброс к сиду снёс бы ровно
// то, что они проверяют, — поэтому здесь он под условием, а не «на всякий
// случай для всех».
if (STAGE === "base") standFromSeed();

// Окно ровно то, в котором сверка нашла дефект. Задаётся явно, а не флагом
// запуска браузера: у headless высота окна и высота страницы разные, и
// проверка «видно без прокрутки» на 813 точках доказывала бы не то.
await send("Emulation.setDeviceMetricsOverride", {
  width: 1440, height: 900, deviceScaleFactor: 1, mobile: false,
});

await loginAs("director");
const { periodHref, gridHref } = await findPeriodAndGrid(APP, evalIn, goto);
await goto(APP + gridHref);

// Числа на экране русские: «1 234,50». Возвращаем их к числу, чтобы сравнивать
// не строки, а суммы.
const NUM = `(t => Number(String(t).replace(/[^0-9,.-]/g, "").replace(",", ".")) || 0)`;

// --------------------------------------------------------------------------
if (STAGE === "base") {
  const size = await evalIn(`({ w: innerWidth, h: innerHeight })`);
  check("окно 1440×900, как у сверки", size.w === 1440 && size.h === 900,
        `${size.w}×${size.h}`);

  // --- T061: база взносов идёт за часами -----------------------------------

  const target = await evalIn(`
    (() => {
      const num = ${NUM};
      const rows = [...document.querySelectorAll('#timesheet-grid tbody tr')];
      for (const tr of rows) {
        const input = tr.querySelector('input.cell[data-kind=holiday]');
        if (!input) continue;
        const total = num(document.getElementById('row-total-' + input.dataset.row).textContent);
        const insured = num(document.getElementById('insured-' + input.dataset.row).textContent);
        if (total > 0 && total === insured) {
          return { row: input.dataset.row, total, insured, holiday: input.value };
        }
      }
      return null;
    })()
  `);
  check("нашлась строка, где база взносов равна часам", !!target,
        target && `итого ${target.total}, база ${target.insured}`);

  await evalIn(`
    (() => {
      const input = document.querySelector(
        'input.cell[data-kind=holiday][data-row="${target.row}"]');
      input.focus();
      input.select();
    })()
  `);
  await key("Backspace", "Backspace", 8);
  await type("8,5");
  await key("Enter", "Enter", 13);
  await new Promise((r) => setTimeout(r, 1000));

  const afterEdit = await evalIn(`
    (() => {
      const num = ${NUM};
      return {
        total: num(document.getElementById('row-total-${target.row}').textContent),
        insured: num(document.getElementById('insured-${target.row}').textContent),
        marked: document.getElementById('insured-${target.row}')
                  .classList.contains('insured-mismatch'),
      };
    })()
  `);
  const expected = target.total - Number(target.holiday || 0) + 8.5;
  check("итог строки пересчитан на месте", afterEdit.total === expected,
        `${target.total} → ${afterEdit.total}`);
  check("база взносов пошла за часами, а не осталась прежней",
        afterEdit.insured === expected,
        `${target.insured} → ${afterEdit.insured}`);
  check("сошедшаяся база не помечена тревогой", afterEdit.marked === false);

  await goto(APP + periodHref);
  await goto(APP + gridHref);
  const afterReload = await evalIn(`
    (() => {
      const num = ${NUM};
      return {
        total: num(document.getElementById('row-total-${target.row}').textContent),
        insured: num(document.getElementById('insured-${target.row}').textContent),
      };
    })()
  `);
  check("после перезагрузки в базе те же два числа",
        afterReload.total === expected && afterReload.insured === expected,
        `итого ${afterReload.total}, база ${afterReload.insured}`);

  // --- T066: отказ виден у ячейки, значение возвращается --------------------
  //
  // Правится ПЕРВАЯ строка: именно на ней сверка нашла объяснение за экраном.

  // Сначала кладём в ячейку нормальное число: возврат прежнего значения на
  // пустой ячейке ничего бы не доказал.
  await evalIn(`
    (() => {
      const input = document.querySelector('#timesheet-grid tbody tr input.cell');
      input.focus();
      input.select();
    })()
  `);
  await key("Backspace", "Backspace", 8);
  await type("12");
  await key("Enter", "Enter", 13);
  await new Promise((r) => setTimeout(r, 1000));

  const first = await evalIn(`
    (() => {
      const input = document.querySelector('#timesheet-grid tbody tr input.cell');
      input.focus();
      input.select();
      return { row: input.dataset.row, kind: input.dataset.kind, before: input.value };
    })()
  `);
  check("в первой строке лежит число, которое можно потерять", first.before === "12.00",
        first.before);
  const logsBefore = logs.length;
  await key("Backspace", "Backspace", 8);
  await type("восемь");
  await key("Enter", "Enter", 13);
  await new Promise((r) => setTimeout(r, 1000));

  const refusal = await evalIn(`
    (() => {
      const box = document.getElementById('cell-error');
      const input = document.querySelector(
        'input.cell[data-row="${first.row}"][data-kind="${first.kind}"]');
      const cell = input.getBoundingClientRect();
      const r = box.getBoundingClientRect();
      const neighbour = [...document.querySelectorAll('input.cell')]
        .find(i => !i.classList.contains('failed'));
      return {
        shown: !box.hidden,
        text: box.textContent.trim(),
        inViewport: r.top >= 0 && r.bottom <= innerHeight && r.left >= 0 && r.right <= innerWidth,
        top: Math.round(r.top),
        distance: Math.round(Math.min(Math.abs(r.top - cell.bottom), Math.abs(cell.top - r.bottom))),
        failed: input.classList.contains('failed'),
        invalid: input.getAttribute('aria-invalid'),
        border: getComputedStyle(input).borderColor,
        neighbourBorder: getComputedStyle(neighbour).borderColor,
        weight: getComputedStyle(input).fontWeight,
        value: input.value,
      };
    })()
  `);
  check("объяснение отказа показано", refusal.shown, refusal.text);
  check("объяснение видно без прокрутки в окне 1440×900", refusal.inViewport,
        "верх подсказки на " + refusal.top);
  check("объяснение рядом с ячейкой, а не внизу документа", refusal.distance <= 60,
        refusal.distance + " px от ячейки");
  check("ячейка отчётливо помечена", refusal.failed && refusal.invalid === "true"
        && refusal.border !== refusal.neighbourBorder,
        `рамка ${refusal.border} против ${refusal.neighbourBorder}, начертание ${refusal.weight}`);
  check("в поле вернулось прежнее значение, а не мусор", refusal.value === first.before,
        `«${refusal.value}» при прежнем «${first.before}»`);

  await goto(APP + gridHref);
  const stored = await evalIn(`
    document.querySelector('input.cell[data-row="${first.row}"][data-kind="${first.kind}"]').value
  `);
  check("в базе после отказа тоже прежнее значение", stored === first.before, stored);

  const noise = logs.slice(logsBefore);
  check("в консоли только ожидаемый отказ 422",
        noise.every((line) => line.includes("422")),
        noise.join(" | ").slice(0, 200) || "пусто");

  // --- расчёт после правки часов проходит ----------------------------------

  await goto(APP + periodHref);
  await evalIn(`document.querySelector('form[action$="/calculate/"]').submit()`);
  await new Promise((r) => setTimeout(r, 2500));
  const calculated = await evalIn(`({
    alert: !!document.querySelector('.alert'),
    ok: !!document.querySelector('.ok'),
    text: (document.querySelector('.alert') || {}).textContent || "",
  })`);
  check("период считается после правки часов", calculated.ok && !calculated.alert,
        calculated.text.trim().slice(0, 160));
}

// --------------------------------------------------------------------------
if (STAGE === "mismatch") {
  const marked = await evalIn(`
    (() => {
      const num = ${NUM};
      const cells = [...document.querySelectorAll('td.insured-mismatch')];
      return {
        count: cells.length,
        text: cells.map(td => td.textContent.trim()).join(" | ").slice(0, 120),
        background: cells.length ? getComputedStyle(cells[0]).backgroundColor : "",
        title: cells.length ? cells[0].getAttribute('title') : "",
      };
    })()
  `);
  check("расхождение базы взносов помечено в сетке", marked.count === 1,
        `${marked.count} строк: ${marked.text}`);
  check("рядом видно, сколько часов входит в базу",
        /по часам/.test(marked.text) && /не считается/.test(marked.title || ""), marked.title);

  await goto(APP + periodHref);
  await evalIn(`document.querySelector('form[action$="/calculate/"]').submit()`);
  await new Promise((r) => setTimeout(r, 2500));
  const refused = await evalIn(`({
    alert: !!document.querySelector('.alert'),
    text: (document.querySelector('.alert') || {}).textContent || "",
  })`);
  check("расчёт периода отказывает, а не считает молча",
        refused.alert && /база для взносов/.test(refused.text),
        refused.text.replace(/\\s+/g, " ").trim().slice(0, 200));
}

// --------------------------------------------------------------------------
if (STAGE === "norules") {
  const page = await evalIn(`({
    status: window.__status || null,
    alert: (document.querySelector('.alert') || {}).textContent || "",
    serverError: /Server Error/i.test(document.body.textContent),
    title: document.title,
  })`);
  check("табель не отдаёт голое «Server Error»", !page.serverError, page.title);
  check("на табеле то же объяснение, что на странице периода",
        /нет правил расчёта/.test(page.alert) && /load_presets/.test(page.alert),
        page.alert.replace(/\s+/g, " ").trim().slice(0, 200));
}

report();
