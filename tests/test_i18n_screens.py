"""Непереведённая строка — на самом экране (T017, третья сторона проверки).

Две первые стороны (`tests/test_i18n.py`) смотрят на исходники и на каталог: там
ловится строка, написанная мимо перевода, и перевод, который забыли дописать или
собрать. Обе читают файлы и ни разу не рисуют страницу — а между «в каталоге всё
есть» и «на экране всё по-английски» помещается целый класс дефектов:

* строка собрана в Python и приехала на страницу готовой (`gettext` там забыли);
* перевод отложенный (`gettext_lazy`), а значение легло в словарь при импорте
  модуля — то есть на языке, который был активен в тот момент, навсегда;
* язык страницы вообще не переключился, потому что маршрут или посредник стоят
  не в том месте.

Поэтому здесь страницы рисуются по-настоящему: всеми ролями, на каждом языке.
Признак непереведённого — **кириллица на нерусской странице**: язык исходника
русский (`LANGUAGE_CODE = "ru"`), и всё, что осталось русским, осталось им
именно потому, что перевода не нашлось.

**Данные партнёра исключаются, и список исключений берётся из базы.** Имена
людей, названия точек и партнёров, подписи типов часов, компонентов, схем и
групп из пресета страны — настройка партнёра, а не слова продукта; переводить
их значило бы решать за бухгалтера, как называется его же «Топли оброк».
Собирать этот список руками нельзя: он мгновенно превратился бы в место, куда
удобно спрятать непереведённую строку продукта. Поэтому он вычитывается из тех
же таблиц, из которых страница берёт данные.
"""
from __future__ import annotations

import re

import pytest

from conftest import body, login_as, period_url

# Языки с каталогом. Русского здесь нет: он и есть исходник, и проверять на нём
# «нет ли русского» бессмысленно.
TRANSLATED = ["en", "sr-latn"]

ROLES = ["director", "accountant", "manager", "admin"]

CYRILLIC = re.compile(r"[а-яёА-ЯЁ]+(?:[\s.,·×/«»()-]+[а-яёА-ЯЁ]+)*")

# Куда девается разметка перед проверкой: в атрибутах и в скриптах кириллица
# тоже видна человеку, но `<style>` и комментарии HTML — нет.
COMMENT = re.compile(r"<!--.*?-->", re.S)
STYLE = re.compile(r"<style\b.*?</style>", re.S | re.I)


def data_terms(dsn: str) -> set[str]:
    """Слова, которые продукт показывает, но переводить не вправе.

    Читается из базы, а не из списка в тесте, — см. модульную строку. Берём
    щедро: любое слово из данных, а не только целые названия, потому что на
    экране они встречаются и по частям (имя без фамилии, точка без города).
    """
    import psycopg

    terms: set[str] = set()

    def add(value) -> None:
        if not isinstance(value, str):
            return
        value = value.strip()
        if not value:
            return
        terms.add(value)
        # Части названия: «Кухня и касса» приезжает на экран целиком, а вот
        # строка P&L «LC / Тестомейкер» — куском внутри чужой фразы.
        terms.update(part for part in re.split(r"[\s/·,()]+", value) if part)

    def walk(node) -> None:
        """Все подписи пресета: их состав у каждой страны свой, перечислять нельзя."""
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("title", "pnl_line") and isinstance(value, str):
                    add(value)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    with psycopg.connect(dsn, autocommit=True) as conn:
        for query in (
            "select title from tenants",
            "select title from units",
            "select code from units",
            "select first_name from employees",
            "select last_name from employees",
            "select title from employee_groups",
            "select title from legal_entities",
            "select title from rule_presets",
        ):
            for (value,) in conn.execute(query).fetchall():
                add(value)
        for (preset,) in conn.execute("select body from rule_presets").fetchall():
            walk(preset)
        for (overrides,) in conn.execute("select body from rule_overrides").fetchall():
            walk(overrides)

    return terms


def foreign_words(html: str, allowed: set[str]) -> list[str]:
    """Русские куски страницы, не объяснимые данными партнёра."""
    text = COMMENT.sub(" ", html)
    text = STYLE.sub(" ", text)
    found = []
    for piece in CYRILLIC.findall(text):
        piece = piece.strip(" .,·×/«»()-")
        if not piece:
            continue
        # Кусок целиком объясняется данными, если каждое его слово — данные.
        words = [word for word in re.split(r"[\s.,·×/«»()-]+", piece) if word]
        if words and all(word in allowed for word in words):
            continue
        found.append(piece)
    return found


