"""Демо: `admin` / `admin` — и это учётка роли, которая может всё (D052).

Зачем отдельным паролем. Продукт целиком смотрят учёткой администратора сети:
у неё все права, все регистры и вся сеть. Пароль ей задан свой и намеренно
простой — его диктуют вслух и вписывают руками, а не копируют из переписки.

Зачем тестом. Учётки заводит сид, а демо сбрасывается по расписанию: заведённая
руками на площадке, эта пара исчезала бы каждую ночь. И второе: пароль у роли
теперь не общий, а свой — значит вход по кнопке титульной и вход паролем берут
его из одного места, иначе кнопка «Network administrator» молча перестала бы
пускать.

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
from demo.seed import ADMIN_ROLE, demo_admin_password, demo_password, role_password
from web.permissions import TITLES

out = {}
out["login"] = ADMIN_ROLE
out["password"] = demo_admin_password()
entered = authenticate(username=ADMIN_ROLE, password=demo_admin_password())
out["password_works"] = entered is not None
out["wrong_password_refused"] = authenticate(username=ADMIN_ROLE, password="nope") is None
# Общий пароль демо администратору не подходит: у него свой, и наоборот.
out["shared_password_refused"] = authenticate(username=ADMIN_ROLE, password=demo_password()) is None
out["button_uses_the_same_password"] = role_password(ADMIN_ROLE) == demo_admin_password()
out["other_roles_keep_the_shared_one"] = role_password("director") == demo_password()

# Права и регистры — по всем членствам, как их складывает сама база.
who = models.User.objects.get(username=ADMIN_ROLE)
mine = list(models.Membership.objects.select_related("role").filter(user_id=who.pk))
out["permissions"] = sorted({code for m in mine for code in (m.role.permissions or [])})
out["ledgers"] = sorted({code for m in mine for code in (m.role.visible_ledgers or [])})
out["every_unit"] = any(not m.unit_ids for m in mine)
out["all_permissions"] = sorted(TITLES)
out["all_ledgers"] = sorted(ALL_LEDGERS)
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
    assert seen["password"] == "admin", "пароль сменился — владелец диктует его по памяти"
    assert seen["password_works"], "учётка администратора сети не пускает"


def test_a_wrong_password_is_still_refused(demo_db):
    seen = probe(demo_db)
    assert seen["wrong_password_refused"]
    assert seen["shared_password_refused"], "администратор входит и общим паролем демо"


def test_the_button_and_the_form_take_the_password_from_one_place(demo_db):
    # Иначе кнопка «Network administrator» на титульной перестала бы пускать —
    # молча и без единого следа в интерфейсе.
    seen = probe(demo_db)
    assert seen["button_uses_the_same_password"]
    assert seen["other_roles_keep_the_shared_one"], "остальным ролям сменили пароль заодно"


def test_the_admin_can_do_everything(demo_db):
    # Список прав сверяется с полным списком продукта, а не со вторым перечнем
    # здесь: добавится право — тест обязан покраснеть, а не промолчать.
    seen = probe(demo_db)
    assert seen["permissions"] == seen["all_permissions"], (
        "администратор сети может не всё: "
        f"нет {sorted(set(seen['all_permissions']) - set(seen['permissions']))}"
    )
    assert seen["ledgers"] == seen["all_ledgers"], "администратор видит не все регистры"
    assert seen["every_unit"], "администратор ограничен точкой — часть сети он не увидит"
