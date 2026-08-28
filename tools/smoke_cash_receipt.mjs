/*
 * Смоук контроля кассы и чека к расходу (T184, T191).
 *
 * Что здесь доказывается такого, чего не докажут тесты. Тесты ходят клиентом
 * Django: они видят код ответа и разметку. Здесь всё идёт настоящим браузером —
 * с настоящей загрузкой файла через `DataTransfer`, с настоящей отправкой формы
 * `multipart/form-data` и с настоящей отрисовкой картинки чека. Ровно эта
 * половина и ломается тише всего: форма без `enctype` присылает вместо файла
 * одно его имя, и продукт честно отвечает «файл не приложен» — а тест,
 * подставляющий файл в POST мимо браузера, этого не увидит никогда.
 *
 * Стенд смоук готовит и убирает сам: заводит статью, вносит два расхода
 * (обычный и перевод) и сносит их за собой — включая случай падения на
 * полпути.
 *
 *     APP=http://127.0.0.1:8060 CDP_PORT=9350 node tools/smoke_cash_receipt.mjs
 */
import { execFileSync } from "node:child_process";
import { attach, loginWith, onCleanup } from "./cdp.mjs";

const APP = process.env.APP || "http://127.0.0.1:8060";
const ROOT = new URL("..", import.meta.url).pathname;

const { evalIn, goto, check, report, logs } = await attach();
const login = loginWith(APP, evalIn, goto);

/** Запрос к базе стенда — только для подготовки и уборки, не для проверок.
 *
 * Берётся ПЕРВАЯ строка вывода, а не весь он: у `insert ... returning id`
 * psql печатает следом свою метку `INSERT 0 1`, и склеенное значение уезжало в
 * следующий запрос как «uuid с хвостом». Куплено этим смоуком: подстановка
 * такого значения в форму молча не находила статью, и отказ приходил не там,
 * где была причина. */
function sql(statement) {
  const out = execFileSync(
    "docker", ["compose", "-p", "dodo-pnl-money", "exec", "-T", "db",
               "psql", "-U", "app", "-d", "dodo_pnl", "-tAc", statement],
    { cwd: ROOT, encoding: "utf8" },
  );
  return out.split("\n").map((line) => line.trim()).filter(Boolean)[0] || "";
}

function wipe() {
  sql(`delete from cash_receipts where entry_key in (
         select replace(dedup_key, 'manual:cash:', '') from facts
          where note like 'СМОУК%')`);
  sql("delete from facts where note like 'СМОУК%'");
  sql("delete from expense_items where code like 'smoke-t184%'");
}

wipe();
onCleanup("расходы и статьи смоука", wipe);

const tenant = sql("select id from tenants order by code limit 1");
const unit = sql(`select id from units where tenant_id = '${tenant}' order by code limit 1`);
const line = sql("select id from pnl_items where code = 'food_cost'");
const moved = sql("select id from pnl_items where kind = 'transfer' limit 1");

// Статья наличных и статья-перевод. Вторая нужна, чтобы разрыв был НЕ нулевым:
// смоук с нулевым разрывом зеленел бы и при неработающем правиле.
const plain = sql(`insert into expense_items
    (tenant_id, code, titles, pnl_item_id, valid_from, surfaces)
  values ('${tenant}', 'smoke-t184-plain',
          '{"ru":"Смоук: хозрасходы","en":"Smoke: supplies","sr-latn":"Smoke"}',
          '${line}', '2020-01-01', '{cash}') returning id`);
sql(`insert into expense_items
    (tenant_id, code, titles, pnl_item_id, valid_from, surfaces)
  values ('${tenant}', 'smoke-t184-move',
          '{"ru":"Смоук: инкассация","en":"Smoke: collection","sr-latn":"Smoke"}',
          '${moved}', '2020-01-01', '{cash}')`);

const today = new Date().toISOString().slice(0, 10);
const month = today.slice(0, 7);

await login("admin");

// --- 1. Расход вносится вместе с чеком, одной формой -------------------------

await goto(`${APP}/expenses/new/`);

// Файл кладётся в поле настоящим `DataTransfer` — так же, как его туда кладёт
// человек. Подмена `input.files` присваиванием невозможна: свойство только для
// чтения, и именно это отличает настоящую загрузку от имитации.
const PNG_BASE64 =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";

