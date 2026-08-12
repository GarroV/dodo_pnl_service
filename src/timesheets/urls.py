"""Маршруты табеля.

Период адресуется id, а не датой: так же, как во всём остальном интерфейсе
(`/periods/<uuid>/`). В контракте блока маршрут записан как `/timesheets/{period}` —
расхождение объяснено в журнале блока.
"""
from django.urls import path

from . import views

urlpatterns = [
    path("<uuid:period_id>/", views.grid, name="timesheets"),
    path("<uuid:period_id>/cell/", views.cell, name="timesheet-cell"),
    # Сдельная величина — свой адрес, а не род ячейки скрытым полем у записи
    # часов (T075): это другая величина с другими правилами.
    path("<uuid:period_id>/piece/", views.piece, name="timesheet-piece"),
    path("<uuid:period_id>/import/", views.import_table, name="timesheet-import"),
    # Закрытие и открытие — разные адреса, а не значение поля в общей форме:
    # два действия с разными последствиями не должны отличаться содержимым
    # скрытого поля (T022).
    path("<uuid:period_id>/close/", views.close, name="timesheet-close"),
    path("<uuid:period_id>/reopen/", views.reopen, name="timesheet-reopen"),
]
