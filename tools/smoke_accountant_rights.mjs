/*
 * Смоук прав бухгалтера: откат утверждения и закрытие часов точки (T115, D036).
 *
 * Проверяет нажатиями то, чего разбор разметки не доказывает: что бухгалтер
 * сам доводит месяц до утверждения и **сам** откатывает его с причиной — то
 * есть исправляет собственную опечатку, не идя за директором. Спека требует
 * этого от бухгалтера дословно, D036 приравнивает его доступ к директорскому.
 *
 * Вторая половина — часы точки: `unit.close` бухгалтеру открыт по тому же
 * решению, и на экране табеля у него есть форма закрытия и открытия обратно.
 *
 * Клики — настоящими событиями мыши (`Input.dispatch*`): обработчик, вызванный
 * напрямую, доказывает работоспособность обработчика, а не экрана.
 *
 * Стенд смоук приводит к сиду сам — и в начале, и после себя (договор в шапке
 * `cdp.mjs`).
 *
 *     COMPOSE_PROJECT_NAME=<стенд> APP=http://127.0.0.1:8080 CDP_PORT=9358 \
 *         node tools/smoke_accountant_rights.mjs
 */
import { attach, findPeriodAndGrid, loginWith, standFromSeed } from "./cdp.mjs";

const APP = process.env.APP || "http://127.0.0.1:8080";
const REASON = "смоук: опечатка в часах";

const { evalIn, goto, send, type, clickOn, check, report, logs } = await attach();
const login = loginWith(APP, evalIn, goto);

standFromSeed();

async function clickButton(text) {
  await clickOn(
    `[...document.querySelectorAll("button")]
       .find(b => b.textContent.includes(${JSON.stringify(text)}))`,
    `кнопка «${text}»`,
  );
  await new Promise((r) => setTimeout(r, 2000));
  return true;
}

const snapshot = () => evalIn(`
  (() => {
    const heading = [...document.querySelectorAll("h2")]
      .find(h => h.textContent.includes("История периода"));
    const table = heading ? heading.nextElementSibling : null;
    const facts = [...document.querySelectorAll("dl.facts dt")];
    const i = facts.findIndex(dt => dt.textContent.includes("Расчёт зарплаты"));
    const dd = document.querySelectorAll("dl.facts dd");
    return {
      approve: !!document.querySelector('form[action$="/approve/"]'),
      reopen: !!document.querySelector('form[action$="/reopen/"]'),
      payrunStatus: i >= 0 ? dd[i].textContent.trim() : "",
      history: table ? table.innerText : "",
      text: document.body.innerText,
    };
  })()
`);

// --- бухгалтер ведёт месяц целиком: посчитать → утвердить → откатить ---------

await login("accountant");
const { periodHref, gridHref } = await findPeriodAndGrid(APP, evalIn, goto);
await goto(APP + periodHref);

await clickButton("Посчитать период");
let page = await snapshot();
check("бухгалтер посчитал период сам", page.payrunStatus === "Посчитан", page.payrunStatus);

await clickButton("Утвердить период");
page = await snapshot();
check("бухгалтер утвердил период сам", page.payrunStatus === "Утверждён", page.payrunStatus);
check("кнопка отката ему предложена", page.reopen);
check(
  "и ему не пишут, что откат не входит в права",
  !page.text.includes("Откат периода не входит в права вашей роли"),
);

// Причина обязательна: набираем её настоящими нажатиями клавиш.
await clickOn(`document.querySelector('form[action$="/reopen/"] [name=reason]')`, "поле причины");
await type(REASON);
await clickButton("Открыть заново");
page = await snapshot();
check("период открыт заново", page.payrunStatus === "Открыт заново", page.payrunStatus);
check("история назвала причину", page.history.includes(REASON), page.history.slice(0, 120));
check("история назвала автором бухгалтера", page.history.includes("Бухгалтер"));

// --- часы точки: закрыть и открыть обратно -----------------------------------

await goto(APP + gridHref);
const forms = await evalIn(`document.querySelectorAll("input[name=unit]").length`);
check("бухгалтеру предложены формы закрытия точек", forms > 0, String(forms));
check(
  "и ему не пишут, что закрытие не входит в права",
  !(await evalIn(`document.body.innerText`)).includes(
    "Закрытие часов по точке не входит в права вашей роли",
  ),
);

check("бухгалтер закрыл часы точки нажатием", await clickButton("Закрыть часы"));
let state = await evalIn(`document.body.innerText`);
check("часы точки закрыты", /часы закрыты/.test(state), state.slice(0, 0) || "");

check("бухгалтер открыл часы обратно", await clickButton("Открыть заново"));
state = await evalIn(`document.body.innerText`);
check("часы точки снова открыты", /часы открыты/.test(state));

if (logs.length) console.log("журнал консоли: " + logs.join(" | "));
report();
