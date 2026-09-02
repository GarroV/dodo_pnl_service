/*
 * Смоук навигации областями учёта и счётчиков ждущей работы (T207, модуль 10).
 *
 * Что здесь проверяется нажатиями и чего не доказать разбором разметки:
 *
 *   1. меню действительно ОТКРЫВАЕТСЯ — мышью и с клавиатуры, — а пункты
 *      закрытого меню табуляцией не ловятся: `<details>` обещает это поведение
 *      браузером, но обещание надо однажды увидеть;
 *   2. открытая панель ложится ПОВЕРХ страницы и не уводит её вбок ни на 1440,
 *      ни на 375 — именно этим ломались прошлые правки шапки (issue #151);
 *   3. счётчик у пункта равен тому, что показывает сам экран инбокса, и у
 *      управляющего он уезжает вместе со списком, который режет база;
 *   4. на телефоне область и пункт ловятся пальцем, а не кончиком ногтя.
 *
 * Три роли, потому что набор областей у них разный: «Настройки» ведёт только
 * администратор сети, у остальных их нет вовсе.
 *
 * Областей стало пять (D064): «Расчёт» и «Отчёты» появились вместе с решением
 * показывать ВСЕ пункты словаря эталона, включая ненаписанные, — ненаписанные
 * ведут на страницу «Разработка в процессе», а не в пустоту. До этого их не
 * было, и владелец, открыв демо, решил, что P&L не построен (#218).
 *
 * Стенд смоук приводит к сиду сам и возвращает к нему после (договор в шапке
 * `cdp.mjs`). Материал инбокса заводится прямо в базе: путь, которым строка
 * попадает в инбокс, проверяют смоуки счетов, а здесь важна шапка.
 *
 *     COMPOSE_PROJECT_NAME=dodo-pnl-web2 APP=http://127.0.0.1:8080 CDP_PORT=9352 \
 *         node tools/smoke_nav_groups.mjs
 */
import { attach, loginWith, sql, standFromSeed } from "./cdp.mjs";

const APP = process.env.APP || "http://127.0.0.1:8080";
const PHONE = { width: 375, height: 812 };
const DESKTOP = { width: 1440, height: 900 };
const TAP = 44;

const { evalIn, goto, send, clickOn, check, report, logs } = await attach();

/*
 * Настоящее нажатие Enter — с `text`.
 *
 * Без него Chrome не порождает `keypress` и не выполняет действие по умолчанию:
 * форма, у которой есть свой обработчик, отправится, а раскрытие `<details>`
 * (действие самого браузера) не произойдёт. Проверять клавиатуру событием,
 * которое до действия по умолчанию не доходит, — значит проверять не то.
 */
const pressEnter = async () => {
  const base = { key: "Enter", code: "Enter", windowsVirtualKeyCode: 13,
                 nativeVirtualKeyCode: 13 };
  await send("Input.dispatchKeyEvent", { type: "rawKeyDown", ...base });
  await send("Input.dispatchKeyEvent", { type: "char", text: "\r", ...base });
  await send("Input.dispatchKeyEvent", { type: "keyUp", ...base });
};

/** Куда уводит табуляция: список того, на что встаёт фокус за N нажатий. */
async function tabWalk(steps = 12) {
  await evalIn(`document.querySelector(".brand").focus()`);
  const seen = [];
  for (let i = 0; i < steps; i++) {
    await send("Input.dispatchKeyEvent", {
      type: "rawKeyDown", key: "Tab", code: "Tab",
      windowsVirtualKeyCode: 9, nativeVirtualKeyCode: 9,
    });
    await send("Input.dispatchKeyEvent", {
      type: "keyUp", key: "Tab", code: "Tab",
      windowsVirtualKeyCode: 9, nativeVirtualKeyCode: 9,
    });
    seen.push(await evalIn(`
      (() => {
        const el = document.activeElement;
        if (!el) return "";
        return (el.className || "") + "|" + (el.getAttribute("href") || "");
      })()
    `));
  }
  return seen;
}
const signIn = loginWith(APP, evalIn, goto);

/*
 * Роли смоука и слово, по которому видно, что вход СОСТОЯЛСЯ.
 *
 * Проверять это обязательно. `loginWith` ждёт отправки формы фиксированные
 * полторы секунды, и на загруженной машине их не хватает: браузер остаётся
 * прежней ролью, а проверки продолжают идти — и часть из них при этом остаётся
 * ЗЕЛЁНОЙ, разглядывая чужую страницу. Зелёная проверка, смотрящая не туда,
 * дороже красной: она не зовёт разбираться.
 */
