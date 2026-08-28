"""Скачивание не выглядит как переключение вида (замечание владельца).

Что было. Под ведомостью стояли два ряда одинаковых кнопок. Верхний менял то,
что на экране (разрез по регистрам), нижний — молча скачивал xlsx, а одна
кнопка в нём вообще открывала другой экран. Три разных поведения под одним
видом: человек нажимает одинаковое и получает разное, причём о скачивании
узнаёт по файлу, появившемуся в загрузках.

Разметку развели раньше (`nav.cuts` против `nav.exports`) — но это спасало
только проверки, которые отбирали кнопки по классу; глазами ряды оставались
близнецами.

Почему проверка именно такая. Она спрашивает не «есть ли класс», а **разное ли
у разных вещей**: ссылка на файл и ссылка на экран обязаны отличаться друг от
друга и обе — от кнопки переключателя. Проверка «класс `file` присутствует»
зеленела бы и в тот день, когда его повесят заодно на переключатель.
"""
from __future__ import annotations

import re

import pytest

from conftest import body, login_as, period_url, wipe_payruns


@pytest.fixture
def calculated(client, web_env):
    """Ряд выгрузок появляется только у посчитанного периода — считаем его.

    Своя копия, а не импорт из соседнего файла: связывать модули ради трёх
    строк дороже, чем повторить их (тот же довод записан в `clean_closures`).
    """
    wipe_payruns(web_env)
    login_as(client, "director")
    assert client.post(period_url(client) + "calculate/", follow=True).status_code == 200
    client.post("/logout/")


def rows(client) -> tuple[str, str]:
    """Разметка двух рядов под ведомостью: переключатель и выгрузки."""
    html = body(client.get(period_url(client)))
    cuts = re.search(r'<nav class="cuts".*?</nav>', html, re.S)
    exports = re.search(r'<nav class="exports".*?</nav>', html, re.S)
    assert cuts and exports, "под ведомостью больше нет двух рядов — проверка устарела"
    return cuts.group(0), exports.group(0)


def test_a_download_is_marked_as_a_download(client, web_env, calculated):
    """Каждая выгрузка помечена и как файл, и атрибутом браузера."""
    login_as(client, "director")
    _cuts, exports = rows(client)

    files = re.findall(r"<a[^>]*>", exports)
    downloads = [a for a in files if "file" in a]
    assert len(downloads) >= 3, f"выгрузки не помечены как файлы: {files}"
    for link in downloads:
        assert "download" in link, f"ссылка на файл без атрибута download: {link}"


def test_the_screen_link_is_not_dressed_as_a_file(client, web_env, calculated):
    """Сверка — экран, а не файл, и это видно до нажатия."""
    login_as(client, "director")
    _cuts, exports = rows(client)

    page_links = re.findall(r'<a[^>]*class="[^"]*\bpage\b[^"]*"[^>]*>', exports)
    # Экранов в этом ряду теперь два — сверка и отчёт P&L (issue #183). Число
    # не приколочено к единице: проверяется свойство («экран помечен как
    # экран»), а не сегодняшний состав ряда.
    assert page_links, f"ссылка на экран не помечена: {exports}"
    for link in page_links:
        assert "download" not in link, (
            f"ссылка на экран притворяется файлом: {link}"
        )
    assert "download" not in page_links[0], "экран сверки притворяется файлом"


def test_the_switcher_stays_a_switcher(client, web_env, calculated):
    """Переключатель разреза не притворяется ни файлом, ни ссылкой на экран.

    Это половина смысла: пометить выгрузки мало, если тем же классом однажды
    пометят и кнопки разреза — вид снова сравняется.
    """
    login_as(client, "director")
    cuts, _exports = rows(client)

    assert "download" not in cuts, "кнопка разреза притворяется файлом"
    assert not re.search(r'class="[^"]*\bfile\b', cuts), "кнопка разреза помечена как файл"
