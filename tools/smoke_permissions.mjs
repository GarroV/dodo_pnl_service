/*
 * Смоук прав на экране (T072) и отказа на записи ячейки (T073).
 *
 * Проверяет ровно то, что нельзя доказать разбором разметки: что живой человек
 * под каждой из четырёх ролей видит на странице периода и в табеле, и что
 * отказ на досылке ячейки доезжает до экрана плашкой, а не «Server Error».
 *
 * Клики и ввод — настоящими событиями мыши и клавиатуры (`Input.dispatch*`),
 * а не вызовами обработчиков: обработчик, вызванный напрямую, доказывает
 * работоспособность обработчика, а не экрана.
 *
 * Стенд смоук приводит к сиду сам — и в начале, и после себя (см. договор в
 * шапке `cdp.mjs`). Поэтому запускать его можно в любом порядке и в одиночку.
 *
 *     COMPOSE_PROJECT_NAME=<стенд> APP=http://127.0.0.1:8047 \
 *         node tools/smoke_permissions.mjs
 */
import { attach, findPeriodAndGrid, loginWith, onCleanup, sql, standFromSeed } from "./cdp.mjs";

const APP = process.env.APP || "http://127.0.0.1:8047";

// Кому что положено — из сида (`seed_dev.ROLES`), а не из головы.
const ROLES = [
  { code: "director", calculate: true, edit: true },
  { code: "accountant", calculate: true, edit: true },
  { code: "manager", calculate: false, edit: true },
  { code: "admin", calculate: false, edit: false },
];

const { evalIn, goto, send, key, type, clickOn, check, report, logs } = await attach();
const login = loginWith(APP, evalIn, goto);

// Стенд к эталону сейчас и обратно к нему после — в том числе если смоук упадёт
// на полпути (issues #76, #80). Этот смоук пишет в табель и выключает правила
// расчёта: раньше ячейка оставалась изменённой, и следующие смоуки краснели на
// контрольных числах при исправном продукте.
standFromSeed();

// Ждём условие, подглядывая в живую страницу, вместо того чтобы спать заранее
// угаданное число секунд: фиксированная пауза либо ждёт дольше нужного, либо
// (как здесь и было) рвёт проверку раньше, чем стенд успевает ответить.
async function waitFor(expr, seconds) {
  for (let i = 0; i < seconds * 5; i++) {
    if (await evalIn(expr).catch(() => false)) return true;
    await new Promise((r) => setTimeout(r, 200));
  }
  return false;
}

const state = {};

// Период считается один раз, настоящим нажатием кнопки под директором: без
// расчёта ведомости на странице нет, и числа-ориентиры сверять было бы не с чем.
await login("director");
{
  const { periodHref } = await findPeriodAndGrid(APP, evalIn, goto);
  await goto(APP + periodHref);
  // Через общий `clickOn`: он сам прокручивает страницу к кнопке и отказывается
  // бить по координатам, которых нет в окне. Раньше клик по кнопке за нижним
  // краем окна уходил в пустоту, и краснела следующая проверка — «ни фразы
  // «Расчёт выполнен», ни полосы прогресса» (issue #81).
  const button = `[...document.querySelectorAll("button")]
        .find(x => x.textContent.includes("Посчитать период"))`;
  check("директор видит кнопку расчёта", await evalIn(`!!(${button})`));
  await clickOn(button, "кнопка «Посчитать период»");

  // «Расчёт выполнен» появляется только когда расчёт идёт прямо в запросе
  // (`?calculated=1`). На стенде по умолчанию включена очередь
  // (PAYRUN_BACKGROUND=1): нажатие уводит на `?queued=1`, и вместо фразы на
  // странице встаёт полоса `section.progress` (см. `period_calculate` и
  // `period.html`). Этой фразы в фоновом режиме не бывает никогда — ждать
  // её означало бы гонять смоук вслепую только на одном из двух режимов
  // стенда. Какой режим — решает стенд, не смоук: годится любой из исходов.
  const started = await waitFor(
    `document.body.innerText.includes("Расчёт выполнен") ||
     !!document.querySelector("section.progress")`,
    10,
  );
  check("расчёт запущен нажатием кнопки", started,
    "ни фразы «Расчёт выполнен», ни полосы прогресса за 10 секунд");

  // Дальше смоуку нужна готовая ведомость (числа по ролям ниже сверяются по
  // table.sheet). При фоновом расчёте страница сама перезагрузится, когда
  // рабочий процесс очереди закончит считать (см. tick() в period.html) —
  // ждём появления таблицы с потолком, а не спим фиксированное время и не
  // гадаем, сколько займёт расчёт на конкретном стенде.
  const ready = await waitFor(`!!document.querySelector("table.sheet")`, 30);
  check("ведомость посчитана и показана", ready,
    "table.sheet не появился за 30 секунд ожидания");
}

