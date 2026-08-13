/*
 * Смоук закрытия часов по точке (T022).
 *
 * Проверяет то, чего разбор разметки доказать не может: что управляющий
 * закрывает свою точку настоящим нажатием кнопки, что после этого часы не
 * пишутся ни с экрана, ни мимо него, что соседняя точка при этом продолжает
 * вводиться, и что право `unit.close` роздано так, как решено в D033:
 * директору — вся сеть, управляющему — только своя точка, бухгалтеру не
 * выдано вовсе.
 *
 * Стенд смоук приводит к сиду сам — и в начале, и после себя (см. договор в
 * шапке `cdp.mjs`). Поэтому запускать его можно в любом порядке и в одиночку.
 *
 *     COMPOSE_PROJECT_NAME=<стенд> node tools/smoke_closing.mjs            (APP=http://127.0.0.1:8052)
 */
import { attach, findPeriodAndGrid, loginWith, standFromSeed } from "./cdp.mjs";

const APP = process.env.APP || "http://127.0.0.1:8052";

const { evalIn, goto, send, key, type, check, report, logs } = await attach();
const login = loginWith(APP, evalIn, goto);

// Стенд к эталону сейчас и обратно к нему после — в том числе если смоук
// упадёт на полпути (issue #76). Порядок запуска смоуков больше ничего не
// решает: каждый начинает с известного входа и ничего за собой не оставляет.
standFromSeed();

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Настоящий клик по элементу, найденному по видимому тексту. */
async function clickByText(selector, text) {
  const box = await evalIn(`
    (() => {
      const el = [...document.querySelectorAll(${JSON.stringify(selector)})]
        .find(x => x.textContent.includes(${JSON.stringify(text)}));
      if (!el) return null;
      el.scrollIntoView({ block: "center" });
      const r = el.getBoundingClientRect();
      return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
    })()
  `);
  if (!box) return false;
  for (const t of ["mousePressed", "mouseReleased"]) {
    await send("Input.dispatchMouseEvent", {
      type: t, x: box.x, y: box.y, button: "left", clickCount: 1,
    });
  }
  await sleep(1200);
  return true;
}

/** Набор в ячейку мышью и клавиатурой, как это делает человек. */
async function typeIntoFirstCell(value) {
  const box = await evalIn(`
    (() => {
      const el = document.querySelector("input.cell");
      if (!el) return null;
      el.scrollIntoView({ block: "center" });
      const r = el.getBoundingClientRect();
      return { x: r.x + r.width / 2, y: r.y + r.height / 2, row: el.dataset.row, kind: el.dataset.kind };
    })()
  `);
  if (!box) return null;
  for (const t of ["mousePressed", "mouseReleased"]) {
    await send("Input.dispatchMouseEvent", {
      type: t, x: box.x, y: box.y, button: "left", clickCount: 3,
    });
  }
  await type(value);
  await key("Enter", "Enter", 13);
  await sleep(1200);
  return box;
}

await send("Emulation.setDeviceMetricsOverride", {
  width: 1440, height: 900, deviceScaleFactor: 1, mobile: false,
});

// --- 1. Расчёт периода: числа-ориентиры не должны поехать --------------------
await login("director");
const { periodHref, gridHref } = await findPeriodAndGrid(APP, evalIn, goto);
await goto(APP + periodHref);
check("директор видит кнопку расчёта",
  await clickByText("button", "Посчитать период"));
await sleep(2500);
{
  const text = await evalIn(`document.body.innerText`);
  const rows = await evalIn(`document.querySelectorAll("table.sheet tbody tr").length`);
  check("директор: 60 строк ведомости", rows === 60, String(rows));
  check("директор: итог 1 951 806,13",
    text.includes("1 951 806,13"), text.match(/1 951 [\d ,]+/)?.[0] || "");
}

