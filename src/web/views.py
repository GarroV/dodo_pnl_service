"""
Страницы первой очереди: список периодов и страница периода.

Представления **не фильтруют по тенанту** — это работа политик базы (контракт
блока `auth`). Здесь есть только доменные фильтры: «этот месяц», «этот период».
Если контекст пользователя не выставлен, выборки пусты, и страница честно
показывает пустоту, а не чужие данные.
"""
from __future__ import annotations

from decimal import Decimal

from django.conf import settings
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.http import (
    Http404,
    HttpResponseBadRequest,
    HttpResponseNotFound,
    JsonResponse,
)
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext, gettext_noop
from django.utils.translation import gettext_lazy as _
from django.views.decorators.http import require_POST

from core.models import Calendar, Payrun, Payslip, Period, Timesheet
from payrun import freezing, jobs, lifecycle, retro
from payrun.errors import PayrunRefused
from reports.sheet import build_slice
from reports.trace import TraceNotFound, build_trace
from reports.variance import ThresholdsMissing, build_variance

from . import auth, onboarding, permissions
from .format import cut_title, hours, ledger_title, money
from .i18n import month_title
from .principal import get_current_principal

STATUS_TITLES = {
    "open": _("Открыт"),
    "closed": _("Закрыт"),
    "locked": _("Заблокирован"),
}


def status_title(code: str) -> str:
    """Состояние месяца словами. Незнакомое состояние показывается кодом как есть."""
    known = STATUS_TITLES.get(code)
    return str(known) if known is not None else code


def index(request):
    return redirect("periods")


@login_required
def periods(request):
    rows = [
        {
            "id": period.id,
            "title": month_title(period.period),
            "status": status_title(period.status),
            "tenant": period.tenant.title,
        }
        for period in Period.objects.select_related("tenant").order_by("-period")
    ]
    return render(
        request,
        "web/periods.html",
        # Порядок работы за месяц показывается и здесь (T077): человек,
        # впервые открывший продукт, попадает на этот экран, и «что делать
        # дальше» он должен прочитать раньше, чем выберет месяц. Текущего шага
        # тут нет — отсюда не видно, о каком месяце речь.
        {"periods": rows, "steps": onboarding.month_steps()},
    )


def find_period(period_id) -> Period:
    period = Period.objects.select_related("tenant").filter(pk=period_id).first()
    if period is None:
        # Чужой период и несуществующий выглядят одинаково: по ответу нельзя
        # понять, что период существует у другого партнёра.
        raise Http404("период не найден")
    return period


def calendar_norm_hours(period: Period):
    """Норма часов месяца — из производственного календаря страны партнёра.

    Не из табеля. В табеле норма **персональная**: у полставочника она честно
    другая, и «первая попавшаяся» строка давала каждой роли своё число — у
    управляющего выборка сужена его точкой, и первой приходила не та строка.
    Так на одной и той же странице одного и того же месяца трое видели 176, а
    четвёртый 88 (найдено сверкой со спекой 2026-08-07).

    Календаря на месяц нет — возвращается `None`, и страница честно говорит об
    этом. Подставлять сюда число самим (хоть константу, хоть чью-то норму)
    нельзя: неверное значение выглядит на экране ровно как верное.
    """
    return (
        Calendar.objects.filter(
            country_code=period.tenant.country_code, period=period.period
        )
        .values_list("norm_hours", flat=True)
        .first()
    )


def first_of_payslip(payslip_id, seen: set) -> bool:
    """Первая ли это строка своей ведомости на экране.

    Нужно затем, что действие относится к строке ведомости, а показанных строк
    у человека может быть две — по одной на регистр учёта.
    """
    if payslip_id is None or payslip_id in seen:
        return False
    seen.add(payslip_id)
    return True


def trace_url(row) -> str:
    """Адрес следа строки ведомости (T029). Пусто — объяснять нечего.

    Строки без `payslip_id` в ведомости быть не должно, но ссылка на «след
    ничего» выглядела бы как обещание, поэтому её просто нет.
    """
    if row.payslip_id is None:
        return ""
    return f"{reverse('payslip-trace', args=[row.payslip_id])}?ledger={row.ledger}"


def cut_url(period: Period, code: str) -> str:
    """Адрес разреза. «Все видимые» — это адрес периода без параметра вовсе.

    Не `?ledger=`, а чистый адрес: пустой параметр в ссылке выглядит как
    четвёртый, безымянный регистр.
    """
    base = reverse("period", args=[period.id])
    return base if not code else f"{base}?ledger={code}"


