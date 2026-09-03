/*
 * Смоук блока «Точки, между которыми делятся затраты» (T210, D055).
 *
 * Зачем он нужен помимо `tests/test_employee_units_screen.py`. Тесты ходят
 * клиентом Django: они доказывают, что POST с нужными полями пишет нужные
 * строки. Чего они не видят — что человек эти поля вообще найдёт и что браузер
 * пошлёт именно их. У набора точек это не абстрактный риск: главный инвариант
 * блока — «галки приходят отмеченными по действующему набору», и держится он
 * поведением браузера, а не разметкой. Неотмеченная галка в разметке выглядит
 * почти так же, как отмеченная; отправленная форма при этом снимет человека с
 * точки, на которой он работает, и узнают об этом из P&L следующего месяца.
 *
 * Здесь ровно этот путь: вход паролем, настоящие клики по галкам, обычный POST
 * — и после него проверяется то, что видит человек: плашка, история версий,
 * отказ формы.
 *
 * Стенд смоук приводит к сиду сам — и в начале, и после себя (договор в шапке
 * `cdp.mjs`). Известный дефект #206: `goto` считает страницу готовой по
 * `document.readyState` **прежней** страницы, поэтому после каждого перехода
 * ждётся текст, которого на прежней странице точно нет.
 *
 *     COMPOSE_PROJECT_NAME=dodo-pnl-hr2 APP=http://127.0.0.1:8120 CDP_PORT=9420 \
 *         SMOKE_SHOTS=/путь/к/снимкам node tools/smoke_employee_units.mjs
 */
import { mkdirSync, writeFileSync } from "node:fs";

import { attach, loginWith, onCleanup, sql, standFromSeed } from "./cdp.mjs";

const APP = process.env.APP || "http://127.0.0.1:8120";
const SHOTS = process.env.SMOKE_SHOTS || "/tmp";
mkdirSync(SHOTS, { recursive: true });

const { evalIn, goto, send, check, report, logs } = await attach();
const login = loginWith(APP, evalIn, goto);

standFromSeed();

// Привязки к точкам сид не трогает — их в нём нет вовсе, — поэтому за собой
// смоук убирает их сам. Оставленная строка меняет разнесение ФОТ у всех, кто
// потом откроет этот стенд, и выглядело бы это как поехавший расчёт, а не как
// чужой мусор.
onCleanup("привязки к точкам убраны", () => sql("delete from employee_units"));
sql("delete from employee_units");

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/*
 * Снимок экрана — доказательство глазами, а не только разметкой.
 *
 * Перед съёмкой блок подводится под верх окна. Без этого снимок ловил бы шапку
 * карточки: блок точек стоит внизу длинной страницы, и «снимок сделан» означало
 * бы картинку, на которой проверяемого нет вовсе. Ровно тот класс молчаливого
 * зелёного, от которого этот смоук и заводился.
 */
async function shot(name, anchor = "делятся затраты") {
  await evalIn(`
    (() => {
      const head = [...document.querySelectorAll("h1, h2, .alert, .ok")]
        .find((n) => n.textContent.includes(${JSON.stringify(anchor)}));
      (head || document.body).scrollIntoView({ block: "start" });
      window.scrollBy(0, -24);
      return !!head;
    })()
  `);
  await sleep(200);
  const { data } = await send("Page.captureScreenshot", { format: "png" });
  const path = `${SHOTS}/${name}.png`;
  writeFileSync(path, Buffer.from(data, "base64"));
  console.log(`     снимок: ${path}`);
}

/** Вход, доведённый до конца: шапка называет роль не сразу после `seed_dev`. */
async function signIn(who) {
  for (let attempt = 0; attempt < 10; attempt++) {
    await login(who);
    if (await evalIn(`!!document.querySelector(".who")`)) return;
    await sleep(500);
  }
  throw new Error(`не удалось войти: ${who}`);
}

/** Переход, который ждёт именно НОВУЮ страницу (issue #206). */
async function visit(url, ready, seconds = 20) {
  await goto(url);
  for (let i = 0; i < seconds * 4; i++) {
    if (await evalIn(ready).catch(() => false)) return;
    await sleep(250);
  }
  throw new Error(`не дождались страницы: ${url}`);
}

const text = () => evalIn(`document.body.innerText`);

/** Человек, чью карточку смотрим: первый по внешнему ключу, как в тестах. */
const PERSON = sql(
  "select id from employees order by external_id limit 1",
);
const CARD = `${APP}/directory/employees/${PERSON}/`;
const HAS_BLOCK = `document.body.innerText.includes("между которыми делятся затраты")`;

/** Отметить точку по её коду и, если надо, вписать долю. */
async function pick(code, share = "") {
  await evalIn(`
    (() => {
      const label = [...document.querySelectorAll("label")]
        .find((l) => l.textContent.trim().startsWith(${JSON.stringify(code + " ·")}));
      const box = document.getElementById(label.getAttribute("for"));
      if (!box.checked) box.click();
      const field = document.getElementById("share_" + box.value);
      field.value = ${JSON.stringify(share)};
      return box.value;
    })()
  `);
}

async function unpickAll() {
  await evalIn(`
    document.querySelectorAll('input[name="units"]:checked').forEach((b) => b.click())
  `);
}

async function submitUnits(from) {
  await evalIn(`
    (() => {
      const form = [...document.querySelectorAll("form")]
        .find((f) => f.querySelector('[name=what][value=units]'));
      form.querySelector('[name=units_from]').value = ${JSON.stringify(from)};
      form.submit();
    })()
  `);
  await sleep(1500);
}

