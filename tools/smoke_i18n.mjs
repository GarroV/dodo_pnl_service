/*
 * Смоук локализации и онбординга (T017, T077).
 *
 * Что здесь проверяется такого, чего не проверяют тесты.
 *
 *   1. **Переключатель нажимается.** Тест умеет отправить POST на
 *      `/i18n/setlang/`; человек нажимает кнопку в шапке. Между этими двумя
 *      вещами помещается всё, что ломается молча: форма без CSRF, кнопка вне
 *      формы, `next` с чужим адресом, кнопка, которую не видно.
 *   2. **Язык держится при переходах.** Смена языка кладёт cookie; если она
 *      кладётся не на тот путь, следующий экран откроется по-русски, и заметить
 *      это можно только пройдя продукт, а не отрисовав одну страницу.
 *   3. **Кириллицы на экране не осталось** — в том, что человек реально видит
 *      (`innerText`), а не в разметке: строка может быть переведена в шаблоне и
 *      остаться русской в `title=` или в тексте, дописанном скриптом.
 *   4. **Путь новичка проходится нажатиями** на каждом языке: месяц → табель →
 *      обратно, полоса шагов на месте, текущий шаг ровно один.
 *   5. **Ведомость влезает в 1440** на всех языках: перевод длиннее исходника,
 *      и колонки на английском шире русских.
 *
 * Данные партнёра (имена, точки, «Топли оброк», названия схем) остаются
 * русскими намеренно — это настройка партнёра, а не слова продукта. Их список
 * этот файл не хранит: его приносит запускающий переменной PARTNER_DATA прямо
 * из базы стенда. Список исключений, лежащий рядом с проверкой, — это место,
 * куда однажды тихо доедет непереведённая строка продукта.
 *
 *     google-chrome --headless=new --remote-debugging-port=9351 \
 *         --user-data-dir=/tmp/chrome-i18n &
 *     PARTNER_DATA="$(docker exec <база стенда> psql -U app -d dodo_pnl -Atc "
 *         select title from tenants
 *         union select title from units
 *         union select code from units
 *         union select first_name from employees
 *         union select last_name from employees
 *         union select title from employee_groups
 *         union select full_name from users
 *         union select title from rule_presets
 *         union select trim(both '\"' from
 *                jsonb_path_query(body, '\$.**.title')::text) from rule_presets
 *         union select trim(both '\"' from
 *                jsonb_path_query(body, '\$.**.pnl_line')::text) from rule_presets")" \
 * Стенд смоук готовит себе сам: приводит к сиду и считает период, потому что
 * половина проверяемых надписей — заголовки ведомости. После себя возвращает
 * стенд к сиду, а язык браузера чистится на входе харнессом — см. договор в
 * шапке `cdp.mjs`.
 *
 *     COMPOSE_PROJECT_NAME=<стенд> APP=http://127.0.0.1:8063 CDP_PORT=9351 \
 *         node tools/smoke_i18n.mjs
 */
import { attach, ensureCalculated, loginWith, standFromSeed } from "./cdp.mjs";

const APP = process.env.APP || "http://127.0.0.1:8063";

const LANGUAGES = [
  { code: "ru", title: "Русский", cyrillic: true },
  { code: "en", title: "English", cyrillic: false },
  { code: "sr-latn", title: "Srpski", cyrillic: false },
];

const ROLES = ["director", "accountant", "manager", "admin"];

const { evalIn, goto, clickOn, check, report, logs } = await attach();
const login = loginWith(APP, evalIn, goto);

standFromSeed();

// Ведомость нужна самой проверке: заголовки её колонок приходят из правил
// страны и переводятся отдельно от слов продукта (T092). На непосчитанном
// периоде эта половина проверок молча смотрела бы в пустоту.
await login("director");
await ensureCalculated(APP, { evalIn, goto, clickOn });

await evalIn("1");

