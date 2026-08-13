/*
 * Смоук списка расходов и разнесения расхода без точки (T110, T111).
 *
 * Что здесь проверяется такого, чего не докажут тесты. Тесты ходят клиентом
 * Django: они видят код ответа и разметку. Здесь всё идёт настоящими нажатиями
 * в живом браузере, тремя ролями подряд, и проверяется то, ради чего экран
 * вообще написан:
 *
 *   управляющий точки  — видит СВОИ расходы и итог, правит и удаляет их,
 *                        а расход на всю сеть разнести не может — и ему об
 *                        этом сказано словами;
 *   оперативный директор — видит расходы всех точек, разносит расход сети по
 *                        правилу статьи и получает «менять было нечего» на
 *                        повторном пересчёте;
 *   бухгалтер          — то же, что директор (D036).
 *
 * Отдельно проверяется главное правило денег: закрытый месяц не двигается ни
 * на копейку — ни правкой расхода, ни пересчётом разнесения.
 *
 * Стенд смоук приводит к сиду сам — и в начале, и после себя (договор в шапке
 * `cdp.mjs`). COMPOSE_PROJECT_NAME обязателен: без него сброс ушёл бы на чужой
 * стенд.
 *
 *     COMPOSE_PROJECT_NAME=dodo-pnl-cash2 APP=http://127.0.0.1:8084 CDP_PORT=9384 \
 *         node tools/smoke_expenses.mjs
 */
import { attach, loginWith, sql, standFromSeed } from "./cdp.mjs";

const APP = process.env.APP || "http://127.0.0.1:8084";

const { evalIn, goto, send, check, report, logs } = await attach();
const loginRaw = loginWith(APP, evalIn, goto);

standFromSeed();

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function login(who) {
  for (let attempt = 0; attempt < 10; attempt++) {
    await loginRaw(who);
    if (await evalIn(`!!document.querySelector(".who")`)) return true;
    await sleep(500);
  }
  return false;
}

async function logout() {
  await evalIn(`
    (() => {
      const form = document.querySelector('form[action="/logout/"]');
      if (form) form.submit();
    })()
  `);
  await sleep(1200);
}

/** Нажать кнопку по тексту — настоящей мышью, по её месту на экране. */
async function clickButton(text) {
  const box = await evalIn(`
    (() => {
      const b = [...document.querySelectorAll("button")]
        .find(x => x.textContent.includes(${JSON.stringify(text)}));
      if (!b) return null;
      b.scrollIntoView({ block: "center" });
      const r = b.getBoundingClientRect();
      return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
    })()
  `);
  if (!box) return false;
  for (const t of ["mousePressed", "mouseReleased"]) {
    await send("Input.dispatchMouseEvent", {
      type: t, x: box.x, y: box.y, button: "left", clickCount: 1,
    });
  }
  await sleep(1800);
  return true;
}

const fill = (name, value) => evalIn(`
  (() => {
    const el = document.querySelector('[name=${JSON.stringify(name)}]');
    if (!el) return false;
    el.value = ${JSON.stringify(value)};
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  })()
`);

const pick = (name, value) => evalIn(`
  (() => {
    const el = document.querySelector('[name=${JSON.stringify(name)}]');
    if (!el) return false;
    el.value = ${JSON.stringify(value)};
    return el.value === ${JSON.stringify(value)};
  })()
`);

const text = () => evalIn(`document.body.innerText`);

/** Показанные строки списка и его итог — так же, как их читает приёмка тестами. */
const shown = () => evalIn(`
  [...document.querySelectorAll("tr[data-amount]")].map(tr => ({
    amount: tr.dataset.amount, state: tr.dataset.state, fact: tr.dataset.fact,
  }))
`);
const total = () => evalIn(`
  (document.querySelector("[data-total]") || {}).dataset?.total || ""
`);

const juneTotal = () =>
  sql(`select coalesce(sum(amount), 0) from facts
        where period = '2026-06-01' and superseded_at is null`).trim();

const WIDE = "?from=2026-01-01&to=2026-12-31";

// --- администратор: статья и правило её разнесения ----------------------------

check("вход администратором сети", await login("admin"));

await goto(APP + "/directory/expense-items/new/");
for (const [name, value] of [
  ["code", "rent"],
  ["title_ru", "Аренда офиса"],
  ["title_en", "Office rent"],
  ["title_sr_latn", "Zakup kancelarije"],
  ["valid_from", "2026-01-01"],
]) {
  check(`поле «${name}» есть в форме статьи`, await fill(name, value));
}
await evalIn(`
  (() => {
    const select = document.querySelector('[name=pnl_item]');
    select.value = [...select.options].find(o => o.value).value;
  })()
`);
check("в карточке статьи есть правило разнесения",
  await evalIn(`!!document.querySelector('[name=alloc_method]')`));
check("правило «поровну между точками» выбирается", await pick("alloc_method", "even"));
await fill("alloc_from", "2026-01-01");
check("администратор сохраняет статью с правилом", await clickButton("Сохранить"));