for (const role of ROLES) {
  await login(role.code);
  const { periodHref, gridHref } = await findPeriodAndGrid(APP, evalIn, goto);

  // --- страница периода ------------------------------------------------------
  await goto(APP + periodHref);
  const page = await evalIn(`
    (() => {
      const button = [...document.querySelectorAll("button")]
        .find(b => b.textContent.includes("Посчитать период"));
      const rows = document.querySelectorAll("table.sheet tbody tr").length;
      const total = document.querySelector("table.sheet tfoot .num:last-child");
      const norm = [...document.querySelectorAll("dl.facts dt")]
        .findIndex(dt => dt.textContent.includes("Норма часов"));
      const dd = document.querySelectorAll("dl.facts dd");
      return {
        button: !!button,
        forms: document.querySelectorAll('form[action*="calculate/"]').length,
        explained: document.body.innerText.includes("Расчёт периода не входит в права"),
        rows,
        total: total ? total.textContent.trim() : "",
        norm: norm >= 0 ? dd[norm].textContent.trim() : "",
        role: document.body.innerText,
      };
    })()
  `);

  check(`${role.code}: кнопка «Посчитать период» ${role.calculate ? "есть" : "скрыта"}`,
    page.button === role.calculate && page.forms === (role.calculate ? 1 : 0));
  check(`${role.code}: запрет расчёта объяснён словами`,
    page.explained === !role.calculate);
  state[role.code] = { rows: page.rows, total: page.total, norm: page.norm };
  console.log(`      строк ведомости ${page.rows}, итог ${page.total}, норма ${page.norm}`);

  // --- табель ----------------------------------------------------------------
  await goto(APP + gridHref);
  const grid = await evalIn(`
    (() => {
      const inputs = document.querySelectorAll("input.cell").length;
      const rows = document.querySelectorAll("#timesheet-grid tbody tr").length;
      return {
        inputs,
        rows,
        htmx: typeof window.htmx !== "undefined",
        total: (document.getElementById("grand-total") || {}).textContent || "",
        explained: document.body.innerText.toLowerCase()
          .includes("правка табеля не входит в права"),
      };
    })()
  `);

  check(`${role.code}: сетка ${role.edit ? "редактируемая" : "на чтение"}`,
    role.edit ? grid.inputs > 0 : grid.inputs === 0,
    `полей ввода ${grid.inputs}, строк ${grid.rows}, итог ${grid.total}`);
  check(`${role.code}: запрет правки объяснён словами`, grid.explained === !role.edit);
  check(`${role.code}: htmx ${role.edit ? "поднят" : "не грузится на страницу чтения"}`,
    grid.htmx === role.edit);
  state[role.code].gridRows = grid.rows;
  state[role.code].gridTotal = grid.total;
}

// --- сетка на чтение показывает те же данные ---------------------------------
// Запрет правки не должен превращаться в скрытие данных: администратор видит
// весь табель тенанта, как и директор.
check("администратор видит в табеле те же строки и тот же итог, что директор",
  state.admin.gridRows === state.director.gridRows &&
    state.admin.gridTotal === state.director.gridTotal,
  `${state.admin.gridRows} строк, итог ${state.admin.gridTotal}`);

