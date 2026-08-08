/*
 * Смоук фонового расчёта периода (T024).
 *
 * Проверяет то, чего ни тест, ни разбор разметки доказать не могут: что человек
 * жмёт кнопку и видит ход работы, а не замершую страницу; что считает настоящий
 * рабочий процесс очереди в своём контейнере; что страница сама узнаёт об
 * окончании и показывает ведомость; и что при погашенной очереди продукт не
 * молчит, а объясняет словами и даёт посчитать прямо сейчас.
 *
 * Очередь смоук гасит и поднимает сам — иначе фазу «задачу никто не взял» на
 * живом стенде не воспроизвести: 35 человек считаются быстрее, чем успевает
 * моргнуть полоса. Служба останавливается и возвращается на место в конце.
 *
 *     COMPOSE_PROJECT_NAME=dodo-pnl-pr3 node tools/smoke_payrun_background.mjs
 *
 * Перед запуском:
 *     "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" --headless=new \
 *         --remote-debugging-port=9339 --user-data-dir=/tmp/chrome-smoke &
 */
import { execFileSync } from "node:child_process";
import { writeFileSync } from "node:fs";

import { attach, findPeriodAndGrid, loginWith } from "./cdp.mjs";

const APP = process.env.APP || "http://127.0.0.1:8053";
const SHOTS = process.env.SMOKE_SHOTS || "/tmp";
const WORKER = process.env.SMOKE_WORKER_SERVICE || "worker";

const { evalIn, goto, send, check, report, logs } = await attach();
const login = loginWith(APP, evalIn, goto);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const compose = (...args) =>
  execFileSync("docker", ["compose", ...args], { encoding: "utf8" }).trim();

const sql = (query) =>
  execFileSync(
    "docker",
    ["compose", "exec", "-T", "db", "psql", "-U", "app", "-d", "dodo_pnl", "-tAc", query],
    { encoding: "utf8" },
  ).trim();

/** Снимок экрана — чтобы полосу можно было увидеть глазами, а не по разметке. */
async function shot(name) {
  const { data } = await send("Page.captureScreenshot", { format: "png" });
  const path = `${SHOTS}/${name}.png`;
  writeFileSync(path, Buffer.from(data, "base64"));
  console.log(`     снимок: ${path}`);
}

/** Настоящий клик по элементу, найденному по видимому тексту. */
async function clickByText(selector, text) {
  const box = await evalIn(`
    (() => {
      const el = [...document.querySelectorAll(${JSON.stringify(selector)})]
        .find(x => x.textContent.includes(${JSON.stringify(text)}));
      if (!el) return null;
      el.scrollIntoView({ block: "center" });
      const r = el.getBoundingClientRect();
      return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
    })()
  `);
  if (!box) return false;
  for (const t of ["mousePressed", "mouseReleased"]) {
    await send("Input.dispatchMouseEvent", {
      type: t, x: box.x, y: box.y, button: "left", clickCount: 1,
    });
  }
  await sleep(1200);
  return true;
}

/** Ждать условия, подглядывая в живую страницу — без перезагрузок руками. */
async function waitFor(expr, seconds = 30) {
  for (let i = 0; i < seconds * 5; i++) {
    if (await evalIn(expr).catch(() => false)) return true;
    await sleep(200);
  }
  return false;
}

await send("Emulation.setDeviceMetricsOverride", {
  width: 1440, height: 900, deviceScaleFactor: 1, mobile: false,
});

// --- 0. Известное состояние: очередь погашена, расчётов нет ------------------
compose("stop", WORKER);
// Историю очереди чистим тоже: она копится между прогонами, и счёт задач стал
// бы «сколько раз гоняли смоук», а не «сколько задач в этом прогоне».
sql(
  "delete from pay_components; delete from payslip_totals; delete from payslips;" +
  " delete from payrun_jobs; delete from payruns;" +
  " delete from django_q_task; delete from django_q_ormq;",
);

await login("director");
const { periodHref } = await findPeriodAndGrid(APP, evalIn, goto);
await goto(APP + periodHref);

// --- 1. Нажатие возвращает страницу сразу и показывает ход работы ------------
check("директор видит кнопку расчёта", await clickByText("button", "Посчитать период"));
{
  const visible = await evalIn(`!!document.querySelector('section.progress')`);
  const text = await evalIn(
    `document.querySelector('section.progress')?.innerText || ''`
  );
  check("на странице полоса хода работы", visible);
  check("полоса говорит, что расчёт идёт", text.includes("Идёт расчёт периода"), text.split("\n")[0]);
  check("этап назван словами", /В очереди|Считаем|Собираем/.test(text), text.split("\n")[1] || "");
  check("ведомости ещё нет", !(await evalIn(`!!document.querySelector('table.sheet')`)));
  await shot("payrun-progress-queued");
}
check(
  "задание встало в очередь фоновым",
  sql("select status || ',' || background from payrun_jobs") === "queued,true",
);