check("правило легло в базу ключом статьи, а не контрагента",
  sql(`select method || '/' || (counterparty_id is null)::text from allocation_rules
        where expense_item_id is not null`).trim() === "even/true");

await goto(APP + "/directory/expense-items/");
const itemHref = await evalIn(`
  (() => {
    const link = [...document.querySelectorAll('a[href^="/directory/expense-items/"]')]
      .find(a => /[0-9a-f-]{20,}/.test(a.getAttribute("href")));
    return link ? link.getAttribute("href") : "";
  })()
`);
await goto(APP + itemHref);
check("карточка показывает версии правила",
  (await text()).includes("Версии правила"), await text());

await logout();

// --- управляющий: свой список, правка и удаление ------------------------------

check("вход управляющим точки", await login("manager"));

await goto(APP + "/expenses/new/");
await fill("date", "2026-08-05");
await fill("amount", "1200.50");
await evalIn(`
  (() => {
    const select = document.querySelector('[name=item]');
    select.value = [...select.options].find(o => o.value).value;
  })()
`);
await fill("note", "вода на точке");
check("управляющий записывает расход нажатием", await clickButton("Записать расход"));

await goto(APP + "/expenses/" + WIDE);
check("в шапке есть путь к расходам",
  await evalIn(`!!document.querySelector('nav a[href="/expenses/"]')`));
let rows = await shown();
check("свой расход виден в списке", rows.length === 1, JSON.stringify(rows));
check("итог равен сумме показанных строк", (await total()) === "1200.50", await total());

// Правка: сумма меняется заменой версии, действующей остаётся одна.
const factHref = await evalIn(`
  (() => {
    const link = document.querySelector('tr[data-amount] a[href^="/expenses/"]');
    return link ? link.getAttribute("href") : "";
  })()
`);
await goto(APP + factHref);
await fill("amount", "1300.00");
check("управляющий правит расход нажатием", await clickButton("Сохранить"));
check("правка прошла заменой версии, действующая одна",
  sql(`select count(*) from facts where dedup_key like 'manual:cash:%'
        and superseded_at is null`).trim() === "1");
check("в истории осталась прежняя версия",
  sql(`select count(*) from facts where dedup_key like 'manual:cash:%'`).trim() === "2");

// Расход на всю сеть: управляющий его вносит, но разнести не может — и об этом
// сказано словами, а не молчанием.
await goto(APP + "/expenses/new/");
await fill("date", "2026-08-06");
await fill("amount", "100.01");
await evalIn(`
  (() => {
    const select = document.querySelector('[name=item]');
    select.value = [...select.options].find(o => o.value).value;
  })()
`);
check("управляющему предлагают вариант «вся сеть»", await pick("unit", "network"));
check("расход на сеть записан", await clickButton("Записать расход"));

const saidNetwork = await evalIn(`(document.querySelector(".ok") || {}).innerText || ""`);
check("продукт сказал, что разносит не он",
  saidNetwork.includes("ведёт все точки"), saidNetwork);
check("расход остался ждать разнесения",
  sql(`select allocation from facts where amount = 100.01
        and superseded_at is null`).trim() === "pending");
check("на чужие точки ничего не легло",
  sql(`select count(*) from facts where parent_fact_id is not null`).trim() === "0");

await goto(APP + "/expenses/unallocated/");
check("нераспределённое видно управляющему", (await text()).includes("100,01"), await text());

// Удаление: строка не исчезает, а выходит из итога.
//
// Ссылку берём заново со списка, а не ту, что открывали до правки: правка
// заменяет версию, и прежний адрес ведёт на заменённую строку — её продукт
// править и удалять не даёт намеренно (править надо ту, что действует).
await goto(APP + "/expenses/" + WIDE);
const liveHref = await evalIn(`
  (() => {
    const row = [...document.querySelectorAll('tr[data-amount]')]
      .find(tr => tr.dataset.state === "active" && tr.dataset.amount === "1300.00");
    const link = row && row.querySelector('a[href^="/expenses/"]');
    return link ? link.getAttribute("href") : "";
  })()
`);
check("на списке есть ссылка на действующую версию расхода", !!liveHref, liveHref);
await goto(APP + liveHref);
// Прежний адрес по-прежнему открывается, но правки на нём нет: заменённую
// версию правят не «где-то ещё», а той, что действует, — и сказано это словами,
// а не пропавшей кнопкой.
await goto(APP + factHref);
check("заменённую версию править не предлагают",
  (await text()).includes("заменена") &&
    !(await evalIn(`[...document.querySelectorAll("button")]
      .some(b => b.textContent.includes("Удалить расход"))`)),
  await text());

await goto(APP + liveHref);
check("управляющий удаляет расход нажатием", await clickButton("Удалить расход"));
await goto(APP + "/expenses/" + WIDE);
rows = await shown();
check("удалённая строка осталась видимой",
  rows.some((r) => r.state === "removed"), JSON.stringify(rows));
check("удалённое вышло из итога", (await total()) === "100.01", await total());

await logout();

// --- директор: чужие строки, фильтр и разнесение ------------------------------

