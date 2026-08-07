"""
Экран табеля: сетка и запись одной ячейки.

Представления не фильтруют по тенанту — это работа политик базы. Единственный
доменный фильтр здесь по правам — точки: у управляющего непустой `unit_ids`, и
чужие строки он не должен ни видеть, ни править (см. журнал блока, решение 8).

Ответ на запись ячейки нарочно не содержит самой ячейки: подменять поле, в
котором может стоять курсор, — верный способ сломать ввод с клавиатуры. Наружу
уходят только пересчитанные итоги (htmx-подмена вне цели) и записанное значение
заголовком, чтобы страница показала его в том же виде, в каком оно легло в базу.
"""
from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, HttpResponseNotFound
from django.shortcuts import render
from django.views.decorators.http import require_POST

from web.principal import get_current_principal
from web.views import find_period, month_title

from .grid import build_grid, visible_rows
from .store import CellRefused, parse_hours, set_cell

__all__ = ["cell", "grid"]

# Отказ на запись ячейки: 422, а не 400. Запрос разобран и понят, не принято
# именно значение — и htmx на странице отличает эти случаи по коду.
REFUSED = 422


def _context(request, period):
    who = get_current_principal(request)
    if who is None or who.tenant_id is None:
        # Вошёл, но ни к какому партнёру не приписан: показывать нечего, и
        # отличать этот случай от «периода нет» снаружи незачем.
        raise Http404("период не найден")
    return who


@login_required
def grid(request, period_id):
    period = find_period(period_id)
    who = _context(request, period)
    table = build_grid(period.tenant_id, period.period, unit_ids=who.unit_ids)
    return render(
        request,
        "timesheets/grid.html",
        {
            "period": period,
            "title": month_title(period.period),
            "grid": table,
            # Управляющему честно говорим, что он видит срез, а не весь табель.
            "limited_to_units": bool(who.unit_ids),
        },
    )


@login_required
@require_POST
def cell(request, period_id):
    period = find_period(period_id)
    who = _context(request, period)

    row = (
        visible_rows(period.tenant_id, period.period, who.unit_ids)
        .filter(pk=request.POST.get("row") or None)
        .first()
    )
    if row is None:
        # Чужая точка и несуществующая строка выглядят одинаково: по ответу
        # нельзя понять, что такой сотрудник вообще есть.
        return HttpResponseNotFound("строка табеля не найдена")

    hour_type = request.POST.get("kind") or ""
    try:
        hours = parse_hours(request.POST.get("hours", ""))
        set_cell(timesheet=row, hour_type=hour_type, hours=hours)
    except CellRefused as refusal:
        return HttpResponse(str(refusal), status=REFUSED, content_type="text/plain; charset=utf-8")

    table = build_grid(period.tenant_id, period.period, unit_ids=who.unit_ids)
    changed = next((item for item in table.rows if item.timesheet_id == row.id), None)
    response = render(
        request,
        "timesheets/_totals.html",
        {"grid": table, "row_id": str(row.id), "row_total": changed.total if changed else 0},
    )
    # Значение в том виде, в каком оно легло в базу: «8,5» и «8.50» — одно и то
    # же число, но человек должен увидеть, что именно сохранилось.
    response["X-Cell-Value"] = f"{hours:.2f}"
    return response