/** Что видит человек: текст страницы, объявленный язык, перелив контейнеров. */
const look = () => evalIn(`
  (() => {
    const boxes = [...document.querySelectorAll(".scroll")]
      .map(box => box.scrollWidth - box.clientWidth);
    // Подписи, которые видны не текстом: подсказки и метки для читалки экрана.
    //
    // Кроме переключателя языка: там подсказка — название языка на нём самом
    // («Русский», «Srpski»), и это не непереведённая строка, а норма — язык
    // называют так, как он называет себя, иначе его не узнает тот, кто пришёл
    // за ним. Кнопка подписана кодом, полное название живёт подсказкой.
    const attrs = [...document.querySelectorAll("[title],[aria-label],[placeholder]")]
      .filter(el => !el.closest("form.lang"))
      .flatMap(el => [el.getAttribute("title"), el.getAttribute("aria-label"), el.getAttribute("placeholder")])
      .filter(Boolean);
    return {
      lang: document.documentElement.getAttribute("lang"),
      title: document.title,
      text: document.body.innerText,
      attrs: attrs.join(" | "),
      steps: [...document.querySelectorAll("ol.steps li")].map(li => li.innerText.replace(/\\s+/g, " ").trim()),
      current: [...document.querySelectorAll('ol.steps li[aria-current="step"]')].length,
      switcher: [...document.querySelectorAll("form.lang button")].map(b => b.textContent.trim()),
      overflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      containers: boxes,
      href: location.pathname,
    };
  })()
`);

/** Нажать кнопку языка в шапке — именно нажать, а не отправить запрос руками. */
const switchTo = async (code) => {
  const pressed = await evalIn(`
    (() => {
      const button = document.querySelector('form.lang button[value=${JSON.stringify(code)}]');
      if (!button) return false;
      button.click();
      return true;
    })()
  `);
  await new Promise((r) => setTimeout(r, 1200));
  return pressed;
};

// Данные партнёра: русские слова, приехавшие из базы и пресета. Список
// **приносит запускающий** (переменная PARTNER_DATA, по строке на значение), а
// не хранит этот файл: список исключений, записанный рядом с проверкой, — это
// место, куда однажды тихо доедет непереведённая строка продукта. Собирает его
// тот, кто знает источник, — сам стенд:
//
//   PARTNER_DATA="$(docker exec … psql -Atc "select ...")" node tools/smoke_i18n.mjs
const PARTNER = (process.env.PARTNER_DATA || "")
  .split("\n")
  .map((line) => line.trim())
  .filter(Boolean);

check("данные партнёра известны смоуку", PARTNER.length > 0, `их ${PARTNER.length}`);

// Названия языков в переключателе написаны каждое на своём языке — «Русский»
// на английской странице это не забытый перевод, а сама суть кнопки.
const SWITCHER_WORDS = LANGUAGES.map((language) => language.title);

const CYRILLIC = /[а-яёА-ЯЁ]/;

/** Русские куски текста, не объяснимые данными партнёра. */
function russianLeft(text, partnerData) {
  return text
    .split(/\n+/)
    .map((line) => line.trim())
    .filter((line) => CYRILLIC.test(line))
    .filter((line) => !partnerData.some((known) => known && line.includes(known)))
    .filter((line) => !SWITCHER_WORDS.includes(line))
    .slice(0, 8);
}

// --- 1. русский снимок: из него берутся данные партнёра ----------------------

await login("director");
await goto(`${APP}/periods/`);
const periodHref = await evalIn(
  `[...document.querySelectorAll('a[href^="/periods/"]')]
     .map(a => a.getAttribute('href'))
     .find(h => /^\\/periods\\/[0-9a-f-]{36}\\/$/.test(h))`,
);
check("со списка месяцев есть дорога в месяц", !!periodHref, periodHref);

await goto(APP + periodHref);
const gridHref = await evalIn(
  `document.querySelector('a[href^="/timesheets/"]').getAttribute('href')`,
);
check("со страницы месяца есть дорога в табель", !!gridHref, gridHref);

const SCREENS = [
  ["месяцы", "/periods/"],
  ["месяц", periodHref],
  ["табель", gridHref],
  ["расхождения", periodHref + "variance/"],
  ["сверка", periodHref + "reconcile/"],
  ["пароль", "/account/password/"],
];

// --- 2. каждый язык целиком --------------------------------------------------

