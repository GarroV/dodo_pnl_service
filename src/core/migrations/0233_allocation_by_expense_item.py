"""Правило разнесения по статье расхода и статья у детей разнесения (T111).

**Зачем второй ключ у правила.** Схема разнесения (`0230_facts`) искала правило
по контрагенту: так приходят фактуры поставщиков — «счёт от EPS разносим
пропорционально выручке». У расхода, внесённого руками из кассы, контрагента
нет и взяться ему неоткуда: продукт пока не ведёт справочник контрагентов и не
принимает фактур. Человек выбирает **статью расхода** («аренда офиса», «реклама
на сеть»), и именно она отвечает на вопрос «как это разносить».

Поэтому у правила два возможных ключа и ровно один из них заполнен:
`counterparty_id` или `expense_item_id`. Проверку держит база
(`allocation_rules_one_key`), а не дисциплина в коде: сюда пишет и админка, и
завтрашний импорт фактур.

**Почему не отдельная таблица правил для статей.** Правило разнесения — это одна
и та же вещь независимо от того, чем оно вызвано: метод, точка для
`fixed_unit`, регистр и период действия. Две таблицы означали бы две копии
версионирования по датам и два места, куда смотрит `allocation_plan`, — то есть
ровно тот способ, которым правила расходятся молча.

**Непересечение версий — вторым ограничением.** Существующее
`allocation_rules_no_overlap` сравнивает контрагентов оператором `=`, а
`null = null` даёт null, то есть «не совпало»: правила по статье не мешали бы
друг другу вовсе. Второе `EXCLUDE` — по статье, регистру и периоду — закрывает
тот же инвариант с другой стороны: двух ответов на вопрос «как разносить аренду
в марте» быть не должно.

**Найденный дефект: разнесение теряло статью.** `allocate_fact` и
`reallocate_period` собирают ребёнка из полей родителя списком, и колонка
`facts.expense_item_id` появилась позже них (`0231_expense_items`) — в списке её
нет. То есть разнесённый расход становился безымянным в списке расходов и
переставал находиться фильтром по статье, при том что строка P&L оставалась на
месте и отчёт сходился: молчаливая потеря. Обе функции переписаны целиком (не
правятся, а переписываются — состав полей ребёнка и есть определение того, что
наследуется от родителя).

**Право вести правила — `directory.manage`**, той же формой политик, что у шести
справочников (`0130`, `0231`): ограничивающие политики только на запись, чтение
не трогаем. До этой миграции на `allocation_rules` стояла одна изоляция тенанта
— то есть переписать правило разнесения мог любой член тенанта. Экрана не было,
поэтому этого никто не делал; экран появился (правило ведётся в карточке статьи
расходов), и долг платится тем же способом, что в `0130`.
"""

import django.db.models.deletion
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import RangeOperators
from django.db import migrations, models

import core.models

COMMENTS = """
comment on column allocation_rules.counterparty_id is
    'Ключ правила для фактур поставщика. Пусто у правил по статье расхода: ключ ровно один';
comment on column allocation_rules.expense_item_id is
    'Ключ правила для расходов, внесённых руками: статья отвечает на вопрос «как разносить», когда контрагента нет';
"""

