"""Инструкция к загрузке справочника называет колонки, которые продукт узнаёт.

T157, находка Н8 восьмой сверки. Текст над формой загрузки статей переведён на
английский и сербский, а названия колонок в переводе оставлены **русскими**:

> The product looks for columns by name — «Название», «Код», «Строка P&L» —
> column order and extra columns do not matter.

Это не непереведённая строка (знакомый класс, issue #96), а переведённая строка,
дающая **неверный совет**. Первый живой пользователь этой формы — бухгалтер
Сербии, ради файла которой заведена вся T147: она прочтёт сербскую инструкцию,
переименует свои `Naziv`/`Šifra` в кириллицу, которую у себя не наберёт, и
решит, что продукт требует русских заголовков. При том что разбор файла
(`COLUMNS` в `web/expense_items_upload.py`) принимает все три языка сразу — то
есть инструкция советует делать лишнюю работу, и делать её неправильно.

**Проверяется не «нет кириллицы», а «названо то, что продукт узнаёт».** Первое
зеленело бы и на инструкции, называющей колонки, которых разбор не знает;
второе — ровно то обещание, которое человек проверяет своим файлом. Поэтому все
названия в кавычках вынимаются из живой страницы и сверяются с `COLUMNS` тем же
`_norm`, каким сверяет их сам разбор.
"""
from __future__ import annotations

import html as html_escapes
import re

import pytest

from conftest import body, login_as
from web.expense_items_upload import COLUMNS, _norm

LIST_URL = "/directory/expense-items/"

LANGUAGES = ["ru", "en", "sr-latn"]

# Названия в кавычках любого из трёх языков: ёлочки, сербские лапки и обычные.
QUOTED = re.compile(r"[«„\"]([^«»„“\"]{2,40})[»“\"]")


def known_headers() -> set[str]:
    """Всё, что разбор файла узнаёт как заголовок, — нормализованно."""
    return {_norm(variant) for variants in COLUMNS.values() for variant in variants}


def upload_hint(client, language: str) -> str:
    """Текст над формой загрузки на выбранном языке — с живой страницы."""
    client.cookies["django_language"] = language
    try:
        html = body(client.get(LIST_URL))
    finally:
        client.cookies.pop("django_language", None)
    found = re.search(
        r'<form class="upload".*?<p class="sub">(.*?)</p>', html, re.S
    )
    assert found, f"на странице ({language}) не нашлось текста над формой загрузки"
    # Разметку возвращаем в текст: «Строка P&L» доезжает до страницы как
    # `P&amp;L`, и сверять с разбором надо то, что человек читает глазами.
    return html_escapes.unescape(found.group(1))


@pytest.mark.parametrize("language", LANGUAGES)
def test_every_column_named_in_the_hint_is_one_the_product_accepts(client, language):
    """ГЛАВНАЯ ПРОВЕРКА: продукт советует ровно те заголовки, которые узнаёт."""
    login_as(client, "admin")
    try:
        hint = upload_hint(client, language)
    finally:
        client.post("/logout/")

    named = QUOTED.findall(hint)
    assert named, f"({language}) инструкция не называет ни одной колонки:\n{hint}"

    accepted = known_headers()
    unknown = [name for name in named if _norm(name) not in accepted]
    assert not unknown, (
        f"({language}) инструкция советует заголовки, которых разбор не знает: "
        f"{unknown}\n{hint}"
    )


@pytest.mark.parametrize("language", ["en", "sr-latn"])
def test_the_translated_hint_names_a_header_in_its_own_language(client, language):
    """Сербу и англичанину названы колонки на их языке, а не только русские.

    Иначе совет формально верен (кириллицу разбор тоже примет), но человек
    переименовывает рабочие колонки в чужой алфавит без всякой нужды.
    """
    own = {"en": ("Name", "Code"), "sr-latn": ("Naziv", "Šifra")}[language]

    login_as(client, "admin")
    try:
        hint = upload_hint(client, language)
    finally:
        client.post("/logout/")

    assert any(word in hint for word in own), (
        f"({language}) в инструкции нет ни одного заголовка на языке страницы:\n{hint}"
    )
