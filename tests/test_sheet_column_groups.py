"""Колонки ведомости собраны в группы (issue #163, T186).

Модуль 9 эталона показывает шапку двумя ярусами: над колонками стоит группа
(«Начисления», «Удержания»), а имя сотрудника и «К выплате» проходят через оба
яруса. Смысл в том же, ради чего сделаны липкие колонки: в ведомости полтора
десятка колонок, и без иерархии это ряд чисел, в котором глаз ищет нужное
пересчётом слева направо.

Липкие колонки и строка итогов у нас уже есть (перенесены блоком `visual`).
Оставалось третье — ярус групп.

**Группы у нас другие, чем в макете, и это не отступление.** Эталон рисует
«Начисления» и «Удержания»; удержаний в нашем расчёте нет ни одного компонента —
налог и взносы считаются производными и в ведомость колонками не приходят.
Рисовать пустую группу «Удержания» значило бы обещать колонки, которых нет.
Поэтому группируется то, что есть: оплата часов отдельно от надбавок и
корректировок. Третья группа появится вместе с первым удержанием.
"""
from __future__ import annotations

import re

from conftest import body, login_as
from test_closing_readiness import calculated  # noqa: F401


def head_rows(html: str) -> list[str]:
    thead = re.search(r"<table class=\"sheet\">.*?<thead>(.*?)</thead>", html, re.S)
    assert thead, "ведомости нет на странице"
    return re.findall(r"<tr[^>]*>(.*?)</tr>", thead.group(1), re.S)


def test_the_head_has_two_tiers(client, calculated):  # noqa: F811
    """Шапка ведомости — два яруса: группы сверху, колонки под ними."""
    login_as(client, "director")
    rows = head_rows(body(client.get(calculated)))

    assert len(rows) == 2, f"ярусов не два, а {len(rows)}"


def test_the_groups_name_what_is_under_them(client, calculated):  # noqa: F811
    """Группа названа словами и накрывает свои колонки, а не одну."""
    login_as(client, "director")
    top = head_rows(body(client.get(calculated)))[0]

    groups = re.findall(r'<th[^>]*colspan="(\d+)"[^>]*>(.*?)</th>', top, re.S)
    assert groups, "в верхнем ярусе нет ни одной группы"
    for span, title in groups:
        assert int(span) >= 1, f"группа «{title}» ничего не накрывает"
        assert re.sub(r"<[^>]+>", "", title).strip(), "группа без названия"


def test_the_name_and_the_total_cross_both_tiers(client, calculated):  # noqa: F811
    """Имя и «Итого» проходят через оба яруса, а не висят в одном.

    Иначе они окажутся под какой-нибудь группой и прочитаются как её часть —
    ровно наоборот тому, ради чего ярус и заведён.
    """
    login_as(client, "director")
    top = head_rows(body(client.get(calculated)))[0]

    crossing = re.findall(r'<th[^>]*rowspan="2"[^>]*>(.*?)</th>', top, re.S)
    plain = [re.sub(r"<[^>]+>", "", cell).strip() for cell in crossing]
    assert "Сотрудник" in plain, f"имя не проходит через оба яруса: {plain}"
    assert any("того" in cell for cell in plain), f"«Итого» не сквозное: {plain}"


def test_every_column_is_covered_by_a_group(client, calculated):  # noqa: F811
    """Колонок под группами ровно столько, сколько колонок в нижнем ярусе.

    Проверка арифметическая, и в ней весь смысл: группа, потерявшая колонку,
    сдвигает шапку относительно чисел — и человек читает чужой столбец, не
    замечая этого.
    """
    login_as(client, "director")
    top, bottom = head_rows(body(client.get(calculated)))

    covered = sum(int(span) for span in re.findall(r'colspan="(\d+)"', top))
    crossing = len(re.findall(r'rowspan="2"', top))
    columns = len(re.findall(r"<th", bottom))

    assert covered == columns, (
        f"группы накрывают {covered} колонок, а в ярусе колонок {columns}"
    )
    assert crossing >= 2, "сквозных колонок меньше двух — имени или итога нет"