def period_page(
    request,
    period,
    *,
    error=None,
    # Умолчание вычисляется внутри, а не стоит в подписи (T017): значение по
    # умолчанию считается один раз при импорте модуля, а язык страницы — у
    # каждого запроса свой. Заголовок, переведённый на импорте, остался бы
    # русским навсегда.
    error_title=None,
    details=(),
    status=200,
    reason_value="",
):
    """Страница периода: сводка, ведомость и запуск расчёта.

    Ведомость собирается **только из компонентов выплаты** — суммарные поля
    ведомости не имеют политики видимости регистров и выдали бы скрытое
    вычитанием. Поэтому и итог здесь равен сумме показанных строк (D023).

    Разрез по регистру приезжает из адреса и разбирается блоком `reports`
    (T028): страница только показывает то, что он отдал. Собирать срез здесь
    значило бы собирать его второй раз в выгрузке — и разъехаться с экраном.
    """
    # Разрез живёт в адресе, а не в сессии: ссылку на «официальный срез июня»
    # можно отправить коллеге, и он увидит то же самое — в пределах того, что
    # видно ему. После действия (расчёт, заморозка) страница возвращается к
    # полной ведомости: адрес перехода собирается заново.
    view = build_slice(
        period.tenant_id, period.period, request.GET.get("ledger", "")
    )
    sheet = view.sheet
    seen_payslips: set = set()
    timesheets = Timesheet.objects.filter(tenant=period.tenant, period=period.period)
    payrun = Payrun.objects.filter(tenant=period.tenant, period=period.period).first()
    norm_hours = calendar_norm_hours(period)
    country = period.tenant.country_code

    # Кнопка запуска расчёта показывается только тому, кому расчёт разрешён
    # (T072). Проверку в `period_calculate` это не заменяет: интерфейс — не
    # контур доступа, а способ не предлагать человеку того, что ему всё равно
    # ответят отказом. Адрес расчёта остаётся рабочим, и он остаётся закрытым.
    who = get_current_principal(request)
    calculate_denied = permissions.explain(who, permissions.PAYRUN_CALCULATE)

    # Что цикл позволяет сделать с расчётом дальше — спрашивается у базы, а не
    # выводится из статуса здесь (T025): единственный источник истины о цикле
    # объявлен в схеме, и «что предложить человеку» — тот же вопрос о цикле.
    payrun_status = payrun.status if payrun else None
    allowed = lifecycle.next_statuses(payrun_status)
    frozen = payrun_status == lifecycle.APPROVED

    # Полоса живёт, только пока живо задание: у завершённого расчёта на экране
    # есть ведомость, и вечная полоса рядом с ней читалась бы как незаконченная
    # работа. А вот отказ последнего задания показать обязаны — иначе фоновый
    # расчёт молча не делал бы ничего, и это выглядело бы как поломка.
    last = jobs.last_job(period.tenant_id, period.period)
    job = last if last is not None and last.status in (jobs.QUEUED, jobs.RUNNING) else None
    if error is None and last is not None and last.status == jobs.FAILED and last.error:
        error = last.error
        details = details or last.details or []

    # Кнопки нет по двум разным причинам, и они не смешиваются: цикл сюда не
    # пускает (тогда о праве говорить нечего) или права нет (тогда на месте
    # кнопки стоит тот же текст, которым ответит отказ).
    approve_denied = (
        permissions.explain(who, permissions.PERIOD_APPROVE)
        if lifecycle.APPROVED in allowed
        else ""
    )
    reopen_denied = (
        permissions.explain(who, permissions.PERIOD_REOPEN)
        if lifecycle.REOPENED in allowed
        else ""
    )

    # Заморозка спорной строки (T027). В утверждённом периоде её не предлагают:
    # там заморожен весь расчёт, и база новую заморозку отвергнет — предлагать
    # кнопку значило бы обещать невозможное. Право проверяется отдельно от
    # состояния, как и у остальных действий: две разные причины «кнопки нет» не
    # смешиваются.
    freeze_denied = permissions.explain(who, permissions.PAYSLIP_FREEZE) if not frozen else ""
    can_freeze = not freeze_denied and not frozen

    # Правки задним числом (T026). Расхождение ищется только у утверждённого
    # месяца: у открытого правка входных данных — это обычная работа, её
    # пересчитывают, а не переносят. Считать теневой расчёт на каждой странице
    # подряд было бы и дорого, и бессмысленно.
    retro_mode = retro.mode(period.tenant_id)
    found = retro.drift(period.tenant_id, period.period) if frozen else retro.Drift()
    retro_denied = permissions.explain(who, permissions.RETRO_POST) if found else ""
    # Разница за этот месяц уже лежит в утверждённом периоде — откат отсюда
    # означал бы заплатить дважды, и база его отвергнет (T026). Кнопки поэтому
    # нет, а на её месте — тот же текст, которым ответит отказ.
    retro_locked = retro.locked_out(period.tenant_id, period.period)
    if retro_locked and lifecycle.REOPENED in allowed:
        # Текст отказа переводится здесь, а не в `payrun`: там он объявлен
        # `gettext_noop`, чтобы попасть в каталог, но остаться обычной строкой
        # для очереди и для журнала (T017).
        reopen_denied = reopen_denied or gettext(retro.LOCKED_REFUSAL)

    return render(
        request,
        "web/period.html",
        {
            "period": period,
            "title": month_title(period.period),
            "status": status_title(period.status),
            "sheet": {
                "columns": [column.title for column in sheet.columns],
                "rows": [
                    {
                        "employee": row.employee,
                        "unit": row.unit,
                        "ledger": ledger_title(row.ledger),
                        "cells": [
                            money(row.amounts.get(column.code)) for column in sheet.columns
                        ],
                        "total": money(row.total),
                        # Заморозка у человека одна на все его регистры, а строк
                        # у него может быть две. Метка стоит у каждой (иначе
                        # половина ведомости выглядела бы живой), а действие —
                        # один раз: две одинаковые кнопки об одном и том же
                        # читаются как разные.
                        "payslip_id": row.payslip_id,
                        "frozen": row.frozen,
                        "freeze_reason": row.freeze_reason,
                        "show_action": first_of_payslip(row.payslip_id, seen_payslips),
                        # След расчёта строки (T029). Разрез в ссылке — регистр
                        # самой строки: строка ведомости это пара «сотрудник ×
                        # регистр», и объяснять её итог надо тем же срезом,
                        # иначе след не сойдётся с числом, по которому кликнули.
                        "trace_url": trace_url(row),
                        # Строка разницы обязана объяснить себя словами: без
                        # месяца-источника это непонятная сумма в чужом месяце.
                        "is_retro": row.is_retro,
                        "retro_title": (
                            retro.month_title(row.retro_source) if row.is_retro else ""
                        ),
                    }
                    for row in sheet.rows
                ],
                "column_totals": [
                    money(sheet.column_totals.get(column.code)) for column in sheet.columns
                ],
                "employees": sheet.employees,
            },
            # Переключатель разреза (T028). Кнопок либо нет вовсе, либо их
            # больше одной: ряд из единственной кнопки намекал бы роли с одним
            # регистром, что где-то есть и другие.
            "cuts": [
                {
                    "code": cut.code,
                    "title": cut_title(cut.code),
                    "selected": cut.selected,
                    "url": cut_url(period, cut.code),
                }
                for cut in view.cuts
            ],
            "cut_title": cut_title(view.cut) if view.cut else "",
            # Код выбранного разреза отдельно от списка кнопок: его берут
            # адреса выгрузок. Второго места, где хранился бы выбранный разрез,
            # быть не должно — иначе экран и файл разъедутся молча.
            "cut_code": view.cut or "",
            "ledgers": [
                {"title": ledger_title(name), "total": money(amount)}
                for name, amount in sheet.ledger_totals
            ],
            # Разбивка по регистрам показывается, только когда регистров больше
            # одного: иначе она слово в слово повторяет подвал ведомости.
            "show_ledger_totals": len(sheet.ledger_totals) > 1,
            "total": money(sheet.total) if sheet else money(None),
            "calculated_at": payrun.calculated_at if payrun else None,
            "employees": timesheets.count(),
            "norm_hours": hours(norm_hours),
            # Две формы, а не одна со вставкой: код страны стоит в предложении
            # в разных местах у разных языков, и «хвост» переводчику не
            # приставить (T017).
            "norm_hours_hint": (
                _("производственный календарь %(country)s") % {"country": country}
                if norm_hours is not None
                else _("календарь %(country)s на этот месяц не заведён")
                % {"country": country}
            ),
            # Порядок работы за месяц и то, где человек сейчас (T077). Шаги
            # считаются по фактам о данных, а не по разметке: «часы внесены» —
            # это наличие табелей, «посчитано» — наличие расчёта, «утверждено» —
            # состояние цикла. Второго источника истины о том же самом быть не
            # должно, поэтому и `has_hours` берётся из тех же табелей, что
            # считает сводка выше.
            "steps": onboarding.month_steps(
                has_hours=timesheets.exists(),
                calculated=payrun is not None,
                approved=frozen,
            ),
            "error": error,
            "error_title": error_title or _("Расчёт не выполнен."),
            "details": list(details),
            "calculated": request.GET.get("calculated") == "1",
            # --- ход фонового расчёта (T024) ---
            "job": jobs.state_of(job),
            "job_running": job is not None,
            # Что обещано под кнопкой. «Считается сразу» при включённой очереди —
            # неправда ровно в тот момент, когда человек решает, ждать ему на
            # странице или уйти.
            "background_calculation": settings.PAYRUN_BACKGROUND,
            "queued": request.GET.get("queued") == "1",
            # Утверждённый период пересчитать нельзя — это отвергает база, и
            # предлагать кнопку значило бы обещать невозможное. Адрес расчёта
            # остаётся рабочим и по-прежнему отвечает отказом со словами.
            # Пока задание живо, кнопки расчёта нет: второе нажатие всё равно
            # получит отказ «уже считается», а предлагать заведомый отказ — то же
            # самое, что обещать невозможное.
            "can_calculate": not calculate_denied and not frozen and job is None,
            "calculate_denied": calculate_denied
            or (gettext(lifecycle.APPROVED_REFUSAL) if frozen else ""),
            # --- цикл периода (T025) ---
            "payrun_status": lifecycle.status_title(payrun_status) if payrun else "",
            "can_approve": lifecycle.APPROVED in allowed and not approve_denied,
            "approve_denied": approve_denied,
            "can_reopen": lifecycle.REOPENED in allowed and not reopen_denied,
            "reopen_denied": reopen_denied,
            "reason_value": reason_value,
            "history": lifecycle.history(payrun),
            "approved": request.GET.get("approved") == "1",
            "reopened": request.GET.get("reopened") == "1",
            # --- заморозка строк (T027) ---
            "can_freeze": can_freeze,
            "freeze_denied": freeze_denied,
            "froze": request.GET.get("froze") == "1",
            "released": request.GET.get("released") == "1",
            # --- правки задним числом (T026) ---
            # Что партнёр делает при расхождении, сказано словами: настройка,
            # молча меняющая поведение денег, — худший вид настройки.
            "retro_mode": retro_mode,
            "retro_mode_title": titled(retro.MODE_TITLES, retro_mode),
            "retro_drift": found,
            "retro_error": found.error,
            "retro_total": money(found.total) if found else "",
            "retro_target": (
                retro.month_title(retro.next_open_period(period.tenant_id, period.period))
                if found and retro_mode == retro.DELTA else ""
            ),
            "retro_lines": [
                {
                    "employee": line.employee,
                    "title": line.title,
                    "ledger": ledger_title(line.ledger),
                    "amount": money(line.amount),
                }
                for line in found.lines
            ],
            # Кнопки нет по двум разным причинам, и они не смешиваются: партнёр
            # ведёт учёт пересчётом (тогда о праве говорить нечего) или права
            # нет (тогда на месте кнопки тот же текст, которым ответит отказ).
            "can_post_retro": bool(found) and retro_mode == retro.DELTA and not retro_denied,
            "retro_denied": retro_denied if retro_mode == retro.DELTA else "",
            # Разница уже перенесена в утверждённый период: откат отсюда
            # означал бы заплатить дважды, и кнопки отката не будет.
            "retro_locked": retro_locked,
            # Перенос сюда есть, а в ведомости его ещё (или уже) нет.
            "retro_pending": [
                {
                    "title": item.title,
                    "live": money(item.live),
                    "shown": money(item.shown),
                    "cancelled": item.cancelled,
                }
                for item in retro.pending(period.tenant_id, period.period)
            ],
            "retro_carried": [
                retro.month_title(source)
                for source in retro.carried_in(period.tenant_id, period.period)
            ],
            "retro_posted": request.GET.get("retro") == "1",
        },
        status=status,
    )


