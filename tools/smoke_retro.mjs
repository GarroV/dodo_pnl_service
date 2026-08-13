/*
 * Смоук правок задним числом (T026).
 *
 * Проверяет то, чего не доказывают ни тесты по базе, ни разбор разметки: что
 * живой человек **нажатием** переносит разницу закрытого месяца вперёд, что
 * закрытый месяц при этом остаётся тем же построчно, что разница видна в
 * текущем периоде и **объясняет себя** (за какой месяц она пришла), и что
 * после утверждения получателя источник заново уже не открывается.
 *
 * Клики и ввод — настоящими событиями мыши и клавиатуры: обработчик, вызванный
 * напрямую, доказывает работоспособность обработчика, а не экрана.
 *
 * Правка задним числом делается С ЭКРАНА (T121): администратор заводит версию
 * правила датой внутри закрытого месяца. Это не проверка, а **вход** — без
 * изменения данных «закрытый месяц не сдвинулся» ничего не доказывает, — но
 * вход обязан быть тем же, каким располагает живой человек: правка запросом в
 * базу доказывала бы механизм, к которому из продукта нет дороги.
 *
 * Стенд смоук приводит к сиду сам — и в начале, и после себя (см. договор в
 * шапке `cdp.mjs`). Поэтому запускать его можно в любом порядке и в одиночку,
 * а `COMPOSE_PROJECT_NAME` обязателен: без него сброс ушёл бы на чужой стенд.
 *
 *     COMPOSE_PROJECT_NAME=<стенд> APP=http://127.0.0.1:8055 \\
 *         node tools/smoke_retro.mjs
 */
import { attach, loginWith, sql, standFromSeed } from "./cdp.mjs";

const APP = process.env.APP || "http://127.0.0.1:8055";
const REFERENCE_TOTAL = "1 951 806,13";
const REOPEN_REASON = "смоук: попытка открыть месяц с уже выплаченной разницей";

const { evalIn, goto, send, check, report, logs } = await attach();
const login = loginWith(APP, evalIn, goto);

// Стенд к эталону сейчас и обратно к нему после — в том числе если смоук
// упадёт на полпути (issue #76). Порядок запуска смоуков больше ничего не
// решает: каждый начинает с известного входа и ничего за собой не оставляет.
standFromSeed();

// Подготовка данных, не проверка: ставки и учётный месяц получателя. Ходит
// `sql` харнесса — в базу стенда, названного в COMPOSE_PROJECT_NAME.
async function clickButton(text, nth = 0) {
  const box = await evalIn(`
    (() => {
      const all = [...document.querySelectorAll("button")]
        .filter(x => x.textContent.includes(${JSON.stringify(text)}));
      const b = all[${nth}];
      if (!b) return null;
      b.scrollIntoView({ block: "center" });
      const r = b.getBoundingClientRect();
      return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
    })()
  `);
  if (!box) return false;
  for (const t of ["mousePressed", "mouseReleased"]) {
    await send("Input.dispatchMouseEvent", {
      type: t, x: box.x, y: box.y, button: "left", clickCount: 1,
    });
  }
  await new Promise((r) => setTimeout(r, 2500));
  return true;
}

/** Что видно на странице периода. Ведомость снимается построчно целиком. */
const snapshot = () => evalIn(`
  (() => {
    const rows = [...document.querySelectorAll("table.sheet tbody tr")].map(tr => {
      const cells = [...tr.children];
      return {
        employee: cells[0].textContent.trim(),
        ledger: cells[2].textContent.trim(),
        total: cells[cells.length - 1].textContent.trim(),
        retro: tr.classList.contains("retro"),
        badge: (tr.querySelector(".badge-retro") || {}).textContent || "",
      };
    });
    const facts = [...document.querySelectorAll("dl.facts dt")];
    const i = facts.findIndex(dt => dt.textContent.includes("Расчёт зарплаты"));
    const dd = document.querySelectorAll("dl.facts dd");
    const norm = facts.findIndex(dt => dt.textContent.includes("Норма"));
    return {
      rows,
      count: rows.length,
      payrunStatus: i >= 0 ? dd[i].textContent.trim() : "",
      norm: norm >= 0 ? dd[norm].textContent.trim() : "",
      total: ((document.querySelector("table.sheet tfoot .num:last-child") || {}).textContent || "").trim(),
      retroSection: !!document.querySelector("section.retro"),
      retroButton: !!document.querySelector('form[action$="/retro/"] button'),
      reopenForm: !!document.querySelector('form[action$="/reopen/"]'),
      text: document.body.innerText,
    };
  })()
`);

