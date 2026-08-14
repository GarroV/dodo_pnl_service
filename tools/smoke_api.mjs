/*
 * Смоук расходов по HTTP (T112): настоящие запросы тремя ролями.
 *
 * Что здесь проверяется такого, чего не докажут тесты. Тесты ходят клиентом
 * Django: посредники там настоящие, но CSRF выключен, а ответ разбирает тот же
 * процесс, который его собрал. Здесь запросы уходят из живой страницы живого
 * браузера — с настоящей сессией, настоящей cookie и настоящим ключом CSRF, —
 * то есть ровно тем способом, которым в вызов пойдёт бот или обёртка.
 *
 * Главная проверка — **сравнение**: один и тот же сценарий проходится экраном и
 * запросом, и ответы обязаны совпасть. Совпадение отказов проверяется отдельно
 * и буквально: чужая точка, чужой регистр и выдуманный номер отвечают
 * одинаково, иначе перебор значений через обёртку становится способом узнать
 * состав скрытого (D014, D023).
 *
 *     google-chrome --headless=new --remote-debugging-port=9386 \
 *         --user-data-dir=/tmp/chrome-smoke-api &
 *     COMPOSE_PROJECT_NAME=dodo-pnl-cash3 APP=http://127.0.0.1:8086 CDP_PORT=9386 \
 *         node tools/smoke_api.mjs
 */
import { attach, loginWith, sql, standFromSeed } from "./cdp.mjs";

const APP = process.env.APP || "http://127.0.0.1:8086";

const { evalIn, goto, check, report, logs } = await attach();
const loginRaw = loginWith(APP, evalIn, goto);

standFromSeed();

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function login(who) {
  for (let attempt = 0; attempt < 10; attempt++) {
    await loginRaw(who);
    if (await evalIn(`!!document.querySelector(".who")`)) return true;
    await sleep(500);
  }
  return false;
}

/**
 * Запрос из страницы: тот же документ, та же сессия, тот же ключ CSRF.
 *
 * Ключ берётся из cookie, как его берёт любой клиент с сессией. Отдельного
 * способа представиться у вызова нет и не должно быть: роль и тенант приезжают
 * контекстом базы, а не параметром (условие 1 спеки).
 */
async function call(method, path, body = null, { csrf = true } = {}) {
  const init = {
    method,
    headers: {},
    credentials: "same-origin",
  };
  if (body !== null) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  return evalIn(`
    (async () => {
      const token = document.cookie.split("; ")
        .find((row) => row.startsWith("csrftoken="))?.split("=")[1] || "";
      const init = ${JSON.stringify(init)};
      ${csrf ? 'init.headers["X-CSRFToken"] = token;' : ""}
      const answer = await fetch(${JSON.stringify(path)}, init);
      const text = await answer.text();
      let parsed = null;
      try { parsed = JSON.parse(text); } catch (e) { parsed = null; }
      return {
        status: answer.status,
        kind: answer.headers.get("content-type") || "",
        allow: answer.headers.get("allow") || "",
        text,
        json: parsed,
      };
    })()
  `);
}

/** Строки списка, как их показал экран: сумма и состояние машиночитаемо. */
async function screenRows(query) {
  await goto(`${APP}/expenses/${query}`);
  return evalIn(`
    Array.from(document.querySelectorAll("[data-fact]")).map((row) => ({
      id: row.getAttribute("data-fact"),
      amount: row.getAttribute("data-amount"),
      state: row.getAttribute("data-state"),
    }))
  `);
}

async function screenTotal() {
  return evalIn(`document.querySelector("[data-total]")?.getAttribute("data-total") || null`);
}

// --- материал -----------------------------------------------------------------
//
// Справочник статей поставляется пустым (Q015), поэтому статью для смоука
// заводит сам смоук — и убирает за собой вместе с фактами: `seed_dev` в конце
// вернёт тенант, но статья и её расходы держат точки внешними ключами.

