"""Касса как справочник, и регистр расхода следует из неё (T145, D039).

**Что меняется в замысле.** До этой миграции регистр учёта был свойством самой
траты: у формы расхода стояло поле «Регистр учёта» с умолчанием «официальный»
(Q013, вариант Б). Ответ владельца показал, что вопрос был задан не про то:
«Из кассы берем только официально. Но есть чёрная касса, где по дефолту идёт в
чёрную. Не понимаю вопроса». Человек на точке не выбирает регистр учёта — он
берёт деньги из одной коробки или из другой, и регистр следует из коробки.

Отсюда таблица `tills`: у точки может быть несколько касс, у кассы есть своя
точка и свой регистр. У факта появляется `till_id` — «из какой кассы платили»,
— и регистр приезжает из кассы умолчанием. Ручной выбор регистра при этом
остаётся: он перестаёт быть главным способом, но не исчезает (D039).

**Остаток по кассе не ведётся и вестись не будет** (D040, дословный ответ
владельца: «Кассу не трогаем нигде же? У нас сервис по сборке ПНЛ»). Поэтому
здесь нет ни прихода, ни сальдо, ни движения: касса — источник денег и признак
регистра, а не кассовая книга.

**Что видно роли.** Три ограничения на `tills`, все по образцу `facts`:

| политика | что делает |
|---|---|
| `tenant_isolation` | пермиссивная, как у всех таблиц с `tenant_id` |
| `unit_visibility` | `as restrictive`: касса чужой точки не видна и не заводится |
| `ledger_visibility` | `as restrictive`: касса регистра, которого роль не видит, для неё не существует |

Регистр в этом списке не лишний. Форма расхода не имеет права предлагать кассу,
из которой запись всё равно отвергнет `ledger_visibility` на `facts`: человек
выбрал бы её и получил отказ, не понимая, за что. Следствие у этого есть, и оно
названо вслух: управляющий точки видит два регистра из трёх (D031), поэтому
касса внутреннего регистра ему не видна вовсе. Это не решение этой миграции —
это D031, применённый к новому справочнику.

**Чужую кассу у факта отвергает база, а не форма.** Внешний ключ для этого не
годится: проверки внешних ключей в Postgres идут правами владельца ограничения и
политики обходят — то есть чужая касса прошла бы. Поэтому на `facts` встаёт
четвёртая ограничивающая политика `till_visibility`, а правило видимости кассы
живёт одной функцией `app_till_is_visible` и не переписывается на каждой таблице
(тот же довод, что у `app_unit_is_visible`).

**`facts_same` переписывается вместе с колонкой.** Функция отвечает на вопрос
«одно ли это событие по существу»; без `till_id` в ней смена **только** кассы не
считалась бы изменением, и правка расхода молча не применилась бы.

**Дети разнесения наследуют кассу.** `allocate_fact` и `reallocate_period`
собирают ребёнка из полей родителя списком, и колонка, забытая в этом списке,
теряется молча — так уже терялась статья расхода (`0233`). Список переписан
целиком, а не поправлен: он и есть определение того, что ребёнок наследует.
"""

import django.db.models.deletion
from django.db import migrations, models

import core.fields

# --- комментарии в самой базе -------------------------------------------------
COMMENTS = """
comment on table tills is
    'Кассы точек: коробки, из которых платят наличными. Регистр учёта расхода следует из кассы (D039). Остаток по кассе не ведётся (D040)';
comment on column tills.unit_id is
    'Точка, на которой стоит касса. Расход из этой кассы — расход этой точки';
comment on column tills.ledger is
    'Регистр учёта, в который по умолчанию попадает расход из этой кассы';
comment on column tills.code is
    'Код кассы, уникальный у партнёра';
comment on column tills.closed_at is
    'Касса закрывается датой, а не удалением: закрытые месяцы на неё ссылаются';
comment on column facts.till_id is
    'Из какой кассы платили. Пусто у всего, что мимо кассы: зарплата, выручка, фактуры — и у расходов, внесённых до появления справочника касс';
"""

