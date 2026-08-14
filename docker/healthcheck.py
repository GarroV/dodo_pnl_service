"""
Проверка здоровья приложения для Docker.

Смысл проверки — падать, когда продуктом нельзя пользоваться. Поэтому она
трогает обе половины: и веб, и базу.

- Проверять только TCP-порт бесполезно: сокет открыт, пока жив процесс, даже
  если база лежит и любая страница отдаёт 500. Такая проверка зелёная всегда,
  то есть хуже, чем никакой.
- Проверять только базу тоже мало: база может быть в порядке, а сервер не
  принимать запросы.

Что считаем живым: HTTP-ответ со статусом меньше 500 (страница нашлась или
честно не нашлась — важно, что запрос дошёл до Django и вернулся) **и**
установленное соединение с базой из того же процесса, что и приложение.

Порог именно 500, а не «строго 200»: 4xx означает, что запрос дошёл до Django
и вернулся, то есть продукт поднят. Сегодня корень (`web.views.index`) отвечает
200 и без входа, но проверка не должна краснеть от того, что маршрут закрыли
правами или перенесли, — за этим следят тесты, а не healthcheck. Путь берётся
из `HEALTHCHECK_PATH`, поэтому перенацелить проверку можно не трогая код.

Код возврата: 0 — здоров, 1 — нет, с причиной в stderr.
"""
from __future__ import annotations

import http.client
import os
import sys

# Порт ВНУТРИ контейнера, а не на хосте, — и поэтому не настройка (issue #107).
# Приложение в контейнере слушает 8000 всегда: это записано в команде службы и в
# правой части проброса портов (`docker-compose.yml`), а `APP_PORT` — левая
# часть, порт снаружи. Пока порт читался из окружения, его правили заодно с
# `APP_PORT` под второй стенд, и проверка стучалась в 8000-й порт хоста изнутри
# контейнера: `Connection refused`, `unhealthy` семь часов подряд при
# работающем продукте. Проверка, краснеющая не по делу, обесценивает себя —
# на неё перестают смотреть. Равенство трёх чисел сторожит
# `tests/test_compose_stand.py`.
PORT = 8000
PATH = os.environ.get("HEALTHCHECK_PATH", "/")
TIMEOUT = float(os.environ.get("HEALTHCHECK_TIMEOUT", "5"))


def fail(reason: str) -> None:
    print(f"нездоров: {reason}", file=sys.stderr)
    raise SystemExit(1)


def check_http() -> None:
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=TIMEOUT)
    try:
        conn.request("GET", PATH)
        status = conn.getresponse().status
    except OSError as exc:
        fail(f"сервер не отвечает на http://127.0.0.1:{PORT}{PATH} ({exc})")
    finally:
        conn.close()
    if status >= 500:
        fail(f"сервер вернул {status} на {PATH}")


def check_database() -> None:
    import django

    django.setup()
    from django.db import connection

    try:
        connection.ensure_connection()
        with connection.cursor() as cur:
            cur.execute("select 1")
    except Exception as exc:  # noqa: BLE001 — причина уходит в вывод проверки
        fail(f"база недоступна ({exc.__class__.__name__}: {exc})")
    finally:
        connection.close()


if __name__ == "__main__":
    check_http()
    check_database()
    print("здоров")
