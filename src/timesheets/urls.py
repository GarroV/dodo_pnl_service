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
    path("<uuid:period_id>/import/", views.import_table, name="timesheet-import"),
]