// --- 2. Живой рабочий процесс подхватывает и досчитывает ---------------------
// Страницу не трогаем: она обязана узнать об окончании сама.
compose("start", WORKER);
const stages = new Set();
const finished = await (async () => {
  for (let i = 0; i < 200; i++) {
    const stage = await evalIn(
      `document.querySelector('section.progress .stage')?.textContent || ''`
    ).catch(() => "");
    if (stage) stages.add(stage.trim());
    if (await evalIn(`!!document.querySelector('table.sheet')`).catch(() => false)) return true;
    await sleep(150);
  }
  return false;
})();
check("страница сама показала ведомость, без перезагрузки руками", finished);
console.log("     этапы, которые успела показать полоса: " + [...stages].join(" → "));
{
  const rows = await evalIn(`document.querySelectorAll("table.sheet tbody tr").length`);
  const text = await evalIn(`document.body.innerText`);
  check("директор: 60 строк ведомости", rows === 60, String(rows));
  check("директор: итог 1 951 806,13", text.includes("1 951 806,13"));
  check("директор: норма 176,00", text.includes("176,00"));
  await shot("payrun-done-director");
}
check(
  "считал рабочий процесс очереди, а не запрос",
  sql("select status || ',' || background from payrun_jobs order by created_at desc limit 1") === "done,true",
);
check(
  "очередь отметила задачу выполненной",
  sql("select count(*) from django_q_task where success") === "1",
);

// --- 3. Числа-ориентиры у ролей с урезанной видимостью -----------------------
for (const [who, rows, total] of [
  ["accountant", 33, "464 752,41"],
  ["manager", 24, "891 373,32"],
]) {
  await login(who);
  await goto(APP + periodHref);
  const seen = await evalIn(`document.querySelectorAll("table.sheet tbody tr").length`);
  const text = await evalIn(`document.body.innerText`);
  check(`${who}: ${rows} строк`, seen === rows, String(seen));
  check(`${who}: итог ${total}`, text.includes(total));
  check(`${who}: норма 176,00`, text.includes("176,00"));
}

// --- 4. Очередь погасили: продукт объясняет, а не делает вид -----------------
compose("stop", WORKER);
await login("director");
await goto(APP + periodHref);
check("директор запускает расчёт заново", await clickByText("button", "Посчитать период"));
{
  const said = await waitFor(
    `(document.querySelector('section.progress')?.innerText || '')
       .includes('рабочий процесс очереди')`,
    40,
  );
  check("страница сама сказала, что задачу некому взять", said);
  const text = await evalIn(`document.querySelector('section.progress').innerText`);
  check("предложена кнопка «Посчитать прямо сейчас»", text.includes("Посчитать прямо сейчас"));
  await shot("payrun-progress-stuck");
}

// --- 5. «Посчитать прямо сейчас» доводит зависшее задание --------------------
const before = sql("select count(*) from payrun_jobs");
check("директор жмёт «Посчитать прямо сейчас»", await clickByText("button", "Посчитать прямо сейчас"));
{
  const rows = await evalIn(`document.querySelectorAll("table.sheet tbody tr").length`);
  const text = await evalIn(`document.body.innerText`);
  check("ведомость на месте: 60 строк", rows === 60, String(rows));
  check("итог тот же: 1 951 806,13", text.includes("1 951 806,13"));
  check("полосы больше нет", !(await evalIn(`!!document.querySelector('section.progress')`)));
  await shot("payrun-inline-takeover");
}
check("второго задания не завелось", sql("select count(*) from payrun_jobs") === before, before);
check(
  "зависшее задание доведено синхронным расчётом",
  sql("select status || ',' || background from payrun_jobs order by created_at desc limit 1") === "done,false",
);

// --- 6. Опоздавшая задача из очереди не пересчитывает заново -----------------
// Задача, поставленная в шаге 4, всё ещё лежит в очереди: её задание уже довели
// вручную. Поднимаем рабочий процесс — он обязан взять её и ничего не сделать.
// Это та самая идемпотентность, но проверенная на живом стенде, а не в тесте.
{
  const slips = sql("select count(*) from payslips");
  compose("start", WORKER);
  const drained = await (async () => {
    for (let i = 0; i < 60; i++) {
      if (sql("select count(*) from django_q_ormq") === "0") return true;
      await sleep(1000);
    }
    return false;
  })();
  check("очередь разобрала опоздавшую задачу", drained);
  check("ведомость не пересчиталась заново", sql("select count(*) from payslips") === slips, slips);
  check(
    "задание осталось доведённым вручную",
    sql("select status || ',' || background from payrun_jobs order by created_at desc limit 1") === "done,false",
  );
}

const noise = logs.filter((l) => l.startsWith("EXCEPTION"));
check("в консоли браузера нет исключений", noise.length === 0, noise.join(" | "));
report();