@login_required
def period_detail(request, period_id):
    return period_page(request, find_period(period_id))


@login_required
@require_POST
def period_calculate(request, period_id):
    """«Посчитать»: движок на данных периода, результат — в базу.

    Два пути, и человек всегда знает, какой из них выбран (T024):

    - **фоном** — задача уходит в очередь, страница возвращается сразу и
      показывает ход работы;
    - **прямо сейчас** — расчёт идёт в этом же запросе, страница ждёт его конца.
      Так работает продукт с выключенной очередью и так же человек добивает
      задачу, которую очередь не взяла.

    Молчаливой подмены одного другим нет: синхронный расчёт вместо фонового
    случается только по явному нажатию «Посчитать прямо сейчас».
    """
    period = find_period(period_id)
    who = get_current_principal(request)
    if who is None or who.tenant_id is None:
        # Вошёл, но ни к какому партнёру не приписан: считать ему нечего.
        raise Http404("период не найден")

    # Право проверяется до расчёта и **раньше регистров**. Порядок важен:
    # администратору сети раньше отказывали за видимость регистров, и ту же
    # плашку он получил бы, даже если бы право у него было (T064).
    try:
        permissions.check(who, permissions.PAYRUN_CALCULATE)
    except permissions.PermissionRefused as refusal:
        return period_page(
            request, period, error=refusal.message, status=refusal.http_status,
        )

    background = settings.PAYRUN_BACKGROUND and request.POST.get("inline") != "1"
    try:
        jobs.start(
            tenant_id=period.tenant_id,
            period=period.period,
            actor_id=who.user_id,
            background=background,
        )
    except PayrunRefused as refusal:
        # Регистры расчёт называет кодами; человеку показываем их названия.
        details = refusal.details or [ledger_title(name) for name in refusal.ledgers]
        return period_page(
            request, period,
            error=refusal.message, details=details, status=refusal.http_status,
        )

    # Перенаправление после записи: обновление страницы не повторяет расчёт.
    flag = "queued" if background else "calculated"
    return redirect(reverse("period", args=[period.id]) + f"?{flag}=1")


