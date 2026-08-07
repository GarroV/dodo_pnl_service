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

Ответ 404 считается живым намеренно: пока блок `web` не завёл маршруты,
корень отдаёт именно 404, и это состояние «поднято». Когда появится страница
входа, проверку стоит перенацелить на неё через `HEALTHCHECK_PATH`.

Код возврата: 0 — здоров, 1 — нет, с причиной в stderr.
"""
from __future__ import annotations

import http.client
import os
import sys

PORT = int(os.environ.get("HEALTHCHECK_PORT", "8000"))
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
