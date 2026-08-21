"""Подсказка поля доступна всем, а не только глазами (находка Н2 сверки 8).

Что было. В T148 подсказки полей спрятали до фокуса через `display: none` —
форма из шести полей перестала растягиваться на два экрана, и это было верно по
существу. Но `display: none` **выкидывает узел из дерева доступности целиком**:
для незрячего подсказки не существовало вовсе, а появившись по фокусу, она
вставала отдельным текстом рядом, а не описанием поля, — диктор её не произносил
никогда. Плюс появление двигало форму на 22 пикселя: проходя её табом, человек
видел, как кнопка сохранения прыгает на каждом поле.

Что проверяется здесь.

**Связь подсказки с полем.** `aria-describedby` у поля и `id` у подсказки — иначе
подсказка остаётся текстом «где-то рядом»: он есть, но к полю не относится.

**Подсказка не выключена из дерева.** Проверяется по разметке и стилям: прячет
её прозрачность, а не `display: none`. Тест смотрит на правило в листе стилей, а
не на слово «opacity» в шаблоне: лист один на весь продукт, и правило вернуть
обратно можно только в нём.

Лист с T177 лежит файлом статики, а не внутри страницы, поэтому правило читается
из файла. Одного файла мало: правило, живущее в листе, который страница не
просит, не действует, — поэтому рядом проверяется и то, что страница этот лист
подключает.

Почему тест такой. Он идёт от **готовой страницы**, а не от компонента:
подсказка собирается тремя разными шаблонами (поле, выбор, форма справочника), и
проверка одного из них осталась бы зелёной, когда разъедется другой.
"""
from __future__ import annotations

import pathlib
import re

from conftest import body, login_as

FORMS = [
    "/directory/tills/new/",
    "/directory/expense-items/new/",
    "/expenses/new/",
]


def fields_with_help(html: str) -> list[tuple[str, str]]:
    """Пары «поле — id его подсказки» со страницы, как их видит браузер."""
    return re.findall(r'<(?:input|select)[^>]*\bid="([^"]+)"[^>]*aria-describedby="([^"]+)"', html)


def test_every_hint_is_tied_to_its_field(client, web_env):
    """У каждой подсказки есть поле, которое на неё ссылается, и наоборот."""
    login_as(client, "admin")
    for url in FORMS:
        html = body(client.get(url))
        hints = set(re.findall(r'<span class="note" id="([^"]+)"', html))
        assert hints, f"{url}: на форме нет ни одной подсказки — проверка устарела"

        tied = fields_with_help(html)
        assert tied, f"{url}: ни одно поле не ссылается на подсказку (aria-describedby)"
        for field_id, help_id in tied:
            assert help_id in hints, (
                f"{url}: поле {field_id} ссылается на подсказку {help_id}, которой нет"
            )


def test_the_hint_is_hidden_from_eyes_but_not_from_the_tree(client, web_env):
    """Прячет прозрачность, а не `display: none`.

    Разница не косметическая: `display: none` убирает узел из дерева
    доступности, и подсказка перестаёт существовать для того, кто читает
    страницу диктором.
    """
    from django.contrib.staticfiles import finders

    login_as(client, "admin")
    html = body(client.get(FORMS[0]))
    assert "web/app.css" in html, (
        "страница не просит лист оформления — правило ниже на ней не действует"
    )

    stylesheet = finders.find("web/app.css")
    assert stylesheet, "листа оформления нет в статике вовсе"
    css = pathlib.Path(stylesheet).read_text(encoding="utf-8")

    rule = re.search(r"form\.card \.field > \.note \{([^}]*)\}", css)
    assert rule, "правило показа подсказки пропало из листа стилей"
    body_of_rule = rule.group(1)
    assert "display: none" not in body_of_rule, (
        "подсказку снова прячут display: none — для диктора она исчезнет"
    )
    assert "opacity: 0" in body_of_rule, "подсказка не спрятана вовсе — форма растянется"
    assert "min-height" in body_of_rule, (
        "под подсказку не зарезервировано место — форма будет дёргаться при табуляции"
    )
