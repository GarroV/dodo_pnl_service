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
import { existsSync, mkdirSync, readdirSync, rmSync } from "node:fs";
import { resolve } from "node:path";

import { attach } from "./cdp.mjs";

const APP = process.env.APP || "http://127.0.0.1:8064";
const DOWNLOADS = resolve(process.env.DOWNLOADS || "/tmp/demo-downloads");

// Русские буквы. Ищем именно их: демо всегда англоязычное, независимо от языка
// продукта, и это правило владельца, а не пожелание.
const CYRILLIC = /[а-яА-ЯёЁ]/;

// Каталог скачиваний чистится на входе: проверка «скачался ровно один файл»
// краснела бы на файле прошлого прогона при исправном демо.
rmSync(DOWNLOADS, { recursive: true, force: true });
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

// T117: проза титульной не должна расходиться со списком ролей на ней же.
// Раньше расходилась: список честно показывал у бухгалтера три регистра, а
// абзац ниже уверял, что бухгалтер видит только официальный (осталось от модели
// доступа до D036). Сравнивается страница сама с собой — списка ролей, который
// собирается из кода, достаточно, чтобы уличить прозу.
const claims = await evalIn(`
  (() => {
    const roles = [...document.querySelectorAll("ul.roles li")].map(li => {
      const name = (li.querySelector("a") || {}).textContent || "";
      return {
        who: name.replace(/^Enter as\\s*/, "").trim().toLowerCase(),
        ledgers: ((li.querySelector("span") || {}).textContent || "")
          .split("·")[0].replace(/^\\s*—\\s*sees\\s*/, "").split(",")
          .map(s => s.trim()).filter(Boolean),
      };
    });
    const prose = [...document.querySelectorAll("p")]
      .map(p => p.textContent.replace(/\\s+/g, " ").trim()).join(" ");
    return { roles, prose: prose.toLowerCase() };
  })()
`);
const fullAccess = claims.roles.filter((r) => r.ledgers.length === 3);
check("на титульной перечислены роли и их регистры", claims.roles.length >= 2,
      JSON.stringify(claims.roles));
check(
  "проза не называет ограниченной роль, у которой все три регистра",
  fullAccess.every((role) => !claims.prose.split(/[.;]/).some(
    (sentence) => sentence.includes(role.who.split(" ")[0])
      && sentence.includes("ledger") && sentence.includes("only"),
  )),
  fullAccess.map((r) => r.who).join(", "),
);
check(
  "и всё-таки объясняет срез — на роли, которая ограничена на самом деле",
  claims.prose.includes("cannot be recovered by subtracting")
    && claims.roles.some((r) => r.ledgers.length < 3
        && claims.prose.includes(r.who.split(" ")[0])),
  claims.prose.slice(0, 300),
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
    // Подпись именно такая: расчёт роли отдан не весь ровно настолько, насколько
    // велит её срез, и строка называет это прямо — «в вашей части расчёта»
    // (T095/T096). Старая подпись «In the table, not in the calculation» из
    // продукта ушла, а смоук искал её и краснел на исправном демо (issue #86).
    check("человек, оставшийся только в файле, показан",
      summary["In the table, not in your part of the calculation"] >= 1,
      String(summary["In the table, not in your part of the calculation"]));
    check("курьеров в таблице бухгалтера нет — и это видно",
      summary["In the calculation, not in the table"] >= 4,
      String(summary["In the calculation, not in the table"]));
  }
}

// --- тот же файл у каждой роли (T104, issue #88) ----------------------------------
//
// Ссылка на таблицу бухгалтера отвечала 404 бухгалтеру и управляющему — тем
// самым ролям, ради которых экран сверки и существует. Файл изображает артефакт
// ВНЕ продукта, поэтому собирается полным срезом и одинаков для всех; проверяем
// это настоящим скачиванием каждой ролью и настоящей сверкой на скачанном.

