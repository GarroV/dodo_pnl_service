"""Шапка и каркас по разделу 08 дизайн-системы (T178).

Что здесь проверяется и почему именно это.

**Где человек сейчас.** Раздел, в котором он находится, — не ссылка, а
`span[aria-current="page"]`. Ссылка на открытую страницу обманывает дважды:
мышь обещает переход, которого не будет, а экранный диктор перечисляет её
наравне с остальными и не сообщает, где человек стоит. Тот же инвариант живёт у
переключателя разреза ведомости, и проверяется он здесь по той же причине.

**Выделение держится на вложенном экране.** Карточка платежа лежит под своим
корнем адреса (`/payments/…`), но принадлежит разделу «Счета». Пункт,
потерявший выделение, читается как «вы ушли из раздела», хотя человек внутри
него. Проверяется на настоящей странице, а не только на фильтре: карта
`NAV_BELONGS` может быть верной, а шапка — собранной мимо неё.

**Роль — одна плашка.** В эталоне рядом нарисованы все выданные наборы прав; у
продукта решение владельца другое — у человека один уровень. Расхождение
разбирается отдельно (issue #130) и оформлением не решается, поэтому здесь
проверяется именно решение владельца: плашка ровно одна на страницу.

**Раздел, которого роль не ведёт, не показывается вовсе.** Ссылка на экран,
который ответит отказом, хуже отсутствующей кнопки: человек уходит со своей
страницы, чтобы прочитать «вам нельзя».

Проверки оформления идут по файлу статики, а не по разметке страницы, потому
что лист с T177 живёт в статике. Рядом с каждой стоит проверка, что страница
этот файл просит: правило в неподключённом листе не действует.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest import body, login_as

ROOT = Path(__file__).resolve().parent.parent
APP_CSS = ROOT / "src" / "web" / "static" / "web" / "app.css"


def rules(css: str) -> str:
    """Лист без комментариев: проверяем правила, а не объяснения к ним.

    Нужно буквально: в этом листе объяснено, почему `min-width` у body **не**
    ставится, и проверка «нет ли min-width» ловила собственный комментарий.
    """
    import re

    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def block(css: str, selector: str) -> str:
    """Тело одного правила по его селектору."""
    head = css.split(selector + " {", 1)
    assert len(head) == 2, f"правила «{selector}» в листе нет"
    return head[1].split("}", 1)[0]


# --- где человек сейчас -------------------------------------------------------


def test_the_current_section_is_not_a_link(client, web_env):
    """Открытый раздел — `span[aria-current]`, и ссылки на него в шапке нет."""
    login_as(client, "accountant")
    page = body(client.get("/periods/"))

    assert '<span class="appnav__current" aria-current="page">' in page, (
        "текущий раздел не помечен для клавиатуры и диктора"
    )
    assert 'class="appnav__link" href="/periods/"' not in page, (
        "шапка предлагает уйти на страницу, которая уже открыта"
    )
    # Остальные разделы ссылками остаются — иначе навигации нет вовсе.
    assert 'class="appnav__link" href="/expenses/"' in page


def test_a_nested_screen_keeps_its_section_highlighted(client, web_env):
    """Карточка платежа лежит под своим корнем адреса, а раздел — «Счета».

    Проверяется на настоящей странице: карта `NAV_BELONGS` может быть верной, а
    шапка — собранной мимо неё, и тогда выделение пропадает на каждом вложенном
    экране.
    """
    login_as(client, "accountant")
    page = body(client.get("/payments/new/"))

    assert 'class="appnav__link" href="/invoices/"' not in page, (
        "на вложенном экране раздел «Счета» снова стал ссылкой"
    )
    assert '<span class="appnav__current" aria-current="page">' in page


def test_the_section_of_another_root_is_not_highlighted(client, web_env):
    """Выделен один раздел, а не все с похожим адресом."""
    login_as(client, "accountant")
    page = body(client.get("/expenses/"))

    assert page.count('aria-current="page"') == 1, (
        "выделенных разделов не один: " + str(page.count('aria-current="page"'))
    )
    assert 'class="appnav__link" href="/periods/"' in page


# --- фильтр «человек в этом разделе» -----------------------------------------


@pytest.mark.parametrize(
    ("path", "section", "same"),
    [
        ("/periods/", "/periods/", True),
        # Вложенные экраны остаются в своём разделе: след расчёта и табель
        # открываются из ведомости, карточка платежа и инбокс — из счетов.
        ("/periods/1234/", "/periods/", True),
        ("/payslips/1234/trace/", "/periods/", True),
        ("/timesheets/1234/", "/periods/", True),
        ("/payments/new/", "/invoices/", True),
        ("/inbox/", "/invoices/", True),
        # Чужой раздел — не выделяется. `/expenses/` и `/invoices/` близкие по
        # смыслу, но это два разных экрана и два разных корня.
        ("/expenses/", "/invoices/", False),
        ("/directory/employees/", "/periods/", False),
        ("/", "/periods/", False),
        # Пустой адрес раздела — не «совпало с корнем», а «сравнивать нечего».
        ("/periods/", "", False),
    ],
)
def test_in_section_compares_roots_not_whole_addresses(path, section, same):
    from web.templatetags.ui import in_section

    assert in_section(path, section) is same, f"{path} против {section}"


# --- кем вошли ---------------------------------------------------------------


@pytest.mark.parametrize("role", ["director", "accountant", "manager", "admin"])
def test_the_header_shows_exactly_one_role(client, web_env, role):
    """Плашка роли одна: решение владельца — у человека один уровень."""
    login_as(client, role)
    page = body(client.get("/periods/"))

    assert page.count('class="role"') == 1, (
        f"{role}: плашек роли {page.count('class=\"role\"')}, а должна быть одна"
    )


def test_the_header_says_whose_eyes_these_numbers_are(client, web_env):
    """Партнёр, регистры и точка — на экране, без наведения мыши.

    Управляющий видит суммы меньше директорских, и объяснение этому обязано
    стоять рядом с числами, а не в справке.
    """
    login_as(client, "manager")
    page = body(client.get("/periods/"))

    assert 'class="viewer__meta"' in page
    assert "регистры:" in page, "не сказано, что видно"
    assert "точка:" in page, "срез по точке не объяснён — суммы меньше без причины"


def test_the_full_access_role_is_not_told_it_is_limited_to_a_unit(client, web_env):
    """Точка называется только там, где человек ею ограничен.

    Пусто здесь значит «все точки партнёра»: объяснять надо срезанные данные, а
    не полные.
    """
    login_as(client, "accountant")
    page = body(client.get("/periods/"))

    assert "точка:" not in page, "бухгалтеру приписали ограничение по точке"


# --- разделы по правам -------------------------------------------------------


def test_a_role_does_not_see_sections_it_does_not_lead(client, web_env):
    """Справочники и правила ведёт администратор сети, и только он."""
    login_as(client, "manager")
    page = body(client.get("/periods/"))

    for absent in ("/directory/", "/rules/"):
        assert f'href="{absent}"' not in page, (
            f"управляющему обещан раздел {absent}, который ответит отказом"
        )
    # А то, что видит каждый вошедший, — на месте: права на внесение первичных
    # данных в продукте нет, расход отвергают точка и регистр.
    assert 'href="/expenses/"' in page and 'href="/invoices/"' in page


def test_the_role_that_leads_the_directory_sees_it(client, web_env):
    """Обратная сторона той же проверки: скрыто по праву, а не всегда."""
    login_as(client, "admin")
    page = body(client.get("/periods/"))

    assert 'href="/directory/"' in page and 'href="/rules/"' in page


# --- оформление: обход табом и узкий экран -----------------------------------


def test_the_page_asks_for_the_stylesheet_that_carries_all_this():
    """Правило в неподключённом листе не действует."""
    from django.template.loader import render_to_string

    # Шапка проверяется на настоящих страницах выше; здесь достаточно того, что
    # базовый шаблон просит лист. Рисуется без запроса — контекст ему не нужен.
    page = render_to_string("web/base.html")
    assert "web/app.css" in page and "web/tokens.css" in page


def test_focus_is_visible_on_everything_reachable_by_tab():
    """Фокус — на всём, чем можно управлять, а не только на кнопках и ссылках.

    До T177 правило стояло на `button` и `a`. Бухгалтер вводит числа с
    клавиатуры, и поле ввода, потерявшее видимый фокус, — это потерянное место
    в ведомости на шестьдесят строк.
    """
    css = APP_CSS.read_text(encoding="utf-8")
    assert ":focus-visible { outline:" in css, "видимого фокуса в листе нет"
    assert "button:focus-visible, a:focus-visible" not in css, (
        "фокус снова обещан только кнопкам и ссылкам"
    )


def test_the_appbar_does_not_fall_apart_on_a_tablet():
    """На 768 шапка переносится на две строки, а не едет вбок вместе со страницей.

    Требование продукта — работоспособность на 768. В эталоне за узкий экран
    отвечает `min-width` у body: страница держит 1280 и прокручивается целиком.
    Здесь так нельзя — прокрутка всей страницы вместе с шапкой и есть
    «рассыпалось», поэтому проверяется именно перенос.
    """
    css = rules(APP_CSS.read_text(encoding="utf-8"))
    narrow = css.split("@media (max-width: 1023px)", 1)
    assert len(narrow) == 2, "правил для планшета в листе нет"
    assert "flex-wrap: wrap" in block(narrow[1], ".appbar"), (
        "шапка на планшете не переносится"
    )
    assert "min-width" not in block(css, "html, body"), (
        "у body появилась фиксированная ширина — страница поедет вбок целиком"
    )


def test_the_tap_targets_of_the_unit_manager_are_finger_sized():
    """Сценарии управляющего — телефон 375: всё нажимаемое не меньше 44 пикселей."""
    css = APP_CSS.read_text(encoding="utf-8")
    phone = css.split("@media (max-width: 767px)", 1)
    assert len(phone) == 2, "правил для телефона в листе нет"
    assert "var(--tap-min)" in phone[1].split("@media", 1)[0], (
        "размер под палец не задан"
    )


def test_the_appbar_grows_instead_of_letting_its_content_hang_out():
    """Шапка не смеет иметь фиксированную высоту, пока внутри может что-то перенестись.

    Замерено в браузере на 1440 (issue #151): `.appnav` 62 пикселя при `.appbar`
    50, верх первого пункта на **минус шести** — то есть «Сотрудники» висели
    ниже границы шапки, а часть навигации уходила выше неё. И у оперативного
    директора, и у администратора сети, у которого разделов шесть.
    Фиксированная высота перенесённую строку не «держит», она её обрезает.

    Поэтому здесь три правила, и каждое закрывает свою половину дефекта:

    * у `.appbar` — `min-height`, а не `height`: при одной строке высота та же
      50 из эталона, при переносе шапка растёт за содержимым;
    * `flex-wrap` — у `.appbar`, чтобы вниз уезжал правый край целиком;
    * `flex-wrap` — НЕ у `.appnav`: перенос внутри навигации рвёт список
      разделов посередине. В эталоне у `.appnav` его и нет, а reflow шапки
      описан ровно как `height: auto; flex-wrap: wrap`.

    Порогом ширины это не решается, и проверка стоит именно на правилах, а не
    на числе: у продукта три языка и шесть ролей, длина правого края у них
    разная, и ширина, на которой одна роль ещё влезает, для другой уже мала.
    Перенос по содержимому верен для всех, порог — ни для кого.
    """
    css = rules(APP_CSS.read_text(encoding="utf-8"))
    bar = block(css, ".appbar")

    assert "min-height: 50px" in bar, "у шапки нет минимальной высоты из эталона"
    assert not re.search(r"[;{\s]height\s*:", bar), (
        "у шапки снова фиксированная высота — перенесённая строка окажется "
        "снаружи, как в issue #151"
    )
    assert "flex-wrap: wrap" in bar, "шапка не переносится, значит переносить будет навигацию"
    assert "flex-wrap" not in block(css, ".appnav"), (
        "навигация снова переносится сама: список разделов рвётся посередине, "
        "а высоту шапки это не меняет"
    )

    # На узком экране перенос навигации нужен: шапка там уже растёт по
    # содержимому, а шесть разделов в одну строку на телефоне не встают.
    narrow = css.split("@media (max-width: 1023px)", 1)
    assert len(narrow) == 2, "правил для планшета в листе нет"
    assert "flex-wrap: wrap" in block(narrow[1], ".appnav"), (
        "на узком экране навигации перенос не разрешён — она поедет вбок"
    )