const tenant = sql("select id from tenants where code = 'rs-dev'");
const pnlItem = sql("select id from pnl_items where code = 'food_cost'");
sql(`insert into expense_items (tenant_id, code, titles, pnl_item_id, valid_from)
     values ('${tenant}', 'smoke-api',
             '{"ru":"Вода","en":"Water","sr-latn":"Voda"}'::jsonb,
             '${pnlItem}', '2020-01-01')
     on conflict do nothing`);
const item = sql(`select id from expense_items where code = 'smoke-api'`);
const units = Object.fromEntries(
  sql(`select code || ' ' || id from units where tenant_id = '${tenant}' order by code`)
    .split("\n")
    .map((line) => line.trim().split(" ")),
);

const MONTH = new Date().toISOString().slice(0, 7);
const DAY = `${MONTH}-05`;
const WIDE = `?from=2026-06-01&to=2026-12-31`;

const key = () => crypto.randomUUID();

// --- бухгалтер: вносит расходы и сверяет список с экраном ----------------------

if (!(await login("accountant"))) {
  check("бухгалтер вошёл", false);
  report();
  process.exit(1);
}
check("бухгалтер вошёл", true);

const written = {};
for (const [code, amount] of [["NS1", "100.00"], ["NS2", "200.00"], ["BG1", "300.00"]]) {
  const recorded = await call("POST", "/api/expenses/", {
    date: DAY, amount, item, unit: units[code], note: `вода ${code}`,
    entry_key: key(), ledger: "official",
  });
  written[code] = recorded.json?.fact_id;
  check(
    `расход на ${code} записан запросом`,
    recorded.status === 200 && recorded.json?.action === "inserted",
    `${recorded.status} ${recorded.text.slice(0, 200)}`,
  );
}

const accountantApi = await call("GET", `/api/expenses/${WIDE}`);
const accountantScreen = await screenRows(WIDE);
const accountantTotal = await screenTotal();

check(
  "бухгалтер: запрос и экран показывают одни и те же строки",
  JSON.stringify(accountantApi.json.rows.map((r) => [r.id, r.amount])) ===
    JSON.stringify(accountantScreen.map((r) => [r.id, r.amount])),
  `${JSON.stringify(accountantApi.json.rows.map((r) => r.amount))} против ` +
    `${JSON.stringify(accountantScreen.map((r) => r.amount))}`,
);
check(
  "бухгалтер: итог запроса равен итогу экрана",
  Number(accountantApi.json.total) === Number(accountantTotal),
  `${accountantApi.json.total} против ${accountantTotal}`,
);

// --- управляющий: свой срез, и отказы ничего не рассказывают -------------------

if (!(await login("manager"))) {
  check("управляющий вошёл", false);
  report();
  process.exit(1);
}
check("управляющий вошёл", true);

const managerApi = await call("GET", `/api/expenses/${WIDE}`);
const managerScreen = await screenRows(WIDE);
check(
  "управляющий: запрос и экран показывают одни и те же строки",
  JSON.stringify(managerApi.json.rows.map((r) => r.id)) ===
    JSON.stringify(managerScreen.map((r) => r.id)),
  `${JSON.stringify(managerApi.json.rows.map((r) => r.id))}`,
);
check(
  "управляющий: чужих точек в ответе нет ни строкой, ни кодом",
  !managerApi.text.includes("BG1") && !managerApi.text.includes("NS2"),
  managerApi.text.slice(0, 300),
);

const byForeignUnit = await call("GET", `/api/expenses/${WIDE}&unit=${units.BG1}`);
const byMadeUpUnit = await call("GET", `/api/expenses/${WIDE}&unit=${crypto.randomUUID()}`);
check(
  "чужая точка в отборе неотличима от выдуманной",
  byForeignUnit.text === byMadeUpUnit.text && byForeignUnit.json.rows.length === 0,
  `${byForeignUnit.text.slice(0, 200)} против ${byMadeUpUnit.text.slice(0, 200)}`,
);

