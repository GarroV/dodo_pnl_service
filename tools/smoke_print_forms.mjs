/*
 * Смоук печатных форм (T187, issue #184).
 *
 * Проверяет то, чего разбором разметки не докажешь: что бумага **печатается**.
 * Тесты в `tests/test_print_forms.py` отвечают за арифметику — сколько строк на
 * каком листе и сходятся ли деньги; здесь отвечают на другой вопрос: совпало ли
 * это с тем, что реально выйдет из принтера.
 *
 * Три вещи, и каждая ловит свою поломку:
 *
 * 1. **Лист — это лист.** Коробка `.paper` ровно 210 × 297 мм. Разъедься
 *    миллиметры CSS с константами `reports/printing.py` — и продукт пишет
 *    «Лист 1 из 2», а печатается три, из которых один пустой.
 * 2. **Ничего не обрезано.** Ни один элемент внутри листа не выходит за его
 *    края. Это прямая проверка обрезки, а не «на глаз влезло»: `overflow:
 *    hidden` режет молча, и заметил бы это тот, кто уже понёс бумагу на подпись.
 * 3. **Столько листов, сколько обещано.** `Page.printToPDF` печатает страницу
 *    по-настоящему, и число страниц в PDF сверяется с числом, написанным на
 *    самой бумаге («Лист N из M»).
 *
 *     google-chrome --headless=new --remote-debugging-port=9351 \
 *         --user-data-dir=/tmp/chrome-smoke-print &
 *     COMPOSE_PROJECT_NAME=dodo-pnl-reports APP=http://127.0.0.1:8070 \
 *         CDP_PORT=9351 node tools/smoke_print_forms.mjs
 *
 * Стенд смоук приводит к сиду сам — и в начале, и после себя (договор в шапке
 * `cdp.mjs`).
 */
import { attach, ensureCalculated, loginWith, standFromSeed } from "./cdp.mjs";

const APP = process.env.APP || "http://127.0.0.1:8070";

// Лист A4 в пикселях CSS: 1 дюйм = 96px, 1 дюйм = 25.4мм.
const MM = 96 / 25.4;
const A4 = { width: 210 * MM, height: 297 * MM };
// Допуск: браузер округляет миллиметры до долей пикселя. Полпикселя — это
// 0,13 мм, разъезд геометрии столько не бывает.
const SLACK = 0.5;

const { evalIn, goto, send, clickOn, check, report, logs } = await attach();
const login = loginWith(APP, evalIn, goto);

standFromSeed();

/**
 * Войти и убедиться, что вошли.
 *
 * `loginWith` отправляет форму и ждёт полторы секунды. Сразу после `seed_dev`
 * первый запрос к приложению отвечает дольше: сид только что переписал тенант
 * целиком. Тогда ответ не успевает приехать, страница остаётся формой входа, а
 * падает смоук через два шага — на пустом списке периодов, «Cannot navigate to
 * invalid URL». Красный не по своей вине хуже отсутствующей проверки (договор в
 * `cdp.mjs`), поэтому вход здесь проверяется, а не предполагается.
 */
async function signIn(who) {
  for (let attempt = 0; attempt < 5; attempt++) {
    await login(who);
    if (!(await evalIn("location.pathname")).startsWith("/login")) return;
  }
  throw new Error(`не удалось войти как ${who}`);
}

await signIn("director");
// Период должен быть посчитан — иначе печатать нечего ни одной роли.
const periodHref = await ensureCalculated(APP, { evalIn, goto, clickOn });

// Дальше смоук ходит ссылками, а не нажатиями: печатные формы открываются
// обычными ссылками, и ссылку он обязан взять с экрана, а не собрать строкой —
// так заодно проверяется, что человек до неё доходит.

await goto(APP + periodHref);
const printHref = await evalIn(`
  (() => {
    const a = [...document.querySelectorAll('a')]
      .find(a => a.textContent.trim() === "Ведомость на печать");
    return a ? a.getAttribute('href') : "";
  })()
`);
check("со страницы периода есть ссылка на печатную ведомость", Boolean(printHref), printHref);

/** Геометрия листов и обрезка — измерением, а не взглядом. */
async function measure() {
  return await evalIn(`
    (() => {
      const papers = [...document.querySelectorAll('.paper')];
      const said = [...document.body.innerText.matchAll(/Лист (\\d+) из (\\d+)/g)]
        .map(m => [Number(m[1]), Number(m[2])]);
      const boxes = papers.map(p => {
        const r = p.getBoundingClientRect();
        // Самый нижний и самый правый край среди всего, что лежит на листе.
        let bottom = r.top, right = r.left;
        for (const el of p.querySelectorAll('*')) {
          const b = el.getBoundingClientRect();
          if (b.height === 0 && b.width === 0) continue;
          bottom = Math.max(bottom, b.bottom);
          right = Math.max(right, b.right);
        }
        return {
          width: r.width, height: r.height,
          overflowBottom: bottom - r.bottom, overflowRight: right - r.right,
        };
      });
      return { papers: papers.length, said, boxes };
    })()
  `);
}

