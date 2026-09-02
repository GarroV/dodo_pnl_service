/*
 * Смоук: приглашённый управляющий точки получает СВОЮ точку, а не все точки
 * партнёра (issue #178/#188, только что исправленный дефект видимости).
 *
 * История дефекта. Экран ролей заводил членство (`Membership`) без `unit_ids`
 * вовсе — форма приглашения точку не спрашивала. В функциях контекста базы
 * (`0264`) `unit_ids is null` означает не «нет точек», а «ВСЕ точки тенанта»:
 * приглашённый управляющий незаметно получал кассы, наличные, табели и
 * надбавки всего партнёра — вопреки собственной роли и вопреки D031. Починка
 * добавила в обе формы экрана (`/roles/`) выбор точки и отказ словами, если
 * она не выбрана или роль её не принимает (`_membership_units` в
 * `roles_views.py`).
 *
 * Что здесь проверяется такого, чего не докажут `tests/test_roles_views.py`
 * или юнит-тесты `_membership_units`. Те гоняют Django test client — то есть
 * форму, отрисованную шаблоном, но НЕ значения атрибутов `value` в реальном
 * `<select>`, которые видит браузер, и не то, что реальный клик по «Пригласить»
 * действительно уходит тем POST'ом, которого ждёт представление. Здесь —
 * настоящий вход, настоящий `<select name="unit">`, настоящий клик, и после
 * него — прямой запрос к базе: не «форма приняла точку», а «в
 * `memberships.unit_ids` легла ровно одна точка, и это именно NS1», а не
 * `NULL` (та самая маскировка «всех точек», из-за которой дефект был не виден
 * ни на одном экране).
 *
 * Известный дефект #206: `goto()` харнесса иногда решает, что страница готова,
 * по `document.readyState` ещё ПРЕЖНЕЙ страницы. Здесь после переходов на новый
 * адрес ждём текст/элемент, которых на прежней странице точно не было; а после
 * отправки форм — либо смену пути (успех уводит на `/roles/`), либo появление
 * ИМЕННО того текста отказа, которого до отправки не было (оба отказа остаются
 * на том же пути `/roles/invite/`, поэтому смена пути тут ничего не докажет).
 *
 * Стенд смоук приводит к сиду сам и возвращает после себя (договор в шапке
 * `cdp.mjs`) — в том числе если упадёт на полпути.
 *
 *     COMPOSE_PROJECT_NAME=dodo-pnl-roles4 APP=http://127.0.0.1:8090 CDP_PORT=9391 \
 *         SMOKE_SHOTS=/путь/к/снимкам node tools/smoke_roles_unit_scope.mjs
 */
import { mkdirSync, writeFileSync } from "node:fs";

import { attach, loginWith, onCleanup, sql, standFromSeed } from "./cdp.mjs";

const APP = process.env.APP || "http://127.0.0.1:8090";
const SHOTS = process.env.SMOKE_SHOTS || "/tmp";
mkdirSync(SHOTS, { recursive: true });

const { evalIn, goto, send, clickOn, check, report, logs } = await attach();
const login = loginWith(APP, evalIn, goto);

// Придуманные люди — не настоящие ФИО партнёра (репозиторий публичный), в том
// же духе, что и `seed_dev.py` (Марко, Ана, Джордже).
const MANAGER_NAME = "Стеван Симич";
const MANAGER_EMAIL = "stevan.simic@example.test";
const NO_UNIT_NAME = "Никола Радович";
const NO_UNIT_EMAIL = "nikola.radovic@example.test";
const ACCOUNTANT_NAME = "Драгана Петрович";
const ACCOUNTANT_EMAIL = "dragana.petrovic@example.test";

// Стенд к эталону сейчас и обратно к нему после — в том числе при падении
// на полпути (issue #76).
standFromSeed();