// --- 2. Управляющий закрывает свою точку ------------------------------------
await login("manager");
await goto(APP + gridHref);
{
  const before = await evalIn(`document.querySelectorAll("input.cell").length`);
  check("управляющий: сетка редактируемая до закрытия", before > 0, String(before));
  const units = await evalIn(`
    [...document.querySelectorAll(".unit .unit-code")].map(x => x.textContent.trim())
  `);
  check("панель показывает только свою точку",
    units.length === 1 && units[0] === "NS1", JSON.stringify(units));

  check("кнопка закрытия нажата", await clickByText("button", "Закрыть часы точки"));
  const state = await evalIn(`document.body.innerText`);
  check("точка отмечена закрытой", /часы закрыты/.test(state));
  const after = await evalIn(`document.querySelectorAll("input.cell").length`);
  check("полей ввода на закрытой точке нет", after === 0, String(after));
}

// --- 3. Попытка мимо интерфейса ---------------------------------------------
{
  const row = await evalIn(`
    (async () => {
      const csrf = document.cookie.match(/csrftoken=([^;]+)/)[1];
      const id = document.querySelector("[id^=row-total-]").id.replace("row-total-", "");
      const body = new URLSearchParams({ row: id, kind: "regular", hours: "99" });
      const res = await fetch(${JSON.stringify(APP + gridHref)} + "cell/", {
        method: "POST", headers: { "X-CSRFToken": csrf,
          "Content-Type": "application/x-www-form-urlencoded" },
        body, credentials: "include",
      });
      return { status: res.status, text: await res.text(), kept: res.headers.get("X-Cell-Value") };
    })()
  `);
  check("запись мимо экрана отвергнута", row.status === 409, String(row.status));
  check("отказ объяснён словами", /закрыт/i.test(row.text), row.text.slice(0, 90));
  check("в ответе значение из базы", !!row.kept, row.kept || "");
}

// --- 4. Соседняя точка не заперта -------------------------------------------
await login("director");
await goto(APP + gridHref);
{
  const open = await evalIn(`
    (() => {
      const el = [...document.querySelectorAll("tbody tr")]
        .find(tr => tr.children[1].textContent.trim() !== "NS1" && tr.querySelector("input.cell"));
      if (!el) return null;
      const input = el.querySelector("input.cell");
      input.scrollIntoView({ block: "center" });
      const r = input.getBoundingClientRect();
      return { x: r.x + r.width / 2, y: r.y + r.height / 2, unit: el.children[1].textContent.trim(),
               row: input.dataset.row, kind: input.dataset.kind };
    })()
  `);
  check("у директора есть открытая соседняя точка", !!open, open?.unit || "");
  for (const t of ["mousePressed", "mouseReleased"]) {
    await send("Input.dispatchMouseEvent", {
      type: t, x: open.x, y: open.y, button: "left", clickCount: 3,
    });
  }
  await type("101");
  // Tab, а не Enter: уход фокуса на соседнюю ячейку — то самое событие
  // `change`, на котором держится сохранение. Enter в последней строке фокус
  // никуда не переводит, и значение уезжает досылкой при уходе со страницы —
  // проверялась бы не запись, а досылка.
  await key("Tab", "Tab", 9);
  await sleep(1500);
  const stored = await evalIn(`
    (() => {
      const el = document.querySelector('input.cell[data-row="' + ${JSON.stringify(open.row)} + '"][data-kind="' + ${JSON.stringify(open.kind)} + '"]');
      return el ? el.value : null;
    })()
  `);
  check("часы соседней точки пишутся при закрытой NS1", stored === "101.00", String(stored));

  const closedRows = await evalIn(`
    [...document.querySelectorAll("tbody tr")]
      .filter(tr => tr.children[1].textContent.trim() === "NS1")
      .every(tr => !tr.querySelector("input.cell"))
  `);
  check("строки закрытой точки на общем экране не правятся", closedRows);
}

// --- 5. Кому право закрытия выдано, а кому нет (D033) ------------------------
// Раньше здесь проверялось обратное — что кнопки нет у директора. Правило
// отменено решением D033 (T076): закрывать вправе и тот, кто ведёт месяц
// целиком, иначе отпуск управляющего запирает период. Проверяется теперь
// действующее распределение права, а не отменённое.

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

