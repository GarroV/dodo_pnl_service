/*
 * Управление headless-браузером по протоколу отладки Chrome.
 *
 * Общая часть смоуков табеля: страница, настоящие события клавиатуры, журнал
 * консоли и счёт проверок. Вынесена отдельно, потому что смоуков стало два, а
 * копия харнесса в каждом — верный способ чинить один и не замечать другой.
 *
 * Внешних зависимостей нет: WebSocket встроен в Node.
 *
 * ============================================================================
 * Договор смоука о состоянии стенда (T098)
 * ============================================================================
 *
 * Проверка, которая краснеет по своей вине, обесценивает всё остальное: в
 * следующий раз настоящую поломку спишут на тот же шум. За один прогон стройки
 * так было трижды — см. issues #76, #80, #81. Поэтому три правила, и они
 * обязательны для каждого файла `smoke_*.mjs`:
 *
 * 1. **Смоук сам приводит стенд в то состояние, которое ему нужно** — вызовом
 *    `seedStand()` в начале, а не рассчитывая на порядок запуска. Раньше
 *    `smoke_period_lifecycle` требовал непосчитанный период и краснел, если
 *    период посчитал кто-то до него, а `smoke_closing` объявили протухшим,
 *    хотя он был засорён ячейкой, оставленной `smoke_permissions`.
 *
 * 2. **Смоук ничего за собой не оставляет** — `seedStand()` зарегистрирован
 *    через `onCleanup`, то есть выполняется и при падении на полпути. Всё, что
 *    сид вернуть не может (правила стран лежат вне тенанта, остановленный
 *    рабочий процесс очереди, скачанные файлы), возвращается своим
 *    `onCleanup` — тем же способом.
 *
 * 3. **Смоук задаёт себе окно браузера сам** (`Emulation.setDeviceMetricsOverride`
 *    ниже). Chrome без `--window-size` даёт 800×600; клик по координатам из
 *    `getBoundingClientRect` промахивался мимо кнопки, и выглядело это как
 *    сломанный фоновый расчёт, а не как низкое окно.
 */
import { execFileSync } from "node:child_process";

// Корень репозитория: смоук могут запустить из любого каталога, а `docker
// compose` обязан читать тот же `docker-compose.yml` и тот же `.env`, что и
// человек.
export const ROOT = new URL("..", import.meta.url).pathname;

// Окно, в котором смоуки договорились работать. 900 в высоту, а не «побольше
// на всякий случай»: часть проверок спрашивает, попала ли плашка в окно, и на
// окне высотой в три экрана этот вопрос перестал бы что-либо значить.
export const VIEWPORT = { width: 1440, height: 900 };

// --- уборка ------------------------------------------------------------------
// Функции синхронные намеренно: они обязаны успеть выполниться при аварийном
// выходе, а асинхронную работу Node в этот момент уже не ждёт.
const cleanups = [];
let cleanupsDone = false;

/** Зарегистрировать уборку. Выполняется в обратном порядке, в том числе при падении. */
export function onCleanup(what, fn) {
  cleanups.unshift({ what, fn });
}

function runCleanups() {
  if (cleanupsDone) return;
  cleanupsDone = true;
  for (const { what, fn } of cleanups) {
    try {
      fn();
      console.log(`убрано: ${what}`);
    } catch (error) {
      // Не убранное молча — это следующий красный прогон на исправном
      // продукте. Поэтому вслух и с ненулевым кодом возврата.
      console.log(`НЕ УБРАНО: ${what} — ${error.message}`);
      process.exitCode = 1;
    }
  }
}

// `exit` срабатывает и на обычном завершении, и после необработанного
// исключения (в том числе из top-level await) — а `finally` в теле смоука не
// срабатывает ни там, ни там, если падение случилось в другом месте файла.
process.on("exit", runCleanups);

// --- стенд -------------------------------------------------------------------

/** Имя compose-проекта стенда. Без него уборка трогала бы чужой стенд. */
function standProject() {
  const name = process.env.COMPOSE_PROJECT_NAME || "";
  if (!name) {
    throw new Error(
      "не задан COMPOSE_PROJECT_NAME: смоук приводит стенд к сиду и обязан " +
        "знать, какой именно стенд. Без имени compose взял бы проект по " +
        "умолчанию — то есть чужой запущенный продукт",
    );
  }
  return name;
}