// Заведённых этим смоуком людей `standFromSeed()` НЕ убирает: `users` живёт
// вне тенанта, и `seed_dev` намеренно сносит только свои детерминированные
// учётки (комментарий в самом сиде — «чужие в этой базе не наши, и трогать их
// сид не вправе»), а не произвольные, заведённые формой приглашения. Без
// этой уборки прогон, оставленный за собой хоть раз, портит все следующие: их
// `memberships` тенант-сброс снесёт, а строка `users` останется — и повторное
// приглашение той же почты вместо «уже заведён» (409, как задумано) падает
// сырой `IntegrityError` (500): проверка на дубликат читает `users` под RLS, а
// осиротевшая без единого членства строка для этой проверки невидима, хотя
// физически на месте и держит уникальность логина. Поймано фактически —
// первый же повторный прогон уронил и этот смоук, и представление разом.
const TEST_EMAILS = [MANAGER_EMAIL, NO_UNIT_EMAIL, ACCOUNTANT_EMAIL];
function wipeTestPeople() {
  for (const email of TEST_EMAILS) sql(`delete from users where email = '${email}'`);
}
onCleanup("тестовые учётки смоука удалены", wipeTestPeople);
wipeTestPeople();

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

/** Снимок экрана — доказательство глазами, а не только разметкой. */
async function shot(name) {
  const { data } = await send("Page.captureScreenshot", { format: "png" });
  const path = `${SHOTS}/${name}.png`;
  writeFileSync(path, Buffer.from(data, "base64"));
  console.log(`     снимок: ${path}`);
}

/** Вход, доведённый до конца: шапка называет роль не сразу, особенно первым
 *  запросом сразу после `seed_dev`, который только что переписал тенант. */
async function signIn(who) {
  for (let attempt = 0; attempt < 10; attempt++) {
    await login(who);
    if (await evalIn(`!!document.querySelector(".who")`)) return;
    await sleep(500);
  }
  throw new Error(`не удалось войти: ${who}`);
}

/** Переход, который ждёт именно НОВУЮ страницу (issue #206), а не готовность
 *  прежней: `ready` — JS-выражение, истинное только на месте назначения. */
async function visit(url, ready, seconds = 20) {
  await goto(url);
  for (let i = 0; i < seconds * 4; i++) {
    if (await evalIn(ready).catch(() => false)) return;
    await sleep(250);
  }
  throw new Error(`не дождались страницы: ${url}`);
}

/** Ждёт, пока на странице не появится конкретный текст — и для успеха, и для
 *  отказа. Одного `location.pathname` здесь недостаточно (issue #206): адрес
 *  меняется, как только браузер начал переход на новый ответ, а тело страницы
 *  в этот момент бывает ещё не дорисовано (экран ролей рисует три таблицы
 *  разом — права, люди, историю до 50 записей) — поймано фактически: тот же
 *  прогон однажды дал пустые значения там, где мгновением позже они были на
 *  месте. Для отказов дело ещё хуже: путь формы `/roles/invite/` после провала
 *  остаётся ТЕМ ЖЕ САМЫМ, и смена пути там вообще ничего не доказывает.
 *  Доказывает переход именно текст ответа — снимок, которого на прежней
 *  странице точно не было. */
async function waitForText(snippet, seconds = 20) {
  for (let i = 0; i < seconds * 4; i++) {
    const found = await evalIn(
      `document.body.innerText.includes(${JSON.stringify(snippet)})`,
    ).catch(() => false);
    if (found) return true;
    await sleep(250);
  }
  return false;
}

const bodyText = () => evalIn(`document.body.innerText`);

const INVITE_FORM = `document.querySelector('form[action="/roles/invite/"]')`;
const inviteSubmitButton = `${INVITE_FORM}.querySelector('button[type=submit]')`;

/** Заполнить форму «Пригласить человека» и нажать «Пригласить» настоящим
 *  кликом. Значения полей проставляются как их проставил бы человек, включая
 *  реальный `<select>` — именно его атрибут `value` формой и читается
 *  (`request.POST.get("unit")`), поэтому подделать это разбором шаблона нельзя. */
