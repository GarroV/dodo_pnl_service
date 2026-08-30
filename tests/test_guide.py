"""Гайд по продукту внутри продукта (T159).

Что здесь проверяется и почему именно это.

**Открыт до входа.** Половина вопроса, на который гайд отвечает, — «кем
входить»; страница, видимая только после входа, на него ответить не может по
устройству. Поэтому проверка ходит анонимным клиентом, а не вошедшим.

**Найти его можно с любого экрана.** Ссылка живёт в шапке, то есть на каждой
странице, — и проверяется это на двух разных экранах, а не на одном: шапка
собирается из веток по правам, и «есть на периодах» не означает «есть на форме
входа». Ровно там она и нужнее всего.

**Роли не переписаны заново.** В продукте уже было два списка ролей, и они
разъехались молча (issue #91). Гайд — самое опасное место для третьего: страница,
которая объясняет продукт, врёт убедительнее, чем строка в сиде. Поэтому
проверяется не «на странице написано четыре роли», а что перечислены ровно те,
что лежат в `core.roles`, и названия действий взяты из `web.permissions`.

**Регистры не названы непредставившемуся.** Список названий регистров — сам по
себе сообщение о том, что регистр существует (D023). На этом уже спотыкались на
форме входа: панель быстрого входа перечисляла все три регистра и код точки до
входа (T096). Гайд объясняет механизм, а имена показывает только тому, кто вошёл,
и только свои.

**Гайд не спрашивает базу.** Это не оптимизация, а то же требование «открыт до
входа», записанное так, чтобы его нельзя было потерять правкой: первый же запрос
к базе на этой странице означает, что она начала зависеть от данных партнёра.
"""
from __future__ import annotations

import pytest

from conftest import body, login_as

GUIDE = "/guide/"


@pytest.fixture
def stranger(web_env):
    """Клиент, который никем не вошёл: гайд обязан открываться и ему."""
    from django.test import Client

    return Client()


# --- открыт до входа ---------------------------------------------------------


def test_the_guide_opens_without_signing_in(stranger):
    """Первый вопрос новичка — «что это вообще», и задаёт он его до входа."""
    response = stranger.get(GUIDE)
    assert response.status_code == 200, response.status_code
    assert "Как это работает" in body(response)


# Что база получает на ЛЮБОЙ запрос продукта, независимо от страницы: открытие
# транзакции (`ATOMIC_REQUESTS`) и выставление контекста доступа
# (`web.dbcontext` — роль приложения и `app.user_id`, на них стоят политики RLS).
# Это каркас запроса, а не чтение данных. Перечислено полностью нарочно: список
# и есть определение того, что считается «ничего не спросил», и любая новая
# строка в нём — повод посмотреть, что за неё добавилось.
REQUEST_FRAME = (
    "BEGIN",
    "COMMIT",
    "ROLLBACK",
    "SAVEPOINT ",
    "RELEASE SAVEPOINT ",
    "ROLLBACK TO SAVEPOINT ",
    "set local role ",
    "select set_config('app.",
    "select current_setting('app.",
)


def test_the_guide_reads_no_data_at_all(stranger):
    """Гайд не читает ни одной таблицы: он про продукт, а не про данные партнёра.

    Требование «открыт до входа», записанное так, чтобы его нельзя было потерять
    правкой: первое же чтение таблицы здесь означает, что страница начала
    зависеть от того, что в базе лежит, — и однажды перестанет отвечать
    непредставившемуся или покажет ему чужое.

    Каркас запроса (транзакция и контекст доступа) не считается: он приходит на
    любую страницу продукта от middleware, а не от этой. Всё, что вне каркаса, —
    чтение, и его тут быть не должно.
    """
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as queries:
        assert stranger.get(GUIDE).status_code == 200

    reads = [
        q["sql"]
        for q in queries.captured_queries
        if not q["sql"].startswith(REQUEST_FRAME)
    ]
    assert not reads, reads


# --- найти с любого экрана ---------------------------------------------------


def test_the_way_to_the_guide_is_on_the_login_form(stranger):
    """Экран, с которого начинает новичок, обязан вести к объяснению."""
    assert f'href="{GUIDE}"' in body(stranger.get("/login/"))


