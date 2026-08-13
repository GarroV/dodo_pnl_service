"""Файл таблицы бухгалтера отдаётся всем ролям одинаково (T104, issue #88).

Что было. Ссылка «таблица бухгалтера» на титульной странице демо работала
анониму и директору, а бухгалтеру и управляющему отвечала 404 — тем самым ролям,
ради которых экран сверки и существует. Да ещё текстом «demo period is not
calculated» про посчитанный и утверждённый период.

Причина. Файл собирался тем же срезом, каким сверка собирает «нашу» сторону
(`reports.reconcile.collect_run`), а тот пропускает строки без итогов. Роли с
неполным набором регистров итоги не видны вовсе (T071) — список выходил пустым,
и представление честно, но неверно объявляло период непосчитанным.

Как решено (см. журнал блока `demo`). Файл по замыслу сделан **вне продукта**:
это таблица, которую бухгалтер ведёт у себя и присылает остальным. Артефакт,
пришедший со стороны, не может зависеть от того, кто его открыл, — иначе сверка
показывала бы разным ролям разные расхождения В ОДНОМ И ТОМ ЖЕ файле. Поэтому он
собирается под полным срезом и одинаков для всех.

Что здесь проверяется:

1. все четыре входа (аноним, директор, бухгалтер, управляющий) получают xlsx;
2. содержимое книги совпадает у всех — файл один и тот же (сравниваются
   значения ячеек, а не байты: время создания в `docProps` разное всегда);
3. загруженный обратно на экран сверки, он проходит разбор у каждой роли.

Проверки идут в подпроцессе: переменные окружения демо читаются при загрузке
настроек, а настройки в процессе Django читаются один раз.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from conftest import MANAGE_PY, temp_database

SCRIPT = """
import hashlib, io, json
import openpyxl
from django.test import Client

URL = "/demo/accountant-table.xlsx"
out = {}


def digest(content):
    # Отпечаток СОДЕРЖИМОГО книги, а не байтов файла: байты у двух скачиваний
    # разные всегда — openpyxl пишет в `docProps` время создания. Сравнение байт
    # в байт краснело бы от секунды между запросами и ничего не говорило бы о
    # данных.
    if not content[:2] == b"PK":
        return "not-a-workbook"
    book = openpyxl.load_workbook(io.BytesIO(content))
    text = ";".join(
        f"{ws.title}|{row}"
        for ws in book.worksheets
        for row in ws.iter_rows(values_only=True)
    )
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# Аноним: ссылка лежит на титульной странице, и нажать её могут раньше входа.
r = Client().get(URL)
out["anonymous"] = {"status": r.status_code, "digest": digest(r.content),
                    "size": len(r.content)}

for role in ("director", "accountant", "manager"):
    c = Client()
    c.get(f"/demo/enter/{role}/")
    r = c.get(URL)
    out[role] = {"status": r.status_code, "digest": digest(r.content),
                 "size": len(r.content)}
    if r.status_code == 200:
        # Тот же файл — обратно на экран сверки, тем же путём, что и человек.
        listing = c.get("/periods/").content.decode()
        import re
        links = re.findall(r'href="(/periods/[0-9a-f-]+/)"', listing)
        page = None
        for link in links:
            html = c.get(link).content.decode()
            if "2026" in html:
                page = link
                break
        from io import BytesIO
        upload = BytesIO(r.content)
        upload.name = "accountant-table.xlsx"
        reconciled = c.post(page + "reconcile/", {"table": upload}, follow=True)
        text = reconciled.content.decode()
        out[role]["reconcile"] = reconciled.status_code
        # «Сошлось до копейки» — сводка сверки; её наличие и означает, что
        # разбор прошёл, а не что страница просто открылась.
        out[role]["reconcile_has_rows"] = "Matched to the cent" in text
        # Человек, оставшийся только в файле бухгалтера, — то самое, ради чего
        # экран существует: сверка обязана назвать его каждой роли.
        out[role]["left_behind"] = "Ashford" in text
print(json.dumps(out))
"""


def probe(dsn: str) -> dict:
    env = {
        **os.environ,
        "DATABASE_URL": dsn,
        "DEMO_DATABASE_URL": dsn,
        "SECRET_KEY": "test-only-not-a-secret",
        "DJANGO_SETTINGS_MODULE": "config.settings",
        "DJANGO_ALLOWED_HOSTS": "localhost,127.0.0.1,testserver",
        "DEMO_MODE": "1",
        "DEMO_KEY": "",
        # Демо всегда англоязычное — так поднимается сам стенд (`docker-compose`),
        # и подписи, которые ищет проверка, должны быть теми же.
        "UI_LANGUAGE": "en",
    }
    result = subprocess.run(
        [sys.executable, str(MANAGE_PY), "shell", "-c", SCRIPT],
        env=env, capture_output=True, text=True, check=True,
    )
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.fixture(scope="module")
def demo_db():
    with temp_database("acctfile") as dsn:
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


@pytest.fixture(scope="module")
def seen(demo_db) -> dict:
    return probe(demo_db)


def test_every_role_gets_the_file(seen):
    """Главная проверка T104: 404 не получает никто."""
    for role in ("anonymous", "director", "accountant", "manager"):
        assert seen[role]["status"] == 200, f"{role}: ответ {seen[role]['status']}"
        assert seen[role]["size"] > 5000, f"{role}: файл подозрительно мал"


def test_the_file_is_the_same_for_everyone(seen):
    """Артефакт, пришедший со стороны, не зависит от того, кто его открыл.

    Иначе сверка показывала бы разным ролям разные расхождения в одном и том же
    файле — то есть демо врало бы про то, ради чего этот экран существует.
    """
    digests = {role: seen[role]["digest"] for role in
               ("anonymous", "director", "accountant", "manager")}
    assert len(set(digests.values())) == 1, digests


def test_the_reconciliation_runs_on_it_for_every_role(seen):
    """Файл не просто скачивается, а проходит сверку у каждой роли."""
    for role in ("director", "accountant", "manager"):
        assert seen[role]["reconcile"] == 200, f"{role}: сверка ответила {seen[role]}"
        assert seen[role]["reconcile_has_rows"], f"{role}: сверка ничего не показала"
        assert seen[role]["left_behind"], (
            f"{role}: человека, оставшегося только в файле, сверка не назвала"
        )
