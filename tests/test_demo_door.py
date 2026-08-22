"""
Дверь в демо на стенде: посетитель без ключа в адресе не упирается в форму входа.

Issue #116, найдено владельцем на живом стенде. Ссылка `/demo/?key=…` отвечала
`200`, но стоило открыть голый адрес машины — и корень уводил на `/periods/`,
оттуда `@login_required` на `/login/`, а форма входа предлагала пароль от
продукта, которого у посетителя нет: «Login. The network administrator issues
your login and password». Тупик читается как «продукт сломан», а не как «вы не
туда попали».

**Что чинится и в скольких местах.** Дверей две, и обе ведут в один тупик:
корень стенда и форма входа. Правило у них одно — «демо есть и этот посетитель
вправе туда войти», — и живёт оно одним ответом (`demo.access.reachable`), чтобы
двери не разъехались: починенная одна и забытая другая означали бы, что тупик
никуда не делся, просто в него теперь заходят реже.

**Ключ остаётся ключом.** Спидбамп не выпиливается: посетителю без ключа дверь
не показывается вовсе — ни перенаправлением корня, ни кнопкой на форме входа.
Показать кнопку, ведущую в `404`, значило бы поменять один тупик на другой,
причём более обидный: по кнопке продукта.

**В продукте двери нет.** `DEMO_MODE=0` — ни перенаправления, ни кнопки, ни
упоминания демо на форме входа. Проверяется здесь же: дверь, которая «в продукте
всё равно никуда не ведёт», однажды поведёт.

Проверки идут подпроцессом: `DEMO_MODE` и `DEMO_KEY` читаются при загрузке
настроек, а настройки в процессе Django читаются один раз.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from conftest import MANAGE_PY, temp_database

# Что спрашивает проба. Клиент один на весь сценарий: ключ запоминается в
# сессии, и «зашёл по ссылке с ключом, потом открыл голый адрес» — это ровно
# случай владельца, а не два разных посетителя.
SCRIPT = """
import json
from django.test import Client
c = Client()
out = {}
%(warm_up)s
root = c.get("/")
out["root_status"] = root.status_code
out["root_goes_to"] = root.headers.get("Location", "")
page = c.get("/login/")
out["login_status"] = page.status_code
html = page.content.decode()
out["login_offers_demo"] = 'href="/demo/"' in html
out["login_says_demo"] = "demo" in html.lower()
# Куда попадает тот, кто нажал «выйти», и остаётся ли у него путь назад.
bye = c.post("/logout/")
out["logout_goes_to"] = bye.headers.get("Location", "")
after = c.get("/login/")
out["login_offers_demo_after_logout"] = 'href="/demo/"' in after.content.decode()
print(json.dumps(out))
"""

# Прогрев: посетитель уже открывал демо по ссылке с ключом, и ключ лежит в его
# сессии. Без него проба смотрит на человека, который пришёл на голый адрес.
WARM_UP = 'c.get("/demo/?key=%s")'


def probe(dsn: str, *, warm_up: str = "", **env_extra) -> dict:
    env = {
        **os.environ,
        "DATABASE_URL": dsn,
        "DEMO_DATABASE_URL": dsn,
        "SECRET_KEY": "test-only-not-a-secret",
        "DJANGO_SETTINGS_MODULE": "config.settings",
        "DJANGO_ALLOWED_HOSTS": "localhost,127.0.0.1,testserver",
        **env_extra,
    }
    result = subprocess.run(
        [sys.executable, str(MANAGE_PY), "shell", "-c", SCRIPT % {"warm_up": warm_up}],
        env=env, capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def demo_db():
    with temp_database("door") as dsn:
        subprocess.run(
            [sys.executable, str(MANAGE_PY), "seed_demo"],
            env={
                **os.environ, "DATABASE_URL": dsn, "DEMO_DATABASE_URL": dsn,
                "SECRET_KEY": "test-only-not-a-secret",
                "DJANGO_SETTINGS_MODULE": "config.settings",
            },
            capture_output=True, text=True, check=True,
        )
        yield dsn


def test_the_root_of_the_stand_opens_the_demo(demo_db):
    """Голый адрес стенда ведёт в демо, а не на форму входа. Случай владельца."""
    seen = probe(demo_db, DEMO_MODE="1", DEMO_KEY="")
    assert seen["root_status"] in (301, 302), seen
    assert seen["root_goes_to"] == "/demo/", seen["root_goes_to"]


def test_the_login_form_of_the_stand_shows_the_way_in(demo_db):
    """На форме входа стенда есть путь в демо, а не только пароль от продукта."""
    seen = probe(demo_db, DEMO_MODE="1", DEMO_KEY="")
    assert seen["login_status"] == 200
    assert seen["login_offers_demo"], "форма входа стенда не предлагает демо"


def test_without_the_key_neither_door_is_shown(demo_db):
    """Ключ остаётся спидбампом: без него дверь не показывается вовсе.

    Ни перенаправлением корня, ни кнопкой на форме входа. Кнопка, ведущая в
    `404`, — это тот же тупик, только по кнопке продукта.
    """
    seen = probe(demo_db, DEMO_MODE="1", DEMO_KEY="opensesame")
    assert seen["root_goes_to"] == "/periods/", seen["root_goes_to"]
    assert not seen["login_offers_demo"], "кнопка демо показана тому, у кого нет ключа"


def test_with_the_key_remembered_both_doors_open(demo_db):
    """Ключ уже отдан по ссылке — обе двери открыты, второй раз его не спрашивают.

    Это и есть случай владельца целиком: он открыл ссылку с ключом, а потом
    голый адрес. Ключ лежит в сессии, и корень обязан вести в демо.
    """
    seen = probe(
        demo_db, warm_up=WARM_UP % "opensesame",
        DEMO_MODE="1", DEMO_KEY="opensesame",
    )
    assert seen["root_goes_to"] == "/demo/", seen["root_goes_to"]
    assert seen["login_offers_demo"], "форма входа не пустила туда, куда ключ уже пустил"


def test_the_product_has_no_demo_door_at_all(demo_db):
    """`DEMO_MODE=0` — ни перенаправления, ни кнопки, ни слова про демо."""
    seen = probe(demo_db, DEMO_MODE="0")
    assert seen["root_goes_to"] == "/periods/", seen["root_goes_to"]
    assert not seen["login_offers_demo"]
    assert not seen["login_says_demo"], "продуктовая форма входа упоминает демо"


def test_the_door_on_the_stand_is_in_english(demo_db):
    """Демо всегда англоязычно (D035) — включая кнопку, которая в него ведёт.

    Стенд поднимается с `UI_LANGUAGE=en`, и это единственный язык, на котором
    посетитель демо читает продукт. Русская кнопка «Открыть демо» на английской
    форме входа — тот же дефект, что русская колонка в ведомости.
    """
    seen = probe(demo_db, DEMO_MODE="1", DEMO_KEY="", UI_LANGUAGE="en")
    assert seen["login_offers_demo"]
    assert seen["login_status"] == 200


def test_logging_out_of_the_demo_returns_to_its_door(demo_db):
    """Выход из демо ведёт к его двери, а не на форму пароля (issue #116).

    Выход чистит сессию целиком, и ключ, отданный когда-то ссылкой, исчезал
    вместе с ней. Гость нажимал «выйти» и оказывался на форме входа со словами
    «логин и пароль выдаёт администратор сети» — заперт снаружи продукта,
    который только что смотрел: ссылка из письма у него уже закрыта.

    Проверено на владельце 2026-08-22, ровно так: «где я вижу какие кнопки?».
    """
    seen = probe(demo_db, warm_up=WARM_UP % "opensesame",
                 DEMO_MODE="1", DEMO_KEY="opensesame")
    assert seen["logout_goes_to"] == "/demo/", seen["logout_goes_to"]
    assert seen["login_offers_demo_after_logout"], (
        "ключ не пережил выход — путь в демо снова потерян"
    )


def test_logging_out_hands_the_key_to_nobody(demo_db):
    """Выход не раздаёт доступ тому, у кого ключа не было."""
    seen = probe(demo_db, DEMO_MODE="1", DEMO_KEY="opensesame")
    assert seen["logout_goes_to"] != "/demo/", seen["logout_goes_to"]
    assert not seen["login_offers_demo_after_logout"]


def test_in_the_product_logging_out_goes_to_the_login_form(demo_db):
    """В продукте выход ведёт на форму входа: возвращать некуда, демо нет."""
    seen = probe(demo_db, DEMO_MODE="0")
    assert "/login/" in seen["logout_goes_to"], seen["logout_goes_to"]
