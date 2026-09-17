/*
 * Смоук рамки страны (T192, эталон модуля 17).
 *
 * Зачем он нужен помимо `tests/test_country_frame.py` и
 * `tests/test_frame_attempts_policy.py`. Тесты Django доказывают, что POST
 * пишет (или не пишет) строку в `rule_overrides` и `rule_frame_attempts` —
 * это про базу. Здесь — про то, что видит живой человек в браузере: находит
 * ли он рамку ДО правки (колонка списка, подпись на карточке), читает ли
 * отказ словами, когда пробует поставить значение мягче страны, и видит ли
 * потом журнал своих же отвергнутых попыток. Ни одна из этих трёх вещей не
 * проверяется чтением HTML в тесте клиента Django — там нет ни настоящего
 * клика по кнопке, ни второго захода на страницу за тем, что легло в базу.
 *
 * Путь везде — вход паролем, настоящие клики и присвоение полям значений (тот
 * же приём, что у `smoke_retro.mjs`: значение кладётся в поле, а нажимает
 * кнопку браузер настоящим событием мыши).
 *
 * Стенд смоук приводит к сиду сам — и в начале, и после себя (договор в
 * шапке `cdp.mjs`). Своей уборкой довозится то, чего сид не знает вовсе:
 * журнал попыток и переопределение ночных часов, заведённое ниже.
 *
 * Известный дефект #206: `goto` считает страницу готовой по `document.
 * readyState` ПРЕЖНЕЙ страницы. Поэтому каждый переход в этом файле не
 * доверяет самому факту навигации, а ждёт (poll'ит) появления конкретного
 * текста, которого на странице, с которой мы уходим, точно не было —
 * `visit()` и `clickAndWait()` ниже.
 *
 *     COMPOSE_PROJECT_NAME=dodo-pnl-rules2 APP=http://127.0.0.1:8130 CDP_PORT=9430 \
 *         SMOKE_SHOTS=/путь/к/снимкам node tools/smoke_country_frame.mjs
 */
import { mkdirSync, writeFileSync } from "node:fs";

import { attach, loginWith, onCleanup, sql, standFromSeed } from "./cdp.mjs";

const APP = process.env.APP || "http://127.0.0.1:8130";
const SHOTS = process.env.SMOKE_SHOTS || "/tmp";
mkdirSync(SHOTS, { recursive: true });

const NIGHT = "hour_types.night.pay_percent";
const GUARANTEE = "minimum_guarantee.enabled";
const FREE_RULE = "rates.net_factor";

const { evalIn, goto, send, clickOn, check, report, logs } = await attach();
const login = loginWith(APP, evalIn, goto);

standFromSeed();

// Рамка не оставляет за собой ни версии правила, ни строк журнала — оба вне
// сида (сид их не заводит и вернуть их к эталону не может).
function cleanupFrame() {
  sql(`delete from rule_overrides where path = '${NIGHT}'`);
  sql("delete from rule_frame_attempts");
}
onCleanup("переопределения и журнал попыток рамки убраны", cleanupFrame);
cleanupFrame();

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const text = () => evalIn("document.body.innerText");

/** Переход, который ждёт именно НОВУЮ страницу, а не смены адреса (issue #206). */
async function visit(url, ready, seconds = 20) {
  await goto(url);
  for (let i = 0; i < seconds * 4; i++) {
    if (await evalIn(ready).catch(() => false)) return;
    await sleep(250);
  }
  throw new Error(`не дождались страницы: ${url} (${ready})`);
}

/** Нажатие кнопки по видимому тексту — настоящим событием мыши — с тем же
 * ожиданием новой страницы, что и у `visit()`: POST через форму тоже
 * навигация, и той же болезни (#206) подвержена та же лечёба. */