async function submitInvite({ fullName, email, roleId, unitId, reason }) {
  await evalIn(`
    (() => {
      const form = ${INVITE_FORM};
      form.querySelector('[name=full_name]').value = ${JSON.stringify(fullName)};
      form.querySelector('[name=email]').value = ${JSON.stringify(email)};
      form.querySelector('[name=role]').value = ${JSON.stringify(roleId)};
      form.querySelector('[name=unit]').value = ${JSON.stringify(unitId || "")};
      form.querySelector('[name=reason]').value = ${JSON.stringify(reason)};
    })()
  `);
}

// =============================================================================
// 1. Вход admin, экран ролей открывается
// =============================================================================

await signIn("admin");
await visit(
  `${APP}/roles/`,
  `document.querySelector("h1")?.textContent.trim() === "Роли и права"`,
);
check(
  "администратор открывает экран ролей",
  (await evalIn(`document.querySelector("h1")?.textContent.trim()`)) === "Роли и права",
);

// =============================================================================
// 2. Форма «Пригласить человека» содержит выбор точки
// =============================================================================

check(
  "в форме приглашения есть select[name=unit]",
  await evalIn(`!!${INVITE_FORM}.querySelector('select[name="unit"]')`),
);

const managerRoleId = await evalIn(`
  (() => {
    const opt = [...${INVITE_FORM}.querySelectorAll('select[name=role] option')]
      .find(o => o.textContent.trim() === "Управляющий точки");
    return opt ? opt.value : null;
  })()
`);
check("в списке ролей формы есть «Управляющий точки»", !!managerRoleId, managerRoleId);

const accountantRoleId = await evalIn(`
  (() => {
    const opt = [...${INVITE_FORM}.querySelectorAll('select[name=role] option')]
      .find(o => o.textContent.trim() === "Бухгалтер");
    return opt ? opt.value : null;
  })()
`);
check("в списке ролей формы есть «Бухгалтер»", !!accountantRoleId, accountantRoleId);

const ns1UnitId = await evalIn(`
  (() => {
    const opt = [...${INVITE_FORM}.querySelectorAll('select[name=unit] option')]
      .find(o => o.textContent.trim().startsWith("NS1"));
    return opt ? opt.value : null;
  })()
`);
check("в списке точек формы есть NS1", !!ns1UnitId, ns1UnitId);

// =============================================================================
// 3. Приглашение управляющего С точкой NS1 проходит
// =============================================================================

await submitInvite({
  fullName: MANAGER_NAME, email: MANAGER_EMAIL, roleId: managerRoleId,
  unitId: ns1UnitId, reason: "смоук: разграничение по точкам",
});
const selectedUnitTitle = await evalIn(`
  ${INVITE_FORM}.querySelector('select[name=unit]').selectedOptions[0]?.textContent.trim()
`);
check("в форме перед отправкой видна выбранная точка NS1", selectedUnitTitle?.startsWith("NS1"), selectedUnitTitle);
// Форма приглашения — в самом низу длинной страницы (после таблиц прав, людей
// и истории); без прокрутки на снимке был бы верх страницы, а не она.
await evalIn(`${INVITE_FORM}.scrollIntoView({ block: "center" })`);
await shot("roles-invite-form-unit-selected");

await clickOn(inviteSubmitButton, "кнопка «Пригласить» (управляющий + NS1)");
// Ждём именно текст уведомления, а не только смену пути (issue #206): путь
// иногда читается уже новым, а тело страницы — ещё прежним, недорисованным.
// Тот же приём, что ниже спасает проверку отказов, где смены пути нет вовсе.
check(
  "продукт сообщил о заведении управляющего",
  await waitForText("заведён с ролью"),
);
check(
  "успешное приглашение увело со /roles/invite/ на /roles/",
  (await evalIn("location.pathname")) === "/roles/",
);
const afterManagerInvite = await bodyText();
check(
  "в уведомлении назван именно Стеван",
  afterManagerInvite.includes(MANAGER_NAME),
  afterManagerInvite.slice(0, 200),
);

