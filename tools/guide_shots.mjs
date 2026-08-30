/*
 * Снимки экранов для пользовательских материалов — гайда, инструкции, онбординга.
 *
 * Зачем скрипт, а не «сниму руками». Скриншот протухает молча и хуже текста:
 * текст противоречит экрану заметно, а картинка выглядит убедительно и врёт —
 * человек ищет кнопку, которой больше нет, и решает, что сломался он. Значит
 * пересъёмка обязана стоить одну команду, иначе после второй правки интерфейса
 * её перестанут делать (глобальное правило про пользовательские материалы).
 *
 * «Одна команда» здесь в буквальном смысле: скрипт САМ приводит стенд в то
 * состояние, которое показывает гайд, — на свежем сиде месяц не посчитан и не
 * утверждён, наличных расходов, счетов и статей расходов нет вовсе, и добрая
 * половина снимков вышла бы пустыми экранами. Заметил бы это только человек,
 * которому гайд уже показали, — поэтому подготовка (`guide_prepare.mjs`) идёт
 * первым шагом сама. `SKIP_PREPARE=1` пропускает её — нужно, когда стенд уже
 * подготовлен и человек просто переснимает во второй раз.
 *
 * Снимает с ЛОКАЛЬНОГО продукта, а не с демо: демо всегда англоязычное (D035),
 * а материалы для команды — на русском.
 *
 *     docker compose up -d app                  # если ещё не поднят
 *     "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
 *         --headless=new --disable-gpu --hide-scrollbars \
 *         --remote-debugging-port=9341 --user-data-dir=/tmp/chrome-shots &
 *     APP=http://127.0.0.1:8000 USER_NAME=admin USER_PASS=dodo-dev \
 *         node tools/guide_shots.mjs <куда-складывать>
 *
 * Дальше снимки сжимаются и встраиваются в страницу как data:-адреса —
 * внешние картинки в опубликованном артефакте не грузятся.
 *
 * Список экранов лежит в `docs/guides/screens.json` — один и тот же на съёмку
 * и на сборку страницы. Добавили экран в гайд — впишите его туда; забыть
 * нельзя, это стережёт `tests/test_guide_screens.py`.
 *
 * Подстановок в адресах пять, и все ищутся на стенде сами:
 *   {period}       id утверждённого месяца — меняется ВНУТРИ пути;
 *   {employee}     полный адрес карточки человека;
 *   {open_period}  полный адрес только что заведённого месяца;
 *   {trace}        полный адрес следа расчёта одной суммы;
 *   {invoice}      полный адрес карточки счёта.
 * Правило подстановки: если значение из `screens.json` целиком начинается с
 * `{`, оно само по себе один из четырёх готовых полных адресов выше (не
 * считая {period}, который сам по себе адресом не бывает). Иначе это путь, и
 * внутри него меняются `{period}` и `{employee}`.
 */
import { readFile, writeFile } from "node:fs/promises";
import { APP, evalIn, goto, login, send } from "./guide_browser.mjs";
import { prepare } from "./guide_prepare.mjs";

const OUT = process.argv[2];
if (!OUT) {
  console.error("нужен путь: node tools/guide_shots.mjs <куда-складывать>");
  process.exit(1);
}

if (!process.env.SKIP_PREPARE) {
  await prepare();
}

// Вход и переключение языка — своим шагом, а не только внутри `prepare()`:
// `SKIP_PREPARE=1` пропускает подготовку целиком, и без этого вызова съёмка
// читала бы страницу входа вместо продукта. Повторный вход, если подготовка
// уже вошла, безвреден.
await login();

/** Строка периода на `/periods/` несёт свой id атрибутом `data-period`. */
async function periodIdByTitle(monthTitle) {
  await goto("/periods/");
  return await evalIn(`
    (() => {
      const row = [...document.querySelectorAll('tr[data-period]')]
        .find(tr => (tr.querySelector('a')?.textContent || '').trim() === ${JSON.stringify(monthTitle)});
      return row ? row.getAttribute('data-period') : '';
    })()
  `);
}

const periodId = await periodIdByTitle("Июнь 2026");
const openPeriodId = await periodIdByTitle("Июль 2026");
if (!periodId || !openPeriodId) {
  throw new Error(
    "утверждённый или открытый месяц не найден на /periods/ — прогоните " +
      "подготовку стенда (запустите без SKIP_PREPARE)",
  );
}
const openPeriodHref = `/periods/${openPeriodId}/`;

