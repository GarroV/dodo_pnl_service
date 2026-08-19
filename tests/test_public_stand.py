"""Что обязано быть верным на публично доступном стенде (T161, D045).

Стенд открывается наружу постоянным адресом, потому что владельцу нужно
показывать продукт (D045). Публичный адрес меняет цену трёх настроек, каждая из
которых до сих пор была теоретической:

1. **Вход по кнопке.** `DEV_LOGIN` при `DEBUG=0` обязан быть выключен: на
   публичном адресе вход-ярлык отдаёт продукт кому угодно.
2. **CSRF на HTTPS.** Django сверяет `Origin` со списком доверенных источников,
   и без него верный пароль на публичном адресе даёт 403 — то есть выглядит как
   сломанный продукт, а не как забытая настройка.
3. **Поисковая выдача.** Страницы с фамилиями и суммами не должны попадать в
   поиск ни на стенде, ни у партнёра.

Проверки идут по настройкам и по живому ответу, а не по документации: описание
площадки в `docs/` от кода не зависит и разъезжается молча.
"""
from __future__ import annotations

import importlib

from django.conf import settings
from django.test import Client

from web.flags import dev_login_enabled


def test_the_shortcut_login_is_off_unless_asked_for_explicitly():
    """`DEBUG=0` и переменная не выставлена — входа по кнопке нет."""
    assert dev_login_enabled({}, False) is False
    assert dev_login_enabled({"DEV_LOGIN": "0"}, True) is False
    # Явное «включить» сильнее умолчания — это тоже часть договора, иначе
    # разработчик не смог бы включить ярлык у себя.
    assert dev_login_enabled({"DEV_LOGIN": "1"}, False) is True


def test_trusted_origins_come_from_the_environment(monkeypatch):
    """Список доверенных источников читается из окружения, а не зашит в код.

    Зашитый адрес стенда означал бы, что следующее развёртывание тащит чужой
    домен и молча не работает на своём.
    """
    monkeypatch.setenv(
        "CSRF_TRUSTED_ORIGINS", "https://muspelheim.example.ts.net, https://second.example",
    )
    module = importlib.import_module("config.settings")
    reloaded = importlib.reload(module)
    assert reloaded.CSRF_TRUSTED_ORIGINS == [
        "https://muspelheim.example.ts.net",
        "https://second.example",
    ]

    monkeypatch.delenv("CSRF_TRUSTED_ORIGINS")
    importlib.reload(module)


def test_the_proxy_header_is_trusted_only_when_asked(monkeypatch):
    """Заголовок прокси о HTTPS принимается только по явному разрешению.

    Верить `X-Forwarded-Proto` без прокси впереди — это позволить любому
    клиенту объявить своё соединение защищённым.
    """
    module = importlib.import_module("config.settings")
    monkeypatch.delenv("BEHIND_TLS_PROXY", raising=False)
    plain = importlib.reload(module)
    assert getattr(plain, "SECURE_PROXY_SSL_HEADER", None) is None

    monkeypatch.setenv("BEHIND_TLS_PROXY", "1")
    behind = importlib.reload(module)
    assert behind.SECURE_PROXY_SSL_HEADER == ("HTTP_X_FORWARDED_PROTO", "https")

    monkeypatch.delenv("BEHIND_TLS_PROXY")
    importlib.reload(module)


def test_every_page_refuses_the_search_engines(web_env):
    """Заголовок стоит на каждом ответе, включая страницу входа."""
    client = Client()
    for url in ("/login/", "/periods/"):
        response = client.get(url)
        assert response.headers.get("X-Robots-Tag") == "noindex, nofollow", (
            f"{url}: страница попадёт в поисковую выдачу"
        )


def test_the_middleware_is_wired_before_anything_can_answer():
    """Заголовок ставит middleware, а не каждое представление по отдельности.

    Представление, забывшее заголовок, — это ровно та молчаливая дыра, ради
    которой middleware и существует.
    """
    assert "web.middleware.NoIndex" in settings.MIDDLEWARE