function compose(args, options = {}) {
  standProject();
  return execFileSync("docker", ["compose", ...args], {
    cwd: ROOT, encoding: "utf8", env: process.env, ...options,
  });
}

/**
 * Вернуть стенд к эталону: `manage.py seed_dev`.
 *
 * Ролью владельца схемы (служба `migrate`), а не ролью продукта: сид сносит и
 * заводит строки заново, и политики базы роль продукта в это не пустят.
 *
 * Идемпотентен по построению — тенант пересобирается целиком, идентификаторы
 * детерминированные. Значит вызов в начале смоука даёт известный вход, а вызов
 * в уборке — отсутствие следов.
 */
export function seedStand() {
  compose(["run", "--rm", "migrate", "python", "manage.py", "seed_dev"], {
    stdio: "pipe",
  });
}

/**
 * Привести стенд к эталону сейчас и обязательно вернуть к нему после.
 *
 * Одной строкой в начале смоука, потому что порознь эти два вызова забываются
 * по одному: первый — и смоук зависит от предыдущего, второй — и от него
 * зависит следующий.
 */
export function standFromSeed() {
  onCleanup("стенд возвращён к сиду", seedStand);
  seedStand();
}

/** Оператор SQL на базе стенда. Читающий — вернёт вывод psql построчно. */
export function sql(statement, { quiet = true } = {}) {
  const user = process.env.POSTGRES_USER || "app";
  const database = process.env.POSTGRES_DB || "dodo_pnl";
  const flags = quiet ? ["-q", "-tA"] : ["-tA"];
  return compose(
    ["exec", "-T", "db", "psql", ...flags, "-U", user, "-d", database, "-c", statement],
    { stdio: ["ignore", "pipe", "pipe"] },
  ).trim();
}

/** Служба compose: остановить и поднять. Нужно смоуку очереди. */
export function service(action, name) {
  compose([action, name], { stdio: "pipe" });
}