for (const role of ["accountant", "manager"]) {
  const folder = resolve(DOWNLOADS, role);
  mkdirSync(folder, { recursive: true });
  await send("Browser.setDownloadBehavior", {
    behavior: "allow", downloadPath: folder, eventsEnabled: true,
  });
  await send("Network.clearBrowserCookies");
  await goto(`${APP}/demo/enter/${role}/`);
  await sleep(1000);
  await goto(`${APP}/demo/`);
  check(
    `${role}: нажата ссылка на таблицу бухгалтера`,
    await clickBy(
      `[...document.querySelectorAll("a")].find(x => x.textContent.includes("Download accountant"))`,
      "Download accountant's spreadsheet",
    ),
  );
  await sleep(2500);
  const got = readdirSync(folder).filter((n) => n.endsWith(".xlsx"));
  check(`${role}: файл скачался, а не 404`, got.length === 1, got.join(", ") || "ничего");
  if (got.length !== 1) continue;

  await goto(`${APP}${june}reconcile/`);
  check(`${role}: файл выбран в поле`,
    await chooseFile("input[type=file][name=table]", resolve(folder, got[0])));
  check(
    `${role}: нажата кнопка сверки`,
    await clickBy(
      `[...document.querySelectorAll("button")].find(x => x.textContent.trim() === "Reconcile")`,
      "Reconcile",
    ),
  );
  await sleep(2500);
  const page = await text();
  check(`${role}: сверка прошла на скачанном файле`, page.includes("Reconciliation result"));
  // Человек, оставшийся только в таблице бухгалтера, обязан быть назван каждой
  // роли: он и есть то, ради чего экран существует.
  check(`${role}: сверка назвала оставшегося только в файле`, page.includes("Ashford"));
}

// --- вторая роль: демо показывает разницу видимости --------------------------------

// Разницу видимости в демо показывает управляющий точки: у него неполный набор
// регистров и одна точка (D031). Бухгалтер на этом месте больше не годится —
// после D036 её доступ равен директорскому, и проверка «видит меньше» краснела
// бы на исправном демо. Обе стороны проверяются рядом: равенство у бухгалтера и
// сужение у управляющего. Одна без другой ничего не значит.
await send("Network.clearBrowserCookies");
await goto(`${APP}/demo/enter/accountant/`);
await sleep(1200);
await goto(APP + june);
const accountantRows = await evalIn(
  `document.querySelectorAll("table.sheet tbody tr").length`,
);
check(
  "бухгалтер видит то же, что директор (D036)",
  accountantRows === sheetRows,
  `${accountantRows} против ${sheetRows}`,
);

await send("Network.clearBrowserCookies");
await goto(`${APP}/demo/enter/manager/`);
await sleep(1200);
await goto(APP + june);
const managerRows = await evalIn(
  `document.querySelectorAll("table.sheet tbody tr").length`,
);
check(
  "управляющий видит меньше строк, чем директор",
  managerRows > 0 && managerRows < sheetRows,
  `${managerRows} против ${sheetRows}`,
);

// --- расходы из кассы: вторая половина того, из чего складывается P&L (T113) ---
//
// Демо с одной зарплатой показывает половину продукта и молчит об этом. Здесь
// проверяется, что посетитель видит траты наполненными, видит нераспределённую
// сумму (продукт не прячет то, чего не смог разложить) и что срез регистров
// действует на расходах так же, как на ведомости.

const WIDE = "?from=2026-06-01&to=2026-12-31";

await send("Network.clearBrowserCookies");
await goto(`${APP}/demo/enter/accountant/`);
await sleep(1200);
await goto(`${APP}/expenses/${WIDE}`);
const spending = await text();
const spentRows = await evalIn(`document.querySelectorAll("tr[data-fact]").length`);
check("бухгалтер видит траты наполненными", spentRows >= 10, `строк: ${spentRows}`);
check(
  "расходы демо написаны по-английски",
  !CYRILLIC.test(spending),
  (spending.match(new RegExp(CYRILLIC.source + "+", "g")) || []).slice(0, 5).join(" "),
);
check(
  "видно, за что деньги, а не только сколько",
  spending.includes("Electricity") && spending.includes("Office rent"),
);

await goto(`${APP}/expenses/unallocated/`);
const waiting = await text();
check(
  "нераспределённая сумма показана, а не спрятана",
  waiting.includes("Marketing campaign"),
  waiting.slice(0, 200),
);

// Разнесённая аренда: родитель ушёл в дети по точкам, и в списке трат он один,
// а не четырьмя строками. Проверяется по сумме списка — она обязана совпасть с
// итогом, который показывает сам продукт.
await goto(`${APP}/expenses/${WIDE}`);
const agreed = await evalIn(`
  (() => {
    const rows = [...document.querySelectorAll("tr[data-fact]")]
      .filter(tr => tr.dataset.state === "active")
      .reduce((sum, tr) => sum + Number(tr.dataset.amount), 0);
    const total = Number(document.querySelector("tr[data-total]").dataset.total);
    return Math.abs(rows - total) < 0.005;
  })()
`);
check("итог списка — сумма показанных строк, а не отдельная выборка", agreed === true);

