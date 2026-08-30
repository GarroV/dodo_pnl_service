/*
 * Приводит стенд в состояние, которое показывает гайд первого месяца.
 *
 * Зачем это отдельным шагом, а не «сид уже всё содержит». Сид даёт движок и
 * тридцать сотрудников, но не даёт того, что человек видит на экранах гайда:
 * посчитанный и утверждённый июнь, наличные расходы, счета поставщиков,
 * заведённую следующую точку месяца. Без этого половина снимков гайда вышла
 * бы пустыми экранами — и заметил бы это только человек, которому гайд уже
 * показали (см. заголовок задачи в журнале блока `guide`).
 *
 * Работает ТОЛЬКО через формы продукта — как человек, а не через ORM: тогда
 * прогон заодно доказывает, что путь по продукту вообще проходим. Каждый шаг
 * идемпотентен — второй прогон подряд ничего не удваивает и не падает:
 * существование проверяется по видимому признаку на экране (код статьи,
 * название контрагента, номер счёта), а не по счётчику строк.
 *
 *     docker compose exec -T app python manage.py seed_dev   # стенд с нуля
 *     APP=http://127.0.0.1:8090 CDP_PORT=9354 \
 *         USER_NAME=admin USER_PASS=dodo-dev node tools/guide_prepare.mjs
 *
 * Роль всюду одна — «admin» (администратор сети): с D052 у неё есть весь цикл
 * месяца и ведение справочников разом, второй роли для подготовки не нужно.
 */
import { evalIn, goto, login, text } from "./guide_browser.mjs";

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// Месяц, который сид оставляет посчитываемым, и месяц сразу после него — тем
// же правилом, что и в задаче: сид сегодня останавливается на июне 2026,
// следующий месяц гайд показывает как «только что заведённый».
const CALCULATED_MONTH = { slug: "2026-06", title: "Июнь 2026" };
const OPEN_MONTH = { slug: "2026-07", title: "Июль 2026" };

