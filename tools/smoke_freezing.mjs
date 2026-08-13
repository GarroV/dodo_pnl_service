/*
 * Смоук построчной заморозки ведомости (T027).
 *
 * Проверяет то, чего не доказывает ни разбор разметки, ни тесты по базе: что
 * живой человек нужной роли **нажатием** морозит спорную строку, что пересчёт
 * после этого обходит её стороной (числа те же до копейки, у остальных —
 * новые), что спорная строка не держит утверждение месяца, и что роль без
 * права не видит ни кнопки, ни молчания.
 *
 * Клики и ввод — настоящими событиями мыши и клавиатуры: обработчик, вызванный
 * напрямую, доказывает работоспособность обработчика, а не экрана.
 *
 * Ставки поднимаются и возвращаются через psql в контейнере базы. Это не
 * проверка, а подготовка входных данных: пересчёт обязан что-то менять, иначе
 * «числа не изменились» ничего не доказывает.
 *
 * Стенд смоук приводит к сиду сам — и в начале, и после себя (см. договор в
 * шапке `cdp.mjs`). Поэтому запускать его можно в любом порядке и в одиночку,
 * а `COMPOSE_PROJECT_NAME` обязателен: без него сброс ушёл бы на чужой стенд.
 *
 *     COMPOSE_PROJECT_NAME=<стенд> APP=http://127.0.0.1:8054 \\
 *         node tools/smoke_freezing.mjs
 */
import { attach, findPeriodAndGrid, loginWith, sql, standFromSeed } from "./cdp.mjs";

const APP = process.env.APP || "http://127.0.0.1:8054";
const REASON = "смоук: спорные ночные часы, разбираемся с сотрудником";
const REOPEN_REASON = "смоук: вернуть период в работу";
const REFERENCE_TOTAL = "1 951 806,13";

const { evalIn, goto, send, type, check, report, logs } = await attach();
const login = loginWith(APP, evalIn, goto);

// Стенд к эталону сейчас и обратно к нему после — в том числе если смоук
// упадёт на полпути (issue #76). Порядок запуска смоуков больше ничего не
// решает: каждый начинает с известного входа и ничего за собой не оставляет.
standFromSeed();

// Ставки правятся через `sql` харнесса: он ходит в базу того стенда, который
// назван в COMPOSE_PROJECT_NAME. Зашитое имя контейнера, стоявшее здесь раньше,
// привязывало сценарий к стенду, на котором его однажды написали.
/** Нажать кнопку по тексту — настоящей мышью, по её месту на экране. */
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
  await new Promise((r) => setTimeout(r, 2000));
  return true;
}

/** Что видно на странице периода: ведомость построчно, состояние, кнопки. */
const snapshot = () => evalIn(`
  (() => {
    const rows = [...document.querySelectorAll("table.sheet tbody tr")].map(tr => {
      const cells = [...tr.children];
      const form = tr.querySelector('form[action*="/freeze/"]');
      return {
        employee: cells[0].textContent.trim(),
        unit: cells[1].textContent.trim(),
        ledger: cells[2].textContent.trim(),
        total: cells[cells.length - 1].textContent.trim(),
        frozen: !!tr.querySelector(".frozen"),
        freezeAction: form ? form.action : "",
        releaseForm: !!tr.querySelector('form[action*="/release/"]'),
      };
    });
    const facts = [...document.querySelectorAll("dl.facts dt")];
    const i = facts.findIndex(dt => dt.textContent.includes("Расчёт зарплаты"));
    const dd = document.querySelectorAll("dl.facts dd");
    return {
      rows,
      count: rows.length,
      payrunStatus: i >= 0 ? dd[i].textContent.trim() : "",
      total: (document.querySelector("table.sheet tfoot .num:last-child") || {}).textContent || "",
      freezeForms: document.querySelectorAll('form[action*="/freeze/"]').length,
      freezeAction: (document.querySelector('form[action*="/freeze/"]') || {}).action || "",
      csrf: (document.querySelector("[name=csrfmiddlewaretoken]") || {}).value || "",
      approve: !!document.querySelector('form[action$="/approve/"]'),
      calculate: !!document.querySelector('form[action$="/calculate/"]'),
      text: document.body.innerText,
    };
  })()
`);