const filled = await evalIn(`
  (async () => {
    const form = document.querySelector('form.card');
    if (!form) return "формы нет";
    if ((form.getAttribute('enctype') || '') !== 'multipart/form-data') {
      return "у формы нет enctype — файл до сервера не доедет";
    }
    form.querySelector('[name=date]').value = ${JSON.stringify(today)};
    form.querySelector('[name=amount]').value = "1234";
    const item = form.querySelector('[name=item]');
    const pick = [...item.options].find(o => o.textContent.includes("хозрасходы"));
    if (!pick) return "статьи смоука нет в списке наличных: " +
      [...item.options].map(o => o.textContent.trim()).join(" | ");
    item.value = pick.value;
    form.querySelector('[name=unit]').value = ${JSON.stringify(unit)};
    form.querySelector('[name=note]').value = "СМОУК с чеком";

    const bytes = Uint8Array.from(atob(${JSON.stringify(PNG_BASE64)}), c => c.charCodeAt(0));
    const file = new File([bytes], "cek.png", { type: "image/png" });
    const box = new DataTransfer();
    box.items.add(file);
    const input = form.querySelector('[name=receipt]');
    if (!input) return "поля чека в форме нет";
    input.files = box.files;
    if (input.files.length !== 1) return "файл в поле не встал";
    form.submit();
    return "ok";
  })()
`);
check("форма внесения принимает файл", filled === "ok", filled);
await new Promise((r) => setTimeout(r, 1500));

// Отказ формы читается словами и сразу. Без этой проверки смоук шёл бы дальше
// по пустому реестру и краснел бы через три шага — на числе, а не на причине.
const refusal = await evalIn(
  `(() => { const a = document.querySelector('.alert'); return a ? a.textContent.trim() : ""; })()`,
);
check("расход с чеком записался без отказа", refusal === "", refusal);

// --- 2. Второй расход, перевод, без чека -------------------------------------

await goto(`${APP}/expenses/new/`);
const second = await evalIn(`
  (() => {
    const form = document.querySelector('form.card');
    form.querySelector('[name=date]').value = ${JSON.stringify(today)};
    form.querySelector('[name=amount]').value = "300";
    const item = form.querySelector('[name=item]');
    const move = [...item.options].find(o => o.textContent.includes("инкассация"));
    if (!move) return "статьи-перевода нет в списке наличных";
    item.value = move.value;
    form.querySelector('[name=unit]').value = ${JSON.stringify(unit)};
    form.querySelector('[name=note]').value = "СМОУК перевод";
    form.submit();
    return "ok";
  })()
`);
check("вторая запись — перевод — вносится", second === "ok", second);
await new Promise((r) => setTimeout(r, 1500));

// --- 3. Три числа на реестре -------------------------------------------------

await goto(`${APP}/expenses/?from=${month}-01&to=${month}-28`);
const numbers = await evalIn(`
  (() => {
    const at = (name) => {
      const node = document.querySelector('[data-' + name + ']');
      return node ? node.getAttribute('data-' + name) : null;
    };
    const card = document.querySelector('.control__card--gap');
    return JSON.stringify({
      cash: at('cash'), pnl: at('pnl'), gap: at('gap'), noreceipt: at('noreceipt'),
      gapNote: card ? card.querySelector('.note').textContent.trim() : "",
      cards: document.querySelectorAll('.control__card').length,
      wide: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    });
  })()
`);
const seen = JSON.parse(numbers);
check("на экране три карточки контроля", seen.cards === 3, numbers);
check("ушло из кассы больше принятого в P&L",
      Number(seen.cash) > Number(seen.pnl), numbers);
check("разрыв равен разнице",
      Math.abs(Number(seen.cash) - Number(seen.pnl) - Number(seen.gap)) < 0.005, numbers);
check("разрыв назвал причиной перевод",
      seen.gapNote.includes("перевод"), seen.gapNote);
check("сумма без чека — это перевод без чека, а не оба расхода",
      Number(seen.noreceipt) === 300, numbers);
check("страница по горизонтали не едет", seen.wide === false, numbers);

