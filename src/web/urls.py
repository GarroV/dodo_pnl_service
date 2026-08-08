"""Маршруты интерфейса."""
from django.urls import path

from . import reports_views, views

urlpatterns = [
    path("", views.index, name="index"),
    path("periods/", views.periods, name="periods"),
    path("periods/<uuid:period_id>/", views.period_detail, name="period"),
    path("periods/<uuid:period_id>/calculate/", views.period_calculate, name="period-calculate"),
    # Ход расчёта отдельным ответом: его спрашивает полоса прогресса на странице
    # периода, пока задача считает (T024). Только чтение, поэтому GET.
    path(
        "periods/<uuid:period_id>/calculate/status/",
        views.period_calculate_status,
        name="period-calculate-status",
    ),
    # Отчёт расхождений с прошлым месяцем (T030). Только чтение, поэтому GET;
    # разрез по регистру приезжает тем же `?ledger=`, что у ведомости.
    path("periods/<uuid:period_id>/variance/", views.period_variance, name="period-variance"),
    # Цикл периода: утверждение и откат. Оба только POST — это запись, а не
    # просмотр, и по ссылке из письма или из истории браузера случиться не должны.
    path("periods/<uuid:period_id>/approve/", views.period_approve, name="period-approve"),
    path("periods/<uuid:period_id>/reopen/", views.period_reopen, name="period-reopen"),
    # Перенос разницы за закрытый месяц в текущий (T026). Только POST: это
    # запись денег, и по ссылке из истории браузера случиться не должна.
    path("periods/<uuid:period_id>/retro/", views.period_retro_post, name="period-retro"),
    # Заморозка спорной строки ведомости (T027). Адрес по строке, а не по
    # периоду: в контракте блока он записан как POST /payslips/{id}/freeze, и
    # строка однозначно определяет период сама.
    path("payslips/<uuid:payslip_id>/freeze/", views.payslip_freeze, name="payslip-freeze"),
    path("payslips/<uuid:payslip_id>/release/", views.payslip_release, name="payslip-release"),
    # След расчёта строки (T029, D025). Адрес по строке, как в контракте блока:
    # объясняется сумма конкретной строки ведомости, а не «период вообще».
    # Разрез приезжает тем же `?ledger=`, что у ведомости, — человек приходит
    # сюда из разреза и обязан остаться в нём.
    path("payslips/<uuid:payslip_id>/trace/", views.payslip_trace, name="payslip-trace"),
    # Вход: логин с паролем, выход, смена своего пароля.
    path("login/", views.login_page, name="login"),
    path("logout/", views.logout_page, name="logout"),
    path("account/password/", views.password_change, name="password-change"),
    # Вход-ярлык на время стройки. Не отдельный способ проверки личности:
    # подставляет пароль учётки сида и идёт тем же путём (см. web/auth.py).
    path("dev/login/", views.dev_login, name="dev-login"),
    path("dev/logout/", views.logout_page, name="dev-logout"),
    # Сверка с таблицей бухгалтера (T031). GET — форма, POST — результат:
    # файл нигде не сохраняется, поэтому показывать по GET нечего (D028).
    path(
        "periods/<uuid:period_id>/reconcile/",
        reports_views.period_reconcile,
        name="period-reconcile",
    ),
    # Выгрузки (T032). Один маршрут на три вида, а не три почти одинаковых:
    # адреса из контракта блока (`/export/payout`, `/export/pnl`,
    # `/export/partner`) получаются те же, а неизвестный вид отвечает 404.
    path(
        "periods/<uuid:period_id>/export/<slug:kind>/",
        reports_views.period_export,
        name="period-export",
    ),
]
