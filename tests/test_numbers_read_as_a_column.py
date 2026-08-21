"""Числа читаются столбиком (T179), первый принцип дизайн-системы.

Дословно: «Деньги читаются столбиком — правый край, моноширинное, табличные
цифры. Ноль — цифра, отсутствие значения — прочерк. Это разные вещи, и путать
их нельзя».

Три стороны проверки, и каждая ловит свой класс дефектов.

**Разметка ячейки.** Столбиком число читается только тогда, когда ячейка
помечена: правый край, моноширинное и `tabular-nums` приезжают классом `num`.
Сумма в неотмеченной ячейке выравнивается по левому краю пропорциональным
шрифтом — и столбец перестаёт складываться глазом в разряды.

**Прочерк не притворяется числом.** `web.format` уже различает ноль и пустоту
знаком, но на странице прочерк набирался тем же цветом, что суммы. В ведомости
это не мелочь: строка сотрудника заполнена по применившимся к нему компонентам,
а остальные ячейки — прочерки, и полтаблицы одинаково тёмных знаков превращают
её в сетку, где числа приходится искать глазами.

**Ноль остаётся цифрой.** Обратная сторона того же требования: бледный ноль
начал бы читаться как отсутствие значения. Поэтому фильтр помечает **только**
прочерк, и это проверяется прямо.

Экраны обходятся ссылками, а не перечисляются списком: список разъехался бы с
продуктом молча — новый экран в него просто не попал бы, и проверка осталась бы
зелёной, ничего не проверяя.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest import body, login_as

ROOT = Path(__file__).resolve().parent.parent
APP_CSS = ROOT / "src" / "web" / "static" / "web" / "app.css"

# Прочерк берётся из продукта, а не переписывается знаком: своя копия знака
# разъехалась бы с `web.format` молча — и проверка искала бы на странице то,
# чего там больше нет.
from web.format import EMPTY  # noqa: E402  (после sys.path тестов)

# Ячейка таблицы вместе с её классами и содержимым.
CELL = re.compile(r"<(td|th)\b([^>]*)>(.*?)</\1>", re.S)
CLASS = re.compile(r'class="([^"]*)"')
TAGS = re.compile(r"<[^>]+>")

# Адреса, по которым обход не ходит. Выход из системы оборвал бы обход на первой
# же странице, смена языка — состояние, а выгрузки отдают файл, а не экран.
SKIP = ("/logout/", "/dev/logout/", "/i18n/setlang/", "/demo/")


def cells(html: str):
    """Ячейки страницы: классы и видимый текст."""
    for match in CELL.finditer(html):
        classes = CLASS.search(match.group(2))
        text = TAGS.sub("", match.group(3))
        yield match.group(1), (classes.group(1) if classes else ""), text.strip()


# Роли, которыми ходит обход. Одной роли мало: экран, которого роль не ведёт,
# ей не показан вовсе — бухгалтер не видит справочников, а администратор сети не
# открывает ведомость. Обход одной ролью нашёл бы половину продукта и молчал бы
# про вторую.
WALKERS = ("accountant", "admin")


@pytest.fixture
def calculated(client, web_env):
    """Посчитанный месяц: без него ведомости на странице нет вовсе.

    Своя копия, а не импорт из соседнего файла: связывать модули ради трёх
    строк дороже, чем повторить их (тот же довод записан в других файлах,
    считающих период).
    """
    from conftest import period_url, wipe_payruns

    wipe_payruns(web_env)
    login_as(client, "director")
    assert client.post(period_url(client) + "calculate/", follow=True).status_code == 200
    client.post("/logout/")


def one_per_kind(url: str) -> str:
    """Вид экрана: адрес без опознавателя записи.

    Нужно обходу. Карточек сотрудников в сиде тридцать пять, и без этого обход
    целиком уходил в них — до ведомости, расходов и счетов он просто не
    доезжал, а проверка при этом оставалась зелёной. Разметка у карточек одна и
    та же, поэтому смотрим по две на вид.
    """
    parts = [p for p in url.split("/") if p]
    return "/".join(p for p in parts if not re.fullmatch(r"[0-9a-f-]{8,}", p))


@pytest.fixture
def screens(client, web_env, calculated):
    """Экраны продукта — обходом ссылок, а не списком.

    Список адресов в тесте разъезжается с продуктом молча: новый экран в него
    просто не попадёт, и проверка останется зелёной, ничего не проверяя.
    """
    from django.test import Client

    found: list[str] = []
    for role in WALKERS:
        walker = Client()
        login_as(walker, role)
        seen: set[str] = set()
        kinds: dict[str, int] = {}
        queue = ["/periods/", "/expenses/", "/invoices/", "/inbox/"]
        while queue and len(seen) < 80:
            url = queue.pop(0)
            kind = one_per_kind(url)
            if url in seen or kinds.get(kind, 0) >= 2:
                continue
            seen.add(url)
            kinds[kind] = kinds.get(kind, 0) + 1
            response = walker.get(url)
            if response.status_code != 200:
                continue
            page = body(response)
            if url not in found:
                found.append(url)
            # Ссылки на файл (`download`) пропускаются: они отдают xlsx, а не
            # экран, и разметки ячеек в них нет вовсе.
            for tag, href in re.findall(r"<a\b([^>]*)href=\"(/[^\"#?]*)\"", page):
                if "download" in tag or href in seen or any(s in href for s in SKIP):
                    continue
                queue.append(href)
        walker.post("/logout/")
    assert len(found) > 12, f"обход нашёл всего {len(found)} экранов: {found}"
    # Ведомость обязана попасть в обход: она главная таблица задачи, и обход,
    # до неё не доехавший, проверяет что угодно, кроме главного.
    assert any(re.fullmatch(r"/periods/[0-9a-f-]+/", u) for u in found), (
        f"обход не дошёл до ведомости: {found}"
    )
    return found


# --- разметка на настоящих страницах ------------------------------------------


def test_a_dash_is_never_dressed_up_as_a_number(client, web_env, screens):
    """Пустое значение помечено пустым, а не набрано как сумма.

    Ловится ровно то, что видит человек: прочерк в общем весе строки читается
    как число, которого нет, и таблица из прочерков спорит с таблицей из сумм.
    """
    loud: list[str] = []
    seen_dashes = 0
    for role in WALKERS:
        login_as(client, role)
        for url in screens:
            response = client.get(url)
            if response.status_code != 200:
                continue
            for _tag, classes, text in cells(body(response)):
                words = classes.split()
                if "num" not in words or text != EMPTY:
                    continue
                seen_dashes += 1
                if "num--empty" not in words:
                    loud.append(f'{url}: <td class="{classes}">{text}')
        client.post("/logout/")
    assert not loud, (
        "прочерк набран как число (нужен фильтр `numclass`):\n"
        + "\n".join(dict.fromkeys(loud))
    )
    # Предохранитель: проверка обязана была увидеть прочерки. Зелёная на нуле
    # найденных ячеек, она не значила бы ничего — а именно так она и зеленела
    # бы, если обход перестанет доезжать до ведомости.
    assert seen_dashes > 20, f"проверка нашла всего {seen_dashes} прочерков"


def test_every_amount_in_the_payroll_sheet_sits_in_a_marked_cell(client, web_env, calculated):
    """Ведомость целиком: ни одной суммы в непомеченной ячейке.

    Ведомость — главная таблица продукта и самая широкая: тридцать строк на
    двадцать колонок. Одна непомеченная ячейка здесь ломает не строку, а
    столбец: соседние числа перестают стоять разряд под разрядом.
    """
    from conftest import period_url

    login_as(client, "accountant")
    page = body(client.get(period_url(client)))
    sheet = page.split('<table class="sheet">', 1)
    assert len(sheet) == 2, "ведомости на странице нет — проверять нечего"
    table = sheet[1].split("</table>", 1)[0]

    # Число в ячейке: знак прочерка либо цифры с разделителями. Подписи
    # сотрудников и точек под это не подходят, поэтому список ячеек собирается
    # по виду содержимого, а не по номеру колонки.
    looks_like_number = re.compile(rf"^(?:{re.escape(EMPTY)}|-?[\d  .,]+)$")
    unmarked = [
        f'<td class="{classes}">{text}'
        for tag, classes, text in cells(table)
        if tag == "td" and text and looks_like_number.match(text)
        and "num" not in classes.split()
    ]
    assert not unmarked, "суммы в ведомости вне числовой ячейки:\n" + "\n".join(unmarked)

    # И обратное: помеченные ячейки в ведомости есть вообще. Без этого проверка
    # выше зеленела бы на странице без таблицы.
    marked = [c for tag, c, _ in cells(table) if tag == "td" and "num" in c.split()]
    assert len(marked) > 20, f"числовых ячеек в ведомости всего {len(marked)}"


def test_the_sheet_shows_both_dashes_and_amounts_at_once(client, web_env, calculated):
    """Ведомость действительно смешивает пустое и заполненное.

    Проверка-предохранитель для двух предыдущих: если бы в сиде у каждого
    сотрудника оказались заполнены все компоненты, они зеленели бы, не увидев
    ни одного прочерка, — и главный дефект задачи остался бы непроверенным.
    """
    from conftest import period_url

    login_as(client, "accountant")
    page = body(client.get(period_url(client)))
    table = page.split('<table class="sheet">', 1)[1].split("</table>", 1)[0]
    kinds = {
        "прочерк": 0,
        "сумма": 0,
    }
    for tag, classes, text in cells(table):
        if tag != "td" or "num" not in classes.split():
            continue
        kinds["прочерк" if text == EMPTY else "сумма"] += 1 if text else 0
    assert kinds["прочерк"] > 0 and kinds["сумма"] > 0, (
        f"в ведомости нет обоих видов ячеек: {kinds}"
    )


# --- сам фильтр ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("shown", "expected"),
    [
        # Прочерк — сообщение о пустоте, и вес на странице у него меньше.
        (EMPTY, "num num--empty"),
        # Ноль — цифра. Тот же класс, что у остальных чисел столбца: бледный
        # ноль начал бы читаться как отсутствие значения.
        ("0,00", "num"),
        ("0.00", "num"),
        ("0", "num"),
        ("1 234,50", "num"),
        ("-6 089,00", "num"),
        # Обрезанный хвост точной величины (`web.format.exact`) — число.
        ("0,701234…", "num"),
        # Пустая строка приезжает со страниц, где считать ещё нечего (сверка до
        # загрузки файла). Знака в ячейке нет вовсе — значит и веса ей не нужно.
        ("", "num num--empty"),
        (None, "num num--empty"),
        # Пробелы вокруг знака приходят от разметки шаблона, а не от данных.
        (f"  {EMPTY} ", "num num--empty"),
    ],
)
def test_the_filter_tells_a_dash_from_a_zero(shown, expected):
    from web.templatetags.ui import numclass

    assert numclass(shown) == expected


def test_the_dash_is_read_from_the_product_not_written_twice():
    """Знак прочерка объявлен один раз на продукт.

    Своя копия знака в фильтре разъехалась бы с `web.format` молча: там прочерк
    и рождается, и поменять его пришлось бы в двух местах.
    """
    source = (ROOT / "src" / "web" / "templatetags" / "ui.py").read_text(encoding="utf-8")
    assert "from web.format import EMPTY" in source
    literal = [f'"{EMPTY}"', f"'{EMPTY}'"]
    assert not [q for q in literal if q in source], (
        "знак прочерка написан в фильтре вторым разом"
    )


# --- оформление ---------------------------------------------------------------


def test_the_stylesheet_makes_a_column_out_of_marked_cells():
    """Правый край, моноширинное и табличные цифры — в листе, а не на словах."""
    css = APP_CSS.read_text(encoding="utf-8")
    rule = css.split(".num {", 1)
    assert len(rule) == 2, "правила `.num` в листе нет"
    body_of_rule = rule[1].split("}", 1)[0]
    assert "text-align: right" in body_of_rule, "числа не по правому краю"
    assert "font-variant-numeric: tabular-nums" in body_of_rule, (
        "цифры не табличные: в пропорциональном начертании единица уже семёрки, "
        "и столбик перестаёт складываться в разряды"
    )
    assert "var(--num)" in body_of_rule, "моноширинного начертания нет"


def test_the_dash_and_the_zero_are_painted_differently():
    """Разница между «нет значения» и «ноль» — в цвете, а не только в знаке."""
    css = APP_CSS.read_text(encoding="utf-8")
    assert ".num--empty { color: var(--ink-empty); }" in css, (
        "у прочерка нет своего цвета — на странице он весит как сумма"
    )
    # Цвет прочерка — свой токен, а не общий серый: `--ink-empty` объявлен в
    # эталоне именно «только для прочерка», и подмена его на `--ink-3` вернула
    # бы прочерк в общий вес приглушённых чисел.
    tokens = (ROOT / "src" / "web" / "static" / "web" / "tokens.css").read_text(
        encoding="utf-8"
    )
    assert "--ink-empty:" in tokens


# --- экраны, которые уже переехали, назад не откатываются ---------------------

# Шаблоны, где числовые ячейки собраны фильтром. Список нужен как замок: экраны
# переезжают на дизайн-систему по одному (правило владельца), и заставлять
# непереехавший экран здесь нечего, а вот переехавший обязан таким остаться.
MIGRATED = [
    "web/period.html",
    "web/cash/expenses.html",
    "web/cash/unallocated.html",
    "web/suppliers/invoices.html",
    "web/suppliers/invoice.html",
    "web/suppliers/inbox.html",
]


@pytest.mark.parametrize("name", MIGRATED)
def test_a_migrated_screen_keeps_its_cells_built_by_the_filter(name):
    """В переехавшем экране числовой ячейки с зашитым классом не осталось.

    `class="num"` руками — это ячейка, в которой прочерк снова весит как сумма.

    Что считается числовой ячейкой: та, что **показывает значение** (`{{ … }}`),
    в теле или в подвале таблицы, — и `td`, и `th` одинаково. Разделение по виду
    тега было бы неверным: главное число таблицы стоит в подвале и набрано
    там `<th>` — первая версия этой проверки смотрела только на `<td>` и
    итоговые суммы четырёх экранов пропустила (нашёл исполнитель, сверяя
    контракт с разметкой).

    Заголовки колонок исключены по месту, а не по тегу: `<thead>` — подписи над
    столбцами, и «прочерка» в них не бывает.
    """
    text = (ROOT / "src" / "web" / "templates" / name).read_text(encoding="utf-8")
    # Подписи столбцов выкидываются целыми блоками `<thead>`: в них тоже бывает
    # `{{ column }}`, но это название компонента, а не сумма.
    values = re.sub(r"<thead\b.*?</thead>", "", text, flags=re.S)
    hardcoded = [
        cell
        for cell in re.findall(r'<t[dh][^>]*class="num[^"{]*"[^>]*>.*?</t[dh]>', values, re.S)
        if "{{" in cell
    ]
    assert not hardcoded, (
        f"{name}: числовые ячейки мимо фильтра:\n" + "\n".join(hardcoded)
    )
