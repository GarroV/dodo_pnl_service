"""Маршруты интерфейса."""
from django.urls import path

from . import directory_views, reports_views, rules_views, views

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
    # Админка справочников (T018). Все адреса под одним префиксом, а не
    # россыпью по корню: право `directory.manage` — одно на все пять экранов, и
    # общий префикс делает это видно по адресу, а не только по коду.
    #
    # Заведения сотрудника среди адресов нет намеренно (D029): карточки
    # появляются из данных партнёра, админка нужна для правки. Отсутствие
    # адреса — часть решения, а не забытый маршрут.
    #
    # Список и правка — разные адреса, а не одна страница с формой на каждой
    # строке: правка справочника меняет расчёт, и человек должен видеть, что
    # именно он открыл, до того как наберёт новую ставку.
    path("directory/", directory_views.index, name="directory"),
    path("directory/employees/", directory_views.employees, name="directory-employees"),
    path(
        "directory/employees/<uuid:employee_id>/",
        directory_views.employee,
        name="directory-employee",
    ),
    path("directory/groups/", directory_views.groups, name="directory-groups"),
    path("directory/groups/new/", directory_views.group, name="directory-group-new"),
    path("directory/groups/<uuid:group_id>/", directory_views.group, name="directory-group"),
    path("directory/units/", directory_views.units, name="directory-units"),
    path("directory/units/new/", directory_views.unit, name="directory-unit-new"),
    path("directory/units/<uuid:unit_id>/", directory_views.unit, name="directory-unit"),
    path(
        "directory/legal-entities/",
        directory_views.legal_entities,
        name="directory-legal-entities",
    ),
    path(
        "directory/legal-entities/new/",
        directory_views.legal_entity,
        name="directory-legal-entity-new",
    ),
    path(
        "directory/legal-entities/<uuid:entity_id>/",
        directory_views.legal_entity,
        name="directory-legal-entity",
    ),
    # Правила расчёта (T090). Отдельный префикс, а не раздел справочников:
    # право другое (`rules.manage`), и партнёр вправе развести ведение
    # справочников и ведение правил по разным людям.
    #
    # Правило адресуется своим путём через точку — тем самым, которым его знают
    # и база (`rule_overrides.path`), и след расчёта. Придумывать ему второй
    # ключ значило бы завести второе имя одному и тому же: в следе человек
    # видит `groups.couriers.work_measure`, и по этой строке он должен попадать
    # на страницу правила, а не искать её глазами.
    path("rules/", rules_views.index, name="rules"),
    path("rules/<str:path>/", rules_views.rule, name="rule"),
    # Месяц календаря адресуется самим месяцем, а не uuid: строка одна на
    # страну и месяц, и `2026-06` в адресе читается человеком, в отличие от
    # случайного ключа.
    path("directory/calendar/", directory_views.calendar, name="directory-calendar"),
    path(
        "directory/calendar/new/",
        directory_views.calendar_month,
        name="directory-calendar-new",
    ),
    path(
        "directory/calendar/<slug:month>/",
        directory_views.calendar_month,
        name="directory-calendar-month",
    ),
]