# --- поиск правила --------------------------------------------------------------
# Переписывается целиком: способ найти правило — это определение того, по чему
# разносится факт, и держать его половинками в двух миграциях нельзя.
PLAN = """
create or replace function allocation_plan(p_fact_id uuid)
returns table (unit_id uuid, share numeric, amount numeric, amount_report numeric, rule_id uuid)
language plpgsql stable
as $$
declare
    f facts;
    r allocation_rules;
    v_period_end date;
begin
    select * into f from facts where id = p_fact_id;
    if not found then
        raise exception 'факт % не найден', p_fact_id;
    end if;
    if f.counterparty_id is null and f.expense_item_id is null then
        return;    -- разносить не по чему: ни контрагента, ни статьи
    end if;

    v_period_end := (f.period + interval '1 month - 1 day')::date;

    -- Правило действует на период учёта, а не на дату документа: отчёт строится
    -- по периоду, разнесение должно жить в той же логике.
    --
    -- Ключей у правила два, и заполнен ровно один (`allocation_rules_one_key`).
    -- Контрагент старше: он приходит с фактурой, где статью человек не выбирал.
    -- Статья — ключ ручного расхода, у которого контрагента нет вовсе.
    --
    -- Регистр входит в поиск наравне с ключом. Так устроено само правило:
    -- ограничения непересечения разрешают одной статье и одному поставщику по
    -- два действующих правила — по одному на регистр, — потому что одна и та же
    -- трата бывает и официальной, и из кассы, и это разные строки P&L. Без
    -- условия по регистру `limit 1` выбирал бы произвольное из двух и молчал.
    select * into r
      from allocation_rules ar
     where ar.tenant_id = f.tenant_id
       and ar.ledger = f.ledger
       and (
             (f.counterparty_id is not null and ar.counterparty_id = f.counterparty_id)
             or (f.counterparty_id is null and ar.expense_item_id = f.expense_item_id)
           )
       and ar.valid_from <= f.period
       and (ar.valid_to is null or ar.valid_to > f.period)
     order by ar.valid_from desc
     limit 1;

    if not found or r.method = 'ask' then
        -- Правила нет или оно требует человека — плана нет, факт ждёт.
        return;
    end if;

    return query
    with base as (
        select u.id as unit_id,
               u.code as unit_code,
               case r.method
                   when 'fixed_unit' then 1::numeric
                   when 'even'       then 1::numeric
                   when 'by_revenue' then coalesce(rev.amount, 0)
               end as weight
          from units u
          left join lateral (
              -- Выручка точки за тот же период учёта, по действующим фактам
              select sum(x.amount) as amount
                from facts x
                join pnl_items pi on pi.id = x.pnl_item_id
               where x.tenant_id = f.tenant_id
                 and x.unit_id = u.id
                 and x.period = f.period
                 and x.superseded_at is null
                 and x.allocation <> 'split'
                 and pi.kind = 'revenue'
          ) rev on r.method = 'by_revenue'
         where u.tenant_id = f.tenant_id
           and (r.method <> 'fixed_unit' or u.id = r.unit_id)
           -- Фактура пришла на юрлицо — разносим только на его точки
           and (f.legal_entity_id is null or u.legal_entity_id = f.legal_entity_id)
           -- Точка должна работать в этом периоде
           and (u.opened_at is null or u.opened_at <= v_period_end)
           and (u.closed_at is null or u.closed_at >= f.period)
    ),
    kept as (
        -- Точка без выручки не получает долю при 'by_revenue'
        select * from base where weight > 0
    ),
    ordered as (
        select k.unit_id,
               k.weight,
               sum(k.weight) over (order by k.unit_code
                                   rows between unbounded preceding and current row) as cum,
               sum(k.weight) over () as total
          from kept k
    )
    -- Копейки. Округляем накопленную сумму и берём разность с предыдущей: так
    -- сумма детей всегда равна родителю до копейки, а распределение остатка
    -- детерминировано (по коду точки), а не зависит от порядка строк.
    select o.unit_id,
           round(o.weight / o.total, 6),
           round(f.amount * o.cum / o.total, 2)
               - round(f.amount * (o.cum - o.weight) / o.total, 2),
           case when f.amount_report is null then null
                else round(f.amount_report * o.cum / o.total, 2)
                     - round(f.amount_report * (o.cum - o.weight) / o.total, 2)
           end,
           r.id
      from ordered o
     where o.total > 0;
end $$;

comment on function allocation_plan(uuid) is
    'Каким должно быть разнесение факта по точкам. Правило ищется по контрагенту или по статье расхода — по тому ключу, который у факта есть';
"""

# Откат: тот же поиск, но только по контрагенту — как в `0230_facts`.
PLAN_BACK = PLAN.replace(
    """    if f.counterparty_id is null and f.expense_item_id is null then
        return;    -- разносить не по чему: ни контрагента, ни статьи
    end if;""",
    """    if f.counterparty_id is null then
        return;    -- без контрагента правило искать негде
    end if;""",
).replace(
    """       and (
             (f.counterparty_id is not null and ar.counterparty_id = f.counterparty_id)
             or (f.counterparty_id is null and ar.expense_item_id = f.expense_item_id)
           )""",
    """       and ar.counterparty_id = f.counterparty_id""",
)

