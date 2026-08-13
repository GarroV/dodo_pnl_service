/*
 * Смоук демо-стенда (T033, T034): пройти его так, как пройдёт посторонний.
 *
 * Проверяет то, чего не докажешь ни тестом на базе, ни чтением кода: что
 * человек, у которого нет ни учётки, ни куки, открывает адрес, жмёт одну
 * кнопку и оказывается в наполненном продукте — с ведомостью, следом расчёта,
 * отчётом расхождений и работающей сверкой.
 *
 * Отдельно проверяется главное свойство демо: **английский**. Не «страница
 * открылась», а что на титульной странице нет ни одного русского слова и что
 * подписи компонентов ведомости пришли по-английски (они попадают туда в момент
 * расчёта, и русское слово здесь означало бы, что стенд считали без английских
 * переопределений правил).
 *
 * Сверка проверяется целиком, с файлом: демо само отдаёт таблицу бухгалтера, и
 * смысл проверки в том, что скачанный файл действительно сходится с расчётом —
 * кроме трёх расхождений, поставленных нарочно.
 *
 *     google-chrome --headless=new --remote-debugging-port=9352 \
 *         --user-data-dir=/tmp/chrome-smoke-demo &
 *     APP=http://127.0.0.1:8064 DOWNLOADS=/tmp/demo-downloads \
 *         node tools/smoke_demo.mjs
 */
import { existsSync, mkdirSync, readdirSync } from "node:fs";
import { resolve } from "node:path";

import { attach } from "./cdp.mjs";

const APP = process.env.APP || "http://127.0.0.1:8064";
const DOWNLOADS = resolve(process.env.DOWNLOADS || "/tmp/demo-downloads");

// Русские буквы. Ищем именно их: демо всегда англоязычное, независимо от языка
// продукта, и это правило владельца, а не пожелание.
const CYRILLIC = /[а-яА-ЯёЁ]/;

mkdirSync(DOWNLOADS, { recursive: true });

const { send, evalIn, goto, check, report, logs } = await attach();

await send("Emulation.setDeviceMetricsOverride", {
  width: 1440, height: 900, deviceScaleFactor: 1, mobile: false,
});
await send("Browser.setDownloadBehavior", {
  behavior: "allow", downloadPath: DOWNLOADS, eventsEnabled: true,
});

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function clickBy(finderJs, name) {
  const box = await evalIn(`
    (() => {
      const el = ${finderJs};
      if (!el) return null;
      el.scrollIntoView({ block: "center" });
      const r = el.getBoundingClientRect();
      return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
    })()
  `);
  if (!box) {
    check(`нажатие «${name}»: элемент найден`, false);
    return false;
  }
  for (const type of ["mousePressed", "mouseReleased"]) {
    await send("Input.dispatchMouseEvent", {
      type, x: box.x, y: box.y, button: "left", clickCount: 1,
    });
  }
  return true;
}

async function chooseFile(selector, path) {
  const { result } = await send("Runtime.evaluate", {
    expression: `document.querySelector(${JSON.stringify(selector)})`,
  });
  if (!result.objectId) return false;
  await send("DOM.setFileInputFiles", { files: [path], objectId: result.objectId });
  return true;
}

const text = () => evalIn("document.body.innerText");
const here = () => evalIn("location.pathname");

// --- посторонний открывает демо ------------------------------------------------

// Куки чистим намеренно: смоук обязан проверять путь человека, который здесь
// впервые, а не остатки прошлой сессии.
await send("Network.clearBrowserCookies");

await goto(`${APP}/demo/`);
const landing = await text();
check("титульная страница демо открылась без учётки", landing.includes("live demo"), await here());
check(
  "на титульной странице нет ни одного русского слова",
  !CYRILLIC.test(landing),
  (landing.match(new RegExp(CYRILLIC.source + "+", "g")) || []).slice(0, 5).join(" "),
);
check(
  "видно, чем наполнен стенд",
  landing.includes("two legal entities") && landing.includes("thirty people"),
);

// --- один клик — и посетитель внутри продукта ----------------------------------

check(
  "нажата единственная кнопка входа",
  await clickBy(
    `[...document.querySelectorAll("a")].find(x => x.textContent.includes("Enter the demo"))`,
    "Enter the demo",
  ),
);
await sleep(1500);
check("посетитель оказался в списке периодов", (await here()) === "/periods/", await here());

const periods = await text();
check("в списке три месяца", (periods.match(/2026/g) || []).length >= 3, periods.slice(0, 200));

// --- ведомость закрытого месяца -----------------------------------------------

const periodLinks = await evalIn(`
  [...document.querySelectorAll('a[href^="/periods/"]')]
    .map(a => a.getAttribute('href'))
    .filter(h => /^\\/periods\\/[0-9a-f-]{36}\\/$/.test(h))
`);
check("со списка ведут ссылки на периоды", periodLinks.length >= 3, String(periodLinks.length));

// Первый в списке — самый поздний месяц (открытый август), последний — июнь.
const june = periodLinks[periodLinks.length - 1];
const august = periodLinks[0];

await goto(APP + june);
const sheetRows = await evalIn(`document.querySelectorAll("table.sheet tbody tr").length`);
check("ведомость июня непуста", sheetRows > 20, `${sheetRows} строк`);

