"""Раздел, который ведёт другая роль: в демо — переключатель, в продукте — ничего (T163).

Что случилось. Кнопка «Enter the demo →» входит оперативным директором, а
справочники и правила ведёт администратор сети — `directory.manage` и
`rules.manage` есть только у него. Раздела, которого роль не ведёт, в шапке нет
вовсе, и в продукте это правильно: партнёр знает своё устройство, а пропавшая
кнопка — не то же самое, что запрет. Посетителю демо сравнивать не с чем, и он
читает отсутствующий раздел как отсутствующую функциональность. Владелец,
открыв демо, так и прочитал: «не вижу ни групп, ни точек, ничего».

Здесь проверяется ровно то, что из этого следует, и обе половины одинаково
важны:

* **в демо** посетитель до раздела добирается — на месте пропавшего пункта стоит
  ссылка «этот раздел ведёт такая-то роль», и она приводит его туда, куда он шёл,
  уже нужной ролью;
* **в продукте** не меняется ничего — ни ссылки, ни упоминания демо в разметке.
  Иначе цена показа демо была бы уплачена продуктом партнёра, а это не сделка,
  на которую блок стенда вправе пойти.

Плюс два свойства, которые ломаются молча и потому вынесены в проверки:
чужой адрес в `?next=` никуда не уводит (ссылку на демо шлют в переписке и
ходят по ней не глядя), а ключ-спидбамп переживает переключение роли — Django
на смене пользователя чистит сессию целиком, и ключ, положенный до входа,
терялся бы на первом же переключении.

Живые проверки идут в подпроцессе: `DEMO_MODE` и `DEMO_KEY` читаются при
загрузке настроек, а настройки в процессе Django читаются один раз. Подпроцесс
здесь ещё и честнее — так стенд поднимается на площадке.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from conftest import MANAGE_PY, temp_database
from core.roles import ROLE_ORDER, ROLE_SHAPES
from demo.switching import safe_next, who_manages


# Права, на которых стоят пункты шапки: их у роли либо нет, и тогда пункт в
# продукте не показывается вовсе, либо есть. Список здесь — не второй источник
# истины, а перечень мест, где переключатель обязан быть: сами роли берутся из
# `ROLE_SHAPES`.
# Список считается из формы ролей, а не перечисляется здесь: справочники с
# 28.08.2026 ведёт и директор тоже (D059), и приколоченный перечень сразу
# разошёлся бы с продуктом. Нужны те права шапки, которых у ДИРЕКТОРА нет: в
# демо посетитель ходит его ролью, и переключатель показывается ровно там, где
# раздела не видно.
def _nav_permissions_the_director_lacks() -> tuple:
    from core.roles import ROLE_SHAPES

    of_director = set(ROLE_SHAPES["director"].permissions)
    return tuple(
        code for code in ("directory.manage", "rules.manage", "roles.manage")
        if code not in of_director
    )


NAV_PERMISSIONS = _nav_permissions_the_director_lacks()

SCRIPT = """
import json, re
from django.conf import settings
from django.test import Client
from demo.seed import demo_password

out = {}
c = Client()

# Входим тем же путём, каким входит человек: в демо — кнопкой, в продукте —
# формой входа. Подменять вход здесь нельзя: проверяется в том числе то, что
# видит шапка, а она смотрит на настоящую сессию.
if settings.DEMO_MODE:
    r = c.get("/demo/enter/director/%(query)s")
    out["enter"] = r.status_code
else:
    r = c.post("/login/", {"username": "director", "password": demo_password()})
    out["enter"] = r.status_code

page = c.get("/periods/")
out["periods"] = page.status_code
body = page.content.decode()

# Ссылки-переключатели: куда ведут и какой ролью открывают.
out["offers"] = re.findall(r'class="nav-offer" href="([^"]+)"', body)
out["mentions_demo"] = "/demo/" in body

