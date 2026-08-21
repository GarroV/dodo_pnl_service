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

# Источники, которым Django верит при проверке CSRF на POST по HTTPS (T161).
# Понадобилось, когда стенд начал открываться наружу постоянным адресом: за
# обратным прокси Django сверяет заголовок Origin со списком, и без него вход
# на публичном адресе отвечал бы 403 на верный пароль — то есть выглядел бы
# как сломанный продукт, а не как настройка.
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]

# Прокси стоит впереди и заканчивает TLS на себе. Без этой пары Django считает
# запрос небезопасным (http), и штука, которая ломается первой, — это как раз
# CSRF на входе.
if os.environ.get("BEHIND_TLS_PROXY") == "1":
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    USE_X_FORWARDED_HOST = True

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
    # Демо-стенд: свои команды наполнения и сброса и своя точка входа. Само
    # приложение безвредно в продукте — маршруты демо подключаются только при
    # DEMO_MODE=1 (см. config/urls.py), а команды отказываются работать где-либо,
    # кроме демо-базы (см. demo/guard.py).
    "demo",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Продукт партнёра не место для поисковика: индексировать его незачем ни в
    # каком развёртывании, а публичный адрес стенда делает это не теорией
    # (T161). Заголовком, а не файлом robots.txt: robots — просьба к обходчику
    # не заходить, X-Robots-Tag — запрет показывать в выдаче то, что уже
    # получили.
    "web.middleware.NoIndex",
    # Статика отдаётся тут же, приложением, а не отдельным веб-сервером
    # (issue #68; почему именно так — у STATIC_ROOT ниже). Место в списке не
    # произвольное: сразу после SecurityMiddleware, как требует whitenoise, —
    # тогда файл уходит клиенту, не проходя через сессии, язык и права.
    "whitenoise.middleware.WhiteNoiseMiddleware",
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
    # Человеческая страница на 404 и при включённой отладке тоже (T099).
    # Стоит ПОСЛЕ DbContextMiddleware намеренно, то есть внутри его транзакции:
    # страница отказа тянет шапку, а шапка спрашивает базу, кем вошли, — и
    # спрашивать обязана под контекстом пользователя, как весь остальной
    # запрос. Снаружи транзакции этот вопрос ушёл бы мимо политик.
    "web.errors.HumanErrorPagesMiddleware",
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

# --- демо-стенд (T033, T034) --------------------------------------------------
# Живое демо — отдельный экземпляр со своей базой (D016), а не режим продукта.
# Выключено по умолчанию, и выключено означает «маршрутов нет»: в продукте
# `demo.urls` не подключается вовсе (см. config/urls.py). Функциональность,
# которая «выключена», но всё ещё отвечает, однажды ответит не то.
DEMO_MODE = os.environ.get("DEMO_MODE", "0") == "1"
# Лёгкий спидбамп на входе: спасает от прохожего и поисковика, а не от
# злоумышленника. Секретом не является — в демо-базе только выдуманные люди.
DEMO_KEY = os.environ.get("DEMO_KEY", "")
# Пароль демо-учёток. Той же природы, что пароль базы в .env.example: нужен не
# для тайны, а чтобы его можно было сменить, не пересобирая образ.
DEMO_USER_PASSWORD = os.environ.get("DEMO_USER_PASSWORD", "demo-only-not-a-secret")

ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"

# Форма без действительного ключа страницы — тоже отказ, который читает человек
# (T099). Штатная страница Django здесь техническая и объясняет не то, о чём он
# спрашивал; своя говорит, что делать. Разбор — в `web/errors.py`.
CSRF_FAILURE_VIEW = "web.errors.csrf_failure"

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

# --- Кто отдаёт статику (issue #68) -----------------------------------------
# Штатный `runserver` отдаёт статику только при DEBUG=1. На площадке стоит
# DJANGO_DEBUG=0, и до этой правки экран табеля там оставался без htmx, grid.js
# и grid.css: страница открывалась, сетка рисовалась, ячейка на сервер не
# уходила — человек вводил часы, видел числа и уходил, ничего не сохранив.
# Проверено на раскатанном стенде: все три файла отдавали 404.
#
# Отдаёт их whitenoise — тем же процессом приложения, без отдельного веб-сервера:
# файлов три, отдельный nginx рядом стоил бы больше, чем весит проблема.
STATIC_ROOT = BASE_DIR / "staticfiles"

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    # Сжатие есть, хешей в именах нет намеренно. Хеши (Manifest…Storage) требуют
    # обязательного `collectstatic` перед каждым запуском: без собранного
    # манифеста падает сам тег `{% static %}`, то есть страница целиком. Менять
    # молчаливую поломку одной ячейки на громкую поломку всех страниц смысла нет
    # — файлов три, и меняются они раз в полгода.
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

# Отдавать и то, что лежит в пакетах, а не только собранное в STATIC_ROOT.
# Это страховка ровно от того случая, который и породил issue #68: забытый
# `collectstatic` снова оставил бы табель без скриптов, и снова молча.
WHITENOISE_USE_FINDERS = True