// Проверяются подписи **компонентов**, а не всей шапки. Собственные слова
// интерфейса («Сотрудник», «Точка», «Итого») пока русские: их переводит
// локализация продукта, T017, которая делается параллельно и не входит в этот
// блок. А вот подписи компонентов приезжают из правил в момент расчёта — они
// обязаны быть английскими уже сейчас, иначе стенд посчитан не теми правилами.
const CHROME = ["Сотрудник", "Точка", "Регистр", "Спор", "Итого", ""];
const componentTitles = await evalIn(`
  [...document.querySelectorAll("table.sheet thead th")].map(th => th.textContent.trim())
`);
const fromRules = componentTitles.filter((t) => !CHROME.includes(t));
check(
  "подписи компонентов ведомости английские",
  fromRules.length > 0 && !fromRules.some((t) => CYRILLIC.test(t)),
  fromRules.join(" | "),
);

// --- след расчёта ---------------------------------------------------------------

const traceHref = await evalIn(
  `(document.querySelector('a[href*="/trace/"]') || {}).getAttribute
     ? document.querySelector('a[href*="/trace/"]').getAttribute("href") : null`,
);
check("со строки ведомости есть ссылка на след расчёта", Boolean(traceHref), String(traceHref));
if (traceHref) {
  await goto(APP + traceHref);
  const trace = await text();
  check("след расчёта показывает правила", trace.length > 200, `${trace.length} символов`);
}

// --- отчёт расхождений ----------------------------------------------------------

await goto(`${APP}${august}variance/`);
const varianceRows = await evalIn(
  `document.querySelectorAll("table.variance tbody tr").length`,
);
const variance = await text();
check("отчёт расхождений непуст", varianceRows > 0, `${varianceRows} строк`);
check(
  "отчёт расхождений сравнил с прошлым месяцем, а не сказал «нечего»",
  !variance.includes("сравнивать не с чем"),
);

// --- сверка: файл демо отдаёт само ------------------------------------------------

// Куки чистим намеренно: ссылку на файл посетитель может нажать первым делом, не
// заходя внутрь. Без сессии данные под контекстом базы не собираются, и человек
// получил бы отказ, ничего не сделав неправильно.
await send("Network.clearBrowserCookies");
await goto(`${APP}/demo/`);
check(
  "нажата ссылка на таблицу бухгалтера (посетителем без сессии)",
  await clickBy(
    `[...document.querySelectorAll("a")].find(x => x.textContent.includes("Download accountant"))`,
    "Download accountant's spreadsheet",
  ),
);
await sleep(2500);
const downloaded = readdirSync(DOWNLOADS).filter((n) => n.endsWith(".xlsx"));
check("таблица бухгалтера скачалась файлом", downloaded.length === 1, downloaded.join(", "));

if (downloaded.length === 1) {
  const file = resolve(DOWNLOADS, downloaded[0]);
  check("скачанный файл на месте", existsSync(file), file);

  await goto(`${APP}${june}reconcile/`);
  check("страница сверки открылась", (await text()).length > 0);
  check("файл выбран в поле", await chooseFile("input[type=file][name=table]", file));
  // Подписи здесь английские, и это не оговорка: демо всегда англоязычное
  // (правило владельца, UI_LANGUAGE=en). Русские подписи в этом файле были
  // остатком от времён, когда интерфейс переводов ещё не имел, — и смоук
  // краснел на исправном демо, потому что искал кнопку, которой там не бывает.
  check(
    "нажата кнопка сверки",
    await clickBy(
      `[...document.querySelectorAll("button")].find(x => x.textContent.trim() === "Reconcile")`,
      "Reconcile",
    ),
  );
  await sleep(2500);

  const summary = await evalIn(`
    (() => {
      const table = [...document.querySelectorAll("table.sheet")]
        .find(t => t.textContent.includes("Reconciliation result"));
      if (!table) return null;
      const out = {};
      for (const tr of table.querySelectorAll("tbody tr")) {
        out[tr.children[0].textContent.trim()] = Number(tr.children[1].textContent.trim());
      }
      return out;
    })()
  `);
  check("сверка ответила сводкой", Boolean(summary), JSON.stringify(summary));
  if (summary) {
    check("большая часть строк сошлась", summary["Matched to the cent"] >= 20,
      String(summary["Matched to the cent"]));
    check("одно расхождение показано", summary["Off"] >= 1,
      String(summary["Off"]));
    check("копеечное расхождение показано отдельно",
      summary["Off by cents (rounding)"] >= 1,
      String(summary["Off by cents (rounding)"]));
    check("человек, оставшийся только в файле, показан",
      summary["In the table, not in the calculation"] >= 1,
      String(summary["In the table, not in the calculation"]));
    check("курьеров в таблице бухгалтера нет — и это видно",
      summary["In the calculation, not in the table"] >= 4,
      String(summary["In the calculation, not in the table"]));
  }
}

// --- вторая роль: демо показывает разницу видимости --------------------------------

await send("Network.clearBrowserCookies");
await goto(`${APP}/demo/enter/accountant/`);
await sleep(1200);
await goto(APP + june);
const accountantRows = await evalIn(
  `document.querySelectorAll("table.sheet tbody tr").length`,
);
check(
  "бухгалтер видит меньше строк, чем директор",
  accountantRows > 0 && accountantRows < sheetRows,
  `${accountantRows} против ${sheetRows}`,
);

// --- демо не притворяется включённым там, где его нет --------------------------

const dead = await evalIn(`
  fetch("/dev/login/", { method: "POST" }).then(r => r.status)
`);
check("вход-ярлык разработчика в демо выключен", dead === 404 || dead === 403, String(dead));

check("в консоли браузера чисто", logs.length === 0, logs.slice(0, 3).join(" | "));

report();