// Число ячеек в строке равно числу заголовков. Проверка выглядит мелочью, но
// куплена дефектом этой же задачи: колонка чека встала НА МЕСТО комментария,
// а не рядом, и таблица молча поехала на одну колонку — заголовки «Чек» и
// «Состояние» стояли над пустотой, а метка чека читалась как комментарий.
// Ни один тест разметки этого не увидел: ячейки были на месте, просто не те.
const grid = await evalIn(`
  (() => {
    const heads = document.querySelectorAll('table thead th').length;
    const cells = document.querySelectorAll('table tbody tr:first-child td').length;
    const foot = [...document.querySelectorAll('table tfoot tr:first-child > *')]
      .reduce((n, c) => n + (Number(c.getAttribute('colspan')) || 1), 0);
    return JSON.stringify({ heads, cells, foot });
  })()
`);
const shape = JSON.parse(grid);
check("в строке столько же ячеек, сколько заголовков",
      shape.heads === shape.cells, grid);
check("подвал закрывает всю ширину таблицы", shape.heads === shape.foot, grid);

// --- 4. Карточка расхода: чек виден картинкой --------------------------------

const factId = sql("select id from facts where note = 'СМОУК с чеком' and superseded_at is null limit 1");
await goto(`${APP}/expenses/${factId}/`);
const card = await evalIn(`
  (() => {
    const shot = document.querySelector('.receipt__shot');
    const mark = document.querySelector('.receipt .mark');
    return JSON.stringify({
      mark: mark ? mark.textContent.trim() : "",
      drawn: shot ? (shot.complete && shot.naturalWidth > 0) : false,
      src: shot ? shot.getAttribute('src') : "",
    });
  })()
`);
const shown = JSON.parse(card);
check("на карточке сказано, что чек приложен", shown.mark === "Чек приложен", card);
check("снимок чека реально нарисовался", shown.drawn === true, card);

// --- 5. Правка расхода не теряет чек -----------------------------------------

const edited = await evalIn(`
  (() => {
    const form = document.querySelector('form.card.wide');
    form.querySelector('[name=amount]').value = "1235";
    form.submit();
    return "ok";
  })()
`);
check("правка суммы отправлена", edited === "ok", edited);
await new Promise((r) => setTimeout(r, 1500));

const fresh = sql("select id from facts where note = 'СМОУК с чеком' and superseded_at is null limit 1");
check("правка завела новую версию факта", fresh !== factId, `${factId} → ${fresh}`);
await goto(`${APP}/expenses/${fresh}/`);
const afterEdit = await evalIn(
  `document.querySelector('.receipt .mark') ? document.querySelector('.receipt .mark').textContent.trim() : ""`,
);
check("чек пережил правку", afterEdit === "Чек приложен", afterEdit);

// --- 6. Где выбирается: короткий список статей -------------------------------

sql(`update expense_items set surfaces = '{invoice}' where id = '${plain}'`);
await goto(`${APP}/expenses/new/`);
const offered = await evalIn(`
  (() => {
    const item = document.querySelector('[name=item]');
    const titles = [...item.options].map(o => o.textContent.trim());
    return JSON.stringify(titles);
  })()
`);
check("статья, снятая с наличных, из формы внесения ушла",
      !JSON.parse(offered).some((t) => t.includes("хозрасходы")), offered);

await goto(`${APP}/expenses/${fresh}/`);
const kept = await evalIn(`
  (() => {
    const item = document.querySelector('[name=item]');
    return JSON.stringify([...item.options].map(o => o.textContent.trim()));
  })()
`);
check("но из формы ПРАВКИ уже записанного расхода — не ушла",
      JSON.parse(kept).some((t) => t.includes("хозрасходы")), kept);

// --- 7. Справочник статей: галки на месте ------------------------------------

await goto(`${APP}/directory/expense-items/${plain}/`);
const boxes = await evalIn(`
  (() => {
    const set = document.querySelector('fieldset.checks');
    if (!set) return JSON.stringify({ found: false });
    const on = [...set.querySelectorAll('input[type=checkbox]')]
      .filter(i => i.checked).map(i => i.value);
    return JSON.stringify({ found: true, on, legend: set.querySelector('legend').textContent.trim() });
  })()
`);
const marks = JSON.parse(boxes);
check("в карточке статьи есть «Где выбирается»", marks.found && marks.legend === "Где выбирается", boxes);
check("отмечено ровно то, что лежит в базе",
      JSON.stringify(marks.on) === JSON.stringify(["invoice"]), boxes);

report(logs);
