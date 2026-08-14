"""НДС у факта: сумма документа и ставка порознь, в P&L — без налога (T146, D042).

**Что решено.** Ответ владельца на Q016: «НДС нужен, да. Это вообще важная вещь
в Сербии и вообще. Хотя вообще в итоговом ПНЛ мы обычно не показываем НДС».
Отсюда два требования, и они не про одно и то же:

* **хранить обязательно** — сумму без налога иначе потом не восстановить;
* **показывать по умолчанию без налога** — но именно по умолчанию, а не всегда:
  партнёру, который НДС не зачитывает, нужна полная сумма.

**Что кладётся в схему.** Две колонки у факта:

| колонка | что значит |
|---|---|
| `vat_rate` | ставка налога в процентах, включённая в `amount`. Пусто — налога нет вовсе |
| `vat_amount` | сумма налога **внутри** `amount`. Считается из ставки, но хранится приколоченной |

`amount` при этом остаётся **суммой документа** — тем числом, которое человек
видел в чеке. Менять его смысл на «без налога» было бы тихой переоценкой всех
уже записанных строк и всех отчётов, которые на него смотрят.

**Почему сумма налога хранится, а не считается на лету.** Ровно по той же
причине, по какой к факту приколочен курс валюты (`fx_rate`): правило
округления — это правило, а правила у нас версионируются. Пересчёт «на лету»
через год дал бы другую копейку в уже закрытом месяце, и никто бы не понял,
откуда она взялась. Считает её при этом **база**, в `upsert_fact`, и это
единственное место: пришедший явно `vat_amount` (так однажды придёт фактура, где
налог указан суммой) уважается как есть.

**Дети разнесения делят налог тем же приёмом, что и сумму.** `allocation_plan`
получает колонку `vat_amount`, посчитанную через накопленную сумму с разностью,
— тем самым, которым уже делится `amount`. Считай ребёнок налог сам от своей
доли, 1200 с 20% на три точки дали бы 66,67 × 3 = 200,01 против 200,00 у
родителя, и сумма без НДС разошлась бы на копейку. Копейка, которой никто не
может объяснить, — это и есть способ потерять доверие к P&L.

**Отчёты получают `amount_net`.** Колонка `amount` в представлениях остаётся
суммой документа, а рядом встаёт сумма без налога — и «по умолчанию без НДС»
записано в самих отчётах, а не в каждом их потребителе по отдельности. Второй
потребитель, взявший не ту колонку, разошёлся бы с первым молча.

**Ставка ограничена базой** (`facts_vat_rate_range`), а не только формой: 150%
не бывает ни в одной стране, и запись такого значения по HTTP — это дефект, а не
ввод, который надо вежливо поправить.
"""

from django.db import migrations, models

# --- комментарии в самой базе -------------------------------------------------
COMMENTS = """
comment on column facts.vat_rate is
    'Ставка НДС в процентах, включённая в amount. Пусто — налога нет вовсе (D042)';
comment on column facts.vat_amount is
    'Сумма налога ВНУТРИ amount. Считается из ставки в upsert_fact и приколачивается к факту, как курс валюты';
"""

# --- правило: сколько налога внутри суммы ---------------------------------------
# Единственное место, где записано «как из суммы с налогом достать налог».
# Формула обратная (`× ставка / (100 + ставка)`), потому что `amount` — сумма
# документа, то есть налог в ней уже сидит.
VAT_OF = """
create or replace function vat_of(p_amount numeric, p_rate numeric)
returns numeric
language sql immutable
as $$
    select case
        when p_rate is null or p_amount is null then null
        else round(p_amount * p_rate / (100 + p_rate), 2)
    end
$$;

comment on function vat_of(numeric, numeric) is
    'Сколько налога внутри суммы документа по ставке. Одно место на продукт: пересчёт по-другому сдвинул бы закрытый месяц';
"""

DROP_VAT_OF = "drop function if exists vat_of(numeric, numeric);"

