/*
 * Снимки экранов для пользовательских материалов — гайда, инструкции, онбординга.
 *
 * Зачем скрипт, а не «сниму руками». Скриншот протухает молча и хуже текста:
 * текст противоречит экрану заметно, а картинка выглядит убедительно и врёт —
 * человек ищет кнопку, которой больше нет, и решает, что сломался он. Значит
 * пересъёмка обязана стоить одну команду, иначе после второй правки интерфейса
 * её перестанут делать (глобальное правило про пользовательские материалы).
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
 * Список экранов лежит в `docs/guides/screens.json` — один и тот же на съёмку и
 * на сборку страницы. Добавили экран в гайд — впишите его туда; забыть нельзя,
 * это стережёт `tests/test_guide_screens.py`.
 */
const APP = process.env.APP || "http://127.0.0.1:8000";
const OUT = process.argv[2];
const PORT = process.env.CDP_PORT || 9341;

const res = await fetch(`http://127.0.0.1:${PORT}/json/new?about:blank`, { method: "PUT" });
const target = await res.json();
const ws = new WebSocket(target.webSocketDebuggerUrl);
await new Promise((ok) => ws.addEventListener("open", ok));

let seq = 0;
const waiting = new Map();
ws.addEventListener("message", (e) => {
  const m = JSON.parse(e.data);
  if (m.id && waiting.has(m.id)) {
    const { ok, fail } = waiting.get(m.id);
    waiting.delete(m.id);
    m.error ? fail(new Error(JSON.stringify(m.error))) : ok(m.result);
  }
});
const send = (method, params = {}) => new Promise((ok, fail) => {
  const id = ++seq; waiting.set(id, { ok, fail });
  ws.send(JSON.stringify({ id, method, params }));
});
const evalIn = async (expr) => {
  const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.text);
  return r.result.value;
};
const goto = async (url) => {
  await send("Page.navigate", { url });
  for (let i = 0; i < 80; i++) {
    await new Promise((r) => setTimeout(r, 150));
    if (await evalIn("document.readyState === 'complete'").catch(() => false)) {
      await new Promise((r) => setTimeout(r, 400));
      return;
    }
  }
  throw new Error("не загрузилось: " + url);
};

await send("Page.enable");
await send("Runtime.enable");
await send("Emulation.setDeviceMetricsOverride", {
  width: 1280, height: 900, deviceScaleFactor: 2, mobile: false,
});

// вход: ждём форму, потом заполняем
await goto(`${APP}/login/`);
await evalIn(`(() => {
  const forms = [...document.querySelectorAll('form')];
  const f = forms.find(x => (x.getAttribute('action') || '').endsWith('/login/'));
  if (!f) throw new Error('формы входа нет');
  f.querySelector('[name=username]').value = '${process.env.USER_NAME || "admin"}';
  f.querySelector('[name=password]').value = '${process.env.USER_PASS || "admin"}';
  f.submit();
})()`);
await new Promise((r) => setTimeout(r, 2500));

// Демо всегда англоязычное (D035), а гайд для русской команды — переключаем
// язык штатным переключателем Django, как это делает человек в шапке.
await goto(`${APP}/periods/`);
await evalIn(`(() => {
  const form = document.createElement('form');
  form.method = 'post';
  form.action = '/i18n/setlang/';
  const csrf = document.cookie.match(/csrftoken=([^;]+)/)?.[1] || '';
  form.innerHTML = '<input name="csrfmiddlewaretoken" value="' + csrf + '">' +
                   '<input name="language" value="ru">' +
                   '<input name="next" value="/periods/">';
  document.body.appendChild(form);
  form.submit();
})()`);
await new Promise((r) => setTimeout(r, 2000));

const periodId = await (async () => {
  await goto(`${APP}/periods/`);
  return await evalIn(`(() => {
    const links = [...document.querySelectorAll('a[href^="/periods/"]')].map(a => a.getAttribute('href'));
    const withId = links.find(h => /\\/periods\\/[0-9a-f-]{36}\\//.test(h));
    return withId ? withId.split('/')[2] : '';
  })()`);
})();

const employeeUrl = await (async () => {
  await goto(`${APP}/directory/employees/`);
  return await evalIn(`(() => {
    const links = [...document.querySelectorAll('a[href^="/directory/employees/"]')].map(a => a.getAttribute('href'));
    return links.find(h => /[0-9a-f-]{36}/.test(h)) || '';
  })()`);
})();

console.log("период:", periodId, "| карточка:", employeeUrl);
// Список экранов — один на съёмку и на сборку гайда: `docs/guides/screens.json`.
// Своя копия здесь означала бы дубль, который разъезжается молча — гайд
// поправили, список поправили, а снимают по старому. Сторож этого не допустит
// (tests/test_guide_screens.py).
const fsSync = await import("node:fs/promises");
const listRaw = await fsSync.readFile(new URL("../docs/guides/screens.json", import.meta.url), "utf8");
const shots = Object.entries(JSON.parse(listRaw))
  .filter(([name]) => !name.startsWith("_"))
  .map(([name, path]) => [
    name,
    APP + path.replace("{period}", periodId).replace("{employee}", employeeUrl),
  ]);


const fs = await import("node:fs/promises");
for (const [name, url] of shots) {
  try {
    await goto(url);
    const shot = await send("Page.captureScreenshot", { format: "jpeg", quality: 72, captureBeyondViewport: true });
    await fs.writeFile(`${OUT}/${name}.jpg`, Buffer.from(shot.data, "base64"));
    console.log("снято", name);
  } catch (e) {
    console.log("НЕ снято", name, String(e).slice(0, 90));
  }
}
process.exit(0);
