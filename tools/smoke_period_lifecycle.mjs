/*
 * Смоук утверждения и отката периода (T025).
 *
 * Проверяет то, чего не доказывает разбор разметки: что живой человек нужной
 * роли действительно доводит период до утверждения и обратно **нажатиями**, что
 * откат без причины не проходит, и что история на экране называет и причину, и
 * автора.
 *
 * Клики и ввод — настоящими событиями мыши и клавиатуры (`Input.dispatch*`):
 * обработчик, вызванный напрямую, доказывает работоспособность обработчика, а
 * не экрана.
 *
 * Стенд смоук приводит к сиду сам — и в начале, и после себя (см. договор в
 * шапке `cdp.mjs`). Поэтому запускать его можно в любом порядке и в одиночку.
 *
 *     COMPOSE_PROJECT_NAME=<стенд> node tools/smoke_period_lifecycle.mjs       (APP=http://127.0.0.1:8051)
 */
import { attach, findPeriodAndGrid, loginWith, standFromSeed } from "./cdp.mjs";

const APP = process.env.APP || "http://127.0.0.1:8051";
const REASON = "смоук: ошиблись в часах за третью неделю";

const { evalIn, goto, send, type, check, report, logs } = await attach();
const login = loginWith(APP, evalIn, goto);

// Стенд к эталону сейчас и обратно к нему после — в том числе если смоук
// упадёт на полпути (issue #76). Порядок запуска смоуков больше ничего не
// решает: каждый начинает с известного входа и ничего за собой не оставляет.
standFromSeed();