# --- «одно ли это событие» ------------------------------------------------------
# Список колонок здесь и есть определение изменения факта. Без ставки смена
# **только** её прошла бы как «то же самое», и правка молча не применилась бы.
FACTS_SAME = """
create or replace function facts_same(a facts, b facts)
returns boolean
language sql immutable
as $$
    select (a.period, a.doc_date, a.unit_id, a.legal_entity_id, a.pnl_item_id,
            a.expense_item_id, a.till_id,
            a.ledger, a.counterparty_id, a.amount, a.currency, a.amount_report,
            a.report_currency, a.vat_rate, a.vat_amount,
            a.quantity, a.uom, a.title, a.note, a.channel,
            a.source, a.source_ref, a.document_id, a.line_no,
            a.allocation, a.allocation_rule_id, a.allocation_share, a.parent_fact_id)
        is not distinct from
           (b.period, b.doc_date, b.unit_id, b.legal_entity_id, b.pnl_item_id,
            b.expense_item_id, b.till_id,
            b.ledger, b.counterparty_id, b.amount, b.currency, b.amount_report,
            b.report_currency, b.vat_rate, b.vat_amount,
            b.quantity, b.uom, b.title, b.note, b.channel,
            b.source, b.source_ref, b.document_id, b.line_no,
            b.allocation, b.allocation_rule_id, b.allocation_share, b.parent_fact_id)
$$;

comment on function facts_same(facts, facts) is
    'Одно ли это событие по существу. Служебные поля не считаются изменением, иначе идемпотентности бы не было';
"""

FACTS_SAME_BACK = FACTS_SAME.replace("a.report_currency, a.vat_rate, a.vat_amount,",
                                     "a.report_currency,") \
                            .replace("b.report_currency, b.vat_rate, b.vat_amount,",
                                     "b.report_currency,")

# --- единственная точка записи считает налог -------------------------------------
UPSERT = """
create or replace function upsert_fact(p_payload jsonb, out fact_id uuid, out action text)
language plpgsql
as $$
declare
    v_new facts;
    v_cmp facts;
    v_old facts;
begin
    v_new := jsonb_populate_record(null::facts, p_payload);

    if v_new.tenant_id is null or v_new.period is null or v_new.pnl_item_id is null
       or v_new.amount is null or v_new.source is null or v_new.dedup_key is null
       or v_new.title is null then
        raise exception 'upsert_fact: обязательны tenant_id, period, pnl_item_id, amount, source, dedup_key, title';
    end if;

    v_new.allocation := coalesce(v_new.allocation, 'direct');
    v_new.ledger     := coalesce(v_new.ledger, 'official');
    v_new.currency   := coalesce(v_new.currency,
                                 (select base_currency from tenants where id = v_new.tenant_id));
    v_new.report_currency := coalesce(v_new.report_currency,
                                 (select report_currency from tenants where id = v_new.tenant_id));

    -- Налог считается здесь и только здесь (T146). Источник шлёт ставку и сумму
    -- документа; сумму налога он вправе прислать явно — так придёт фактура, где
    -- налог указан отдельной строкой, — и тогда она уважается как есть.
    --
    -- Считается ОТ СУММЫ, а не копируется: сторно шлёт ту же ставку с
    -- отрицательной суммой, и налог у него обязан быть отрицательным. Копия
    -- положительного налога отменяла бы сумму и добавляла налог.
    if v_new.vat_amount is null and v_new.vat_rate is not null then
        v_new.vat_amount := vat_of(v_new.amount, v_new.vat_rate);
    end if;

    select * into v_old
      from facts
     where tenant_id = v_new.tenant_id and dedup_key = v_new.dedup_key
       and superseded_at is null;

    if found then
        -- Состояние разнесения ведёт система, а не источник. Коннектор всегда
        -- присылает «точка неизвестна», а факт к этому моменту мог быть уже
        -- разнесён. Без этой поправки каждая повторная выгрузка сбрасывала бы
        -- разнесение и заново плодила версии — то есть идемпотентности бы не было.
        v_cmp := v_new;
        if v_old.allocation = 'split' and v_new.allocation = 'pending' then
            v_cmp.allocation := 'split';
        end if;

        if facts_same(v_old, v_cmp) then
            fact_id := v_old.id;
            action  := 'unchanged';
            return;
        end if;

        v_new.id       := gen_random_uuid();
        v_new.revision := v_old.revision + 1;
        perform supersede_fact(v_old.id, v_new.id);
        action := 'updated';
    else
        v_new.id       := coalesce(v_new.id, gen_random_uuid());
        v_new.revision := 1;
        action := 'inserted';
    end if;

    v_new.created_at    := coalesce(v_new.created_at, now());
    v_new.created_by    := coalesce(v_new.created_by, app_user_id());
    v_new.superseded_at := null;
    v_new.superseded_by := null;

    insert into facts select (v_new).*;
    fact_id := v_new.id;
end $$;

comment on function upsert_fact(jsonb) is
    'Единственная точка записи факта: идемпотентность по dedup_key, версионирование заменой и расчёт НДС. Возвращает inserted | updated | unchanged';
"""

