"""Маршруты проекта. Интерфейс живёт в приложении `web`."""
from django.urls import include, path

urlpatterns = [
    path("", include("web.urls")),
]
