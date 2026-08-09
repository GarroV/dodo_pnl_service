"""
Настройки Django.

Пока одна среда — локальная разработка и тесты. Всё, что зависит от площадки,
приходит переменными окружения (см. `.env.example`); значений в коде нет.
"""
from __future__ import annotations

import os
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict

# Модуль без единого импорта Django: настройки читаются раньше, чем поднимаются
# приложения, поэтому тащить сюда код входа нельзя.
from web.flags import dev_login_enabled, dev_password

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = os.environ.get("SECRET_KEY", "insecure-dev-key-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.postgres",
    # Отдаёт статику экрана табеля (htmx, стили, островок) под runserver. Файлы
    # лежат внутри пакета `timesheets`, а не в корневом static/: в образ
    # копируется только src/, и корневой каталог до контейнера бы не доехал.
    "django.contrib.staticfiles",
    # Очередь фоновых задач на самом Postgres: свои таблицы заводит миграциями,
    # брокера рядом не требует (T024).
    "django_q",
    "core",
    "web",
    "timesheets",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    # Язык страницы (T017). Место в списке не произвольное: LocaleMiddleware
    # обязан стоять после сессий (язык может лежать в сессии) и до всего, что
    # собирает текст, — а текст у нас собирают представления.
    "django.middleware.locale.LocaleMiddleware",
    # Закреплённый язык, если он задан (UI_LANGUAGE). Стоит ПОСЛЕ
    # LocaleMiddleware намеренно: тот уже выбрал язык по cookie и заголовку
    # браузера, а этот переопределяет его последним словом — иначе демо
    # открывалось бы на языке гостя вопреки правилу «демо всегда английское».
    "web.i18n.ForcedLanguageMiddleware",
    # Последним: транзакцию запроса и контекст пользователя в базе нужно
    # открыть как можно ближе к представлению, но обязательно ДО него.
    "web.dbcontext.DbContextMiddleware",
]

# Сессии — в подписанной cookie, без таблицы в базе. Причина не в удобстве:
# запрос идёт ролью app_user внутри транзакции с контекстом, и запись сессии
# в базу тянула бы за собой права и политики на служебную таблицу.
# Цена решения, которую нужно понимать: серверного списка сессий нет, поэтому
# «разлогинить всех» — это смена SECRET_KEY, а одного человека — смена его
# пароля (Django кладёт в сессию хэш пароля и сверяет его на каждом запросе).
SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"

# Учётка своя (D013): один uuid на человека — он же уходит в контекст базы и
# лежит в memberships.user_id.
AUTH_USER_MODEL = "core.User"
AUTHENTICATION_BACKENDS = ["django.contrib.auth.backends.ModelBackend"]
LOGIN_URL = "/login/"
LOGIN_REDIRECT_URL = "/periods/"

# Требования к паролю — штатные проверки Django, своих нет.
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Вход-ярлык на время стройки: кнопка на странице входа подставляет пароль
# учётки тестового сида и идёт тем же путём, что человек с клавиатурой —
# отдельного способа проверить личность у него нет. По умолчанию живёт только
# при DJANGO_DEBUG=1: на площадке о нём нужно попросить явно (DEV_LOGIN=1).
DEV_LOGIN_ENABLED = dev_login_enabled(os.environ, DEBUG)
DEV_LOGIN_PASSWORD = dev_password(os.environ)

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.contrib.auth.context_processors.auth",
                "django.template.context_processors.request",
                # Кем вошли — видно в шапке на каждой странице.
                "web.principal.principal",
                # Чем переключить язык — тоже на каждой странице (T017).
                "web.i18n.switcher",
            ],
        },
    },
]


def _database_from_url(url: str) -> dict:
    """DSN → настройки Django.

    Разбирает и `postgresql://user:pass@host:port/db`, и форму `key=value`:
    conninfo_to_dict — та же функция, которой пользуется сам драйвер, поэтому
    расхождений между строкой в .env и реальным подключением не будет.
    """
    parts = conninfo_to_dict(url)
    return {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": parts.get("dbname", ""),
        "USER": parts.get("user", ""),
        "PASSWORD": parts.get("password", ""),
        "HOST": parts.get("host", ""),
        "PORT": str(parts.get("port", "")),
        # Транзакция на запрос — не удобство, а условие безопасности: контекст
        # пользователя выставляется через set_config(..., true) и живёт ровно
        # до конца транзакции, поэтому не может протечь между запросами в пуле.
        "ATOMIC_REQUESTS": True,
    }


_DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql:///dodo_pnl")

