/*
 * Общий драйвер headless-браузера по протоколу отладки Chrome.
 *
 * Вынесен из `guide_shots.mjs`, потому что теперь его просит и подготовка
 * стенда (`guide_prepare.mjs`): снимку и наполнению нужен один и тот же набор
 * примитивов — открыть страницу, заполнить форму, войти паролем. Копия этого
 * кода в каждом файле — верный способ однажды починить один и не заметить
 * второй (тот же довод, что увёл общий харнесс смоуков в `cdp.mjs`).
 *
 * Внешних зависимостей нет: `WebSocket` встроен в Node.
 *
 * Модуль подключается к CDP ОДИН РАЗ при импорте — открывает одну вкладку на
 * весь прогон. Node кеширует модуль по пути: `guide_prepare.mjs` и
 * `guide_shots.mjs` внутри одного процесса получают одну и ту же вкладку и
 * одну и ту же сессию входа, поэтому подготовка стенда и съёмка работают в
 * одном окне — так же, как это было бы у человека, а не в двух браузерах,
 * которые друг о друге не знают.
 *
 * Читает окружение:
 *   APP        адрес продукта (умолчание http://127.0.0.1:8000)
 *   CDP_PORT   порт отладки Chrome (умолчание 9341)
 *   USER_NAME  логин для входа (умолчание admin)
 *   USER_PASS  пароль для входа (умолчание admin)
 */

export const APP = process.env.APP || "http://127.0.0.1:8000";
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

/** Сырая команда протокола отладки — низкий уровень, для остальных функций. */
export const send = (method, params = {}) => new Promise((ok, fail) => {
  const id = ++seq; waiting.set(id, { ok, fail });
  ws.send(JSON.stringify({ id, method, params }));
});

/** Выполнить выражение JS на странице и вернуть его значение. */
export const evalIn = async (expr) => {
  const r = await send("Runtime.evaluate", { expression: expr, returnByValue: true, awaitPromise: true });
  if (r.exceptionDetails) throw new Error(r.exceptionDetails.text);
  return r.result.value;
};

/** Видимый текст страницы — то же самое, что видит человек глазами. */
export const text = () => evalIn("document.body.innerText");

/**
 * Перейти по адресу и дождаться загрузки.
 *
 * Принимает и путь («/periods/»), и полный адрес («http://127.0.0.1:8090/…») —
 * вызывающему не нужно самому решать, склеивать ли `APP` спереди.
 */
export const goto = async (url) => {
  const full = /^https?:\/\//.test(url) ? url : APP + url;
  await send("Page.navigate", { url: full });
  for (let i = 0; i < 80; i++) {
    await new Promise((r) => setTimeout(r, 150));
    if (await evalIn("document.readyState === 'complete'").catch(() => false)) {
      await new Promise((r) => setTimeout(r, 400));
      return;
    }
  }
  throw new Error("не загрузилось: " + full);
};

await send("Page.enable");
await send("Runtime.enable");
await send("Emulation.setDeviceMetricsOverride", {
  width: 1280, height: 900, deviceScaleFactor: 2, mobile: false,
});

/**
 * Вход штатной формой + переключение языка интерфейса на русский.
 *
 * Демо всегда англоязычное (D035), а материалы для команды — на русском:
 * headless Chrome по умолчанию просит английский, и без явного переключения
 * все снимки и все проверки видимого текста читали бы чужой язык.
 */
export async function login(user = process.env.USER_NAME || "admin",
                             pass = process.env.USER_PASS || "admin") {
  await goto("/login/");
  await evalIn(`(() => {
    const forms = [...document.querySelectorAll('form')];
    const f = forms.find(x => (x.getAttribute('action') || '').endsWith('/login/'));
    if (!f) throw new Error('формы входа нет');
    f.querySelector('[name=username]').value = ${JSON.stringify(user)};
    f.querySelector('[name=password]').value = ${JSON.stringify(pass)};
    f.submit();
  })()`);
  await new Promise((r) => setTimeout(r, 2500));

  await goto("/periods/");
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
}
