/*
 * Смоук каркаса интерфейса (T016).
 *
 * Остальные смоуки проверяют поведение экранов; этот — то общее, что после
 * сборки экранов из компонентов стало одним на продукт, и потому ломается
 * сразу везде:
 *
 *   1. шапка говорит, чьими глазами человек смотрит (роль, партнёр, регистры,
 *      точка) — на каждом экране, а не только на первом;
 *   2. ни один экран не роняет в текст остатки шаблона: незакрытый тег или
 *      многострочный {# … #} вываливается на страницу видимым текстом, и
 *      разбором разметки это ловится плохо (проверено дважды на этом проекте);
 *   3. широкая таблица прокручивается внутри своего контейнера, а страница на
 *      1440 не едет по горизонтали ни на одном экране;
 *   4. в консоли браузера чисто.
 *
 * Проход настоящий: вход паролем с клавиатуры, переходы по ссылкам, все
 * четыре роли, все экраны продукта.
 *
 *     google-chrome --headless=new --remote-debugging-port=9350 \
 *         --user-data-dir=/tmp/chrome-web2 &
 * Стенд смоук готовит себе сам: приводит к сиду и считает период, потому что
 * смотрит на результат расчёта (метки регистров, след суммы). После себя
 * возвращает стенд к сиду — см. договор в шапке `cdp.mjs`.
 *
 *     COMPOSE_PROJECT_NAME=<стенд> APP=http://127.0.0.1:8060 \
 *         node tools/smoke_ui_shell.mjs
 */
import { attach, ensureCalculated, findPeriodAndGrid, loginWith, standFromSeed } from "./cdp.mjs";

const APP = process.env.APP || "http://127.0.0.1:8060";

const ROLES = [
  { code: "director", role: "Оперативный директор", ledgers: ["Официальный", "Дополнительный", "Внутренний"], unit: "" },
  { code: "accountant", role: "Бухгалтер", ledgers: ["Официальный"], unit: "" },
  { code: "manager", role: "Управляющий точки", ledgers: ["Официальный", "Дополнительный"], unit: "NS1" },
  { code: "admin", role: "Администратор сети", ledgers: ["Официальный"], unit: "" },
];

// Следы недоделанного шаблона: если они попали в текст страницы, человек читает
// исходник вместо продукта.
const LEFTOVERS = ["{%", "{{", "{#", "endblock", "endcomment", "None"];

const { evalIn, goto, send, clickOn, check, report, logs } = await attach();
const login = loginWith(APP, evalIn, goto);

standFromSeed();

await send("Emulation.setDeviceMetricsOverride", {
  width: 1440, height: 900, deviceScaleFactor: 1, mobile: false,
});

// Ведомость и след расчёта существуют только у посчитанного периода, а сид
// оставляет месяц непосчитанным намеренно. Считаем сами и настоящим нажатием —
// иначе смоук зависел бы от того, кто запускался до него (issue #76).
await login("director");
await ensureCalculated(APP, { evalIn, goto, clickOn });

/** Что видно на экране: шапка, текст, перелив таблиц и страницы. */
const look = () => evalIn(`
  (() => {
    const header = document.querySelector("header");
    const widest = [...document.querySelectorAll(".scroll")]
      .map(box => box.scrollWidth - box.clientWidth);
    return {
      header: header ? header.innerText.replace(/\\s+/g, " ").trim() : "",
      text: document.body.innerText,
      title: document.title,
      h1: (document.querySelector("h1") || {}).textContent || "",
      pageOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      containers: widest,
      // Плашки и метки регистров — те самые компоненты, которые должны
      // отрисоваться, а не остаться текстом.
      notices: document.querySelectorAll(".ok, .alert, .empty").length,
      ledgers: [...new Set([...document.querySelectorAll(".ledger")].map(x => x.textContent.trim()))],
    };
  })()
`);