def test_the_way_to_the_guide_is_on_a_working_screen(client):
    """И на рабочем экране тоже: шапка собирается по правам, ветки разные."""
    login_as(client, "director")
    assert f'href="{GUIDE}"' in body(client.get("/periods/"))


def test_on_the_guide_itself_it_is_not_a_link(stranger):
    """Ссылка на открытую страницу обманывает мышь и молчит диктору.

    Тот же инвариант, что у текущего раздела навигации (`appnav_item.html`), и
    проверяется он здесь по той же причине: ссылка на себя обещает переход,
    которого не будет.
    """
    html = body(stranger.get(GUIDE))
    assert f'href="{GUIDE}"' not in html, "гайд ссылается сам на себя"
    assert 'aria-current="page"' in html, "не сказано, что человек уже здесь"


# --- роли берутся из формы роли, а не переписаны --------------------------


def test_the_guide_lists_exactly_the_roles_of_the_product(stranger):
    """Ровно те роли, что лежат в `core.roles`, — не больше и не меньше.

    «Не меньше» ловит забытую роль, «не больше» — выдуманную. Оба случая
    одинаково плохи: гайд читают, чтобы понять, кем входить.
    """
    from core.roles import ROLE_ORDER
    from web.i18n import role_title

    html = body(stranger.get(GUIDE))
    for code in ROLE_ORDER:
        assert role_title(code) in html, f"роли {code} нет в гайде"


def test_the_guide_names_actions_the_way_the_product_names_them(stranger):
    """Названия действий — из `web.permissions`, а не своими словами.

    Иначе гайд объяснял бы «заморозку строки» одним словом, а отказ на экране —
    другим, и человек читал бы про два разных запрета.
    """
    from core.roles import ROLE_SHAPES
    from web import permissions

    html = body(stranger.get(GUIDE))
    for code in ROLE_SHAPES["director"].permissions:
        assert permissions.title(code) in html, f"действие {code} не названо"


def test_a_role_limited_to_one_unit_is_shown_as_limited(stranger):
    """Управляющий ограничен своей точкой — гайд обязан это сказать.

    Сам код точки при этом не показывается: код точки конкретного партнёра —
    его данные, а не устройство продукта, и на странице, открытой до входа, ему
    делать нечего.
    """
    from core.roles import ROLE_SHAPES
    from web.guide import role_cards

    cards = {card["code"]: card for card in role_cards()}
    assert cards["manager"]["one_unit"], "ограничение точкой потерялось"
    assert not cards["director"]["one_unit"], "директору приписана одна точка"

    unit = ROLE_SHAPES["manager"].unit
    assert unit, "у управляющего в форме роли нет точки — проверка потеряла смысл"
    assert unit not in body(stranger.get(GUIDE)), "код точки партнёра показан до входа"


# --- регистры: механизм объясняется, имена — нет -------------------------


def test_the_guide_does_not_name_the_ledgers_to_a_stranger(stranger):
    """D023: перечисление регистров и есть сообщение о том, что они есть.

    Тот же дефект уже был на форме входа (T096). Механизм гайд объясняет —
    слово «регистр» на странице есть и должно быть, — а вот названия
    непредставившемуся не показываются.
    """
    from web.format import LEDGER_TITLES

    html = body(stranger.get(GUIDE))
    assert "егистр" in html, "гайд вообще не объясняет регистры учёта"
    for code in LEDGER_TITLES:
        from web.format import ledger_title

        assert ledger_title(code) not in html, f"регистр «{code}» назван до входа"


def test_the_guide_names_the_ledgers_to_the_one_who_is_in(client):
    """Вошедшему — его собственные, те же, что стоят в шапке."""
    from web.format import ledger_title

    login_as(client, "director")
    html = body(client.get(GUIDE))
    assert ledger_title("official") in html


# --- сквозной месяц ---------------------------------------------------------


