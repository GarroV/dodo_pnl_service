/*
 * Смоук: отказ расчёта не называет тех, кого смотрящему не показывают (T114).
 *
 * Проверяет ровно то, чем утечка была видна человеку: живой человек под каждой
 * из четырёх ролей открывает свою обычную страницу месяца после неудавшегося
 * расчёта и читает (или не читает) поимённый список. Плюс тот же список
 * запросом, без страницы, — `GET /periods/<id>/calculate/status/`, которым
 * утечка и вылезала мимо экрана.
 *
 * Ломается то же, что ломается в живой системе правкой справочника: схема
 * расчёта, которой нет в правилах страны. Сначала у людей внутреннего регистра
 * (управляющему они не видны по регистру), потом у человека чужой точки
 * (не виден по точке).
 *
 * Стенд смоук приводит к сиду сам — и в начале, и после себя (договор в шапке
 * `cdp.mjs`).
 *
 *     COMPOSE_PROJECT_NAME=<стенд> APP=http://127.0.0.1:8080 CDP_PORT=9358 \
 *         node tools/smoke_refusal_slice.mjs
 */
import { attach, loginWith, sql, standFromSeed } from "./cdp.mjs";

const APP = process.env.APP || "http://127.0.0.1:8080";

// Внутренний регистр: управляющему NS1 эти строки не показывают нигде.
const HIDDEN_LEDGER = ["dev-courier-1", "dev-courier-2"];
// Чужая точка: регистр управляющему виден, а точка BG1 — нет.
const OTHER_UNIT = "UROS ANDRIC";

const { evalIn, goto, clickOn, check, report, logs } = await attach();
const login = loginWith(APP, evalIn, goto);

standFromSeed();

function breakScheme(names) {
  const list = names.map((name) => `'${name}'`).join(",");
  sql(
    `update employment_terms set scheme = 'no_such_scheme'
      where employee_id in (select id from employees where external_id in (${list}))`,
  );
}

function healSchemes() {
  // Между двумя случаями стенд обязан быть чистым: иначе второй отказ приехал
  // бы со списком первого, и проверка позеленела бы не по своей причине.
  sql(`update employment_terms t set scheme = g.scheme
         from employee_groups g
        where g.id = t.group_id and t.scheme = 'no_such_scheme'`);
}

async function periodHref() {
  await goto(`${APP}/periods/`);
  return evalIn(
    `[...document.querySelectorAll('a[href^="/periods/"]')]
       .map(a => a.getAttribute('href'))
       .find(h => /^\\/periods\\/[0-9a-f-]{36}\\/$/.test(h))`,
  );
}

/** Дождаться отказа на странице периода: расчёт идёт фоном. */
async function waitForRefusal(href, seconds = 60) {
  for (let i = 0; i < seconds * 2; i++) {
    await new Promise((r) => setTimeout(r, 500));
    await goto(APP + href);
    const failed = await evalIn(
      `document.body.innerText.includes("Расчёт не выполнен")`,
    );
    if (failed) return;
  }
  throw new Error("расчёт не отказал за отведённое время — смоуку нечего смотреть");
}

async function refuseCalculation(href) {
  await login("director");
  await goto(APP + href);
  await clickOn(
    `[...document.querySelectorAll("button")]
       .find(b => b.textContent.includes("Посчитать период"))`,
    "кнопка «Посчитать период»",
  );
  await waitForRefusal(href);
}

/** Ответ опроса состояния — тем же браузером и той же сессией. */
async function statusOf(href) {
  const raw = await evalIn(
    `fetch(${JSON.stringify(APP + href)} + "calculate/status/",
           { headers: { Accept: "application/json" } }).then(r => r.text())`,
  );
  return JSON.parse(raw);
}

async function pageText(who, href) {
  await login(who);
  await goto(APP + href);
  return evalIn(`document.body.innerText`);
}

// Вход раньше поиска адреса: анониму список периодов отвечает переходом на
// страницу входа, и ссылки на месяц там нет вовсе.
await login("director");
const href = await periodHref();

// --- случай 1: имена скрытого регистра ---------------------------------------

breakScheme(HIDDEN_LEDGER);
await refuseCalculation(href);

for (const who of ["director", "accountant", "admin"]) {
  const seen = await pageText(who, href);
  check(
    `${who}: видит, у кого именно не сошлось`,
    HIDDEN_LEDGER.every((name) => seen.includes(name)),
  );
}

let text = await pageText("manager", href);
check(
  "управляющий: имён внутреннего регистра на странице нет",
  HIDDEN_LEDGER.every((name) => !text.includes(name)),
);
check("управляющий: узнаёт, что расчёт не выполнен", text.includes("Расчёт не выполнен"));
check("управляющий: причина отказа названа", text.includes("нет схем расчёта"));
check("управляющий: сказано, почему списка нет", text.includes("весь расчёт партнёра"));

let state = await statusOf(href);
check("управляющий: опрос отдаёт пустые подробности", state.details.length === 0);
check(
  "управляющий: имён нет и в ответе опроса целиком",
  HIDDEN_LEDGER.every((name) => !JSON.stringify(state).includes(name)),
);

await login("director");
state = await statusOf(href);
check(
  "директор: опрос по-прежнему называет имена",
  state.details.length === HIDDEN_LEDGER.length,
);

// --- случай 2: человек чужой точки -------------------------------------------

healSchemes();
breakScheme([OTHER_UNIT]);
await refuseCalculation(href);

text = await pageText("manager", href);
check("управляющий: имени с чужой точки на странице нет", !text.includes(OTHER_UNIT));
state = await statusOf(href);
check(
  "управляющий: и в ответе опроса его нет",
  !JSON.stringify(state).includes(OTHER_UNIT),
);

text = await pageText("director", href);
check("директор: видит имя с любой точки", text.includes(OTHER_UNIT));

// --- счётчик расчёта ---------------------------------------------------------

healSchemes();
sql("delete from payrun_jobs");
sql(`insert into payrun_jobs (tenant_id, period, status, stage, done, total)
     select id, '2026-06-01', 'running'::payrun_job_status, 'Считаем', 20, 35
       from tenants where code = 'rs-dev'`);

await login("manager");
state = await statusOf(href);
check(
  `управляющий: счётчик не называет размер расчёта (done=${state.done}, total=${state.total})`,
  state.done === 0 && state.total === 0,
);
check("управляющий: полоса прогресса при этом жива", state.percent > 0);

await login("director");
state = await statusOf(href);
check("директор: счётчик на месте", state.done === 20 && state.total === 35);

if (logs.length) console.log("журнал консоли: " + logs.join(" | "));
report();
