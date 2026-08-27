/*
 * Смоук разбора инбокса пачкой (issue #173).
 *
 * Проверяет нажатиями то, чего не доказать разбором разметки и чего нет в
 * pytest: Shift со щелчком отмечает диапазон, счётчик считает отмеченное, а
 * кнопка «Присвоить статью отмеченным» уводит из инбокса ровно отмеченные
 * строки. Диапазон живёт в скрипте страницы — тест по HTML увидел бы его
 * разметку, но не поведение.
 *
 * Заодно проверяется подсказка: разобрал строку поставщика — следующая строка
 * того же поставщика приходит с предложенной статьёй, но НЕ разобранной сама.
 *
 * Стенд смоук приводит к сиду сам — и в начале, и после себя (договор в шапке
 * `cdp.mjs`). Материал (контрагент, статья, три строки без статьи) заводится
 * прямо в базе: экраны счетов проверяются своими смоуками, а здесь важен
 * инбокс, а не путь, которым строки в него попали.
 *
 *     COMPOSE_PROJECT_NAME=dodo-pnl-inbox APP=http://127.0.0.1:8096 CDP_PORT=9361 \
 *         node tools/smoke_inbox_batch.mjs
 */
import { attach, loginWith, sql, standFromSeed } from "./cdp.mjs";

const APP = process.env.APP || "http://127.0.0.1:8096";

const { evalIn, goto, clickOn, send, check, report, logs } = await attach();
const login = loginWith(APP, evalIn, goto);

standFromSeed();

const text = () => evalIn(`document.body.innerText`);
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

/*
 * Дождаться, пока страница станет той, которую спрашивают.
 *
 * `goto` считает страницу готовой по `readyState`, а он остаётся `complete` у
 * ПРЕЖНЕЙ страницы, пока новая не начала грузиться. Переход на тот же адрес
 * (`/inbox/` после `/inbox/?sorted=3`) из-за этого читается старым содержимым:
 * проверка краснеет на исправном продукте — ровно тот шум, ради которого
 * написан договор в шапке `cdp.mjs`. Поймано здесь фактически.
 */
const waitFor = async (expr, what, tries = 40) => {
  for (let i = 0; i < tries; i++) {
    if (await evalIn(expr).catch(() => false)) return true;
    await wait(150);
  }
  throw new Error(`не дождались: ${what}`);
};

// ── материал: один поставщик, одна статья, три строки без статьи ───────────
const tenant = sql("select id from tenants where code = 'rs-dev'");
sql(`
  insert into counterparties (tenant_id, title, valid_from)
  values ('${tenant}', 'Smoke Dobavljač', '2020-01-01')
  on conflict do nothing;
  insert into expense_items (tenant_id, code, titles, pnl_item_id, valid_from)
  select '${tenant}', 'smoke-batch',
         '{"ru": "Электричество смоук", "en": "Smoke electricity", "sr-latn": "Struja"}'::jsonb,
         id, '2020-01-01'
    from pnl_items where code = 'food_cost' limit 1
  on conflict do nothing;
  insert into facts (tenant_id, period, doc_date, ledger, amount, currency,
                     title, source, dedup_key, allocation, pnl_item_id, counterparty_id)
  select '${tenant}', date_trunc('month', current_date)::date, current_date,
         'official', -1000 - n, 'RSD', 'Смоук-строка ' || n, 'manual',
         'smoke:batch:' || n, 'pending',
         (select id from pnl_items where code = 'unclassified'),
         (select id from counterparties where tenant_id = '${tenant}' and title = 'Smoke Dobavljač')
    from generate_series(1, 3) as n;
`);

// ── 1. Инбокс показывает строки и панель пачки ─────────────────────────────
await login("accountant");
await goto(`${APP}/inbox/`);
await waitFor(`document.querySelectorAll('tr[data-fact]').length >= 3`, "строки инбокса");
const screen = await text();
check("инбокс открылся", screen.includes("Инбокс"));
check("панель пачки на месте", screen.includes("Присвоить статью отмеченным"));
check("подсказка объясняет, как отмечать", screen.includes("Shift со щелчком"));

const rows = await evalIn(`
  document.querySelectorAll('input[type=checkbox][name=facts]').length
`);
check("строки инбокса отмечаемы", rows >= 3, `галочек: ${rows}`);

