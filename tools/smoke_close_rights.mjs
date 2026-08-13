/*
 * Смоук прав на закрытие часов точки (T076, D033).
 *
 * Проверяет настоящими нажатиями то, чего тесты доказать не могут: что
 * директор закрывает и открывает **чужую** для управляющего точку, что
 * управляющему по-прежнему предложена только своя и чужую он не закрывает даже
 * мимо экрана, и что у роли без права кнопки нет, а на её месте объяснение.
 *
 * Стенд смоук приводит к сиду сам — и в начале, и после себя (см. договор в
 * шапке `cdp.mjs`). Поэтому запускать его можно в любом порядке и в одиночку.
 *
 *     COMPOSE_PROJECT_NAME=<стенд> node tools/smoke_close_rights.mjs        (APP=http://127.0.0.1:8062)
 */
import { attach, findPeriodAndGrid, loginWith, standFromSeed } from "./cdp.mjs";

const APP = process.env.APP || "http://127.0.0.1:8062";

const { evalIn, goto, send, check, report, logs } = await attach();
const login = loginWith(APP, evalIn, goto);

// Стенд к эталону сейчас и обратно к нему после — в том числе если смоук
// упадёт на полпути (issue #76). Порядок запуска смоуков больше ничего не
// решает: каждый начинает с известного входа и ничего за собой не оставляет.
standFromSeed();

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Настоящий клик по кнопке внутри карточки точки с нужным кодом. */
async function clickUnitButton(unitCode, text) {
  const box = await evalIn(`
    (() => {
      const card = [...document.querySelectorAll(".unit")]
        .find(u => u.querySelector(".unit-code")?.textContent.trim() === ${JSON.stringify(unitCode)});
      if (!card) return null;
      const btn = [...card.querySelectorAll("button")]
        .find(b => b.textContent.includes(${JSON.stringify(text)}));
      if (!btn) return null;
      btn.scrollIntoView({ block: "center" });
      const r = btn.getBoundingClientRect();
      return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
    })()
  `);
  if (!box) return false;
  for (const t of ["mousePressed", "mouseReleased"]) {
    await send("Input.dispatchMouseEvent", {
      type: t, x: box.x, y: box.y, button: "left", clickCount: 1,
    });
  }
  await sleep(1500);
  return true;
}

const unitCards = () => evalIn(`
  [...document.querySelectorAll(".unit")].map(u => ({
    code: u.querySelector(".unit-code")?.textContent.trim(),
    state: u.querySelector(".unit-state")?.textContent.trim(),
    buttons: [...u.querySelectorAll("button")].map(b => b.textContent.trim()),
  }))
`);

/** POST мимо экрана настоящей сессией браузера. */
const postAs = (url, fields) => evalIn(`
  (async () => {
    const csrf = document.cookie.match(/csrftoken=([^;]+)/)[1];
    const res = await fetch(${JSON.stringify(url)}, {
      method: "POST",
      headers: { "X-CSRFToken": csrf, "Content-Type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams(${JSON.stringify(fields)}),
      credentials: "include",
    });
    return { status: res.status, text: await res.text() };
  })()
`);

await send("Emulation.setDeviceMetricsOverride", {
  width: 1440, height: 900, deviceScaleFactor: 1, mobile: false,
});

// --- 1. Расчёт периода: контрольные числа не должны поехать ------------------
await login("director");
const { periodHref, gridHref } = await findPeriodAndGrid(APP, evalIn, goto);
await goto(APP + periodHref);
{
  const clicked = await evalIn(`
    (() => {
      const btn = [...document.querySelectorAll("button")]
        .find(b => b.textContent.includes("Посчитать период"));
      if (!btn) return false;
      btn.click();
      return true;
    })()
  `);
  check("директор запускает расчёт периода", clicked);
  await sleep(3000);
  await goto(APP + periodHref);
  const rows = await evalIn(`document.querySelectorAll("table.sheet tbody tr").length`);
  const text = await evalIn(`document.body.innerText`);
  check("директор: 60 строк ведомости", rows === 60, String(rows));
  check("директор: итог 1 951 806,13", text.includes("1 951 806,13"));
  check("директор: норма часов 176,00", /Норма часов[\s\S]{0,40}176,00/.test(text));
}

