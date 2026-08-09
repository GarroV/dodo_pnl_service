"""
Адреса демо. Подключаются только при `DEMO_MODE=1` (см. `config/urls.py`).

Вход — GET, и это осознанно. Обычно вход в продукт делается POST'ом, потому что
он меняет состояние; здесь состояние — сессия посетителя демо-стенда, и цена
перехода по ссылке из переписки равна нулю. Требовать форму значило бы отобрать
у демо ровно то, ради чего оно есть: один клик из письма.
"""
from django.urls import path

from . import views

urlpatterns = [
    path("", views.landing, name="demo"),
    path("enter/", views.enter, name="demo-enter"),
    path("enter/<slug:role>/", views.enter, name="demo-enter-as"),
    # Таблица бухгалтера для экрана сверки: собирается из демо-расчёта в момент
    # скачивания, поэтому после сброса демо она та же самая.
    path("accountant-table.xlsx", views.accountant_file, name="demo-accountant-table"),
]