UPSERT_BACK = UPSERT.replace(
    """    if v_new.vat_amount is null and v_new.vat_rate is not null then
        v_new.vat_amount := vat_of(v_new.amount, v_new.vat_rate);
    end if;

""",
    "",
)

# --- план разнесения делит и налог -----------------------------------------------
# Сигнатура меняется (новая колонка), поэтому функция пересоздаётся целиком.
# Тело — то, что оставила `0236_allocation_reason`, плюс налог, посчитанный тем
# же приёмом «накопленная сумма минус предыдущая»: только так сумма детей равна
# родителю до копейки.
PLAN = """
drop function if exists allocation_plan(uuid);

create function allocation_plan(p_fact_id uuid)
returns table (unit_id uuid, share numeric, amount numeric, amount_report numeric,
               vat_amount numeric, rule_id uuid)
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

    v_period_end := (f.period + interval '1 month - 1 day')::date;

    r := allocation_rule_for(f.id);
    if r.id is null or r.method = 'ask' then
        -- Правила нет или оно требует человека — плана нет, факт ждёт.
        -- Почему именно ждёт, отвечает `allocation_reason`.
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
           and (f.legal_entity_id is null or u.legal_entity_id = f.legal_entity_id)
           and (u.opened_at is null or u.opened_at <= v_period_end)
           and (u.closed_at is null or u.closed_at >= f.period)
    ),
    kept as (
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
    -- детерминировано (по коду точки), а не зависит от порядка строк. Налог
    -- делится ровно так же — иначе сумма без НДС у детей разойдётся с
    -- родителем, и объяснить эту копейку будет нечем (T146).
    select o.unit_id,
           round(o.weight / o.total, 6),
           round(f.amount * o.cum / o.total, 2)
               - round(f.amount * (o.cum - o.weight) / o.total, 2),
           case when f.amount_report is null then null
                else round(f.amount_report * o.cum / o.total, 2)
                     - round(f.amount_report * (o.cum - o.weight) / o.total, 2)
           end,
           case when f.vat_amount is null then null
                else round(f.vat_amount * o.cum / o.total, 2)
                     - round(f.vat_amount * (o.cum - o.weight) / o.total, 2)
           end,
           r.id
      from ordered o
     where o.total > 0;
end $$;

comment on function allocation_plan(uuid) is
    'Каким должно быть разнесение факта по точкам. Ничего не пишет: пересчёт сравнивает план с тем, что есть';
"""

PLAN_BACK = PLAN.replace(
    """               vat_amount numeric, rule_id uuid)""", """               rule_id uuid)""",
).replace(
    """           case when f.vat_amount is null then null
                else round(f.vat_amount * o.cum / o.total, 2)
                     - round(f.vat_amount * (o.cum - o.weight) / o.total, 2)
           end,
""",
    "",
)

# --- ребёнок наследует ставку и получает свою долю налога -------------------------
# Список полей в этих двух функциях — определение того, что ребёнок наследует.
# Колонка, забытая в нём, теряется молча: так уже терялась статья расхода
# (`0233`) и чуть не потерялась касса (`0239`).
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
            'expense_item_id',    f.expense_item_id,
            'till_id',            f.till_id,
            'ledger',             f.ledger,
            'counterparty_id',    f.counterparty_id,
            'amount',             p.amount,
            'currency',           f.currency,
            'amount_report',      p.amount_report,
            'report_currency',    f.report_currency,
            -- Ставка наследуется, а сумма налога приезжает из плана посчитанной:
            -- посчитай её ребёнок сам от своей доли, сумма детей разошлась бы с
            -- родителем на копейку (T146).
            'vat_rate',           f.vat_rate,
            'vat_amount',         p.vat_amount,
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

