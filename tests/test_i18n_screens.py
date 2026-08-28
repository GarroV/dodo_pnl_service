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
людей, названия точек и партнёров — настройка партнёра, а не слова продукта;
переводить их значило бы решать за бухгалтера, как зовут его сотрудника.
Собирать этот список руками нельзя: он мгновенно превратился бы в место, куда
удобно спрятать непереведённую строку продукта. Поэтому он вычитывается из тех
же таблиц, из которых страница берёт данные.

**Подписи правил исключаются только на своём языке (T092).** Раньше сюда
попадали подписи типов часов, компонентов, схем и групп целиком — как «данные
партнёра». В эту щель и утекли колонки ведомости и табеля: они оставались
русскими на всех трёх языках, а проверка молчала, потому что сама же их и
разрешила. Подпись правила действительно данные — но данные, обязанные нести
все языки продукта. Поэтому разрешается ровно та подпись, которая объявлена для
проверяемого языка; русская подпись на английской странице — снова дефект.
"""
from __future__ import annotations

import re

import pytest

from conftest import body, login_as, period_url

# Языки с каталогом. Русского здесь нет: он и есть исходник, и проверять на нём
# «нет ли русского» бессмысленно.
TRANSLATED = ["en", "sr-latn"]

# Названия языков в переключателе написаны каждое на своём языке. «Русский» на
# английской странице — не забытый перевод, а сама суть кнопки: человек ищет
# глазами родное слово, а не «Russian», которого он может не знать.
SWITCHER = {"Русский"}

ROLES = ["director", "accountant", "manager", "admin"]

CYRILLIC = re.compile(r"[а-яёА-ЯЁ]+(?:[\s.,·×/«»()-]+[а-яёА-ЯЁ]+)*")

# Куда девается разметка перед проверкой: в атрибутах и в скриптах кириллица
# тоже видна человеку, но `<style>` и комментарии HTML — нет.
COMMENT = re.compile(r"<!--.*?-->", re.S)
STYLE = re.compile(r"<style\b.*?</style>", re.S | re.I)


def data_terms(dsn: str, language: str = "") -> set[str]:
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

    def add_title(value) -> None:
        """Подпись правила: разрешена та, что объявлена для проверяемого языка.

        Многоязычная подпись (T092) отдаёт ровно одно значение — на языке
        страницы. Подпись, написанную одной строкой, разрешаем как есть: партнёр
        вправе вести учёт на одном языке, и требовать от него перевода продукт
        не может. Полноту подписей самого пресета страны проверяет
        `tests/test_rule_titles.py` — здесь проверяется экран.
        """
        if isinstance(value, str):
            add(value)
            return
        if isinstance(value, dict):
            wanted = language.lower().replace("_", "-")
            for code, text in value.items():
                if str(code).lower().replace("_", "-") == wanted:
                    add(text)

    def walk(node) -> None:
        """Все подписи пресета: их состав у каждой страны свой, перечислять нельзя."""
        if isinstance(node, dict):
            for key, value in node.items():
                if key in ("title", "pnl_line"):
                    # `pnl_line` разбирается ровно как подпись, а не как данные
                    # целиком. Пока он разрешался любой строкой, экран показывал
                    # `Расходы на управление` по-английски, а проверка молчала:
                    # она числила данными партнёра то, что приезжает из пресета
                    # СТРАНЫ, то есть от продукта (T103).
                    add_title(value)
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
            # Имена людей, вошедших в продукт. У учёток сида имя совпадает
            # с названием роли по-русски — но это по-прежнему имя человека
            # в базе, а не подпись продукта: подпись роли рядом с ним
            # переводится (`web.i18n.role_title`), и её отсутствие тест
            # поймает, а имя — нет и не должен.
            "select full_name from users",
        ):
            for (value,) in conn.execute(query).fetchall():
                add(value)
        for (preset,) in conn.execute("select body from rule_presets").fetchall():
            walk(preset)
        # Переопределения партнёра лежат по одному значению на строку
        # (`path` + `value`), а не целым пресетом: подпись, заменённая
        # партнёром, — такие же его данные, как и подпись из страны.
        for (value,) in conn.execute("select value from rule_overrides").fetchall():
            walk(value)

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
        if piece in SWITCHER:
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
    # Карточка сотрудника и его выплаты по месяцам (T166). Берутся переходом со
    # списка людей — так же, как до них доходит человек. Добавлены потому, что
    # без них дефект прожил бы до глаз владельца: на экране выплат две строки
    # собирались в Python константами модуля, то есть переводились ОДИН РАЗ при
    # импорте, и английская страница показывала русский текст при полностью
    # зелёном каталоге. Ни одна проверка по файлам такого не видит — только
    # нарисованная страница.
    card = re.search(
        r'href="(/directory/employees/[0-9a-f-]+/)"',
        body(client.get("/directory/employees/")),
    )
    if card:
        found.append(card.group(1))
        found.append(card.group(1) + "pay/")
    client.post("/logout/")
    return found


@pytest.mark.parametrize("language", TRANSLATED)
def test_no_russian_left_on_the_screens(client, web_env, screens, language):
    """Ни один экран ни под одной ролью не показывает русского текста.

    Это и есть приёмка задачи со стороны человека: «переключение работает и
    непереведённых строк на экранах нет». Всё, что здесь падает, человек увидел
    бы своими глазами на английской или сербской странице.
    """
    from django.test.utils import override_settings

    allowed = data_terms(web_env, language)
    client.cookies["django_language"] = language

    problems: list[str] = []
    # Страницы смотрятся так, как их видит человек у партнёра, а не разработчик:
    # с `DEBUG=1` Django на 404 подставляет свою техническую страницу с текстом
    # исключения («строка ведомости не найдена»), и проверка ловила бы служебное
    # сообщение вместо экрана продукта. Сообщения исключений — для журнала и для
    # разработчика, они и остаются на языке исходника.
    with override_settings(DEBUG=False):
        for role in ROLES:
            login_as(client, role)
            for url in screens:
                response = client.get(url)
                # 403 и 404 — законные ответы: не всякий экран открыт всякой
                # роли. Их текст проверяется тоже: отказ человек читает так же,
                # как страницу, и русский отказ на английской странице — тот же
                # дефект.
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
    problems = foreign_words(body(client.get("/login/")), data_terms(web_env, language))
    assert not problems, f"{language}: вход по-русски:\n" + "\n".join(problems)


def test_the_switch_actually_changes_the_page(client, web_env):
    """Переключатель языка работает: та же страница после нажатия — на другом языке.

    Не «маршрут отвечает 302», а именно смена языка страницы: маршрут может
    вернуть перенаправление и не переключить ничего (например, если посредник
    стоит не в том месте), и это самый тихий способ сломать всю задачу.
    """
    login_as(client, "director")
    russian = body(client.get("/periods/"))
    # «Табель» вместо «Периоды»: раздел переименован по словарю эталона
    # (issue #162). Слово-маркер обязано быть тем, что на странице действительно
    # есть, иначе проверка охраняет прошлое.
    assert "Табель" in russian, "исходный язык страницы уже не русский"

    answer = client.post("/i18n/setlang/", {"language": "en", "next": "/periods/"})
    assert answer.status_code in (302, 200), answer.status_code

    english = body(client.get("/periods/"))
    assert "Табель" not in english, "после переключения страница осталась русской"
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


def test_forced_language_hides_the_switch(client, web_env):
    """Закреплённый язык (демо) отменяет выбор человека и убирает переключатель.

    Правило владельца: демо всегда открывается по-английски, независимо от того,
    кто смотрит. Проверяется здесь, а не в блоке демо, потому что закрепление
    живёт в этом блоке и ломается правкой этого блока.

    Клиенту нужен свой стек посредников: цепочка собирается один раз, а
    `ForcedLanguageMiddleware` читает настройку при своём создании. Со старым
    стеком проверялся бы экземпляр, созданный до подмены, — то есть ничего.
    """
    from django.test import Client
    from django.test.client import ClientHandler
    from django.test.utils import override_settings

    with override_settings(UI_LANGUAGE="en"):
        fresh = Client()
        # Пересборка цепочки, а не новый клиент: `Client` создаёт обработчик в
        # своём `__init__`, и к этому моменту подмена настройки ещё не видна.
        fresh.handler = ClientHandler(enforce_csrf_checks=False)
        fresh.handler.load_middleware()
        login_as(fresh, "director")
        # Человек просит русский — и всё равно получает английский.
        fresh.cookies["django_language"] = "ru"
        html = body(fresh.get("/periods/"))

    assert 'lang="en"' in html, "закреплённый язык не переопределил выбор человека"
    assert "Табель" not in html
    assert 'class="lang"' not in html, "при закреплённом языке переключатель показан"


def test_role_titles_are_translated_and_not_taken_from_the_database(web_env):
    """Название роли переводится, а не берётся из базы как есть.

    Отдельным тестом, потому что через экран это **не проверяется**: у учёток
    тестового сида имя человека дословно совпадает с названием его роли
    («Оперативный директор» и там, и там), а имя человека — данные, которые
    продукт переводить не вправе. То есть подмена перевода на строку из базы
    прошла бы по экранам незамеченной.

    Проверяется именно то, что ломается: незнакомая роль показывается так, как
    её назвал партнёр (переводить её неоткуда), а знакомая — на языке страницы.
    """
    from django.utils import translation

    from web.i18n import role_title

    expected = {
        "ru": "Оперативный директор",
        "en": "Operations Director",
        "sr-latn": "Operativni direktor",
    }
    for language, title in expected.items():
        with translation.override(language):
            assert role_title("director", "из базы") == title
            # Роль, которой продукт не знает, остаётся такой, как её завёл
            # партнёр: выдумывать ей перевод неоткуда.
            assert role_title("chief-taster", "Главный дегустатор") == "Главный дегустатор"
