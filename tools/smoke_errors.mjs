/*
 * Смоук страниц отказа (T099): что человек видит на несуществующем адресе.
 *
 * Почему смоук, а не только тесты. Дефект (issue #82) жил именно в браузере: на
 * стенде с `DJANGO_DEBUG=1` — а это умолчание, с которым продукт поднимают
 * локально и показывают коллеге, — Django подставляет свою техническую
 * страницу раньше всякого представления. Тест ходит клиентом Django и легко
 * может проверять не тот стенд; здесь страницу открывает настоящий браузер на
 * настоящем стенде.
 *
 * Данные не трогает вовсе — поэтому и стенд к сиду не приводит: приводить
 * нечего, а лишний сброс отнимал бы у соседних проверок минуту на пустом месте.
 *
 *     COMPOSE_PROJECT_NAME=… APP=http://127.0.0.1:8076 node tools/smoke_errors.mjs
 */
import { attach, loginWith } from "./cdp.mjs";

const APP = process.env.APP || "http://127.0.0.1:8076";

// Заголовок страницы на каждом языке — тот же ключ, три написания. Проверяем
// именно их: «страница отдалась» и «страница отдалась на языке человека» —
// разные утверждения, и второе ломается тише.
const TITLES = {
  ru: "Страница не найдена",
  en: "Page not found",
  "sr-latn": "Stranica nije pronađena",
};

// Слова технической страницы Django. Любое на экране — человек снова читает
// страницу разработчика.
const TECHNICAL = [
  "URLconf",
  "Django tried these URL patterns",
  "Request Method:",
  "Exception Type:",
  "Traceback",
];

const { evalIn, goto, send, check, report } = await attach();
const login = loginWith(APP, evalIn, goto);

async function page() {
  return evalIn(`
    (() => ({
      text: document.body.innerText,
      brand: !!document.querySelector("a.brand"),
      back: !!document.querySelector('a[href="/periods/"]'),
      lang: document.documentElement.lang,
      title: document.title,
    }))()
  `);
}

// --- аноним ------------------------------------------------------------------
// Первым делом именно он: посторонний на демо и человек, промахнувшийся адресом
// до входа, видят эту страницу чаще всех.
await goto(`${APP}/no-such-address/`);
{
  const seen = await page();
  check("аноним видит страницу продукта, а не каркас", seen.brand && seen.back,
    seen.title);
  check("аноним не видит технических подробностей",
    !TECHNICAL.some((word) => seen.text.includes(word)),
    TECHNICAL.filter((word) => seen.text.includes(word)).join(", "));
  check("аноним не видит адреса, по которому пришёл",
    !seen.text.includes("no-such-address"));
}

// --- три языка ---------------------------------------------------------------
// Cookie сильнее заголовка, поэтому язык задаём тем же способом, каким его
// задаёт переключатель, — иначе проверялся бы заголовок браузера, а не продукт.
for (const [code, title] of Object.entries(TITLES)) {
  await send("Network.setCookie", {
    name: "django_language", value: code, url: APP,
  });
  await goto(`${APP}/no-such-address/`);
  const seen = await page();
  check(`${code}: заголовок на своём языке`, seen.text.includes(title),
    seen.title);
  check(`${code}: страница объявила свой язык`, seen.lang === code, seen.lang);
}

await send("Network.setCookie", { name: "django_language", value: "ru", url: APP });

// --- вошедший ----------------------------------------------------------------
// У него страница обязана остаться страницей продукта с навигацией: промах
// адресом посреди работы не должен выбрасывать человека из продукта.
await login("director");
await goto(`${APP}/periods/00000000-0000-4000-8000-000000000009/`);
{
  const seen = await page();
  check("вошедший видит страницу продукта", seen.brand && seen.back, seen.title);
  check("вошедшему не показали текст исключения",
    !seen.text.includes("период не найден"), seen.text.slice(0, 120));
  check("на странице отказа есть навигация продукта",
    await evalIn(`!!document.querySelector('nav[aria-label] a[href="/periods/"]')`));
}

// --- чужая строка и несуществующая неотличимы --------------------------------
// Это и есть требование видимости (D023): по ответу нельзя понять, что строка
// существует. Сравниваем текстом, а не кодом ответа: код совпадал и раньше.
await login("manager");
const texts = [];
for (const id of [
  "00000000-0000-4000-8000-000000000001",
  "00000000-0000-4000-8000-000000000002",
]) {
  await goto(`${APP}/payslips/${id}/trace/`);
  texts.push((await page()).text);
}
check("несуществующая и чужая строка выглядят одинаково", texts[0] === texts[1]);

report();
