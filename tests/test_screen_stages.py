"""Продукт показывает ВСЕ экраны и честно называет состояние каждого (D064).

Владелец 31.08.2026, открыв демо: «в демо нужны вообще все экраны , те что
сейчас в дработке - так и помечаем что В ДОРАБОТКЕ». Причина — не бедность
продукта, а то, что по нему нельзя было пройти: P&L, сверка и выгрузки
отвечали `200`, но в шапку не попали, и владелец решил, что их не построили
(#218). Тот же вывод он сделал пятью днями раньше (`docs/design-gap.md`,
26.08) — то есть один раз это случайность, два раза уже свойство.

Что здесь охраняется и почему именно это.

**Опечатка в имени маршрута молчит.** Пометка ищется по имени маршрута
(`stages.REFINED`); ошибись в нём — экран просто останется без пометки, ничего
не упадёт, и узнает об этом снова тот, кому показывают продукт. Поэтому имена
сверяются с резолвером Django.

**Пункт, ведущий в никуда, — та самая беда, которой боялись до D064.** Решение
её не отменяет: оно требует, чтобы у ненаписанного экрана была страница со
словами, а не пустота. Значит каждый пункт шапки обязан отвечать.
"""
from __future__ import annotations

import pytest


def test_marked_routes_exist():
    """Каждое помеченное имя маршрута — настоящее.

    Без этой сверки список пометок тихо расходится с продуктом: переименовали
    маршрут — пометка осталась висеть на несуществующем, а живой экран её
    потерял. Оба конца ошибки невидимы глазами.
    """
    from django.urls import NoReverseMatch, reverse

    from web import stages

    broken = []
    for name in stages.REFINED:
        try:
            reverse(name)
        except NoReverseMatch:
            # Маршруты с параметрами (экран периода, P&L) резолвятся только с
            # аргументом — для них достаточно, что имя вообще известно.
            try:
                reverse(name, args=["00000000-0000-0000-0000-000000000000"])
            except NoReverseMatch:
                broken.append(name)
    assert not broken, f"помечены несуществующие маршруты: {broken}"


def test_every_navigation_item_resolves():
    """Ни один пункт шапки не ведёт в никуда — включая ненаписанные экраны."""
    from django.urls import NoReverseMatch, reverse

    from web import navigation

    broken = []
    for group in navigation.GROUPS:
        for item in group.items:
            try:
                reverse(item.route)
            except NoReverseMatch:
                broken.append(f"{item.title} → {item.route}")
    assert not broken, f"пункты шапки без адреса: {broken}"


def test_the_reference_menu_is_covered():
    """Все пункты словаря эталона стоят в шапке — в этом и было решение D064.

    Читается из самого эталона, а не из списка, переписанного сюда: копия
    разошлась бы с источником молча, и тест начал бы охранять её вместо него.
    """
    import re
    from pathlib import Path

    from web import navigation

    root = Path(__file__).resolve().parent.parent
    text = (root / "Дизайн-система Dodo P&L" / "Модуль 10 - Вход и каркас.dc.html").read_text(
        encoding="utf-8",
    )
    menu = re.search(r"menu:\s*\{(.*?)\n\s*\}", text, re.S).group(1)
    wanted = set(re.findall(r'\[\s*"([^"]+)"', menu))

    ours = {str(item.title) for group in navigation.GROUPS for item in group.items}
    # Справочники эталона (Контрагенты, Точки, Юрлица) у нас лежат под одним
    # пунктом «Справочники» — это T173, отдельное решение: два входа в один
    # экран из одной шапки читаются как два разных места. «Доступы» названы
    # «Роли и права» по `navigation.OUR_OWN`.
    under_directories = {"Контрагенты", "Точки", "Юрлица", "Доступы"}
    missing = sorted(wanted - ours - under_directories)
    assert not missing, f"пункты эталона, которых нет в шапке: {missing}"


@pytest.mark.parametrize("code", ["statement", "payouts", "people-analytics", "dodo-is"])
def test_a_planned_screen_says_what_it_will_be(client, web_env, code):
    """Ненаписанный экран отвечает и объясняет себя, а не молчит пустотой."""
    from conftest import body, login_as

    from web import stages

    login_as(client, "director")
    screen = next(item for item in stages.planned_screens() if item.code == code)
    html = body(client.get(f"/{screen.url}"))

    assert str(screen.title) in html, "заглушка не называет сам экран"
    assert str(stages.LABELS[stages.PLANNED]) in html, (
        "на ненаписанном экране нет пометки «Разработка в процессе»"
    )
    assert str(screen.what)[:40] in html, "заглушка не говорит, что здесь будет"


def test_a_refined_screen_carries_its_mark(client, web_env):
    """Дорабатываемый экран помечен плашкой, готовый — нет.

    Ищется КЛАСС плашки, а не слово «Доработка»: слово законно стоит и в шапке —
    подписью к точке у пункта меню, — и проверка по тексту прошла бы на любой
    странице продукта. Поймано первым же прогоном этого теста.
    """
    from conftest import body, login_as

    login_as(client, "director")

    refined = body(client.get("/inbox/"))
    assert 'class="stage stage--refining"' in refined, (
        "инбокс дорабатывается (#200, #143), а плашки на нём нет"
    )

    ready = body(client.get("/periods/"))
    assert 'class="stage stage--' not in ready, (
        "табель готов — плашки состояния на нём быть не должно"
    )


def test_entry_points_lead_into_the_latest_month(client, web_env):
    """Пункты «Ведомость», «P&L» и сверка ведут в месяц, а не в тупик."""
    from conftest import login_as

    login_as(client, "director")
    for url in ("/payroll/sheet/", "/payroll/closing/", "/reports/pnl/",
                "/reports/reconcile/"):
        response = client.get(url)
        assert response.status_code == 302, f"{url} не переадресует"
        assert response.headers["Location"].startswith("/periods/"), (
            f"{url} ведёт мимо периодов: {response.headers['Location']}"
        )