const hiddenLedger = await call("GET", `/api/expenses/${WIDE}&ledger=internal`);
const madeUpLedger = await call("GET", `/api/expenses/${WIDE}&ledger=no-such-ledger`);
check(
  "невидимый регистр неотличим от выдуманного слова",
  hiddenLedger.text === madeUpLedger.text && hiddenLedger.status === 200,
  `${hiddenLedger.status}/${madeUpLedger.status}: ` +
    `${hiddenLedger.text.slice(0, 160)} против ${madeUpLedger.text.slice(0, 160)}`,
);

const foreignCard = await call("GET", `/api/expenses/${written.BG1}/`);
const madeUpCard = await call("GET", `/api/expenses/${crypto.randomUUID()}/`);
check(
  "чужой расход неотличим от выдуманного номера",
  foreignCard.status === 404 && foreignCard.text === madeUpCard.text,
  `${foreignCard.status}: ${foreignCard.text.slice(0, 200)}`,
);
check(
  "отказ приходит разбираемым ответом, а не страницей на сорок килобайт",
  foreignCard.kind.startsWith("application/json"),
  foreignCard.kind,
);

const toForeignUnit = await call("POST", "/api/expenses/", {
  date: DAY, amount: "5.00", item, unit: units.BG1, entry_key: key(),
});
const toMadeUpUnit = await call("POST", "/api/expenses/", {
  date: DAY, amount: "5.00", item, unit: crypto.randomUUID(), entry_key: key(),
});
check(
  "запись в чужую точку отвергнута теми же словами, что в выдуманную",
  toForeignUnit.status === 400 && toForeignUnit.text === toMadeUpUnit.text,
  `${toForeignUnit.status}: ${toForeignUnit.text.slice(0, 200)}`,
);

const ownExpense = managerApi.json.rows[0]?.id;
const getOnDelete = await call("GET", `/api/expenses/${ownExpense}/delete/`);
check(
  "удаление по GET не проходит и называет разрешённый метод",
  getOnDelete.status === 405 && getOnDelete.allow.includes("POST"),
  `${getOnDelete.status} allow=${getOnDelete.allow}`,
);

const withoutToken = await call("POST", "/api/expenses/", {
  date: DAY, amount: "9.00", item, unit: units.NS1, entry_key: key(),
}, { csrf: false });
check(
  "запись без ключа CSRF отвергнута: дверь та же, что у формы",
  withoutToken.status === 403,
  `${withoutToken.status}`,
);

const stillThere = await call("GET", `/api/expenses/${WIDE}`);
check(
  "ни один отвергнутый вызов ничего не записал",
  stillThere.json.rows.length === managerApi.json.rows.length,
  `${stillThere.json.rows.length} против ${managerApi.json.rows.length}`,
);

// --- директор: правка, удаление, нераспределённое и пересчёт ------------------

if (!(await login("director"))) {
  check("директор вошёл", false);
  report();
  process.exit(1);
}
check("директор вошёл", true);

const edited = await call("POST", `/api/expenses/${written.NS1}/`, {
  date: DAY, amount: "155.55", item, unit: units.NS1, note: "правка запросом",
});
check(
  // `updated` — это и есть замена версии: старая строка помечается заменённой,
  // новая встаёт рядом. `inserted` здесь означало бы вторую запись теми же
  // деньгами, то есть удвоенный расход.
  "правка запросом заводит новую версию, а не второй расход",
  edited.status === 200 && edited.json?.action === "updated",
  `${edited.status} ${edited.text.slice(0, 200)}`,
);

const afterEdit = await call("GET", `/api/expenses/${WIDE}`);
const editedRow = afterEdit.json.rows.find((row) => row.amount === "155.55");
check("правка видна в списке", !!editedRow, JSON.stringify(afterEdit.json.rows.map((r) => r.amount)));

