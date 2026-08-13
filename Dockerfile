# Образ приложения. Python 3.13 — решение D018: 3.11 и 3.12 уже security-only,
# а Django 6.1 требует 3.12+.
#
# Зачем slim, а не alpine: psycopg[binary] и openpyxl приезжают готовыми
# колёсами под glibc. На alpine их пришлось бы собирать, то есть тянуть
# компилятор в образ ради того же результата.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DJANGO_SETTINGS_MODULE=config.settings

WORKDIR /app

# Зависимости ставятся установкой самого проекта: список живёт в pyproject.toml
# в одном экземпляре. Дублировать его в Dockerfile нельзя — разъедется молча.
COPY pyproject.toml ./
COPY src ./src
RUN pip install .

COPY manage.py ./
# Сид тестовых данных читает обезличенную фикстуру по пути BASE_DIR/tests/fixtures
# (решение D028: настоящая таблица партнёра не загружается вообще). Кладём только
# сам файл — остальные тесты в рабочем образе не нужны.
COPY tests/fixtures/plata-sample.xlsx ./tests/fixtures/
# Каталогом целиком, а не пофайлово: проверок здоровья стало две — веб и рабочий
# процесс очереди (T024), — и перечисление здесь превращается в список, который
# однажды забудут пополнить. Забыли ровно так: compose ссылался на
# `queue_healthcheck.py`, а в образ он не попадал, и контейнер очереди был бы
# нездоров всегда.
COPY docker/ ./docker/

# Статика собирается в образ, а не при старте: старт контейнера — не место для
# работы, которая может упасть, и не место, где её кто-то увидит. Отдаёт файлы
# whitenoise прямо из приложения (issue #68); базы этой команде не нужно.
RUN python manage.py collectstatic --noinput --clear

# Не root: приложение ничего не пишет в файловую систему, и повода давать ему
# права на неё нет.
RUN useradd --create-home --uid 10001 app && chown -R app:app /app
USER app

# Внутри контейнера порт всегда 8000. Наружу он публикуется через APP_PORT
# (8000 локально, 8030 на площадке) — см. docker-compose.yml.
EXPOSE 8000

CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]