/**
 * Сколько страниц в напечатанном PDF.
 *
 * Считается по дереву страниц самого файла, а не по нашему представлению о нём:
 * весь смысл проверки в том, чтобы спросить браузер, а не себя.
 */
async function printedPages() {
  const { data } = await send("Page.printToPDF", {
    printBackground: true,
    paperWidth: 8.27, paperHeight: 11.69,
    marginTop: 0, marginBottom: 0, marginLeft: 0, marginRight: 0,
    preferCSSPageSize: true,
  });
  const pdf = Buffer.from(data, "base64").toString("latin1");
  const counts = [...pdf.matchAll(/\/Type\s*\/Pages[^>]*?\/Count\s+(\d+)/g)]
    .map((m) => Number(m[1]));
  const leaves = (pdf.match(/\/Type\s*\/Page[^s]/g) || []).length;
  if (!counts.length && !leaves) throw new Error("не удалось прочитать число страниц в PDF");
  return counts.length ? Math.max(...counts) : leaves;
}

async function inspect(name, url, { sheets = 0, paged = true } = {}) {
  await goto(url);
  const seen = await measure();

  check(`${name}: лист есть`, seen.papers > 0, `листов ${seen.papers}`);
  if (sheets) {
    check(`${name}: листов ${sheets}`, seen.papers === sheets, `${seen.papers}`);
  }
  // Подпись «Лист N из M» стоит на многолистовом документе; расчётный листок
  // всегда один лист, и эталон её там не рисует — обещать нечего.
  if (paged) {
    check(
      `${name}: листов на экране столько же, сколько подписей «Лист N из M»`,
      seen.papers === seen.said.length,
      `листов ${seen.papers}, подписей ${seen.said.length}`,
    );
    check(
      `${name}: подписи листов идут по порядку и знают общее число`,
      seen.said.length > 0 && seen.said.every(([n, of], i) => n === i + 1 && of === seen.papers),
      JSON.stringify(seen.said),
    );
  }

  const wrongSize = seen.boxes.filter(
    (b) => Math.abs(b.width - A4.width) > SLACK || Math.abs(b.height - A4.height) > SLACK,
  );
  check(
    `${name}: каждый лист ровно 210 × 297 мм`,
    wrongSize.length === 0,
    wrongSize.length ? JSON.stringify(wrongSize[0]) : `${seen.boxes[0]?.width}×${seen.boxes[0]?.height}px`,
  );

  const cut = seen.boxes
    .map((b, i) => ({ i: i + 1, ...b }))
    .filter((b) => b.overflowBottom > SLACK || b.overflowRight > SLACK);
  check(
    `${name}: ничего не выходит за края листа`,
    cut.length === 0,
    cut.length
      ? `лист ${cut[0].i}: вниз на ${cut[0].overflowBottom.toFixed(1)}px, ` +
        `вправо на ${cut[0].overflowRight.toFixed(1)}px`
      : "запас есть на всех листах",
  );

  const pages = await printedPages();
  check(
    `${name}: в напечатанном PDF ровно ${seen.papers} страниц`,
    pages === seen.papers,
    `в PDF ${pages}`,
  );
  return seen;
}

await inspect("платёжная ведомость", APP + printHref);

// Разрез печатью не поддерживается: адрес с ним обязан ответить отказом со
// своими словами, а не документом всего расчёта молча.
await goto(APP + printHref + "?ledger=official");
const refusedByCut = await evalIn(`
  ({ paper: !!document.querySelector('.paper'),
     said: (document.querySelector('.refused') || {}).innerText || "" })
`);
check(
  "разрез печатной ведомости отвечает отказом, а не документом",
  !refusedByCut.paper && refusedByCut.said.includes("по всему расчёту"),
  refusedByCut.said.slice(0, 90),
);

// Расчётный листок берётся тем же путём, что у человека: ведомость → след
// расчёта → бумага.
await goto(APP + periodHref);
const traceHref = await evalIn(
  `document.querySelector('a.trace')?.getAttribute('href') || ""`,
);
check("из ведомости открывается след расчёта", Boolean(traceHref), traceHref);
await goto(APP + traceHref);
const slipHref = await evalIn(`
  (() => {
    const a = [...document.querySelectorAll('a')]
      .find(a => a.textContent.trim() === "расчётный листок на печать");
    return a ? a.getAttribute('href') : "";
  })()
`);
check("со следа расчёта открывается расчётный листок", Boolean(slipHref), slipHref);
await inspect("расчётный листок", APP + slipHref, { sheets: 1, paged: false });

// Печать не тащит с собой тёмную тему: бумага всегда белая (эталон, tokens.css).
await send("Emulation.setEmulatedMedia", { media: "print" });
const paperColour = await evalIn(`
  getComputedStyle(document.querySelector('.paper')).backgroundColor
`);
await send("Emulation.setEmulatedMedia", { media: "" });
check("лист печатается белым", paperColour === "rgb(255, 255, 255)", paperColour);

const noise = logs.filter((line) => /EXCEPTION|Failed to load/.test(line));
check("консоль чистая", noise.length === 0, noise.slice(0, 2).join(" | "));

report();
