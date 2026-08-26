"""Учётка осмотра демо: `admin` / `admin` со всеми правами сразу.

Зачем она есть (запрос владельца 2026-08-26). Кнопки на титульной показывают
роли по одной — это и есть демонстрация разграничения. Но тому, кто смотрит
продукт целиком (сам владелец, коллега, заказчик за плечом), переключаться
между четырьмя ролями ради одного обхода незачем: ему нужен один вход, из
которого видно всё.

Прав у неё максимум, и получены они **продуктовым** способом — двумя ролями на
одном человеке (D047, T170: права складываются по всем членствам). Не выдуманная
пятая роль: демо обязано показывать то, что партнёр получит у себя, и роль
«может всё», которой в продукте нет, была бы враньём в самом центре демо.

Зачем это тестом. Учётка заводится сидом, а демо сбрасывается по расписанию —
заведённая руками на площадке, она исчезала бы каждую ночь. И второе: логин
`admin` раньше принадлежал учётке роли «администратор сети». Тест держит обе
стороны — что осмотр видит всё, а кнопка роли по-прежнему входит именно ролью,
а не осмотром.

Проверки идут подпроцессом: демо-база создаётся сидом целиком, как на стенде.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from conftest import MANAGE_PY, temp_database

SCRIPT = """
import json
from django.contrib.auth import authenticate
from core import models
from core.roles import ALL_LEDGERS
from demo.seed import ADMIN_LOGIN, demo_admin_password, demo_password, role_login
from web.permissions import TITLES

out = {}
out["login"] = ADMIN_LOGIN
out["password"] = demo_admin_password()
entered = authenticate(username=ADMIN_LOGIN, password=demo_admin_password())
out["password_works"] = entered is not None
out["wrong_password_refused"] = authenticate(username=ADMIN_LOGIN, password="nope") is None

# Права и регистры складываются по ВСЕМ членствам — так же, как их складывает
# сама база (`app_has_permission`, `app_visible_ledgers`). Читаем тем же
# способом, каким читает продукт (web.principal), а не по одному членству.
who = models.User.objects.get(username=ADMIN_LOGIN)
mine = list(models.Membership.objects.select_related("role").filter(user_id=who.pk))
out["permissions"] = sorted({code for m in mine for code in (m.role.permissions or [])})
out["ledgers"] = sorted({code for m in mine for code in (m.role.visible_ledgers or [])})
out["every_unit"] = any(not m.unit_ids for m in mine)
out["all_permissions"] = sorted(TITLES)
out["all_ledgers"] = sorted(ALL_LEDGERS)

# Учётка роли «администратор сети» — отдельный человек с отдельным логином:
# кнопка на титульной обязана показывать роль, а не осмотр.
netadmin = models.User.objects.get(username=role_login("admin"))
out["role_login"] = role_login("admin")
out["role_login_differs"] = role_login("admin") != ADMIN_LOGIN
as_role = authenticate(username=role_login("admin"), password=demo_password())
out["role_password_works"] = as_role is not None
role_side = list(models.Membership.objects.select_related("role").filter(user_id=netadmin.pk))
out["role_permissions"] = sorted({c for m in role_side for c in (m.role.permissions or [])})
print(json.dumps(out))
"""


@pytest.fixture(scope="module")
def demo_db():
    with temp_database("admin") as dsn:
        env = {
            **os.environ, "DATABASE_URL": dsn, "DEMO_DATABASE_URL": dsn,
            "SECRET_KEY": "test-only-not-a-secret",
            "DJANGO_SETTINGS_MODULE": "config.settings",
        }
        subprocess.run([sys.executable, str(MANAGE_PY), "seed_demo"],
                       env=env, capture_output=True, text=True, check=True)
        yield dsn


def probe(dsn: str) -> dict:
    result = subprocess.run(
        [sys.executable, str(MANAGE_PY), "shell", "-c", SCRIPT],
        env={
            **os.environ, "DATABASE_URL": dsn, "DEMO_DATABASE_URL": dsn,
            "SECRET_KEY": "test-only-not-a-secret",
            "DJANGO_SETTINGS_MODULE": "config.settings",
            "DEMO_MODE": "1",
        },
        capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


def test_admin_admin_gets_in(demo_db):
    seen = probe(demo_db)
    assert seen["login"] == "admin"
    assert seen["password"] == "admin", "пароль осмотра сменился — владелец диктует его по памяти"
    assert seen["password_works"], "учётка осмотра не пускает"


def test_a_wrong_password_is_still_refused(demo_db):
    assert probe(demo_db)["wrong_password_refused"]


def test_the_admin_sees_everything(demo_db):
    # Смысл учётки — один вход, из которого виден весь продукт. Список прав
    # сверяется с полным списком продукта, а не с записанным здесь вторым
    # перечнем: добавится право — тест обязан покраснеть, а не промолчать.
    seen = probe(demo_db)
    assert seen["permissions"] == seen["all_permissions"], (
        "у осмотра не все права продукта: "
        f"нет {sorted(set(seen['all_permissions']) - set(seen['permissions']))}"
    )
    assert seen["ledgers"] == seen["all_ledgers"], "осмотр видит не все регистры учёта"
    assert seen["every_unit"], "осмотр ограничен точкой — часть сети он не увидит"


def test_the_network_admin_button_still_enters_as_the_role(demo_db):
    # Иначе демо перестало бы показывать разграничение: кнопка обещает роль
    # «администратор сети», а внутри оказывался бы человек, который может всё.
    seen = probe(demo_db)
    assert seen["role_login_differs"], "учётка роли и учётка осмотра — один логин"
    assert seen["role_password_works"], "кнопка роли больше не входит"
    assert seen["role_permissions"] == ["directory.manage", "roles.manage", "rules.manage"], (
        "роль администратора сети получила чужие права"
    )
