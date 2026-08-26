/*
 * Смоук визуала: живые экраны против чисел дизайн-системы.
 *
 * Зачем он есть. Проверки разметки говорят, что класс на месте, но не что
 * экран выглядит как макет. 23.08.2026 замер показал: строка ведомости 45
 * пикселей вместо 30 из эталона, обычные списки 40 вместо 34, заголовок весом
 * 700 вместо 600. Все тесты при этом были зелёными — плотность не проверял
 * никто, а на глаз «чуть больше воздуха» не бросается, хотя в макете 35
 * человек помещаются в экран, а у нас нет.
 *
 * Что меряет: высоту строк таблиц, высоту кнопок, размер и вес заголовка,
 * базовый размер и шрифт страницы, наличие инлайновых цветов в разметке.
 *
 *     google-chrome --headless=new --remote-debugging-port=9390 \
 *         --user-data-dir=/tmp/chrome-visual &
 *     APP=http://127.0.0.1:8001 node tools/smoke_visual.mjs
 *
 * Экран ролей выпадает намеренно: в его строке решётка из двадцати флажков, и
 * шаг 30 пикселей там невозможен по природе содержимого.
 */
import { attach, loginWith, ensureCalculated } from "/Users/garva/Documents/projects/dodo_pnl_service/tools/cdp.mjs";
const APP = process.env.APP || "http://127.0.0.1:8001";
const ЭТАЛОН = { строка: 30, кнопка: 32, кнопкаМалая: 27, тело: 13, заголовок: 20, вес: "600", шрифт: "Golos Text" };
const page = await attach({ cdpPort: 9390 });
const { evalIn, goto, send } = page;
await loginWith(APP, evalIn, goto)("admin");
const href = await ensureCalculated(APP, page);
const экраны = [
  ["ведомость", href], ["табель", href + "timesheet-replace"], ["периоды", "/periods/"],
  ["справочники", "/directory/"], ["сотрудники", "/directory/employees/"],
  ["группы", "/directory/groups/"], ["точки", "/directory/units/"],
  ["календарь", "/directory/calendar/"], ["правила", "/rules/"], ["роли", "/roles/"],
  ["расходы", "/expenses/"], ["счета", "/invoices/"], ["инбокс", "/inbox/"],
  ["гайд", "/guide/"], ["расхождения", href + "variance/"], ["сверка", href + "reconcile/"],
];
await send("Emulation.setDeviceMetricsOverride", { width: 1440, height: 900, deviceScaleFactor: 1, mobile: false });
const проблемы = [];
for (const [имя, url] of экраны) {
  if (url.includes("timesheet-replace")) continue;
  await goto(APP + url);
  await new Promise(r => setTimeout(r, 250));
  const данные = JSON.parse(await evalIn(`(() => {
    const h = s => { const e = document.querySelector(s); return e ? Math.round(e.getBoundingClientRect().height) : null; };
    const cs = (s, p) => { const e = document.querySelector(s); return e ? getComputedStyle(e)[p] : null; };
    const rows = [...document.querySelectorAll('table tbody tr')].map(t => Math.round(t.getBoundingClientRect().height));
    return JSON.stringify({
      строки: rows.length ? [Math.min(...rows), Math.max(...rows)] : null,
      кнопка: h('button:not(.link):not([class*=sm])'),
      h1вес: cs('h1', 'fontWeight'), h1размер: cs('h1', 'fontSize'),
      тело: cs('body', 'fontSize'), шрифт: cs('body', 'fontFamily'),
      литералы: [...document.querySelectorAll('[style*="color"], [style*="background"]')].length,
    });
  })()`));
  const бяки = [];
  if (данные.строки && (данные.строки[0] < 26 || данные.строки[1] > 40)) бяки.push(`строки ${данные.строки[0]}–${данные.строки[1]} (эталон ${ЭТАЛОН.строка})`);
  if (данные.кнопка && Math.abs(данные.кнопка - ЭТАЛОН.кнопка) > 2 && Math.abs(данные.кнопка - ЭТАЛОН.кнопкаМалая) > 2) бяки.push(`кнопка ${данные.кнопка}`);
  if (данные.h1вес && данные.h1вес !== ЭТАЛОН.вес) бяки.push(`вес h1 ${данные.h1вес}`);
  if (данные.h1размер && данные.h1размер !== "20px") бяки.push(`h1 ${данные.h1размер}`);
  if (данные.тело !== "13px") бяки.push(`тело ${данные.тело}`);
  if (!String(данные.шрифт).includes(ЭТАЛОН.шрифт)) бяки.push(`шрифт ${данные.шрифт}`);
  if (данные.литералы) бяки.push(`инлайновых цветов: ${данные.литералы}`);
  console.log(`${имя.padEnd(14)} ${бяки.length ? "⚠ " + бяки.join(" · ") : "ок"}`);
  if (бяки.length) проблемы.push(имя);
}
console.log("\nэкранов с расхождениями:", проблемы.length, проблемы.join(", "));
process.exit(0);