// Управляющий: своя точка и свои регистры. Расход внутреннего регистра (мелкий
// ремонт за наличные) ему не виден вовсе — ни строкой, ни вкладом в итог.
await send("Network.clearBrowserCookies");
await goto(`${APP}/demo/enter/manager/`);
await sleep(1200);
await goto(`${APP}/expenses/${WIDE}`);
const managerSees = await text();
check(
  "управляющий видит только свою точку",
  !managerSees.includes("BG1") && !managerSees.includes("NS2"),
  managerSees.slice(0, 200),
);
// Смотреть надо СТРОКИ СПИСКА, а не весь текст страницы: название статьи
// «Small repairs» законно стоит в выпадающем фильтре — справочник статей общий
// на партнёра и от регистров не зависит, как строки P&L или группы сотрудников.
// Первая редакция проверки искала строку по всей странице и краснела на
// исправном продукте; утечкой было бы другое — сам расход или его сумма.
const hiddenLeak = await evalIn(`
  (() => {
    const rows = [...document.querySelectorAll("tr[data-fact]")]
      .map(tr => tr.textContent);
    return rows.some(t => t.includes("Small repairs") || t.includes("25,400"));
  })()
`);
check(
  "расход невидимого регистра управляющему не виден ни строкой, ни суммой",
  hiddenLeak === false && !managerSees.includes("25,400"),
);

// Демо не витрина: посетитель вносит трату сам и видит её в списке.
await goto(`${APP}/expenses/new/`);
const entered = await evalIn(`
  (async () => {
    // Именно форма расхода, а не первая форма страницы: первой в разметке идёт
    // форма выхода из шапки, и на ней нет ни одного нужного поля — проверка
    // падала на чтении её csrf-поля, не дойдя до самого сценария.
    const form = [...document.querySelectorAll("form")]
      .find(f => f.querySelector("[name=entry_key]"));
    const token = form.querySelector("[name=csrfmiddlewaretoken]").value;
    const item = [...form.querySelectorAll("[name=item] option")]
      .find(o => o.value)?.value;
    const unit = [...form.querySelectorAll("[name=unit] option")]
      .find(o => o.value && o.value !== "network")?.value;
    const body = new URLSearchParams({
      csrfmiddlewaretoken: token, date: "2026-08-21", amount: "1234.50",
      item, unit, ledger: "official", note: "Smoke check",
      entry_key: form.querySelector("[name=entry_key]").value,
    });
    const answer = await fetch("/expenses/new/", {
      method: "POST", body, credentials: "same-origin", redirect: "follow",
    });
    return answer.status;
  })()
`);
check("посетитель вносит трату сам", entered === 200, String(entered));
await goto(`${APP}/expenses/${WIDE}`);
check("внесённая трата сразу видна в списке", (await text()).includes("Smoke check"));

// Выгрузка «Строки для P&L» скачивается посетителем. Что внутри неё обе части,
// проверяет тест на демо-базе (`test_the_pnl_export_of_a_closed_month_has_both_halves`):
// xlsx — это zip, и читать его в браузере значило бы написать здесь второй
// разборщик книги.
await send("Network.clearBrowserCookies");
await goto(`${APP}/demo/enter/accountant/`);
await sleep(1200);
await goto(APP + june);
const exported = await evalIn(`
  (async () => {
    const answer = await fetch("export/pnl/", { credentials: "same-origin" });
    const body = await answer.blob();
    return { status: answer.status, size: body.size,
             name: answer.headers.get("content-disposition") || "" };
  })()
`);
check(
  "выгрузка строк для P&L скачивается посетителем",
  exported.status === 200 && exported.size > 4000 && exported.name.includes("pnl-2026-06"),
  JSON.stringify(exported),
);

// --- демо не притворяется включённым там, где его нет --------------------------

const dead = await evalIn(`
  fetch("/dev/login/", { method: "POST" }).then(r => r.status)
`);
check("вход-ярлык разработчика в демо выключен", dead === 404 || dead === 403, String(dead));

check("в консоли браузера чисто", logs.length === 0, logs.slice(0, 3).join(" | "));

report();
