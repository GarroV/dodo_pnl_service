/*
 * Смоук переноса точки в другое юрлицо (T189, issue #179).
 *
 * Миграции `0265`/`0266` и `tests/test_unit_legal_entity.py` проверяют триггеры
 * и стену закрытого месяца напрямую SQL — вставкой строк в `unit_legal_entities`
 * под ролью `app_user`. Это доказывает, что база устроена правильно, но не
 * доказывает, что человек, который просто выбрал в форме точки другое юрлицо и
 * нажал «Сохранить», получит тот же результат: правка идёт через Django-форму
 * (`item.legal_entity_id = entity_id; item.save()`), и именно UPDATE обычного
 * поля обязан запустить триггер `unit_entity_version` — с тем же успехом форма
 * могла бы обновлять только колонку `units.legal_entity_id` и не задевать
 * историю вовсе, и это было бы видно только на живом экране, а не в модели.
 *
 * Здесь ровно тот путь: вход паролем, обычный `<select>` в форме, обычный
 * POST — и после него проверяется то, что тест видит только с обратной
 * стороны — саму таблицу версий и колонку-снимок. Венчает смоук платёжная
 * ведомость за уже прошедший июнь: её шапку берёт `reports.printing._entity_of`
 * по дате периода, а не по «сейчас», и здесь это проверяется тем самым
 * документом, под которым бухгалтер расписывается, а не разбором функции.
 *
 * Стенд смоук приводит к сиду сам — и в начале, и после себя (договор в шапке
 * `cdp.mjs`). Известный дефект #206: `goto` харнесса иногда считает страницу
 * готовой по `document.readyState` **прежней** страницы — здесь после каждого
 * перехода дополнительно ждётся текст или элемент, которых на прежней странице
 * точно нет, а не только факт загрузки.
 *
 *     COMPOSE_PROJECT_NAME=dodo-pnl-roles4 APP=http://127.0.0.1:8090 CDP_PORT=9390 \
 *         SMOKE_SHOTS=/путь/к/снимкам node tools/smoke_unit_legal_entity.mjs
 */
import { mkdirSync, writeFileSync } from "node:fs";

import { attach, ensureCalculated, loginWith, sql, standFromSeed } from "./cdp.mjs";

const APP = process.env.APP || "http://127.0.0.1:8090";
const SHOTS = process.env.SMOKE_SHOTS || "/tmp";
mkdirSync(SHOTS, { recursive: true });

const { evalIn, goto, send, clickOn, check, report, logs } = await attach();
const login = loginWith(APP, evalIn, goto);

// Стенд к эталону сейчас и обратно к нему после — в том числе если смоук
// упадёт на полпути (issue #76).
standFromSeed();

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Снимок экрана — доказательство глазами, а не только разметкой. */
async function shot(name) {
  const { data } = await send("Page.captureScreenshot", { format: "png" });
  const path = `${SHOTS}/${name}.png`;
  writeFileSync(path, Buffer.from(data, "base64"));
  console.log(`     снимок: ${path}`);
}

/** Вход, доведённый до конца: `loginWith` шлёт форму, а шапка называет роль
 *  не сразу — особенно первым запросом сразу после `seed_dev`, который только
 *  что переписал тенант целиком. */
async function signIn(who) {
  for (let attempt = 0; attempt < 10; attempt++) {
    await login(who);
    if (await evalIn(`!!document.querySelector(".who")`)) return;
    await sleep(500);
  }
  throw new Error(`не удалось войти: ${who}`);
}

/** Переход, который ждёт именно НОВУЮ страницу (issue #206), а не готовность
 *  прежней: `ready` — JS-выражение, истинное только на месте назначения. */
async function visit(url, ready, seconds = 20) {
  await goto(url);
  for (let i = 0; i < seconds * 4; i++) {
    if (await evalIn(ready).catch(() => false)) return;
    await sleep(250);
  }
  throw new Error(`не дождались страницы: ${url}`);
}

/** Ждёт обычной (не htmx) навигации браузера после отправки формы. */
async function waitForPath(pathname, seconds = 20) {
  for (let i = 0; i < seconds * 4; i++) {
    if ((await evalIn("location.pathname")) === pathname) return true;
    await sleep(250);
  }
  return false;
}

/** Строка BG1 из таблицы точек: код, название, юрлицо, открыта, закрыта. */
const bg1Row = () => evalIn(`
  (() => {
    const row = [...document.querySelectorAll("tbody tr")]
      .find(r => r.querySelector("td")?.textContent.trim() === "BG1");
    if (!row) return null;
    return {
      cells: [...row.querySelectorAll("td")].map(td => td.textContent.trim()),
      href: row.querySelector("td a")?.getAttribute("href") || null,
    };
  })()
`);

const saveButton = `[...document.querySelectorAll("button")]
    .find(b => b.textContent.trim() === "Сохранить")`;

const NEW_ENTITY = "Dodo RS Novo d.o.o.";
const OLD_ENTITY = "Dodo RS d.o.o.";

// =============================================================================
// 1. Вход admin и экран точек
// =============================================================================

await signIn("admin");
await visit(
  `${APP}/directory/units/`,
  `document.querySelector("h1")?.textContent.trim() === "Точки"`,
);
check(
  "администратор открывает экран точек",
  (await evalIn(`document.querySelector("h1")?.textContent.trim()`)) === "Точки",
);
const before = await bg1Row();
check("BG1 есть в списке точек", !!before, JSON.stringify(before));
check(
  "до переноса BG1 стоит за прежним юрлицом",
  before?.cells[2] === OLD_ENTITY,
  before?.cells[2],
);

