"""Маршруты интерфейса."""
from django.urls import path

from . import (
    api,
    bulk_raise_views,
    cash_views,
    counterparties_views,
    directory_views,
    expense_items_views,
    expenses_views,
    guide,
    papers_views,
    person_views,
    planned_views,
    platform_views,
    positions_views,
    printing_views,
    reports_views,
    roles_views,
    rules_views,
    stages,
    suppliers_api,
    suppliers_views,
    theme,
    tills_views,
    views,
)

urlpatterns = [
    path("", views.index, name="index"),
    # Тема оформления (issue #164). Без `@login_required`: тема — свойство
    # читателя, а не роли, и гайд с титульной страницей открываются до входа.
    path("theme/", theme.set_theme, name="set-theme"),
    # Гайд по продукту (T159). Без `@login_required` намеренно: половина
    # вопроса, на который он отвечает, — «кем входить», и страница, видимая
    # только после входа, на него ответить не может по устройству.
    path("guide/", guide.page, name="guide"),
    path("periods/", views.periods, name="periods"),
    # Завести учётный месяц (issue #192). Постоянный адрес идёт раньше адреса
    # по номеру периода, иначе `open` разбирался бы как номер.
    path("periods/open/", views.open_month, name="period-open"),
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
    # Платёжная ведомость на бумагу (T187, issue #184). Только чтение, поэтому
    # GET. Разрез по регистру этот адрес не принимает: «к выплате» считается по
    # строке ведомости целиком и регистру не принадлежит — подобранный руками
    # `?ledger=` получает отказ словами, а не документ всего расчёта молча.
    path(
        "periods/<uuid:period_id>/print/payout/",
        printing_views.payout,
        name="period-print-payout",
    ),
    # Цикл периода: утверждение и откат. Оба только POST — это запись, а не
    # просмотр, и по ссылке из письма или из истории браузера случиться не должны.
    path("periods/<uuid:period_id>/approve/", views.period_approve, name="period-approve"),
    # Отложить находку проверки полноты с причиной (#175). Отдельный адрес, а не
    # поле у утверждения: это самостоятельное решение со своим следом, и человек
    # принимает его до того, как нажать «утвердить».
    path("periods/<uuid:period_id>/postpone/", views.period_postpone, name="period-postpone"),
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
    # Расчётный листок на бумагу (T187). Адрес по строке, как и у следа: листок
    # объясняет сумму конкретной строки ведомости, а не «период вообще».
    path(
        "payslips/<uuid:payslip_id>/print/",
        printing_views.payslip,
        name="payslip-print",
    ),
    # Вход: логин с паролем, выход, смена своего пароля.
    # Платформенная админка (D065, issue #193). Отдельный префикс, а не раздел
    # админки партнёра: право не партнёрское, и по адресу это должно быть видно
    # так же, как видно по правам.
    path("platform/", platform_views.index, name="platform-index"),
    path("platform/new/", platform_views.space_create, name="platform-space-create"),
    path("platform/<uuid:tenant_id>/", platform_views.space, name="platform-space"),
    path(
        "platform/<uuid:tenant_id>/roles/",
        platform_views.member_role,
        name="platform-member-role",
    ),
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
    # Решение по расхождению сверки (#172): протокол без решений отвечает
    # «разошлось» и молчит о том, что с этим сделали.
    path(
        "reconciliations/findings/<uuid:finding_id>/",
        reports_views.reconciliation_decide,
        name="reconciliation-decide",
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
    # Заведения сотрудника здесь не было намеренно (D029): карточки появлялись
    # из данных партнёра, админка нужна была для правки. **Снято в T164**, и это
    # не отмена решения задним числом, а его исчерпание: для стройки на тестовых
    # данных загрузки таблицы хватало, для передачи продукта партнёру — нет.
    # Человек выходит на работу пятнадцатого числа, и до следующей загрузки его
    # в системе не существует. Загрузка таблицы при этом осталась: это два входа
    # для двух случаев — месяц целиком и один человек. Оформление решения в
    # `decisions.md` — за владельцем, здесь только след, почему адрес появился.
    #
    # Постоянный адрес (`new/`) стоит раньше адреса по номеру записи, как у
    # групп, точек и юрлиц: иначе `new` разбирался бы как номер.
    #
    # Список и правка — разные адреса, а не одна страница с формой на каждой
    # строке: правка справочника меняет расчёт, и человек должен видеть, что
    # именно он открыл, до того как наберёт новую ставку.
    # Роли и права (T171, issue #77). Адрес есть у всех, отвечает отказом
    # словами тому, кому не положено, — как справочники и правила: сокрытие
    # адреса не защита, а проверка стоит в представлении и в политиках базы.
    path("roles/", roles_views.index, name="roles"),
    path("roles/<uuid:role_id>/rights/", roles_views.role_rights, name="role-rights"),
    path("roles/people/<uuid:user_id>/", roles_views.person_roles, name="person-roles"),
    path("directory/", directory_views.index, name="directory"),
    path("directory/employees/", directory_views.employees, name="directory-employees"),
    path(
        "directory/employees/new/",
        directory_views.employee_new,
        name="directory-employee-new",
    ),
    path(
        "directory/employees/<uuid:employee_id>/",
        directory_views.employee,
        name="directory-employee",
    ),
    # Выплаты человека по месяцам (T166). Адрес под карточкой, а не отдельным
    # корнем: приходят сюда с карточки, и вложенность делает это видно по адресу.
    # Экран при этом не справочник, а данные расчёта — открыт он тому, у кого есть
    # `payrun.calculate`, и отвечает отказом словами тому, кому не положено
    # (разбор — в шапке `web/person_views.py`). Карточка того же человека остаётся
    # открытой на чтение управляющему точки: справочник и расчёт — разные вещи.
    path(
        "directory/employees/<uuid:employee_id>/pay/",
        person_views.employee_pay,
        name="directory-employee-pay",
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
    # Статьи расходов (T108). Шестой справочник, поэтому и адрес под тем же
    # префиксом: право одно и то же (`directory.manage`), и общий префикс делает
    # это видно по адресу, а не только по коду.
    path(
        "directory/expense-items/",
        expense_items_views.expense_items,
        name="directory-expense-items",
    ),
    path(
        "directory/expense-items/new/",
        expense_items_views.expense_item,
        name="directory-expense-item-new",
    ),
    # Наполнение справочника файлом бухгалтера (T147, D041). Отдельный адрес, а
    # не POST на список: список — чтение, и его повторная отправка не должна
    # загружать файл второй раз.
    path(
        "directory/expense-items/upload/",
        expense_items_views.expense_items_upload,
        name="directory-expense-items-upload",
    ),
    path(
        "directory/expense-items/<uuid:item_id>/",
        expense_items_views.expense_item,
        name="directory-expense-item",
    ),
    # Контрагенты (T150). Восьмой справочник и единственный, чей СПИСОК открыт
    # всем ролям: контрагента выбирает бухгалтер, внося счёт, и закрытый от него
    # список означал бы форму с пустым обязательным полем. Заведение и правка
    # закрыты тем же `directory.manage`, что и остальные семь, — и в базе
    # (`0242`), и на экране.
    #
    # Адрес всё равно под префиксом справочников: это словарь, а не первичные
    # данные, и лежать ему рядом с остальными.
    path(
        "directory/counterparties/",
        counterparties_views.counterparties,
        name="directory-counterparties",
    ),
    path(
        "directory/counterparties/new/",
        counterparties_views.counterparty,
        name="directory-counterparty-new",
    ),
    path(
        "directory/counterparties/<uuid:counterparty_id>/",
        counterparties_views.counterparty,
        name="directory-counterparty",
    ),
    # Массовое изменение ставок (issue #181): индексация группе одним действием.
    # Постоянный адрес под префиксом групп, потому что и правит он группу целиком.
    path(
        "directory/groups/raise/",
        bulk_raise_views.raise_rates,
        name="directory-groups-raise",
    ),
    # Должности (issue #181). Восьмой справочник: шаблон условий найма, чтобы
    # человека заводили выбором из списка, а не набором семи полей.
    path("directory/positions/", positions_views.positions, name="directory-positions"),
    path("directory/positions/new/", positions_views.position, name="directory-position-new"),
    path(
        "directory/positions/<uuid:position_id>/",
        positions_views.position,
        name="directory-position",
    ),
    # Кассы (T145). Седьмой справочник, тот же префикс и то же право
    # `directory.manage`: касса — словарь, а не первичные данные.
    path("directory/tills/", tills_views.tills, name="directory-tills"),
    path("directory/tills/new/", tills_views.till, name="directory-till-new"),
    path("directory/tills/<uuid:till_id>/", tills_views.till, name="directory-till"),
    # Внесение расхода из кассы (T109). Не под префиксом справочников: это
    # внесение первичных данных, а не ведение словаря, и делают это все роли,
    # кроме администратора сети, — у которого как раз справочники и есть.
    #
    # Список расходов (T110) стоит первым: с него начинается работа со своими
    # тратами, а внесение — действие на нём. Постоянные адреса (`new/`) идут
    # раньше адреса по номеру записи, иначе `new` разбирался бы как номер.
    path("expenses/", expenses_views.expenses, name="expenses"),
    path("expenses/new/", cash_views.cash_expense, name="expense-new"),
    # Нераспределённое (T111): суммы без точки и пересчёт разнесения. Постоянный
    # адрес, поэтому стоит раньше адреса по номеру записи.
    path(
        "expenses/unallocated/",
        expenses_views.unallocated,
        name="expenses-unallocated",
    ),
    # Разнесение накладной руками (issue #174, модуль эталона 15). Запись — своим
    # адресом и только POST; номер строки приезжает в теле, потому что долей
    # много и форма всё равно отправляется целиком. Постоянный адрес стоит
    # раньше адреса по номеру записи, иначе `split` разбирался бы как номер.
    path("expenses/split/", expenses_views.split, name="expense-split"),
    path(
        "expenses/<uuid:fact_id>/split/",
        expenses_views.split_form,
        name="expense-split-form",
    ),
    # Карточка расхода: правка и удаление. Удаление — своим адресом и только
    # POST: это запись, и по ссылке из истории браузера случиться не должна.
    path("expenses/<uuid:fact_id>/", expenses_views.expense, name="expense"),
    path(
        "expenses/<uuid:fact_id>/delete/",
        expenses_views.expense_delete,
        name="expense-delete",
    ),
    # Чек расхода (T184): один адрес и на «показать», и на «приложить». Это одна
    # вещь — бумага этого расхода, — и разводить её по двум адресам значило бы,
    # что «где чек» и «куда его класть» отвечают разные страницы.
    path(
        "expenses/<uuid:fact_id>/receipt/",
        expenses_views.expense_receipt,
        name="expense-receipt",
    ),
    # Счета поставщиков (T151). Не под префиксом справочников: это первичные
    # данные, а не словарь. Список стоит первым — с него начинается работа со
    # счетами, а внесение это действие на нём. Постоянные адреса (`new/`) идут
    # раньше адреса по номеру записи, иначе `new` разбирался бы как номер.
    path("invoices/", suppliers_views.invoices, name="invoices"),
    path("invoices/new/", suppliers_views.invoice, name="invoice-new"),
    path("invoices/<uuid:document_id>/", suppliers_views.invoice, name="invoice"),
    # Оплата — свой адрес, а не поле в форме счёта: платёж это отдельное
    # событие со своей датой, и привязать его дату к правке счёта значило бы
    # стереть разницу, ради которой события разведены.
    path(
        "invoices/<uuid:document_id>/pay/",
        suppliers_views.invoice_pay,
        name="invoice-pay",
    ),
    # Оплата без счёта: расход признаётся датой денег, потому что бумаги нет.
    path("payments/new/", suppliers_views.payment_new, name="payment-new"),
    # Бумаги с точек (T174, D047): накладная и чек, принесённые управляющим.
    # Свой корень адреса, а не под `invoices/`: бумага — это ещё не счёт, и
    # адрес обязан говорить то же, что данные. К разделу «Счета» в шапке она при
    # этом принадлежит (`NAV_BELONGS` в `templatetags/ui.py`) — работа со счетами
    # начинается с неё.
    path("papers/", papers_views.paper_list, name="papers"),
    path("papers/new/", papers_views.paper_new, name="paper-new"),
    path("papers/<uuid:document_id>/", papers_views.paper, name="paper"),
    # Сам файл — своим адресом: карточка бумаги показывает его картинкой, а
    # PDF отдаётся на сохранение. Кому файл виден, решают политики базы.
    path(
        "papers/<uuid:document_id>/file/",
        papers_views.paper_file,
        name="paper-file",
    ),
    # Инбокс классификации (T152): строки без статьи одним списком. Разбор —
    # своим адресом и только POST: это запись денег, и по ссылке из истории
    # браузера случиться не должна.
    # Позиции счёта (T204, модуль эталона 3 «Разбор документа»): одна бумага —
    # несколько статей. Только POST и своим адресом: форма карточки правит первую
    # строку, и смешивать это с добавлением значит однажды переписать позицию
    # вместо новой.
    path(
        "invoices/<uuid:document_id>/positions/",
        suppliers_views.invoice_positions,
        name="invoice-positions",
    ),
    # «Не наша» (T205): бумага чужого юрлица уходит из очереди со следом.
    path(
        "invoices/<uuid:document_id>/not-ours/",
        suppliers_views.invoice_not_ours,
        name="invoice-not-ours",
    ),
    # P&L (issue #183, модуль эталона 5): отчёт месяца и раскрытие строки до
    # первичных фактов. Внутри периода, а не отдельным разделом: отчёт — это
    # срез месяца, и открывают его оттуда же, откуда считают.
    path(
        "periods/<uuid:period_id>/pnl/",
        reports_views.pnl,
        name="period-pnl",
    ),
    path(
        "periods/<uuid:period_id>/pnl/<slug:code>/",
        reports_views.pnl_line,
        name="period-pnl-line",
    ),
    path("inbox/", suppliers_views.inbox, name="inbox"),
    path(
        "inbox/<uuid:fact_id>/classify/",
        suppliers_views.inbox_classify,
        name="inbox-classify",
    ),
    # Разбор пачкой (issue #173): отмеченным строкам присваивается одна статья
    # за одно действие. Адрес без номера строки — их несколько, и они приезжают
    # в теле запроса.
    path("inbox/classify/", suppliers_views.inbox_batch, name="inbox-batch"),
    # Расходы по HTTP (T112). Отдельный префикс `api/`, а не те же адреса с
    # другим заголовком: два разных ответа на один адрес разъезжаются молча — и
    # разъезжаться будут именно там, где их никто не смотрит глазами. Роль и
    # тенант в адресах не участвуют вовсе: срез делает контекст базы, тот же,
    # что рисует страницы (спека, «API и будущая MCP-обёртка», условие 1).
    #
    # Разрез по регистру — `?ledger=` у списка, а не свой маршрут (условие 2).
    # Постоянные адреса идут раньше адреса по номеру записи, иначе `allocate`
    # разбирался бы как номер.
    path("api/expenses/", api.expenses, name="api-expenses"),
    path("api/expenses/unallocated/", api.unallocated, name="api-expenses-unallocated"),
    # Пересчёт разнесения: только POST и по одному месяцу за вызов (условие 5).
    path("api/expenses/allocate/", api.allocate, name="api-expenses-allocate"),
    path("api/expenses/<uuid:fact_id>/", api.expense, name="api-expense"),
    path(
        "api/expenses/<uuid:fact_id>/delete/",
        api.expense_delete,
        name="api-expense-delete",
    ),
    # Счета, платежи и инбокс по HTTP (T153). Тот же префикс `api/` и та же
    # обвязка, что у расходов (`web/api.py`): два набора правил о том, как
    # выглядит отказ и что такое ответ, разъехались бы молча — экран
    # проверяется смоуком, а вызов нет.
    #
    # Постоянные адреса идут раньше адреса по номеру записи, иначе `pay`
    # разбирался бы как номер.
    path("api/invoices/", suppliers_api.invoices, name="api-invoices"),
    path(
        "api/invoices/<uuid:document_id>/",
        suppliers_api.invoice,
        name="api-invoice",
    ),
    path(
        "api/invoices/<uuid:document_id>/pay/",
        suppliers_api.invoice_pay,
        name="api-invoice-pay",
    ),
    path("api/payments/", suppliers_api.payments, name="api-payments"),
    path("api/inbox/", suppliers_api.inbox, name="api-inbox"),
    path(
        "api/inbox/<uuid:fact_id>/classify/",
        suppliers_api.inbox_classify,
        name="api-inbox-classify",
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
    # ─────────────────────────────────────────────────────────
    #  Входы из шапки в экраны, которые живут внутри месяца (D064)
    #
    #  Пункт меню обязан вести куда-то без выбора периода, а ведомость, закрытие,
    #  P&L и сверка открываются из конкретного. Эти четыре адреса ведут в
    #  последний заведённый месяц; месяцев нет — в их список, где стоит кнопка
    #  «Завести месяц». Тупика не остаётся ни в одном случае.
    # ─────────────────────────────────────────────────────────
    path("payroll/sheet/", planned_views.payroll_sheet, name="payroll-sheet"),
    path("payroll/closing/", planned_views.closing, name="month-closing"),
    path("reports/pnl/", planned_views.pnl, name="reports-pnl"),
    path("reports/reconcile/", planned_views.reconcile, name="reports-reconcile"),
    # Экраны, которых ещё нет: маршруты собираются из того же списка, что и сами
    # страницы (`web/stages.py`), — иначе экран заводился бы в двух местах и
    # однажды был бы заведён в одном.
    *[
        path(
            screen.url,
            planned_views.planned,
            {"code": screen.code},
            name=f"planned-{screen.code}",
        )
        for screen in stages.planned_screens()
    ],
]
