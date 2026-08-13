/*
 * Смоук ведомости с разрезом по регистрам (T028).
 *
 * Проверяет то, чего разбором разметки в тестах не докажешь: что живой человек
 * под каждой из четырёх ролей видит на экране, что переключатель разреза
 * работает настоящим нажатием, и что после нажатия итог по-прежнему равен
 * сумме показанных строк — то есть скрытое не вычисляется вычитанием (D023).
 *
 * Отдельно проверяется ширина: ведомость на 1440 не должна двигать страницу по
 * горизонтали (Definition of Done блока reports).
 *
 *     google-chrome --headless=new --remote-debugging-port=9339 \
 *         --user-data-dir=/tmp/chrome-smoke-rep &
 * Стенд смоук приводит к сиду сам — и в начале, и после себя (см. договор в
 * шапке `cdp.mjs`). Поэтому запускать его можно в любом порядке и в одиночку,
 * а `COMPOSE_PROJECT_NAME` обязателен: без него сброс ушёл бы на чужой стенд.
 *
 *     COMPOSE_PROJECT_NAME=<стенд> APP=http://127.0.0.1:8056 \\
 *         node tools/smoke_reports_sheet.mjs
 */
import { attach, findPeriodAndGrid, loginWith, standFromSeed } from "./cdp.mjs";

const APP = process.env.APP || "http://127.0.0.1:8056";

// Ориентир приёмки, снятый на данных сида. Регистры перечислены те, что роль
// обязана видеть, — и ровно они, ни одним больше.
const ROLES = [
  { code: "director", rows: 60, total: "1 951 806,13", ledgers: ["Официальный", "Дополнительный", "Внутренний"] },
  { code: "accountant", rows: 33, total: "464 752,41", ledgers: ["Официальный"] },
  { code: "manager", rows: 24, total: "891 373,32", ledgers: ["Официальный", "Дополнительный"] },
  // Администратор сети видит все три регистра (T089): справочники и правила
  // ведёт он один, и с одним официальным регистром он не видел бы карточки
  // курьеров и кухни — то есть менять ставку было бы некому. Ожидание здесь
  // отстало от сида на две задачи и делало смоук красным при исправном
  // продукте — ровно тот шум, ради которого написан договор в `cdp.mjs`.
  { code: "admin", rows: 60, total: "1 951 806,13",
    ledgers: ["Официальный", "Дополнительный", "Внутренний"] },
];

const ALL_LEDGERS = ["Официальный", "Дополнительный", "Внутренний"];

const { evalIn, goto, send, check, report, logs } = await attach();
const login = loginWith(APP, evalIn, goto);

// Стенд к эталону сейчас и обратно к нему после — в том числе если смоук
// упадёт на полпути (issue #76). Порядок запуска смоуков больше ничего не
// решает: каждый начинает с известного входа и ничего за собой не оставляет.
standFromSeed();

const money = (text) => Number(text.replace(/[  ]/g, "").replace(",", "."));

// Окно ровно 1440 — ширина, записанная в Definition of Done блока.
await send("Emulation.setDeviceMetricsOverride", {
  width: 1440, height: 900, deviceScaleFactor: 1, mobile: false,
});

/** Что видно на странице ведомости — глазами, из разметки самой страницы. */
const readSheet = () => evalIn(`
  (() => {
    const rows = [...document.querySelectorAll("table.sheet tbody tr")];
    const cell = (tr) => tr.querySelector("td:last-child").textContent.trim();
    const foot = document.querySelector("table.sheet tfoot .grand td:last-child");
    return {
      rows: rows.length,
      rowTotals: rows.map(cell),
      ledgers: [...new Set(rows.map(tr => tr.children[2].textContent.trim()))],
      total: foot ? foot.textContent.trim() : "",
      cuts: [...document.querySelectorAll("nav.cuts .cut")].map(x => x.textContent.trim()),
      current: (document.querySelector("nav.cuts .cut.current") || {}).textContent || "",
      text: document.body.innerText,
      // Горизонтальная прокрутка меряется у самой ведомости, а не у страницы.
      // Страница не едет никогда: таблица завёрнута в .scroll, и перелив
      // прячется внутрь него — то есть требование «читается без
      // горизонтальной прокрутки» проверкой страницы не проверяется вовсе.
      // Найдено смоуком 2026-08-08: у директора внутри было 191px перелива.
      sheetScroll: (() => {
        const box = document.querySelector(".scroll");
        return box ? box.scrollWidth - box.clientWidth : 0;
      })(),
      pageScroll: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    };
  })()
`);

/** Нажать кнопку разреза настоящей мышью и дождаться перерисовки. */
async function clickCut(title) {
  const box = await evalIn(`
    (() => {
      const a = [...document.querySelectorAll("nav.cuts a.cut")]
        .find(x => x.textContent.trim() === ${JSON.stringify(title)});
      if (!a) return null;
      const r = a.getBoundingClientRect();
      return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
    })()
  `);
  if (!box) return false;
  for (const type of ["mousePressed", "mouseReleased"]) {
    await send("Input.dispatchMouseEvent", { type, x: box.x, y: box.y, button: "left", clickCount: 1 });
  }
  await new Promise((r) => setTimeout(r, 1200));
  return true;
}