// =============================================================================
// 2. Заводится второе юрлицо
// =============================================================================

await visit(
  `${APP}/directory/legal-entities/new/`,
  `document.querySelector("h1")?.textContent.trim() === "Новое юрлицо"`,
);
await evalIn(`document.querySelector('[name=title]').value = ${JSON.stringify(NEW_ENTITY)}`);
await clickOn(saveButton, "кнопка «Сохранить» на форме юрлица");
check(
  "форма юрлица вернула на список юрлиц",
  await waitForPath("/directory/legal-entities/"),
);
const entityListed = await evalIn(`
  [...document.querySelectorAll("tbody td")]
    .some(td => td.textContent.trim() === ${JSON.stringify(NEW_ENTITY)})
`);
check("новое юрлицо появилось в списке юрлиц", entityListed);

// =============================================================================
// 3. Точка BG1 переносится в новое юрлицо формой правки
// =============================================================================

await visit(
  `${APP}/directory/units/`,
  `document.querySelector("h1")?.textContent.trim() === "Точки"`,
);
const listed = await bg1Row();
check("у BG1 есть ссылка на карточку точки", !!listed?.href, JSON.stringify(listed));

await visit(APP + listed.href, `!!document.querySelector("#legal_entity")`);
const newEntityId = await evalIn(`
  (() => {
    const opt = [...document.querySelectorAll("#legal_entity option")]
      .find(o => o.textContent.trim() === ${JSON.stringify(NEW_ENTITY)});
    return opt ? opt.value : null;
  })()
`);
check("в выпадающем списке точки видно новое юрлицо", !!newEntityId, newEntityId);

await evalIn(`document.querySelector("#legal_entity").value = ${JSON.stringify(newEntityId)}`);
const selectedTitle = await evalIn(`
  document.querySelector("#legal_entity").selectedOptions[0]?.textContent.trim()
`);
check("новое юрлицо выбрано в форме точки", selectedTitle === NEW_ENTITY, selectedTitle);
await shot("unit-edit-new-entity-selected");

await clickOn(saveButton, "кнопка «Сохранить» на форме точки");
check("форма точки вернула на список точек", await waitForPath("/directory/units/"));

const after = await bg1Row();
check(
  "список точек показывает у BG1 новое юрлицо",
  after?.cells[2] === NEW_ENTITY,
  after?.cells[2],
);
await shot("units-list-bg1-new-entity");

// =============================================================================
// 4. База завела версию, а не переписала историю
// =============================================================================

const today = sql("select current_date::text");
// Один запрос, а не два: порядок версий сравнивается тут же, `order by`, без
// риска молча сопоставить не ту строку не той при отдельных запросах.
const history = sql(
  "select string_agg(" +
    "le.title || '|' || uve.valid_from::text || '|' || coalesce(uve.valid_to::text, '')," +
    " ';' order by uve.valid_from)" +
  " from unit_legal_entities uve" +
  " join units u on u.id = uve.unit_id" +
  " join legal_entities le on le.id = uve.legal_entity_id" +
  " where u.code = 'BG1'",
);
const versions = history.split(";").map((row) => {
  const [title, validFrom, validTo] = row.split("|");
  return { title, validFrom, validTo };
});
check("у BG1 в базе стало две версии юрлица", versions.length === 2, history);
check(
  "прежняя версия — старое юрлицо с 2023-01-01 (дата открытия точки)",
  versions[0]?.title === OLD_ENTITY && versions[0]?.validFrom === "2023-01-01",
  JSON.stringify(versions[0]),
);
check(
  "прежняя версия закрыта сегодняшним днём, а не удалена и не переписана",
  versions[0]?.validTo === today,
  `${versions[0]?.validTo} вместо ${today}`,
);
check(
  "новая версия — новое юрлицо с сегодняшнего дня и открыта (valid_to пуст)",
  versions[1]?.title === NEW_ENTITY && versions[1]?.validFrom === today && versions[1]?.validTo === "",
  JSON.stringify(versions[1]),
);

// =============================================================================
// 5. Снимок «сейчас» переехал
// =============================================================================

const snapshot = sql(
  "select le.title from units u" +
  " join legal_entities le on le.id = u.legal_entity_id" +
  " where u.code = 'BG1'",
);
check(
  "units.legal_entity_id снимком указывает на новое юрлицо",
  snapshot === NEW_ENTITY,
  snapshot,
);

// =============================================================================
// 6. Июнь остался за прежним юрлицом
// =============================================================================

const periodHref = await ensureCalculated(APP, { evalIn, goto, clickOn });
await visit(APP + periodHref + "print/payout/", `!!document.querySelector(".doc-who")`);
const payoutHead = await evalIn(`document.querySelector(".doc-who")?.textContent || ""`);
const payoutBody = await evalIn(`document.body.innerText`);
check("шапка июньской ведомости называет прежнее юрлицо", payoutHead.includes(OLD_ENTITY), payoutHead);
check("нового юрлица на ведомости нет нигде", !payoutBody.includes(NEW_ENTITY));
await shot("payout-june-old-entity");

const noise = logs.filter((l) => /EXCEPTION|Uncaught/.test(l));
check("в консоли браузера нет исключений", noise.length === 0, noise.slice(0, 2).join(" | "));

report();
