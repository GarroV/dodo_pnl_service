"""Маршруты проекта. Интерфейс живёт в приложении `web`, табель — в `timesheets`."""
from django.urls import include, path

urlpatterns = [
    # Переключатель языка — штатный набор Django (`/i18n/setlang/`, T017).
    # Своего не пишем: этот уже умеет и проверять код языка, и не пускать
    # возврат на чужой домен, а переключатель, написанный руками, ошибается
    # ровно в этом втором месте.
    path("i18n/", include("django.conf.urls.i18n")),
    path("timesheets/", include("timesheets.urls")),
    path("", include("web.urls")),
]
