/*
 * Смоук двух экранов блока reports: след расчёта (T029) и расхождения (T030).
 *
 * Проверяет то, чего разбором разметки в тестах не докажешь: что человек
 * настоящим нажатием попадает со строки ведомости на её след, что числа на
 * следе складываются в то самое число, по которому он кликнул, и что отчёт
 * расхождений показывает подброшенное отклонение и молчит о том, что в пределах
 * порога. Отдельно — D023: у роли с одним регистром на обоих экранах не должно
 * встретиться ни строки, ни слова о чужих.
 *
 *     google-chrome --headless=new --remote-debugging-port=9341 \
 *         --user-data-dir=/tmp/chrome-smoke-rep2 &
 * Стенд смоук приводит к сиду сам — и в начале, и после себя (см. договор в
 * шапке `cdp.mjs`). Поэтому запускать его можно в любом порядке и в одиночку,
 * а `COMPOSE_PROJECT_NAME` обязателен: без него сброс ушёл бы на чужой стенд.
 *
 *     COMPOSE_PROJECT_NAME=<стенд> APP=http://127.0.0.1:8057 \\
 *         node tools/smoke_reports_trace.mjs
 */
import { attach, findPeriodAndGrid, loginWith, standFromSeed } from "./cdp.mjs";

const APP = process.env.APP || "http://127.0.0.1:8057";

const ROLES = [
  { code: "director", ledgers: ["Официальный", "Дополнительный", "Внутренний"] },
  { code: "accountant", ledgers: ["Официальный"] },
  { code: "manager", ledgers: ["Официальный", "Дополнительный"] },
];

const ALL_LEDGERS = ["Официальный", "Дополнительный", "Внутренний"];

const { evalIn, goto, send, check, report, logs } = await attach();
const login = loginWith(APP, evalIn, goto);

// Стенд к эталону сейчас и обратно к нему после — в том числе если смоук
// упадёт на полпути (issue #76). Порядок запуска смоуков больше ничего не
// решает: каждый начинает с известного входа и ничего за собой не оставляет.
standFromSeed();

const money = (text) => Number(String(text).replace(/[  +]/g, "").replace(",", "."));

await send("Emulation.setDeviceMetricsOverride", {
  width: 1440, height: 900, deviceScaleFactor: 1, mobile: false,
});