@login_required
def period_calculate_status(request, period_id):
    """Состояние расчёта отдельным ответом — его спрашивает полоса прогресса.

    Тот же `state_of`, что рисует страницу: два разных ответа об одном и том же
    расчёте означали бы полосу, которая говорит не то, что написано рядом с ней.
    Своей проверки доступа здесь нет и не нужно — задание чужого партнёра не
    видно политикам базы, и ответ честно скажет «расчёта нет».
    """
    period = find_period(period_id)
    return JsonResponse(jobs.state_of(jobs.last_job(period.tenant_id, period.period)))


def current_payrun(period: Period):
    return Payrun.objects.filter(tenant=period.tenant, period=period.period).first()


def period_transition(request, period_id, *, code, run, done_flag, error_title):
    """Общая обвязка утверждения и отката: право, переход, отказ словами.

    Обе кнопки устроены одинаково, и разница между ними ровно в трёх вещах —
    какое право, что сделать и как назвать отказ. Разводить их двумя копиями
    значило бы получить два разных порядка проверок на одной странице.
    """
    period = find_period(period_id)
    who = get_current_principal(request)
    if who is None or who.tenant_id is None:
        # Вошёл, но ни к какому партнёру не приписан: периода для него нет.
        raise Http404("период не найден")

    # Право проверяется первым — как в расчёте (T064). Иначе человек без права
    # узнавал бы сначала о состоянии периода, до которого ему нет дела.
    try:
        permissions.check(who, code)
    except permissions.PermissionRefused as refusal:
        return period_page(
            request, period,
            error=refusal.message, error_title=error_title,
            status=refusal.http_status,
        )

    try:
        run(current_payrun(period), who)
    except PayrunRefused as refusal:
        return period_page(
            request, period,
            error=refusal.message, error_title=error_title,
            status=refusal.http_status,
            # Написанное человеком не пропадает вместе с отказом: иначе длинную
            # причину пришлось бы набирать заново из-за опечатки.
            reason_value=(request.POST.get("reason") or ""),
        )

    # Перенаправление после записи: обновление страницы не повторяет переход.
    return redirect(reverse("period", args=[period.id]) + f"?{done_flag}=1")


