/*
 * Смоук экрана ролей: выход из тупика, в который упёрся владелец (T171, T172).
 *
 * Проверяет нажатиями то, чего разбор разметки не доказывает: администратор
 * читает на отказе, что делать, доходит до экрана ролей ссылкой из шапки,
 * отмечает право галочкой, сохраняет — и человек, которому оно выдано,
 * действительно перестаёт получать отказ.
 *
 * Почему именно так, а не проверкой базы: до этой задачи продукт отправлял
 * администратора «попросить того, у кого право есть», и просить было некого.
 * Проверять надо ровно тот путь, которого не было.
 *
 * Стенд смоук приводит к сиду сам — и в начале, и после себя (договор в шапке
 * `cdp.mjs`).
 *
 *     COMPOSE_PROJECT_NAME=dodo-pnl-roles APP=http://127.0.0.1:8096 CDP_PORT=9361 \
 *         node tools/smoke_roles.mjs
 */
import { attach, loginWith, standFromSeed } from "./cdp.mjs";

const APP = process.env.APP || "http://127.0.0.1:8096";

const { evalIn, goto, clickOn, check, report, logs } = await attach();
const login = loginWith(APP, evalIn, goto);

standFromSeed();

const text = () => evalIn(`document.body.innerText`);

// ── 1. Отказ администратору называет выход, а не «попросите кого-нибудь» ────
await login("admin");
await goto(`${APP}/periods/`);
const periodHref = await evalIn(`
  (() => {
    const link = [...document.querySelectorAll('a')]
      .map(a => a.getAttribute('href'))
      .find(h => h && /^\\/periods\\/[0-9a-f-]+\\/$/.test(h));
    return link || "";
  })()
`);
check("на списке периодов есть период", Boolean(periodHref));

await goto(APP + periodHref);
const refusal = await text();
check(
  "отказ администратору называет выход",
  refusal.includes("откройте «Роли и права» и выдайте его"),
);
check("и не отправляет просить некого", !refusal.includes("Попросите того"));

// ── 2. Раздел «Роли» есть в шапке и открывается ────────────────────────────
await clickOn(
  `[...document.querySelectorAll("nav a")].find(a => a.textContent.trim() === "Роли")`,
  "ссылка «Роли» в шапке",
);
await new Promise((r) => setTimeout(r, 800));
const screen = await text();
check("открылся экран ролей", screen.includes("Роли и права"));
check("права показаны словами", screen.includes("Расчёт периода"));
check("люди партнёра видны по именам", screen.includes("Бухгалтер"));

// ── 3. Право выдаётся галочкой и сохраняется ───────────────────────────────
const before = await evalIn(`
  (() => {
    const row = [...document.querySelectorAll("tr")]
      .find(tr => tr.textContent.includes("Управляющий точки"));
    if (!row) return "нет строки роли";
    const box = row.querySelector('input[name="right:payrun.calculate"]');
    return box ? String(box.checked) : "нет галочки";
  })()
`);
check("у управляющего расчёт не отмечен", before === "false");

await clickOn(
  `(() => {
     const row = [...document.querySelectorAll("tr")]
       .find(tr => tr.textContent.includes("Управляющий точки"));
     return row.querySelector('input[name="right:payrun.calculate"]');
   })()`,
  "галочка «Расчёт периода» у управляющего",
);
await clickOn(
  `(() => {
     const row = [...document.querySelectorAll("tr")]
       .find(tr => tr.textContent.includes("Управляющий точки"));
     return row.querySelector("button[type=submit]");
   })()`,
  "кнопка «Сохранить»",
);
await new Promise((r) => setTimeout(r, 1200));
check("продукт сказал, что сохранил", (await text()).includes("сохранены"));

// ── 4. И право действительно действует у того, кому выдано ─────────────────
await login("manager");
await goto(APP + periodHref);
const managerSees = await text();
check(
  "управляющему расчёт больше не отказан",
  !managerSees.includes("Расчёт периода не входит в права вашей роли"),
);

report(logs);
