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

import pytest
from django.test.utils import override_settings

# Файлы экрана табеля. Список нарочно записан здесь руками, а не собран из
# каталога: собранный из того же места, откуда берёт продукт, он подтвердил бы
# сам себя и остался бы зелёным, даже если файл из шаблона исчез.
FILES = [
    ("timesheets/htmx-2.0.10.min.js", b"htmx", "web/components/htmx.html"),
    ("timesheets/grid.js", None, "timesheets/grid.html"),
    ("timesheets/grid.css", None, "timesheets/grid.html"),
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
    """
    from django.template.loader import get_template

    for path, _, template in FILES:
        source = get_template(template).template.source
        assert path in source, f"{template} больше не просит {path} — список устарел"