# --- наследование полей ребёнком -------------------------------------------------
# Список полей в этих двух функциях — и есть определение того, что ребёнок
# наследует от родителя. Статья в нём отсутствовала: колонка появилась позже
# (`0231_expense_items`), а функции остались от `0230_facts`.
ALLOCATE = """
create or replace function allocate_fact(p_fact_id uuid)
returns int
language plpgsql
as $$
declare
    f facts;
    p record;
    v_unit_code text;
    n int := 0;
begin
    select * into f from facts where id = p_fact_id and superseded_at is null;
    if not found then
        raise exception 'факт % не найден или уже заменён', p_fact_id;
    end if;
    if f.allocation <> 'pending' then
        return 0;    -- уже разнесён или разносить нечего
    end if;

    -- Разносит тот, кто ведёт ВСЕ точки партнёра, и это правило базы, а не
    -- экрана. Причина не в правах, а в верности плана: `allocation_plan` читает
    -- `units` под политиками того, кто её позвал, и у роли, ограниченной своей
    -- точкой, список точек короче. Такой вызов не отказывал бы, а тихо клал на
    -- одну точку всю сумму сети — то есть два человека, нажавших одну и ту же
    -- кнопку, получали бы разное разнесение и не узнали бы об этом.
    --
    -- Условие спрашивает про контекст, а не только про список точек: без
    -- контекста приложения (обслуживание, миграция данных) ходит владелец схемы,
    -- у которого список точек не урезан вовсе, — и запрещать ему разнесение
    -- значило бы запретить обслуживание. Роли приложения без контекста политики
    -- и так не покажут ни одной точки: план выйдет пустым, а не половинным.
    if app_user_id() is not null and app_unit_ids(f.tenant_id) is not null then
        raise exception 'разнесение по точкам доступно тому, кто ведёт все точки партнёра'
            using errcode = '42501';
    end if;

    for p in select * from allocation_plan(f.id) loop
        select code into v_unit_code from units where id = p.unit_id;

        perform upsert_fact(jsonb_build_object(
            'tenant_id',          f.tenant_id,
            'period',             f.period,
            'doc_date',           f.doc_date,
            'unit_id',            p.unit_id,
            'legal_entity_id',    f.legal_entity_id,
            'pnl_item_id',        f.pnl_item_id,
            -- Статья наследуется вместе со строкой P&L: без неё разнесённый
            -- расход становится безымянным в списке расходов и не находится
            -- фильтром по статье, хотя отчёт при этом сходится.
            'expense_item_id',    f.expense_item_id,
            'ledger',             f.ledger,
            'counterparty_id',    f.counterparty_id,
            'amount',             p.amount,
            'currency',           f.currency,
            'amount_report',      p.amount_report,
            'report_currency',    f.report_currency,
            'fx_rate',            f.fx_rate,
            'fx_rate_date',       f.fx_rate_date,
            'title',              f.title,
            'note',               f.note,
            'channel',            f.channel,
            'source',             f.source,
            'source_ref',         f.source_ref,
            'document_id',        f.document_id,
            'line_no',            f.line_no,
            'batch_id',           f.batch_id,
            -- Ключ ребёнка выводится из ключа родителя: устойчив между
            -- пересчётами, поэтому повторный расчёт не плодит строки.
            'dedup_key',          f.dedup_key || '#' || v_unit_code,
            'allocation',         'allocated',
            'allocation_rule_id', p.rule_id,
            'allocation_share',   p.share,
            'parent_fact_id',     f.id
        ));
        n := n + 1;
    end loop;

    if n > 0 then
        update facts set allocation = 'split' where id = f.id;
    end if;

    return n;
end $$;

comment on function allocate_fact(uuid) is
    'Разнести ожидающий факт по точкам правилом. Родитель становится split и в P&L больше не считается';
"""