export async function attach({ cdpPort = process.env.CDP_PORT || 9339 } = {}) {
  const CDP = `http://127.0.0.1:${cdpPort}`;
  const res = await fetch(`${CDP}/json/new?about:blank`, { method: "PUT" });
  const target = await res.json();
  const ws = new WebSocket(target.webSocketDebuggerUrl);
  await new Promise((ok) => ws.addEventListener("open", ok));

  let seq = 0;
  const waiting = new Map();
  const logs = [];

  ws.addEventListener("message", (event) => {
    const msg = JSON.parse(event.data);
    if (msg.id && waiting.has(msg.id)) {
      const { ok, fail } = waiting.get(msg.id);
      waiting.delete(msg.id);
      msg.error ? fail(new Error(JSON.stringify(msg.error))) : ok(msg.result);
    }
    if (msg.method === "Runtime.consoleAPICalled") {
      logs.push(msg.params.args.map((a) => a.value ?? a.description).join(" "));
    }
    if (msg.method === "Runtime.exceptionThrown") {
      logs.push("EXCEPTION " + JSON.stringify(msg.params.exceptionDetails.text));
    }
  });

  const send = (method, params = {}) =>
    new Promise((ok, fail) => {
      const id = ++seq;
      waiting.set(id, { ok, fail });
      ws.send(JSON.stringify({ id, method, params }));
    });

  const evalIn = async (expr) => {
    const r = await send("Runtime.evaluate", {
      expression: expr,
      returnByValue: true,
      awaitPromise: true,
    });
    if (r.exceptionDetails) throw new Error(r.exceptionDetails.text + " :: " + expr);
    return r.result.value;
  };

  const goto = async (url) => {
    await send("Page.navigate", { url });
    // Ждём готовности разметки и подъёма htmx.
    for (let i = 0; i < 60; i++) {
      await new Promise((r) => setTimeout(r, 150));
      const ready = await evalIn("document.readyState === 'complete'").catch(() => false);
      if (ready) return;
    }
    throw new Error("страница не загрузилась: " + url);
  };

  const key = async (text, code, keyCode) => {
    const base = { key: text, code, windowsVirtualKeyCode: keyCode, nativeVirtualKeyCode: keyCode };
    await send("Input.dispatchKeyEvent", { type: "keyDown", ...base });
    await send("Input.dispatchKeyEvent", { type: "keyUp", ...base });
  };

  const type = async (text) => {
    for (const ch of text) {
      await send("Input.dispatchKeyEvent", { type: "keyDown", text: ch, key: ch });
      await send("Input.dispatchKeyEvent", { type: "keyUp", key: ch });
    }
  };

  /**
   * Нажать мышью на элемент, найденный выражением JS.
   *
   * Прокрутка до элемента — часть нажатия, а не забота вызывающего: координаты
   * из `getBoundingClientRect` считаются от области просмотра, и элемент,
   * оставшийся за её краем, получает клик в пустоту. Молча: страница ничего не
   * скажет, а красной станет следующая проверка (issue #81).
   *
   * Поэтому здесь два отказа вместо промаха: «нечего нажимать» и «не влезает в
   * окно». Оба называют, что искали, — красный смоук должен читаться без
   * повторного запуска.
   */
  const clickOn = async (finder, what = finder) => {
    const box = await evalIn(`
      (() => {
        const el = (${finder});
        if (!el) return null;
        el.scrollIntoView({ block: "center", inline: "center" });
        const r = el.getBoundingClientRect();
        return {
          x: Math.round(r.x + r.width / 2),
          y: Math.round(r.y + r.height / 2),
          inWindow: r.top >= 0 && r.bottom <= innerHeight
                 && r.left >= 0 && r.right <= innerWidth,
          window: innerWidth + "x" + innerHeight,
        };
      })()
    `);
    if (!box) throw new Error(`нечего нажимать: ${what}`);
    if (!box.inWindow) {
      throw new Error(
        `${what}: элемент не удалось привести в окно ${box.window} — ` +
          "клик по координатам промахнулся бы молча",
      );
    }
    for (const type of ["mousePressed", "mouseReleased"]) {
      await send("Input.dispatchMouseEvent", {
        type, x: box.x, y: box.y, button: "left", clickCount: 1,
      });
    }
    return box;
  };

  const checks = [];
  const check = (name, ok, detail = "") => {
    checks.push({ name, ok, detail });
    console.log(`${ok ? "OK  " : "FAIL"}  ${name}${detail ? " — " + detail : ""}`);
  };

  const report = () => {
    console.log("\n" + "=".repeat(60));
    const failed = checks.filter((c) => !c.ok);
    console.log(`${checks.length - failed.length} из ${checks.length} проверок прошли`);
    if (failed.length) {
      console.log("ПРОВАЛЕНО: " + failed.map((c) => c.name).join("; "));
      process.exitCode = 1;
    }
    ws.close();
  };

  await send("Page.enable");
  await send("Runtime.enable");
  await send("Network.enable");

  // Браузер тоже состояние, и его тоже портили друг другу. Выбор языка живёт в
  // cookie и переживает смоук: смоук локализации оставлял английский, а
  // соседние написаны по-русски и сверяют русские надписи — и валились все
  // разом, выглядя как сломанный продукт. Чистим на входе, а не просим каждого
  // прибраться на выходе: уборка на выходе не срабатывает ровно тогда, когда
  // она нужна, — при падении на полпути.
  await send("Network.clearBrowserCookies");

  // Кэш браузера — то же самое состояние, и оно врёт тише всех. Chrome держит
  // отданный `runserver` статический файл в своём кэше и после того, как файл
  // на сервере изменился: смоук ходит по новой странице со **вчерашним**
  // `grid.js` и честно рассказывает про поведение, которого в репозитории уже
  // нет. Поймано фактически при T123 (issue #105): проверка досылки покраснела
  // на исправленном коде, потому что браузер держал версию, оставшуюся от
  // проверки порчей часом раньше; `curl` того же адреса отдавал правильный
  // файл. Проверка, зависящая от кэша, — это проверка, которой нельзя верить
  // ни в зелёном, ни в красном.
  await send("Network.setCacheDisabled", { cacheDisabled: true });

  // Окно задаёт смоук, а не тот, кто запустил браузер (issue #81). Chrome без
  // `--window-size` даёт 800×600: кнопка «Посчитать период» уходит за нижний
  // край, клик по координатам из `getBoundingClientRect` попадает в пустоту, и
  // краснеет следующая проверка — «ни фразы «Расчёт выполнен», ни полосы
  // прогресса за 10 секунд». Выглядит как сломанный фоновый расчёт при
  // исправном продукте.
  //
  // Смоук, которому нужно другое окно, переопределяет его после `attach` своим
  // вызовом — этот лишь снимает зависимость от запускающего.
  await send("Emulation.setDeviceMetricsOverride", {
    width: VIEWPORT.width, height: VIEWPORT.height,
    deviceScaleFactor: 1, mobile: false,
  });

  // Язык, на котором смоук разговаривает с продуктом (T017). Задаётся явно, а
  // не берётся у браузера: с появлением локализации продукт честно отвечает на
  // языке, который просит браузер, а headless Chrome по умолчанию просит
  // английский. Смоуки при этом написаны по-русски и сверяют русские надписи —
  // без этой строки они начали бы падать все разом, и выглядело бы это как
  // сломанный продукт, а не как несогласованный язык.
  //
  // Cookie сильнее заголовка, поэтому смоук локализации по-прежнему может
  // переключать язык нажатием кнопки, и эта настройка ему не мешает.
  await send("Network.setExtraHTTPHeaders", {
    headers: { "Accept-Language": process.env.SMOKE_LANG || "ru,ru-RU;q=0.9" },
  });

  return { send, evalIn, goto, key, type, clickOn, check, checks, report, logs, ws };
}