/** Досылка формы мимо экрана — той же сессией, что и у человека. */
const post = (action, fields) => evalIn(`
  (async () => {
    const csrf = document.querySelector("[name=csrfmiddlewaretoken]").value;
    const body = new URLSearchParams({ csrfmiddlewaretoken: csrf, ...${JSON.stringify(fields)} });
    const r = await fetch(${JSON.stringify(action)}, { method: "POST", body, redirect: "follow" });
    return { status: r.status, text: await r.text() };
  })()
`);

const totalsByEmployee = (page) => {
  const map = {};
  for (const row of page.rows) map[row.employee + "|" + row.ledger] = row.total;
  return map;
};

// --- директор: посчитать и заморозить спорную строку --------------------------
await login("director");
const { periodHref } = await findPeriodAndGrid(APP, evalIn, goto);
await goto(APP + periodHref);

let page = await snapshot();
if (!page.count) {
  await clickButton("Посчитать период");
  page = await snapshot();
}
check("ведомость директора: 60 строк", page.count === 60, String(page.count));
check(`итог директора ${REFERENCE_TOTAL}`, page.total.trim() === REFERENCE_TOTAL, page.total.trim());
check("до заморозки замороженных строк нет", page.rows.every((r) => !r.frozen));
check("директору предложена заморозка строк", page.freezeForms > 0, String(page.freezeForms));

const disputed = page.rows[0];
await evalIn(`
  (() => {
    const input = document.querySelector('form[action*="/freeze/"] input[name=reason]');
    input.scrollIntoView({ block: "center" });
    input.focus();
  })()
`);
await type(REASON);
const typed = await evalIn(
  `document.querySelector('form[action*="/freeze/"] input[name=reason]').value`,
);
check("причина набрана с клавиатуры", typed === REASON, typed);

check("директор морозит строку нажатием", await clickButton("Заморозить"));
page = await snapshot();
const frozenRows = page.rows.filter((r) => r.frozen);
check("строка помечена как замороженная", frozenRows.length >= 1, String(frozenRows.length));
check(
  "заморожен именно тот, кого выбрали",
  frozenRows.every((r) => r.employee === disputed.employee),
  frozenRows.map((r) => r.employee).join(", "),
);
check("причина спора видна на экране", page.text.includes(REASON));
check("у замороженной строки предложено снятие", page.rows.some((r) => r.releaseForm));
check("ведомость не потеряла строк", page.count === 60, String(page.count));

const before = totalsByEmployee(page);
const beforeTotal = page.total.trim();
// Адрес заморозки запоминается заранее: в утверждённом периоде форм на экране
// нет вовсе, а проверить надо именно ответ сервера.
const disputedAction = disputed.freezeAction;

// --- пересчёт: замороженная строка обязана остаться прежней -------------------
sql("update employment_terms set base_rate = base_rate * 2");
check("директор пересчитывает период нажатием", await clickButton("Посчитать период"));
page = await snapshot();
const after = totalsByEmployee(page);
const frozenKeys = Object.keys(before).filter((k) => k.startsWith(disputed.employee + "|"));

check(
  "числа замороженной строки не изменились",
  frozenKeys.every((k) => after[k] === before[k]),
  frozenKeys.map((k) => `${k}: ${before[k]} → ${after[k]}`).join("; "),
);
check(
  "остальные строки пересчитались",
  Object.keys(before).some((k) => !frozenKeys.includes(k) && after[k] !== before[k]),
);
check("итог периода изменился", page.total.trim() !== beforeTotal, page.total.trim());
check("замороженный никуда не делся", page.count === 60, String(page.count));
check("метка заморозки на месте", page.rows.some((r) => r.frozen));