// --- клик по единственной оставшейся кнопке ----------------------------------
// Управляющий: кнопки расчёта нет, но ссылка на табель рядом с ней должна
// работать — иначе «спрятали кнопку» означало бы «сломали строку действий».
await login("manager");
{
  const { periodHref } = await findPeriodAndGrid(APP, evalIn, goto);
  await goto(APP + periodHref);
  const link = `[...document.querySelectorAll("a")]
        .find(a => a.textContent.trim() === "Табель")`;
  check("управляющий: ссылка «Табель» на месте", await evalIn(`!!(${link})`));
  await clickOn(link, "ссылка «Табель»");
  await new Promise((r) => setTimeout(r, 1200));
  const url = await evalIn("location.pathname");
  check("управляющий: клик по «Табель» открывает табель", url.startsWith("/timesheets/"), url);
}

// --- T073: отказ на записи ячейки --------------------------------------------
// Правила на месяц выключаются на время проверки прямо в базе стенда: сценарий
// узкий и другим способом не воспроизводится.
// Правила стран лежат ВНЕ тенанта, поэтому сид их не возвращает: он сносит и
// заводит заново данные партнёра, а `rule_presets` только дописывает по ключу
// (код, дата начала). Сдвинутую дату он не починит — заведёт вторую строку.
// Значит возврат обязан быть свой и обязан пережить падение смоука: без него
// следующий прогон получал «в базе нет правил расчёта» и выглядел сломанным
// продуктом (issue #80).
function rulesShiftedTo(year) {
  sql(`update rule_presets set valid_from = '${year}-01-01'`);
}

await login("director");
{
  const { gridHref } = await findPeriodAndGrid(APP, evalIn, goto);
  await goto(APP + gridHref);

  // Ставим известное число обычным путём: клик в ячейку, набор, уход фокуса.
  const cell = await evalIn(`
    (() => {
      const input = document.querySelector("input.cell");
      input.scrollIntoView({ block: "center" });
      const r = input.getBoundingClientRect();
      return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
    })()
  `);
  for (const t of ["mousePressed", "mouseReleased"]) {
    await send("Input.dispatchMouseEvent", { type: t, x: cell.x, y: cell.y, button: "left", clickCount: 1 });
  }
  await evalIn(`document.activeElement.select()`);
  await type("12");
  await key("Enter", "Enter", 13);
  await new Promise((r) => setTimeout(r, 900));
  const saved = await evalIn(`document.querySelector("input.cell").value`);
  check("ячейка сохранена обычным путём", saved === "12.00", saved);

  // Уборка регистрируется ДО сдвига, а не после: падение случается между
  // строками, а не после них.
  onCleanup("правила расчёта возвращены на место", () => rulesShiftedTo(2026));
  rulesShiftedTo(2030);
  try {
    // Тот же ввод, но правил на месяц больше нет.
    for (const t of ["mousePressed", "mouseReleased"]) {
      await send("Input.dispatchMouseEvent", { type: t, x: cell.x, y: cell.y, button: "left", clickCount: 1 });
    }
    await evalIn(`document.activeElement.select()`);
    await type("9");
    await key("Enter", "Enter", 13);
    await new Promise((r) => setTimeout(r, 1200));

    const refusal = await evalIn(`
      (() => {
        const box = document.getElementById("cell-error");
        const input = document.querySelector("input.cell");
        const r = box.hidden ? null : box.getBoundingClientRect();
        const c = input.getBoundingClientRect();
        return {
          shown: !box.hidden,
          text: box.textContent,
          distance: r ? Math.round(Math.abs(r.top - c.bottom)) : null,
          inWindow: r ? r.top >= 0 && r.bottom <= window.innerHeight : false,
          value: input.value,
          invalid: input.getAttribute("aria-invalid"),
        };
      })()
    `);
    check("T073: отказ показан у ячейки, а не «Server Error»",
      refusal.shown && refusal.text.includes("нет правил расчёта"), refusal.text.slice(0, 90));
    check("T073: плашка в окне, рядом с ячейкой", refusal.inWindow && refusal.distance < 60,
      `${refusal.distance} px от ячейки`);
    check("T073: в ячейке значение из базы", refusal.value === "12.00", refusal.value);
    check("T073: ячейка помечена для доступности", refusal.invalid === "true");

    // Страница целиком в том же состоянии объясняет то же самое (T062).
    await goto(APP + gridHref);
    const page = await evalIn(`document.body.innerText`);
    check("страница табеля объясняет то же самое",
      page.includes("нет правил расчёта") && page.includes("load_presets"));
  } finally {
    rulesShiftedTo(2026);
  }

  // Правила вернули — ячейка снова пишется, и в базе то, что показано.
  await goto(APP + gridHref);
  const back = await evalIn(`document.querySelector("input.cell").value`);
  check("после возврата правил ячейка снова редактируется", back === "12.00", back);
}

