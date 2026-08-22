"""Простая учётка демо: `demodemo` / `password` (запрос владельца 2026-08-22).

Зачем она есть. Гость, вышедший из демо или пришедший по ссылке без ключа,
попадает на форму входа. Логины ролей — `accountant`, `director`, `manager` —
там не угадать, а спросить не у кого: демо на то и демо, что человек в нём один.

Зачем это тестом. Учётка заводится сидом, а демо сбрасывается по расписанию:
заведённая руками на площадке, она исчезала бы каждую ночь, и «вчера работало»
повторялось бы каждое утро. Тест держит её в сиде, а не в чьей-то памяти.

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
from demo.seed import GUEST_LOGIN, GUEST_ROLE, demo_guest_password
from demo.views import DEFAULT_ROLE
from core import models

out = {}
out["login"] = GUEST_LOGIN
ok = authenticate(username=GUEST_LOGIN, password=demo_guest_password())
out["password_works"] = ok is not None
out["wrong_password_refused"] = authenticate(username=GUEST_LOGIN, password="nope") is None
out["role_matches_the_button"] = GUEST_ROLE == DEFAULT_ROLE
# У членства нет связи на пользователя объектом — только user_id (D013).
guest = models.User.objects.get(username=GUEST_LOGIN)
member = models.Membership.objects.filter(user_id=guest.pk).first()
out["has_membership"] = member is not None
out["role_code"] = member.role.code if member else ""
out["sees_every_unit"] = (member.unit_ids is None) if member else False
print(json.dumps(out))
"""


@pytest.fixture(scope="module")
def demo_db():
    with temp_database("guest") as dsn:
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


def test_the_simple_login_exists_after_a_reset(demo_db):
    seen = probe(demo_db)
    assert seen["login"] == "demodemo"
    assert seen["password_works"], "простая учётка не пускает — гость снова заперт"


def test_a_wrong_password_is_still_refused(demo_db):
    assert probe(demo_db)["wrong_password_refused"]


def test_the_guest_gets_the_same_role_as_the_button(demo_db):
    # Иначе вход паролем и вход кнопкой показывали бы разный продукт.
    seen = probe(demo_db)
    assert seen["role_matches_the_button"], "роль гостя разъехалась с кнопкой демо"
    assert seen["has_membership"], "учётка есть, а прав нет — вход в пустоту"
    assert seen["role_code"] == "director"
    assert seen["sees_every_unit"], "гостю демо показывают не всю сеть"