ALLOCATE_BACK = ALLOCATE.replace(
    """            -- Ставка наследуется, а сумма налога приезжает из плана посчитанной:
            -- посчитай её ребёнок сам от своей доли, сумма детей разошлась бы с
            -- родителем на копейку (T146).
            'vat_rate',           f.vat_rate,
            'vat_amount',         p.vat_amount,
""",
    "",
)

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

        for c in
            select * from facts ch
             where ch.parent_fact_id = f.id and ch.superseded_at is null
               and not exists (select 1 from allocation_plan(f.id) pl where pl.unit_id = ch.unit_id)
        loop
            perform supersede_fact(c.id);
            n := n + 1;
        end loop;

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
                'till_id',            f.till_id,
                'ledger',             f.ledger,
                'counterparty_id',    f.counterparty_id,
                'amount',             p.amount,
                'currency',           f.currency,
                'amount_report',      p.amount_report,
                'report_currency',    f.report_currency,
                'vat_rate',           f.vat_rate,
                'vat_amount',         p.vat_amount,
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

REALLOCATE_BACK = REALLOCATE.replace(
    """                'vat_rate',           f.vat_rate,
                'vat_amount',         p.vat_amount,
""",
    "",
)

# --- отчёты знают, сколько тут налога --------------------------------------------
# Новые колонки добавляются В КОНЕЦ: `create or replace view` умеет только это,
# и зависимые представления при этом не приходится сносить.
VIEWS = """
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
       f.expense_item_id,
       f.vat_rate,
       f.vat_amount,
       -- `coalesce`, а не вычитание в лоб: null у строки без налога сделал бы
       -- null всю сумму без НДС, и в отчёте пропала бы вся строка целиком.
       f.amount - coalesce(f.vat_amount, 0) as amount_net
  from facts f
  join tenants t   on t.id = f.tenant_id
  join pnl_items i on i.id = f.pnl_item_id
  left join units u on u.id = f.unit_id
 where f.superseded_at is null
   and f.allocation <> 'split';

comment on view pnl_lines is
    'Действующие факты с раскрытыми справочниками. Основа всех отчётов: RLS работает через security_invoker';

create or replace view pnl_by_unit with (security_invoker = true) as
select tenant_id, period, unit_id, unit_code, unit_title,
       pnl_item_id, pnl_code, pnl_title, kind,
       sum(amount)        as amount,
       sum(amount_report) as amount_report,
       max(report_currency) as report_currency,
       count(*)           as fact_count,
       -- Сумма без НДС стоит рядом с суммой документа, а не вместо неё: в P&L
       -- по умолчанию едет она (D042), но полная сумма нужна тому, кто налог не
       -- зачитывает.
       sum(amount_net)    as amount_net,
       sum(coalesce(vat_amount, 0)) as vat_amount
  from pnl_lines
 where kind <> 'transfer'
 group by 1, 2, 3, 4, 5, 6, 7, 8, 9;

comment on view pnl_by_unit is 'P&L по точке и статье за период';

create or replace view pnl_by_network with (security_invoker = true) as
select tenant_id, period, pnl_item_id, pnl_code, pnl_title, kind,
       sum(amount)        as amount,
       sum(amount_report) as amount_report,
       max(report_currency) as report_currency,
       count(distinct unit_id) as unit_count,
       sum(amount_net)    as amount_net,
       sum(coalesce(vat_amount, 0)) as vat_amount
  from pnl_lines
 where kind <> 'transfer'
 group by 1, 2, 3, 4, 5, 6;

comment on view pnl_by_network is 'P&L по сети целиком: суммы сходятся с разрезом по точкам';
"""

# Откат представлений убирает колонки, а `create or replace` этого не умеет —
# значит пересоздаются все три подряд, сверху вниз.
VIEWS_BACK = """
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
       f.title,
       f.expense_item_id
  from facts f
  join tenants t   on t.id = f.tenant_id
  join pnl_items i on i.id = f.pnl_item_id
  left join units u on u.id = f.unit_id
 where f.superseded_at is null
   and f.allocation <> 'split';

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

create view pnl_by_network with (security_invoker = true) as
select tenant_id, period, pnl_item_id, pnl_code, pnl_title, kind,
       sum(amount)        as amount,
       sum(amount_report) as amount_report,
       max(report_currency) as report_currency,
       count(distinct unit_id) as unit_count
  from pnl_lines
 where kind <> 'transfer'
 group by 1, 2, 3, 4, 5, 6;

grant select on pnl_lines, pnl_by_unit, pnl_by_network to app_user;
"""

