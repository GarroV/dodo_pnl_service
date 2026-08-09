"""
Вход в демо: за один клик, без регистрации — и ровно там, где демо включено.

Три свойства, каждое из которых стоит дефекта, если сломается молча:

* **выключено — значит маршрутов нет.** Не «страница отвечает отказом»: вход без
  регистрации, который живёт в продукте и лишь притворяется выключенным, —
  обходной путь, ждущий своей опечатки в переменной окружения;
* **включено — значит один клик.** Посетитель без учётки и без куки попадает в
  наполненный продукт, а не на форму входа;
* **спидбамп работает как спидбамп.** Ключ задан — без него страницы нет; с ним
  открыто. Секретом он при этом не притворяется.

Проверки идут в подпроцессе: переменные окружения демо читаются при загрузке
настроек, а настройки в процессе Django читаются один раз. Подпроцесс — это
ещё и честнее: так стенд поднимается на площадке.
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
from django.test import Client
from django.urls import Resolver404, resolve
c = Client()
out = {}
# Отдельно от кода ответа: 404 умеет отдавать и представление, а требование —
# чтобы маршрута не существовало. Снаружи эти два случая неразличимы, и без
# этой проверки «выключено» могло бы означать «включено, но отвечает отказом».
try:
    resolve("/demo/")
    out["route"] = True
except Resolver404:
    out["route"] = False
r = c.get("/demo/%(query)s")
out["landing"] = r.status_code
if r.status_code == 200:
    out["english"] = "live demo" in r.content.decode()
r = c.get("/demo/enter/%(query)s")
out["enter"] = r.status_code
out["lands_on"] = r.headers.get("Location", "")
if r.status_code in (301, 302):
    # Идём по редиректу тем же клиентом: важно не «нас перенаправили», а что на
    # той странице посетитель уже вошедший и видит данные.
    page = c.get(out["lands_on"])
    out["periods"] = page.status_code
    out["rows"] = page.content.decode().count('href="/periods/')
print(json.dumps(out))
"""


def probe(dsn: str, **env_extra) -> dict:
    env = {
        **os.environ,
        "DATABASE_URL": dsn,
        "DEMO_DATABASE_URL": dsn,
        "SECRET_KEY": "test-only-not-a-secret",
        "DJANGO_SETTINGS_MODULE": "config.settings",
        "DJANGO_ALLOWED_HOSTS": "localhost,127.0.0.1,testserver",
        **env_extra,
    }
    query = f"?key={env_extra['KEY']}" if env_extra.get("KEY") else ""
    env.pop("KEY", None)
    result = subprocess.run(
        [sys.executable, str(MANAGE_PY), "shell", "-c", SCRIPT % {"query": query}],
        env=env, capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def demo_db():
    with temp_database("entry") as dsn:
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


def test_demo_is_absent_where_it_is_switched_off(demo_db):
    """`DEMO_MODE=0` — адресов /demo/* не существует вовсе."""
    seen = probe(demo_db, DEMO_MODE="0")
    assert seen["route"] is False, "маршрут /demo/ существует при выключенном демо"
    assert seen["landing"] == 404
    assert seen["enter"] == 404


def test_one_click_puts_a_stranger_inside_the_product(demo_db):
    """Ни учётки, ни куки — и сразу наполненный продукт."""
    seen = probe(demo_db, DEMO_MODE="1", DEMO_KEY="")
    assert seen["route"] is True
    assert seen["landing"] == 200
    assert seen["english"], "титульная страница не англоязычная"
    assert seen["enter"] in (301, 302), "вход не перевёл посетителя дальше"
    assert seen["lands_on"] == "/periods/"
    assert seen["periods"] == 200, "посетитель не попал в продукт"
    assert seen["rows"] >= 3, "в списке периодов пусто — демо не наполнено"


def test_the_key_is_a_speed_bump_and_it_works(demo_db):
    """Ключ задан: без него страницы нет, с ним — открыта."""
    closed = probe(demo_db, DEMO_MODE="1", DEMO_KEY="opensesame")
    assert closed["landing"] == 404
    assert closed["enter"] == 404

    opened = probe(demo_db, DEMO_MODE="1", DEMO_KEY="opensesame", KEY="opensesame")
    assert opened["landing"] == 200
    assert opened["enter"] in (301, 302)

    wrong = probe(demo_db, DEMO_MODE="1", DEMO_KEY="opensesame", KEY="guess")
    assert wrong["landing"] == 404