// --- 2. Директор закрывает чужую для управляющего точку ----------------------
let alien = null;
await goto(APP + gridHref);
{
  const cards = await unitCards();
  check("директору предложена вся сеть", cards.length > 1,
    JSON.stringify(cards.map(c => c.code)));
  check("на каждой точке есть кнопка закрытия",
    cards.every(c => c.buttons.some(b => b.includes("Закрыть часы точки"))),
    JSON.stringify(cards.map(c => c.buttons)));

  alien = cards.map(c => c.code).find(code => code !== "NS1");
  check("нашлась точка, которая управляющему не своя", !!alien, String(alien));

  check(`директор нажал «Закрыть часы точки» на ${alien}`,
    await clickUnitButton(alien, "Закрыть часы точки"));
  const after = await unitCards();
  const closed = after.find(c => c.code === alien);
  check(`${alien}: часы закрыты`, /часы закрыты/.test(closed?.state || ""), closed?.state || "");
  check("остальные точки остались открытыми",
    after.filter(c => c.code !== alien).every(c => /часы открыты/.test(c.state || "")),
    JSON.stringify(after.map(c => [c.code, c.state])));

  const editable = await evalIn(`
    [...document.querySelectorAll("tbody tr")]
      .filter(tr => tr.children[1].textContent.trim() === ${JSON.stringify(alien)})
      .every(tr => !tr.querySelector("input.cell"))
  `);
  check(`строки ${alien} потеряли поля ввода`, editable);
}

// --- 3. Управляющий: только своя точка, чужая недоступна даже мимо экрана ----
await login("manager");
await goto(APP + gridHref);
let alienId = null;
{
  const cards = await unitCards();
  check("управляющему предложена ровно одна точка", cards.length === 1,
    JSON.stringify(cards.map(c => c.code)));
  check("и это его NS1", cards[0]?.code === "NS1", String(cards[0]?.code));
  check("закрытия чужой точки на экране не видно",
    !(await evalIn(`document.body.innerText`)).includes(alien + " "),
    "");
}
{
  // Идентификатор чужой точки управляющему неоткуда взять с экрана — берём его
  // как берёт злоумышленник: подставляем чужой uuid в запрос.
  await login("director");
  await goto(APP + gridHref);
  alienId = await evalIn(`
    (() => {
      const card = [...document.querySelectorAll(".unit")]
        .find(u => u.querySelector(".unit-code")?.textContent.trim() === ${JSON.stringify(alien)});
      return card?.querySelector("input[name=unit]")?.value || null;
    })()
  `);
  check("идентификатор чужой точки получен у директора", !!alienId, String(alienId));

  await login("manager");
  await goto(APP + gridHref);
  const denied = await postAs(APP + gridHref + "reopen/", { unit: alienId });
  check("управляющий не открывает чужую точку мимо экрана", denied.status === 404,
    String(denied.status));
  const closing = await postAs(APP + gridHref + "close/", { unit: alienId });
  check("и не закрывает её", closing.status === 404, String(closing.status));
}

// --- 4. Роль без права: кнопки нет, действие отвергается ---------------------
await login("accountant");
await goto(APP + gridHref);
{
  const forms = await evalIn(`document.querySelectorAll("input[name=unit]").length`);
  check("у бухгалтера кнопки закрытия нет", forms === 0, String(forms));
  check("и сказано, почему",
    /не входит в права вашей роли/.test(await evalIn(`document.body.innerText`)));
  const denied = await postAs(APP + gridHref + "close/", { unit: alienId });
  check("закрытие бухгалтером мимо экрана отвергнуто", denied.status === 403,
    String(denied.status));
  check("отказ по праву объяснён словами",
    /не входит в права вашей роли/.test(denied.text), denied.text.slice(0, 80));
}

// --- 5. Директор открывает точку заново --------------------------------------
await login("director");
await goto(APP + gridHref);
{
  check(`директор нажал «Открыть заново» на ${alien}`,
    await clickUnitButton(alien, "Открыть заново"));
  const cards = await unitCards();
  const back = cards.find(c => c.code === alien);
  check(`${alien}: часы снова открыты`, /часы открыты/.test(back?.state || ""),
    back?.state || "");
  const editable = await evalIn(`
    [...document.querySelectorAll("tbody tr")]
      .filter(tr => tr.children[1].textContent.trim() === ${JSON.stringify(alien)})
      .some(tr => !!tr.querySelector("input.cell"))
  `);
  check(`поля ввода на ${alien} вернулись`, editable);
}

// --- 6. Ведомость остальных ролей: числа не поехали --------------------------
for (const [who, rows, total] of [
  ["accountant", 33, "464 752,41"],
  ["manager", 24, "891 373,32"],
]) {
  await login(who);
  await goto(APP + periodHref);
  const seen = await evalIn(`document.querySelectorAll("table.sheet tbody tr").length`);
  const text = await evalIn(`document.body.innerText`);
  check(`${who}: ${rows} строк ведомости`, seen === rows, String(seen));
  check(`${who}: итог ${total}`, text.includes(total));
  check(`${who}: норма часов 176,00`, /Норма часов[\s\S]{0,40}176,00/.test(text));
}

check("консоль браузера чиста", logs.length === 0, logs.join(" | ").slice(0, 200));
report();
