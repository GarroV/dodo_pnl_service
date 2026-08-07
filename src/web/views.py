"""
Страницы первой очереди: список периодов и страница периода.

Представления **не фильтруют по тенанту** — это работа политик базы (контракт
блока `auth`). Здесь есть только доменные фильтры: «этот месяц», «этот период».
Если контекст пользователя не выставлен, выборки пусты, и страница честно
показывает пустоту, а не чужие данные.
"""
from __future__ import annotations

from django.http import Http404, HttpResponseBadRequest, HttpResponseNotFound
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.models import Payrun, Period, Timesheet
from payrun.calc import calculate_period
from payrun.errors import PayrunRefused
from payrun.sheet import build_sheet

from . import devauth
from .format import ledger_title, money
from .principal import current_principal

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


def period_page(request, period, *, error=None, details=(), status=200):
    """Страница периода: сводка, ведомость и запуск расчёта.

    Ведомость собирается **только из компонентов выплаты** — суммарные поля
    ведомости не имеют политики видимости регистров и выдали бы скрытое
    вычитанием. Поэтому и итог здесь равен сумме показанных строк (D023).
    """
    sheet = build_sheet(period.tenant_id, period.period)
    timesheets = Timesheet.objects.filter(tenant=period.tenant, period=period.period)
    payrun = Payrun.objects.filter(tenant=period.tenant, period=period.period).first()

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
            "norm_hours": timesheets.values_list("norm_hours", flat=True).first(),
            "error": error,
            "details": list(details),
            "calculated": request.GET.get("calculated") == "1",
        },
        status=status,
    )


def period_detail(request, period_id):
    return period_page(request, find_period(period_id))


@require_POST
def period_calculate(request, period_id):
    """«Посчитать»: движок на данных периода, результат — в базу.

    Синхронно: 32 человека считаются мгновенно. Отказ показывается на той же
    странице плашкой и своим кодом ответа — молча ничего не происходит.
    """
    period = find_period(period_id)
    who = current_principal(request)
    if who is None:
        raise Http404("период не найден")

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


# --- вход на время стройки ---------------------------------------------------


def dev_login(request):
    """Страница выбора пользователя. Выключается настройкой DEV_LOGIN_ENABLED."""
    if not devauth.is_enabled():
        return HttpResponseNotFound("dev-вход выключен")

    if request.method == "POST":
        code = request.POST.get("user", "")
        if code not in devauth.DEV_USERS:
            return HttpResponseBadRequest("неизвестная учётка")
        devauth.login(request, code)
        return redirect("periods")

    return render(request, "web/dev_login.html", {"users": devauth.DEV_USERS.values()})


@require_POST
def dev_logout(request):
    devauth.logout(request)
    return redirect("periods")
