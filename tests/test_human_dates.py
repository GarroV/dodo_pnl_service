"""Даты показываются человеку как 01.06.2026, а не 2026-06-01 (решение владельца).

Владелец 27.08.2026 дословно: «сделай нормальный, адекватный человеческий формат
дат на весь проект. День, месяц, год. Не надо вот эту хуйню исходно впереди. Это
не для людей, это не нормально».

Машинный формат остаётся ровно там, где он не для чтения: значение поля
`<input type="date">` — это стандарт HTML, браузер другого не примет, — и обмен
с внешними системами. Всё остальное идёт через `web.format.day`.

Сторож ниже держит правило на живых страницах: он ходит по продукту и ищет в
разметке даты вида `2026-06-01` вне полей ввода. Без него формат вернулся бы
первым же новым экраном — не по злому умыслу, а потому что `isoformat()` короче.
"""
from __future__ import annotations

import re
from datetime import date

from conftest import body, login_as, period_url

# Дата в машинном виде: 2026-06-01. Ищем её в тексте страницы, а не в значениях
# полей — там она обязана быть именно такой.
MACHINE_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")


def visible_text(html: str) -> str:
    """Что человек читает: без полей ввода, скриптов и служебных атрибутов."""
    html = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S)
    html = re.sub(r"<input[^>]*>", " ", html)
    html = re.sub(r"<option[^>]*>", " ", html)
    html = re.sub(r"<[^>]+>", " ", html)
    return " ".join(html.split())


def test_the_formatter_writes_the_day_first():
    from web.format import day, month

    assert day(date(2026, 6, 1)) == "01.06.2026"
    assert day(date(2026, 12, 31)) == "31.12.2026"
    assert month(date(2026, 6, 1)) == "06.2026"


def test_an_empty_date_is_a_dash_not_a_blank():
    """Пусто — это ответ «даты нет», и он должен читаться как ответ."""
    from web.format import EMPTY, day

    assert day(None) == EMPTY


def test_no_machine_dates_on_the_screens(client, web_env):
    """Ни на одной странице продукта человек не видит 2026-06-01."""
    login_as(client, "director")
    period = period_url(client)
    pages = [
        "/periods/", period, "/directory/", "/directory/employees/",
        "/directory/groups/", "/directory/units/", "/directory/calendar/",
        "/directory/counterparties/", "/expenses/", "/invoices/", "/rules/",
    ]
    machine = {}
    for url in pages:
        found = MACHINE_DATE.findall(visible_text(body(client.get(url))))
        if found:
            machine[url] = sorted(set(found))[:3]
    assert not machine, f"машинные даты на экранах: {machine}"