// --- спорная строка не держит утверждение месяца ------------------------------
check("директор утверждает период нажатием", await clickButton("Утвердить период"));
page = await snapshot();
check("месяц утверждён вместе со спорной строкой", page.payrunStatus === "Утверждён", page.payrunStatus);
check("утверждённый период заморозки не предлагает", page.freezeForms === 0, String(page.freezeForms));

const inApproved = await post(disputedAction, { reason: REASON });
check("заморозка в утверждённом периоде отвергнута (409)", inApproved.status === 409,
  String(inApproved.status));
check(
  "отказ говорит про период, а не про строку",
  inApproved.text.includes("Период утверждён") && !inApproved.text.includes("Строка сотрудника заморожена"),
);

// --- откат и снятие заморозки -------------------------------------------------
await goto(APP + periodHref);
await evalIn(`document.querySelector('#reason').focus()`);
await type(REOPEN_REASON);
check("директор открывает период нажатием", await clickButton("Открыть заново"));
page = await snapshot();
check("период открыт заново", page.payrunStatus === "Открыт заново", page.payrunStatus);

check("директор снимает заморозку нажатием", await clickButton("Снять заморозку"));
page = await snapshot();
check("метки заморозки не осталось", page.rows.every((r) => !r.frozen));

check("директор пересчитывает после снятия", await clickButton("Посчитать период"));
page = await snapshot();
const released = totalsByEmployee(page);
check(
  "размороженный посчитан заново",
  frozenKeys.some((k) => released[k] !== before[k]),
  frozenKeys.map((k) => `${k}: ${before[k]} → ${released[k]}`).join("; "),
);

// --- возвращаем ставки и сверяем ориентиры блока ------------------------------
sql("update employment_terms set base_rate = base_rate / 2");
check("директор пересчитывает на прежних ставках", await clickButton("Посчитать период"));
page = await snapshot();
check("ведомость снова 60 строк", page.count === 60, String(page.count));
check(`итог снова ${REFERENCE_TOTAL}`, page.total.trim() === REFERENCE_TOTAL, page.total.trim());
check("норма часов 176,00", page.text.includes("176,00"));

// --- отказы: пустая причина и роль без права ----------------------------------
const blank = await post(page.rows.find((r) => r.freezeAction).freezeAction, { reason: "   " });
check("заморозка без причины отвергнута (400)", blank.status === 400, String(blank.status));
check("отказ объяснён словами", blank.text.includes("требует причины"));

// Управляющему предлагается строка **его** точки: чужой строки он не видит
// вовсе, и 404 там означал бы невидимость, а не отсутствие права.
const ownRow = page.rows.find((r) => r.unit === "NS1" && r.freezeAction);
check("нашлась строка точки управляющего", !!ownRow, ownRow ? ownRow.employee : "нет");
const managerRowAction = ownRow ? ownRow.freezeAction : "";

await login("manager");
await goto(APP + periodHref);
page = await snapshot();
check("управляющий не видит форм заморозки", page.freezeForms === 0, String(page.freezeForms));
check(
  "управляющему объяснено, почему кнопки нет",
  page.text.includes("Заморозка строки ведомости не входит в права вашей роли"),
);
const denied = await post(managerRowAction, { reason: REASON });
check("заморозка без права отвергнута (403)", denied.status === 403, String(denied.status));
check("отказ назван словами", denied.text.includes("Заморозка строки ведомости"));

await login("accountant");
await goto(APP + periodHref);
page = await snapshot();
check("бухгалтер вправе морозить строку", page.freezeForms > 0, String(page.freezeForms));
check("бухгалтер: 33 строки", page.count === 33, String(page.count));
check("бухгалтер: итог 464 752,41", page.total.trim() === "464 752,41", page.total.trim());

await login("manager");
await goto(APP + periodHref);
page = await snapshot();
check("управляющий: 24 строки", page.count === 24, String(page.count));
check("управляющий: итог 891 373,32", page.total.trim() === "891 373,32", page.total.trim());

console.log("\nКонсоль браузера:", logs.length ? logs.join(" | ") : "чисто");
report();
