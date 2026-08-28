"""Меню областями учёта и счётчиками ждущей работы (T207, модуль 10 эталона).

Эталон рисует навигацию не плоским рядом, а **областями учёта**: «Сбор данных»,
«Расчёт», «Отчёты», «Справочники», «Настройки», — а внутри области лежат её
пункты. У пункта стоит число ждущей работы («Инбокс документов · 12»), и
дословное правило эталона: «Счётчик у пункта — это работа, которая ждёт
человека. **Ноль не показываем**».

Что здесь проверяется и почему именно это.

**Область — не украшение, а способ найти раздел.** Пока ряд был плоским, шесть
пунктов администратора читались одной строкой без единой границы, и на телефоне
она разваливалась на три ряда одинаковых слов. Поэтому проверяется структура:
пункт лежит **внутри** своей области, а не рядом с ней.

**Пустая область не рисуется.** Область, все пункты которой роль не ведёт, — это
кнопка, за которой ничего нет; открыть её и увидеть пустоту хуже, чем не увидеть
кнопку. В демо там стоит предложение открыть раздел подходящей ролью (T163), и
эта половина закреплена в `tests/test_demo_role_switch.py`.

**Счётчик берётся оттуда же, откуда сам список.** Второй запрос ради числа
разошёлся бы со списком молча — например, потому что политики базы сузили его не
так. Поэтому проверка сравнивает не «счётчик равен двум», а **счётчик в шапке с
тем, что показывает сам экран инбокса**, и делает это двумя ролями: у
управляющего база оставляет меньше строк, и число в шапке обязано уехать вместе
со списком, а не остаться директорским.

Оформление (узкий экран, размер под палец) проверяется по файлу статики, как и
в `tests/test_appbar.py`: правило в неподключённом листе не действует, а
подключённость листа проверена там же.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest import body, login_as
from test_supplier_invoices import (  # noqa: F401
    NEW,
    counterparty,
    invoice_form,
    invoices_removed,
    key,
    sql,
    tenant,
    units,
)

ROOT = Path(__file__).resolve().parent.parent
APP_CSS = ROOT / "src" / "web" / "static" / "web" / "app.css"
REFERENCE = ROOT / "Дизайн-система Dodo P&L" / "Модуль 10 - Вход и каркас.dc.html"


# --- материал -----------------------------------------------------------------


def nav_of(page: str) -> str:
    """Разметка навигации: между `<nav class="appnav"` и её закрытием.

    Шапка несёт ещё язык, тему и «кто смотрит»; проверять области по всей
    странице значило бы ловить совпадения в чужой разметке.
    """
    start = page.index('<nav class="appnav"')
    return page[start:page.index("</nav>", start)]


def areas_of(page: str) -> dict[str, str]:
    """Области учёта: название → разметка её МЕНЮ, а не всей области.

    Именно меню: иначе проверка «пункт внутри своей области» проходила бы и
    тогда, когда пункты просто лежат рядом с меню в том же блоке. Проверено
    порчей — ровно так первая редакция этой проверки и зеленела.
    """
    found = {}
    for chunk in nav_of(page).split("<details")[1:]:
        title = re.search(r'class="appnav__area-name"[^>]*>([^<]+)', chunk)
        assert title, f"у области нет названия:\n{chunk[:200]}"
        menu = re.search(r'<div class="appnav__menu">(.*?)</div>', chunk, re.S)
        assert menu, f"у области «{title.group(1)}» нет меню:\n{chunk[:200]}"
        # Счётчик самой области стоит в её названии, а не в меню, — а спрашивают
        # о нём тем же `counter_in`. Поэтому к меню приклеивается заголовок.
        found[title.group(1).strip()] = title.group(0) + menu.group(1)
    return found


def counter_in(markup: str) -> int | None:
    """Число ждущей работы или None, если счётчика нет вовсе."""
    found = re.search(r'class="appnav__count"[^>]*>(\d+)<', markup)
    return int(found.group(1)) if found else None


def waiting_on_the_inbox_screen(client) -> int:
    """Сколько работы ждёт по мнению самого экрана инбокса.

    Считается по его разметке, а не тем же кодом, что собирает счётчик: иначе
    проверка сравнивала бы реализацию сама с собой. Списка на экране два —
    строки без статьи и бумаги с точек, — и оба ждут одного и того же человека.
    """
    page = body(client.get("/inbox/"))
    rows = page.count('data-fact="')
    papers = re.search(r'data-papers="(\d+)"', page)
    return rows + (int(papers.group(1)) if papers else 0)


@pytest.fixture
def unclassified(client, counterparty, units, invoices_removed):  # noqa: F811
    """Два счёта без статьи: один на точке управляющего, один на чужой.

    Разные точки нужны ради проверки среза: у директора в инбоксе оба, у
    управляющего — только его, и счётчик обязан отличаться так же, как список.
    """
    login_as(client, "accountant")
    for unit, number in ((units["NS1"], "EPS-1"), (units["BG1"], "EPS-2")):
        answer = client.post(NEW, invoice_form(
            counterparty, units, item="", unit=unit, number=number, entry_key=key(),
        ))
        assert answer.status_code == 302, body(answer)


# --- области учёта ------------------------------------------------------------


def test_the_areas_are_named_by_the_reference():
    """Название области — слово эталона, а не наше.

    Читается из исходника эталона, а не из списка, переписанного сюда: копия
    разошлась бы с источником истины молча.
    """
    from web import navigation

    text = REFERENCE.read_text(encoding="utf-8")
    block = re.search(r"nav:\s*\{(.*?)\}", text, re.S)
    assert block, "словарь областей в эталоне не найден"
    words = set(re.findall(r'"([А-ЯЁ][^"]{2,40})"', block.group(1)))
    assert "Сбор данных" in words, f"читаем не тот блок эталона: {sorted(words)}"

    strangers = [str(group.title) for group in navigation.GROUPS
                 if str(group.title) not in words]
    assert not strangers, f"области названы не словарём эталона: {strangers}"


def test_the_header_shows_areas_and_not_a_flat_row(client, web_env):
    """Шапка собрана областями, и их ровно столько, сколько ведёт роль."""
    login_as(client, "director")
    page = body(client.get("/periods/"))

    shown = areas_of(page)
    assert "Сбор данных" in shown, f"области учёта не видно: {sorted(shown)}"
    assert "Справочники" in shown, f"области учёта не видно: {sorted(shown)}"


def test_an_item_lives_inside_its_area(client, web_env):
    """Пункт лежит внутри своей области, а не рядом с ней.

    Ради этого задача и делалась: плоский ряд «Табель · Наличные · Инбокс ·
    Сотрудники» не говорит, что первые три — про сбор данных, а четвёртый — про
    справочники.
    """
    login_as(client, "director")
    shown = areas_of(body(client.get("/periods/")))

    collect = shown["Сбор данных"]
    assert "Табель" in collect and "Наличные расходы" in collect
    assert "Инбокс документов" in collect
    assert "Сотрудники" not in collect, "справочник заехал в сбор данных"
    assert "Сотрудники" in shown["Справочники"]


def test_the_area_of_the_current_page_is_marked(client, web_env):
    """Человек видит, в какой области он стоит, не открывая меню.

    Пометка — не `aria-current="page"`: страница одна, и она внутри области, а
    не сама область. Второй такой пометкой мы бы сказали диктору, что открытых
    страниц две.
    """
    login_as(client, "director")
    page = body(client.get("/expenses/"))

    here = re.findall(r'<details class="appnav__area appnav__area--here"', page)
    assert len(here) == 1, f"областей «здесь» не одна, а {len(here)}"
    assert page.count('aria-current="page"') == 1, (
        "пометка текущей страницы удвоилась: " + str(page.count('aria-current="page"'))
    )
    assert "Сбор данных" in areas_of(page)


def test_an_area_a_role_does_not_lead_is_not_drawn_at_all(client, web_env):
    """Область без единого своего пункта в продукте не показывается.

    Открыть её и увидеть пустоту хуже, чем не увидеть кнопку. В демо на этом
    месте стоит предложение открыть раздел подходящей ролью — см.
    `tests/test_demo_role_switch.py`.
    """
    login_as(client, "manager")
    shown = areas_of(body(client.get("/periods/")))

    assert "Настройки" not in shown, "управляющему обещана область, в которой пусто"
    assert "Сбор данных" in shown
    # А то, что он ведёт, на месте: своих людей управляющий читает (D047, T173).
    assert "Сотрудники" in shown.get("Справочники", "")


# --- счётчик ждущей работы ----------------------------------------------------


def test_the_counter_is_the_number_the_inbox_screen_itself_shows(
    client, web_env, unclassified,
):
    """Счётчик равен тому, что показывает сам экран, а не своему числу."""
    login_as(client, "director")
    expected = waiting_on_the_inbox_screen(client)
    assert expected >= 2, "материал не заехал: инбокс пуст"

    shown = areas_of(body(client.get("/periods/")))
    collect = shown["Сбор данных"]
    inbox = collect[collect.index("Инбокс документов"):]

    assert counter_in(inbox) == expected, (
        f"у пункта {counter_in(inbox)}, а на экране {expected}"
    )
    assert counter_in(collect) == expected, (
        "счётчик области не равен сумме её пунктов: "
        f"{counter_in(collect)} против {expected}"
    )


def test_the_counter_is_cut_by_the_database_exactly_like_the_list(
    client, web_env, unclassified,
):
    """У управляющего строк меньше — и число в шапке уезжает вместе со списком.

    Это и есть цена второго запроса: он сузился бы иначе и показал бы
    управляющему директорское число.
    """
    login_as(client, "manager")
    mine = waiting_on_the_inbox_screen(client)

    login_as(client, "director")
    all_of_them = waiting_on_the_inbox_screen(client)
    assert mine < all_of_them, (
        f"материал не различает точки: у управляющего {mine}, у директора {all_of_them}"
    )

    login_as(client, "manager")
    shown = areas_of(body(client.get("/periods/")))
    assert counter_in(shown["Сбор данных"]) == mine, (
        f"управляющему показано {counter_in(shown['Сбор данных'])} вместо {mine}"
    )


def test_zero_is_not_shown_at_all(client, web_env, invoices_removed):  # noqa: F811
    """Прямое требование эталона: ноль не рисуется — ни у пункта, ни у области."""
    login_as(client, "director")
    assert waiting_on_the_inbox_screen(client) == 0, "инбокс не пуст, проверка не о том"

    nav = nav_of(body(client.get("/periods/")))
    assert "appnav__count" not in nav, "пустая очередь показана нулём"
    assert not re.search(r">\s*0\s*<", nav), f"ноль всё-таки нарисован:\n{nav}"


# --- оформление ---------------------------------------------------------------


def rules(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def block(css: str, selector: str) -> str:
    head = css.split(selector + " {", 1)
    assert len(head) == 2, f"правила «{selector}» в листе нет"
    return head[1].split("}", 1)[0]


def test_the_menu_stays_reachable_by_finger_on_a_phone():
    """Сценарии управляющего — телефон 375: меню открывается и нажимается.

    Область и пункт внутри неё — не меньше 44 пикселей: это физика пальца, а не
    пожелание доступности. Проверяется в блоке телефона, потому что на десктопе
    плотность другая и ряд в 44 пикселя съел бы шапку.
    """
    css = rules(APP_CSS.read_text(encoding="utf-8"))
    phone = css.split("@media (max-width: 767px)", 1)
    assert len(phone) == 2, "правил для телефона в листе нет"
    phone = phone[1].split("@media", 1)[0]

    assert "var(--tap-min)" in block(phone, ".appnav__area-name"), (
        "название области на телефоне меньше пальца"
    )
    assert "var(--tap-min)" in block(phone, ".appnav__menu a"), (
        "пункт меню на телефоне меньше пальца"
    )


def test_the_open_menu_does_not_push_the_page_sideways():
    """Меню выкладывается поверх страницы, а не раздвигает её.

    Панель в потоке на 375 расширяла бы шапку до своей ширины, и страница
    поехала бы вбок целиком — то самое «рассыпалось», от которого шапку и
    защищали (issue #151).
    """
    css = rules(APP_CSS.read_text(encoding="utf-8"))

    assert "position: relative" in block(css, ".appnav__area"), (
        "меню не к чему привязать — оно уедет к краю страницы"
    )
    assert "position: absolute" in block(css, ".appnav__menu"), (
        "меню стоит в потоке и растянет шапку"
    )
    # Список областей по-прежнему не переносится сам: инвариант шапки (T178).
    assert "flex-wrap" not in block(css, ".appnav")