// Сюда мы пришли директором (шаг 4) — с его же экрана берём чужую для
// управляющего точку: её идентификатор ему самому взять неоткуда.
let alien = null;
{
  const cards = await evalIn(`
    [...document.querySelectorAll(".unit")].map(u => ({
      code: u.querySelector(".unit-code")?.textContent.trim(),
      unit: u.querySelector("input[name=unit]")?.value || null,
    }))
  `);
  alien = cards.find((c) => c.code !== "NS1" && c.unit);
  check("директору кнопка закрытия предложена на каждой точке",
    cards.length > 1 && cards.every((c) => !!c.unit),
    JSON.stringify(cards.map((c) => [c.code, !!c.unit])));
}

await login("manager");
await goto(APP + gridHref);
{
  // Именно 404, а не 403: чужая точка не должна выдавать управляющему даже
  // факт своего существования.
  const denied = await postAs(APP + gridHref + "close/", { unit: alien?.unit });
  // Наличие самой точки — часть проверки: на несуществующем идентификаторе 404
  // приходит сам собой, и без этого условия проверка прошла бы впустую.
  check(`управляющему чужая точка ${alien?.code} отвечает 404, а не 403`,
    !!alien?.unit && denied.status === 404,
    (alien?.unit || "чужой точки не нашлось") + " → " + denied.status);
}

// Роль без права закрытия — администратор сети. Бухгалтер им был до T115;
// теперь у него `unit.close` есть (D036), и здесь он ничего бы не доказывал.
await login("admin");
await goto(APP + gridHref);
{
  const forms = await evalIn(`document.querySelectorAll('input[name=unit]').length`);
  const html = await evalIn(`document.body.innerText`);
  check("у администратора сети кнопки закрытия нет, а на её месте объяснение",
    forms === 0 && /не входит в права вашей роли/.test(html), String(forms));

  const denied = await postAs(APP + gridHref + "close/", { unit: alien?.unit });
  check("закрытие им мимо экрана отвергнуто по праву",
    denied.status === 403 && /не входит в права вашей роли/.test(denied.text),
    denied.status + " " + denied.text.slice(0, 60));
}

// --- 6. Открытие заново возвращает правку -----------------------------------
await login("manager");
await goto(APP + gridHref);
check("кнопка «Открыть заново» нажата", await clickByText("button", "Открыть заново"));
{
  const cells = await evalIn(`document.querySelectorAll("input.cell").length`);
  check("поля ввода вернулись", cells > 0, String(cells));
  const box = await typeIntoFirstCell("7");
  const value = await evalIn(`
    document.querySelector('input.cell[data-row="' + ${JSON.stringify(box?.row)} + '"][data-kind="' + ${JSON.stringify(box?.kind)} + '"]').value
  `);
  check("часы снова пишутся после открытия", value === "7.00", String(value));
}

// --- 7. Ведомость остальных ролей: числа не поехали -------------------------
for (const [who, rows, total] of [
  // После D036 доступ бухгалтера равен директорскому — числа поэтому
  // директорские. Роль с неполным набором в списке остаётся (управляющий,
  // D031): без неё проверка была бы зелёной и при снятом срезе.
  ["accountant", 60, "1 951 806,13"],
  ["manager", 24, "891 373,32"],
]) {
  await login(who);
  await goto(APP + periodHref);
  const seen = await evalIn(`document.querySelectorAll("table.sheet tbody tr").length`);
  const text = await evalIn(`document.body.innerText`);
  check(`${who}: ${rows} строк ведомости`, seen === rows, String(seen));
  check(`${who}: итог ${total}`, text.includes(total));
  // Норма общая для всех ролей: разное число у разных людей уже было дефектом.
  check(`${who}: норма часов 176,00`, /Норма часов[\s\S]{0,40}176,00/.test(text));
}

// Директор смотрит ту же страницу последним — иначе его норму пришлось бы
// проверять до расчёта, когда числа ещё нет.
await login("director");
await goto(APP + periodHref);
check("директор: норма часов 176,00",
  /Норма часов[\s\S]{0,40}176,00/.test(await evalIn(`document.body.innerText`)));

check("консоль браузера чиста", logs.length === 0, logs.join(" | ").slice(0, 200));
report();