@login_required
@require_POST
def period_approve(request, period_id):
    """«Утвердить период»: расчёт замораживается, автор попадает в историю."""
    return period_transition(
        request, period_id,
        code=permissions.PERIOD_APPROVE,
        run=lambda payrun, who: lifecycle.approve(payrun, actor_id=who.user_id),
        done_flag="approved",
        error_title=_("Период не утверждён."),
    )


@login_required
@require_POST
def period_reopen(request, period_id):
    """«Открыть заново»: только с причиной, и она видна в истории с автором."""
    return period_transition(
        request, period_id,
        code=permissions.PERIOD_REOPEN,
        run=lambda payrun, who: lifecycle.reopen(
            payrun, reason=request.POST.get("reason", "")
        ),
        done_flag="reopened",
        error_title=_("Период не открыт."),
    )


@login_required
@require_POST
def period_retro_post(request, period_id):
    """«Перенести разницу»: закрытый месяц остаётся прежним, разница едет вперёд.

    Порядок проверок тот же, что у расчёта и у цикла (T064): право → всё
    остальное. Человек без права не должен сначала узнавать, есть ли вообще
    расхождение, — до него ему нет дела.

    В сам закрытый период не пишется ничего, и это видно по коду: перенос —
    вставка в свою таблицу. Гарантией это, однако, не является — её держит
    сторож `payrun_frozen_guard` (T023), отвергающий любую запись в утверждённый
    расчёт на любом пути.
    """
    period = find_period(period_id)
    who = get_current_principal(request)
    if who is None or who.tenant_id is None:
        # Вошёл, но ни к какому партнёру не приписан: периода для него нет.
        raise Http404("период не найден")

    try:
        permissions.check(who, permissions.RETRO_POST)
        target, moved = retro.post(
            tenant_id=period.tenant_id,
            source=period.period,
            actor_id=who.user_id,
            visible_ledgers=who.visible_ledgers,
        )
    except permissions.PermissionRefused as refusal:
        return period_page(
            request, period, error=refusal.message,
            error_title=_("Разница не перенесена."), status=refusal.http_status,
        )
    except PayrunRefused as refusal:
        # Регистры отказ называет кодами; человеку показываем их названия.
        details = refusal.details or [ledger_title(name) for name in refusal.ledgers]
        return period_page(
            request, period, error=refusal.message, details=details,
            error_title=_("Разница не перенесена."), status=refusal.http_status,
        )

    # Перенаправление после записи: обновление страницы не переносит второй раз.
    # Второй перенос и так не нашёл бы расхождения (оно уже вычтено), но
    # надеяться на это вместо перенаправления значило бы обещать, что арифметика
    # заменяет защиту от повторной отправки формы.
    del target, moved
    return redirect(reverse("period", args=[period.id]) + "?retro=1")


def find_payslip(request, payslip_id) -> tuple[Payslip, Period]:
    """Строка ведомости и её учётный месяц — или 404.

    Невидимой строки для человека не существует: политики её не отдают, и
    «нет доступа» здесь неотличимо от «нет такой строки» намеренно — по коду
    ответа не должно быть видно, что у соседней точки кто-то есть.
    """
    payslip = (
        Payslip.objects.filter(pk=payslip_id).select_related("payrun").first()
    )
    if payslip is None:
        raise Http404("строка ведомости не найдена")
    period = Period.objects.filter(
        tenant_id=payslip.tenant_id, period=payslip.payrun.period
    ).first()
    if period is None:
        raise Http404("период не найден")
    return payslip, period


def payslip_action(request, payslip_id, *, run, done_flag, error_title):
    """Общая обвязка заморозки и снятия: право, действие, отказ словами.

    Порядок тот же, что у расчёта и цикла периода (T064): право → состояние →
    поля формы. Человек без права не должен сначала узнавать про состояние
    периода, до которого ему нет дела.
    """
    payslip, period = find_payslip(request, payslip_id)
    who = get_current_principal(request)
    if who is None or who.tenant_id is None:
        raise Http404("строка ведомости не найдена")

    try:
        permissions.check(who, permissions.PAYSLIP_FREEZE)
    except permissions.PermissionRefused as refusal:
        return period_page(
            request, period,
            error=refusal.message, error_title=error_title,
            status=refusal.http_status,
        )

    try:
        run(payslip, who)
    except PayrunRefused as refusal:
        return period_page(
            request, period,
            error=refusal.message, error_title=error_title,
            status=refusal.http_status,
        )

    # Перенаправление после записи: обновление страницы не повторяет действие.
    return redirect(reverse("period", args=[period.id]) + f"?{done_flag}=1")


@login_required
@require_POST
def payslip_freeze(request, payslip_id):
    """«Заморозить строку»: спор по одному человеку не держит остальных."""
    return payslip_action(
        request, payslip_id,
        run=lambda payslip, who: freezing.freeze(
            payslip, actor_id=who.user_id, reason=request.POST.get("reason", "")
        ),
        done_flag="froze",
        error_title=_("Строка не заморожена."),
    )


@login_required
@require_POST
def payslip_release(request, payslip_id):
    """«Снять заморозку»: человек возвращается в общий расчёт."""
    return payslip_action(
        request, payslip_id,
        run=lambda payslip, who: freezing.release(payslip, actor_id=who.user_id),
        done_flag="released",
        error_title=_("Заморозка не снята."),
    )


# --- след расчёта строки (T029) ----------------------------------------------


# Подписи производных величин. Здесь, а не в `reports`: там данные, тут слова —
# та же граница, что у названий регистров и формата чисел (T028).
DERIVED_TITLES = {
    "gross": gettext_noop("Бруто"),
    "tax": gettext_noop("Налог"),
    "contributions": gettext_noop("Взносы"),
    "total_cost": gettext_noop("Полная стоимость"),
}

