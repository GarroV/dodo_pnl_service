"""Метка регистра учёта различает регистры цветом (T168, раздел «Семантика регистров»).

Зачем эта проверка существует. До переноса все три регистра рисовались одной
серой меткой: разрез переключаешь, в строке что-то написано — а глаз разницы не
видит. Владелец 2026-08-22, посмотрев демо: «визуал старый, всратый». Цвет метки
— первое, что в ведомости сообщает, о каком контуре учёта строка, и его
отсутствие делало таблицу одинаково-серой независимо от смысла.

Держать это тестом нужно потому, что цвет приезжает НЕ из шаблона: название
регистра несёт свой код с собой (`LedgerTitle`), и любая правка, где название
превратится в обычную строку, молча вернёт серые метки — страница при этом
останется исправной, и ни один существующий тест не покраснеет.

Требование D023 не нарушено: код относится к строке, уже отобранной политиками
базы для этой роли, и нового знания о существующих регистрах не даёт — что
отдельно проверяется ниже.
"""
from __future__ import annotations

import re
from pathlib import Path

from django.template import Context, Template

from web.format import LedgerTitle, ledger_title

CSS = Path(__file__).resolve().parent.parent / "src/web/static/web/app.css"


def render(title) -> str:
    return Template("{% load ui %}{% ledger title %}").render(Context({"title": title}))


def test_name_of_ledger_carries_its_code():
    title = ledger_title("official")
    assert title == "Официальный"          # ведёт себя как обычная строка
    assert isinstance(title, str)
    assert title.code == "official"


def test_name_stays_usable_where_plain_text_is_expected():
    # Название уезжает в join и в ячейки xlsx — там подкласс не должен мешать.
    names = ", ".join(ledger_title(code) for code in ("official", "supplementary"))
    assert names == "Официальный, Дополнительный"
    assert f"{ledger_title('internal')}" == "Внутренний"


def test_mark_gets_the_colour_class_of_its_ledger():
    for code, css_class in (
        ("official", "reg--official"),
        ("supplementary", "reg--supplementary"),
        ("internal", "reg--internal"),
    ):
        html = render(ledger_title(code))
        assert css_class in html, f"метка {code} без своего класса: {html}"


def test_three_ledgers_are_told_apart_by_class_not_by_words():
    # Сравниваются КЛАССЫ, а не разметка целиком: названия у регистров и так
    # разные, поэтому проверка «html различается» оставалась бы зелёной даже
    # при полностью серых метках. Поймано порчей при написании теста.
    classes = {
        re.search(r'class="([^"]+)"', render(ledger_title(c))).group(1)
        for c in ("official", "supplementary", "internal")
    }
    assert len(classes) == 3, f"разные регистры нарисованы одним классом: {classes}"


def test_mark_without_a_code_stays_neutral():
    # Название из чужого источника (обычная строка) не должно ломать метку —
    # она просто остаётся нейтральной, как была.
    html = render("Что-то своё")
    assert 'class="ledger"' in html
    assert "reg--" not in html


def test_unknown_code_does_not_leak_into_markup():
    html = render(LedgerTitle("Придуманный", 'x" onmouseover="alert(1)'))
    assert "onmouseover" not in html
    assert "reg--" not in html


def test_each_ledger_colour_is_defined_in_the_stylesheet():
    css = CSS.read_text(encoding="utf-8")
    for css_class, token in (
        ("reg--official", "--reg-official"),
        ("reg--supplementary", "--reg-supp"),
        ("reg--internal", "--reg-internal"),
    ):
        rule = re.search(rf"\.ledger\.{re.escape(css_class)}\s*\{{([^}}]*)\}}", css)
        assert rule, f"нет правила для {css_class}"
        assert f"var({token})" in rule.group(1), f"{css_class} покрашен мимо токена"
