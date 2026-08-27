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
from django.shortcuts import redirect, render
from django.utils.translation import gettext as _
from django.views.decorators.http import require_POST

from payrun.errors import PayrunRefused
from web import permissions
from web.principal import get_current_principal
from web.views import find_period, month_title

from .closing import (
    ClosureRefused,
    close_unit,
    refuse_if_closed,
    reopen_unit,
    unit_states,
)
from .grid import build_grid, visible_rows
from .importer import import_partner_table
from .store import (
    CellRefused,
    ensure_row,
    parse_hours,
    parse_insured,
    parse_piece,
    set_cell,
    set_insured,
    set_piece,
)

__all__ = ["cell", "close", "grid", "import_table", "insured", "piece", "reopen"]

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


def _refused(status: int, text: str, stored) -> HttpResponse:
    """Отказ на запись ячейки — одним и тем же способом, каким бы он ни был.

    Три вещи, которые обязан получить экран: код ответа, текст для человека и
    значение, оставшееся **в базе**. Последнее — не мелочь: без него в поле
    оставался бы непринятый ввод, и человек читал бы отказ, глядя на число,
    которого в базе нет (T066). Здесь это одно место на все причины отказа,
    чтобы новая причина не завела себе третий способ объясняться.

    Значение передаётся готовым, а не выковыривается отсюда из строки табеля:
    ячеек в сетке два рода — часы и сдельная величина (T075), — и лезть за
    каждым в своё поле означало бы ветку на род ячейки внутри общего отказа.
    """
    response = HttpResponse(
        text, status=status, content_type="text/plain; charset=utf-8"
    )
    response["X-Cell-Value"] = f"{Decimal(str(stored or 0)):.2f}"
    return response


def _hours_of(row, hour_type: str):
    return (row.hours or {}).get(hour_type, 0)


@login_required
def grid(request, period_id):
    period = find_period(period_id)
    who = _context(request, period)
    return _grid_page(request, period, who)


def _grid_page(request, period, who, *, notice: str = "", status: int = 200):
    """Страница табеля. Одна на все случаи: и обычный показ, и отказ на действии.

    Отказ показывается на самом табеле, а не отдельной страницей: человек нажал
    кнопку здесь и должен увидеть ответ здесь же, рядом с точками, о которых
    идёт речь.
    """
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
    # То же самое для закрытия часов: кнопки нет — сказано, почему её нет.
    # Экран, который молча теряет действие, читается как поломка, а не как
    # запрет (T064).
    close_denied = permissions.explain(who, permissions.UNIT_CLOSE)

    return render(
        request,
        "timesheets/grid.html",
        {
            "period": period,
            "title": month_title(period.period),
            "grid": table,
            "error": refusal.message if refusal else None,
            "details": refusal.details if refusal else [],
            "notice": notice,
            # Управляющему честно говорим, что он видит срез, а не весь табель.
            "limited_to_units": bool(who.unit_ids),
            "can_edit": not denied,
            "edit_denied": denied,
            "can_close": not close_denied,
            "close_denied": close_denied,
            "units": (
                unit_states(period.tenant_id, period.period, table.rows)
                if table else []
            ),
        },
        status=refusal.http_status if refusal else status,
    )


@login_required
@require_POST
def _row_of(request, period, who):
    """Строка табеля, в которую пишет ячейка. Заводит её, если ещё нет.

    Одно место на все три ячейки — часы, база взносов, сдельная величина, —
    потому что правило одно: пишем в строку этого месяца, а её может не быть.
    Три копии этого поиска разъехались бы ровно на новом пути: часы заводили бы
    строку, а сдельная величина отвечала бы «не найдено» тому же курьеру.
    """
    row = (
        visible_rows(period.tenant_id, period.period, who.unit_ids)
        .filter(pk=request.POST.get("row") or None)
        .first()
    )
    if row is not None or not request.POST.get("employee"):
        return row
    # Строки нет, потому что человека завели с экрана посреди месяца (issue
    # #152). Заводим её здесь — на правке, а не на открытии страницы, чтобы
    # чтение оставалось чтением.
    return _new_row(request, period, who)


