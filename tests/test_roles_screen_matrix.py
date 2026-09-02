"""Матрица прав на экране: три состояния, а не две галочки (T203, issue #129).

Экран обязан показывать разницу между «право не выдано, но выдать можно» и
«этой роли не бывает никогда» — до нажатия, а не отказом после. Иначе стена
выглядит как обычная незажатая галочка, человек её жмёт, и продукт отвечает
ошибкой на действие, которое сам же и предложил.

Проверяется и обратная сторона: попытка выдать заваленное право не должна
проходить молча. Молчаливое отбрасывание здесь — худший исход из возможных:
администратор уверен, что право выдал, а его нет.
"""
from __future__ import annotations

import re

from conftest import body, login_as
from core.roles import ALL_PERMISSIONS
from test_roles_screen import granted_rights, role_id


def rights_block(html: str, code: str) -> str:
    block = re.search(
        r'<span class="note">' + code + r'</span>.*?</form>', html, flags=re.S,
    )
    assert block, f"на экране нет формы прав роли {code}"
    return block.group(0)


def test_the_screen_offers_every_permission_the_product_has(client, web_env):
    """Право, которого нет на экране, выдать нечем.

    Так `suppliers.classify` и жило: право есть, политика есть, а экран ролей о
    нём не знал — и разбор первички нельзя было ни выдать, ни снять.
    """
    login_as(client, "admin")
    html = body(client.get("/roles/"))
    offered = set(re.findall(r'name="right:([a-z.]+)"', html))
    assert offered == set(ALL_PERMISSIONS)


def test_a_walled_permission_is_shown_as_a_wall_and_not_as_a_checkbox(client, web_env):
    """У управляющего точки «Ведение ролей» — прочерк, а не пустая галочка."""
    login_as(client, "admin")
    html = body(client.get("/roles/"))
    block = rights_block(html, "manager")
    assert 'name="right:roles.manage"' not in block, "стена предлагается галочкой"
    assert "Ведение ролей" in block, "стена не названа — человек не поймёт, чего нет"


def test_the_wall_stands_only_where_the_matrix_puts_it(client, web_env):
    """У администратора стен нет: он может всё (D052)."""
    login_as(client, "admin")
    block = rights_block(body(client.get("/roles/")), "admin")
    assert 'name="right:roles.manage"' in block
    assert 'name="right:rules.manage"' in block


def test_granting_a_walled_permission_is_refused_in_words(client, web_env):
    """Не молча отброшено, а сказано: этой роли такого права не бывает."""
    login_as(client, "admin")
    html = body(client.get("/roles/"))
    manager = role_id(html, "manager")

    response = client.post(
        f"/roles/{manager}/rights/",
        {"right:timesheet.edit": "on", "right:roles.manage": "on"},
    )
    assert response.status_code == 409
    assert "не бывает" in body(response)

    login_as(client, "admin")
    assert "roles.manage" not in granted_rights(body(client.get("/roles/")), "manager")


def test_an_optional_permission_is_granted_as_usual(client, web_env):
    """Стена — не запрет на настройку: ○ выдаётся той же галочкой (D060)."""
    login_as(client, "admin")
    html = body(client.get("/roles/"))
    manager = role_id(html, "manager")

    client.post(
        f"/roles/{manager}/rights/",
        {"right:timesheet.edit": "on", "right:unit.close": "on",
         "right:payrun.calculate": "on"},
    )
    assert "payrun.calculate" in granted_rights(body(client.get("/roles/")), "manager")