/** Вход настоящим логином и паролем — тем путём, которым пойдёт человек. */
export function loginWith(app, evalIn, goto) {
  return async (who, password = "dodo-dev") => {
    await goto(`${app}/login/`);
    await evalIn(`
      (() => {
        const form = document.querySelector('form[action="/login/"]');
        form.querySelector('[name=username]').value = ${JSON.stringify(who)};
        form.querySelector('[name=password]').value = ${JSON.stringify(password)};
        form.submit();
      })()
    `);
    await new Promise((r) => setTimeout(r, 1500));
  };
}

/**
 * Довести период до посчитанного — если он ещё не посчитан.
 *
 * Нужно смоукам, которые период не считают, а только смотрят на его результат
 * (каркас интерфейса, языки). Раньше они молча рассчитывали на то, что период
 * посчитал кто-то до них, и в одиночку краснели на пустой ведомости — при
 * исправном продукте. Сид период не считает намеренно: непосчитанный месяц —
 * это тоже состояние продукта, и половине смоуков нужно именно оно.
 *
 * Нажатием кнопки, а не запросом мимо экрана: считать период умеет только тот,
 * у кого есть право, и подготовка должна идти тем же путём, что у человека.
 */
export async function ensureCalculated(app, { evalIn, goto, clickOn }, seconds = 120) {
  const { periodHref } = await findPeriodAndGrid(app, evalIn, goto);
  await goto(app + periodHref);
  if (await evalIn(`!!document.querySelector("table.sheet")`)) return periodHref;

  const button = `[...document.querySelectorAll("button")]
      .find(b => b.textContent.includes("Посчитать период"))`;
  await clickOn(button, "кнопка «Посчитать период» (подготовка стенда)");
  for (let i = 0; i < seconds * 2; i++) {
    await new Promise((r) => setTimeout(r, 500));
    if (await evalIn(`!!document.querySelector("table.sheet")`)) return periodHref;
  }
  throw new Error(
    `период не посчитался за ${seconds} с — смоуку не с чем работать; ` +
      "запущен ли рабочий процесс очереди?",
  );
}

/** Адреса периода и его табеля — так же, как их берёт человек со страниц. */
export async function findPeriodAndGrid(app, evalIn, goto) {
  await goto(`${app}/periods/`);
  const periodHref = await evalIn(
    `[...document.querySelectorAll('a[href^="/periods/"]')]
       .map(a => a.getAttribute('href'))
       .find(h => /^\\/periods\\/[0-9a-f-]{36}\\/$/.test(h))`
  );
  await goto(app + periodHref);
  const gridHref = await evalIn(
    `document.querySelector('a[href^="/timesheets/"]').getAttribute('href')`
  );
  return { periodHref, gridHref };
}