// --- 1. Блок виден и объясняет пустоту ---------------------------------------

await signIn("admin");
await visit(CARD, HAS_BLOCK);

let page = await text();
check(
  "блок точек есть в карточке сотрудника",
  page.includes("Точки, между которыми делятся затраты"),
);
check(
  "пустое состояние называет точку из условий найма, а не молчит",
  page.includes("Точки не заданы") && page.includes("условиях найма"),
  page.split("Точки не заданы")[1]?.slice(0, 120)?.replace(/\n/g, " "),
);
check(
  "форма набора предлагает все точки партнёра",
  await evalIn(`document.querySelectorAll('input[name="units"]').length`) === 3,
);
check(
  "ни одна точка не отмечена за человека",
  await evalIn(`document.querySelectorAll('input[name="units"]:checked').length`) === 0,
);
await shot("units-01-empty");

// --- 2. Управляющий на две пиццерии заводится формой --------------------------

await pick("BG1");
await pick("NS1");
await submitUnits("2026-07-01");
page = await text();
check(
  "после сохранения продукт говорит, что случится с деньгами",
  page.includes("Точки человека заведены") && page.includes("разделятся"),
  page.split("\n").find((l) => l.includes("Точки человека заведены"))?.slice(0, 140),
);
const saved = sql(
  `select string_agg(u.code, ',' order by u.code) from employee_units eu
     join units u on u.id = eu.unit_id where eu.employee_id = '${PERSON}'
    and eu.valid_to is null`,
);
check("в базе ровно те две точки, что отмечены", saved === "BG1,NS1", saved);
check(
  "история показана словами и датами",
  (await text()).includes("01.07.2026"),
);
await shot("units-02-two-pizzerias");

// --- 3. Галки приходят отмеченными — форма не снимает того, о чём не просили ---

await visit(CARD, HAS_BLOCK);
const checkedCodes = await evalIn(`
  [...document.querySelectorAll('input[name="units"]:checked')]
    .map((b) => document.querySelector('label[for="' + b.id + '"]').textContent.trim().split(" ")[0])
    .sort().join(",")
`);
check(
  "действующий набор пришёл в форму отмеченным (иначе правка снимет точку молча)",
  checkedCodes === "BG1,NS1",
  checkedCodes,
);

// --- 4. Половина долей заданных — отказ словами, а не молчаливая арифметика ----

await pick("BG1", "0,7");
await submitUnits("2026-08-01");
page = await text();
check(
  "половина долей отвергнута словами",
  page.includes("либо у всех выбранных точек") && page.includes("Не сохранено"),
  page.split("\n").find((l) => l.includes("либо у всех"))?.slice(0, 160),
);
const untouched = sql(
  `select count(*) from employee_units where employee_id = '${PERSON}'`,
);
check("отвергнутая форма ничего не записала", untouched === "2", untouched);
await shot("units-03-refusal", "Не сохранено");

// --- 5. Перевод закрывает прежний набор, а не переписывает его -----------------

await visit(CARD, HAS_BLOCK);
await unpickAll();
await pick("NS2");
await submitUnits("2026-09-01");
const history = sql(
  `select string_agg(u.code || ':' || eu.valid_from || '..' || coalesce(eu.valid_to::text, '—'), ' | '
          order by eu.valid_from, u.code)
     from employee_units eu join units u on u.id = eu.unit_id
    where eu.employee_id = '${PERSON}'`,
);
check(
  "перевод закрыл прежние точки датой и оставил их в истории",
  history === "BG1:2026-07-01..2026-09-01 | NS1:2026-07-01..2026-09-01 | NS2:2026-09-01..—",
  history,
);
page = await text();
check(
  "история версий видна человеком, а не только в базе",
  page.includes("01.09.2026") && page.includes("BG1") && page.includes("NS2"),
);
await shot("units-04-history");

// --- 6. Читатель: таблица есть, формы нет, чужих точек не видно ----------------

// Человек управляющего — заведомо ДРУГОЙ, а не тот, чью карточку правили выше:
// его привязки уже заняты историей перевода, и вторая версия той же точки
// упёрлась бы в непересечение — то есть смоук покраснел бы на своей подготовке.
const NS1_PERSON = sql(
  `select e.id from employees e
     join employment_terms t on t.employee_id = e.id
     join units u on u.id = t.unit_id
    where u.code = 'NS1' and e.id <> '${PERSON}'
    order by e.external_id limit 1`,
);
sql(
  `insert into employee_units (tenant_id, employee_id, unit_id, valid_from)
   select e.tenant_id, e.id, u.id, '2020-01-01' from employees e, units u
    where e.id = '${NS1_PERSON}' and u.code in ('NS1', 'BG1')`,
);

await signIn("manager");
await visit(
  `${APP}/directory/employees/${NS1_PERSON}/`,
  HAS_BLOCK,
);
page = await text();
check("управляющий видит блок точек своего человека", page.includes("делятся затраты"));
check(
  "формы записи у читателя нет",
  await evalIn(`!document.querySelector('[name=what][value=units]')`),
);
const seenByBoss = await evalIn(`
  [...document.querySelectorAll('tr[data-unit]')].map((r) => r.dataset.unit).sort().join(",")
`);
check(
  "управляющий не узнал о точке, которой ему не видно (D023)",
  seenByBoss === "NS1",
  seenByBoss,
);
await shot("units-05-manager-reads");

check("консоль браузера молчит", logs.length === 0, logs.slice(0, 3).join(" | "));
report();