const WHO = {
  director: "Оперативный директор",
  manager: "Управляющий точки",
  admin: "Администратор сети",
};

/** Войти и дождаться, пока шапка подтвердит, что вошли именно этой ролью. */
async function login(code) {
  await signIn(code);
  for (let i = 0; i < 60; i++) {
    const role = await evalIn(`
      (() => {
        const badge = document.querySelector(".role");
        return badge ? badge.textContent.trim() : "";
      })()
    `).catch(() => "");
    if (role === WHO[code]) return;
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error(`вход ролью ${code} не состоялся: шапка не назвала «${WHO[code]}»`);
}

standFromSeed();

const wait = (ms) => new Promise((r) => setTimeout(r, ms));

/*
 * Перейти на страницу и дождаться, что открылась ИМЕННО ОНА.
 *
 * `goto` из `cdp.mjs` считает страницу готовой по `readyState`, а он остаётся
 * `complete` у ПРЕЖНЕЙ страницы, пока новая не начала грузиться. Здесь это
 * давало не красный смоук, а хуже — фальшиво-зелёный: после входа управляющим
 * читалась ещё не сменившаяся директорская страница, и проверка «счётчик равен
 * экрану» сходилась на директорских числах у обоих. Срез по точке при этом был
 * исправен — проверено прямым запросом под ролью `app_user`: директору видно 5
 * строк, управляющему 2.
 *
 * Поэтому документ помечается ДО перехода: свежий документ метки не несёт, и
 * её исчезновение — единственный надёжный признак, что мы смотрим на новую
 * страницу, а не на остатки прежней.
 */
async function visit(path) {
  await evalIn(`document.documentElement.dataset.smokeStale = "1"`);
  await goto(APP + path);
  for (let i = 0; i < 60; i++) {
    const ready = await evalIn(`
      (() => document.documentElement.dataset.smokeStale !== "1"
              && document.readyState === "complete"
              && location.pathname === ${JSON.stringify(path)})()
    `).catch(() => false);
    if (ready) return;
    await wait(150);
  }
  throw new Error(`страница ${path} так и не открылась`);
}
const screen = (size) => send("Emulation.setDeviceMetricsOverride", {
  ...size, deviceScaleFactor: 1, mobile: false,
});

/** Что видно в навигации: области, их счётчики, состояние и размеры. */
const nav = () => evalIn(`
  (() => {
    const areas = [...document.querySelectorAll(".appnav__area")].map(area => {
      const name = area.querySelector(".appnav__area-name");
      const badge = name.querySelector(".appnav__count");
      const box = name.getBoundingClientRect();
      return {
        title: name.childNodes[0].textContent.trim(),
        count: badge ? Number(badge.textContent.trim()) : null,
        here: area.classList.contains("appnav__area--here"),
        current: name.getAttribute("aria-current"),
        open: area.hasAttribute("open"),
        height: Math.round(box.height),
        items: [...area.querySelectorAll(".appnav__menu > *")].map(el => ({
          title: (el.childNodes[0].textContent || "").trim(),
          href: el.getAttribute("href") || "",
          count: el.querySelector(".appnav__count")
            ? Number(el.querySelector(".appnav__count").textContent.trim()) : null,
          height: Math.round(el.getBoundingClientRect().height),
          // Высота самой метки, а не строки: правило «под палец» однажды
          // зацепило её вместе со строкой, и круглая метка стала овалом на
          // все 44 пикселя. Разбором разметки такое не ловится.
          badge: el.querySelector(".appnav__count")
            ? Math.round(el.querySelector(".appnav__count").getBoundingClientRect().height) : 0,
          // Видна ли строка меню целиком: закрытое меню даёт нули, открытое —
          // настоящие координаты, и по ним же видно, не уехало ли оно за край.
          inWindow: (() => {
            const r = el.getBoundingClientRect();
            return r.width > 0 && r.left >= 0 && r.right <= innerWidth;
          })(),
        })),
      };
    });
    return {
      areas,
      counters: document.querySelectorAll(".appnav .appnav__count").length,
      overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    };
  })()
`);

/** Сколько работы ждёт по мнению самого экрана инбокса. */
async function waitingOnScreen() {
  await visit("/inbox/");
  return evalIn(`
    (() => {
      const rows = document.querySelectorAll("tr[data-fact]").length;
      const papers = document.querySelector("[data-papers]");
      return rows + (papers ? Number(papers.getAttribute("data-papers")) : 0);
    })()
  `);
}

const area = (seen, title) => seen.areas.find((a) => a.title === title);

// ── 1. Пустая очередь: ноля нет нигде ──────────────────────────────────────

await screen(DESKTOP);
await login("director");
await visit("/periods/");
{
  const seen = await nav();
  check("director: области учёта на месте",
    seen.areas.map((a) => a.title).join(" · ")
      === "Сбор данных · Расчёт · Отчёты · Справочники",
    seen.areas.map((a) => a.title).join(" · "));
  check("director: область текущей страницы помечена",
    area(seen, "Сбор данных").here && area(seen, "Сбор данных").current === "true");
  check("director: чужая область не помечена", !area(seen, "Справочники").here);
  check("пустая очередь не показана нулём", seen.counters === 0, `счётчиков: ${seen.counters}`);
}
{
  // Клавиатура не должна ходить по невидимому: закрытое меню — не «спрятанное
  // оформлением», а по-настоящему свёрнутое, и браузер обязан пропускать его
  // пункты. Проверяется настоящим обходом Tab, а не догадкой по стилям.
  const walk = await tabWalk();
  const hidden = walk.filter((where) => where.startsWith("appnav__link"));
  check("пункты закрытого меню не ловит табуляция", hidden.length === 0,
    walk.join(" → "));
}

// ── 2. Материал: строки без статьи по сети и на точке управляющего ─────────

const tenant = sql("select id from tenants where code = 'rs-dev'");
const ns1 = sql(`select id from units where tenant_id = '${tenant}' and code = 'NS1'`);
sql(`
  insert into facts (tenant_id, period, doc_date, ledger, amount, currency,
                     title, source, dedup_key, allocation, pnl_item_id, unit_id)
  select '${tenant}', date_trunc('month', current_date)::date, current_date,
         'official', -1000 - n, 'RSD', 'Смоук-навигация ' || n, 'manual',
         'smoke:nav:net:' || n, 'pending',
         (select id from pnl_items where code = 'unclassified'), null
    from generate_series(1, 3) as n;
  insert into facts (tenant_id, period, doc_date, ledger, amount, currency,
                     title, source, dedup_key, allocation, pnl_item_id, unit_id)
  select '${tenant}', date_trunc('month', current_date)::date, current_date,
         'official', -2000 - n, 'RSD', 'Смоук-навигация NS1 ' || n, 'manual',
         'smoke:nav:ns1:' || n, 'direct',
         (select id from pnl_items where code = 'unclassified'), '${ns1}'
    from generate_series(1, 2) as n;
`);

// ── 3. Счётчик равен тому, что показывает экран, — у каждой роли своё ──────

const counted = {};
for (const who of ["director", "manager"]) {
  await login(who);
  const onScreen = await waitingOnScreen();
  await visit("/periods/");
  const seen = await nav();
  const collect = area(seen, "Сбор данных");
  const inbox = collect.items.find((i) => i.title === "Инбокс документов");
  counted[who] = inbox.count;

  check(`${who}: у пункта столько же, сколько на экране инбокса`,
    inbox.count === onScreen, `в шапке ${inbox.count}, на экране ${onScreen}`);
  check(`${who}: счётчик области равен сумме её пунктов`,
    collect.count === onScreen, `у области ${collect.count}, на экране ${onScreen}`);
  check(`${who}: у пунктов без очереди счётчика нет`,
    collect.items.filter((i) => i.count !== null).length === 1,
    collect.items.map((i) => `${i.title}:${i.count}`).join(" "));
}

// Материал заведён так, что три строки лежат на сети, а две — на точке
// управляющего. Сойдись числа — значит база срез не сделала (или мы смотрим не
// той ролью), а «счётчик равен экрану» этого не поймает: разъедутся они оба
// одинаково.
check("у управляющего очередь короче директорской — база режет и её тоже",
  counted.manager < counted.director,
  `управляющий ${counted.manager}, директор ${counted.director}`);

// ── 4. Меню открывается мышью, клавиатурой и не уводит страницу вбок ───────

await login("director");
await visit("/periods/");
await clickOn(
  `[...document.querySelectorAll(".appnav__area-name")].find(el => el.textContent.includes("Справочники"))`,
  "область «Справочники»",
);
await wait(250);
{
  const seen = await nav();
  const refs = area(seen, "Справочники");
  check("меню открылось нажатием", refs.open);
  check("пункты открытого меню видны целиком", refs.items.every((i) => i.inWindow),
    refs.items.map((i) => `${i.title}:${i.inWindow}`).join(" "));
  check("открытое меню не увело страницу вбок на 1440", seen.overflow === 0,
    `${seen.overflow}px`);
}
{
  const walk = await tabWalk();
  check("пункты открытого меню табуляция ловит",
    walk.some((where) => where.includes("/directory/employees/")), walk.join(" → "));
}

// Клавиатура: фокус на кнопке области и Enter. Ровно то, ради чего взят
// `<details>`, — и ровно то, что своя реализация на скрипте обычно теряет.
await visit("/periods/");
await evalIn(`
  [...document.querySelectorAll(".appnav__area-name")]
    .find(el => el.textContent.includes("Сбор данных")).focus()
`);
await pressEnter();
await wait(250);
{
  const seen = await nav();
  check("меню открывается с клавиатуры", area(seen, "Сбор данных").open);
}

// Пункт меню — настоящая ссылка: нажали и ушли.
await clickOn(
  `document.querySelector('.appnav__menu a[href="/expenses/"]')`,
  "пункт «Наличные расходы»",
);
await wait(800);
{
  const where = await evalIn(`location.pathname`);
  check("пункт меню уводит на свой экран", where === "/expenses/", where);
  const seen = await nav();
  check("после перехода помечена область нового экрана",
    area(seen, "Сбор данных").here);
}

// ── 5. Управляющий на телефоне ─────────────────────────────────────────────

await screen(PHONE);
await login("manager");
await visit("/periods/");
{
  const seen = await nav();
  check("manager: областей ровно те, что он ведёт",
    seen.areas.map((a) => a.title).join(" · ")
      === "Сбор данных · Расчёт · Отчёты · Справочники",
    seen.areas.map((a) => a.title).join(" · "));
  check("375: страница не едет вбок с закрытым меню", seen.overflow === 0,
    `${seen.overflow}px`);
  check("375: кнопка области ловится пальцем",
    seen.areas.every((a) => a.height >= TAP),
    seen.areas.map((a) => `${a.title}:${a.height}`).join(" "));
}

await clickOn(
  `[...document.querySelectorAll(".appnav__area-name")].find(el => el.textContent.includes("Сбор данных"))`,
  "область «Сбор данных» на телефоне",
);
await wait(250);
{
  const seen = await nav();
  const collect = area(seen, "Сбор данных");
  check("375: меню открылось", collect.open);
  check("375: страница не поехала вбок от открытого меню", seen.overflow === 0,
    `${seen.overflow}px`);
  check("375: пункты меню видны целиком", collect.items.every((i) => i.inWindow),
    collect.items.map((i) => `${i.title}:${i.inWindow}`).join(" "));
  check("375: строка меню ловится пальцем",
    collect.items.every((i) => i.height >= TAP),
    collect.items.map((i) => `${i.title}:${i.height}`).join(" "));
  check("375: счётчик виден и там", collect.count > 0, `${collect.count}`);
  check("375: метка счётчика осталась меткой, а не растянулась на строку",
    collect.items.every((i) => i.badge === 0 || i.badge <= 24),
    collect.items.map((i) => `${i.title}:${i.badge}`).join(" "));
}

// ── 6. Администратор сети: три области, и каждая со своими пунктами ────────

await screen(DESKTOP);
await login("admin");
await visit("/periods/");
{
  const seen = await nav();
  check("admin: все пять областей учёта",
    seen.areas.map((a) => a.title).join(" · ")
      === "Сбор данных · Расчёт · Отчёты · Справочники · Настройки",
    seen.areas.map((a) => a.title).join(" · "));
  check("admin: справочники ведёт он, и они внутри своей области",
    area(seen, "Справочники").items.map((i) => i.href).join(" ") === "/directory/ /rules/",
    area(seen, "Справочники").items.map((i) => i.href).join(" "));
  check("admin: роли и права — в настройках, рядом с интеграцией",
    area(seen, "Настройки").items.map((i) => i.href).join(" ")
      === "/roles/ /settings/dodo-is/",
    area(seen, "Настройки").items.map((i) => i.href).join(" "));
}

const noisy = logs.filter((line) => /error|exception|Uncaught/i.test(line));
check("в консоли браузера чисто", noisy.length === 0, noisy.join(" | "));

report();