const removed = await call("POST", `/api/expenses/${editedRow.id}/delete/`);
check(
  "удаление помечает строку, а не стирает её",
  removed.status === 200 && removed.json?.state === "removed",
  `${removed.status} ${removed.text.slice(0, 200)}`,
);
const afterDelete = await call("GET", `/api/expenses/${WIDE}`);
check(
  "удалённая строка осталась видимой с состоянием",
  afterDelete.json.rows.some((row) => row.id === editedRow.id && row.state === "removed"),
  JSON.stringify(afterDelete.json.rows.map((r) => [r.amount, r.state])),
);

const network = await call("POST", "/api/expenses/", {
  date: DAY, amount: "999.99", item, unit: "network", note: "аренда офиса",
  entry_key: key(),
});
check(
  "расход на всю сеть записан и сказано, что правила разнесения нет",
  network.status === 200 && network.json?.allocation?.state === "waiting",
  `${network.status} ${network.text.slice(0, 200)}`,
);

const waiting = await call("GET", "/api/expenses/unallocated/");
check(
  "нераспределённое видно запросом и не потерялось молча",
  waiting.status === 200 &&
    waiting.json.rows.some((row) => row.id === network.json.fact_id),
  `${waiting.status} ${waiting.text.slice(0, 200)}`,
);

const withoutMonth = await call("POST", "/api/expenses/allocate/", {});
check(
  "пересчёт без месяца отвергнут: соединение неограниченным обходом не держится",
  withoutMonth.status === 400 && !!withoutMonth.json?.error,
  `${withoutMonth.status} ${withoutMonth.text.slice(0, 200)}`,
);

const oneMonth = await call("POST", "/api/expenses/allocate/", { period: MONTH });
check(
  "пересчёт месяца отвечает числом изменений",
  oneMonth.status === 200 && oneMonth.json?.period === MONTH,
  `${oneMonth.status} ${oneMonth.text.slice(0, 200)}`,
);

// --- окно списка --------------------------------------------------------------

const firstPage = await call("GET", `/api/expenses/${WIDE}&limit=1`);
// Итог страницы — сумма её **действующих** строк: заменённые и удалённые видны,
// но в счёт не идут (то же правило, что на экране).
const pageTotal = firstPage.json.rows
  .filter((row) => row.state === "active")
  .reduce((sum, row) => sum + Number(row.amount), 0);
check(
  "список ограничен окном, говорит, что есть ещё, и считает итог по показанному",
  firstPage.json.rows.length === 1 && firstPage.json.has_more === true &&
    Number(firstPage.json.total) === pageTotal,
  JSON.stringify(firstPage.json).slice(0, 300),
);

const secondPage = await call("GET", `/api/expenses/${WIDE}&limit=1&offset=1`);
check(
  "следующая страница отдаёт другую строку",
  secondPage.json.rows.length === 1 &&
    secondPage.json.rows[0].id !== firstPage.json.rows[0].id,
  JSON.stringify(secondPage.json.rows.map((r) => r.id)),
);

// --- никто без входа ----------------------------------------------------------

await goto(`${APP}/login/`);
await evalIn(`
  (() => {
    const form = document.querySelector('form[action="/logout/"]');
    if (form) form.submit();
  })()
`);
await sleep(1000);
await goto(`${APP}/login/`);
const anonymous = await call("GET", `/api/expenses/${WIDE}`);
check(
  "не вошёл — 401 разбираемым ответом, а не страницей входа",
  anonymous.status === 401 && anonymous.kind.startsWith("application/json"),
  `${anonymous.status} ${anonymous.kind} ${anonymous.text.slice(0, 160)}`,
);

const noise = logs.filter((line) => !/favicon|DevTools/i.test(line));
check("в консоли браузера чисто", noise.length === 0, noise.slice(0, 3).join(" | "));

report();