// Ищем именно в таблице «У кого какая роль» (`.chip` там и только там) — а не
// по всей строке: строка «Выдать роль» несёт `<select>` со ВСЕМИ ролями как
// вариантами, и текст «Управляющий точки» там есть всегда, независимо от того,
// какая роль на самом деле выдана. Такая же строка есть и в «Истории
// доступов» («… пригласил Стеван Симич с ролью «Управляющий точки»») — она не
// должна сойти за подтверждение вместо самой таблицы ролей.
const managerRow = await evalIn(`
  (() => {
    const row = [...document.querySelectorAll("tbody tr")]
      .find(tr => tr.textContent.includes(${JSON.stringify(MANAGER_NAME)}) && tr.querySelector(".chip"));
    if (!row) return null;
    return {
      who: row.querySelector("td")?.textContent.replace(/\\s+/g, " ").trim(),
      roles: [...row.querySelectorAll(".chip")].map((c) => c.textContent.replace(/\\s+/g, " ").trim()),
    };
  })()
`);
check("Стеван появился в списке людей", !!managerRow, JSON.stringify(managerRow?.who));
check(
  "у Стевана в списке видна роль «Управляющий точки»",
  !!managerRow?.roles?.some((r) => r.includes("Управляющий точки")),
  JSON.stringify(managerRow?.roles),
);
await shot("roles-people-list-manager-invited");

// =============================================================================
// 4. Главная проверка — в базе: unit_ids ровно из одной точки, и это NS1
// =============================================================================

const tenantId = sql("select id::text from tenants where code = 'rs-dev'");
check("тенант rs-dev найден в базе", !!tenantId, tenantId);

const ns1DbId = sql(
  `select id::text from units where tenant_id = '${tenantId}' and code = 'NS1'`,
);
check("точка NS1 найдена в базе", !!ns1DbId, ns1DbId);
check(
  "точка из формы и точка из базы — одна и та же строка",
  ns1UnitId === ns1DbId,
  `${ns1UnitId} vs ${ns1DbId}`,
);

const managerUserId = sql(`select id::text from users where email = '${MANAGER_EMAIL}'`);
check("учётка Стевана заведена", !!managerUserId, managerUserId);

const rawUnitIds = sql(
  `select m.unit_ids::text from memberships m
     join users u on u.id = m.user_id
    where u.full_name = '${MANAGER_NAME}' and m.tenant_id = '${tenantId}'`,
);
// Пустая строка здесь — это NULL в psql -tA, то есть «все точки партнёра».
// Именно это молчаливое умолчание и было дефектом (D031, unit_ids is null в
// `app_unit_is_visible`): проверка обязана падать на нём, а не пропускать его
// как «пусто, но не страшно».
check(
  "membership.unit_ids НЕ NULL — иначе это все точки партнёра, а не одна",
  rawUnitIds !== "",
  JSON.stringify(rawUnitIds),
);
const memberUnitIds = rawUnitIds === "" ? [] : rawUnitIds.replace(/^\{|\}$/g, "").split(",");
check(
  "у управляющего в членстве ровно одна точка",
  memberUnitIds.length === 1,
  JSON.stringify(memberUnitIds),
);
check(
  "эта точка — именно NS1, а не любая другая",
  memberUnitIds[0] === ns1DbId,
  `${memberUnitIds[0]} vs ${ns1DbId}`,
);

// =============================================================================
// 5. Отказ словами, если точку не выбрали
// =============================================================================

await submitInvite({
  fullName: NO_UNIT_NAME, email: NO_UNIT_EMAIL, roleId: managerRoleId,
  unitId: "", reason: "смоук: без точки — должен быть отказ",
});
await clickOn(inviteSubmitButton, "кнопка «Пригласить» (управляющий без точки)");
check(
  "отказ называет, что без точки управляющий получил бы все точки партнёра",
  await waitForText("Без точки человек получил бы все точки партнёра"),
);
check(
  "путь остался /roles/invite/ — редиректа на успех не было",
  (await evalIn("location.pathname")) === "/roles/invite/",
);
await shot("roles-invite-refused-no-unit");

