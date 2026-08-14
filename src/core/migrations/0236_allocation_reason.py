"""Почему факт ждёт разнесения — вопрос к базе, а не догадка представления (T132).

**Что чинилось.** Продукт объяснял ожидание одной фразой на все случаи —
«правила разнесения у статьи нет», — в том числе когда правило есть и лежит
рядом в `allocation_rules`. Так вели себя два способа разнесения из четырёх:
`by_revenue` (выручки в продукте нет вовсе — она придёт с коннектором Dodo IS)
и `ask` (точку по замыслу выбирает человек). Оба возвращали пустой план, а
пустой план читался представлением как «правила нет».

**Почему причина считается здесь, а не в представлении.** Ответ на вопрос «какое
правило действует для этого факта» уже есть — им пользуется `allocation_plan`.
Написать второй такой же поиск на стороне Django значило бы завести вторую копию
определения правила: она осталась бы верной по отдельности и разошлась бы с
оригиналом молча — ровно так в этом блоке уже дважды расходились копии одного
правила (`allocate_fact` терял статью, `allocation_plan` строил план по половине
точек). Поэтому поиск **вынесен** в отдельную функцию `allocation_rule_for`, и
обе — и план, и объяснение — зовут её.

**`refused` считается здесь по той же причине, что и запрет в `allocate_fact`.**
План строится по списку точек вызвавшего, а у роли, ограниченной своей точкой,
он короче. Причину, выведенную из такого плана, называть нельзя — она была бы
неверной («ни одна точка не подошла» вместо «разносить вправе не вы»). Условие
поэтому то же самое, слово в слово, что в `allocate_fact`.

**`facts_unallocated` получает статью расхода.** Экран нераспределённых показывал
`facts.title` — снимок названия статьи, снятый в момент внесения на языке
вносившего, — и «Аренда» стояла на английской странице рядом с `Rent` в списке
расходов и в выгрузке (T134). Название статьи выбирается по языку читателя
только из самой статьи, поэтому в представление добавляется её номер. Колонка
дописывается **в конец** (`create or replace view`), порядок прежних не меняется.
"""
from django.db import migrations

# --- поиск правила, вынесенный из плана ------------------------------------------
# Тело — дословно то, что стояло в `allocation_plan` (0233), включая доводы.
RULE_FOR = """
create or replace function allocation_rule_for(p_fact_id uuid)
returns allocation_rules
language plpgsql stable
as $$
declare
    f facts;
    r allocation_rules;
begin
    select * into f from facts where id = p_fact_id;
    if not found then
        return null;
    end if;
    if f.counterparty_id is null and f.expense_item_id is null then
        return null;    -- разносить не по чему: ни контрагента, ни статьи
    end if;

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

    if not found then
        return null;
    end if;
    return r;
end $$;

comment on function allocation_rule_for(uuid) is
    'Правило разнесения, действующее для факта. Один поиск на план и на объяснение: две копии разошлись бы молча';
"""

DROP_RULE_FOR = "drop function if exists allocation_rule_for(uuid);"

# Поиск правила, как он выглядел внутри плана до этой миграции. Нужен откату:
# без него откатанный план звал бы снесённую функцию.
LOOKUP_INLINE = """    if f.counterparty_id is null and f.expense_item_id is null then
        return;    -- разносить не по чему: ни контрагента, ни статьи
    end if;

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
        return;
    end if;"""

# --- план: то же самое, но поиск правила спрашивается функцией --------------------
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

CALL_RULE_FOR = """    r := allocation_rule_for(f.id);
    if r.id is null or r.method = 'ask' then
        -- Правила нет или оно требует человека — плана нет, факт ждёт.
        -- Почему именно ждёт, отвечает `allocation_reason`.
        return;
    end if;"""

# Откат плана: тот же текст, но с поиском правила внутри — как в `0233`.
PLAN_BACK = PLAN.replace(CALL_RULE_FOR, LOOKUP_INLINE)

# --- объяснение ------------------------------------------------------------------
REASON = """
create or replace function allocation_reason(p_fact_id uuid)
returns text
language plpgsql stable
as $$
declare
    f facts;
    r allocation_rules;
    v_rows int;
begin
    select * into f from facts where id = p_fact_id;
    if not found or f.superseded_at is not null or f.allocation <> 'pending' then
        return '';    -- ждать нечего: строки нет, она заменена или уже разнесена
    end if;

    r := allocation_rule_for(f.id);
    if r.id is null then
        return 'no_rule';
    end if;
    if r.method = 'ask' then
        return 'ask';
    end if;

    -- Тот же довод и то же условие, что в `allocate_fact`: план строится по
    -- списку точек вызвавшего, а он у роли, ограниченной своей точкой, короче.
    -- Причина, выведенная из такого плана, была бы неверной — «ни одна точка не
    -- подошла» вместо «разносить вправе не вы».
    if app_user_id() is not null and app_unit_ids(f.tenant_id) is not null then
        return 'refused';
    end if;

    select count(*) into v_rows from allocation_plan(f.id);
    if v_rows > 0 then
        return 'not_spread';    -- план есть, разнесения ещё не было
    end if;
    if r.method = 'by_revenue' then
        -- Выручки в продукте нет вовсе (коннектор Dodo IS — шестая очередь),
        -- поэтому план по ней пуст всегда, а не «в этот раз».
        return 'no_revenue';
    end if;
    return 'no_units';
end $$;

comment on function allocation_reason(uuid) is
    'Почему факт ждёт разнесения: no_rule | ask | no_revenue | no_units | refused | not_spread. Пусто — не ждёт';
"""

DROP_REASON = "drop function if exists allocation_reason(uuid);"

# --- нераспределённое знает свою статью -------------------------------------------
UNALLOCATED = """
create or replace view facts_unallocated with (security_invoker = true) as
select f.tenant_id, f.period, f.id as fact_id, f.title, f.amount, f.currency,
       f.counterparty_id, c.title as counterparty_title, f.document_id, f.source,
       f.expense_item_id
  from facts f
  left join counterparties c on c.id = f.counterparty_id
 where f.superseded_at is null and f.allocation = 'pending';

comment on view facts_unallocated is
    'Суммы без точки: что мешает закрыть месяц. Факт без правила обязан быть видимым, а не исчезать';
"""

# Откат: колонку `create or replace view` убрать не умеет, поэтому представление
# пересоздаётся — а вместе с ним возвращаются права и комментарий, которые
# `drop view` уносит с собой.
UNALLOCATED_BACK = UNALLOCATED.replace(
    """       f.counterparty_id, c.title as counterparty_title, f.document_id, f.source,
       f.expense_item_id""",
    """       f.counterparty_id, c.title as counterparty_title, f.document_id, f.source""",
).replace(
    "create or replace view", "drop view facts_unallocated;\ncreate view"
) + """
grant select on facts_unallocated to app_user;
"""


class Migration(migrations.Migration):
    """Откат возвращает план ровно к телу из `0233`, а представление — к `0230`."""

    dependencies = [("core", "0235_pnl_lines_expense_item")]

    operations = [
        migrations.RunSQL(RULE_FOR, DROP_RULE_FOR),
        migrations.RunSQL(PLAN, PLAN_BACK),
        migrations.RunSQL(REASON, DROP_REASON),
        migrations.RunSQL(UNALLOCATED, UNALLOCATED_BACK),
    ]