@pytest.fixture
def screens(client, web_env):
    """Адреса всех экранов продукта — найденные переходами, а не списком.

    Список адресов в тесте разъехался бы с продуктом молча: новый экран в него
    просто не попал бы, и проверка осталась бы зелёной, ничего не проверяя.
    """
    login_as(client, "director")
    period = period_url(client)
    html = body(client.get(period))
    grid = re.search(r'href="(/timesheets/[^"]+)"', html)
    trace = re.search(r'href="(/payslips/[^"]+)"', html)
    found = [
        "/periods/",
        period,
        period + "variance/",
        period + "reconcile/",
        "/account/password/",
    ]
    if grid:
        found.append(grid.group(1))
    if trace:
        found.append(trace.group(1))
    client.post("/logout/")
    return found


@pytest.mark.parametrize("language", TRANSLATED)
def test_no_russian_left_on_the_screens(client, web_env, screens, language):
    """Ни один экран ни под одной ролью не показывает русского текста.

    Это и есть приёмка задачи со стороны человека: «переключение работает и
    непереведённых строк на экранах нет». Всё, что здесь падает, человек увидел
    бы своими глазами на английской или сербской странице.
    """
    allowed = data_terms(web_env)
    client.cookies["django_language"] = language

    problems: list[str] = []
    for role in ROLES:
        login_as(client, role)
        for url in screens:
            response = client.get(url)
            # 403 и 404 — законные ответы: не всякий экран открыт всякой роли.
            # Их текст проверяется тоже: отказ человек читает так же, как
            # страницу, и русский отказ на английской странице — тот же дефект.
            if response.status_code >= 500:
                problems.append(f"{language} {role} {url}: {response.status_code}")
                continue
            for piece in foreign_words(body(response), allowed):
                problems.append(f"{language} {role} {url}: {piece}")
        client.post("/logout/")

    assert not problems, (
        f"русский текст на страницах языка {language} "
        f"({len(problems)}):\n" + "\n".join(dict.fromkeys(problems))
    )


@pytest.mark.parametrize("language", TRANSLATED)
def test_the_entrance_is_translated_too(client, web_env, language):
    """Страница входа — единственная, которую видят до входа, и она тоже переводится.

    Отдельным тестом, потому что все остальные проверки ходят по продукту уже
    вошедшими: забытый перевод входа не увидел бы ни один из них, а человек
    видит его первым.
    """
    client.cookies["django_language"] = language
    problems = foreign_words(body(client.get("/login/")), data_terms(web_env))
    assert not problems, f"{language}: вход по-русски:\n" + "\n".join(problems)


def test_the_switch_actually_changes_the_page(client, web_env):
    """Переключатель языка работает: та же страница после нажатия — на другом языке.

    Не «маршрут отвечает 302», а именно смена языка страницы: маршрут может
    вернуть перенаправление и не переключить ничего (например, если посредник
    стоит не в том месте), и это самый тихий способ сломать всю задачу.
    """
    login_as(client, "director")
    russian = body(client.get("/periods/"))
    assert "Периоды" in russian, "исходный язык страницы уже не русский"

    answer = client.post("/i18n/setlang/", {"language": "en", "next": "/periods/"})
    assert answer.status_code in (302, 200), answer.status_code

    english = body(client.get("/periods/"))
    assert "Периоды" not in english, "после переключения страница осталась русской"
    assert 'lang="en"' in english, "страница не объявила свой язык"


def test_numbers_follow_the_language(client, web_env):
    """Разделитель тысяч меняется вместе с языком, а не остаётся русским.

    Формат чисел — такая же часть языка, как слова: `1 951 806,13` на английской
    странице читается как другое число, а не как то же самое.
    """
    from django.utils import translation

    from web.format import money

    with translation.override("ru"):
        assert money(1951806.13) == "1 951 806,13"
    with translation.override("en"):
        assert money(1951806.13) == "1,951,806.13"
    with translation.override("sr-latn"):
        assert money(1951806.13) == "1.951.806,13"


def test_forced_language_hides_the_switch(client, web_env, settings):
    """Закреплённый язык (демо) отменяет выбор человека и убирает переключатель.

    Правило владельца: демо всегда открывается по-английски, независимо от
    того, кто смотрит. Проверяется здесь, а не в блоке демо, потому что
    закрепление живёт в этом блоке и ломается правкой этого блока.
    """
    from django.test import Client

    settings.UI_LANGUAGE = "en"
    # Посредник читает настройку при своём создании, поэтому клиенту нужен
    # свежий стек — иначе проверялся бы старый экземпляр со старым значением.
    from django.core.handlers.wsgi import WSGIHandler

    fresh = Client()
    fresh.handler = WSGIHandler()
    login_as(fresh, "director")
    # Человек просит русский — и всё равно получает английский.
    fresh.cookies["django_language"] = "ru"
    html = body(fresh.get("/periods/"))
    assert 'lang="en"' in html, "закреплённый язык не переопределил выбор человека"
    assert "Периоды" not in html
    assert 'class="lang"' not in html, "при закреплённом языке переключатель показан"
