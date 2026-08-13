/*
 * Смоук админки справочников (T018).
 *
 * Что здесь проверяется такого, чего не докажут тесты. Тесты ходят клиентом
 * Django: они видят код ответа и разметку, но не видят, дошёл ли человек до
 * кнопки и что он прочитал на экране. Здесь всё идёт настоящими нажатиями в
 * живом браузере: вход паролем, переходы по ссылкам, клик по «Сохранить»,
 * переключение языка кнопкой в шапке.
 *
 * Порядок разделов не случайный: сначала право на экран, потом обычная правка,
 * и только в конце — утверждение июня. После него стенд остаётся с закрытым
 * месяцем, поэтому смоук сам открывает его заново с причиной, а полный возврат
 * к эталону делается сидом (`manage.py seed_dev`).
 *
 * Стенд перед прогоном должен быть с эталона — смоук заводит версию условий
 * найма с фиксированной датой и повторный прогон поверх своих же следов
 * проверял бы не то. Возврат к эталону:
 *
 *     docker compose exec app python manage.py seed_dev
 * Стенд смоук приводит к сиду сам — и в начале, и после себя (см. договор в
 * шапке `cdp.mjs`). Поэтому запускать его можно в любом порядке и в одиночку,
 * а `COMPOSE_PROJECT_NAME` обязателен: без него сброс ушёл бы на чужой стенд.
 *
 *     COMPOSE_PROJECT_NAME=<стенд> APP=http://127.0.0.1:8065 CDP_PORT=9365 \\
 *         node tools/smoke_directory.mjs
 */
import { attach, loginWith, standFromSeed } from "./cdp.mjs";

const APP = process.env.APP || "http://127.0.0.1:8065";

const { evalIn, goto, send, check, report, logs } = await attach();
const loginRaw = loginWith(APP, evalIn, goto);

// Стенд к эталону сейчас и обратно к нему после — в том числе если смоук
// упадёт на полпути (issue #76). Порядок запуска смоуков больше ничего не
// решает: каждый начинает с известного входа и ничего за собой не оставляет.
standFromSeed();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Вход, доведённый до конца.
 *
 * Без ожидания смоук однажды читает страницу входа вместо продукта и объявляет
 * это отказом в правах: разница видна только по тексту, а проверка красная —
 * то есть ложная тревога выглядит ровно как настоящая находка. Уже случилось
 * на первом прогоне с бухгалтером.
 */
async function login(who) {
  for (let attempt = 0; attempt < 10; attempt++) {
    await loginRaw(who);
    const inside = await evalIn(`!!document.querySelector(".who")`);
    if (inside) return;
    await sleep(500);
  }
  throw new Error("не удалось войти: " + who);
}

const text = () => evalIn("document.body.innerText");

// Дата новой версии условий найма: заведомо позже всего, что есть в сиде, и
// позже любого месяца, который смоук утверждает ниже.
const NEW_VERSION_FROM = "2026-11-01";
const logout = () => evalIn(`
  (() => { const f = document.querySelector('form[action="/logout/"]'); if (f) f.submit(); })()
`).then(() => sleep(800));

/** Настоящий клик по элементу, найденному селектором и подписью. */
async function clickOn(selector, label = null) {
  const box = await evalIn(`
    (() => {
      const items = [...document.querySelectorAll(${JSON.stringify(selector)})];
      const el = ${label === null}
        ? items[0]
        : items.find(i => i.textContent.trim().includes(${JSON.stringify(label)}));
      if (!el) return null;
      el.scrollIntoView({ block: "center" });
      const r = el.getBoundingClientRect();
      return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
    })()
  `);
  if (!box) return false;
  for (const type of ["mousePressed", "mouseReleased"]) {
    await send("Input.dispatchMouseEvent", {
      type, x: box.x, y: box.y, button: "left", clickCount: 1,
    });
  }
  await sleep(1200);
  return true;
}

