"""
Страницы первой очереди: список периодов и страница периода.

Представления **не фильтруют по тенанту** — это работа политик базы (контракт
блока `auth`). Здесь есть только доменные фильтры: «этот месяц», «этот период».
Если контекст пользователя не выставлен, выборки пусты, и страница честно
показывает пустоту, а не чужие данные.
"""
from __future__ import annotations

from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import PasswordChangeForm
from django.http import Http404, HttpResponseBadRequest, HttpResponseNotFound
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.models import Calendar, Payrun, Period, Timesheet
from payrun.calc import calculate_period
from payrun.errors import PayrunRefused
from payrun.sheet import build_sheet

from . import auth, permissions
from .format import hours, ledger_title, money
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


def period_page(request, period, *, error=None, details=(), status=200):
    """Страница периода: сводка, ведомость и запуск расчёта.

    Ведомость собирается **только из компонентов выплаты** — суммарные поля
    ведомости не имеют политики видимости регистров и выдали бы скрытое
    вычитанием. Поэтому и итог здесь равен сумме показанных строк (D023).
    """
    sheet = build_sheet(period.tenant_id, period.period)
    timesheets = Timesheet.objects.filter(tenant=period.tenant, period=period.period)
    payrun = Payrun.objects.filter(tenant=period.tenant, period=period.period).first()
    norm_hours = calendar_norm_hours(period)
    country = period.tenant.country_code

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
                    }
                    for row in sheet.rows
                ],
                "column_totals": [
                    money(sheet.column_totals.get(column.code)) for column in sheet.columns
                ],
                "employees": sheet.employees,
            },
            "ledgers": [
                {"title": ledger_title(name), "total": money(amount)}
                for name, amount in sheet.ledger_totals
            ],
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
            "details": list(details),
            "calculated": request.GET.get("calculated") == "1",
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

    Синхронно: 32 человека считаются мгновенно. Отказ показывается на той же
    странице плашкой и своим кодом ответа — молча ничего не происходит.
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

    try:
        calculate_period(
            tenant_id=period.tenant_id,
            period=period.period,
            visible_ledgers=who.visible_ledgers,
        )
    except PayrunRefused as refusal:
        # Регистры расчёт называет кодами; человеку показываем их названия.
        details = refusal.details or [ledger_title(name) for name in refusal.ledgers]
        return period_page(
            request, period,
            error=refusal.message, details=details, status=refusal.http_status,
        )

    # Перенаправление после записи: обновление страницы не повторяет расчёт.
    return redirect(reverse("period", args=[period.id]) + "?calculated=1")


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