async function clickAndWait(buttonText, ready, seconds = 20) {
  const finder = `[...document.querySelectorAll("button")]
      .find(b => b.textContent.includes(${JSON.stringify(buttonText)}))`;
  await clickOn(finder, `кнопка «${buttonText}»`);
  for (let i = 0; i < seconds * 4; i++) {
    if (await evalIn(ready).catch(() => false)) return;
    await sleep(250);
  }
  throw new Error(`не дождались результата кнопки «${buttonText}»: ${ready}`);
}

/** Значения — в поля формы напрямую (тот же приём, что у `smoke_retro.mjs`):
 * жмёт кнопку браузер, а не обработчик, но набор текста по символу здесь не
 * добавил бы уверенности — только время. */
async function fillFields(values) {
  const missing = await evalIn(`
    (() => {
      const values = ${JSON.stringify(values)};
      for (const [name, value] of Object.entries(values)) {
        const field = document.querySelector('[name=' + name + ']');
        if (!field) return name;
        field.value = value;
      }
      return "";
    })()
  `);
  if (missing) throw new Error(`нет поля формы: ${missing}`);
}

/** Ячейка «Кто меняет» строки правила в списке — по коду правила в её ссылке. */
async function rowOwner(path) {
  return evalIn(`
    (() => {
      const link = [...document.querySelectorAll("td a")]
        .find((a) => a.textContent.trim() === ${JSON.stringify(path)});
      const row = link ? link.closest("tr") : null;
      if (!row) return null;
      return row.children[4] ? row.children[4].textContent.trim() : null;
    })()
  `);
}

/**
 * Снимок экрана рядом с тем текстом, который проверяется.
 *
 * Ищется самый маленький (без детей) элемент, содержащий фразу, — иначе
 * подходящим «нашедшимся» узлом оказался бы `<body>`, где угодно на странице
 * есть эта подстрока, и прокрутка никуда бы не сдвинулась.
 */
async function shot(name, anchor = "") {
  await evalIn(`
    (() => {
      const phrase = ${JSON.stringify(anchor)};
      if (!phrase) { window.scrollTo(0, 0); return true; }
      const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_ELEMENT);
      let found = null, node;
      while ((node = walker.nextNode())) {
        if (node.children.length === 0 && node.textContent.includes(phrase)) {
          found = node;
          break;
        }
      }
      (found || document.body).scrollIntoView({ block: "start" });
      if (found) window.scrollBy(0, -24);
      return !!found;
    })()
  `);
  await sleep(200);
  const { data } = await send("Page.captureScreenshot", { format: "png" });
  const path = `${SHOTS}/${name}.png`;
  writeFileSync(path, Buffer.from(data, "base64"));
  console.log(`     снимок: ${path}`);
}

// ==============================================================================
// Роль admin: право rules.manage
// ==============================================================================

await login("admin");
// Первая же страница правил — надёжный признак того, что вход состоялся:
// `.who .role` рисуется только вошедшему принципалу (T090), в отличие от
// `.who`, который стоит в шапке всегда, вошёл человек или нет.
await visit(`${APP}/rules/?q=hour_types.night`, `!!document.querySelector(".who .role")`);

// --- 1. Список правил называет владельца значения -----------------------------

let owner = await rowOwner(NIGHT);
check(
  "список правил называет владельца значения — «Внутри рамки» у ночных часов",
  owner === "Внутри рамки",
  owner,
);
await shot("frame-list", NIGHT);

// --- 2. Запертое правило подписано законом -------------------------------------

await visit(
  `${APP}/rules/?q=minimum_guarantee`,
  `document.body.innerText.includes(${JSON.stringify(GUARANTEE)})`,
);
owner = await rowOwner(GUARANTEE);
check(
  "запертое правило подписано законом — «Закон» у гарантии часов",
  owner === "Закон",
  owner,
);

// --- 3. Рамка видна ДО правки ---------------------------------------------------