def test_the_month_is_walked_from_the_timesheet_to_the_closing(stranger):
    """Сквозной сценарий целиком: табель → расчёт → ведомость → закрытие.

    Порядок в нём обязательный, и гайд без одного из шагов оставляет читателя
    ровно там, где он и застревал: он знает про экраны, но не знает, что за чем.
    """
    html = body(stranger.get(GUIDE))
    for step in ("Табель", "Расчёт", "Ведомость", "Закрытие"):
        assert f"<h3>{step}</h3>" in html, f"шага «{step}» нет в сквозном месяце"


def test_the_guide_explains_the_second_half_of_the_primary_data(stranger):
    """Наличные, счета и инбокс — вторая половина P&L, и она тоже объяснена."""
    html = body(stranger.get(GUIDE))
    assert "нбокс" in html, "инбокс классификации не объяснён"
    assert "аличны" in html, "наличные расходы не объяснены"


# --- путь до первой ведомости -----------------------------------------------


def test_the_guide_walks_the_seven_steps_to_the_first_payslip(stranger):
    """Гайд ведёт по месяцу шагами, а не описывает экраны (T195, issue #187).

    Первая версия страницы рассказывала устройство продукта — что такое регистр,
    кто такие роли, из чего собирается P&L. Всё верно и всё бесполезно тому, кто
    сел и не знает, с чего начать. Эталон «Онбординг» требует пути: семь шагов до
    первой ведомости, и следующий невозможен без предыдущего.

    Проверяется не число «семь» в тексте, а что показаны ровно те шаги, что лежат
    в `web.guide.STEPS`: список в разметке был бы вторым, и разошёлся бы молча.
    """
    from web.guide import STEPS

    html = body(stranger.get(GUIDE))
    for step in STEPS:
        assert str(step.title) in html, f"шага «{step.title}» нет на странице"


def test_every_step_says_why_where_done_and_where_people_stumble(stranger):
    """У каждого шага четыре вещи, и без любой из них шаг бесполезен.

    «Зачем» — иначе шаг выполняют не понимая; «где это» — иначе его негде
    сделать; «готово, когда» — проверяемый признак вместо «настроено»; «где
    спотыкаются» — то, на чём спотыкаются все одинаково. Требование эталона, и
    оно легко теряется при следующей правке текста: абзац выкинули, страница
    осталась связной.
    """
    from django.utils.html import escape

    from web.guide import STEPS

    html = body(stranger.get(GUIDE))
    for step in STEPS:
        for field in (step.why, step.done, step.trap):
            # `escape`, а не сырая строка: «P&L» на странице живёт как «P&amp;L»,
            # и сравнение без экранирования краснело бы на верном тексте.
            assert escape(str(field)) in html, (
                f"шаг «{step.title}»: потеряно «{field}»"
            )


def test_every_step_points_at_a_real_screen(stranger):
    """«Где это» — настоящий адрес продукта, а не написанный руками путь.

    Выдуманный адрес в гайде хуже отсутствующего: он выглядит проверенным. Здесь
    он собирается из маршрута, поэтому переименование экрана его не ломает, — и
    проверка следит, что маршрут вообще существует и адрес попал на страницу.
    """
    from django.urls import reverse

    from web.guide import STEPS

    html = body(stranger.get(GUIDE))
    for step in STEPS:
        url = reverse(step.route)
        assert f'href="{url}"' in html, f"шаг «{step.title}» никуда не ведёт"


def test_the_guide_names_the_sections_the_way_the_header_does(stranger):
    """Раздел в гайде называется так же, как в шапке.

    Куплено разрывом: разделы переименовали по словарю эталона (issue #162, было
    «Периоды · Расходы · Счета», стало «Табель · Наличные расходы · Инбокс
    документов»), а гайд остался со старыми именами. Человек читал про «Счета»,
    искал их в шапке и не находил — и решал, что сломался он.

    Список берётся из `web.navigation`, то есть оттуда же, откуда собирается
    шапка: переименуют раздел — покраснеет здесь, а не у читателя.
    """
    from web.navigation import GROUPS

    html = body(stranger.get(GUIDE))
    missing = [
        str(item.title)
        for group in GROUPS
        for item in group.items
        if str(item.title) not in html
    ]
    assert not missing, f"гайд не называет разделы продукта: {missing}"