# Как назвать вход шага по-человечески. Ключи движка английские и стабильные —
# перевод их дело интерфейса, а не следа (см. `payroll.trace.TraceStep`).
INPUT_TITLES = {
    "hours": gettext_noop("часов"),
    "rate": gettext_noop("ставка за час"),
    "pay_percent": gettext_noop("процент оплаты"),
    "floor": gettext_noop("минимум за час"),
    "hour_types": gettext_noop("типы часов"),
    "prorate_by": gettext_noop("пропорция"),
    "amount_per_norm": gettext_noop("за полную норму"),
    "worked_days": gettext_noop("отработано дней"),
    "norm_days": gettext_noop("рабочих дней в месяце"),
    "worked_hours": gettext_noop("отработано часов"),
    "norm_hours": gettext_noop("норма часов"),
    "method": gettext_noop("способ"),
    "base": gettext_noop("база"),
    "insured_hours": gettext_noop("база взносов, часов"),
    # Найдено смоуком: эти два ключа приезжают в шаге часов и без подписи
    # читались как отладочный вывод. Ставка сотрудника — это базовая ставка,
    # умноженная на коэффициент, и обе величины нужны, чтобы повторить её.
    "base_rate": gettext_noop("базовая ставка"),
    "coefficient": gettext_noop("коэффициент"),
    # Производные величины: по этим числам повторяют бруто, налог и взносы.
    # Список снят с движка целиком, а не по памяти: ключ без подписи читается
    # на экране как отладочный вывод, и это нашёл смоук.
    "net": gettext_noop("нето"),
    "gross": gettext_noop("бруто"),
    "tax": gettext_noop("налог"),
    "contributions": gettext_noop("взносы"),
    "credit": gettext_noop("зачтено"),
    "withheld": gettext_noop("удержано с работника"),
    "share": gettext_noop("доля"),
    "income_tax": gettext_noop("ставка налога"),
    "employee_contributions": gettext_noop("взносы работника"),
    "employer_contributions": gettext_noop("взносы работодателя"),
    "combined_contributions": gettext_noop("взносы вместе"),
    "net_factor": gettext_noop("множитель нето → бруто"),
    "tax_free_monthly": gettext_noop("необлагаемый минимум в месяц"),
    "half_tax_free": gettext_noop("половина необлагаемого"),
    "min_contribution_base": gettext_noop("минимальная база взносов"),
    "reference_norm_hours": gettext_noop("эталонная норма часов"),
    "hours_divisor": gettext_noop("делитель часов"),
    "hours_per_day": gettext_noop("часов в рабочем дне"),
    "rate_key": gettext_noop("какая ставка"),
    # Сдельная работа (T075). `rate` и `quantity` меняют смысл вместе с
    # правилом `pay_per_unit` — их подписи ниже, здесь только умолчания.
    "measure": gettext_noop("мера работы"),
    "quantity": gettext_noop("величина из табеля"),
    "pay_per_unit": gettext_noop("как считается"),
    # Правка руками — не правило, а ввод: сумма, которую поставил бухгалтер.
    "amount": gettext_noop("сумма"),
}

# Подписи, у которых смысл зависит от соседнего входа того же шага (T081,
# issue #72). У сдельной группы `rate` — цена ЕДИНИЦЫ, а не часа: рядом стоит
# `quantity 120` и `pay_per_unit True`, и подпись «ставка за час» превращает
# объяснение в неправду про единицу измерения. Одна подпись на все шаги здесь
# невозможна в принципе — величина у входа одна, а означает она разное.
PIECE_TITLES = {
    "rate": gettext_noop("цена единицы"),
    "quantity": gettext_noop("количество"),
}

# Фиксированная выплата: в табель вводят саму сумму, а не количество чего-то.
# Ставки при этом нет вовсе — не «ставка None», а нет (см. `input_pairs`).
FIXED_PAYOUT_TITLES = {
    "quantity": gettext_noop("сумма из табеля"),
}

# Значение `pay_per_unit` словами: «True» посреди объяснения денег читается как
# отладочный вывод, а сказать здесь нужно ровно одно — как получилась сумма.
PAY_PER_UNIT_VALUES = {
    True: gettext_noop("количество × цена единицы"),
    False: gettext_noop("сумма из табеля как есть"),
}

# Откуда приехало правило: чьё это решение — страны, партнёра, группы или
# человека. «input» — не правило вовсе, а число, введённое руками.
LEVEL_TITLES = {
    "country": gettext_noop("правило страны"),
    "tenant": gettext_noop("переопределение партнёра"),
    "group": gettext_noop("переопределение группы"),
    "employee": gettext_noop("переопределение по сотруднику"),
    "input": gettext_noop("введено руками"),
}

# Порядок входов на экране — как в формуле, слева направо: часы × ставка ×
# процент, количество × цена единицы. Прочие идут следом по алфавиту, чтобы не
# прыгали от шага к шагу.
INPUT_ORDER = ["hours", "quantity", "rate", "pay_percent", "floor", "amount_per_norm"]


def titled(titles: dict, code: str, fallback: str = "") -> str:
    """Подпись из словаря на языке страницы; незнакомый код — как есть (T017).

    Словари выше держат русские строки через `gettext_noop`, а переводятся они
    здесь, в момент показа: словарь собирается один раз на импорт, а язык у
    каждого запроса свой. Пропущенный код — не ошибка: движок вправе завести
    новый вход раньше, чем интерфейс придумает ему подпись, и тогда на экране
    честнее показать ключ, чем пустоту.
    """
    known = titles.get(code)
    if known is not None:
        return gettext(known)
    return fallback or code


