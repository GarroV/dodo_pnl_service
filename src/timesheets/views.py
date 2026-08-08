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

from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, HttpResponseNotFound
from django.shortcuts import render
from django.views.decorators.http import require_POST

from payrun.errors import PayrunRefused
from web import permissions
from web.principal import get_current_principal
from web.views import find_period, month_title

from .grid import build_grid, visible_rows
from .importer import import_partner_table
from .store import CellRefused, parse_hours, set_cell

__all__ = ["cell", "grid", "import_table"]

# Больше этого файл зарплатной таблицы не бывает: восемь листов на несколько
# сотен человек — это сотни килобайт. Ограничение не про безопасность, а про
# внятный отказ вместо съеденной памяти на случайно выбранном видеофайле.
MAX_UPLOAD = 20 * 1024 * 1024

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


def _refused(status: int, text: str, row, hour_type: str) -> HttpResponse:
    """Отказ на запись ячейки — одним и тем же способом, каким бы он ни был.

    Три вещи, которые обязан получить экран: код ответа, текст для человека и
    значение, оставшееся **в базе**. Последнее — не мелочь: без него в поле
    оставался бы непринятый ввод, и человек читал бы отказ, глядя на число,
    которого в базе нет (T066). Здесь это одно место на все причины отказа,
    чтобы новая причина не завела себе третий способ объясняться.
    """
    response = HttpResponse(
        text, status=status, content_type="text/plain; charset=utf-8"
    )
    response["X-Cell-Value"] = f"{Decimal(str((row.hours or {}).get(hour_type, 0))):.2f}"
    return response


@login_required
def grid(request, period_id):
    period = find_period(period_id)
    who = _context(request, period)

    # Колонки сетки — это типы часов страны, то есть те же правила, на которых
    # стоит расчёт. Нет правил на месяц — сетку строить не из чего, и человек
    # должен прочитать то же объяснение, что на странице периода, а не «Server
    # Error»: при выключенной отладке других слов он не увидит.
    table, refusal = None, None
    try:
        table = build_grid(period.tenant_id, period.period, unit_ids=who.unit_ids)
    except PayrunRefused as denied:
        refusal = denied

    # Ячейки — поля ввода только у того, кому правка табеля разрешена (T072).
    # Роль без права раньше получала ту же редактируемую сетку 35×6 и узнавала
    # о запрете, лишь покинув ячейку: значение уходило на сервер и возвращалось
    # отказом. Проверку в `cell` это не отменяет — она ниже и остаётся.
    denied = permissions.explain(who, permissions.TIMESHEET_EDIT)

    return render(
        request,
        "timesheets/grid.html",
        {
            "period": period,
            "title": month_title(period.period),
            "grid": table,
            "error": refusal.message if refusal else None,
            "details": refusal.details if refusal else [],
            # Управляющему честно говорим, что он видит срез, а не весь табель.
            "limited_to_units": bool(who.unit_ids),
            "can_edit": not denied,
            "edit_denied": denied,
        },
        status=refusal.http_status if refusal else 200,
    )


@login_required
@require_POST
def cell(request, period_id):
    period = find_period(period_id)
    who = _context(request, period)

    # Право править табель — не то же самое, что видеть его. Без этой проверки
    # запись всё равно не прошла бы (её режет политика базы), но человек получил
    # бы ошибку сервера вместо объяснения (T064).
    try:
        permissions.check(who, permissions.TIMESHEET_EDIT)
    except permissions.PermissionRefused as refusal:
        return HttpResponse(
            refusal.message,
            status=refusal.http_status,
            content_type="text/plain; charset=utf-8",
        )

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
        table = build_grid(period.tenant_id, period.period, unit_ids=who.unit_ids)
    except CellRefused as refusal:
        return _refused(REFUSED, str(refusal), row, hour_type)
    except PayrunRefused as refusal:
        # Колонки сетки — типы часов страны, то есть те же правила, на которых
        # стоит расчёт. Страница могла открыться, когда они ещё действовали, а
        # ячейка уходит на сервер уже после того, как перестали (T073). Без этой
        # ветки человек получал 500 — «Server Error» ровно тому, кто набирал
        # часы, — вместо того же объяснения, что даёт страница с T062.
        return _refused(refusal.http_status, refusal.message, row, hour_type)

    changed = next((item for item in table.rows if item.timesheet_id == row.id), None)
    response = render(
        request,
        "timesheets/_totals.html",
        {
            "grid": table,
            "row_id": str(row.id),
            "row_total": changed.total if changed else 0,
            # Строка целиком нужна ответу ради базы для взносов: правка часов
            # могла её сдвинуть (см. `_insured_cell.html`).
            "row": changed,
        },
    )
    # Значение в том виде, в каком оно легло в базу: «8,5» и «8.50» — одно и то
    # же число, но человек должен увидеть, что именно сохранилось.
    response["X-Cell-Value"] = f"{hours:.2f}"
    return response


@login_required
@require_POST
def import_table(request, period_id):
    """Загрузка таблицы партнёра и отчёт о ней (T020, T021).

    Отдельная страница, а не фрагмент на сетке: отчёт бывает длинным (десятки
    находок), и показывать его в углу экрана, поверх 35 строк, значило бы
    предлагать человеку читать самое важное в самом неудобном месте.

    Загрузка — то же право, что правка ячейки: она пишет в тот же табель.
    Отдельное право завело бы вторую правду об одном действии.
    """
    period = find_period(period_id)
    who = _context(request, period)

    try:
        permissions.check(who, permissions.TIMESHEET_EDIT)
    except permissions.PermissionRefused as refusal:
        return _report(request, period, error=refusal.message,
                       status=refusal.http_status)

    upload = request.FILES.get("table")
    if upload is None:
        return _report(request, period, error="Файл не выбран.", status=400)
    if upload.size > MAX_UPLOAD:
        return _report(
            request, period, status=400,
            error=f"Файл больше {MAX_UPLOAD // 1024 // 1024} МБ — "
                  f"это не зарплатная таблица.",
        )

    try:
        result = import_partner_table(
            upload, tenant_id=period.tenant_id, period=period.period,
            actor_id=who.user_id,
        )
    except PayrunRefused as refusal:
        # Тот же отказ и теми же словами, что на сетке и на странице периода
        # (T062, T073): типы часов — это правила страны, и без них загружать
        # часы некуда.
        return _report(request, period, error=refusal.message,
                       details=refusal.details, status=refusal.http_status)
    except CellRefused as refusal:
        return _report(request, period, error=str(refusal), status=REFUSED)
    except Exception as broken:  # noqa: BLE001 — намеренно широко, см. ниже
        # Чужой файл может быть чем угодно: не тем форматом, битым архивом,
        # защищённой книгой. Разбирать исключения openpyxl по одному значило бы
        # гадать за библиотеку; а вот молчаливая 500-я здесь недопустима —
        # человек должен прочитать, что файл не разобран, и каким он был.
        return _report(
            request, period, status=REFUSED,
            error=f"Файл не удалось прочитать как книгу Excel: {broken}",
        )

    return _report(request, period, result=result)


def _report(request, period, *, result=None, error=None, details=(), status=200):
    return render(
        request,
        "timesheets/import_report.html",
        {
            "period": period,
            "title": month_title(period.period),
            "result": result,
            "error": error,
            "details": list(details),
        },
        status=status,
    )
