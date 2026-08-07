/*
 * Управление headless-браузером по протоколу отладки Chrome.
 *
 * Общая часть смоуков табеля: страница, настоящие события клавиатуры, журнал
 * консоли и счёт проверок. Вынесена отдельно, потому что смоуков стало два, а
 * копия харнесса в каждом — верный способ чинить один и не замечать другой.
 *
 * Внешних зависимостей нет: WebSocket встроен в Node.
 */

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

  return { send, evalIn, goto, key, type, check, checks, report, logs, ws };
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
