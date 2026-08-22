"""Состояние расчёта показано плашкой своего цвета, а не строчкой текста (T168).

Раздел 08 эталона ставит состояние периода плашкой с точкой цвета рядом с
заголовком: это то, ради чего человек и открыл экран. У нас оно стояло обычным
текстом в ряду фактов — наравне с нормой часов и датой последнего расчёта, то
есть читалось последним.

Цвет приезжает из кода состояния, а не из его названия: название переводится
(«Посчитан» / «Calculated» / «Obračunato»), код — нет. Проверяется именно это,
иначе первая же страница на английском осталась бы серой.
"""
from __future__ import annotations

import re
from pathlib import Path

from django.template import Context, Template

from web.format import CodedTitle

CSS = Path(__file__).resolve().parent.parent / "src/web/static/web/app.css"

STATES = ("draft", "calculated", "approved", "reopened")


def render(title) -> str:
    return Template("{% load ui %}{% state title %}").render(Context({"title": title}))


def test_every_state_gets_its_own_class():
    for code in STATES:
        html = render(CodedTitle("Название", code))
        assert f"status--{code}" in html, f"{code} без своего класса: {html}"


def test_states_are_told_apart_by_class_not_by_words():
    # Сравниваются классы: названия и так разные, и проверка «html различается»
    # осталась бы зелёной при полностью серых плашках.
    classes = {
        re.search(r'class="([^"]+)"', render(CodedTitle("Одно и то же", code))).group(1)
        for code in STATES
    }
    assert len(classes) == len(STATES), f"состояния нарисованы одним классом: {classes}"


def test_the_badge_carries_a_dot():
    # Точка цвета — часть компонента: она читается быстрее текста и работает
    # там, где плашка попала в плотный ряд.
    assert "<i></i>" in render(CodedTitle("Посчитан", "calculated"))


def test_a_title_without_a_code_stays_neutral():
    html = render("Что-то своё")
    assert 'class="status"' in html
    assert "status--" not in html


def test_unknown_code_does_not_leak_into_markup():
    html = render(CodedTitle("Придуманное", 'x" onmouseover="alert(1)'))
    assert "onmouseover" not in html
    assert "status--" not in html


def test_each_state_colour_comes_from_its_token():
    css = CSS.read_text(encoding="utf-8")
    for code in STATES:
        rule = re.search(rf"\.status--{code}\s*\{{([^}}]*)\}}", css)
        assert rule, f"нет правила для состояния {code}"
        assert f"var(--st-{code})" in rule.group(1), f"{code} покрашен мимо токена"
