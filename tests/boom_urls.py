"""Маршруты продукта плюс два нарочно ломающихся — материал для T099.

Отдельным модулем, потому что 500 и `PermissionDenied` в продукте взять негде:
представления объясняют свои отказы сами, а падать нарочно ради проверки они не
должны. Здесь же проверяется не поломка, а **страница**, которую человек на ней
видит, — и путь до неё должен быть настоящим: маршрутизатор, посредники,
обработчик каркаса, шаблон.

Обработчики импортируются из `config.urls`, а не переписываются: Django ищет их
по имени в модуле корневых маршрутов, и своя копия здесь означала бы, что
проверка смотрит на другой продукт.
"""
from __future__ import annotations

from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.urls import path

from config.urls import handler403, handler404, handler500  # noqa: F401
from config.urls import urlpatterns as product_urls


def boom(request):
    raise RuntimeError("нарочная поломка: текст исключения не для человека")


def forbidden(request):
    raise PermissionDenied("нарочный отказ")


def missing(request):
    raise Http404("строка ведомости не найдена")


urlpatterns = [
    path("boom/", boom),
    path("forbidden/", forbidden),
    path("missing/", missing),
    *product_urls,
]