/** Заполнить поля формы по имени. Значения кладутся в поля, жмёт человек. */
const fillFields = (values) => evalIn(`
  (() => {
    const values = ${JSON.stringify(values)};
    for (const [name, value] of Object.entries(values)) {
      const field = document.querySelector('[name=' + name + ']');
      if (!field) return "нет поля " + name;
      field.value = value;
    }
    return "";
  })()
`);

const logout = () => evalIn(`
  (() => { const f = document.querySelector('form[action="/logout/"]'); if (f) f.submit(); })()
`).then(() => new Promise((r) => setTimeout(r, 900)));

/** Досылка формы мимо экрана — той же сессией, что и у человека. */
const post = (action, fields = {}) => evalIn(`
  (async () => {
    const csrf = document.querySelector("[name=csrfmiddlewaretoken]").value;
    const body = new URLSearchParams({ csrfmiddlewaretoken: csrf, ...${JSON.stringify(fields)} });
    const r = await fetch(${JSON.stringify(action)}, { method: "POST", body, redirect: "follow" });
    return { status: r.status, text: await r.text() };
  })()
`);

/** Ссылка на страницу нужного месяца: список отсортирован по убыванию. */
async function pageOf(title) {
  await goto(APP + "/periods/");
  return evalIn(`
    [...document.querySelectorAll('table tbody tr')]
      .filter(tr => tr.children[0].textContent.includes(${JSON.stringify(title)}))
      .map(tr => tr.querySelector('a').getAttribute('href'))[0] || ""
  `);
}

const lines = (page) => page.rows.map((r) => r.employee + "|" + r.ledger + "|" + r.total);

// --- 1. Директор считает и утверждает июнь ------------------------------------
await login("director");
const june = await pageOf("Июнь 2026");
check("страница июня найдена", !!june, june);
await goto(APP + june);

let page = await snapshot();
if (!page.count) {
  await clickButton("Посчитать период");
  page = await snapshot();
}
check("ведомость директора: 60 строк", page.count === 60, String(page.count));
check(`итог директора ${REFERENCE_TOTAL}`, page.total === REFERENCE_TOTAL, page.total);
check("норма часов 176,00", page.norm.includes("176,00"), page.norm);
check("до правки блока о расхождении нет", !page.retroSection);

await clickButton("Утвердить период");
page = await snapshot();
check("июнь утверждён", page.payrunStatus === "Утверждён", page.payrunStatus);

const closedBefore = lines(page);
check("снимок закрытого месяца снят", closedBefore.length === 60, String(closedBefore.length));

