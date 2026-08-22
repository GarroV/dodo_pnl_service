"""Липкая шапка таблицы держится не сама по себе, а связкой из трёх правил.

Что случилось. Правило `position: sticky` у шапки ведомости было написано и
выглядело работающим — а шапка уезжала вместе со страницей. Причина в том, что
контейнер таблицы объявлен `overflow-x: auto`, и по спецификации это делает его
контекстом прокрутки по ОБЕИМ осям: липкий элемент внутри привязывается к
контейнеру, а не к окну. Пока высота контейнера была по содержимому,
прокручивать внутри было нечего, и `sticky` не делал ничего.

Замер в браузере: при прокрутке страницы на 1200 пикселей заголовок колонок
оказывался на 580 пикселей выше окна. Правило стояло, поведения не было.

Поэтому проверяется именно связка: у контейнера ограничена высота И задана
прокрутка, а у шапки с итогом — липкость. Убери любое из трёх — и остальные
превращаются в украшение, которое ничего не держит.
"""
from __future__ import annotations

import re
from pathlib import Path

CSS = (Path(__file__).resolve().parent.parent / "src/web/static/web/app.css").read_text(
    encoding="utf-8"
)


def rule(selector: str) -> str:
    """Тело правила по точному селектору."""
    found = re.search(rf"(?m)^{re.escape(selector)}\s*\{{([^}}]*)\}}", CSS)
    assert found, f"нет правила {selector}"
    return found.group(1)


def test_the_container_can_actually_be_scrolled_inside():
    body = rule(".scroll")
    assert "max-height" in body, (
        "у контейнера нет предела высоты — прокручивать внутри нечего, "
        "и липкая шапка внутри него держаться не за что"
    )
    assert "overflow" in body, "контейнер не прокручивается"


def test_the_head_of_the_sheet_is_sticky():
    body = rule("table.sheet th")
    assert "position: sticky" in body, "шапка ведомости не липкая"
    assert "top: 0" in body


def test_the_totals_row_is_sticky_too():
    body = rule("table.sheet tfoot td, table.sheet tfoot th")
    assert "position: sticky" in body, "строка итогов не липкая"
    assert "bottom: 0" in body


def test_the_name_column_is_sticky():
    body = rule("table.sheet th:first-child, table.sheet td:first-child")
    assert "position: sticky" in body, "колонка с именем не липкая"
    assert "left: 0" in body


def test_printing_lets_the_whole_table_out():
    # На бумаге прокрутки нет: контейнер с пределом высоты унёс бы на печать
    # только видимую часть ведомости, а остальное пропало бы молча.
    printing = re.search(r"@media print \{(.*?)\n\}", CSS, re.S)
    assert printing, "нет блока печати"
    block = printing.group(1)
    assert "max-height: none" in block, (
        "предел высоты на печати не снят — на лист уйдёт видимая часть ведомости, "
        "а остальные строки пропадут молча"
    )
    assert "position: static" in block, (
        "липкость на печати не снята — шапка и итог напечатаются поверх строк"
    )