// ── 2. Shift со щелчком отмечает диапазон ──────────────────────────────────
const box = (n) => `document.querySelectorAll('input[type=checkbox][name=facts]')[${n}]`;
await clickOn(box(0), "первая галочка");
await wait(200);

// Тот же щелчок, но с зажатым Shift: событие мыши шлётся с модификатором,
// поэтому проверяется настоящий обработчик страницы, а не вызов функции.
const at = await evalIn(`
  (() => {
    const el = ${box(2)};
    el.scrollIntoView({ block: "center" });
    const r = el.getBoundingClientRect();
    return { x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2) };
  })()
`);
for (const type of ["mousePressed", "mouseReleased"]) {
  await send("Input.dispatchMouseEvent", {
    type, x: at.x, y: at.y, button: "left", clickCount: 1, modifiers: 8,  // 8 — Shift
  });
}
await wait(300);

const picked = await evalIn(`
  [...document.querySelectorAll('input[type=checkbox][name=facts]')]
    .filter(b => b.checked).length
`);
check("Shift отметил диапазон, а не одну строку", picked === 3, `отмечено: ${picked}`);

const counter = await evalIn(`document.querySelector('p.note[data-selected]').textContent`);
check("счётчик назвал число отмеченных", counter.includes("3"), counter.trim().slice(0, 80));

// ── 3. Пачка разбирается одним действием ───────────────────────────────────
await evalIn(`
  (() => {
    const select = document.getElementById('batch-item');
    const option = [...select.options].find(o => o.textContent.includes('Электричество смоук'));
    select.value = option.value;
    return option.textContent;
  })()
`);
await clickOn(
  `[...document.querySelectorAll('#inbox-batch button')].find(b => b.type === 'submit')`,
  "кнопка «Присвоить статью отмеченным»",
);
await wait(1200);

const after = await text();
check("продукт сказал, сколько разобрал", after.includes("Разобрано строк: 3"));

const left = sql(`
  select count(*) from facts
   where dedup_key like 'smoke:batch:%' and superseded_at is null
     and expense_item_id is null
`);
check("в инбоксе не осталось строк пачки", left === "0", `осталось: ${left}`);

// ── 4. Решение запомнилось и предлагается, но не разбирает само ────────────
sql(`
  insert into facts (tenant_id, period, doc_date, ledger, amount, currency,
                     title, source, dedup_key, allocation, pnl_item_id, counterparty_id)
  select '${tenant}', date_trunc('month', current_date)::date, current_date,
         'official', -4000, 'RSD', 'Смоук-строка после памяти', 'manual',
         'smoke:batch:after', 'pending',
         (select id from pnl_items where code = 'unclassified'),
         (select id from counterparties where tenant_id = '${tenant}' and title = 'Smoke Dobavljač');
`);
await goto(`${APP}/inbox/`);
await waitFor(
  `[...document.querySelectorAll('tr[data-fact]')]
     .some(r => r.textContent.includes('после памяти'))`,
  "строка, заведённая после разбора",
);
// Проверки идут по DOM, а не по `innerText`: заголовки таблицы в него не
// попадают (проверено здесь же — колонка на экране была, а в тексте её не
// было), и проверка по тексту краснела на исправном продукте.
const heads = await evalIn(`
  [...document.querySelectorAll('table th')].map(h => h.textContent.trim()).join(' | ')
`);
check("колонка подсказки на месте", heads.includes("Похоже на"), heads.slice(0, 200));

// Подсказка ищется В СТРОКЕ, а не на странице: название статьи есть и в списке
// пакетной панели, и проверка по всей странице прошла бы, даже если бы
// подсказки не было вовсе.
const hint = await evalIn(`
  (() => {
    const row = [...document.querySelectorAll('tr[data-fact]')]
      .find(r => r.textContent.includes('после памяти'));
    const chip = row && row.querySelector('.chip');
    return chip ? chip.textContent.trim() : "";
  })()
`);
check("статья предложена в самой строке", hint === "Электричество смоук", hint || "подсказки нет");

const stillWaiting = sql(`
  select count(*) from facts
   where dedup_key = 'smoke:batch:after' and superseded_at is null
     and expense_item_id is null
`);
check("подсказка ничего не разобрала сама", stillWaiting === "1", `строк ждёт: ${stillWaiting}`);

report(logs);
