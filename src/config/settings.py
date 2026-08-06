"""
Настройки Django.

Пока одна среда — локальная разработка и тесты. Всё, что зависит от площадки,
приходит переменными окружения (см. `.env.example`); значений в коде нет.
"""
from __future__ import annotations

import os
from pathlib import Path

from psycopg.conninfo import conninfo_to_dict

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SECRET_KEY = os.environ.get("SECRET_KEY", "insecure-dev-key-change-me")
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"
ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "django.contrib.sessions",
    "django.contrib.postgres",
    "core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
]

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


DATABASES = {
    "default": _database_from_url(
        os.environ.get("DATABASE_URL", "postgresql:///dodo_pnl")
    )
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LANGUAGE_CODE = "ru"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