# --- видимость кассы ------------------------------------------------------------
# Единственное место, где записано правило «видна ли роли эта касса». Ровно тем
# же приёмом, что `app_unit_is_visible`: правило, переписанное на каждой таблице
# по-своему, и есть способ потерять доступ незаметно (D014).
#
# `security definer` обязателен: функцию зовёт политика на `facts`, а строку
# кассы под политиками самой `tills` вызывающий может не увидеть — тогда ответ
# «не видно» приходил бы даже на свою кассу. Права владельца здесь ничего не
# открывают: функция не отдаёт строку, она отвечает «да/нет» по тем же двум
# правилам, которыми `tills` и закрыта.
VISIBILITY = """
create or replace function app_till_is_visible(p_tenant uuid, p_till uuid)
returns boolean
language sql stable security definer
set search_path = public
as $$
    select p_till is null      -- расход мимо кассы: скрывать нечего
        or exists (
            select 1
              from tills t
             where t.id = p_till
               and t.tenant_id = p_tenant
               and app_unit_is_visible(t.tenant_id, t.unit_id)
               and t.ledger = any (app_visible_ledgers(t.tenant_id))
        )
$$;

comment on function app_till_is_visible(uuid, uuid) is
    'Видна ли роли эта касса: своя точка и видимый регистр. Пустая касса видна всем — это расход мимо кассы';
"""

DROP_VISIBILITY = """
drop function if exists app_till_is_visible(uuid, uuid);
"""

# --- «одно ли это событие» ------------------------------------------------------
# Переписывается целиком: список колонок в этой функции и есть определение того,
# что считается изменением факта.
FACTS_SAME = """
create or replace function facts_same(a facts, b facts)
returns boolean
language sql immutable
as $$
    select (a.period, a.doc_date, a.unit_id, a.legal_entity_id, a.pnl_item_id,
            a.expense_item_id, a.till_id,
            a.ledger, a.counterparty_id, a.amount, a.currency, a.amount_report,
            a.report_currency, a.quantity, a.uom, a.title, a.note, a.channel,
            a.source, a.source_ref, a.document_id, a.line_no,
            a.allocation, a.allocation_rule_id, a.allocation_share, a.parent_fact_id)
        is not distinct from
           (b.period, b.doc_date, b.unit_id, b.legal_entity_id, b.pnl_item_id,
            b.expense_item_id, b.till_id,
            b.ledger, b.counterparty_id, b.amount, b.currency, b.amount_report,
            b.report_currency, b.quantity, b.uom, b.title, b.note, b.channel,
            b.source, b.source_ref, b.document_id, b.line_no,
            b.allocation, b.allocation_rule_id, b.allocation_share, b.parent_fact_id)
$$;

comment on function facts_same(facts, facts) is
    'Одно ли это событие по существу. Служебные поля не считаются изменением, иначе идемпотентности бы не было';
"""

# Откат — список колонок без кассы, тот, что оставила `0231_expense_items`.
FACTS_SAME_BACK = FACTS_SAME.replace("a.expense_item_id, a.till_id,", "a.expense_item_id,") \
                            .replace("b.expense_item_id, b.till_id,", "b.expense_item_id,")