/** Заполнить поля формы по имени. Значения кладутся в поля, жмёт человек. */
const fill = (values) => evalIn(`
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

const cells = () => evalIn(`
  [...document.querySelectorAll("tbody tr")].map(
    tr => [...tr.querySelectorAll("td")].map(td => td.textContent.trim())
  )
`);

// =============================================================================
// 1. Право на экран: показано только тому, кому разрешено
// =============================================================================

const DIRECTORY_URLS = [
  "/directory/", "/directory/employees/", "/directory/groups/",
  "/directory/units/", "/directory/legal-entities/", "/directory/calendar/",
];

for (const role of ["director", "accountant", "manager"]) {
  await login(role);
  await goto(`${APP}/periods/`);
  const link = await evalIn(`!!document.querySelector('a[href="/directory/"]')`);
  check(`${role}: ссылки на справочники в шапке нет`, link === false);

  await goto(`${APP}/directory/`);
  const page = await text();
  check(`${role}: адрес отвечает отказом, а не пустотой`, page.includes("Ведение справочников"),
        page.slice(0, 120).replace(/\n/g, " "));
  check(`${role}: отказ не выдал содержимого справочника`, !page.includes("Из чего расчёт берёт"));
  await logout();
}

await login("admin");
await goto(`${APP}/periods/`);
check("администратор: ссылка на справочники в шапке есть",
      await evalIn(`!!document.querySelector('a[href="/directory/"]')`));

for (const url of DIRECTORY_URLS) {
  await goto(APP + url);
  const heading = await evalIn(`document.querySelector("h1")?.textContent.trim()`);
  check(`администратор открывает ${url}`, !!heading, heading || "нет заголовка");
}

// =============================================================================
// 2. Правка идёт настоящими нажатиями и видна на других экранах
// =============================================================================

await goto(`${APP}/directory/employees/`);
const employeeHref = await evalIn(`document.querySelector("tbody a")?.getAttribute("href")`);
check("список сотрудников ведёт в карточку", !!employeeHref, employeeHref || "нет ссылок");

await goto(APP + employeeHref);
const wasName = await evalIn(`document.querySelector('[name=last_name]').value`);
check("карточка открылась с заполненной фамилией", !!wasName, wasName);

await fill({ last_name: `${wasName} ПРОВЕРКА` });
await clickOn("button", "Сохранить карточку");
check("карточка сохранена — страница сказала об этом словами",
      (await text()).includes("Карточка сохранена"));
check("новая фамилия действительно записана",
      (await evalIn(`document.querySelector('[name=last_name]').value`)) === `${wasName} ПРОВЕРКА`);

await fill({ last_name: wasName });
await clickOn("button", "Сохранить карточку");
check("фамилия возвращена как была",
      (await evalIn(`document.querySelector('[name=last_name]').value`)) === wasName);

// --- версия условий найма ----------------------------------------------------

const versionsBefore = (await cells()).length;
// След прошлого прогона виден сразу и называется словами: иначе «версия не
// появилась» выглядело бы поломкой продукта, а не грязным стендом.
check("стенд с эталона — своей версии на карточке ещё нет",
      !(await text()).includes(NEW_VERSION_FROM),
      `на карточке уже есть версия с ${NEW_VERSION_FROM}: прогоните seed_dev`);
const current = await evalIn(`
  (() => {
    const row = [...document.querySelectorAll("tbody tr")].pop();
    const c = [...row.querySelectorAll("td")].map(td => td.textContent.trim());
    return { from: c[0], rate: c[4] };
  })()
`);

await fill({
  valid_from: NEW_VERSION_FROM,
  base_rate: "999.0000",
  coefficient: "1.0000",
});
await clickOn("button", "Завести версию");
const afterAdd = await cells();
check("новая версия условий появилась", afterAdd.length === versionsBefore + 1,
      `было ${versionsBefore}, стало ${afterAdd.length}`);
check("прежняя версия закрыта днём начала новой",
      afterAdd[versionsBefore - 1][1] === NEW_VERSION_FROM, afterAdd[versionsBefore - 1].join(" | "));
check("прежняя ставка осталась прежней — история не переписана",
      afterAdd[versionsBefore - 1][4] === current.rate,
      `${afterAdd[versionsBefore - 1][4]} вместо ${current.rate}`);
check("страница назвала, что именно случилось",
      (await text()).includes("Заведена новая версия условий найма"));

// Повторное «Сохранить» тем же самым: версия не заводится.
await clickOn("button", "Завести версию");
check("правка без изменений не плодит версий", (await cells()).length === afterAdd.length,
      `стало ${(await cells()).length}`);
check("и страница об этом говорит", (await text()).includes("Ничего не изменилось"));

// =============================================================================
// 3. Календарь: что набрал администратор, читает бухгалтер на своём экране
// =============================================================================

await goto(`${APP}/directory/calendar/`);
const monthHref = await evalIn(`document.querySelector("tbody a")?.getAttribute("href")`);
await goto(APP + monthHref);
const wasNorm = await evalIn(`document.querySelector('[name=norm_hours]').value`);
await fill({ norm_hours: "168" });
await clickOn("button", "Сохранить");
check("норма часов сохранена", (await text()).includes("168"), wasNorm);

await logout();
await login("director");
await goto(`${APP}/periods/`);
const periodHref = await evalIn(
  `[...document.querySelectorAll('a[href^="/periods/"]')]
     .map(a => a.getAttribute("href"))
     .find(h => /^\\/periods\\/[0-9a-f-]{36}\\/$/.test(h))`
);
await goto(APP + periodHref);
check("страница месяца показывает норму из календаря", (await text()).includes("168,00"));

await logout();
await login("admin");
await goto(APP + monthHref);
await fill({ norm_hours: wasNorm });
await clickOn("button", "Сохранить");
check("норма часов возвращена как была", (await text()).includes("176,00"), wasNorm);

// =============================================================================
// 4. Регистры: чужого не названо ни строкой, ни словом (D023)
// =============================================================================

// Чужой регистр — тот, которого нет у САМОГО смотрящего, а не «любой, кроме
// официального». Так было написано раньше, и проверка отстала от продукта:
// с T089 администратор сети видит все три регистра (иначе он не видел бы
// карточки курьеров и кухни — а справочник ведёт он один), его собственные
// названия честно стоят в шапке, и проверка краснела на исправном продукте.
//
// Список смотрящего берём с самой страницы, а не из головы: он и есть ответ на
// вопрос «что этой роли своё». Если своих у роли все три, сравнивать не с чем —
// и проверка об этом говорит вслух, а не делает вид, что что-то проверила.
const ALL_LEDGERS = ["Официальный", "Дополнительный", "Внутренний"];

for (const url of ["/directory/", "/directory/groups/", "/directory/employees/"]) {
  await goto(APP + url);
  const page = await text();
  const header = (page.match(/регистры: ([^\n]*)/) || ["", ""])[1];
  const foreign = ALL_LEDGERS.filter((name) => !header.includes(name));
  const named = foreign.filter((name) => page.replace(header, "").includes(name));
  check(`${url}: чужой регистр не назван`, named.length === 0,
        foreign.length === 0
          ? "у этой роли чужих регистров нет — сравнивать не с чем"
          : `чужие: ${foreign.join(", ")}; названы: ${named.join(", ") || "нет"}`);
}

// =============================================================================
// 5. Закрытый месяц не двигается правкой справочника
// =============================================================================

await logout();
await login("director");
await goto(APP + periodHref);
await clickOn("button", "Посчитать период");

/* Ждать расчёт **запросом**, а не переходом на страницу.
 *
 * Расчёт уходит в очередь, и страница прогресса опрашивает сервер сама. Пока
 * она это делает, переход на неё не завершается вовсе: смоук вставал намертво
 * посреди прогона, и выглядело это как зависший продукт. Поэтому готовность
 * проверяется запросом со страницы, и только потом делается переход. */
async function waitFor(url, marker, seconds = 120) {
  for (let i = 0; i < seconds; i++) {
    const ready = await evalIn(`
      fetch(${JSON.stringify(url)}, { credentials: "include" })
        .then(r => r.text())
        .then(t => t.includes(${JSON.stringify(marker)}))
    `);
    if (ready) return true;
    await sleep(1000);
  }
  return false;
}

check("расчёт периода дошёл до конца",
      await waitFor(APP + periodHref, "Утвердить период"));
await goto(APP + periodHref);
await clickOn("button", "Утвердить период");
await sleep(1500);
await goto(APP + periodHref);
check("июнь утверждён — есть на чём проверять отказ",
      (await text()).includes("Утверждён"), (await text()).slice(0, 100).replace(/\n/g, " "));

await logout();
await login("admin");
await goto(APP + employeeHref);
const beforeRefusal = (await cells()).length;
await fill({ valid_from: "2026-06-15", base_rate: "777.0000", coefficient: "1.0000" });
await clickOn("button", "Завести версию");
const refusal = await text();
check("правка внутри закрытого месяца отклонена словами", refusal.includes("уже утверждена"),
      refusal.slice(0, 160).replace(/\n/g, " "));
check("отказ назвал месяц, из-за которого отказано", refusal.includes("2026-06"));
check("отказ ничего не записал", (await cells()).length === beforeRefusal);

await goto(`${APP}/directory/calendar/`);
await goto(APP + monthHref);
await fill({ norm_hours: "100" });
await clickOn("button", "Сохранить");
check("норма закрытого месяца тоже не правится",
      (await text()).includes("уже утверждена"));

// Вернуть стенд: месяц открывается заново с причиной — тем же путём, что у человека.
await logout();
await login("director");
await goto(APP + periodHref);
await evalIn(`
  (() => {
    const field = document.querySelector('[name=reason]');
    if (field) field.value = "смоук админки справочников";
  })()
`);
await clickOn("button", "Открыть заново");
await sleep(1500);

// =============================================================================
// 6. Языки: справочник переведён целиком
// =============================================================================

await logout();
await login("admin");
for (const [language, label] of [["en", "English"], ["sr", "Srpski"]]) {
  await goto(`${APP}/directory/`);
  const switched = await clickOn("form.lang button", label);
  check(`${language}: язык переключается кнопкой в шапке`, switched);
  for (const url of ["/directory/", "/directory/employees/", "/directory/groups/",
                     "/directory/units/", "/directory/legal-entities/", "/directory/calendar/"]) {
    await goto(APP + url);
    // Данные партнёра остаются как есть — переводится интерфейс. Поэтому
    // кириллица ищется только в том, что пишет продукт: заголовки, подписи
    // колонок, подсказки и пустые состояния.
    const russian = await evalIn(`
      (() => {
        const parts = [
          document.querySelector("h1")?.textContent || "",
          document.querySelector("p.sub")?.textContent || "",
          [...document.querySelectorAll("th")].map(t => t.textContent).join(" "),
          [...document.querySelectorAll("label")].map(t => t.textContent).join(" "),
          [...document.querySelectorAll("a.btn, button")]
            .filter(b => !b.closest("form.lang"))
            .map(t => t.textContent).join(" "),
          document.querySelector(".notice")?.textContent || "",
        ].join(" ");
        return (parts.match(/[а-яА-ЯёЁ]+/g) || []).slice(0, 5);
      })()
    `);
    check(`${language} ${url}: русского на экране не осталось`, russian.length === 0,
          russian.join(", "));
  }
  await goto(APP + employeeHref);
  const card = await evalIn(`
    ([...document.querySelectorAll("h2, label, button")]
      .filter(e => !e.closest("form.lang"))
      .map(e => e.textContent).join(" ")
      .match(/[а-яА-ЯёЁ]+/g) || []).slice(0, 5)
  `);
  check(`${language} карточка сотрудника: русского не осталось`, card.length === 0, card.join(", "));
}

// Вернуть язык интерфейса, иначе следующий смоук читает английские надписи.
await goto(`${APP}/directory/`);
await clickOn("form.lang button", "Русский");

const noisy = logs.filter((l) => /EXCEPTION|Uncaught/.test(l));
check("в консоли браузера нет исключений", noisy.length === 0, noisy.slice(0, 2).join(" | "));

report();
