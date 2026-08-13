/*
 * Смоук справочника статей и внесения расхода из кассы (T108, T109).
 *
 * Что здесь проверяется такого, чего не докажут тесты. Тесты ходят клиентом
 * Django: они видят код ответа и разметку, но не видят, дошёл ли человек до
 * формы, что он на ней прочитал и чем ему ответили. Здесь всё идёт настоящими
 * нажатиями в живом браузере, тремя ролями подряд:
 *
 *   администратор сети — заводит статью на трёх языках;
 *   управляющий точки  — вносит расход, видит свою точку и не может её сменить;
 *   бухгалтер          — вносит по любой точке (D036).
 *
 * Отдельно проверяется главное правило задачи в его живом виде: расход,
 * датированный внутри утверждённого месяца, ложится в текущий и **говорит об
 * этом словами**, а закрытый месяц не двигается ни на копейку.
 *
 * Стенд смоук приводит к сиду сам — и в начале, и после себя (договор в шапке
 * `cdp.mjs`). Поэтому запускать его можно в любом порядке и в одиночку, а
 * COMPOSE_PROJECT_NAME обязателен: без него сброс ушёл бы на чужой стенд.
 *
 *     COMPOSE_PROJECT_NAME=dodo-pnl-cash APP=http://127.0.0.1:8083 CDP_PORT=9383 \
 *         node tools/smoke_cash.mjs
 */
import { attach, loginWith, sql, standFromSeed } from "./cdp.mjs";

const APP = process.env.APP || "http://127.0.0.1:8083";

const { evalIn, goto, send, check, report, logs } = await attach();
const loginRaw = loginWith(APP, evalIn, goto);

standFromSeed();

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Вход, доведённый до конца: без ожидания смоук читает страницу входа за продукт. */
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

/** Заполнить поле формы по имени. */
const fill = (name, value) => evalIn(`
  (() => {
    const el = document.querySelector('[name=${JSON.stringify(name)}]');
    if (!el) return false;
    el.value = ${JSON.stringify(value)};
    el.dispatchEvent(new Event("change", { bubbles: true }));
    return true;
  })()
`);

const text = () => evalIn(`document.body.innerText`);

// Сколько денег в закрытом месяце — считаем по самим фактам, а не по экрану:
// экран расходов появится только в T110, а двигаться закрытый месяц не должен
// уже сейчас.
const juneTotal = () =>
  sql(`select coalesce(sum(amount), 0) from facts
        where period = '2026-06-01' and superseded_at is null`).trim();

const cashRows = () =>
  sql(`select count(*) from facts where dedup_key like 'manual:cash:%'
        and superseded_at is null`).trim();

// --- администратор: завести статью расходов (T108) -----------------------------

check("вход администратором сети", await login("admin"));

await goto(APP + "/directory/");
check("раздел статей расходов есть в оглавлении справочников",
  (await text()).includes("Статьи расходов"));

await goto(APP + "/directory/expense-items/");
check("пустой справочник объясняет, почему он пуст",
  (await text()).includes("поставляется пустым намеренно"));

await goto(APP + "/directory/expense-items/new/");
for (const [name, value] of [
  ["code", "water"],
  ["title_ru", "Вода"],
  ["title_en", "Water"],
  ["title_sr_latn", "Voda"],
  ["valid_from", "2026-01-01"],
]) {
  check(`поле «${name}» есть в форме статьи`, await fill(name, value));
}
check("строка P&L выбирается из списка", await evalIn(`
  (() => {
    const select = document.querySelector('[name=pnl_item]');
    if (!select) return false;
    const option = [...select.options].find(o => o.value);
    if (!option) return false;
    select.value = option.value;
    return true;
  })()
`));
check("подытог в списке строк P&L не предлагается", !(await evalIn(`
  [...document.querySelectorAll('[name=pnl_item] option')]
    .some(o => o.textContent.includes("Результат"))
`)));

check("администратор сохраняет статью нажатием", await clickButton("Сохранить"));
check("статья появилась в списке", (await text()).includes("Вода"));
check("статья лежит в базе", sql(`select count(*) from expense_items`).trim() === "1");

await logout();

// --- управляющий: внести расход (T109) ----------------------------------------

check("вход управляющим точки", await login("manager"));
await goto(APP + "/expenses/new/");

// С T110 в шапке лежит путь к СПИСКУ расходов, а кнопка «Внести расход» — на
// самом списке. Проверка ходила по старому адресу и краснела на исправном
// продукте; проверяется то же самое — что до внесения можно дойти из шапки.
check("в шапке есть путь к расходам",
  await evalIn(`!!document.querySelector('nav a[href="/expenses/"]')`));
check("управляющему показана его точка", (await text()).includes("NS1"));
// С T111 у поля точки есть второй вариант — «вся сеть» (расход юрлица
// целиком), поэтому у управляющего это список, а не надпись. Чужих точек в
// нём по-прежнему нет, и это здесь и проверяется.
check("управляющему предлагают только его точку и всю сеть", await evalIn(`
  (() => {
    const options = [...document.querySelectorAll('[name=unit] option')]
      .map(o => o.textContent.trim());
    return options.length === 2 && options.includes("NS1") && !options.includes("BG1");
  })()
`));
check("внутренний регистр управляющему не предлагают", !(await evalIn(`
  [...document.querySelectorAll('[name=ledger] option')].some(o => o.value === "internal")
`)));

await fill("date", "2026-06-15");
await fill("amount", "1200.50");
await evalIn(`
  (() => {
    const select = document.querySelector('[name=item]');
    select.value = [...select.options].find(o => o.value).value;
  })()
`);
await fill("note", "вода на точке");
check("управляющий записывает расход нажатием", await clickButton("Записать расход"));