// --- 2. Правка задним числом — С ЭКРАНА, а не запросом в базу (T121) ----------
// Раньше здесь стоял `update employment_terms ...` через psql, и это скрывало
// главное расхождение со спекой: продукт такую правку не принимал вовсе.
// Правку делает администратор сети на своей странице правил, датой внутри
// закрытого месяца.
await logout();
await login("admin");
// Правило выбрано такое, которое двигает деньги ВСЕМ и во всех регистрах
// (процент оплаты обычного часа): иначе «разница поехала вперёд» проверялось бы
// на подмножестве людей, а проверка регистров ниже стала бы бессмысленной.
// `rates.net_factor` для этого не годится — он двигает бруто и налоги, а они
// живут в итогах строки, не в её компонентах, и расхождения не дают.
//
// Дата — первое число закрытого месяца: правила расчёт берёт на месяц целиком
// (`select_rules(..., period)`), и версия с серединой июня на июнь не влияет
// вовсе. Правка с такой датой прошла бы, но переносить было бы нечего.
await goto(APP + "/rules/hour_types.regular.pay_percent/");
let rulePage = await evalIn("document.body.innerText");
check(
  "форма правила предупреждает о закрытом месяце до правки",
  rulePage.includes("Зарплата утверждена по 2026-06-30"),
  rulePage.slice(0, 200).replace(/\n/g, " "),
);
check(
  "и не обещает отказа, которого больше нет",
  !rulePage.includes("будет отклонена"),
  "",
);
await fillFields({ valid_from: "2026-06-01", value: "1.1" });
await clickButton("Завести версию");
rulePage = await evalIn("document.body.innerText");
check(
  "правка задним числом принята и объяснена",
  rulePage.includes("Закрытый месяц остался прежним"),
  rulePage.slice(0, 250).replace(/\n/g, " "),
);
check(
  "версия правила лежит в базе с июньской датой",
  sql("select count(*) from rule_overrides "
      + "where path = 'hour_types.regular.pay_percent' "
      + "and valid_from = date '2026-06-01'") === "1",
  sql("select path, valid_from from rule_overrides"),
);

await logout();
await login("director");
await goto(APP + june);
page = await snapshot();
check("страница увидела расхождение", page.retroSection);
check("получатель назван словами — Июль 2026", page.text.includes("Июль 2026"), "");
check("директору предложен перенос", page.retroButton);
check(
  "закрытый месяц ещё не сдвинулся",
  JSON.stringify(lines(page)) === JSON.stringify(closedBefore),
  "",
);
check(`итог июня прежний ${REFERENCE_TOTAL}`, page.total === REFERENCE_TOTAL, page.total);

// --- 3. Перенос нажатием ------------------------------------------------------
await clickButton("Перенести разницу");
page = await snapshot();
check("перенос подтверждён словами", page.text.includes("Разница перенесена"), "");
check(
  "ЗАКРЫТЫЙ МЕСЯЦ ОСТАЛСЯ БАЙТ В БАЙТ ПРЕЖНИМ",
  JSON.stringify(lines(page)) === JSON.stringify(closedBefore),
  "",
);
check("июнь по-прежнему утверждён", page.payrunStatus === "Утверждён", page.payrunStatus);
check("расхождения больше нет", !page.retroSection);

const inDb = sql(
  "select count(*), coalesce(sum(amount), 0) from retro_adjustments where cancelled_at is null"
);
check("перенос лёг в базу", Number(inDb.split("|")[0]) > 0, inDb);
// Не список регистров наизусть, а равенство двум множествам: набор регистров
// переноса обязан совпасть с набором регистров самого закрытого месяца. Список
// в смоуке разошёлся бы с сидом молча и проверял бы память автора.
const movedLedgers = sql("select distinct ledger from retro_adjustments order by 1");
const sourceLedgers = sql(
  "select distinct c.ledger from pay_components c join payslips s on s.id = c.payslip_id " +
  "join payruns p on p.id = s.payrun_id where p.period = date '2026-06-01' order by 1"
);
check(
  "регистры переноса — те же, что у закрытого месяца",
  movedLedgers === sourceLedgers,
  movedLedgers + " против " + sourceLedgers,
);

// --- 4. Получатель: плашка, пересчёт, объясняющая себя строка -----------------
sql(
  "insert into periods (tenant_id, period, status) " +
  "select id, date '2026-07-01', 'open' from tenants where code = 'rs-dev' " +
  "on conflict do nothing"
);
sql(
  "insert into timesheets (tenant_id, employee_id, unit_id, period, hours, insured_hours, " +
  "norm_hours, deduction, cash_payout, manual_correction, corrected_by, correction_reason) " +
  "select tenant_id, employee_id, unit_id, date '2026-07-01', hours, insured_hours, " +
  "norm_hours, deduction, cash_payout, manual_correction, corrected_by, correction_reason " +
  "from timesheets where period = date '2026-06-01' on conflict do nothing"
);

