"""
Страницы первой очереди: список периодов и страница периода.

Представления **не фильтруют по тенанту** — это работа политик базы (контракт
блока `auth`). Здесь есть только доменные фильтры: «этот месяц», «этот период».
Если контекст пользователя не выставлен, выборки пусты, и страница честно
показывает пустоту, а не чужие данные.
"""
from __future__ import annotations

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
from django.views.decorators.http import require_POST

from core.models import Calendar, Payrun, Payslip, Period, Timesheet
from payrun import freezing, jobs, lifecycle
from payrun.errors import PayrunRefused
from reports.sheet import build_slice

from . import auth, permissions
from .format import cut_title, hours, ledger_title, money
from .principal import get_current_principal

MONTHS = (
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
)

STATUS_TITLES = {
    "open": "Открыт",
    "closed": "Закрыт",
    "locked": "Заблокирован",
}


def month_title(value) -> str:
    """«Июнь 2026». Своим списком, а не через локаль: месяц в шапке — не дата в тексте."""
    return f"{MONTHS[value.month - 1]} {value.year}"


def index(request):
    return redirect("periods")


@login_required
def periods(request):
    rows = [
        {
            "id": period.id,
            "title": month_title(period.period),
            "status": STATUS_TITLES.get(period.status, period.status),
            "tenant": period.tenant.title,
        }
        for period in Period.objects.select_related("tenant").order_by("-period")
    ]
    return render(request, "web/periods.html", {"periods": rows})


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
    error_title="Расчёт не выполнен.",
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

    return render(
        request,
        "web/period.html",
        {
            "period": period,
            "title": month_title(period.period),
            "status": STATUS_TITLES.get(period.status, period.status),
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
            "norm_hours_hint": (
                f"производственный календарь {country}"
                if norm_hours is not None
                else f"календарь {country} на этот месяц не заведён"
            ),
            "error": error,
            "error_title": error_title,
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
            "calculate_denied": calculate_denied or (lifecycle.APPROVED_REFUSAL if frozen else ""),
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
        error_title="Период не утверждён.",
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
        error_title="Период не открыт.",
    )


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
        error_title="Строка не заморожена.",
    )


@login_required
@require_POST
def payslip_release(request, payslip_id):
    """«Снять заморозку»: человек возвращается в общий расчёт."""
    return payslip_action(
        request, payslip_id,
        run=lambda payslip, who: freezing.release(payslip, actor_id=who.user_id),
        done_flag="released",
        error_title="Заморозка не снята.",
    )


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
                "error": "Логин или пароль не подходят",
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