# Берём ЛЮБОЙ доступный переключатель, а не именно справочники: с 28.08.2026
# справочники ведёт и директор (D059), и предложения переключиться на них в
# демо больше нет — а проверка «посетитель доходит до раздела, которого не
# ведёт его роль» от этого не перестаёт быть нужной.
target = out["offers"]
if target:
    hop = c.get(target[0])
    out["switch"] = hop.status_code
    out["switch_to"] = hop.headers.get("Location", "")
    if out["switch"] in (301, 302):
        landed = c.get(out["switch_to"])
        out["directory"] = landed.status_code
        seen = landed.content.decode()
        # Не «страница ответила 200», а «посетитель видит содержимое раздела»:
        # ровно этого владелец и не увидел. Раздел берётся тот, на который
        # предложили переключиться, — его адрес и считаем в ссылках.
        section = out["switch_to"].split("next=")[-1].replace("%%2F", "/")
        out["directory_links"] = seen.count(f'href="{section}')
        out["as_admin"] = "Network administrator" in seen

if settings.DEMO_MODE:
    # Переключение прямым адресом, а не ссылкой из шапки. Отдельно от `switch`
    # намеренно: то проверяет, что шапка отдаёт работающую ссылку, а это — что
    # само переключение живо. Иначе сломанная шапка утаскивала бы за собой
    # проверку ключа-спидбампа, которая к ней отношения не имеет.
    direct = c.get("/demo/enter/admin/", {"next": "/directory/"})
    out["direct_switch"] = direct.status_code
    out["direct_switch_to"] = direct.headers.get("Location", "")
    if out["direct_switch"] in (301, 302):
        after = c.get(out["direct_switch_to"])
        out["direct_directory"] = after.status_code
        out["direct_as_admin"] = "Network administrator" in after.content.decode()

    # Чужой адрес в `?next=` — открытое перенаправление, если его не проверить.
    for where in ("https://evil.example/", "//evil.example/", "/periods/"):
        away = c.get("/demo/enter/admin/", {"next": where})
        out.setdefault("redirects", {})[where] = away.headers.get("Location", "")

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
    with temp_database("switch") as dsn:
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


# --- кто ведёт раздел: из формы ролей, а не из списка рядом с ней --------------


def test_the_section_owner_is_read_from_the_role_shapes():
    """Роль-владелец раздела вычисляется из прав, а не перечислена рядом.

    Список «раздел → роль» рядом с `ROLE_SHAPES` разъехался бы с ним молча —
    ровно то, что уже случилось с двумя списками ролей (issue #91). Поэтому
    проверяется не «владелец справочников — администратор» (это правда
    сегодняшнего дня, и партнёр вправе её изменить), а то, что у названной роли
    названное право действительно есть.
    """
    for permission in NAV_PERMISSIONS:
        code = who_manages(permission)
        assert code is not None, f"{permission}: раздел не ведёт никто — пункт исчезнет"
        assert permission in ROLE_SHAPES[code].permissions, (
            f"{permission}: названа роль {code}, у которой этого права нет"
        )

    assert who_manages("nothing.like.this") is None, (
        "право, которого нет ни у одной роли, получило владельца"
    )


def test_the_first_role_in_the_reading_order_is_offered():
    """Право у нескольких ролей — предлагается первая по порядку экранов.

    Иначе посетителю досталась бы случайная из них, и ссылка меняла бы роль от
    прогона к прогону.
    """
    everyone = [code for code in ROLE_ORDER if "unit.close" in ROLE_SHAPES[code].permissions]
    assert len(everyone) > 1, "проверка потеряла смысл: право осталось у одной роли"
    assert who_manages("unit.close") == everyone[0]


def test_a_foreign_address_is_not_a_destination():
    """`?next=` внутрь продукта — да, куда угодно ещё — нет.

    Ссылку на демо шлют в переписке и ходят по ней не глядя, поэтому уводящий на
    чужой хост адрес здесь дороже, чем в закрытом продукте.
    """
    assert safe_next("/directory/") == "/directory/"
    for hostile in (
        "https://evil.example/", "//evil.example/", "http://127.0.0.1:8109@evil.example/",
        "javascript:alert(1)", "", "directory/",
    ):
        assert safe_next(hostile) is None, f"адрес «{hostile}» принят за внутренний"