check("вход оперативным директором", await login("director"));
await goto(APP + "/expenses/" + WIDE);
rows = await shown();
check("директор видит и расход сети, и удалённую строку управляющего",
  rows.length >= 2, JSON.stringify(rows));

// Фильтр по чужой точке, подставленный руками в адрес: пусто и без подсказок.
const alienUnit = sql(`select id from units where code = 'BG1'`).trim();
await goto(APP + "/expenses/" + WIDE + "&unit=" + alienUnit);
check("фильтр по точке без расходов даёт пустой список",
  (await shown()).length === 0);
check("итог пустого списка — ноль", (await total()) === "0", await total());

// Разнесение: расход сети расходится по правилу статьи до копейки.
await goto(APP + "/expenses/unallocated/");
check("директор разносит расходы нажатием", await clickButton("Разнести по правилам"));
check("сумма детей сошлась с родителем до копейки",
  sql(`select string_agg(f.amount::text, '/' order by u.code)
         from facts f join units u on u.id = f.unit_id
        where f.parent_fact_id is not null and f.superseded_at is null`).trim()
    === "33.34/33.33/33.34");
check("родитель вышел из счёта",
  sql(`select allocation from facts where amount = 100.01
        and superseded_at is null`).trim() === "split");
check("список нераспределённого опустел",
  (await text()).includes("Нераспределённого нет"), await text());

// Повторный пересчёт ничего не переписывает и говорит об этом.
const beforeSpread = sql(
  `select string_agg(revision::text, ',' order by id) from facts
    where superseded_at is null`).trim();
check("директор жмёт пересчёт второй раз", await clickButton("Разнести по правилам"));
const saidAgain = await evalIn(`(document.querySelector(".ok") || {}).innerText || ""`);
check("продукт сказал, что менять было нечего",
  saidAgain.includes("менять было нечего"), saidAgain);
check("ни одна строка не получила новой версии",
  sql(`select string_agg(revision::text, ',' order by id) from facts
        where superseded_at is null`).trim() === beforeSpread);

// --- закрытый месяц не двигается ----------------------------------------------

await goto(APP + "/expenses/new/");
await fill("date", "2026-06-15");
await fill("amount", "500.00");
await evalIn(`
  (() => {
    for (const name of ["item", "unit"]) {
      const select = document.querySelector('[name=' + name + ']');
      select.value = [...select.options].find(o => o.value).value;
    }
  })()
`);
check("директор вносит июньский расход", await clickButton("Записать расход"));

await goto(APP + "/periods/");
const periodHref = await evalIn(`
  (() => {
    const link = [...document.querySelectorAll('a[href^="/periods/"]')]
      .find(a => /^\\/periods\\/[0-9a-f-]{8,}\\/$/.test(a.getAttribute("href")));
    return link ? link.getAttribute("href") : "";
  })()
`);
await goto(APP + periodHref);
check("директор считает июнь нажатием", await clickButton("Посчитать период"));
check("директор утверждает июнь нажатием", await clickButton("Утвердить период"));
const closedTotal = juneTotal();
check("июнь закрыт",
  sql(`select status from periods where period = '2026-06-01'`).trim() === "closed");

await goto(APP + "/expenses/?from=2026-06-01&to=2026-06-30");
const juneHref = await evalIn(`
  (() => {
    const link = document.querySelector('tr[data-amount] a[href^="/expenses/"]');
    return link ? link.getAttribute("href") : "";
  })()
`);
await goto(APP + juneHref);
check("карточка предупреждает, что месяц закрыт",
  (await text()).includes("Июнь 2026"), await text());
await fill("amount", "600.00");
check("директор правит расход закрытого месяца", await clickButton("Сохранить"));
check("закрытый месяц не сдвинулся", juneTotal() === closedTotal,
  `было ${closedTotal}, стало ${juneTotal()}`);
check("правка легла сторно и новой строкой в текущем месяце",
  sql(`select string_agg(amount::text, '/' order by amount) from facts
        where dedup_key like '%#storno' or dedup_key like '%#fix'`).trim()
    === "-500.00/600.00");

await logout();

// --- бухгалтер: тот же доступ, что у директора (D036) --------------------------

check("вход бухгалтером", await login("accountant"));
await goto(APP + "/expenses/" + WIDE);
const seen = await shown();
check("бухгалтер видит все расходы сети", seen.length >= 3, JSON.stringify(seen));
check("итог бухгалтера — сумма показанных действующих строк", await evalIn(`
  (() => {
    const rows = [...document.querySelectorAll("tr[data-amount]")]
      .filter(tr => tr.dataset.state === "active")
      .reduce((sum, tr) => sum + Number(tr.dataset.amount), 0);
    const shownTotal = Number(document.querySelector("[data-total]").dataset.total);
    return Math.abs(rows - shownTotal) < 0.005;
  })()
`));

await logout();

check("в журнале консоли нет исключений",
  !logs.some((line) => String(line).startsWith("EXCEPTION")),
  JSON.stringify(logs).slice(0, 300));

report();