// --- контуры отказа на сервере остались на месте -----------------------------
// Скрытая кнопка — не контур доступа. Адреса рабочие, и роль без права обязана
// получать на них тот же отказ, что и до T072: запросы идут настоящим fetch со
// страницы, с её же защитой от подделки, — то есть ровно так, как их отправил
// бы подделанный интерфейс.
// Ответ на расчёт — целая страница периода с плашкой, поэтому наличие текста
// проверяется по всему телу, а наружу отдаётся только признак и начало ответа:
// сверять срез первых строк значило бы сверять <head>.
async function post(path, params, expect) {
  return evalIn(`
    (async () => {
      const token = document.querySelector("[name=csrfmiddlewaretoken]").value;
      const r = await fetch(${JSON.stringify(path)}, {
        method: "POST",
        headers: { "X-CSRFToken": token },
        body: new URLSearchParams(${JSON.stringify(params)}),
      });
      const text = await r.text();
      return {
        status: r.status,
        found: text.includes(${JSON.stringify(expect)}),
        text: text.replace(/<[^>]+>/g, " ").replace(/\\s+/g, " ").trim().slice(0, 160),
      };
    })()
  `);
}

await login("admin");
{
  const { periodHref, gridHref } = await findPeriodAndGrid(APP, evalIn, goto);
  await goto(APP + periodHref);
  const calculate = await post(
    periodHref + "calculate/", {}, "Расчёт периода не входит в права",
  );
  check("сервер по-прежнему отказывает администратору в расчёте",
    calculate.status === 403 && calculate.found, `${calculate.status}: ${calculate.text}`);

  // Строку табеля берём глазами директора: у администратора её id на странице
  // теперь тоже есть (сетка на чтение), но честнее взять адрес так, как его
  // взял бы подделывающий запрос — со страницы того, кто правит.
  await login("director");
  await goto(APP + gridHref);
  const row = await evalIn(`
    (() => {
      const i = document.querySelector("input.cell");
      return { row: i.dataset.row, kind: i.dataset.kind };
    })()
  `);
  await login("admin");
  await goto(APP + gridHref);
  const cell = await post(
    gridHref + "cell/", { row: row.row, kind: row.kind, hours: "7" },
    "Правка табеля не входит в права",
  );
  check("сервер по-прежнему отказывает администратору в правке ячейки",
    cell.status === 403 && cell.found, `${cell.status}: ${cell.text}`);

  await login("director");
  await goto(APP + gridHref);
  const kept = await evalIn(`document.querySelector("input.cell").value`);
  check("отказ ничего не изменил в базе", kept === "12.00", kept);
}

const noise = logs.filter((line) => !/422|409|403|Failed to load resource/.test(line));
check("консоль браузера чиста", noise.length === 0, noise.join(" | ").slice(0, 200));

console.log("\nЧисла по ролям:", JSON.stringify(state, null, 1));
report();