# --- наследование кассы ребёнком -------------------------------------------------
# Обе функции переписываются целиком по тому же доводу, что в `0233`: список
# полей в них — определение того, что ребёнок наследует от родителя, и колонка,
# в него не попавшая, теряется молча.
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
    -- экрана: `allocation_plan` читает `units` под политиками того, кто её
    -- позвал, и у роли, ограниченной своей точкой, план выходит не половинным,
    -- а правдоподобным — вся сумма сети ложится на её единственную точку.
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
            -- Касса наследуется вместе со статьёй: она отвечает на вопрос
            -- «откуда взялись деньги», и у детей ответ тот же, что у родителя.
            'till_id',            f.till_id,
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
    """            -- Касса наследуется вместе со статьёй: она отвечает на вопрос
            -- «откуда взялись деньги», и у детей ответ тот же, что у родителя.
            'till_id',            f.till_id,
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
                'till_id',            f.till_id,
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

REALLOCATE_BACK = REALLOCATE.replace(
    """                'till_id',            f.till_id,\n""", "",
)


class Migration(migrations.Migration):

    dependencies = [
        # Явно от текущего листа: разведённые номера сами по себе от второго
        # листа миграций не спасают, а на этом проекте он расходился уже трижды.
        ("core", "0238_timesheet_edit_trace"),
    ]

    operations = [
        migrations.CreateModel(
            name="Till",
            fields=[
                ("id", models.UUIDField(db_default=models.Func(function="gen_random_uuid"), primary_key=True, serialize=False)),
                ("code", models.TextField()),
                ("title", models.TextField()),
                ("ledger", core.fields.EnumField(db_default="official", db_type_name="ledger")),
                ("closed_at", models.DateField(blank=True, null=True)),
                ("created_at", models.DateTimeField(db_default=models.Func(function="now"))),
                ("created_by", models.UUIDField(blank=True, null=True)),
                ("tenant", models.ForeignKey(db_column="tenant_id", on_delete=django.db.models.deletion.CASCADE, to="core.tenant")),
                ("unit", models.ForeignKey(db_column="unit_id", on_delete=django.db.models.deletion.PROTECT, to="core.unit")),
            ],
            options={
                "db_table": "tills",
            },
        ),
        migrations.AddConstraint(
            model_name="till",
            constraint=models.UniqueConstraint(fields=("tenant", "code"), name="tills_tenant_code_uniq"),
        ),
        migrations.AddField(
            model_name="fact",
            name="till",
            field=models.ForeignKey(blank=True, db_column="till_id", null=True, on_delete=django.db.models.deletion.PROTECT, to="core.till"),
        ),
        migrations.RunSQL(COMMENTS, migrations.RunSQL.noop),
        migrations.RunSQL(VISIBILITY, DROP_VISIBILITY),
        migrations.RunSQL(FACTS_SAME, FACTS_SAME_BACK),
        migrations.RunSQL(ALLOCATE, ALLOCATE_BACK),
        migrations.RunSQL(REALLOCATE, REALLOCATE_BACK),
        migrations.RunSQL(
            """
            alter table tills enable row level security;
            alter table tills force  row level security;

            create policy tenant_isolation on tills
                for all
                using (tenant_id in (select app_tenant_ids()))
                with check (tenant_id in (select app_tenant_ids()));

            -- `as restrictive` — иначе политика объединялась бы через OR и не
            -- сужала бы выборку вообще. `for all`, а не `for select`: без
            -- `with check` ограничение обходится вставкой.
            create policy unit_visibility on tills
                as restrictive for all
                using (app_unit_is_visible(tenant_id, unit_id))
                with check (app_unit_is_visible(tenant_id, unit_id));

            create policy ledger_visibility on tills
                as restrictive for all
                using (ledger = any (app_visible_ledgers(tenant_id)))
                with check (ledger = any (app_visible_ledgers(tenant_id)));

            -- Ведёт справочник тот же, кто ведёт остальные шесть (`0130`):
            -- ограничивающие политики только на запись, чтение не трогаем —
            -- кассу обязан видеть каждый, кто вносит расходы.
            create policy directory_manage_insert on tills
                as restrictive for insert
                with check (app_has_permission(tenant_id, 'directory.manage'));

            create policy directory_manage_update on tills
                as restrictive for update
                with check (app_has_permission(tenant_id, 'directory.manage'));

            create policy directory_manage_delete on tills
                as restrictive for delete
                using (app_has_permission(tenant_id, 'directory.manage'));

            -- Чужая касса у факта: внешний ключ её пропустит (проверки ключей
            -- идут правами владельца ограничения и политики обходят), поэтому
            -- отвергает её отдельная ограничивающая политика.
            create policy till_visibility on facts
                as restrictive for all
                using (app_till_is_visible(tenant_id, till_id))
                with check (app_till_is_visible(tenant_id, till_id));
            """,
            """
            drop policy if exists till_visibility on facts;
            drop policy if exists directory_manage_delete on tills;
            drop policy if exists directory_manage_update on tills;
            drop policy if exists directory_manage_insert on tills;
            drop policy if exists ledger_visibility on tills;
            drop policy if exists unit_visibility on tills;
            drop policy if exists tenant_isolation on tills;
            alter table tills no force row level security;
            alter table tills disable row level security;
            """,
        ),
        # Права роли продукта. Без них разграничение выглядело бы работающим, а
        # продукт не читал бы ничего: отказ по привилегии и отказ по политике —
        # разные вещи, и путать их нельзя.
        migrations.RunSQL(
            "grant select, insert, update, delete on tills to app_user;",
            "revoke all on tills from app_user;",
        ),
    ]