DATABASES = {
    "default": _database_from_url(_DATABASE_URL),
    # Второе подключение к той же базе — канал прогресса фонового расчёта (T024).
    #
    # Зачем оно вообще: расчёт периода идёт одной транзакцией, а незакоммиченная
    # транзакция снаружи не видна. Прогресс, записанный внутри неё, появился бы
    # на экране ровно в тот момент, когда он уже не нужен. Автономных транзакций
    # в Postgres нет, поэтому отметки о ходе работы пишутся по отдельному
    # соединению своими короткими транзакциями.
    #
    # Отсюда правило, которое нельзя нарушать: транзакция расчёта не трогает
    # `payrun_jobs`. Иначе она заблокировала бы строку, канал прогресса встал бы
    # на этой блокировке, и снаружи это выглядело бы как зависший расчёт.
    #
    # Миграции сюда не едут: `migrate` работает с `default`, а база одна и та же.
    "progress": {
        **_database_from_url(_DATABASE_URL),
        # Транзакциями здесь управляет код прогресса, а не каркас: каждая отметка
        # обязана коммитнуться сама по себе.
        "ATOMIC_REQUESTS": False,
    },
}

# --- фоновый расчёт периода (T024) -------------------------------------------
# Выключатель, а не «как получится»: с PAYRUN_BACKGROUND=0 расчёт синхронный и
# страница не обещает прогресса. Молчаливой подмены одного другим нет ни в одну
# сторону — см. журнал блока payrun.
PAYRUN_BACKGROUND = os.environ.get("PAYRUN_BACKGROUND", "1") == "1"

# Сколько секунд задача может простоять в очереди, прежде чем страница скажет
# человеку, что рабочий процесс очереди, похоже, не запущен. Живой кластер
# забирает задачу за доли секунды (опрос очереди — 0,2 с), поэтому порог с
# большим запасом.
PAYRUN_QUEUE_STALE_SECONDS = int(os.environ.get("PAYRUN_QUEUE_STALE_SECONDS", "10"))

Q_CLUSTER = {
    "name": "dodo-pnl",
    # Брокер — сама база: очередь живёт в тех же транзакциях, что данные.
    # Поэтому задача становится видимой рабочему процессу ровно тогда, когда
    # коммитится запрос, который её поставил, — и не раньше.
    "orm": "default",
    "workers": int(os.environ.get("PAYRUN_QUEUE_WORKERS", "2")),
    "timeout": int(os.environ.get("PAYRUN_QUEUE_TIMEOUT", "600")),
    # retry обязан быть больше timeout: иначе очередь считает задачу потерянной
    # раньше, чем рабочий процесс успевает её закончить, и выдаёт второму.
    "retry": int(os.environ.get("PAYRUN_QUEUE_TIMEOUT", "600")) + 120,
    # Повтор упавшей задачи запрещён. Расчёт периода — не то, что можно молча
    # переиграть: причина отказа записана в задании и показана человеку, решение
    # о повторе принимает он.
    "max_attempts": 1,
    "ack_failures": True,
    # Расписаний в продукте нет: задача ставится только нажатием кнопки.
    "scheduler": False,
    "save_limit": 100,
    "label": "Фоновые задачи",
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# --- языки интерфейса (T017, решение D005) -----------------------------------
# Русский — язык исходника: строки в коде и шаблонах написаны по-русски, и они
# же служат ключами перевода. Отдельного каталога `ru` поэтому нет и не нужно:
# без перевода gettext отдаёт сам ключ, то есть русский текст.
LANGUAGE_CODE = "ru"

# Сербский — латиницей (`sr-latn`), а не кириллицей. Причина предметная: сама
# зарплатная таблица партнёра написана латиницей («IME I PREZIME», «KOREKCIJA
# DO MINIMALCA»), и интерфейс не должен разговаривать с бухгалтером не тем
# письмом, каким написаны его же документы.
LANGUAGES = [
    ("ru", "Русский"),
    ("en", "English"),
    ("sr-latn", "Srpski"),
]

# Один каталог на весь проект, а не по каталогу на приложение: строки одного
# экрана приходят из `web`, `timesheets`, `payrun` и `reports` сразу, и
# раскладывать перевод одной страницы по четырём файлам значило бы гарантировать
# расхождение. Путь внутри src/: в образ копируется именно он (см. Dockerfile).
LOCALE_PATHS = [BASE_DIR / "src" / "locale"]

# Закрепить язык независимо от выбора человека. Пусто — язык выбирает человек.
#
# Ради демо (конституция: «язык демо — всегда английский, независимо от языка
# интерфейса продукта»). Демо-стенд поднимается с UI_LANGUAGE=en, и тогда
# переключателя языка на страницах нет вовсе: показывать выключенную ручку
# хуже, чем не показывать никакой.
UI_LANGUAGE = os.environ.get("UI_LANGUAGE", "").strip()

TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