const noUnitCreated = sql(
  `select count(*)::text from users where email = '${NO_UNIT_EMAIL}'`,
);
check("человек без выбранной точки в базе НЕ заведён", noUnitCreated === "0", noUnitCreated);

// =============================================================================
// 6. Роль, ведущая всего партнёра, точку не принимает
// =============================================================================

await submitInvite({
  fullName: ACCOUNTANT_NAME, email: ACCOUNTANT_EMAIL, roleId: accountantRoleId,
  unitId: ns1UnitId, reason: "смоук: бухгалтер + точка — должен быть отказ",
});
await clickOn(inviteSubmitButton, "кнопка «Пригласить» (бухгалтер + NS1)");
check(
  "отказ называет, что роль ведёт всего партнёра и точка ей не нужна",
  await waitForText("точка для неё не выбирается"),
);
check(
  "путь остался /roles/invite/ и для этого отказа тоже",
  (await evalIn("location.pathname")) === "/roles/invite/",
);

const accountantCreated = sql(
  `select count(*)::text from users where email = '${ACCOUNTANT_EMAIL}'`,
);
check(
  "бухгалтер с навязанной точкой в базе НЕ заведён",
  accountantCreated === "0", accountantCreated,
);

// =============================================================================
// 7. Что видно приглашённому под ролью приложения, а не владельца базы
// =============================================================================

const anyTills = sql(`select count(*)::text from tills where tenant_id = '${tenantId}'`);
if (anyTills === "0") {
  // Честно, а не подделано: сид `seed_dev` касс не заводит вовсе (проверено по
  // коду `management/commands/seed_dev.py` — модель `Till` там только
  // удаляется при уборке, ни разу не создаётся). Значит утверждение «видна
  // только NS1, BG1 не видна» здесь доказать нечем: список был бы пуст и для
  // NS1, и для BG1 одинаково, и зелёная проверка была бы пройдена вхолостую.
  // Дальше — санитарная проверка того же пути: политика `unit_visibility` не
  // должна ронять запрос под ролью приложения от имени человека с сужением по
  // точке (проверка правил на `NULL`-случае и на непустом — уже выше, п.4).
  console.log(
    "ПРИМЕЧАНИЕ: касс (tills) у партнёра в сиде нет вовсе — сравнить видимость " +
      "NS1 против BG1 через кассы нельзя. Ниже — только санитарная проверка, что " +
      "запрос от имени приглашённого под ролью app_user не падает.",
  );
  const scopedRaw = sql(`
    begin;
    set local role app_user;
    select set_config('app.user_id', '${managerUserId}', true);
    select count(*)::text from tills;
    rollback;
  `);
  // Строк несколько: `set_config` тоже возвращает строку (переданный uuid),
  // а нужен нам только результат ПОСЛЕДНЕГО select — счёта касс.
  const scopedLines = scopedRaw.split("\n").filter(Boolean);
  const scopedCount = scopedLines[scopedLines.length - 1];
  check(
    "запрос касс от имени приглашённого под ролью app_user не падает (касс в сиде нет — не проверка охвата)",
    scopedCount === "0",
    `count=${scopedCount}`,
  );
} else {
  const scopedRaw = sql(`
    begin;
    set local role app_user;
    select set_config('app.user_id', '${managerUserId}', true);
    select coalesce(string_agg(code, ',' order by code), '') from tills;
    rollback;
  `);
  const scopedLines = scopedRaw.split("\n").filter(Boolean);
  const codes = (scopedLines[scopedLines.length - 1] || "").split(",").filter(Boolean);
  check("приглашённый управляющий видит кассу своей точки NS1", codes.some((c) => c.startsWith("NS1")), codes.join(","));
  check("приглашённый управляющий НЕ видит кассу чужой точки BG1", !codes.some((c) => c.startsWith("BG1")), codes.join(","));
}

// =============================================================================
const noise = logs.filter((l) => /EXCEPTION|Uncaught/.test(l));
check("в консоли браузера нет исключений", noise.length === 0, noise.slice(0, 2).join(" | "));

report();