const july = await pageOf("Июль 2026");
check("страница июля найдена", !!july, july);
await goto(APP + july);
page = await snapshot();
check(
  "июль честно говорит, что разница ещё не в ведомости",
  page.text.toLowerCase().includes("пересчитайте период"),
  "",
);

await clickButton("Посчитать период");
page = await snapshot();
const retroRows = page.rows.filter((r) => r.retro);
check("в июле появились строки разницы", retroRows.length > 0, String(retroRows.length));
check(
  "строка разницы объясняет, за какой месяц она пришла",
  retroRows.every((r) => r.badge.includes("Перерасчёт за Июнь 2026")),
  retroRows[0] ? retroRows[0].badge : "",
);
check("плашки о неперенесённой разнице больше нет",
  !page.text.toLowerCase().includes("пересчитайте период"), "");

// --- 5. Утверждённая разница не даёт открыть источник заново ------------------
await clickButton("Утвердить период");
page = await snapshot();
check("июль утверждён", page.payrunStatus === "Утверждён", page.payrunStatus);

await goto(APP + june);
page = await snapshot();
check("кнопки отката июня больше нет", !page.reopenForm);
check(
  "и сказано почему",
  page.text.includes("уже перенесена в утверждённый период"),
  "",
);
const refused = await post(june + "reopen/", { reason: REOPEN_REASON });
check("откат мимо экрана — 409", refused.status === 409, String(refused.status));
check(
  "и теми же словами",
  refused.text.includes("уже перенесена в утверждённый период"),
  "",
);
page = await snapshot();
check("июнь остался утверждённым", page.payrunStatus === "Утверждён", page.payrunStatus);

// --- 6. Роли: числа закрытого месяца не сдвинулись ни у кого ------------------
for (const [who, count, total] of [
  // После D036 доступ бухгалтера равен директорскому — числа поэтому
  // директорские. Роль с неполным набором в списке остаётся (управляющий,
  // D031): без неё проверка была бы зелёной и при снятом срезе.
  ["accountant", 60, "1 951 806,13"],
  ["manager", 24, "891 373,32"],
]) {
  await login(who);
  const href = await pageOf("Июнь 2026");
  await goto(APP + href);
  const view = await snapshot();
  check(`${who}: ${count} строк июня`, view.count === count, String(view.count));
  check(`${who}: итог июня ${total}`, view.total === total, view.total);
  check(`${who}: норма 176,00`, view.norm.includes("176,00"), view.norm);
}

// --- 7. У роли без права переноса кнопки нет, а адрес отвечает 403 ------------
sql("update employment_terms set base_rate = base_rate * 2");
await login("manager");
const forManager = await pageOf("Июнь 2026");
await goto(APP + forManager);
page = await snapshot();
check("управляющему перенос не предложен", !page.retroButton);
const denied = await post(forManager + "retro/");
check("перенос мимо экрана управляющим — 403", denied.status === 403, String(denied.status));
check(
  "и объяснён названием действия",
  denied.text.includes("Перенос разницы за закрытый месяц"),
  "",
);

// --- 8. Настройка тенанта действительно решает --------------------------------
sql("update tenants set retro_mode = 'recalculate' where code = 'rs-dev'");
await login("director");
await goto(APP + june);
page = await snapshot();
check("в режиме пересчёта расхождение показано", page.retroSection);
check("но кнопки переноса нет", !page.retroButton);
check(
  "и сказано, что делать вместо",
  page.text.includes("откройте период заново"),
  "",
);
const wrongMode = await post(june + "retro/");
check("перенос в режиме пересчёта — 409", wrongMode.status === 409, String(wrongMode.status));
sql("update tenants set retro_mode = 'delta' where code = 'rs-dev'");

// --- уборка за смоуком --------------------------------------------------------
sql("update employment_terms set base_rate = base_rate / 4");

check("консоль чистая", logs.length === 0, logs.join(" | "));
report();
