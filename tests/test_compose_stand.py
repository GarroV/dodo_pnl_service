"""Обвязка стенда: проверка здоровья и имя проекта Compose (T138).

Два дефекта, и оба молчаливые — стенд при них работает, а говорит неправду.

**Проверка здоровья стучалась в порт хоста** (issue #107). `APP_PORT` — порт
снаружи контейнера, `HEALTHCHECK_PORT` читался внутри, где приложение слушает
8000 всегда. Обе переменные лежали рядом в `.env.example`, обе назывались
«порт», и тот, кто разводил порты под второй стенд, правил их пачкой: контейнер
семь часов стоял `unhealthy` при работающем продукте. Проверка, краснеющая не по
делу, обесценивает себя — на неё перестают смотреть.

Чинится не подсказкой в комментарии, а тем, что рассинхронизировать становится
нечего: порт внутри контейнера остаётся ровно в двух местах compose (команда
службы и правая часть проброса), переменной окружения на него больше нет, а
`docker/healthcheck.py` знает его одной константой. Тесты ниже сторожат
равенство этих трёх чисел и отсутствие переменной.

**Имя проекта было зашито** (issue #51). `name: dodo-pnl` в файле означает, что
все рабочие копии репозитория управляют одними контейнерами и томами: `up` из
соседней копии молча меняет вам порты, `down -v` уносит вашу базу вместе с
сидом. Воспроизведено на живой стройке дважды.

Чинится так, чтобы забыть было **безопасно**: `name` из файла убран, и тогда
Compose берёт имя проекта из каталога — а каталоги у рабочих копий разные. Тег
образа уезжает туда же (`${COMPOSE_PROJECT_NAME}-app`): compose подставляет
вычисленное имя проекта и в интерполяцию, так что копии не перетирают сборки
друг другу даже без переменной в `.env`. Это спрошено у compose тестом ниже, а
не взято из документации. Общий тег при этом остаётся общим на все службы: одна
сборка на схему, продукт и очередь — см. комментарий службы `migrate`.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "docker-compose.yml"
ENV_EXAMPLE = ROOT / ".env.example"


def compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def _published(service: dict) -> list[str]:
    return [str(item) for item in service.get("ports", [])]


# =============================================================================
# Проверка здоровья ходит внутрь контейнера
# =============================================================================


def test_healthcheck_knocks_on_the_port_the_app_listens_on_inside_the_container():
    """Три числа — команда, проброс и сама проверка — обязаны совпадать.

    Разошлись — стенд стоит `unhealthy` при работающем продукте, и узнать об
    этом можно только заглянув в `docker compose ps`.
    """
    sys.path.insert(0, str(ROOT))
    from docker import healthcheck

    services = compose()["services"]
    for name in ("app", "demo"):
        service = services[name]
        command = service["command"]
        assert f"0.0.0.0:{healthcheck.PORT}" in command, (
            f"служба {name} слушает не на {healthcheck.PORT}: {command}"
        )
        inside = [item.rsplit(":", 1)[-1] for item in _published(service)]
        assert inside == [str(healthcheck.PORT)], (
            f"служба {name} пробрасывает наружу не {healthcheck.PORT}, а {inside}"
        )


def test_the_healthcheck_port_is_not_a_setting_anymore():
    """Переменной нет ни в compose, ни в примере окружения — путать нечего.

    Ровно она и была ловушкой: её правили заодно с `APP_PORT`, потому что обе
    называются «порт» и лежат рядом.
    """
    assert "HEALTHCHECK_PORT" not in COMPOSE.read_text(encoding="utf-8")
    assert "HEALTHCHECK_PORT" not in ENV_EXAMPLE.read_text(encoding="utf-8")


def test_the_path_and_the_timeout_of_the_healthcheck_stay_settings():
    """Перенацелить проверку на площадке по-прежнему можно — портом это не делают."""
    text = COMPOSE.read_text(encoding="utf-8")
    assert "HEALTHCHECK_PATH" in text and "HEALTHCHECK_TIMEOUT" in text
    example = ENV_EXAMPLE.read_text(encoding="utf-8")
    assert "HEALTHCHECK_PATH=" in example and "HEALTHCHECK_TIMEOUT=" in example


# =============================================================================
# Имя проекта: забыть нельзя
# =============================================================================


def test_compose_does_not_pin_one_project_name_for_every_working_copy():
    """Зашитое `name` склеивает параллельные копии в один стенд (issue #51)."""
    assert "name" not in compose(), (
        "в docker-compose.yml снова зашито имя проекта — рабочие копии "
        "склеятся и `down -v` из соседней унесёт чужую базу"
    )


def test_every_built_image_is_tagged_with_the_project_name():
    """Тег образа берёт имя проекта — иначе копии перетирают сборки друг другу.

    Умолчание в этой подстановке было бы дефектом: с ним забытая переменная
    означала бы общий тег `dodo-pnl-app`, то есть соседняя копия молча
    подсовывает свой код при следующем перезапуске службы. `:?` оставлен затем,
    чтобы на compose, который имя проекта в интерполяцию не подставляет, вышел
    внятный отказ, а не пустой тег `-app`.
    """
    services = compose()["services"]
    tagged = {name: service["image"] for name, service in services.items() if "image" in service}
    built = {name: image for name, image in tagged.items() if "pgvector" not in image}
    assert built, "в compose не осталось ни одной собираемой службы — тест проверял бы не то"
    for name, image in built.items():
        assert "${COMPOSE_PROJECT_NAME:?" in image, (
            f"служба {name} тегирует образ без обязательного имени проекта: {image}"
        )
    assert len(set(built.values())) == 1, (
        f"службы собираются в разные образы, схема и продукт разъедутся: {built}"
    )


def test_the_example_env_does_not_hand_out_a_ready_project_name():
    """Скопированный `.env.example` не должен приносить чужое имя стенда.

    Готовое значение в примере вернуло бы дефект целиком: две копии, скопировав
    пример, снова оказались бы одним проектом.
    """
    lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()
    assert not [line for line in lines if line.strip().startswith("COMPOSE_PROJECT_NAME=")]
    assert any("COMPOSE_PROJECT_NAME" in line for line in lines), (
        "про имя проекта в примере окружения не сказано вовсе — забудут"
    )


# =============================================================================
# То же самое, но спросив у самого compose
# =============================================================================


def _compose_config(extra_env: dict[str, str]) -> subprocess.CompletedProcess:
    """`docker compose config` на примере окружения и без чужих переменных.

    Окружение собирается с нуля: `COMPOSE_PROJECT_NAME` часто стоит в оболочке
    того, кто гоняет тесты (им же разводятся стенды блоков), и унаследованная
    переменная сделала бы проверку зелёной всегда.
    """
    env = {"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
    env.update(extra_env)
    return subprocess.run(
        ["docker", "compose", "--env-file", str(ENV_EXAMPLE), "config"],
        cwd=ROOT, capture_output=True, text=True, env=env, timeout=120,
    )


needs_docker = pytest.mark.skipif(
    shutil.which("docker") is None, reason="нет docker — проверку конфигурации спросить не у кого"
)


@needs_docker
def test_without_a_project_name_the_stand_takes_the_name_of_the_working_copy():
    """Забыть имя можно, и это безопасно: стенд называется по каталогу копии.

    Спрошено у самого compose, а не выведено из документации: он подставляет
    вычисленное имя проекта и в интерполяцию, поэтому тег образа уезжает туда
    же. Каталоги у рабочих копий разные — значит разные и контейнеры, и тома, и
    образы. Проверено на Compose v5.3.1.
    """
    done = _compose_config({})
    assert done.returncode == 0, done.stderr
    config = yaml.safe_load(done.stdout)
    assert config["name"] == ROOT.name.lower()
    assert config["services"]["app"]["image"] == f"{ROOT.name.lower()}-app", (
        "образ собирается под именем, не зависящим от рабочей копии — соседняя "
        "копия перетрёт его своей сборкой"
    )


@needs_docker
def test_with_a_project_name_the_stand_configures_and_takes_that_name():
    """С именем — обычная работа, и стенд называется именно так."""
    done = _compose_config({"COMPOSE_PROJECT_NAME": "dodo-pnl-probe"})
    assert done.returncode == 0, done.stderr
    config = yaml.safe_load(done.stdout)
    assert config["name"] == "dodo-pnl-probe"
    assert config["services"]["app"]["image"] == "dodo-pnl-probe-app"