def _new_row(request, period, who):
    """Строка табеля для человека, которого в табеле ещё нет.

    Заводится только тому, у кого **действуют условия найма** в этом месяце:
    без них расчёт всё равно отвергнет строку по имени, а экран обещал бы
    ввод, который никуда не приедет. Точка берётся оттуда же — из условий, а не
    из запроса: иначе часы можно было бы вписать в чужую точку, назвав её в
    форме.
    """
    from payrun.calc import terms_in_force

    term = terms_in_force(period.tenant_id, period.period).get(
        _uuid_or_none(request.POST.get("employee"))
    )
    if term is None:
        return None
    # Чужая точка — то же правило, что у видимых строк: заведение не должно
    # быть обходным путём к точке, которую человеку не показывают.
    if who.unit_ids and term.unit_id not in set(who.unit_ids):
        return None
    return ensure_row(
        tenant_id=period.tenant_id, employee_id=term.employee_id,
        period=period.period, unit_id=term.unit_id,
    )


def _uuid_or_none(value):
    from uuid import UUID as _UUID

    try:
        return _UUID(str(value))
    except (TypeError, ValueError):
        return None


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

    try:
        row = _row_of(request, period, who)
    except CellRefused as refusal:
        # Строку завести нечем — например, на месяц нет производственного
        # календаря, и норму часов взять неоткуда. Человек читает причину.
        return HttpResponse(
            str(refusal), status=422, content_type="text/plain; charset=utf-8",
        )
    if row is None:
        # Чужая точка и несуществующая строка выглядят одинаково: по ответу
        # нельзя понять, что такой сотрудник вообще есть.
        return HttpResponseNotFound("строка табеля не найдена")

    hour_type = request.POST.get("kind") or ""
    try:
        hours = parse_hours(request.POST.get("hours", ""))
        # Часы закрытой точки не пишутся. Запись всё равно не прошла бы — её
        # режет политика базы (T022), — но человек получил бы ошибку сервера
        # вместо объяснения. Проверка стоит именно здесь, а не только при
        # построении страницы: точку могли закрыть после того, как страница
        # открылась, и ячейка уходит на сервер уже в закрытый табель.
        refuse_if_closed(row)
        # Автор правки записывается вместе с числом (T143, issue #52): вопрос
        # «кто поставил 176» задают тогда, когда числа разошлись, и ответить на
        # него задним числом уже нечем.
        set_cell(timesheet=row, hour_type=hour_type, hours=hours,
                 actor_id=who.user_id)
        table = build_grid(period.tenant_id, period.period, unit_ids=who.unit_ids)
    except CellRefused as refusal:
        return _refused(REFUSED, str(refusal), _hours_of(row, hour_type))
    except ClosureRefused as refusal:
        # Тем же способом, что и остальные отказы на записи ячейки: код,
        # текст для человека и значение, оставшееся в базе (T066, T073).
        return _refused(refusal.http_status, refusal.message, _hours_of(row, hour_type))
    except PayrunRefused as refusal:
        # Колонки сетки — типы часов страны, то есть те же правила, на которых
        # стоит расчёт. Страница могла открыться, когда они ещё действовали, а
        # ячейка уходит на сервер уже после того, как перестали (T073). Без этой
        # ветки человек получал 500 — «Server Error» ровно тому, кто набирал
        # часы, — вместо того же объяснения, что даёт страница с T062.
        return _refused(refusal.http_status, refusal.message, _hours_of(row, hour_type))

    changed = next((item for item in table.rows if item.timesheet_id == row.id), None)
    response = render(
        request,
        "timesheets/_totals.html",
        {
            "grid": table,
            # Период нужен колонке базы для взносов: у её поля свой адрес, и
            # собирается он от периода (`{% url 'timesheet-insured' %}`).
            "period": period,
            # Право правки сюда доехало проверенным выше: без него этот запрос
            # не дошёл бы досюда. Передаётся оно потому, что колонка базы для
            # взносов — поле ввода у того, кто правит, и число у остальных
            # (T143). Без него подмена вне цели заменила бы поле на число, и
            # база переставала бы правиться после первой же правки часов.
            "can_edit": True,
            # Строка целиком, а не её итог числом: ответу нужны и база для
            # взносов (`_insured_cell.html`), и подсказка о подозрительных
            # числах (`_row_total.html`, T118) — обе считаются по строке.
            # Строка тут всегда есть: её только что вернул тот же `visible_rows`,
            # из которого построена сетка.
            "row": changed,
        },
    )
    # Значение в том виде, в каком оно легло в базу: «8,5» и «8.50» — одно и то
    # же число, но человек должен увидеть, что именно сохранилось.
    response["X-Cell-Value"] = f"{hours:.2f}"
    return response


