"""Маршруты интерфейса."""
from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("periods/", views.periods, name="periods"),
    path("periods/<uuid:period_id>/", views.period_detail, name="period"),
    path("periods/<uuid:period_id>/calculate/", views.period_calculate, name="period-calculate"),
    # Цикл периода: утверждение и откат. Оба только POST — это запись, а не
    # просмотр, и по ссылке из письма или из истории браузера случиться не должны.
    path("periods/<uuid:period_id>/approve/", views.period_approve, name="period-approve"),
    path("periods/<uuid:period_id>/reopen/", views.period_reopen, name="period-reopen"),
    # Вход: логин с паролем, выход, смена своего пароля.
    path("login/", views.login_page, name="login"),
    path("logout/", views.logout_page, name="logout"),
    path("account/password/", views.password_change, name="password-change"),
    # Вход-ярлык на время стройки. Не отдельный способ проверки личности:
    # подставляет пароль учётки сида и идёт тем же путём (см. web/auth.py).
    path("dev/login/", views.dev_login, name="dev-login"),
    path("dev/logout/", views.logout_page, name="dev-logout"),
]