await visit(
  `${APP}/rules/${NIGHT}/`,
  `document.body.innerText.includes("Кто меняет: Внутри рамки")`,
);
let page = await text();
check(
  "карточка правила называет владельца до правки",
  page.includes("Кто меняет: Внутри рамки"),
);
check("карточка называет границу — «не ниже 1.26»", page.includes("не ниже 1.26"));
check(
  "карточка называет источник — статью закона",
  page.includes("Закон о раду, чл. 108"),
);
await shot("frame-card", "Кто меняет: Внутри рамки");

// --- 3b. Действующее значение уже мягче рамки (заведено раньше, чем рамка -----
//         стала такой) -----------------------------------------------------------
//
// Строка кладётся В ОБХОД экрана нарочно: через форму такое переопределение
// уже не проходит (это и держат проверки 4/7 ниже) — смысл именно в значении
// ИЗ ПРОШЛОГО, заведённом до того, как рамка появилась или поднялась.
//
// Место в файле — здесь, а не позже: ставится ПОСЛЕ проверки чистой карточки
// (3), иначе подготовленная строка попала бы в неё и показала бы плашку там,
// где её быть не должно. И ДО проверок 4/7, потому что 7 сама заводит
// переопределение той же строкой (`hour_types.night.pay_percent`,
// `valid_from = 2026-09-01`) — два переопределения на одном пути и дате
// столкнулись бы об `rule_overrides_no_overlap`. Поэтому строка убирается
// сразу после использования, а не в конце файла через `onCleanup`.
sql(`
  insert into rule_overrides (tenant_id, scope_type, path, value, valid_from)
  select id, 'tenant', '${NIGHT}', '1.1'::jsonb, date '2026-09-01'
  from tenants where code = 'rs-dev'
`);
await visit(
  `${APP}/rules/${NIGHT}/?on=2026-09-01`,
  `document.body.innerText.includes("Действующее значение вне рамки страны.")`,
);
page = await text();
check(
  "плашка называет нарушение — «Действующее значение вне рамки страны»",
  page.includes("Действующее значение вне рамки страны."),
);
check("плашка называет его словом — «мягче рамки страны»", page.includes("мягче рамки страны"));
check("плашка называет границу — «не ниже 1.26»", page.includes("не ниже 1.26"));
await shot("frame-out-of-frame", "Действующее значение вне рамки страны.");
sql(`delete from rule_overrides where path = '${NIGHT}'`);

// Назад к чистой карточке — явным переходом, а не по памяти: следующие
// проверки (4, 6, 7) рассчитаны на страницу без строки, которую мы только что
// убрали, и без баннера «вне рамки», который эта строка рисовала.
await visit(
  `${APP}/rules/${NIGHT}/`,
  `document.body.innerText.includes("Кто меняет: Внутри рамки") &&
   !document.body.innerText.includes("Действующее значение вне рамки страны.")`,
);

// --- 4. Мягче страны — отказ словами --------------------------------------------

await fillFields({ valid_from: "2026-09-01", value: "1.10" });
await clickAndWait(
  "Завести версию",
  `document.body.innerText.includes("мягче правил страны")`,
);
page = await text();
check("отказ называет нарушение — «мягче правил страны»", page.includes("мягче правил страны"));
check("отказ называет границу числом — 1.26", page.includes("1.26"));
check("отказ называет источник — статью закона", page.includes("Закон о раду, чл. 108"));
check(
  "отказ советует выполнимое — «столько же или больше»",
  page.includes("столько же или больше"),
);
await shot("frame-refusal", "мягче правил страны");

// --- 5. И в базе ничего не появилось --------------------------------------------

let overrides = sql(`select count(*) from rule_overrides where path = '${NIGHT}'`);
check("отвергнутая правка не легла в rule_overrides", overrides === "0", overrides);

// --- 6. Попытка записана и видна -------------------------------------------------

// Кем подписана попытка — то же имя, что показывает шапка у вошедшего admin
// (created_by_name = who.display_name, см. rules_views.rule()). Читается с
// самой страницы, а не подставляется строкой: разъедется перевод роли —
// разъедется и ожидание, и это будет честный повод перечитать оба места.
const adminWho = await evalIn(
  `(document.querySelector(".who .role") || {}).textContent?.trim() || ""`,
);
check("шапка называет роль вошедшего admin", !!adminWho, adminWho);