// Расчёт — один раз, настоящим нажатием под директором: без него ведомости нет.
await login("director");
const { periodHref } = await findPeriodAndGrid(APP, evalIn, goto);
await goto(APP + periodHref);
{
  const box = await evalIn(`
    (() => {
      const b = [...document.querySelectorAll("button")]
        .find(x => x.textContent.includes("Посчитать период"));
      if (!b) return null;
      const r = b.getBoundingClientRect();
      return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
    })()
  `);
  if (box) {
    for (const type of ["mousePressed", "mouseReleased"]) {
      await send("Input.dispatchMouseEvent", { type, x: box.x, y: box.y, button: "left", clickCount: 1 });
    }
  }
  // Расчёт может уйти в очередь (PAYRUN_BACKGROUND=1) — тогда страница
  // обновится сама. Ждём появления строк, а не фиксированную паузу: пауза
  // «на глазок» либо тормозит смоук, либо однажды не дожидается.
  let has = false;
  for (let i = 0; i < 40 && !has; i++) {
    await new Promise((r) => setTimeout(r, 700));
    has = await evalIn(`document.querySelectorAll("table.sheet tbody tr").length > 0`);
  }
  check("период посчитан, ведомость на экране", has);
}

for (const role of ROLES) {
  await login(role.code);
  await goto(APP + periodHref);
  const seen = await readSheet();

  check(`${role.code}: строк ${role.rows}`, seen.rows === role.rows, String(seen.rows));
  check(`${role.code}: итог ${role.total}`, seen.total === role.total, seen.total);

  const sum = seen.rowTotals.reduce((acc, t) => acc + money(t), 0);
  check(
    `${role.code}: итог равен сумме показанных строк`,
    Math.abs(sum - money(seen.total)) < 0.005,
    `строки ${sum.toFixed(2)} vs итог ${seen.total}`,
  );

  check(
    `${role.code}: в строках ровно его регистры`,
    JSON.stringify([...seen.ledgers].sort()) === JSON.stringify([...role.ledgers].sort()),
    seen.ledgers.join(", "),
  );

  // Ни строкой, ни кнопкой, ни текстом: чужой регистр не должен быть даже
  // назван — иначе роль узнаёт о его существовании.
  const forbidden = ALL_LEDGERS.filter((name) => !role.ledgers.includes(name));
  for (const name of forbidden) {
    check(`${role.code}: слова «${name}» на странице нет`, !seen.text.includes(name));
  }

  check(
    `${role.code}: ведомость читается без горизонтальной прокрутки на 1440`,
    seen.sheetScroll <= 0 && seen.pageScroll <= 0,
    `в ведомости ${seen.sheetScroll}px, на странице ${seen.pageScroll}px`,
  );

  // --- переключатель разреза -------------------------------------------------
  if (role.ledgers.length === 1) {
    check(`${role.code}: переключателя нет — переключать нечего`, seen.cuts.length === 0);
    continue;
  }

  check(
    `${role.code}: в переключателе «Все регистры» и его регистры`,
    JSON.stringify(seen.cuts) === JSON.stringify(["Все регистры", ...role.ledgers]),
    seen.cuts.join(" | "),
  );

  let parts = 0;
  for (const ledger of role.ledgers) {
    check(`${role.code}: нажатие «${ledger}»`, await clickCut(ledger));
    const cut = await readSheet();
    check(`${role.code}/${ledger}: выбран он`, cut.current.trim() === ledger, cut.current);
    check(
      `${role.code}/${ledger}: показан один регистр`,
      cut.ledgers.length === 1 && cut.ledgers[0] === ledger,
      cut.ledgers.join(", "),
    );
    const cutSum = cut.rowTotals.reduce((acc, t) => acc + money(t), 0);
    check(
      `${role.code}/${ledger}: итог равен сумме строк разреза`,
      Math.abs(cutSum - money(cut.total)) < 0.005,
      `строки ${cutSum.toFixed(2)} vs итог ${cut.total}`,
    );
    parts += money(cut.total);
    // Возврат к полной ведомости — тоже нажатием, а не набором адреса.
    await clickCut("Все регистры");
  }
  check(
    `${role.code}: разрезы в сумме дают полную ведомость`,
    Math.abs(parts - money(seen.total)) < 0.005,
    `${parts.toFixed(2)} vs ${seen.total}`,
  );
}

// Подобранный чужой регистр в адресе: ответ обязан быть неотличим от обычного.
await login("accountant");
await goto(APP + periodHref + "?ledger=internal");
{
  const guessed = await readSheet();
  check("бухгалтер, ?ledger=internal: та же ведомость", guessed.total === "464 752,41", guessed.total);
  check("бухгалтер, ?ledger=internal: слова «Внутренний» нет", !guessed.text.includes("Внутренний"));
}

if (logs.length) console.log("\nконсоль браузера:\n" + logs.join("\n"));
report();
