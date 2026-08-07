"""Маршруты интерфейса."""
from django.urls import path

from . import views

urlpatterns = [
    path("", views.index, name="index"),
    path("periods/", views.periods, name="periods"),
    path("periods/<uuid:period_id>/", views.period_detail, name="period"),
    # Вход на время стройки. Настоящий появится в блоке auth.
    path("dev/login/", views.dev_login, name="dev-login"),
    path("dev/logout/", views.dev_logout, name="dev-logout"),
]
