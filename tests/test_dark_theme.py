"""Тёмная тема включается человеком и переживает перезагрузку (issue #164, T193).

Страница эталона «Тёмная тема» формулирует своё устройство прямо: «ни один
компонент не переписан; в `tokens.css` добавлен блок `[data-theme="dark"]`,
который переопределяет значения — разметка про тему не знает». Токены у нас уже
перенесены. Не хватало ручки: тему нельзя было включить.

**Тема хранится в cookie и ставится сервером**, а не выбирается скриптом на
странице. Причина видна глазами: выбранная скриптом тема применяется после
загрузки, и человек каждый раз видит вспышку светлого фона перед тёмным. Кроме
того, страница обязана открываться правильной и без скриптов вовсе.

**Три состояния, а не два**: светлая, тёмная и «как в системе». Последнее —
умолчание: продукт не должен спорить с настройкой машины, пока его об этом не
попросили.
"""
from __future__ import annotations

from conftest import body, login_as

THEMES = "/theme/"


def html_tag(page: str) -> str:
    """Открывающий тег `<html …>` — там и живёт атрибут темы."""
    start = page.index("<html")
    return page[start:page.index(">", start) + 1]


def test_by_default_the_product_does_not_argue_with_the_system(client, web_env):
    """Ничего не выбрано — атрибута нет: тему решает система, а не мы."""
    login_as(client, "director")
    tag = html_tag(body(client.get("/periods/")))
    assert "data-theme" not in tag, f"тема навязана без выбора человека: {tag}"


def test_the_dark_theme_is_switched_on_and_stays(client, web_env):
    """Выбрал тёмную — она стоит и после перезагрузки."""
    login_as(client, "director")
    answer = client.post(THEMES, {"theme": "dark", "next": "/periods/"})
    assert answer.status_code in (302, 303), body(answer)[:200]

    tag = html_tag(body(client.get("/periods/")))
    assert 'data-theme="dark"' in tag, f"тёмная тема не включилась: {tag}"


def test_the_choice_can_be_given_back_to_the_system(client, web_env):
    """«Как в системе» — не третья тема, а отказ от выбора."""
    login_as(client, "director")
    client.post(THEMES, {"theme": "dark", "next": "/periods/"})
    client.post(THEMES, {"theme": "system", "next": "/periods/"})

    tag = html_tag(body(client.get("/periods/")))
    assert "data-theme" not in tag, f"выбор не вернулся системе: {tag}"


def test_an_unknown_theme_is_refused(client, web_env):
    """Выдуманное значение не ставится: атрибут уезжает прямо в разметку."""
    login_as(client, "director")
    client.post(THEMES, {"theme": "<script>", "next": "/periods/"})

    tag = html_tag(body(client.get("/periods/")))
    assert "script" not in tag.lower(), f"в тег страницы попало чужое: {tag}"


def test_the_switch_is_in_the_header(client, web_env):
    """Ручка стоит в шапке рядом с языком: обе — про то, как читать продукт."""
    login_as(client, "director")
    page = body(client.get("/periods/"))
    assert 'action="/theme/"' in page, "переключателя темы нет в шапке"


def test_the_theme_survives_a_visitor_without_login(client, web_env):
    """Тема — свойство читателя, а не роли: работает и до входа."""
    client.post(THEMES, {"theme": "dark", "next": "/guide/"})
    tag = html_tag(body(client.get("/guide/")))
    assert 'data-theme="dark"' in tag, f"до входа тема не применяется: {tag}"