/** Нажать кнопку по тексту — настоящей мышью, по её месту на экране. */
async function clickButton(text) {
  const box = await evalIn(`
    (() => {
      const b = [...document.querySelectorAll("button")]
        .find(x => x.textContent.includes(${JSON.stringify(text)}));
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

/** Что видно на странице периода: кнопки, состояние, история. */
const snapshot = () => evalIn(`
  (() => {
    const heading = [...document.querySelectorAll("h2")]
      .find(h => h.textContent.includes("История периода"));
    const table = heading ? heading.nextElementSibling : null;
    const facts = [...document.querySelectorAll("dl.facts dt")];
    const i = facts.findIndex(dt => dt.textContent.includes("Расчёт зарплаты"));
    const dd = document.querySelectorAll("dl.facts dd");
    return {
      approve: !!document.querySelector('form[action$="/approve/"]'),
      reopen: !!document.querySelector('form[action$="/reopen/"]'),
      calculate: !!document.querySelector('form[action$="/calculate/"]'),
      payrunStatus: i >= 0 ? dd[i].textContent.trim() : "",
      // Историю читаем только из её собственной таблицы: имя вошедшего стоит в
      // шапке каждой страницы, и поиск по всему тексту прошёл бы и по ней.
      history: table ? table.innerText : "",
      historyRows: table ? table.querySelectorAll("tbody tr").length : 0,
      text: document.body.innerText,
      rows: document.querySelectorAll("table.sheet tbody tr").length,
      total: (document.querySelector("table.sheet tfoot .num:last-child") || {}).textContent || "",
    };
  })()
`);

// --- директор: посчитать и утвердить -----------------------------------------
await login("director");
const { periodHref } = await findPeriodAndGrid(APP, evalIn, goto);
await goto(APP + periodHref);

let page = await snapshot();
check("до расчёта кнопки утверждения нет", !page.approve, page.payrunStatus);

check("директор считает период нажатием", await clickButton("Посчитать период"));
page = await snapshot();
check("расчёт оставил состояние «Посчитан»", page.payrunStatus === "Посчитан", page.payrunStatus);
check("ведомость директора: 60 строк", page.rows === 60, String(page.rows));
check("итог директора 1 951 806,13", page.total.trim() === "1 951 806,13", page.total.trim());
check("кнопка утверждения появилась", page.approve);
check("кнопки отката ещё нет", !page.reopen);

check("директор утверждает период нажатием", await clickButton("Утвердить период"));
page = await snapshot();
check("состояние стало «Утверждён»", page.payrunStatus === "Утверждён", page.payrunStatus);
check("утверждённый период не предлагает пересчёт", !page.calculate);
check("появилась кнопка отката", page.reopen);
check("история назвала утверждение", page.history.includes("Утверждён"));
check("история назвала автора утверждения", page.history.includes("Оперативный директор"));

// --- управляющий: ни кнопки, ни молчания --------------------------------------
await login("manager");
await goto(APP + periodHref);
page = await snapshot();
check("управляющий не видит кнопки отката", !page.reopen);
check(
  "управляющему объяснено, почему кнопки нет",
  page.text.includes("Откат периода не входит в права вашей роли"),
);

// --- бухгалтер: и утверждает, и откатывает (D036, T115) -----------------------
// Спека требует откат от бухгалтера дословно: «хочу откатить утверждение с
// указанием причины, чтобы опечатка не превращалась в разбирательство». До T115
// кнопки у него не было — за откатом собственной опечатки приходилось идти к
// директору.
await login("accountant");
await goto(APP + periodHref);
page = await snapshot();
check("бухгалтер видит кнопку отката", !!page.reopen);
check(
  "и ему не пишут, что откат не входит в права",
  !page.text.includes("Откат периода не входит в права вашей роли"),
);
check("бухгалтер видит историю периода", page.historyRows >= 2, String(page.historyRows));

// --- директор: откат без причины и с причиной ---------------------------------
await login("director");
await goto(APP + periodHref);

// Пустое поле останавливает браузер сам (required) — досылаем форму мимо него,
// чтобы проверить не разметку, а сервер: за формой обязан стоять отказ.
const refused = await evalIn(`
  (async () => {
    const form = document.querySelector('form[action$="/reopen/"]');
    const body = new URLSearchParams({
      csrfmiddlewaretoken: form.querySelector('[name=csrfmiddlewaretoken]').value,
      reason: "   ",
    });
    const r = await fetch(form.action, { method: "POST", body, redirect: "follow" });
    return { status: r.status, text: (await r.text()).includes("требует причины") };
  })()
`);
check("откат без причины отвергнут сервером (400)", refused.status === 400, String(refused.status));
check("отказ объяснён словами", refused.text);

await goto(APP + periodHref);
page = await snapshot();
check("после отказа период всё ещё утверждён", page.payrunStatus === "Утверждён", page.payrunStatus);

// Теперь настоящий откат: причина набирается с клавиатуры.
await evalIn(`document.querySelector('#reason').focus()`);
await type(REASON);
const typed = await evalIn(`document.querySelector('#reason').value`);
check("причина набрана с клавиатуры", typed === REASON, typed);

check("директор открывает период нажатием", await clickButton("Открыть заново"));
page = await snapshot();
check("состояние стало «Открыт заново»", page.payrunStatus === "Открыт заново", page.payrunStatus);
check("открытый период не предлагает утверждение", !page.approve);
check("открытый период снова можно считать", page.calculate);
check("история назвала причину", page.history.includes(REASON));
check("история назвала автора отката", page.history.includes("Оперативный директор"));
check("история показывает все переходы", page.historyRows >= 4, String(page.historyRows));

// --- бухгалтер и управляющий видят числа своего среза -------------------------
for (const [who, rows, total] of [
  // После D036 доступ бухгалтера равен директорскому — числа поэтому
  // директорские. Роль с неполным набором в списке остаётся (управляющий,
  // D031): без неё проверка была бы зелёной и при снятом срезе.
  ["accountant", 60, "1 951 806,13"],
  ["manager", 24, "891 373,32"],
]) {
  await login(who);
  await goto(APP + periodHref);
  const seen = await snapshot();
  check(`${who}: ${rows} строк ведомости`, seen.rows === rows, String(seen.rows));
  check(`${who}: итог ${total}`, seen.total.trim() === total, seen.total.trim());
  check(`${who}: норма часов 176,00`, seen.text.includes("176,00"));
}

console.log("\nКонсоль браузера:", logs.length ? logs.join(" | ") : "чисто");
report();
