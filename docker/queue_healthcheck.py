"""
Проверка здоровья рабочего процесса очереди для Docker (T024).

Смысл тот же, что у `healthcheck.py`: падать, когда пользоваться нельзя. Но
«нельзя пользоваться» здесь означает не «процесса нет», а «задачи не забирают»,
и это разные вещи. Рабочий процесс `qcluster` — это надзиратель над пулом:
надзиратель бывает жив, а пул задач не берёт (застрял на подключении к базе,
подавился задачей, потерял брокера). Проверка «процесс существует» была бы
зелёной всё это время, то есть хуже отсутствующей.

Поэтому проверяются три вещи, и каждая закрывает свой способ соврать:

1. **Надзиратель на месте.** Без него контейнер жив, а очередь мертва совсем.
   Смотрим прямо в `/proc`, а не через `pgrep`: лишней зависимости в образе не
   заводим, а перебор десятка процессов стоит доли миллисекунды.
2. **База отвечает.** Брокер — это сама база; без неё очередь недееспособна,
   даже если все процессы на месте.
3. **В очереди нет задач, которые никто не взял.** Вот это и есть «работу
   забирают». Порог берётся из `PAYRUN_QUEUE_STALE_SECONDS` — того же, по
   которому страница периода говорит человеку, что расчёт, похоже, некому
   считать, — но с запасом: страница обязана заговорить раньше, чем контейнер
   объявят нездоровым, иначе Docker перезапустит его прежде, чем кто-нибудь
   успеет понять причину.

Почему не спрашиваем саму `django-q2` о состоянии кластера: её статистика лежит
в кэше Django, а кэш по умолчанию — в памяти процесса. Проверка живёт отдельным
процессом от `qcluster`, поэтому увидела бы пустоту и краснела бы всегда.
Заводить общий кэш ради предсказания значило бы завести второй источник истины,
который врёт в обе стороны. Наблюдаемый факт «задачи разбираются» честнее.

Код возврата: 0 — здоров, 1 — нет, с причиной в stderr.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

# Запас над порогом страницы: человек узнаёт о простое раньше, чем Docker
# начинает лечить контейнер перезапуском.
STALE_MARGIN = 3
STALE_SECONDS = int(os.environ.get("PAYRUN_QUEUE_STALE_SECONDS", "10")) * STALE_MARGIN

CLUSTER_MARKER = "qcluster"


def fail(reason: str) -> None:
    print(f"нездоров: {reason}", file=sys.stderr)
    raise SystemExit(1)


def check_cluster_process() -> None:
    """Надзиратель очереди запущен в этом контейнере."""
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().decode(errors="replace")
        except OSError:
            # Процесс закончился, пока мы читали, — обычное дело, не повод падать.
            continue
        if CLUSTER_MARKER in cmdline and "queue_healthcheck" not in cmdline:
            return
    fail(f"процесса {CLUSTER_MARKER} в контейнере нет")


def check_queue_is_being_drained() -> None:
    """База отвечает, и непринятых задач в очереди не накопилось."""
    import django

    django.setup()
    from django.db import connection

    try:
        connection.ensure_connection()
        with connection.cursor() as cur:
            # Прямо по таблице брокера: своей модели у неё нет смысла заводить,
            # а «сколько лежит непринятого» — ровно один вопрос к базе.
            cur.execute(
                "select count(*) from django_q_ormq "
                "where lock < now() - make_interval(secs => %s)",
                (STALE_SECONDS,),
            )
            stale = cur.fetchone()[0]
    except Exception as exc:  # noqa: BLE001 — причина уходит в вывод проверки
        fail(f"брокер очереди недоступен ({exc.__class__.__name__}: {exc})")
    finally:
        connection.close()

    if stale:
        fail(
            f"в очереди {stale} задач(и) старше {STALE_SECONDS} с — "
            "рабочий процесс их не забирает"
        )


if __name__ == "__main__":
    check_cluster_process()
    check_queue_is_being_drained()
    print("здоров")