function todayISO() {
  const d = new Date();
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

// --- мелкие примитивы поверх guide_browser -----------------------------------

/** Есть ли строка с этим текстом на списочной странице — признак «уже заведено». */
async function includesOnPage(path, needle) {
  await goto(path);
  return (await text()).includes(needle);
}

/**
 * Заполнить форму по имени поля. `{ text }` вместо значения — выбор варианта
 * списка по видимому тексту: списки справочников и счёта отдают в разметку
 * UUID, и хардкодить его значило бы завести собственную, вечно отстающую
 * копию базы прямо в скрипте съёмки.
 */
async function fillForm(selector, entries) {
  const error = await evalIn(`
    (() => {
      const form = document.querySelector(${JSON.stringify(selector)});
      if (!form) return "нет формы " + ${JSON.stringify(selector)};
      const entries = ${JSON.stringify(entries)};
      for (const [name, spec] of entries) {
        const field = form.querySelector('[name="' + name + '"]');
        if (!field) return "нет поля " + name;
        if (spec && typeof spec === "object" && "text" in spec) {
          const opt = [...field.options].find(o => o.textContent.includes(spec.text));
          if (!opt) return "нет варианта «" + spec.text + "» в поле " + name;
          field.value = opt.value;
        } else {
          field.value = spec;
        }
      }
      return "";
    })()
  `);
  if (error) throw new Error(error);
}

async function waitLoad() {
  for (let i = 0; i < 40; i++) {
    await sleep(150);
    if (await evalIn("document.readyState === 'complete'").catch(() => false)) {
      await sleep(300);
      return;
    }
  }
  throw new Error("страница не загрузилась после отправки формы");
}

async function submitForm(selector) {
  await evalIn(`document.querySelector(${JSON.stringify(selector)}).submit()`);
  await waitLoad();
}

/** Отказ формы виден плашкой `.alert` независимо от текста и языка — по ней и ловим. */
async function assertNoAlert() {
  if (await evalIn(`!!document.querySelector(".alert")`)) {
    throw new Error("форма отказала: " + (await text()).slice(0, 300).replace(/\n/g, " "));
  }
}

/** Строка периода на `/periods/` несёт свой id атрибутом — искать проще, чем ссылку. */
async function findPeriodId(monthTitle) {
  await goto("/periods/");
  return await evalIn(`
    (() => {
      const row = [...document.querySelectorAll('tr[data-period]')]
        .find(tr => (tr.querySelector('a')?.textContent || '').trim() === ${JSON.stringify(monthTitle)});
      return row ? row.getAttribute('data-period') : '';
    })()
  `);
}

// --- 1. статья расходов --------------------------------------------------------

async function ensureExpenseItem() {
  const label = "1. статья расходов «supplies»";
  if (await includesOnPage("/directory/expense-items/", "supplies")) {
    console.log(`${label}: уже было`);
    return;
  }
  await goto("/directory/expense-items/new/");
  await fillForm("form.card.wide", [
    ["code", "supplies"],
    ["title_ru", "Хозрасходы"],
    ["title_en", "Supplies"],
    ["title_sr_latn", "Potrošni materijal"],
    ["pnl_item", { text: "Оплата поставщику" }],
    ["valid_from", "2026-01-01"],
    ["alloc_method", "even"],
  ]);
  await submitForm("form.card.wide");
  await assertNoAlert();
  console.log(`${label}: сделал`);
}

// --- 2. контрагенты --------------------------------------------------------

async function ensureCounterparty(title, taxNumber) {
  const label = `2. контрагент «${title}»`;
  if (await includesOnPage("/directory/counterparties/", title)) {
    console.log(`${label}: уже было`);
    return;
  }
  await goto("/directory/counterparties/new/");
  await fillForm("form.card.wide", [
    ["title", title],
    ["tax_number", taxNumber],
    ["valid_from", "2026-01-01"],
  ]);
  await submitForm("form.card.wide");
  await assertNoAlert();
  console.log(`${label}: сделал`);
}

// --- 3. касса --------------------------------------------------------------

async function ensureTill() {
  const label = "3. касса «BG1-CASH»";
  if (await includesOnPage("/directory/tills/", "BG1-CASH")) {
    console.log(`${label}: уже было`);
    return;
  }
  await goto("/directory/tills/new/");
  await fillForm("form.card.wide", [
    ["code", "BG1-CASH"],
    ["title", "Касса Beograd 1"],
    ["unit", { text: "BG1" }],
    ["ledger", "supplementary"],
  ]);
  await submitForm("form.card.wide");
  await assertNoAlert();
  console.log(`${label}: сделал`);
}

// --- 4. должность ------------------------------------------------------------

async function ensurePosition() {
  const label = "4. должность «cook»";
  if (await includesOnPage("/directory/positions/", "cook")) {
    console.log(`${label}: уже было`);
    return;
  }
  await goto("/directory/positions/new/");
  await fillForm("form.card.wide", [
    ["code", "cook"],
    ["title", "Повар"],
    ["group", { text: "Кухня и касса" }],
    ["contract_hours", "176"],
  ]);
  await submitForm("form.card.wide");
  await assertNoAlert();
  console.log(`${label}: сделал`);
}

// --- 5. календарь на следующий месяц ------------------------------------------

async function ensureCalendarMonth() {
  const label = `5. календарь на ${OPEN_MONTH.title.toLowerCase()}`;
  await goto("/directory/calendar/");
  const exists = await evalIn(
    `!!document.querySelector('a[href="/directory/calendar/${OPEN_MONTH.slug}/"]')`,
  );
  if (exists) {
    console.log(`${label}: уже было`);
    return;
  }
  await goto("/directory/calendar/new/");
  await fillForm("form.card.wide", [
    ["month", OPEN_MONTH.slug],
    ["norm_hours", "184"],
    ["working_days", "23"],
  ]);
  await submitForm("form.card.wide");
  await assertNoAlert();
  console.log(`${label}: сделал`);
}

// --- 6. открытый месяц без часов ----------------------------------------------

async function ensureOpenPeriod() {
  const label = `6. открытый месяц без часов (${OPEN_MONTH.title.toLowerCase()})`;
  if (await findPeriodId(OPEN_MONTH.title)) {
    console.log(`${label}: уже было`);
    return;
  }
  await goto("/periods/");
  await fillForm('form[action="/periods/open/"]', [["month", OPEN_MONTH.slug]]);
  await submitForm('form[action="/periods/open/"]');
  await assertNoAlert();
  if (!(await findPeriodId(OPEN_MONTH.title))) {
    throw new Error(`месяц «${OPEN_MONTH.title}» не появился в /periods/ после заведения`);
  }
  console.log(`${label}: сделал`);
}

// --- 7. посчитанный и утверждённый месяц --------------------------------------

async function ensureCalculatedAndApproved() {
  const label = `7. посчитанный и утверждённый месяц (${CALCULATED_MONTH.title.toLowerCase()})`;
  const periodId = await findPeriodId(CALCULATED_MONTH.title);
  if (!periodId) {
    throw new Error(
      `период «${CALCULATED_MONTH.title}» не найден в /periods/ — его заводит seed_dev`,
    );
  }

  await goto(`/periods/${periodId}/`);
  if ((await text()).includes("Утверждён")) {
    console.log(`${label}: уже было`);
    return periodId;
  }

  const calculated = await evalIn(`!!document.querySelector("table.sheet")`);
  if (!calculated) {
    await submitForm(`form[action="/periods/${periodId}/calculate/"]`);
    let done = false;
    for (let i = 0; i < 90 && !done; i++) {
      await goto(`/periods/${periodId}/`);
      const body = await text();
      done = !body.includes("Идёт расчёт") && await evalIn(`!!document.querySelector("table.sheet")`);
      if (!done) await sleep(1000);
    }
    if (!done) {
      throw new Error(
        `${CALCULATED_MONTH.title} не посчитался за 90 секунд — запущен ли рабочий процесс очереди?`,
      );
    }
    await assertNoAlert();
  }

  await submitForm(`form[action="/periods/${periodId}/approve/"]`);
  await assertNoAlert();
  if (!(await text()).includes("Утверждён")) {
    throw new Error(`${CALCULATED_MONTH.title}: после утверждения статус «Утверждён» не появился`);
  }
  console.log(`${label}: сделал`);
  return periodId;
}

// --- 8. наличные расходы -----------------------------------------------------

async function ensureCashExpense({ amount, note, unitText, tillText }) {
  const label = `8. наличный расход «${note}»`;
  if (await includesOnPage("/expenses/", note)) {
    console.log(`${label}: уже было`);
    return;
  }
  await goto("/expenses/new/");
  const fields = [
    ["date", todayISO()],
    ["amount", amount],
    ["vat_rate", "20"],
    ["item", { text: "Хозрасходы" }],
    ["unit", { text: unitText }],
    ["note", note],
  ];
  // Поле кассы есть в разметке, только если хоть одна касса заведена (T145).
  // Заполняем его лишь тогда, когда есть подходящий вариант — иначе выбор
  // кассы другой точки молча отвергнет весь расход (несовпадение точки и кассы).
  if (tillText) {
    const hasMatchingTill = await evalIn(`
      (() => {
        const field = document.querySelector('form.card.wide [name="till"]');
        if (!field) return false;
        return [...field.options].some(o => o.textContent.includes(${JSON.stringify(tillText)}));
      })()
    `);
    if (hasMatchingTill) fields.push(["till", { text: tillText }]);
  }
  await fillForm("form.card.wide", fields);
  await submitForm("form.card.wide");
  await assertNoAlert();
  console.log(`${label}: сделал`);
}

// --- 9 и 10. счета поставщиков --------------------------------------------------

async function ensureInvoice({ number, counterpartyText, amount, unitText, itemText, note }) {
  const label = `счёт №${number}`;
  if (await includesOnPage("/invoices/", number)) {
    console.log(`${label}: уже было`);
    return;
  }
  await goto("/invoices/new/");
  const fields = [
    ["date", todayISO()],
    ["number", number],
    ["counterparty", { text: counterpartyText }],
    ["amount", amount],
    ["vat_rate", "20"],
    ["unit", { text: unitText }],
    ["note", note || ""],
  ];
  // Статья не выбирается намеренно там, где счёт должен остаться в инбоксе
  // «пока не разобрано» (itemText не передан вызывающим).
  if (itemText) fields.push(["item", { text: itemText }]);
  await fillForm("form.card.wide", fields);
  await submitForm("form.card.wide");
  await assertNoAlert();
  console.log(`${label}: сделал`);
}

// --- сборка ------------------------------------------------------------------

export async function prepare() {
  await login();

  await ensureExpenseItem();
  await ensureCounterparty("Delta Agrar d.o.o.", "101234567");
  await ensureCounterparty("Voda Voda d.o.o.", "102345678");
  await ensureTill();
  await ensurePosition();
  await ensureCalendarMonth();
  await ensureOpenPeriod();
  await ensureCalculatedAndApproved();

  await ensureCashExpense({
    amount: "4500", note: "Моющие средства", unitText: "BG1", tillText: "BG1-CASH",
  });
  await ensureCashExpense({
    amount: "2900", note: "Лампы в зал", unitText: "NS1",
  });

  await ensureInvoice({
    number: "2026-0812", counterpartyText: "Delta Agrar d.o.o.", amount: "86400",
    unitText: "Вся сеть", note: "Сыр и тесто, поставка за неделю",
  });
  await ensureInvoice({
    number: "2026-0815", counterpartyText: "Voda Voda d.o.o.", amount: "12400",
    unitText: "BG1", itemText: "Хозрасходы",
  });
}

// Запускается и сам: `node tools/guide_prepare.mjs`.
const { pathToFileURL } = await import("node:url");
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  try {
    await prepare();
    process.exit(0);
  } catch (error) {
    console.error("подготовка стенда упала:", error.message);
    process.exit(1);
  }
}