# --- живой стенд: демо и продукт ----------------------------------------------


def test_the_visitor_reaches_the_section_another_role_keeps(demo_db):
    """Демо: на месте пропавшего пункта — ссылка, и она доводит до раздела."""
    seen = probe(demo_db, DEMO_MODE="1", DEMO_KEY="")

    assert seen["enter"] in (301, 302), "вход в демо не сработал"
    assert seen["periods"] == 200
    assert len(seen["offers"]) == len(NAV_PERMISSIONS), (
        f"переключателей не столько, сколько разделов не ведёт директор: {seen['offers']}"
    )
    assert all(url.startswith("/demo/enter/admin/") for url in seen["offers"]), (
        f"ссылки открывают не ту роль: {seen['offers']}"
    )
    assert seen["switch"] in (301, 302)
    # Куда именно ведёт первое предложение, зависит от того, какие разделы роль
    # директора ведёт сама: справочники с 28.08.2026 её (D059), поэтому проверка
    # смотрит не на приколоченный адрес, а на то, что переключение уводит в
    # раздел, которого посетителю не хватало.
    assert seen["switch_to"].startswith("/"), (
        f"переключение увело не туда, куда посетитель шёл: {seen['switch_to']}"
    )
    assert seen["directory"] == 200, "раздел не открылся и после переключения роли"
    assert seen["as_admin"], "переключились, но роль осталась прежней"
    assert seen["directory_links"] >= 1, (
        "раздел открылся пустым — посетитель по-прежнему «не видит ни групп, ни точек»"
    )


def test_the_product_header_is_left_exactly_as_it_was(demo_db):
    """Продукт: ни ссылок-переключателей, ни упоминания демо в разметке.

    Демо не вправе платить за свою наглядность устройством продукта партнёра.
    Проверяется той же страницей и той же ролью, что и в демо, — разница только
    в `DEMO_MODE`.
    """
    seen = probe(demo_db, DEMO_MODE="0")

    assert seen["enter"] in (301, 302), "обычный вход в продукт не сработал"
    assert seen["periods"] == 200
    assert seen["offers"] == [], f"в продукте появились ссылки демо: {seen['offers']}"
    assert seen["mentions_demo"] is False, "в разметке продукта упомянуто демо"


def test_a_foreign_address_does_not_take_the_visitor_away(demo_db):
    """То же, что в модульной проверке, но на живом маршруте.

    Отдельной проверкой, потому что проверяет другое: не функцию `safe_next`, а
    что представление её действительно зовёт. Забытый вызов — самая обычная
    причина открытого перенаправления.
    """
    seen = probe(demo_db, DEMO_MODE="1", DEMO_KEY="")

    assert seen["redirects"]["https://evil.example/"] == "/periods/"
    assert seen["redirects"]["//evil.example/"] == "/periods/"
    assert seen["redirects"]["/periods/"] == "/periods/"


def test_the_speed_bump_survives_a_role_switch(demo_db):
    """Ключ задан: переключение роли не выбрасывает посетителя из демо.

    Django на смене пользователя чистит сессию целиком, поэтому ключ, положенный
    в неё до входа, терялся бы на первом же переключении — и ссылка отвечала бы
    404 при заданном `DEMO_KEY`. Тот случай, где всё зелено, пока у демо нет
    ключа, а на площадке он есть.
    """
    seen = probe(demo_db, DEMO_MODE="1", DEMO_KEY="opensesame", KEY="opensesame")

    assert seen["enter"] in (301, 302), "вход по ключу не сработал"
    assert seen["periods"] == 200, "ключ потерялся сразу после входа"
    assert seen["direct_switch"] in (301, 302), (
        "переключение роли отвергнуто — ключ не пережил смену пользователя"
    )
    assert seen["direct_directory"] == 200
    assert seen["direct_as_admin"], "переключились, но роль осталась прежней"