# --- готовый отчёт тоже отдаёт сумму без налога -----------------------------------
REPORT = """
drop function if exists pnl_report(uuid, date, uuid);

create function pnl_report(p_tenant uuid, p_period date, p_unit_id uuid default null)
returns table (
    pnl_item_id   uuid,
    code          text,
    title         text,
    kind          text,
    level         int,
    sort_path     text,
    amount        numeric,
    amount_report numeric,
    signed_amount numeric,
    amount_net    numeric
)
language sql stable
as $$
    with recursive tree as (
        select i.id, i.parent_id, i.code, i.title, i.kind, 1 as level,
               lpad(i.sort_order::text, 6, '0') || '.' || i.code as sort_path,
               array[i.id] as path
          from pnl_items i
         where i.parent_id is null
           and (i.tenant_id = p_tenant or i.tenant_id is null)
        union all
        select c.id, c.parent_id, c.code, c.title, c.kind, t.level + 1,
               t.sort_path || '/' || lpad(c.sort_order::text, 6, '0') || '.' || c.code,
               t.path || c.id
          from pnl_items c
          join tree t on c.parent_id = t.id
         where (c.tenant_id = p_tenant or c.tenant_id is null)
    ),
    own as (
        select l.pnl_item_id,
               sum(l.amount)        as amount,
               sum(l.amount_report) as amount_report,
               sum(case when l.kind = 'revenue' then l.amount else -l.amount end) as signed_amount,
               sum(l.amount_net)    as amount_net
          from pnl_lines l
         where l.tenant_id = p_tenant
           and l.period = p_period
           and l.kind <> 'transfer'
           and (p_unit_id is null or l.unit_id = p_unit_id)
         group by 1
    )
    select t.id, t.code, t.title, t.kind, t.level, t.sort_path,
           coalesce(sum(o.amount), 0),
           sum(o.amount_report),
           coalesce(sum(o.signed_amount), 0),
           coalesce(sum(o.amount_net), 0)
      from tree t
      left join tree d on t.id = any (d.path)
      left join own o on o.pnl_item_id = d.id
     group by t.id, t.code, t.title, t.kind, t.level, t.sort_path
     order by t.sort_path
$$;

comment on function pnl_report(uuid, date, uuid) is
    'P&L за период: дерево статей с подытогами. amount — сумма документа, amount_net — без НДС (D042). p_unit_id = null — по всей сети';
"""

REPORT_BACK = REPORT.replace(
    """    signed_amount numeric,
    amount_net    numeric
)""", """    signed_amount numeric
)""",
).replace(
    """               sum(case when l.kind = 'revenue' then l.amount else -l.amount end) as signed_amount,
               sum(l.amount_net)    as amount_net""",
    """               sum(case when l.kind = 'revenue' then l.amount else -l.amount end) as signed_amount""",
).replace(
    """           coalesce(sum(o.signed_amount), 0),
           coalesce(sum(o.amount_net), 0)""",
    """           coalesce(sum(o.signed_amount), 0)""",
)


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0239_tills"),
    ]

    operations = [
        migrations.AddField(
            model_name="fact",
            name="vat_rate",
            field=models.DecimalField(blank=True, decimal_places=3, max_digits=6, null=True),
        ),
        migrations.AddField(
            model_name="fact",
            name="vat_amount",
            field=models.DecimalField(blank=True, decimal_places=2, max_digits=18, null=True),
        ),
        migrations.AddConstraint(
            model_name="fact",
            constraint=models.CheckConstraint(
                condition=models.Q(vat_rate__isnull=True)
                | models.Q(vat_rate__gte=0, vat_rate__lte=100),
                name="facts_vat_rate_range",
            ),
        ),
        migrations.RunSQL(COMMENTS, migrations.RunSQL.noop),
        migrations.RunSQL(VAT_OF, DROP_VAT_OF),
        migrations.RunSQL(FACTS_SAME, FACTS_SAME_BACK),
        migrations.RunSQL(UPSERT, UPSERT_BACK),
        migrations.RunSQL(PLAN, PLAN_BACK),
        migrations.RunSQL(ALLOCATE, ALLOCATE_BACK),
        migrations.RunSQL(REALLOCATE, REALLOCATE_BACK),
        migrations.RunSQL(VIEWS, VIEWS_BACK),
        migrations.RunSQL(REPORT, REPORT_BACK),
    ]
