"""Маршруты проекта. Интерфейс живёт в приложении `web`, табель — в `timesheets`."""
from django.conf import settings
from django.urls import include, path

urlpatterns = [
    path("timesheets/", include("timesheets.urls")),
]

# Демо-стенд подключается только там, где он включён (T034). Не «страница
# отвечает отказом», а именно отсутствие маршрутов: вход без регистрации,
# который живёт в продукте и лишь притворяется выключенным, — это обходной путь,
# ждущий своей опечатки в переменной окружения.
if settings.DEMO_MODE:
    urlpatterns.append(path("demo/", include("demo.urls")))

# Последним: пустой префикс приложения `web` перехватывает всё остальное.
urlpatterns.append(path("", include("web.urls")))