// Перезагрузка той же страницы: предыдущая (только что провалившая форму)
// уже показывала журнал попыток в том же ответе, поэтому признаком СВЕЖЕЙ
// страницы служит не сам заголовок журнала, а исчезновение баннера отказа
// («Не сохранено.» рисуется только у `{% if error %}`, а обычный GET его не
// несёт) вместе с журналом.
await visit(
  `${APP}/rules/${NIGHT}/`,
  `!document.body.innerText.includes("Не сохранено.") &&
   document.body.innerText.includes("Попытки выйти за рамку")`,
);
page = await text();
check("после перезагрузки виден заголовок «Попытки выйти за рамку»", page.includes("Попытки выйти за рамку"));
check("в журнале — отвергнутое значение 1.1", page.includes("1.1"));
check("в журнале назван автор попытки", adminWho.length > 0 && page.includes(adminWho), adminWho);
await shot("frame-attempts", "Попытки выйти за рамку");

// --- 7. Строже страны — проходит -------------------------------------------------

await fillFields({ valid_from: "2026-09-01", value: "1.40" });
// Успешная правка уводит редиректом на список: `_back_to_list` возвращает на
// `/rules/`, а у карточки правила такой метки (label поля даты) нет вовсе —
// надёжный признак того, что произошёл именно переход, а не остались на
// прежней странице с ошибкой.
await clickAndWait("Завести версию", `document.body.innerText.includes("Действующие на")`);
page = await text();
check("после строгой правки отказа на экране нет", !page.includes("мягче правил страны"));
overrides = sql(`select count(*) from rule_overrides where path = '${NIGHT}'`);
check("строгая правка легла в rule_overrides", overrides === "1", overrides);

// --- 8. У запертого правила формы нет вовсе -------------------------------------

await visit(
  `${APP}/rules/${GUARANTEE}/`,
  `document.body.innerText.includes("Это правило не переопределяется.")`,
);
page = await text();
check("у запертого правила нет кнопки «Завести версию»", !page.includes("Завести версию"));
check("вместо формы — «Это правило не переопределяется»", page.includes("Это правило не переопределяется."));
check("назван источник запрета", page.includes("Закон о раду, чл. 111"));
// Пустая история переопределений у запертого правила не должна звать к
// форме, которой на этой странице нет вовсе: прежний текст обещал «первую же
// версию ниже», хотя ставить её было нечем.
check(
  "пустая история не зовёт к форме, которой у запертого правила нет",
  !page.includes("Первая же версия ниже"),
);
await shot("frame-locked", "Это правило не переопределяется.");

// --- 9. Пусто — значит молчим ----------------------------------------------------

await visit(
  `${APP}/rules/${FREE_RULE}/`,
  `document.body.innerText.includes("Кто меняет: Решает партнёр")`,
);
page = await text();
check("у правила без рамки заголовка журнала попыток нет", !page.includes("Попытки выйти за рамку"));
check("«Кто меняет» говорит — «Решает партнёр»", page.includes("Кто меняет: Решает партнёр"));

// ==============================================================================
// Роль manager: права rules.manage нет
// ==============================================================================

await login("manager");
await visit(
  `${APP}/rules/`,
  `document.body.innerText.includes("Этот экран вам не открыт.")`,
);
page = await text();
check(
  "у роли без прав — отказ словами, а не пустая страница",
  page.includes("Этот экран вам не открыт.") && page.trim().length > 0,
);
check(
  "и ни одного правила не показано",
  await evalIn(`!document.querySelector("table")`),
);
await shot("frame-denied", "Этот экран вам не открыт.");

check("консоль браузера молчит", logs.length === 0, logs.slice(0, 3).join(" | "));
report();
