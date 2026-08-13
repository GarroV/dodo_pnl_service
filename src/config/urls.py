"""Маршруты проекта. Интерфейс живёт в приложении `web`, табель — в `timesheets`."""
from django.conf import settings
from django.urls import include, path

urlpatterns = [
    # Переключатель языка — штатный набор Django (`/i18n/setlang/`, T017).
    # Своего не пишем: этот уже умеет и проверять код языка, и не пускать
    # возврат на чужой домен, а переключатель, написанный руками, ошибается
    # ровно в этом втором месте.
    path("i18n/", include("django.conf.urls.i18n")),
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

# Страницы отказов — свои, а не каркаса Django (T099, issue #82). Django ищет
# их по этим именам именно в модуле корневых маршрутов, поэтому они здесь, а не
# в настройках. Разбор, что показывается и что намеренно не показывается, —
# в `web/errors.py`.
handler404 = "web.errors.page_not_found"
handler403 = "web.errors.permission_denied"
handler500 = "web.errors.server_error"
