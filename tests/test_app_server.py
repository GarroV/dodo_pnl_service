"""Чем поднимается продукт: боевой сервер, а не `runserver` (T181, issue #191).

`runserver` — сервер разработки, и Django говорит об этом прямо. На стенде это
стоило трижды:

- **автоперезагрузчик** обходит файловую систему в поисках правок, которых в
  контейнере не бывает: код запечён в образ, монтирования исходников нет ни у
  одной службы. Работа выброшена целиком, а платится за неё постоянным CPU —
  замер простоя дал 5,5 % у продукта и 5,8 % у демо против 1 % у чужих
  контейнеров рядом (issue #190);
- **один процесс без присмотра**: упавший поток превращается в 500 навсегда,
  перезапускать некому;
- **демо смотрят снаружи** — оно за публичным адресом, и показывать там сервер
  разработки не стоит даже когда всё работает.

Отдельно про то, чего в решении НЕТ. Первым напрашивается «локально оставить
`runserver` ради автоперезагрузки, а на площадке боевой сервер» — так сказано
и в самом issue. Оно не взято: автоперезагружать в контейнере нечего. Исходники
в образ **скопированы**, а не подмонтированы (в `docker-compose.yml` нет ни
одного `volumes:` у служб приложения), поэтому файл на хосте меняется, а в
контейнере — нет. То есть локальный `runserver` в compose давал не удобство, а
только свой расход. Разработка с автоперезагрузкой шла и идёт через
`manage.py runserver` из venv, и её это изменение не касается вовсе.

Выигрыш от того, что вариант один: локальный стенд и площадка поднимаются
**одинаково**. Расхождение «локально одно, на площадке другое» этот проект уже
оплачивал не раз — ломается оно всегда на площадке и всегда молча.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "docker-compose.yml"
DOCKERFILE = ROOT / "Dockerfile"
PYPROJECT = ROOT / "pyproject.toml"

# Службы, которые обслуживают людей по HTTP. `worker` и `migrate` — не про это.
SERVING = ("app", "demo")


def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def command_of(name: str) -> list[str]:
    return [str(part) for part in compose()["services"][name]["command"]]


def test_no_service_that_answers_people_runs_the_development_server():
    """`runserver` не поднимает ни продукт, ни демо."""
    for name in SERVING:
        assert "runserver" not in command_of(name), (
            f"служба {name} снова поднимается сервером разработки: {command_of(name)}"
        )


def image_default_command() -> list[str]:
    """`CMD` из Dockerfile списком. Строка склеивается из продолжений `\\`."""
    text = DOCKERFILE.read_text(encoding="utf-8").replace("\\\n", " ")
    line = next(row for row in text.splitlines() if row.startswith("CMD "))
    return json.loads(line[len("CMD "):])


def test_the_image_default_command_is_not_the_development_server_either():
    """`CMD` образа — то, чем контейнер стартует без compose.

    Забытый там `runserver` возвращает дефект целиком для любого, кто запустит
    образ напрямую: `docker run <образ>`. Проверяется сама команда, а не текст
    файла: слово `runserver` в объяснении рядом — это как раз хорошо.
    """
    assert "runserver" not in image_default_command(), (
        f"в CMD образа остался runserver: {image_default_command()}"
    )


def test_the_image_and_compose_do_not_drift_apart():
    """Образ напрямую и образ под compose обязаны вести себя одинаково.

    Числа живут в двух местах — умолчаниями подстановок в compose и литералами
    в `CMD`, — и это осознанная плата: подстановки видны в
    `docker compose config` и не зависят от того, доехала ли переменная в
    контейнер. Плата допустима ровно до тех пор, пока за разъездом кто-то
    следит; следит этот тест.
    """
    defaults = re.compile(r"^\$\{[A-Z_]+:-(.*)\}$")
    from_compose = [defaults.sub(r"\1", part) for part in command_of("app")]
    assert from_compose == image_default_command(), (
        "команда запуска в Dockerfile и в docker-compose.yml разъехались:\n"
        f"  compose:    {from_compose}\n"
        f"  Dockerfile: {image_default_command()}"
    )


def test_the_serving_services_run_gunicorn_on_the_wsgi_entry_point():
    """Боевой сервер и точка входа названы явно, а не подразумеваются."""
    for name in SERVING:
        command = command_of(name)
        assert command[0] == "gunicorn", f"служба {name} поднимается не gunicorn: {command}"
        assert "config.wsgi:application" in command, (
            f"служба {name} не сказала gunicorn, что запускать: {command}"
        )


def test_the_app_server_is_a_declared_dependency_and_pinned_to_a_major():
    """Сервер, которого нет в зависимостях, не окажется в образе.

    Верхняя граница обязательна: смена мажора у сервера приложений — это смена
    поведения тайм-аутов и воркеров, и узнавать о ней из упавшей сборки нельзя.
    """
    text = PYPROJECT.read_text(encoding="utf-8")
    line = next((row for row in text.splitlines() if "gunicorn" in row), None)
    assert line, "gunicorn не объявлен в зависимостях — в образ он не попадёт"
    assert re.search(r"gunicorn>=\d+\.\d+.*<\d+", line), (
        f"зависимость gunicorn без верхней границы мажора: {line.strip()}"
    )


def test_the_image_puts_the_source_tree_ahead_of_the_installed_copy():
    """PYTHONPATH обязателен, и это не украшение — проверено в контейнере.

    В образе код лежит дважды: исходники в `/app/src` и установленная копия в
    `site-packages`. `manage.py` кладёт `/app/src` первым сам, а gunicorn —
    консольный сценарий, каталога запуска в путь он не добавляет. Без PYTHONPATH
    он импортировал бы настройки из `site-packages`, где

        BASE_DIR = Path(__file__).resolve().parent.parent.parent

    равен не `/app`, а `/usr/local/lib/python3.13`. Спрошено у живого
    контейнера: `STATIC_ROOT` уезжает в `/usr/local/lib/python3.13/staticfiles`,
    `LOCALE_PATHS` — туда же. То есть продукт поднялся бы без собранной статики
    и без переводов: экраны без стилей и htmx (ровно issue #68) и англоязычное
    демо, внезапно заговорившее по-русски.
    """
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"^ENV PYTHONPATH=/app/src$", text, re.MULTILINE), (
        "в Dockerfile нет `ENV PYTHONPATH=/app/src` — gunicorn возьмёт настройки "
        "из site-packages, и BASE_DIR уедет мимо /app"
    )


def test_the_request_timeout_is_not_shorter_than_the_work_a_request_may_do():
    """Умолчание gunicorn (30 с) оставлять нельзя, и число обязано быть ручкой.

    Что этот тайм-аут значит, выяснено опытом на самом образе, а не вычитано:
    это сторож **застрявшего воркера**, а не срок на запрос. `sync`-воркер
    отмечается живым только между запросами, поэтому запрос длиннее тайм-аута
    его убивает — `--timeout 3` и запрос на 20 с дали HTTP 500 через 3,35 с и
    `Worker exiting`. Потоковый воркер отмечается из главного цикла, и тот же
    запрос отдал 200 через 20,01 с, не перезапустившись.

    Значит синхронный расчёт периода (`PAYRUN_BACKGROUND=0`, осознанный режим
    для площадки без рабочего процесса очереди) на выбранном воркере безопасен,
    а запас нужен на другое: долгий расчёт держит GIL и может подтормозить сам
    цикл отметок.
    """
    for name in SERVING:
        command = command_of(name)
        assert "--timeout" in command, (
            f"служба {name} оставила тайм-аут gunicorn на умолчании 30 с — "
            "синхронный расчёт периода в него не укладывается"
        )
        value = command[command.index("--timeout") + 1]
        assert "APP_REQUEST_TIMEOUT" in value, (
            f"служба {name} зашила тайм-аут числом: {value}. На площадке его "
            "меняют переменной, а не пересборкой образа"
        )


def test_a_slow_request_does_not_hold_the_whole_stand():
    """Потоковый воркер назван явно, а не получен молчаливой подменой.

    `sync` отпадает по двум проверенным причинам: он убивает запрос длиннее
    тайм-аута (см. соседнюю проверку) и не держит keep-alive, из-за чего доки
    gunicorn требуют перед ним буферизующий прокси — а перед продуктом его нет.
    Async-воркер отпадает тоже: Django здесь синхронный (обычные представления,
    ORM, `ATOMIC_REQUESTS`), транзакций в асинхронном режиме Django пока не
    умеет, а whitenoise ASGI-версии не имеет вовсе.

    Почему `--worker-class` обязателен, хотя `--threads` включил бы поток и сам:
    gunicorn подменяет `sync` на `gthread`, как только потоков больше одного
    (`Config.worker_class` — прочитано в образе). Подмена молчаливая, и
    полагаться на неё нельзя: уберут `--threads` — воркер станет `sync`, ничего
    об этом не сказав.
    """
    for name in SERVING:
        command = command_of(name)
        assert "gthread" in command, f"служба {name} поднимается не потоковым воркером: {command}"
        assert "--worker-class" in command, (
            f"служба {name} получила поток молчаливой подменой, а не назвала его: {command}"
        )
        assert "--threads" in command, f"служба {name} не задала число потоков: {command}"


def test_the_stand_keeps_saying_which_requests_it_served():
    """Журнал обращений не должен пропасть вместе с `runserver`.

    Он и был единственным способом увидеть в `docker compose logs`, что запрос
    вообще дошёл. Молчащий стенд отлаживают вслепую.
    """
    for name in SERVING:
        command = command_of(name)
        assert "--access-logfile" in command, (
            f"служба {name} перестала писать журнал обращений: {command}"
        )


def test_the_example_env_carries_the_knobs_of_the_app_server():
    """Ручки, которых нет в примере окружения, не существуют для площадки."""
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    for name in ("APP_SERVER_WORKERS", "APP_SERVER_THREADS", "APP_REQUEST_TIMEOUT"):
        assert f"{name}=" in example, f"{name} не описана в .env.example"
