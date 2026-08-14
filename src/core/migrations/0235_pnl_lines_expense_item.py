"""Статья расхода становится видна в `pnl_lines` (T113).

**Что чинится.** `pnl_lines` — каноническое определение строки P&L: действующие
факты с раскрытыми справочниками, без заменённых версий и без родителей
разнесения (`split`). На нём стоят все остальные представления и весь отчёт.
Колонка `facts.expense_item_id` появилась позже него (`0231_expense_items`), и в
представление её никто не добавил.

**Чем это мешает.** Выгрузка «Строки для P&L» обязана называть, **за что**
потрачены деньги: «Коммунальные / вода», а не просто «Коммунальные». Статья P&L
у расхода одна на десяток трат, и файл без второй половины названия для сборки
P&L бесполезен ровно так же, как файл без расходов.

Взять её мимо представления — значит переписать в выборке отчёта условия
«действующая версия» и «не родитель разнесения». Это ровно тот способ, которым
уже дважды разъезжались копии одного правила в этом блоке (`allocate_fact`
терял статью, `allocation_plan` строил план по половине точек): копия остаётся
верной по отдельности и расходится с оригиналом молча. Поэтому колонка
добавляется в само представление.

**Почему `create or replace`, а не пересоздание.** На `pnl_lines` смотрят три
представления и функция отчёта; `drop … cascade` снёс бы их вместе с правами и
комментариями. `create or replace view` разрешает дописать колонку **в конец**,
не трогая существующие, — что и сделано: порядок и типы прежних колонок не
меняются, поэтому зависимые объекты остаются как были.
"""
from django.db import migrations

# Определение целиком, а не «дописать колонку»: у представления нет
# инкрементального изменения — `create or replace` требует полного тела. Тело
# скопировано из `0230_facts` без единой правки, кроме последней строки.
PNL_LINES = """
create or replace view pnl_lines with (security_invoker = true) as
select f.id           as fact_id,
       f.tenant_id,
       f.period,
       f.doc_date,
       f.unit_id,
       u.code         as unit_code,
       u.title        as unit_title,
       f.legal_entity_id,
       f.pnl_item_id,
       i.code         as pnl_code,
       i.title        as pnl_title,
       i.kind,
       f.ledger,
       f.counterparty_id,
       f.allocation,
       f.allocation_rule_id,
       f.source,
       f.channel,
       f.amount,
       f.currency,
       coalesce(
           f.amount_report,
           round(f.amount * fx_rate_on(f.currency, t.report_currency,
                                       (f.period + interval '1 month - 1 day')::date), 2)
       )              as amount_report,
       t.report_currency,
       f.document_id,
       f.title,
       -- Новое: чем человек назвал трату, когда вносил её (T108). Название
       -- показывается на языке читателя, поэтому наружу уезжает ссылка, а не
       -- готовая строка: снимок `f.title` заморожен на языке исходника.
       f.expense_item_id
  from facts f
  join tenants t   on t.id = f.tenant_id
  join pnl_items i on i.id = f.pnl_item_id
  left join units u on u.id = f.unit_id
 where f.superseded_at is null
   and f.allocation <> 'split';
"""

# Откат — то же тело без последней колонки. `create or replace` убрать колонку не
# умеет, поэтому представление пересоздаётся, а с ним и два зависимых — в том же
# виде, в каком их оставила `0230_facts`. `facts_unallocated` в списке нет
# намеренно: оно читает `facts` напрямую и от `pnl_lines` не зависит.
BACK = """
drop view if exists pnl_by_network;
drop view if exists pnl_by_unit;
drop view if exists pnl_lines;

create view pnl_lines with (security_invoker = true) as
select f.id           as fact_id,
       f.tenant_id,
       f.period,
       f.doc_date,
       f.unit_id,
       u.code         as unit_code,
       u.title        as unit_title,
       f.legal_entity_id,
       f.pnl_item_id,
       i.code         as pnl_code,
       i.title        as pnl_title,
       i.kind,
       f.ledger,
       f.counterparty_id,
       f.allocation,
       f.allocation_rule_id,
       f.source,
       f.channel,
       f.amount,
       f.currency,
       coalesce(
           f.amount_report,
           round(f.amount * fx_rate_on(f.currency, t.report_currency,
                                       (f.period + interval '1 month - 1 day')::date), 2)
       )              as amount_report,
       t.report_currency,
       f.document_id,
       f.title
  from facts f
  join tenants t   on t.id = f.tenant_id
  join pnl_items i on i.id = f.pnl_item_id
  left join units u on u.id = f.unit_id
 where f.superseded_at is null
   and f.allocation <> 'split';

comment on view pnl_lines is
    'Действующие факты с раскрытыми справочниками. Основа всех отчётов: RLS работает через security_invoker';

create view pnl_by_unit with (security_invoker = true) as
select tenant_id, period, unit_id, unit_code, unit_title,
       pnl_item_id, pnl_code, pnl_title, kind,
       sum(amount)        as amount,
       sum(amount_report) as amount_report,
       max(report_currency) as report_currency,
       count(*)           as fact_count
  from pnl_lines
 where kind <> 'transfer'
 group by 1, 2, 3, 4, 5, 6, 7, 8, 9;

comment on view pnl_by_unit is 'P&L по точке и статье за период';

create view pnl_by_network with (security_invoker = true) as
select tenant_id, period, pnl_item_id, pnl_code, pnl_title, kind,
       sum(amount)        as amount,
       sum(amount_report) as amount_report,
       max(report_currency) as report_currency,
       count(distinct unit_id) as unit_count
  from pnl_lines
 where kind <> 'transfer'
 group by 1, 2, 3, 4, 5, 6;

comment on view pnl_by_network is 'P&L по сети целиком: суммы сходятся с разрезом по точкам';

grant select on pnl_lines, pnl_by_unit, pnl_by_network to app_user;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0234_merge_queue_role_and_allocation"),
    ]

    operations = [
        migrations.RunSQL(PNL_LINES, BACK),
    ]
