<!-- СОБИРАЕТСЯ КОМАНДОЙ, РУКАМИ НЕ ПРАВИТЬ -->
<!-- Пересобрать: python manage.py routes --write -->

# Карта маршрутов

Адрес в браузере → имя маршрута → модуль, где лежит код. Нужна для одного:
понять, какой файл открыть, чтобы поправить конкретный экран, не вычитывая
проект целиком.

Собирается из резолвера Django командой `python manage.py routes`, то есть
описывает то, что продукт отдаёт на самом деле. Свежесть держит тест
`tests/test_routes_doc.py`.

| Адрес | Имя | Код |
|---|---|---|
| `/` | `index` | `web.views.index` |
| `/account/password/` | `password-change` | `web.views.password_change` |
| `/api/expenses/` | `api-expenses` | `web.api.expenses` |
| `/api/expenses/<uuid:fact_id>/` | `api-expense` | `web.api.expense` |
| `/api/expenses/<uuid:fact_id>/delete/` | `api-expense-delete` | `web.api.expense_delete` |
| `/api/expenses/allocate/` | `api-expenses-allocate` | `web.api.allocate` |
| `/api/expenses/unallocated/` | `api-expenses-unallocated` | `web.api.unallocated` |
| `/api/inbox/` | `api-inbox` | `web.suppliers_api.inbox` |
| `/api/inbox/<uuid:fact_id>/classify/` | `api-inbox-classify` | `web.suppliers_api.inbox_classify` |
| `/api/invoices/` | `api-invoices` | `web.suppliers_api.invoices` |
| `/api/invoices/<uuid:document_id>/` | `api-invoice` | `web.suppliers_api.invoice` |
| `/api/invoices/<uuid:document_id>/pay/` | `api-invoice-pay` | `web.suppliers_api.invoice_pay` |
| `/api/payments/` | `api-payments` | `web.suppliers_api.payments` |
| `/dev/login/` | `dev-login` | `web.views.dev_login` |
| `/dev/logout/` | `dev-logout` | `web.views.logout_page` |
| `/directory/` | `directory` | `web.directory_views.index` |
| `/directory/calendar/` | `directory-calendar` | `web.directory_views.calendar` |
| `/directory/calendar/<slug:month>/` | `directory-calendar-month` | `web.directory_views.calendar_month` |
| `/directory/calendar/new/` | `directory-calendar-new` | `web.directory_views.calendar_month` |
| `/directory/counterparties/` | `directory-counterparties` | `web.counterparties_views.counterparties` |
| `/directory/counterparties/<uuid:counterparty_id>/` | `directory-counterparty` | `web.counterparties_views.counterparty` |
| `/directory/counterparties/new/` | `directory-counterparty-new` | `web.counterparties_views.counterparty` |
| `/directory/employees/` | `directory-employees` | `web.directory_views.employees` |
| `/directory/employees/<uuid:employee_id>/` | `directory-employee` | `web.directory_views.employee` |
| `/directory/expense-items/` | `directory-expense-items` | `web.expense_items_views.expense_items` |
| `/directory/expense-items/<uuid:item_id>/` | `directory-expense-item` | `web.expense_items_views.expense_item` |
| `/directory/expense-items/new/` | `directory-expense-item-new` | `web.expense_items_views.expense_item` |
| `/directory/expense-items/upload/` | `directory-expense-items-upload` | `web.expense_items_views.expense_items_upload` |
| `/directory/groups/` | `directory-groups` | `web.directory_views.groups` |
| `/directory/groups/<uuid:group_id>/` | `directory-group` | `web.directory_views.group` |
| `/directory/groups/new/` | `directory-group-new` | `web.directory_views.group` |
| `/directory/legal-entities/` | `directory-legal-entities` | `web.directory_views.legal_entities` |
| `/directory/legal-entities/<uuid:entity_id>/` | `directory-legal-entity` | `web.directory_views.legal_entity` |
| `/directory/legal-entities/new/` | `directory-legal-entity-new` | `web.directory_views.legal_entity` |
| `/directory/tills/` | `directory-tills` | `web.tills_views.tills` |
| `/directory/tills/<uuid:till_id>/` | `directory-till` | `web.tills_views.till` |
| `/directory/tills/new/` | `directory-till-new` | `web.tills_views.till` |
| `/directory/units/` | `directory-units` | `web.directory_views.units` |
| `/directory/units/<uuid:unit_id>/` | `directory-unit` | `web.directory_views.unit` |
| `/directory/units/new/` | `directory-unit-new` | `web.directory_views.unit` |
| `/expenses/` | `expenses` | `web.expenses_views.expenses` |
| `/expenses/<uuid:fact_id>/` | `expense` | `web.expenses_views.expense` |
| `/expenses/<uuid:fact_id>/delete/` | `expense-delete` | `web.expenses_views.expense_delete` |
| `/expenses/new/` | `expense-new` | `web.cash_views.cash_expense` |
| `/expenses/unallocated/` | `expenses-unallocated` | `web.expenses_views.unallocated` |
| `/i18n/setlang/` | `set_language` | `django.views.i18n.set_language` |
| `/inbox/` | `inbox` | `web.suppliers_views.inbox` |
| `/inbox/<uuid:fact_id>/classify/` | `inbox-classify` | `web.suppliers_views.inbox_classify` |
| `/invoices/` | `invoices` | `web.suppliers_views.invoices` |
| `/invoices/<uuid:document_id>/` | `invoice` | `web.suppliers_views.invoice` |
| `/invoices/<uuid:document_id>/pay/` | `invoice-pay` | `web.suppliers_views.invoice_pay` |
| `/invoices/new/` | `invoice-new` | `web.suppliers_views.invoice` |
| `/login/` | `login` | `web.views.login_page` |
| `/logout/` | `logout` | `web.views.logout_page` |
| `/payments/new/` | `payment-new` | `web.suppliers_views.payment_new` |
| `/payslips/<uuid:payslip_id>/freeze/` | `payslip-freeze` | `web.views.payslip_freeze` |
| `/payslips/<uuid:payslip_id>/release/` | `payslip-release` | `web.views.payslip_release` |
| `/payslips/<uuid:payslip_id>/trace/` | `payslip-trace` | `web.views.payslip_trace` |
| `/periods/` | `periods` | `web.views.periods` |
| `/periods/<uuid:period_id>/` | `period` | `web.views.period_detail` |
| `/periods/<uuid:period_id>/approve/` | `period-approve` | `web.views.period_approve` |
| `/periods/<uuid:period_id>/calculate/` | `period-calculate` | `web.views.period_calculate` |
| `/periods/<uuid:period_id>/calculate/status/` | `period-calculate-status` | `web.views.period_calculate_status` |
| `/periods/<uuid:period_id>/export/<slug:kind>/` | `period-export` | `web.reports_views.period_export` |
| `/periods/<uuid:period_id>/reconcile/` | `period-reconcile` | `web.reports_views.period_reconcile` |
| `/periods/<uuid:period_id>/reopen/` | `period-reopen` | `web.views.period_reopen` |
| `/periods/<uuid:period_id>/retro/` | `period-retro` | `web.views.period_retro_post` |
| `/periods/<uuid:period_id>/variance/` | `period-variance` | `web.views.period_variance` |
| `/roles/` | `roles` | `web.roles_views.index` |
| `/roles/<uuid:role_id>/rights/` | `role-rights` | `web.roles_views.role_rights` |
| `/roles/people/<uuid:user_id>/` | `person-roles` | `web.roles_views.person_roles` |
| `/rules/` | `rules` | `web.rules_views.index` |
| `/rules/<str:path>/` | `rule` | `web.rules_views.rule` |
| `/timesheets/<uuid:period_id>/` | `timesheets` | `timesheets.views.grid` |
| `/timesheets/<uuid:period_id>/cell/` | `timesheet-cell` | `timesheets.views.cell` |
| `/timesheets/<uuid:period_id>/close/` | `timesheet-close` | `timesheets.views.close` |
| `/timesheets/<uuid:period_id>/import/` | `timesheet-import` | `timesheets.views.import_table` |
| `/timesheets/<uuid:period_id>/insured/` | `timesheet-insured` | `timesheets.views.insured` |
| `/timesheets/<uuid:period_id>/piece/` | `timesheet-piece` | `timesheets.views.piece` |
| `/timesheets/<uuid:period_id>/reopen/` | `timesheet-reopen` | `timesheets.views.reopen` |
