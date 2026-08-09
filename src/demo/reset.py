"""
Сброс демо: пересоздание базы из эталона (D016).

Почему именно пересоздание, а не «удалить строки и насыпать заново». Сид в той
же базе оставляет за собой всё, чего он не знает: чужой тенант, забытую
последовательность, строку, которую посетитель успел создать в момент уборки.
Пересозданная из эталона база не может отличаться от эталона ничем — это
свойство самой операции, а не аккуратности кода. Ровно поэтому владелец и решил
D016 так: сброс обязан быть атомарным и физически неспособным задеть боевые
данные.

Как устроено:

    <demo>_template   эталон: миграции + наполнение, собирается редко
    <demo>            сама демо-база, пересоздаётся из эталона по расписанию

Сброс = `drop database <demo>` + `create database <demo> template <demo>_template`.
Секунды вместо минут пересчёта трёх месяцев, и результат побайтово тот же.

Два предохранителя, и они разной природы, чтобы не отказать вместе:

1. удалить можно только **помеченную** базу — метку (`demo_stamp`) ставит эта же
   команда при создании базы или при первом наполнении пустой. Чужая база не
   будет удалена, даже если `DEMO_DATABASE_URL` показывает на неё. Это главный
   предохранитель: сравнение адресов здесь бесполезно, потому что у демо
   `DATABASE_URL` и `DEMO_DATABASE_URL` совпадают по замыслу (см. `demo.guard`);
2. имя эталона выводится из имени демо-базы, а не задаётся отдельной
   переменной: ещё одна переменная — ещё одно место, где окажется чужое имя.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import psycopg
from django.conf import settings

from .guard import (
    STAMP,
    STAMP_TABLE,
    DemoGuardRefused,
    database_name,
    demo_dsn,
    maintenance_dsn,
    template_name,
)

__all__ = ["exists", "is_stamped", "rebuild_template", "restore", "run_every", "stamp"]

# Сколько ждать между попытками отпустить базу. Пересоздание не пройдёт, пока к
# базе кто-то подключён, а приложение переподключается само.
RELEASE_ATTEMPTS = 5
RELEASE_PAUSE = 0.5


def _dsn_for(dsn: str, dbname: str) -> str:
    from psycopg.conninfo import make_conninfo

    return make_conninfo(dsn, dbname=dbname)


def exists(dsn: str, dbname: str) -> bool:
    with psycopg.connect(maintenance_dsn(dsn), autocommit=True) as admin:
        return bool(
            admin.execute(
                "select 1 from pg_database where datname = %s", (dbname,)
            ).fetchone()
        )


def stamp(dsn: str, dbname: str) -> None:
    """Пометить базу как созданную демо-командой."""
    with psycopg.connect(_dsn_for(dsn, dbname), autocommit=True) as conn:
        conn.execute(f"create table if not exists {STAMP_TABLE} (marker text primary key)")
        conn.execute(
            f"insert into {STAMP_TABLE} (marker) values (%s) on conflict do nothing",
            (STAMP,),
        )


def is_stamped(dsn: str, dbname: str) -> bool:
    """Помечена ли база. Несуществующая база — не помечена (и не удаляется)."""
    if not exists(dsn, dbname):
        return False
    with psycopg.connect(_dsn_for(dsn, dbname)) as conn:
        row = conn.execute(
            "select to_regclass(%s) is not null", (STAMP_TABLE,)
        ).fetchone()
        if not row or not row[0]:
            return False
        marked = conn.execute(
            f"select 1 from {STAMP_TABLE} where marker = %s", (STAMP,)
        ).fetchone()
        return bool(marked)


def _drop(dsn: str, dbname: str, log) -> None:
    """Удалить базу, если она помечена нашей меткой. Иначе — отказ словами."""
    if not exists(dsn, dbname):
        return
    if not is_stamped(dsn, dbname):
        raise DemoGuardRefused(
            f"база «{dbname}» существует, но не помечена как демо "
            f"(нет строки «{STAMP}» в таблице {STAMP_TABLE}). "
            "Удалять её эта команда не будет: помеченные базы создаёт она сама, "
            "а непомеченная — чужая. Проверьте DEMO_DATABASE_URL."
        )

    with psycopg.connect(maintenance_dsn(dsn), autocommit=True) as admin:
        for attempt in range(RELEASE_ATTEMPTS):
            admin.execute(
                """select pg_terminate_backend(pid) from pg_stat_activity
                    where datname = %s and pid <> pg_backend_pid()""",
                (dbname,),
            )
            try:
                admin.execute(f'drop database if exists "{dbname}" with (force)')
                log(f"база {dbname} удалена")
                return
            except psycopg.errors.ObjectInUse:
                # Приложение переподключается само и может успеть занять базу
                # между отключением и удалением. Это не ошибка, это гонка —
                # ждём и повторяем, а не сдаёмся молча.
                if attempt == RELEASE_ATTEMPTS - 1:
                    raise
                time.sleep(RELEASE_PAUSE)


def _create_from(dsn: str, dbname: str, template: str, log) -> None:
    with psycopg.connect(maintenance_dsn(dsn), autocommit=True) as admin:
        # Эталон тоже нужно отпустить: копировать можно только базу, к которой
        # никто не подключён.
        admin.execute(
            """select pg_terminate_backend(pid) from pg_stat_activity
                where datname = %s and pid <> pg_backend_pid()""",
            (template,),
        )
        admin.execute(f'create database "{dbname}" template "{template}"')
    log(f"база {dbname} создана из эталона {template}")


def _manage(dsn: str, *args: str) -> None:
    """Команда Django на указанной базе — подпроцессом.

    Подпроцессом, а не импортом: настройки Django читаются один раз на процесс,
    и переключить их на другую базу внутри живого процесса нельзя честно. Тот же
    способ, каким пользуются тесты схемы.
    """
    manage = Path(settings.BASE_DIR) / "manage.py"
    env = {**os.environ, "DATABASE_URL": dsn}
    result = subprocess.run(
        [sys.executable, str(manage), *args],
        env=env, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise DemoGuardRefused(
            f"команда {' '.join(args)} на демо-базе не прошла:\n"
            f"{result.stdout}\n{result.stderr}"
        )


def rebuild_template(*, log=None) -> str:
    """Собрать эталон заново: чистая база, миграции, наполнение.

    Возвращает имя эталона. Операция редкая: её гоняют при выкладке новой
    версии, а не по расписанию — по расписанию идёт только восстановление.
    """
    log = log or (lambda *_: None)
    dsn = demo_dsn()
    template = template_name(dsn)

    _drop(dsn, template, log)
    with psycopg.connect(maintenance_dsn(dsn), autocommit=True) as admin:
        admin.execute(f'create database "{template}"')
    # Метка ставится сразу после создания, до миграций: упавшее наполнение не
    # должно оставлять базу, которую эта же команда потом откажется удалять.
    stamp(dsn, template)
    log(f"эталон {template} создан")

    template_dsn = _dsn_for(dsn, template)
    _manage(template_dsn, "migrate", "--noinput")
    _manage(template_dsn, "seed_demo")
    log(f"эталон {template} наполнен")
    return template


def restore(*, log=None) -> None:
    """Вернуть демо-базу к эталону. Это и есть сброс.

    Эталона нет — он собирается сам: «сброс» на пустом месте обязан дать
    работающий стенд, а не отказ «сначала соберите эталон».
    """
    log = log or (lambda *_: None)
    dsn = demo_dsn()
    template = template_name(dsn)
    if not exists(dsn, template):
        log("эталона нет — собираем")
        rebuild_template(log=log)

    _drop(dsn, database_name(dsn), log)
    _create_from(dsn, database_name(dsn), template, log)


def run_every(minutes: int, *, log=None) -> None:
    """Сброс по расписанию: ровно это крутится службой демо в compose.

    Расписание, а не кнопка: демо, которое чинят руками, протухает в первый же
    день, когда на него никто не смотрит.
    """
    log = log or (lambda *_: None)
    while True:
        time.sleep(minutes * 60)
        try:
            restore(log=log)
            log("демо сброшено к эталону")
        except Exception as broken:  # noqa: BLE001 — служба обязана пережить сбой
            # Молчать нельзя: невидимо не сработавший сброс и есть протухшее
            # демо. Но и падать нельзя — служба перезапустится и попробует снова.
            log(f"сброс не прошёл: {broken}")