const employeeHref = await (async () => {
  await goto("/directory/employees/");
  return await evalIn(`(() => {
    const links = [...document.querySelectorAll('a[href^="/directory/employees/"]')].map(a => a.getAttribute('href'));
    return links.find(h => /[0-9a-f-]{36}/.test(h)) || '';
  })()`);
})();

const traceHref = await (async () => {
  await goto(`/periods/${periodId}/`);
  return await evalIn(`(() => {
    const a = document.querySelector('a[href*="/trace/"]');
    return a ? a.getAttribute('href') : '';
  })()`);
})();

const invoiceHref = await (async () => {
  await goto("/invoices/");
  return await evalIn(`(() => {
    const links = [...document.querySelectorAll('a[href^="/invoices/"]')].map(a => a.getAttribute('href'));
    return links.find(h => /^\\/invoices\\/[0-9a-f-]{36}\\/$/.test(h)) || '';
  })()`);
})();

for (const [label, href] of [
  ["карточка человека", employeeHref],
  ["след расчёта", traceHref],
  ["карточка счёта", invoiceHref],
]) {
  if (!href) {
    throw new Error(
      `не нашлось: ${label} — прогоните подготовку стенда (запустите без SKIP_PREPARE)`,
    );
  }
}

console.log(
  "период:", periodId, "| открытый месяц:", openPeriodId, "| карточка:", employeeHref,
);

const FULL_ADDRESS = {
  "{employee}": APP + employeeHref,
  "{open_period}": APP + openPeriodHref,
  "{trace}": APP + traceHref,
  "{invoice}": APP + invoiceHref,
};

// Список экранов — один на съёмку и на сборку гайда: `docs/guides/screens.json`.
// Своя копия здесь означала бы дубль, который разъезжается молча — гайд
// поправили, список поправили, а снимают по старому. Сторож этого не допустит
// (tests/test_guide_screens.py).
const listRaw = await readFile(new URL("../docs/guides/screens.json", import.meta.url), "utf8");
const shots = Object.entries(JSON.parse(listRaw))
  .filter(([name]) => !name.startsWith("_"))
  .map(([name, raw]) => {
    if (raw.startsWith("{")) {
      if (!(raw in FULL_ADDRESS)) {
        throw new Error(`неизвестная подстановка ${raw} у экрана ${name}`);
      }
      return [name, FULL_ADDRESS[raw]];
    }
    return [name, APP + raw.replace("{period}", periodId).replace("{employee}", employeeHref)];
  });

// Снимок обрезается по СОДЕРЖИМОМУ, а не по окну. Причина видна на любом
// коротком экране: `captureBeyondViewport` берёт не меньше высоты окна, и
// страница календаря на четыре строки приезжала картинкой, у которой две трети
// — пустой фон. В гайде это выглядит как обрезанный экран или как пустое место,
// то есть как дефект продукта, которого нет.
//
// Верхний предел нужен с другой стороны: реестр правил — это шесть тысяч
// пикселей таблицы, полтора мегабайта в одном снимке, из которых читатель
// видит верхние пятьсот (`figure img { max-height }` в самом гайде). Платить
// мегабайтами за невидимое незачем.
const WIDTH = 1280;
const MAX_HEIGHT = 2400;

let failed = 0;
for (const [name, url] of shots) {
  try {
    await goto(url);
    const height = await evalIn(`Math.ceil(Math.max(
      document.documentElement.scrollHeight,
      document.body ? document.body.scrollHeight : 0,
      320
    ))`);
    const shot = await send("Page.captureScreenshot", {
      format: "jpeg",
      quality: 72,
      captureBeyondViewport: true,
      // `scale: 2` повторяет `deviceScaleFactor` окна: с явным `clip` окно уже
      // не решает, и без этого снимки стали бы вдвое мельче остальных.
      clip: { x: 0, y: 0, width: WIDTH, height: Math.min(height, MAX_HEIGHT), scale: 2 },
    });
    await writeFile(`${OUT}/${name}.jpg`, Buffer.from(shot.data, "base64"));
    console.log("снято", name, `(${Math.min(height, MAX_HEIGHT)} px)`);
  } catch (e) {
    failed += 1;
    console.log("НЕ снято", name, String(e).slice(0, 90));
  }
}
// Дыра в снимках обязана быть видна тому, кто гоняет команду, а не только
// тому, кто потом открыл собранный гайд и нашёл пустое место (T098 — та же
// логика, что у смоуков: молчаливый провал дороже красного кода возврата).
process.exit(failed ? 1 : 0);