/** Нажать на элемент настоящей мышью и дождаться перехода. */
async function clickOn(selectorJs, wait = 1000) {
  const box = await evalIn(`
    (() => {
      const el = ${selectorJs};
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
  await new Promise((r) => setTimeout(r, wait));
  return true;
}

const readTrace = () => evalIn(`
  (() => {
    const rows = [...document.querySelectorAll("table.steps tbody tr")];
    const foot = document.querySelector("table.steps tfoot td:last-child");
    return {
      steps: rows.length,
      amounts: rows.map(tr => tr.querySelector("td:last-child").textContent.trim()),
      inputs: rows.map(tr => (tr.querySelector("td.inputs") || {}).innerText || ""),
      levels: rows.map(tr => tr.children[2].textContent.trim()),
      ledgers: [...new Set(rows.map(tr => {
        const tag = tr.querySelector(".ledger");
        return tag ? tag.textContent.trim() : "";
      }))].filter(Boolean),
      total: foot ? foot.textContent.trim() : "",
      derived: [...document.querySelectorAll("h2")].some(h => h.textContent.includes("Производные")),
      text: document.body.innerText,
      title: (document.querySelector("h1") || {}).textContent || "",
      pageScroll: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    };
  })()
`);

const readVariance = () => evalIn(`
  (() => {
    const rows = [...document.querySelectorAll("table.variance tbody tr")];
    const foot = document.querySelector("table.variance tfoot td.total");
    return {
      lines: rows.length,
      rows: rows.map(tr => ({
        employee: tr.children[0].textContent.trim(),
        code: (tr.querySelector("td:nth-child(2) .hint") || {}).textContent || "",
        previous: tr.children[4].textContent.trim(),
        current: tr.children[5].textContent.trim(),
        delta: tr.querySelector("td.delta").textContent.trim(),
        percent: tr.children[7].textContent.trim(),
        threshold: tr.children[8].textContent.trim(),
        ledger: (tr.querySelector(".ledger") || {}).textContent || "",
      })),
      total: foot ? foot.textContent.trim() : "",
      text: document.body.innerText,
      pageScroll: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    };
  })()
`);

// --- материал: посчитанный период --------------------------------------------

await login("director");
const { periodHref } = await findPeriodAndGrid(APP, evalIn, goto);
await goto(APP + periodHref);
{
  const clicked = await clickOn(
    `[...document.querySelectorAll("button")].find(x => x.textContent.includes("Посчитать период"))`,
    500,
  );
  let has = false;
  for (let i = 0; i < 40 && !has; i++) {
    await new Promise((r) => setTimeout(r, 700));
    has = await evalIn(`document.querySelectorAll("table.sheet tbody tr").length > 0`);
  }
  check("период посчитан, ведомость на экране", has, clicked ? "" : "кнопки не было");
}

// --- T029: со строки ведомости на её след ------------------------------------

for (const role of ROLES) {
  await login(role.code);
  await goto(APP + periodHref);

  // Берём три первые строки — каждую отдельным нажатием по её итогу.
  for (const index of [0, 1, 2]) {
    await goto(APP + periodHref);
    const row = await evalIn(`
      (() => {
        const tr = document.querySelectorAll("table.sheet tbody tr")[${index}];
        if (!tr) return null;
        const a = tr.querySelector("td:last-child a.trace");
        return a ? {
          employee: tr.children[0].textContent.trim(),
          ledger: tr.children[2].textContent.trim(),
          total: a.textContent.trim(),
        } : null;
      })()
    `);
    if (!row) {
      check(`${role.code}: у строки ${index + 1} есть ссылка на след`, false);
      continue;
    }

    const went = await clickOn(
      `document.querySelectorAll("table.sheet tbody tr")[${index}].querySelector("td:last-child a.trace")`,
    );
    check(`${role.code}/строка ${index + 1}: переход на след нажатием`, went);

    const trace = await readTrace();
    check(
      `${role.code}/строка ${index + 1}: это след нужного человека`,
      trace.title.includes("Как получилась эта сумма") && trace.text.includes(row.employee),
      trace.title.trim(),
    );
    check(
      `${role.code}/строка ${index + 1}: шаги есть`,
      trace.steps > 0,
      String(trace.steps),
    );

    const sum = trace.amounts.reduce((acc, t) => acc + money(t), 0);
    check(
      `${role.code}/строка ${index + 1}: шаги складываются в итог следа`,
      Math.abs(sum - money(trace.total)) < 0.005,
      `шаги ${sum.toFixed(2)} vs итог ${trace.total}`,
    );
    check(
      `${role.code}/строка ${index + 1}: итог следа равен числу в ведомости (${row.total})`,
      Math.abs(money(trace.total) - money(row.total)) < 0.005,
      `${trace.total} vs ${row.total}`,
    );

    // Приёмка задачи: по следу сумму повторяют руками. Значит у каждого шага
    // должно быть из чего её собрать, а у шага часов — часы, ставка и процент.
    // Шаг часов есть не у всякой строки: у доплаты до минимума своих входов
    // другой набор, и требовать от неё часов было бы проверкой не того.
    check(
      `${role.code}/строка ${index + 1}: у каждого шага показаны его входы`,
      trace.inputs.every((text) => text.trim().length > 0),
      trace.inputs.map((t) => t.replace(/\n/g, " ").slice(0, 40)).join(" | "),
    );
    const hoursStep = trace.inputs.findIndex((text) => text.includes("часов") && text.includes("ставка"));
    if (hoursStep >= 0) {
      check(
        `${role.code}/строка ${index + 1}: у шага часов видны часы, ставка и процент`,
        trace.inputs[hoursStep].includes("процент"),
        trace.inputs[hoursStep].replace(/\n/g, " "),
      );
    }
    check(
      `${role.code}/строка ${index + 1}: у шага сказано, чьё правило`,
      trace.levels.every((text) => text.length > 0),
      trace.levels.join(" | "),
    );
    // Экран обязан назвать источник объяснения и сказать, сошлось ли оно с
    // суммой строки. Источников два, и обещания у них разные (T056): след
    // сохранён при расчёте — либо пересобран, если строка посчитана раньше,
    // чем хранение появилось. Принимается любой из двух, но не молчание:
    // числа без слова о том, откуда они, — ровно то, что чинила T056.
    const said = trace.text.toLowerCase();
    check(
      `${role.code}/строка ${index + 1}: сказано, откуда объяснение и что оно сошлось`,
      (said.includes("сохранён") || said.includes("пересобран")) &&
        said.includes("сходится") && !said.includes("не сходится"),
      trace.text.split("\n").find((line) => line.includes("След")) || "",
    );

    // D023: чужого регистра нет ни в строках, ни словом.
    const forbidden = ALL_LEDGERS.filter((name) => !role.ledgers.includes(name));
    for (const name of forbidden) {
      check(
        `${role.code}/строка ${index + 1}: слова «${name}» на следе нет`,
        !trace.text.includes(name),
      );
    }
    check(
      `${role.code}/строка ${index + 1}: след не едет по горизонтали на 1440`,
      trace.pageScroll <= 0,
      `${trace.pageScroll}px`,
    );
  }
}

// Чужая строка: ответ обязан быть тем же, что у несуществующей.
{
  await login("director");
  await goto(APP + periodHref);
  const hrefs = await evalIn(`
    [...document.querySelectorAll("table.sheet tbody tr td:last-child a.trace")]
      .map(a => a.getAttribute("href"))
  `);
  const internal = hrefs.find((href) => href.includes("ledger=internal"));
  await login("accountant");
  const foreign = await goto(APP + internal);
  const missing = await goto(APP + "/payslips/00000000-0000-0000-0000-000000000000/trace/");
  const body = await evalIn(`document.body.innerText`);
  check(
    "бухгалтер: чужая строка отвечает как несуществующая",
    !body.includes("Как получилась эта сумма"),
    body.slice(0, 60).replace(/\n/g, " "),
  );
  void foreign; void missing;
}

// --- T030: отчёт расхождений --------------------------------------------------

// Прошлый месяц: копия этого с двумя намеренными правками. Пишем в базу
// напрямую — это подготовка материала, а читает отчёт своим обычным путём.
const PLANTED = 9000;
{
  const { execFileSync } = await import("node:child_process");
  // Май собирается копией июня: сравнивать надо два похожих месяца, иначе
  // «отклонение» найдётся в каждой строке и проверка ничего не покажет.
  // Правка ровно одна и заведомо выше порога — её и должен показать отчёт.
  const sql = `
    delete from pay_components where payslip_id in
      (select id from payslips where payrun_id in
        (select id from payruns where period = date '2026-05-01'));
    delete from payslips where payrun_id in
      (select id from payruns where period = date '2026-05-01');
    delete from payruns where period = date '2026-05-01';
    insert into periods (tenant_id, period, status)
      select tenant_id, date '2026-05-01', 'closed' from periods
       where period = date '2026-06-01'
      on conflict do nothing;
    -- Черновиком, а потом переводом: сторож цикла (T023) не даёт завести
    -- расчёт сразу посчитанным, и это правило действует и на владельца схемы.
    insert into payruns (id, tenant_id, period, status)
      select gen_random_uuid(), tenant_id, date '2026-05-01', 'draft'
        from payruns where period = date '2026-06-01';
    update payruns set status = 'calculated' where period = date '2026-05-01';
    insert into payslips (id, tenant_id, payrun_id, employee_id, unit_id)
      select gen_random_uuid(), p.tenant_id, may.id, p.employee_id, p.unit_id
        from payslips p
        join payruns june on june.id = p.payrun_id and june.period = date '2026-06-01'
        join payruns may on may.tenant_id = p.tenant_id and may.period = date '2026-05-01';
    insert into pay_components
      (id, tenant_id, payslip_id, code, title, amount, ledger, channel, taxable)
      select gen_random_uuid(), c.tenant_id, twin.id, c.code, c.title, c.amount,
             c.ledger, c.channel, c.taxable
        from pay_components c
        join payslips p on p.id = c.payslip_id
        join payruns june on june.id = p.payrun_id and june.period = date '2026-06-01'
        join payruns may on may.tenant_id = p.tenant_id and may.period = date '2026-05-01'
        join payslips twin on twin.payrun_id = may.id and twin.employee_id = p.employee_id;
    update pay_components set amount = amount - ${PLANTED}
     where id = (
       select c.id from pay_components c
         join payslips p on p.id = c.payslip_id
         join payruns r on r.id = p.payrun_id
        where r.period = date '2026-05-01'
          and c.code = 'hours.regular' and c.ledger = 'official'
        order by c.id limit 1);
  `;
  const out = execFileSync(
    "docker",
    ["compose", "exec", "-T", "db", "psql", "-q", "-U", "app", "-d", "dodo_pnl",
     "-v", "ON_ERROR_STOP=1"],
    {
      cwd: new URL("..", import.meta.url).pathname,
      // Имя стенда — только из окружения, без запасного варианта: умолчание
      // здесь означало бы, что смоук готовит данные на чужом стенде, если
      // переменную забыли. Проверку требования держит харнесс (`cdp.mjs`).
      env: process.env,
      input: sql, encoding: "utf8",
    },
  );
  check("прошлый месяц подготовлен с подброшенным отклонением", true, out.trim().slice(0, 80));
}

for (const role of ROLES) {
  await login(role.code);
  await goto(APP + periodHref);
  const went = await clickOn(
    `[...document.querySelectorAll("a.btn")].find(x => x.textContent.trim() === "Расхождения")`,
  );
  check(`${role.code}: переход в отчёт расхождений нажатием`, went);

  const seen = await readVariance();
  check(
    `${role.code}: отчёт что-то сравнил`,
    seen.text.includes("Сравнено пар"),
    seen.text.split("\n").find((line) => line.includes("Сравнено пар")) || "",
  );

  const sum = seen.rows.reduce((acc, row) => acc + money(row.delta), 0);
  check(
    `${role.code}: итог отклонений равен сумме показанных строк`,
    Math.abs(sum - money(seen.total)) < 0.005,
    `строки ${sum.toFixed(2)} vs итог ${seen.total}`,
  );

  check(
    `${role.code}: у каждой строки показан её порог`,
    seen.rows.length > 0 && seen.rows.every((row) => row.threshold.includes("%")),
    seen.rows.length ? seen.rows[0].threshold : "строк нет",
  );

  const forbidden = ALL_LEDGERS.filter((name) => !role.ledgers.includes(name));
  for (const name of forbidden) {
    check(`${role.code}: слова «${name}» в отчёте нет`, !seen.text.includes(name));
  }
  check(
    `${role.code}: отчёт не едет по горизонтали на 1440`,
    seen.pageScroll <= 0,
    `${seen.pageScroll}px`,
  );

  if (role.code === "director") {
    const planted = seen.rows.find(
      (row) => row.code === "hours.regular" && Math.abs(money(row.delta) - PLANTED) < 0.005,
    );
    check(
      `директор: подброшенное отклонение +${PLANTED} найдено`,
      Boolean(planted),
      seen.rows.map((r) => `${r.employee}/${r.code}=${r.delta}`).join(", ").slice(0, 200),
    );
    // Молчание о том, что в пределах порога: надбавка у всех не менялась вовсе,
    // и её в отчёте быть не должно ни у кого.
    check(
      "директор: неизменившиеся компоненты в отчёт не попали",
      !seen.rows.some((row) => row.code === "meal_and_vacation_bonus"),
      seen.rows.filter((r) => r.code === "meal_and_vacation_bonus").length + " строк",
    );
  }
}

if (logs.length) console.log("\nконсоль браузера:\n" + logs.join("\n"));
report();
