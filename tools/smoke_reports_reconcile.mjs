/*
 * Смоук сверки с таблицей бухгалтера и трёх выгрузок (T031, T032).
 *
 * Проверяет то, чего разбором разметки не докажешь: что файл действительно
 * выбирается в поле и уезжает на сервер настоящим нажатием, что сверка отвечает
 * на экране, и что три ссылки выгрузок **скачивают настоящие файлы** — они
 * ложатся на диск, и их потом читает обратно `tools/check_smoke_exports.py`.
 *
 * Скачивание проверяется именно так, потому что это главная опасность T032:
 * файл уходит из продукта и живёт своей жизнью. «Ссылка нажалась» не значит
 * ничего — важно, что легло в файл.
 *
 * Роли берутся две, и это не формальность: у бухгалтера база не отдаёт итоги
 * расчёта вовсе (T071), и сверка обязана сказать это прямо, а не отрапортовать
 * совпадение по деньгам, которых она не видела.
 *
 *     google-chrome --headless=new --remote-debugging-port=9341 \
 *         --user-data-dir=/tmp/chrome-smoke-rep3 &
 *     APP=http://127.0.0.1:8058 DOWNLOADS=/tmp/rep3-downloads \
 * Стенд смоук приводит к сиду сам — и в начале, и после себя (см. договор в
 * шапке `cdp.mjs`). Поэтому запускать его можно в любом порядке и в одиночку,
 * а `COMPOSE_PROJECT_NAME` обязателен: без него сброс ушёл бы на чужой стенд.
 *
 *         COMPOSE_PROJECT_NAME=<стенд> node tools/smoke_reports_reconcile.mjs
 */
import { mkdirSync, rmSync } from "node:fs";
import { resolve } from "node:path";

import { attach, findPeriodAndGrid, loginWith, standFromSeed } from "./cdp.mjs";

const APP = process.env.APP || "http://127.0.0.1:8058";
const DOWNLOADS = resolve(process.env.DOWNLOADS || "/tmp/rep3-downloads");
// Каталог скачиваний чистится на входе, а не на выходе: проверка считает
// скачанные файлы, и файлы прошлого прогона сделали бы её красной при
// исправном продукте. На выходе уборка не срабатывает ровно тогда, когда она
// нужна, — при падении на полпути.
rmSync(DOWNLOADS, { recursive: true, force: true });
const SAMPLE = resolve(process.env.SAMPLE || "tests/fixtures/plata-sample.xlsx");

// Ориентиры приёмки на данных сида. Разные роли получают разную сверку, и это
// не побочный эффект, а суть: итоги видны только роли, которой видны все
// регистры учёта.
const ROLES = [
  {
    code: "director",
    // Директору отданы все итоги — сверка идёт по деньгам.
    summary: {
      "Сошлось до копейки": 32,
      "Разошлось на копейки (округление)": 0,
      "Разошлось": 0,
      "Сверены только входы — деньги не сравнивались": 0,
      "Есть в таблице, нет в вашей части расчёта": 0,
      "Есть в расчёте, нет в таблице": 3,
    },
    exports: 3,
  },
  {
    code: "accountant",
    // Бухгалтеру итогов не отдано ни по одной строке: сверяются входы.
    summary: {
      "Сошлось до копейки": 0,
      "Разошлось на копейки (округление)": 0,
      "Разошлось": 0,
      "Сверены только входы — деньги не сравнивались": 32,
      "Есть в таблице, нет в вашей части расчёта": 0,
      "Есть в расчёте, нет в таблице": 3,
    },
    exports: 3,
  },
];

mkdirSync(DOWNLOADS, { recursive: true });

const { send, evalIn, goto, check, report, logs } = await attach();
const login = loginWith(APP, evalIn, goto);

// Стенд к эталону сейчас и обратно к нему после — в том числе если смоук
// упадёт на полпути (issue #76). Порядок запуска смоуков больше ничего не
// решает: каждый начинает с известного входа и ничего за собой не оставляет.
standFromSeed();

await send("Emulation.setDeviceMetricsOverride", {
  width: 1440, height: 900, deviceScaleFactor: 1, mobile: false,
});
/* Папка скачивания — своя на каждую роль. Имена файлов у всех ролей одинаковы
 * (`payout-2026-06.xlsx`), и в одной папке Chrome дописал бы к ним « (1)»: файл
 * бухгалтера тогда невозможно отличить от файла директора, а именно эту разницу
 * проверка и ищет. Без самого вызова headless-Chrome отменяет скачивания
 * молча — смоук «прошёл бы», не получив ни одного файла. */
const downloadsTo = (folder) => send("Browser.setDownloadBehavior", {
  behavior: "allow", downloadPath: folder, eventsEnabled: true,
});
await downloadsTo(DOWNLOADS);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Нажать настоящей мышью по элементу, найденному тем же способом, что глазами. */
async function clickBy(finderJs, name) {
  const box = await evalIn(`
    (() => {
      const el = ${finderJs};
      if (!el) return null;
      el.scrollIntoView({ block: "center" });
      const r = el.getBoundingClientRect();
      return { x: r.x + r.width / 2, y: r.y + r.height / 2 };
    })()
  `);
  if (!box) {
    check(`нажатие «${name}»: элемент найден`, false);
    return false;
  }
  for (const type of ["mousePressed", "mouseReleased"]) {
    await send("Input.dispatchMouseEvent", {
      type, x: box.x, y: box.y, button: "left", clickCount: 1,
    });
  }
  return true;
}