REALLOCATE = """
create or replace function reallocate_period(p_tenant uuid, p_period date)
returns int
language plpgsql
as $$
declare
    v_status period_status;
    f       facts;
    c       facts;
    p       record;
    v_action text;
    v_unit_code text;
    v_plan_count int;
    n int := 0;
begin
    select status into v_status from periods where tenant_id = p_tenant and period = p_period;
    if v_status = 'closed' then
        raise exception 'период % закрыт: пересчёт разнесения невозможен',
            to_char(p_period, 'YYYY-MM');
    end if;

    -- Тот же довод, что в `allocate_fact`: план строится по списку точек, а его
    -- видимость зависит от того, кто спрашивает.
    if app_user_id() is not null and app_unit_ids(p_tenant) is not null then
        raise exception 'пересчёт разнесения доступен тому, кто ведёт все точки партнёра'
            using errcode = '42501';
    end if;

    for f in
        select * from facts
         where tenant_id = p_tenant and period = p_period
           and superseded_at is null
           and allocation in ('pending', 'split')
         order by created_at, id
    loop
        select count(*) into v_plan_count from allocation_plan(f.id);

        if v_plan_count = 0 then
            -- Правило исчезло или снова требует человека: снимаем детей и
            -- возвращаем факт в ожидание, чтобы он не потерялся.
            if f.allocation = 'split' then
                for c in select * from facts
                          where parent_fact_id = f.id and superseded_at is null loop
                    perform supersede_fact(c.id);
                    n := n + 1;
                end loop;
                update facts set allocation = 'pending' where id = f.id;
            end if;
            continue;
        end if;

        -- Точки, которых в новом плане нет
        for c in
            select * from facts ch
             where ch.parent_fact_id = f.id and ch.superseded_at is null
               and not exists (select 1 from allocation_plan(f.id) pl where pl.unit_id = ch.unit_id)
        loop
            perform supersede_fact(c.id);
            n := n + 1;
        end loop;

        -- Остальные: upsert сам решит, менять или оставить как есть
        for p in select * from allocation_plan(f.id) loop
            select code into v_unit_code from units where id = p.unit_id;

            select action into v_action from upsert_fact(jsonb_build_object(
                'tenant_id',          f.tenant_id,
                'period',             f.period,
                'doc_date',           f.doc_date,
                'unit_id',            p.unit_id,
                'legal_entity_id',    f.legal_entity_id,
                'pnl_item_id',        f.pnl_item_id,
                'expense_item_id',    f.expense_item_id,
                'ledger',             f.ledger,
                'counterparty_id',    f.counterparty_id,
                'amount',             p.amount,
                'currency',           f.currency,
                'amount_report',      p.amount_report,
                'report_currency',    f.report_currency,
                'fx_rate',            f.fx_rate,
                'fx_rate_date',       f.fx_rate_date,
                'title',              f.title,
                'note',               f.note,
                'channel',            f.channel,
                'source',             f.source,
                'source_ref',         f.source_ref,
                'document_id',        f.document_id,
                'line_no',            f.line_no,
                'batch_id',           f.batch_id,
                'dedup_key',          f.dedup_key || '#' || v_unit_code,
                'allocation',         'allocated',
                'allocation_rule_id', p.rule_id,
                'allocation_share',   p.share,
                'parent_fact_id',     f.id
            ));

            if v_action <> 'unchanged' then
                n := n + 1;
            end if;
        end loop;

        if f.allocation = 'pending' then
            update facts set allocation = 'split' where id = f.id;
        end if;
    end loop;

    return n;
end $$;

comment on function reallocate_period(uuid, date) is
    'Пересчёт разнесения за период после правки правил. На неизменившихся правилах не меняет ничего; в закрытом месяце отказывает вслух';
"""