@login_required
@require_POST
def piece(request, period_id):
    """Записать сдельную величину строки табеля (T075).

    Свой адрес, а не значение поля `kind` у записи часов. Причина та же, по
    которой закрытие и открытие точки — разные адреса: это разные величины с
    разными правилами. Часы проверяются по типам часов страны и раскладываются
    по дням; сдельная величина — ни то, ни другое, и одна общая ручка означала
    бы, что род ячейки решает скрытое поле.

    Отвечает пустым телом: пересчитывать на сетке нечего. Итог строки и итоги
    колонок — это часы, а сдельную величину не суммируют по столбцу вовсе:
    у одного партнёра рядом могут стоять доставки и фиксированные суммы, и их
    сумма не означала бы ничего.
    """
    period = find_period(period_id)
    who = _context(request, period)

    try:
        permissions.check(who, permissions.TIMESHEET_EDIT)
    except permissions.PermissionRefused as refusal:
        return HttpResponse(
            refusal.message,
            status=refusal.http_status,
            content_type="text/plain; charset=utf-8",
        )

    try:
        row = _row_of(request, period, who)
    except CellRefused as refusal:
        return HttpResponse(
            str(refusal), status=422, content_type="text/plain; charset=utf-8",
        )
    if row is None:
        return HttpResponseNotFound("строка табеля не найдена")

    try:
        value = parse_piece(request.POST.get("piece", ""))
        # Тот же запрет, что у часов: закрытую точку не правят. Проверка стоит
        # здесь, а не только при построении страницы, — точку могли закрыть уже
        # после того, как страница открылась.
        refuse_if_closed(row)
        set_piece(timesheet=row, value=value, actor_id=who.user_id)
    except CellRefused as refusal:
        return _refused(REFUSED, str(refusal), row.piece_value)
    except ClosureRefused as refusal:
        return _refused(refusal.http_status, refusal.message, row.piece_value)

    response = HttpResponse("", content_type="text/plain; charset=utf-8")
    response["X-Cell-Value"] = f"{value:.2f}"
    return response