/** Экран целиком: общие правила каркаса, одинаковые для всех страниц. */
async function screen(who, name, href) {
  await goto(APP + href);
  const seen = await look();

  check(`${who.code}/${name}: страница открылась`, seen.h1.trim().length > 0, seen.title);
  check(
    `${who.code}/${name}: шапка называет роль`,
    seen.header.includes(who.role),
    seen.header.slice(0, 120),
  );
  if (who.ledgers.length) {
    check(
      `${who.code}/${name}: шапка перечисляет его регистры`,
      who.ledgers.every((x) => seen.header.includes(x)),
      seen.header.slice(0, 160),
    );
  }
  check(
    `${who.code}/${name}: точка названа, только если человек ею ограничен`,
    who.unit ? seen.header.includes(who.unit) : !seen.header.includes("точка:"),
    seen.header.slice(0, 160),
  );
  const leftover = LEFTOVERS.filter((mark) => seen.text.includes(mark));
  check(`${who.code}/${name}: следов шаблона в тексте нет`, leftover.length === 0, leftover.join(" "));
  check(
    `${who.code}/${name}: страница не едет по горизонтали на 1440`,
    seen.pageOverflow === 0,
    `${seen.pageOverflow}px`,
  );
  const overflow = seen.containers.filter((x) => x > 0);
  check(
    `${who.code}/${name}: широкие таблицы читаются на 1440`,
    overflow.length === 0,
    overflow.join(", ") + "px",
  );
  // Чужого регистра не должно быть ни строкой, ни словом (D023).
  const forbidden = ["Официальный", "Дополнительный", "Внутренний"]
    .filter((x) => !who.ledgers.includes(x))
    .filter((x) => seen.text.includes(x));
  check(`${who.code}/${name}: чужих регистров на экране нет`, forbidden.length === 0, forbidden.join(", "));
  return seen;
}

// --- страницы без входа -------------------------------------------------------

await goto(APP + "/login/");
{
  const seen = await look();
  check("вход: заголовок на месте", seen.h1.includes("Вход"), seen.h1);
  check("вход: подписи полей связаны с полями", await evalIn(`
    (() => {
      const labels = [...document.querySelectorAll("form.card label")];
      return labels.length >= 2 && labels.every(l => document.getElementById(l.htmlFor));
    })()
  `));
  check(
    "вход: следов шаблона нет",
    LEFTOVERS.filter((m) => seen.text.includes(m)).length === 0,
  );
  check("вход: данных до входа не видно", !seen.text.includes("1 951 806,13"));
}

// --- все экраны под каждой ролью ---------------------------------------------

for (const who of ROLES) {
  await login(who.code);
  const { periodHref, gridHref } = await findPeriodAndGrid(APP, evalIn, goto);

  await screen(who, "периоды", "/periods/");
  const period = await screen(who, "период", periodHref);
  await screen(who, "табель", gridHref);
  await screen(who, "расхождения", periodHref + "variance/");
  await screen(who, "сверка", periodHref + "reconcile/");
  await screen(who, "смена пароля", "/account/password/");

  // След расчёта — со страницы периода, настоящей ссылкой с суммы. Сначала
  // возвращаемся на неё: ссылку надо брать там, где она есть, а не на той
  // странице, которая осталась открытой от прошлой проверки.
  await goto(APP + periodHref);
  const traceHref = await evalIn(`
    (() => {
      const a = document.querySelector("table.sheet tbody tr td:last-child a.trace");
      return a ? a.getAttribute("href") : "";
    })()
  `);
  check(`${who.code}: сумма строки ведёт к следу расчёта`, !!traceHref, traceHref);
  if (traceHref) {
    await screen(who, "след расчёта", traceHref);
  }

  check(
    `${who.code}: метки регистров на ведомости — ровно его`,
    period.ledgers.every((x) => who.ledgers.includes(x)),
    period.ledgers.join(", "),
  );
}

const noisy = logs.filter((line) => /error|exception|Uncaught/i.test(line));
check("в консоли браузера чисто", noisy.length === 0, noisy.join(" | "));

report();
