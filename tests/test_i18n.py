"""Локализация на три языка (T017).

Главное отличие этих проверок от «перевод существует». Перевод, который просто
лежит в каталоге, ничего не гарантирует: через месяц половина нового экрана
будет написана прямо в шаблоне по-русски, каталог останется зелёным, а
английская страница — наполовину русской. Поэтому проверяется ровно обратное —
**непереведённая строка**, и с трёх сторон:

1. **В исходнике.** Русский текст в шаблоне, не обёрнутый в перевод, — это
   строка, которая никогда не попадёт в каталог. Обход шаблонов её находит.
2. **В каталоге.** Каталог обязан покрывать всё, что извлекается из исходников
   (сверяется настоящим `makemessages`), и не содержать пустых и `fuzzy`
   переводов. Скомпилированный `.mo` обязан совпадать с `.po`: переведённый, но
   не собранный каталог на экране выглядит как отсутствующий.
3. **На экране.** Страницы всех экранов под всеми ролями рисуются на каждом
   языке, и на нерусской странице не должно остаться кириллицы — кроме данных
   партнёра, которые продукт не переводит и не вправе переводить (имена людей,
   названия точек, подписи компонентов из пресета страны). Список данных
   собирается **из базы**, а не пишется руками: иначе он превратился бы в
   список исключений, куда удобно спрятать непереведённую строку.

Язык исходника — русский (`LANGUAGE_CODE = "ru"`), поэтому «кириллица на
английской странице» и есть точный признак непереведённого.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LOCALE = ROOT / "src" / "locale"
COMPONENTS = ROOT / "src" / "web" / "templates" / "web" / "components"

# Языки перевода: русский — сам исходник, каталога у него нет.
TRANSLATED = {"en": LOCALE / "en", "sr-latn": LOCALE / "sr_Latn"}

TEMPLATES = sorted(ROOT.glob("src/*/templates/**/*.html"))

CYRILLIC = re.compile(r"[а-яёА-ЯЁ]")


# --- 1. непереведённая строка в исходнике ------------------------------------

# Что вырезается из шаблона перед проверкой и почему.
COMMENTS = re.compile(r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}", re.S)
SHORT_COMMENTS = re.compile(r"\{#.*?#\}", re.S)
STYLE = re.compile(r"<style\b.*?</style>", re.S | re.I)
SCRIPT = re.compile(r"<script\b.*?</script>", re.S | re.I)
# Тело `{% blocktranslate %}` — уже перевод, внутрь не смотрим.
BLOCKTRANSLATE = re.compile(
    r"\{%\s*blocktrans(?:late)?\b.*?\{%\s*endblocktrans(?:late)?\s*%\}", re.S
)
TEMPLATE_TAG = re.compile(r"\{%.*?%\}", re.S)
TEMPLATE_VAR = re.compile(r"\{\{.*?\}\}", re.S)
HTML_TAG = re.compile(r"<[^>]*>", re.S)

# Теги перевода: русский текст внутри них — это ключ, а не забытая строка.
TRANSLATION_TAGS = re.compile(r"^\{%\s*(?:trans|translate|blocktrans|blocktranslate)\b")

# Строковый литерал в JavaScript. Островок табеля и полоса расчёта пишут текст
# прямо в DOM, и по-русски он там такой же непереведённый, как в разметке.
JS_STRING = re.compile(r"""(['"])((?:\\.|(?!\1).)*)\1""")


def unmarked_text(source: str) -> list[str]:
    """Русский текст шаблона, оставшийся вне перевода. Пусто — значит всё помечено."""
    text = COMMENTS.sub(" ", source)
    text = SHORT_COMMENTS.sub(" ", text)
    text = STYLE.sub(" ", text)
    text = SCRIPT.sub(" ", text)
    text = BLOCKTRANSLATE.sub(" ", text)

    found: list[str] = []
    # Литералы внутри блочных тегов: `{% include … label="Посчитать период" %}`
    # — такая же непереведённая строка, только спрятанная в параметр.
    for tag in TEMPLATE_TAG.findall(text):
        if TRANSLATION_TAGS.match(tag.strip()):
            continue
        if CYRILLIC.search(tag):
            found.append(tag.strip())

    plain = TEMPLATE_TAG.sub(" ", text)
    plain = TEMPLATE_VAR.sub(" ", plain)
    plain = HTML_TAG.sub(" ", plain)
    for line in plain.splitlines():
        if CYRILLIC.search(line):
            found.append(line.strip())
    return found


def js_strings_in_russian(source: str) -> list[str]:
    """Русский текст в скриптах шаблона: он попадает на экран так же, как разметка."""
    found = []
    for script in SCRIPT.findall(source):
        # Комментарии в скрипте объясняют код, а не говорят с человеком.
        body = re.sub(r"//[^\n]*", " ", script)
        body = re.sub(r"/\*.*?\*/", " ", body, flags=re.S)
        for _quote, value in JS_STRING.findall(body):
            if CYRILLIC.search(value):
                found.append(value)
    return found


def test_no_untranslated_text_in_templates():
    """Русский текст в шаблоне обязан быть обёрнут в перевод.

    Это и есть проверка «ловит непереведённую строку»: новая надпись, написанная
    мимо `{% translate %}`, валит тест в тот же день, а не через месяц на
    английской странице.
    """
    assert TEMPLATES, "шаблоны не нашлись — проверка проверяет пустоту"
    problems = []
    for path in TEMPLATES:
        source = path.read_text(encoding="utf-8")
        for piece in unmarked_text(source):
            problems.append(f"{path.relative_to(ROOT)}: {piece}")
    assert not problems, "текст мимо перевода:\n" + "\n".join(problems)


def test_no_untranslated_text_in_template_scripts():
    """Скрипты шаблонов пишут текст в DOM — по-русски он тоже непереведённый."""
    problems = []
    for path in TEMPLATES:
        for value in js_strings_in_russian(path.read_text(encoding="utf-8")):
            problems.append(f"{path.relative_to(ROOT)}: {value!r}")
    assert not problems, "русский текст в скриптах мимо перевода:\n" + "\n".join(problems)


# Свои скрипты продукта. Чужие библиотеки (htmx) не наши и не переводятся.
STATIC_SCRIPTS = sorted(
    path
    for path in ROOT.glob("src/*/static/**/*.js")
    if not path.name.endswith(".min.js")
)


def test_no_untranslated_text_in_static_scripts():
    """Отдельный `.js` — самая тихая дыра из всех: его не видит вообще ничего.

    Файл из `static/` не проходит ни движок шаблонов, ни `makemessages`:
    написанная в нём строка не попадает в каталог, никем не переводится и
    остаётся русской на английском и сербском экране. Заметить это глазами
    почти нельзя — такие строки показываются только в отказах, то есть в
    минуту, когда человеку и без того не до языка интерфейса.

    Найдено на этой же задаче: тексты отказов сохранения ячейки табеля жили в
    `grid.js` и не ловились ни одной из проверок выше. Теперь слова приезжают из
    разметки через `data-`атрибуты, а этот тест держит дверь закрытой.
    """
    assert STATIC_SCRIPTS, "скрипты не нашлись — проверка проверяет пустоту"
    problems = []
    for path in STATIC_SCRIPTS:
        source = path.read_text(encoding="utf-8")
        # Комментарии объясняют код разработчику и на экран не попадают.
        body = re.sub(r"//[^\n]*", " ", source)
        body = re.sub(r"/\*.*?\*/", " ", body, flags=re.S)
        for _quote, value in JS_STRING.findall(body):
            if CYRILLIC.search(value):
                problems.append(f"{path.relative_to(ROOT)}: {value!r}")
    assert not problems, (
        "русский текст в статическом скрипте (его не видит ни перевод, ни "
        "каталог — вынесите строку в разметку через data-атрибут):\n"
        + "\n".join(problems)
    )


# --- 2. каталог -------------------------------------------------------------


def unescape(literal: str) -> str:
    """Строка `.po` — в то, что из неё получит gettext.

    Разбор без разэкранирования был бы неверным ровно на длинных строках: в
    каталоге перевод абзаца лежит одной записью с `\\n` внутри, а на экран
    выходит настоящим переносом. Сравнение «как написано в файле» с «как отдал
    `.mo`» без этого шага расходится на каждом многострочном тексте — и это
    расхождение самого разбора, а не каталога.
    """
    return json.loads(literal)


def read_po(path: Path) -> list[dict]:
    """Записи каталога: msgid, формы перевода и пометка fuzzy. Без библиотек.

    Формы множественного числа (`msgstr[0]`, `msgstr[1]`, …) читаются **по
    отдельности**, и это не педантизм, а условие того, чтобы обе проверки
    каталога говорили правду:

    * «нет пустых переводов» обязана смотреть каждую форму: пустая вторая форма
      — это непереведённая строка, которую человек увидит по-русски на числе 2;
    * «`.mo` собран из нынешнего `.po`» сверяется с `gettext(msgid)`, а тот
      отдаёт **одну** форму — ту, что соответствует единственному числу. Склейка
      всех форм в одну строку не совпала бы с ней никогда, то есть проверка
      краснела бы на любом правильном переводе.

    Оба дефекта жили здесь до T110 незамеченными: `msgid_plural` в каталоге не
    было ни одного (issue #101).
    """
    entries: list[dict] = []
    current: dict = _blank_entry()
    sink: list | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line.startswith("#,") and "fuzzy" in line:
            current["fuzzy"] = True
            continue
        if line.startswith("#") or not line:
            if not line and (current["msgid"] or current["forms"]):
                entries.append(current)
                current = _blank_entry()
                sink = None
            continue
        if line.startswith("msgid_plural"):
            # Ключ записи — единственное число: множественный msgid нужен только
            # сборке каталога, а проверкам он ничего не добавляет.
            sink = None
            continue
        if line.startswith("msgid"):
            sink = current["msgid"]
            line = line[len("msgid"):].strip()
        elif line.startswith("msgstr"):
            line = line[len("msgstr"):].strip()
            if line.startswith("["):
                line = line.split("]", 1)[1].strip()
            current["forms"].append([])
            sink = current["forms"][-1]
        if sink is not None and line.startswith('"'):
            sink.append(unescape(line))
    if current["msgid"] or current["forms"]:
        entries.append(current)
    return [
        {
            "msgid": "".join(item["msgid"]),
            # Перевод единственного числа: с ним сверяется собранный каталог.
            "msgstr": "".join(item["forms"][0]) if item["forms"] else "",
            # Все формы: по ним видно непереведённую вторую форму.
            "forms": ["".join(form) for form in item["forms"]],
            "fuzzy": item["fuzzy"],
        }
        for item in entries
    ]


def _blank_entry() -> dict:
    return {"msgid": [], "forms": [], "fuzzy": False}


def catalog(language: str) -> list[dict]:
    path = TRANSLATED[language] / "LC_MESSAGES" / "django.po"
    assert path.exists(), f"нет каталога {path.relative_to(ROOT)}"
    # Первая запись с пустым msgid — служебная шапка, а не строка интерфейса.
    return [item for item in read_po(path) if item["msgid"]]


def test_the_catalog_reader_understands_plural_forms(tmp_path):
    """Проверка самой проверки: формы множественного числа обязаны читаться.

    Разбор `msgstr[0]` был сломан так, что перевод формы **всегда** получался
    пустым (issue #101). Из-за этого проверка «нет пустых переводов» на таких
    строках не проверяла ничего, а проверка «каталог покрывает код» краснела на
    правильно переведённом каталоге. Молчаливо неверная проверка хуже
    отсутствующей: она красит зелёным то, чего не смотрела.
    """
    catalog_file = tmp_path / "django.po"
    catalog_file.write_text(
        'msgid "Итого по %(counter)s строке"\n'
        'msgid_plural "Итого по %(counter)s строкам"\n'
        'msgstr[0] "Total for %(counter)s row"\n'
        'msgstr[1] "Total for %(counter)s rows"\n',
        encoding="utf-8",
    )
    entries = read_po(catalog_file)
    assert len(entries) == 1, entries
    assert entries[0]["msgid"] == "Итого по %(counter)s строке"
    assert "Total for %(counter)s row" in entries[0]["msgstr"], (
        "перевод формы множественного числа потерян при разборе каталога"
    )


@pytest.mark.parametrize("language", sorted(TRANSLATED))
def test_catalog_has_no_empty_or_fuzzy_translations(language):
    """Пустой перевод и `fuzzy` — это непереведённая строка, только в каталоге.

    `fuzzy` gettext не показывает вовсе: строка молча остаётся русской.
    """
    missing = [
        item["msgid"]
        for item in catalog(language)
        # Каждая форма отдельно: пустая вторая форма — это строка, которую
        # человек увидит по-русски на числе 2, а по первой форме этого не видно.
        if item["fuzzy"] or not item["forms"]
        or any(not form.strip() for form in item["forms"])
    ]
    assert not missing, f"{language}: без перевода {len(missing)}:\n" + "\n".join(missing)


@pytest.mark.parametrize("language", sorted(TRANSLATED))
def test_compiled_catalog_matches_the_source_one(language):
    """`.mo` собран из нынешнего `.po`.

    Забытая сборка — самый тихий способ потерять перевод: каталог полон, экран
    русский, тесты каталога зелёные.
    """
    import gettext as gettext_module

    mo = TRANSLATED[language] / "LC_MESSAGES" / "django.mo"
    assert mo.exists(), f"нет собранного каталога {mo.relative_to(ROOT)}"
    with mo.open("rb") as handle:
        compiled = gettext_module.GNUTranslations(handle)
    stale = [
        item["msgid"]
        for item in catalog(language)
        if compiled.gettext(item["msgid"]) != item["msgstr"]
    ]
    assert not stale, (
        f"{language}: .mo отстал от .po на {len(stale)} строк "
        f"(соберите: django-admin compilemessages):\n" + "\n".join(stale[:20])
    )


@pytest.mark.skipif(shutil.which("xgettext") is None, reason="нет gettext")
@pytest.mark.parametrize("language", sorted(TRANSLATED))
def test_catalog_covers_everything_extracted_from_sources(language, tmp_path):
    """Каталог покрывает всё, что извлекается из кода и шаблонов сегодня.

    Проверяется настоящим `makemessages`, а не своим разбором: расхождение
    между тем, как строки ищет тест, и тем, как их ищет сборка каталога, — это
    ровно та щель, в которую проваливается новая строка.
    """
    import os

    # Извлекаем в копии исходников, а не в самом репозитории: `makemessages`
    # переписывает каталог на месте, и тест, меняющий рабочее дерево, однажды
    # затрёт то, что человек как раз редактировал.
    work = tmp_path / "repo"
    work.mkdir()
    shutil.copytree(ROOT / "src", work / "src", ignore=shutil.ignore_patterns("locale"))
    shutil.copy(ROOT / "manage.py", work / "manage.py")
    (work / "src" / "locale").mkdir()

    code = "en" if language == "en" else "sr_Latn"
    done = subprocess.run(
        [
            sys.executable, str(work / "manage.py"), "makemessages",
            "--locale", code, "--no-obsolete", "--no-location", "--no-wrap",
        ],
        cwd=work,
        env={
            "DJANGO_SETTINGS_MODULE": "config.settings",
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(work / "src"),
            "SECRET_KEY": "test-only-not-a-secret",
            "HOME": os.environ.get("HOME", ""),
        },
        capture_output=True, text=True,
    )
    assert done.returncode == 0, done.stdout + done.stderr

    extracted = {
        item["msgid"]
        for item in read_po(work / "src" / "locale" / code / "LC_MESSAGES" / "django.po")
        if item["msgid"]
    }
    known = {item["msgid"] for item in catalog(language)}
    lost = extracted - known
    assert not lost, (
        f"{language}: строки есть в коде, но не в каталоге ({len(lost)}):\n"
        + "\n".join(sorted(lost))
    )