@login_required
@require_POST
def insured(request, period_id):
    """Записать базу для взносов строки табеля (T143, issue #54).

    Свой адрес, как у сдельной величины и по той же причине: это не тип часов, и
    род ячейки не должен решаться скрытым полем.

    Отвечает **колонкой базы целиком** — тем же фрагментом, каким её присылает
    ответ на правку часов. Причина: у колонки есть не только число, но и
    пометка расхождения с часами, а она — единственное, что говорит человеку,
    почему период не считается. Оставить её прежней значило бы показывать не то,
    что в базе. Поле под курсором при этом не вырывается: подмена вне цели
    отменяется скриптом, если в заменяемом месте стоит курсор (`grid.js`).
    """
    period = find_period(period_id)
    who = _context(request, period)

    try:
        permissions.check(who, permissions.TIMESHEET_EDIT)
    except permissions.PermissionRefused as refusal:
        return HttpResponse(
            refusal.message,
            status=refusal.http_status,
            content_type="text/plain; charset=utf-8",
        )

    try:
        row = _row_of(request, period, who)
    except CellRefused as refusal:
        return HttpResponse(
            str(refusal), status=422, content_type="text/plain; charset=utf-8",
        )
    if row is None:
        return HttpResponseNotFound("строка табеля не найдена")

    try:
        value = parse_insured(request.POST.get("insured", ""))
        # Тот же запрет, что у часов: закрытую точку не правят. База для взносов
        # входит в расчёт наравне с ними, и оставить её правимой после закрытия
        # значило бы закрывать часы, но не деньги.
        refuse_if_closed(row)
        set_insured(timesheet=row, value=value, actor_id=who.user_id)
        table = build_grid(period.tenant_id, period.period, unit_ids=who.unit_ids)
    except CellRefused as refusal:
        return _refused(REFUSED, str(refusal), row.insured_hours)
    except ClosureRefused as refusal:
        return _refused(refusal.http_status, refusal.message, row.insured_hours)
    except PayrunRefused as refusal:
        # Та же ветка и по той же причине, что у записи часов: колонки сетки —
        # правила страны, и они могли перестать действовать после того, как
        # страница открылась (T073).
        return _refused(refusal.http_status, refusal.message, row.insured_hours)

    changed = next((item for item in table.rows if item.timesheet_id == row.id), None)
    response = render(
        request,
        "timesheets/_insured_cell.html",
        {"row": changed, "oob": True, "can_edit": True, "period": period},
    )
    response["X-Cell-Value"] = f"{value:.2f}"
    return response


@login_required
@require_POST
def close(request, period_id):
    """Закрыть часы точки за месяц (T022)."""
    return _switch_closing(request, period_id, closing=True)


@login_required
@require_POST
def reopen(request, period_id):
    """Открыть часы точки заново.

    Отдельный маршрут, а не поле `action` в одной форме: два разных действия с
    разными последствиями не должны отличаться значением скрытого поля — так
    ошибка в разметке превращает закрытие в открытие незаметно.
    """
    return _switch_closing(request, period_id, closing=False)


def _switch_closing(request, period_id, *, closing: bool):
    period = find_period(period_id)
    who = _context(request, period)

    # Право проверяется до всего остального: отказ по праву не должен зависеть
    # от того, существует ли точка, которую человек назвал.
    try:
        permissions.check(who, permissions.UNIT_CLOSE)
    except permissions.PermissionRefused as refusal:
        return _grid_page(request, period, who, notice=refusal.message,
                          status=refusal.http_status)

    wanted = request.POST.get("unit") or ""
    table = build_grid(period.tenant_id, period.period, unit_ids=who.unit_ids)
    states = {
        str(state.unit_id): state
        for state in unit_states(period.tenant_id, period.period, table.rows)
    }
    state = states.get(wanted)
    if state is None:
        # Чужая точка и несуществующая выглядят одинаково: по ответу нельзя
        # понять, что такая точка вообще есть. Так же отвечает запись ячейки.
        return HttpResponseNotFound(_("точка не найдена"))

    action = close_unit if closing else reopen_unit
    action(
        tenant_id=period.tenant_id, unit_id=state.unit_id,
        period=period.period, actor_id=who.user_id,
    )
    # Редирект, а не отрисовка на месте: после POST обновление страницы не
    # должно повторять действие.
    return redirect("timesheets", period_id=period.id)


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
        return _report(request, period, error=_("Файл не выбран."), status=400)
    if upload.size > MAX_UPLOAD:
        return _report(
            request, period, status=400,
            error=_("Файл больше %(limit)s МБ — это не зарплатная таблица.")
            % {"limit": MAX_UPLOAD // 1024 // 1024},
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
            error=_("Файл не удалось прочитать как книгу Excel: %(reason)s")
            % {"reason": broken},
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