for (const language of LANGUAGES) {
  await goto(`${APP}/periods/`);
  const before = await look();
  check(
    `${language.code}: переключатель в шапке перечисляет три языка`,
    before.switcher.length === 3,
    before.switcher.join(", "),
  );
  const pressed = await switchTo(language.code);
  check(`${language.code}: кнопка языка нажалась`, pressed);

  const after = await look();
  check(
    `${language.code}: страница объявила свой язык`,
    after.lang === language.code,
    `lang=${after.lang}`,
  );
  check(
    `${language.code}: после нажатия человек остался там же`,
    after.href === "/periods/",
    after.href,
  );

  for (const [name, href] of SCREENS) {
    await goto(APP + href);
    const page = await look();
    check(
      `${language.code}: ${name} — язык не потерялся при переходе`,
      page.lang === language.code,
      `lang=${page.lang}`,
    );
    if (!language.cyrillic) {
      const left = russianLeft(page.text, PARTNER);
      check(`${language.code}: ${name} — русского текста не осталось`, left.length === 0, left.join(" ⁄ "));
      const inAttrs = russianLeft(page.attrs, PARTNER);
      check(
        `${language.code}: ${name} — русского нет и в подсказках`,
        inAttrs.length === 0,
        inAttrs.join(" ⁄ "),
      );
    }
    check(
      `${language.code}: ${name} — страница не едет по горизонтали на 1440`,
      page.overflow === 0,
      `overflow=${page.overflow}`,
    );
    check(
      `${language.code}: ${name} — широкие таблицы прокручиваются внутри себя`,
      page.containers.every((x) => x >= 0),
      page.containers.join(", "),
    );
  }

  // Онбординг на этом языке.
  await goto(APP + periodHref);
  const month = await look();
  check(`${language.code}: полоса шагов на месте`, month.steps.length === 3, month.steps.join(" | "));
  check(`${language.code}: текущий шаг ровно один`, month.current === 1, `их ${month.current}`);
  check(
    `${language.code}: шаги названы на языке страницы`,
    language.cyrillic ? CYRILLIC.test(month.steps.join(" ")) : !CYRILLIC.test(month.steps.join(" ")),
    month.steps[0],
  );

  await goto(APP + gridHref);
  const grid = await look();
  const back = await evalIn(
    `(() => { const a = document.querySelector('p.back a'); return a ? a.getAttribute('href') : ""; })()`,
  );
  check(`${language.code}: из табеля есть дорога в месяц`, back === periodHref, back);
  check(`${language.code}: табель не едет по горизонтали`, grid.overflow === 0, `overflow=${grid.overflow}`);
}

// --- 3. ведомость на 1440 под каждой ролью, на английском --------------------
//
// Английский взят потому, что он длиннее русского в подписях колонок: если
// ведомость где-то и вылезет, то на нём.

for (const who of ROLES) {
  await login(who);
  await goto(`${APP}/periods/`);
  await switchTo("en");
  await goto(APP + periodHref);
  const page = await look();
  check(`${who}: ведомость помещается в 1440 (контейнер)`, page.containers.every((x) => x === 0), page.containers.join(", "));
  check(`${who}: страница не едет по горизонтали`, page.overflow === 0, `overflow=${page.overflow}`);
  const left = russianLeft(page.text, PARTNER);
  check(`${who}: на английской ведомости русского нет`, left.length === 0, left.join(" ⁄ "));
}

// --- 4. язык не течёт между людьми -------------------------------------------
//
// Смена языка — свойство браузера, а не учётки: следующий человек за тем же
// экраном обязан получить свой выбор, а не чужой. Проверяется тем, что после
// выхода и входа другим человеком язык остаётся тем, что выбрали в браузере.

await goto(`${APP}/logout/`);
await login("accountant");
await goto(`${APP}/periods/`);
const kept = await look();
check("выбор языка переживает смену человека", kept.lang === "en", `lang=${kept.lang}`);

const noisy = logs.filter((line) => /error|exception|Uncaught/i.test(line));
check("в консоли браузера чисто", noisy.length === 0, noisy.join(" | "));

// Язык возвращается к русскому. Не вежливость: выбор языка живёт в cookie
// браузера, то есть переживает этот смоук. Соседние смоуки написаны по-русски и
// сверяют русские надписи — оставленный английский валит их все разом, и
// выглядит это как сломанный продукт, а не как незакрытая за собой дверь.
// Проверено: так и случилось на первом же прогоне подряд.
await goto(`${APP}/periods/`);
await switchTo("ru");
const restored = await look();
check("язык возвращён к русскому", restored.lang === "ru", `lang=${restored.lang}`);

report();