const said = await evalIn(`(document.querySelector(".ok") || {}).innerText || ""`);
check("продукт подтвердил запись словами", said.includes("Записано"), said);
check("подтверждение называет месяц учёта", said.includes("Июнь 2026"), said);
check("расход лёг на точку управляющего",
  sql(`select u.code from facts f join units u on u.id = f.unit_id
        where f.dedup_key like 'manual:cash:%' and f.superseded_at is null`).trim() === "NS1");
check("расход помечен наличными и ручным вводом",
  sql(`select channel || '/' || source from facts
        where dedup_key like 'manual:cash:%' and superseded_at is null`).trim()
    === "cash/manual");

// --- подмена точки в запросе --------------------------------------------------
// Именно подменой, а не глазами по форме: отвергать обязана база, а не экран
// (D014). Форму собираем и отправляем сами — так же, как это сделал бы тот,
// кто правит запрос в браузере.

const alienUnit = sql(`select id from units where code = 'BG1'`).trim();
const before = cashRows();
await goto(APP + "/expenses/new/");
await evalIn(`
  (() => {
    const form = document.querySelector('form.card');
    const unit = document.createElement("input");
    unit.name = "unit";
    unit.value = ${JSON.stringify(alienUnit)};
    form.appendChild(unit);
    form.querySelector('[name=date]').value = "2026-06-16";
    form.querySelector('[name=amount]').value = "5000";
    const item = form.querySelector('[name=item]');
    item.value = [...item.options].find(o => o.value).value;
    form.submit();
  })()
`);
await sleep(1500);
// Читаем саму плашку отказа, а не весь текст страницы: слова «закрыт» и
// «точка» стоят и в подсказках под полями, и проверка по всей странице была бы
// зелёной независимо от того, отказали человеку или нет.
const refusal = await evalIn(`(document.querySelector(".alert") || {}).innerText || ""`);
check("подмена точки отвергнута", refusal.includes("Не записано"), refusal);
check("отказ не рассказывает о чужой точке",
  refusal.includes("Точка не найдена") && !refusal.includes("BG1"), refusal);
check("чужой строки в базе не появилось", cashRows() === before);

await logout();

// --- бухгалтер: любая точка (D036) --------------------------------------------

check("вход бухгалтером", await login("accountant"));
await goto(APP + "/expenses/new/");
check("бухгалтеру точку предлагают выбором",
  await evalIn(`!!document.querySelector('select[name=unit]')`));

await fill("date", "2026-06-17");
await fill("amount", "300");
await evalIn(`
  (() => {
    for (const name of ["item", "unit"]) {
      const select = document.querySelector('[name=' + name + ']');
      select.value = [...select.options].find(o => o.value).value;
    }
    const unit = document.querySelector('[name=unit]');
    unit.value = ${JSON.stringify(alienUnit)};
  })()
`);
check("бухгалтер записывает расход по чужой для управляющего точке",
  await clickButton("Записать расход"));
check("бухгалтеру запись подтверждена", (await text()).includes("Записано"));
check("расход бухгалтера лёг на BG1",
  sql(`select count(*) from facts where unit_id = '${alienUnit}'
        and dedup_key like 'manual:cash:%' and superseded_at is null`).trim() === "1");

await logout();

// --- закрытый месяц не двигается ----------------------------------------------

check("вход оперативным директором", await login("director"));
await goto(APP + "/periods/");
// Именно ссылка на КОНКРЕТНЫЙ месяц, а не первая попавшаяся на «/periods/»:
// такая же ссылка стоит в шапке каждой страницы, и по ней смоук возвращался бы
// на список, не находил кнопки расчёта и объявлял это поломкой продукта.
const periodHref = await evalIn(`
  (() => {
    const link = [...document.querySelectorAll('a[href^="/periods/"]')]
      .find(a => /^\\/periods\\/[0-9a-f-]{8,}\\/$/.test(a.getAttribute("href")));
    return link ? link.getAttribute("href") : "";
  })()
`);
check("на списке месяцев есть ссылка на июнь", !!periodHref, periodHref);
await goto(APP + periodHref);
check("директор считает июнь нажатием", await clickButton("Посчитать период"));
check("директор утверждает июнь нажатием", await clickButton("Утвердить период"));
const closedTotal = juneTotal();
check("июнь закрыт", sql(
  `select status from periods where period = '2026-06-01'`).trim() === "closed");

await logout();
check("вход управляющим второй раз", await login("manager"));
await goto(APP + "/expenses/new/");
await fill("date", "2026-06-20");
await fill("amount", "777");
await evalIn(`
  (() => {
    const select = document.querySelector('[name=item]');
    select.value = [...select.options].find(o => o.value).value;
  })()
`);
check("расход за закрытый месяц принят", await clickButton("Записать расход"));

const saidWhere = await evalIn(`(document.querySelector(".ok") || {}).innerText || ""`);
check("продукт сказал, что месяц закрыт и куда лёг расход",
  saidWhere.includes("закрыт") && saidWhere.includes("Июнь 2026"), saidWhere);
check("закрытый месяц не сдвинулся", juneTotal() === closedTotal,
  `было ${closedTotal}, стало ${juneTotal()}`);
check("расход не потерялся: дата осталась июньской", sql(
  `select doc_date::text from facts where amount = 777 and superseded_at is null`
).trim() === "2026-06-20");
check("расход учтён не в июне", sql(
  `select (period <> '2026-06-01') from facts where amount = 777 and superseded_at is null`
).trim() === "t");

await logout();

check("в журнале консоли нет исключений",
  !logs.some((line) => String(line).startsWith("EXCEPTION")),
  JSON.stringify(logs).slice(0, 300));

report();
