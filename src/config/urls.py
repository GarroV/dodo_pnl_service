"""Маршруты проекта. Интерфейс живёт в приложении `web`, табель — в `timesheets`."""
from django.urls import include, path

urlpatterns = [
    path("timesheets/", include("timesheets.urls")),
    path("", include("web.urls")),
]
