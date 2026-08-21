"""Статика отдаётся и при выключенной отладке (issue #68).

Что было. Статику отдавал только штатный `runserver` под `DEBUG=True`. На
площадке стоит `DJANGO_DEBUG=0` — и экран табеля оставался там без `htmx`,
`grid.js` и `grid.css`. Ломалось это молча и в самом неудобном месте: страница
открывалась, сетка рисовалась, а ячейка на сервер не уходила. Человек вводил
часы, видел числа на экране и уходил, ничего не сохранив. Проверка здоровья
контейнера этого не видела — она ходит на `/`.

Почему тест именно такой.

**Проверяются те самые три файла, а не «какой-нибудь».** Отдающий вообще всё,
но не отдающий `grid.js`, — это ровно та поломка, которая уже была.

**Проверяется при `DEBUG=False`.** При `True` файл отдаст `runserver`, и тест
зеленел бы, ничего не проверяя: вся суть дефекта в другом значении.

**Проверяется содержимое, а не только код ответа.** Пустой файл с кодом 200
сломает страницу так же надёжно, как 404, и по коду ответа этого не видно.
"""
from __future__ import annotations

import pathlib

import pytest
from django.test.utils import override_settings

# Файлы, без которых экран ломается. Список нарочно записан здесь руками, а не
# собран из каталога: собранный из того же места, откуда берёт продукт, он
# подтвердил бы сам себя и остался бы зелёным, даже если файл из шаблона исчез.
#
# Оформление здесь наравне со скриптами табеля, и это не вкусовщина: до T177
# лист стилей лежал инлайном в `base.html` именно из страха перед этим 404, и
# страх был обоснован (issue #68). Как только оформление уехало в статику,
# неотданный `app.css` означает нечитаемый продукт на **всех** страницах —
# ровно то состояние, из-за которого владелец не мог его протестировать.
FILES = [
    ("timesheets/htmx-2.0.10.min.js", b"htmx", "web/components/htmx.html"),
    ("timesheets/grid.js", None, "timesheets/grid.html"),
    ("timesheets/grid.css", None, "timesheets/grid.html"),
    # Значения дизайн-системы (T176) и правила на них (T177).
    ("web/tokens.css", b"--canvas", "web/base.html"),
    ("web/app.css", b"var(--ink)", "web/base.html"),
    # Шрифты локально: внешние загрузки в проде запрещены, а без файла продукт
    # молча уезжает на системный шрифт — метрики другие, вёрстка «почти та же».
    ("web/fonts/golos-text-cyrillic.woff2", b"wOF2", "web/tokens.css"),
    ("web/fonts/ibm-plex-mono-400-latin.woff2", b"wOF2", "web/tokens.css"),
]


@pytest.mark.parametrize("path,needle,template", FILES)
def test_static_file_is_served_with_debug_off(client, web_env, path, needle, template):
    """Ровно тот случай площадки: отладка выключена, файл обязан приехать."""
    with override_settings(DEBUG=False):
        response = client.get(f"/static/{path}")

    assert response.status_code == 200, (
        f"{path}: {response.status_code} при DEBUG=False — на площадке это и было 404"
    )
    body = b"".join(response.streaming_content) if response.streaming else response.content
    assert body.strip(), f"{path}: ответ 200, но файл пустой — страница сломается так же"
    if needle:
        assert needle in body, f"{path}: отдалось что-то не то"


def test_the_pages_ask_for_those_very_files():
    """Связка списка выше с продуктом: страницы просят именно эти файлы.

    Без этой проверки список зажил бы своей жизнью — тест остался бы зелёным на
    файлах, которых страница уже не просит, и молчал бы о новом, который она
    просит и который не отдаётся. Подключение htmx живёт отдельным включаемым
    куском (D017: версия и путь названы в продукте один раз), поэтому у каждого
    файла в списке записано, какой шаблон его просит.

    Просящий не всегда шаблон. Шрифт просит не страница, а `@font-face` внутри
    `tokens.css`: путь там относительный, `{% static %}` внутри CSS не работает,
    и опечатка в имени файла не видна нигде — страница откроется системным
    шрифтом, «почти той же» вёрсткой. Поэтому CSS проверяется как файл статики,
    а не через загрузчик шаблонов, который его не найдёт вовсе.
    """
    from django.contrib.staticfiles import finders
    from django.template.loader import get_template

    for path, _, asker in FILES:
        if asker.endswith(".css"):
            found = finders.find(asker)
            assert found, f"не нашёлся сам {asker} — а он просит {path}"
            source = pathlib.Path(found).read_text(encoding="utf-8")
            # В CSS путь относительный: от /static/web/ до /static/web/fonts/.
            expected = path.removeprefix(asker.rsplit("/", 1)[0] + "/")
        else:
            source = get_template(asker).template.source
            expected = path
        assert expected in source, f"{asker} больше не просит {path} — список устарел"