def input_title(name: str, values: dict) -> str:
    """Подпись входа с оглядкой на соседей по шагу (T081).

    Смысл `rate` и `quantity` задаёт правило `pay_per_unit` того же шага, а не
    имя ключа: 420 — это цена доставки, если работа меряется доставками, и
    ставка часа, если часами. Подпись обязана говорить именно то, что верно для
    этой строки, иначе след врёт ровно в том месте, ради которого его читают.
    """
    per_unit = values.get("pay_per_unit")
    special = PIECE_TITLES if per_unit is True else FIXED_PAYOUT_TITLES if per_unit is False else {}
    if name in special:
        return gettext(special[name])
    return titled(INPUT_TITLES, name)


def input_value(name: str, value) -> str:
    """Значение входа так, как его читает человек."""
    if name == "pay_per_unit":
        # Признак правила словами: «True» посреди денег читается как отладка.
        return gettext(PAY_PER_UNIT_VALUES[bool(value)])
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if isinstance(value, Decimal):
        return hours(value) if name.endswith(("hours", "days")) else money(value)
    return str(value)


def input_pairs(values: dict) -> list[dict]:
    """Входы шага в том порядке, в котором по ним повторяют сумму на калькуляторе.

    Пустая величина не показывается вовсе. У фиксированной сдельной выплаты
    ставки нет — правило её не применяет, — и строка «ставка за час None» на
    экране означала бы, что ставка есть и она неизвестна. Это разные вещи, и
    вторая неправда (issue #72).
    """
    def key(name: str):
        return (INPUT_ORDER.index(name) if name in INPUT_ORDER else len(INPUT_ORDER), name)

    return [
        {"title": input_title(name, values), "value": input_value(name, values[name])}
        for name in sorted(values, key=key)
        if values[name] is not None
    ]


def trace_step(step) -> dict:
    """Шаг для показа. Расхождение с сохранённым не прячется, а называется."""
    return {
        "code": step.code,
        "title": step.title or step.code,
        "amount": money(step.amount),
        "ledger": ledger_title(step.ledger) if step.ledger else "",
        "inputs": input_pairs(step.inputs),
        "level": titled(LEVEL_TITLES, step.level),
        "differs": step.differs,
        # Сохранённое показывается, только когда оно отличается: одинаковое
        # число во второй колонке — шум, из-за которого перестают замечать
        # разное.
        "stored": money(step.stored) if step.differs and step.stored is not None else "",
        "appeared": step.stored is None,
    }


def period_url_for(period, tenant_id, cut: str) -> str:
    """Ссылка назад — на ту же ведомость и в тот же разрез, из которого пришли."""
    row = Period.objects.filter(tenant_id=tenant_id, period=period).first() if period else None
    if row is None:
        return reverse("periods")
    base = reverse("period", args=[row.id])
    return base if not cut else f"{base}?ledger={cut}"


@login_required
def payslip_trace(request, payslip_id):
    """«Как получилась эта сумма»: шаги расчёта одной строки ведомости.

    След **сохранён вместе с расчётом** (T056), поэтому закрытый месяц
    объясняется тем, чем считался. У строк, посчитанных до появления хранения,
    объяснение пересобирается по сегодняшним правилам и сверяется с сохранённой
    суммой — экран говорит, какой из двух следов показывает: обещания у них
    разные, и молчать об этом нельзя.
    """
    who = get_current_principal(request)
    if who is None or who.tenant_id is None:
        raise Http404("строка ведомости не найдена")

    try:
        view = build_trace(
            who.tenant_id, payslip_id, who.visible_ledgers, request.GET.get("ledger", "")
        )
    except TraceNotFound as missing:
        # Чужая строка и несуществующая отвечают одинаково: иначе перебором
        # адресов узнаётся, что строка есть и просто не видна.
        raise Http404("строка ведомости не найдена") from missing

    return render(
        request,
        "web/trace.html",
        {
            "employee": view.employee,
            "unit": view.unit,
            "title": month_title(view.period) if view.period else "",
            "back_url": period_url_for(view.period, who.tenant_id, view.cut),
            "cut_title": cut_title(view.cut) if view.cut else "",
            "steps": [trace_step(step) for step in view.steps],
            "derived": [
                {**trace_step(step), "title": titled(DERIVED_TITLES, step.kind, step.title)}
                for step in view.derived
            ],
            "carried": [
                {
                    "title": line.title,
                    "amount": money(line.amount),
                    "ledger": ledger_title(line.ledger),
                    "source": retro.month_title(line.source_period),
                }
                for line in view.carried
            ],
            "traced_total": money(view.traced_total),
            "stored_total": money(view.stored_total),
            "row_total": money(view.row_total),
            "agrees": view.agrees,
            "stored_trace": view.stored_trace,
            "error": view.error,
            "approved": view.approved,
        },
    )


# --- расхождения с прошлым месяцем (T030) ------------------------------------


def percent(value) -> str:
    """«+12,4 %» либо прочерк, если росло с нуля: процента у этого нет."""
    if value is None:
        return EMPTY_PERCENT
    quantized = Decimal(value).quantize(Decimal("0.1"))
    sign = "+" if quantized > 0 else ""
    return f"{sign}{quantized}".replace(".", ",") + " %"