/** Положить файл в поле выбора — тем же путём, что диалог операционной системы. */
async function chooseFile(selector, path) {
  const { result } = await send("Runtime.evaluate", {
    expression: `document.querySelector(${JSON.stringify(selector)})`,
  });
  if (!result.objectId) return false;
  await send("DOM.setFileInputFiles", { files: [path], objectId: result.objectId });
  return true;
}

/** Сводка сверки — числами, а не наличием подписи: подписи стоят всегда. */
const readSummary = () => evalIn(`
  (() => {
    const table = [...document.querySelectorAll("table.sheet")]
      .find(t => t.textContent.includes("Итог сверки"));
    if (!table) return null;
    const out = {};
    for (const tr of table.querySelectorAll("tbody tr")) {
      out[tr.children[0].textContent.trim()] = Number(tr.children[1].textContent.trim());
    }
    return {
      counts: out,
      sections: [...document.querySelectorAll("h2")].map(h => h.textContent.trim()),
      text: document.body.innerText,
      // Имена в разделе «Сверены только входы»: строка без имени — это то, во
      // что превращалась несравнённая строка, попав в «Разошлось».
      inputNames: (() => {
        const head = [...document.querySelectorAll("h2")]
          .find(h => h.textContent.includes("Сверены только входы"));
        if (!head) return [];
        const t = head.nextElementSibling && head.nextElementSibling.nextElementSibling;
        if (!t || t.tagName !== "TABLE") return [];
        return [...t.querySelectorAll("tbody tr")]
          .map(tr => tr.children[1].textContent.trim());
      })(),
    };
  })()
`);

// --- расчёт периода: без него сверять и выгружать нечего ----------------------

await login("director");
const { periodHref } = await findPeriodAndGrid(APP, evalIn, goto);
await goto(APP + periodHref);
{
  await clickBy(
    `[...document.querySelectorAll("button")].find(x => x.textContent.includes("Посчитать период"))`,
    "Посчитать период",
  );
  let has = false;
  for (let i = 0; i < 40 && !has; i++) {
    await sleep(700);
    has = await evalIn(`document.querySelectorAll("table.sheet tbody tr").length > 0`);
  }
  check("период посчитан, ведомость на экране", has);
}

// --- сверка и выгрузки под каждой ролью ---------------------------------------

for (const role of ROLES) {
  await login(role.code);

  // Сверка: на страницу переходим ссылкой с ведомости, как это делает человек.
  await goto(APP + periodHref);
  check(
    `${role.code}: со страницы периода есть ссылка на сверку`,
    await clickBy(
      `[...document.querySelectorAll("a")].find(x => x.textContent.trim() === "Сверка с таблицей")`,
      "Сверка с таблицей",
    ),
  );
  await sleep(1200);

  check(
    `${role.code}: файл выбран в поле`,
    await chooseFile("input[type=file][name=table]", SAMPLE),
  );
  check(
    `${role.code}: нажата кнопка «Сверить»`,
    await clickBy(
      `[...document.querySelectorAll("button")].find(x => x.textContent.trim() === "Сверить")`,
      "Сверить",
    ),
  );
  await sleep(2500);

  const seen = await readSummary();
  check(`${role.code}: сверка ответила сводкой`, Boolean(seen));
  if (!seen) continue;

  for (const [label, want] of Object.entries(role.summary)) {
    check(
      `${role.code}: «${label}» = ${want}`,
      seen.counts[label] === want,
      String(seen.counts[label]),
    );
  }

  if (role.summary["Сверены только входы — деньги не сравнивались"]) {
    // Три проверки одного и того же требования с разных сторон: сверка не
    // должна ни назвать себя чистой, ни выдать несравнённое за расхождение,
    // ни потерять имена людей.
    check(
      `${role.code}: сверка не назвала себя сошедшейся`,
      !seen.text.includes("Всё сошлось до копейки"),
    );
    check(
      `${role.code}: раздела «Разошлось» нет — сравнивать было нечего`,
      !seen.sections.includes("Разошлось"),
      seen.sections.join(" | "),
    );
    check(
      `${role.code}: подвал говорит, что деньги не сравнивались`,
      seen.text.includes("Деньги не сравнивались ни по одной строке"),
    );
    check(
      `${role.code}: в разделе входов 32 строки и у каждой есть имя`,
      seen.inputNames.length === 32 && seen.inputNames.every((n) => n.length > 0),
      `${seen.inputNames.length} строк`,
    );
  } else {
    check(
      `${role.code}: сверка названа сошедшейся не была бы верна — строки сверх таблицы`,
      !seen.text.includes("Всё сошлось до копейки"),
    );
  }

  // Чужого регистра на странице сверки быть не должно ни в каком виде (D023).
  if (role.code === "accountant") {
    for (const word of ["Дополнительный", "Внутренний", "supplementary", "internal"]) {
      check(`${role.code}: слова «${word}» на сверке нет`, !seen.text.includes(word));
    }
  }

  // Выгрузки: три настоящих нажатия по ссылкам с ведомости.
  const folder = `${DOWNLOADS}/${role.code}`;
  mkdirSync(folder, { recursive: true });
  await downloadsTo(folder);
  await goto(APP + periodHref);
  for (const title of ["Ведомость к выплате", "Строки для P&L", "Вид бухгалтера"]) {
    check(
      `${role.code}: нажата «${title}»`,
      await clickBy(
        `[...document.querySelectorAll("a")].find(x => x.textContent.trim() === ${JSON.stringify(title)})`,
        title,
      ),
    );
    await sleep(1500);
  }
}

if (logs.length) console.log("\nконсоль браузера:\n" + logs.join("\n"));
console.log(`\nскачанное лежит в ${DOWNLOADS} — прочитать обратно: ` +
            `python tools/check_smoke_exports.py ${DOWNLOADS}`);
report();
