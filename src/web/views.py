"""
Страницы первой очереди: список периодов и страница периода.

Представления **не фильтруют по тенанту** — это работа политик базы (контракт
блока `auth`). Здесь есть только доменные фильтры: «этот месяц», «этот период».
Если контекст пользователя не выставлен, выборки пусты, и страница честно
показывает пустоту, а не чужие данные.
"""
from __future__ import annotations

from decimal import Decimal

from django.db.models import Sum
from django.http import Http404, HttpResponseBadRequest, HttpResponseNotFound
from django.shortcuts import redirect, render
from django.views.decorators.http import require_POST

from core.models import PayComponent, Period, Timesheet

from . import devauth
from .format import ledger_title, money

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


def period_detail(request, period_id):
    period = Period.objects.select_related("tenant").filter(pk=period_id).first()
    if period is None:
        # Чужой период и несуществующий выглядят одинаково: по ответу нельзя
        # понять, что период существует у другого партнёра.
        raise Http404("период не найден")

    # Заготовка под ведомость (её строит T042): пока только итоги по регистрам.
    # Строки чужих регистров сюда не доедут — их отсекает политика базы,
    # поэтому итог считается по видимому срезу, а не маскируется на выводе.
    totals = (
        PayComponent.objects.filter(
            tenant=period.tenant, payslip__payrun__period=period.period
        )
        .values("layer")
        .annotate(total=Sum("amount"))
        .order_by("layer")
    )
    ledgers = [
        {
            "title": ledger_title(row["layer"]),
            "total": money(row["total"]),
        }
        for row in totals
    ]
    total = sum((row["total"] for row in totals), Decimal("0"))

    timesheets = Timesheet.objects.filter(tenant=period.tenant, period=period.period)
    return render(
        request,
        "web/period.html",
        {
            "period": period,
            "title": month_title(period.period),
            "status": STATUS_TITLES.get(period.status, period.status),
            "ledgers": ledgers,
            "total": money(total) if ledgers else money(None),
            "employees": timesheets.count(),
            "norm_hours": timesheets.values_list("norm_hours", flat=True).first(),
        },
    )


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