EMPTY_PERCENT = "—"


def signed(value: Decimal) -> str:
    """Отклонение со знаком: «сколько прибавилось» читается только со знаком."""
    shown = money(value)
    return f"+{shown}" if value > 0 else shown


def variance_line(line) -> dict:
    """Строка отчёта. Порог показан рядом: по нему видно, почему строка здесь."""
    return {
        "employee": line.employee,
        "unit": line.unit,
        "ledger": ledger_title(line.ledger),
        "code": line.code,
        "title": line.title,
        "previous": money(line.previous),
        "current": money(line.current),
        "delta": signed(line.delta),
        "percent": percent(line.percent),
        "grew": line.delta > 0,
        "threshold": (
            f"{line.threshold.percent:g} % и {money(line.threshold.absolute)}"
        ),
    }


@login_required
def period_variance(request, period_id):
    """«Что изменилось против прошлого месяца» с порогами на каждый компонент.

    Обе стороны сравнения собираются тем же способом, что ведомость
    (`reports.sheet` → `payrun.sheet.collect_cells`), поэтому в отчёт физически
    не может попасть сумма из регистра, которого роли не видно (D023).
    """
    period = find_period(period_id)
    try:
        report = build_variance(
            period.tenant_id, period.period, request.GET.get("ledger", "")
        )
    except ThresholdsMissing as refusal:
        # Порогов нет — отчёт отказывается словами. Показать «отклонений нет»
        # значило бы соврать: их не искали.
        return render(
            request, "web/variance.html",
            {
                "title": month_title(period.period),
                "back_url": reverse("period", args=[period.id]),
                "error": str(refusal),
                "lines": [],
            },
            status=409,
        )

    return render(
        request,
        "web/variance.html",
        {
            "title": month_title(period.period),
            "previous_title": month_title(report.previous_period),
            "back_url": cut_url(period, report.cut),
            "cuts": [
                {
                    "code": code,
                    "title": cut_title(code),
                    "selected": code == report.cut,
                    "url": variance_cut_url(period, code),
                }
                for code in ([""] + report.cuts if report.cuts else [])
            ],
            "cut_title": cut_title(report.cut) if report.cut else "",
            "lines": [variance_line(line) for line in report.lines],
            "total_delta": signed(report.total_delta),
            "employees": report.employees,
            "compared": report.compared,
            "nothing_to_compare": report.nothing_to_compare,
            "error": "",
        },
    )


def variance_cut_url(period: Period, code: str) -> str:
    """Адрес разреза отчёта. «Все видимые» — адрес без параметра вовсе (T028)."""
    base = reverse("period-variance", args=[period.id])
    return base if not code else f"{base}?ledger={code}"


# --- вход --------------------------------------------------------------------


def safe_next(request) -> str:
    """Куда вернуться после входа. Только внутрь продукта, без чужих адресов."""
    target = request.POST.get("next") or request.GET.get("next") or ""
    if target.startswith("/") and not target.startswith("//"):
        return target
    return reverse("periods")


def login_page(request):
    """Вход по логину и паролю — единственный способ доказать личность."""
    if request.method == "POST":
        username = (request.POST.get("username") or "").strip()
        password = request.POST.get("password") or ""
        if auth.login_with_password(request, username, password) is not None:
            return redirect(safe_next(request))
        # Что именно не подошло — не уточняем: иначе форма отвечала бы на
        # вопрос «а есть ли такой пользователь».
        return render(
            request,
            "web/login.html",
            {
                "error": _("Логин или пароль не подходят"),
                "username": username,
                "next": safe_next(request),
                "dev_users": auth.DEV_USERS.values() if auth.dev_login_is_enabled() else [],
            },
            status=200,
        )

    return render(
        request,
        "web/login.html",
        {
            "next": safe_next(request),
            "dev_users": auth.DEV_USERS.values() if auth.dev_login_is_enabled() else [],
        },
    )


@require_POST
def logout_page(request):
    auth.logout(request)
    return redirect("login")


@login_required
def password_change(request):
    """Смена своего пароля. Хранение и проверка — штатные, своей криптографии нет."""
    if request.method == "POST":
        form = PasswordChangeForm(request.user, request.POST)
        if form.is_valid():
            user = form.save()
            # Иначе смена пароля выкинула бы человека из его же сессии.
            update_session_auth_hash(request, user)
            return redirect(reverse("password-change") + "?changed=1")
    else:
        form = PasswordChangeForm(request.user)

    return render(
        request,
        "web/password_change.html",
        {"form": form, "changed": request.GET.get("changed") == "1"},
    )


# --- вход-ярлык на время стройки ---------------------------------------------
# Не второй способ проверки личности: кнопка подставляет пароль учётки сида и
# идёт тем же путём, что человек с клавиатурой. Выключается настройкой.


def dev_login(request):
    if not auth.dev_login_is_enabled():
        return HttpResponseNotFound("dev-вход выключен")

    if request.method == "POST":
        code = request.POST.get("user", "")
        if code not in auth.DEV_USERS:
            return HttpResponseBadRequest("неизвестная учётка")
        if auth.dev_login(request, code) is None:
            # Учётки сида в базе нет или у неё другой пароль — это отказ, а не
            # тихий вход неизвестно кем.
            return HttpResponseBadRequest("учётка сида не подошла")
        return redirect("periods")

    # Отдельной страницы у ярлыка нет: кнопки живут на той же странице входа,
    # чтобы вход был один и на вид тоже.
    return redirect("login")