# Откат обеих функций — тот же текст без наследования статьи и примечания.
ALLOCATE_BACK = (
    ALLOCATE.replace("""
    -- Разносит тот, кто ведёт ВСЕ точки партнёра, и это правило базы, а не
    -- экрана. Причина не в правах, а в верности плана: `allocation_plan` читает
    -- `units` под политиками того, кто её позвал, и у роли, ограниченной своей
    -- точкой, список точек короче. Такой вызов не отказывал бы, а тихо клал на
    -- одну точку всю сумму сети — то есть два человека, нажавших одну и ту же
    -- кнопку, получали бы разное разнесение и не узнали бы об этом.
    --
    -- Условие спрашивает про контекст, а не только про список точек: без
    -- контекста приложения (обслуживание, миграция данных) ходит владелец схемы,
    -- у которого список точек не урезан вовсе, — и запрещать ему разнесение
    -- значило бы запретить обслуживание. Роли приложения без контекста политики
    -- и так не покажут ни одной точки: план выйдет пустым, а не половинным.
    if app_user_id() is not null and app_unit_ids(f.tenant_id) is not null then
        raise exception 'разнесение по точкам доступно тому, кто ведёт все точки партнёра'
            using errcode = '42501';
    end if;
""", "").replace(
        """            -- Статья наследуется вместе со строкой P&L: без неё разнесённый
            -- расход становится безымянным в списке расходов и не находится
            -- фильтром по статье, хотя отчёт при этом сходится.
            'expense_item_id',    f.expense_item_id,
""", "")
    .replace("""            'note',               f.note,\n""", "")
)
REALLOCATE_BACK = (
    REALLOCATE.replace("""
    -- Тот же довод, что в `allocate_fact`: план строится по списку точек, а его
    -- видимость зависит от того, кто спрашивает.
    if app_user_id() is not null and app_unit_ids(p_tenant) is not null then
        raise exception 'пересчёт разнесения доступен тому, кто ведёт все точки партнёра'
            using errcode = '42501';
    end if;
""", "").replace("""                'expense_item_id',    f.expense_item_id,\n""", "")
    .replace("""                'note',               f.note,\n""", "")
)

# --- право вести правила ---------------------------------------------------------
POLICIES = """
create policy directory_manage_insert on allocation_rules
    as restrictive for insert
    with check (app_has_permission(tenant_id, 'directory.manage'));

create policy directory_manage_update on allocation_rules
    as restrictive for update
    with check (app_has_permission(tenant_id, 'directory.manage'));

create policy directory_manage_delete on allocation_rules
    as restrictive for delete
    using (app_has_permission(tenant_id, 'directory.manage'));
"""

DROP_POLICIES = """
drop policy if exists directory_manage_delete on allocation_rules;
drop policy if exists directory_manage_update on allocation_rules;
drop policy if exists directory_manage_insert on allocation_rules;
"""


class Migration(migrations.Migration):

    dependencies = [
        # Явно от текущего листа блока: статьи расходов, к которым правило
        # привязывается.
        ("core", "0232_merge_expense_items"),
    ]

    operations = [
        migrations.AlterField(
            model_name="allocationrule",
            name="counterparty",
            field=models.ForeignKey(
                blank=True, db_column="counterparty_id", null=True,
                on_delete=django.db.models.deletion.CASCADE, to="core.counterparty",
            ),
        ),
        migrations.AddField(
            model_name="allocationrule",
            name="expense_item",
            field=models.ForeignKey(
                blank=True, db_column="expense_item_id", null=True,
                on_delete=django.db.models.deletion.PROTECT, to="core.expenseitem",
            ),
        ),
        migrations.AddConstraint(
            model_name="allocationrule",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(counterparty__isnull=True, expense_item__isnull=False)
                    | models.Q(counterparty__isnull=False, expense_item__isnull=True)
                ),
                name="allocation_rules_one_key",
            ),
        ),
        migrations.AddConstraint(
            model_name="allocationrule",
            constraint=ExclusionConstraint(
                name="allocation_rules_item_no_overlap",
                expressions=[
                    ("tenant", RangeOperators.EQUAL),
                    ("expense_item", RangeOperators.EQUAL),
                    ("ledger", RangeOperators.EQUAL),
                    (core.models.validity_range(), RangeOperators.OVERLAPS),
                ],
            ),
        ),
        migrations.RunSQL(COMMENTS, migrations.RunSQL.noop),
        migrations.RunSQL(PLAN, PLAN_BACK),
        migrations.RunSQL(ALLOCATE, ALLOCATE_BACK),
        migrations.RunSQL(REALLOCATE, REALLOCATE_BACK),
        migrations.RunSQL(POLICIES, DROP_POLICIES),
    ]
