"""Правки задним числом: разница переносится вперёд, закрытый месяц не трогают (T026).

**Что появляется.** Таблица переносов `retro_adjustments`, пометка на компоненте
выплаты (`pay_components.retro_source_period`), настройка тенанта
`tenants.retro_mode` и два правила базы, которыми закрыт двойной счёт.

**Почему перенос — своя таблица, а не строки прямо в ведомости.** Дельта это
**вход** периода-получателя, ровно как часы: `calc._store` сносит ведомости
расчёта и пишет заново, поэтому строка, приписанная к готовой ведомости, не
пережила бы первого же пересчёта. Хранится вход — материализуется при расчёте.
Это же снимает вопрос «а если получатель уже посчитан»: пересчитают — дельта
встанет на место, а до тех пор экран говорит об этом прямо.

**Почему пометка живёт колонкой `pay_components`, а не знанием одного экрана.**
Ведомость и строки P&L в этом продукте собираются из компонентов (T042). Дельта,
помеченная только на экране, для любого следующего потребителя выглядела бы как
обычная сумма июля — а это разница за июнь, и бухгалтер обязан видеть, за какой
месяц она пришла (D020).

**Два правила против двойного счёта, и оба здесь, а не в приложении.**

| что могло случиться | что этому мешает |
|---|---|
| источник пересчитали, а перенос остался жить | триггер `payruns_retro_cancel`: пересчёт источника отменяет его переносы |
| перенос отменили или подправили руками | политика `only_from_trigger`: `update` возможен только изнутри триггера |
| источник открыли заново, а дельта уже утверждена у получателя | триггер `payruns_retro_lock`: такой откат отклонён — деньги уже выплачены |

Отмена сделана триггером по той же причине, по которой триггером написан журнал
переходов в `0041`: тогда пересчёта источника **без** отмены переноса не бывает
в принципе, а не «пока каждый путь записи о ней помнит».

**Регистр удерживается политикой, а не кодом.** Перенос покомпонентный, а
регистр — свойство компонента, поэтому «в тот же регистр» получается по
построению; `ledger_visibility` здесь ровно та же, что на `pay_components`, и
бухгалтеру дополнительный регистр не покажет ни строкой, ни следом.

**Настройка тенанта, а не переменная окружения.** Партнёры ведут учёт по-разному
(D020), а страновая и партнёрская специфика в этом проекте живёт в конфигурации.
Настройка решает, какой путь продукт **предлагает** при расхождении, и не
отбирает права откатить период: обратимость гарантирована D021 отдельно.

**Чего эта миграция не делает.**

- Не трогает `payslip_totals`. Они описывают расчёт **этого** месяца, а налог и
  взносы июньской правки относятся к июньской декларации — сложить их в июльские
  итоги значило бы испортить то, по чему бухгалтер отчитывается.
- Не запрещает править входные данные закрытого месяца. Запрет решил бы задачу
  наоборот: по контракту блока правка задним числом обязана давать помеченную
  дельту, а не отказ.
- Не удаляет переносов. Отменённый перенос помечается: история, которую можно
  стереть, историей не является (то же решение, что у заморозки строки в `0050`).
"""
import django.db.models.deletion
from django.db import migrations, models

from core.fields import EnumField

# --- словарь домена -----------------------------------------------------------
# Нативный enum, как остальные словари продукта: чужое значение обязана
# отвергать база, а не приложение (см. шапку `core/fields.py`).
TYPES = """
create type retro_mode as enum ('delta', 'recalculate');

comment on type retro_mode is
    'Как партнёр ведёт правки задним числом: delta — разница переносится в текущий период, recalculate — период открывается заново и пересчитывается';
"""

DROP_TYPES = "drop type if exists retro_mode;"

COMMENTS = """
comment on column tenants.retro_mode is
    'Что продукт предлагает при расхождении закрытого месяца с сегодняшними данными: delta — перенести разницу вперёд, recalculate — открыть период заново и пересчитать. Права откатить период не отбирает ни то, ни другое (D021)';

comment on column pay_components.retro_source_period is
    'Пусто — обычная сумма этого месяца. Заполнено — это разница за указанный закрытый месяц, перенесённая сюда (T026)';

comment on table retro_adjustments is
    'Перенос разницы из закрытого месяца в текущий: вход периода-получателя, материализуется в pay_components при его расчёте';
comment on column retro_adjustments.source_period is
    'Закрытый месяц, за который посчитана разница';
comment on column retro_adjustments.target_period is
    'Месяц, в который разница переносится: первый после источника, чей расчёт не утверждён';
comment on column retro_adjustments.amount is
    'Разница, а не сумма: бывает отрицательной, если сегодняшние данные дают меньше';
comment on column retro_adjustments.ledger is
    'Тот же регистр, что у исходной строки. Не выводится и не выбирается: регистр — свойство компонента';
comment on column retro_adjustments.cancelled_at is
    'Перенос отменён: источник пересчитали, и разница потеряла смысл. Ставится триггером, руками не правится';
"""

# --- перенос ------------------------------------------------------------------
RULES = """
-- Внешние ключи ставятся руками по образцу `0041` и `0050`: Django действие
-- удаления в схему не пишет (каскад он исполняет в Python), а перенос обязан
-- исчезать вместе со своим партнёром и сотрудником любым путём.
alter table retro_adjustments
    add constraint retro_adjustments_employee_fk
    foreign key (employee_id) references employees (id) on delete cascade;

-- Есть ли у месяца неотменённые переносы, уже лежащие в утверждённом расчёте.
-- security definer по той же причине, что у `payslip_is_frozen()`: на самой
-- таблице висит RLS, и без него сторож читал бы переносы глазами того, кого он
-- ограничивает — невидимый перенос означал бы «переносов нет», то есть запрет
-- снимался бы сам собой у того, кто его не видит.
create or replace function retro_is_locked(p_tenant uuid, p_period date)
returns boolean
language sql stable security definer
set search_path = public
as $$
    select exists (
        select 1
          from retro_adjustments a
          join payruns p
            on p.tenant_id = a.tenant_id
           and p.period = a.target_period
         where a.tenant_id = p_tenant
           and a.source_period = p_period
           and a.cancelled_at is null
           and p.status = 'approved'
    )
$$;

comment on function retro_is_locked(uuid, date) is
    'Лежит ли разница за этот месяц в уже утверждённом периоде. На этом стоит запрет открывать месяц заново: деньги выплачены, пересчёт означал бы заплатить дважды';
"""

DROP_RULES = """
drop function if exists retro_is_locked(uuid, date);
alter table retro_adjustments drop constraint if exists retro_adjustments_employee_fk;
"""

# --- пересчёт источника отменяет перенос ---------------------------------------
CANCEL = """
create or replace function payrun_retro_cancel()
returns trigger
language plpgsql security definer
set search_path = public
as $$
begin
    -- Признак пересчёта — сдвинувшийся `calculated_at`. Именно он, а не статус:
    -- повторный расчёт статуса не меняет вовсе (см. `mark_calculated`), и по
    -- статусу пересчёт был бы незаметен.
    if new.calculated_at is distinct from old.calculated_at then
        update retro_adjustments
           set cancelled_at = now(),
               cancelled_reason = 'источник пересчитан: разница вошла в сам закрытый месяц'
         where tenant_id = new.tenant_id
           and source_period = new.period
           and cancelled_at is null;
    end if;
    return new;
end
$$;

comment on function payrun_retro_cancel() is
    'Пересчёт месяца отменяет разницы, перенесённые из него вперёд: иначе они посчитались бы дважды';

-- security definer здесь обязателен вдвойне: `force row level security`
-- действует и на владельца таблиц, поэтому владельцем функция быть должна, а
-- писать ей разрешает политика `only_from_trigger` — она пропускает запись
-- изнутри триггера и только оттуда.
create trigger payruns_retro_cancel
    after update on payruns
    for each row execute function payrun_retro_cancel();
"""

DROP_CANCEL = """
drop trigger if exists payruns_retro_cancel on payruns;
drop function if exists payrun_retro_cancel();
"""

# --- утверждённую дельту нельзя пересчитать -------------------------------------
LOCK = """
create or replace function payrun_retro_lock()
returns trigger
language plpgsql
as $$
begin
    -- Только откат: пока месяц утверждён, менять в нём и так нечего (`0041`).
    -- Ловится именно выход из `approved` — момент, после которого пересчёт
    -- становится возможным.
    if old.status = 'approved'
       and new.status is distinct from old.status
       and retro_is_locked(new.tenant_id, new.period)
    then
        raise exception
            'разница за этот месяц уже перенесена в утверждённый период: открывать его заново нельзя'
            using hint = 'перенесённая разница уже выплачена, пересчёт месяца означал бы заплатить дважды';
    end if;
    return new;
end
$$;

comment on function payrun_retro_lock() is
    'Не даёт открыть заново месяц, разница за который уже утверждена у получателя';

-- Имя выбрано так, чтобы сторож цикла (`payrun_guard`) отвечал раньше: у
-- `before`-триггеров одной таблицы Postgres соблюдает алфавитный порядок имён,
-- и о незаконном переходе человек должен читать раньше, чем о переносах.
-- Порядок здесь тот же, что заведён в `0050`: общее правило впереди частного.
create trigger payrun_retro_lock
    before update on payruns
    for each row execute function payrun_retro_lock();
"""

DROP_LOCK = """
drop trigger if exists payrun_retro_lock on payruns;
drop function if exists payrun_retro_lock();
"""

# --- кто что видит и кто вправе переносить -------------------------------------
# Те же слои, что у заморозки строки (`0050`): тенант, видимость, право.
POLICIES = """
alter table retro_adjustments enable row level security;
-- force обязателен: без него политики не действуют на владельца таблиц, а
-- миграции и обслуживание ходят как раз им.
alter table retro_adjustments force row level security;

create policy tenant_isolation on retro_adjustments
    for all
    using (tenant_id in (select app_tenant_ids()))
    with check (tenant_id in (select app_tenant_ids()));

-- Регистр учёта. Та же политика, что на `pay_components`: разница живёт в том
-- же регистре, что исходная строка, и бухгалтеру дополнительный регистр не
-- виден ни строкой, ни следом в итогах (D023).
create policy ledger_visibility on retro_adjustments
    as restrictive for select
    using (ledger = any (app_visible_ledgers(tenant_id)));

-- Свой ли это человек. Правило не переписывается, а зовётся: разъехавшиеся
-- копии одного правила и есть тот способ, которым доступ ломается незаметно
-- (D014). Управляющий видит переносы только по своим людям.
create policy unit_visibility on retro_adjustments
    as restrictive for all
    using (app_employee_is_visible(tenant_id, employee_id))
    with check (app_employee_is_visible(tenant_id, employee_id));

-- Форма как в `0022`: `with check` без `using`, чтобы отказ был громким
-- (`new row violates row-level security policy`), а не тихим «изменено 0 строк».
create policy retro_post_insert on retro_adjustments
    as restrictive for insert
    with check (app_has_permission(tenant_id, 'retro.post'));

-- Правится только отмена, и только триггером. Тот же приём, которым закрыт
-- журнал переходов в `0041`: перенос, который можно отменить руками, ничего не
-- гарантирует — отмена обязана быть следствием пересчёта, а не отдельным
-- действием, о котором надо помнить.
create policy only_from_trigger on retro_adjustments
    as restrictive for update
    using (pg_trigger_depth() > 0)
    with check (pg_trigger_depth() > 0);

-- Политики на `delete` нет намеренно: сторож ниже запрещает удаление целиком,
-- любым путём. Политика рядом с ним была бы вторым правилом об одном и том же.
"""

DROP_POLICIES = """
drop policy if exists only_from_trigger on retro_adjustments;
drop policy if exists retro_post_insert on retro_adjustments;
drop policy if exists unit_visibility on retro_adjustments;
drop policy if exists ledger_visibility on retro_adjustments;
drop policy if exists tenant_isolation on retro_adjustments;
alter table retro_adjustments no force row level security;
alter table retro_adjustments disable row level security;
"""

GUARD = """
create or replace function retro_adjustment_guard()
returns trigger
language plpgsql
as $$
declare
    probe retro_adjustments%rowtype;
begin
    if tg_op = 'DELETE' then
        -- Каскад от удаления сотрудника или партнёра приходит глубже первого
        -- уровня триггеров, прямое удаление — с первого. Тот же приём, что у
        -- журнала переходов (`0041`) и заморозки строки (`0050`).
        if pg_trigger_depth() > 1 then
            return old;
        end if;
        raise exception 'перенос разницы отменяется пометкой, а не удалением'
            using hint = 'отмена ставится сама при пересчёте месяца-источника';
    end if;

    -- Правится только отмена. Сумма, регистр и месяцы — запись о том, что было
    -- перенесено; переписанная задним числом, она врала бы правдоподобно.
    probe := new;
    probe.cancelled_at := old.cancelled_at;
    probe.cancelled_reason := old.cancelled_reason;
    if probe is distinct from old then
        raise exception 'перенос разницы: правится только отмена, прочие изменения отклонены';
    end if;
    return new;
end
$$;

comment on function retro_adjustment_guard() is
    'Перенос разницы: правится только отмена, удаляется только вместе с сотрудником или партнёром';

create trigger retro_adjustments_guard
    before update or delete on retro_adjustments
    for each row execute function retro_adjustment_guard();
"""

DROP_GUARD = """
drop trigger if exists retro_adjustments_guard on retro_adjustments;
drop function if exists retro_adjustment_guard();
"""

PRIVILEGES = """
grant select, insert, update on retro_adjustments to app_user;
"""

DROP_PRIVILEGES = """
revoke all on retro_adjustments from app_user;
"""


class Migration(migrations.Migration):

    dependencies = [
        ("core", "0051_merge_0047_queue_privileges_0050_payslip_freezing"),
    ]

    operations = [
        # Тип раньше колонки, которая на него ссылается.
        migrations.RunSQL(TYPES, DROP_TYPES),
        migrations.AddField(
            model_name="tenant",
            name="retro_mode",
            field=EnumField(db_default="delta", db_type_name="retro_mode"),
        ),
        migrations.AddField(
            model_name="paycomponent",
            name="retro_source_period",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="RetroAdjustment",
            fields=[
                ("id", models.UUIDField(db_default=models.Func(function="gen_random_uuid"), primary_key=True, serialize=False)),
                ("source_period", models.DateField()),
                ("target_period", models.DateField()),
                ("code", models.TextField()),
                ("title", models.TextField()),
                ("amount", models.DecimalField(decimal_places=2, max_digits=14)),
                ("ledger", EnumField(db_type_name="ledger")),
                ("channel", EnumField(db_default="bank", db_type_name="payout_channel")),
                ("taxable", models.BooleanField(db_default=True)),
                ("created_at", models.DateTimeField(db_default=models.Func(function="now"))),
                ("created_by", models.UUIDField(blank=True, null=True)),
                ("cancelled_at", models.DateTimeField(blank=True, null=True)),
                ("cancelled_reason", models.TextField(blank=True, db_default="")),
                ("employee", models.ForeignKey(db_column="employee_id", db_constraint=False, on_delete=django.db.models.deletion.DO_NOTHING, to="core.employee")),
                ("tenant", models.ForeignKey(db_column="tenant_id", on_delete=django.db.models.deletion.CASCADE, to="core.tenant")),
                ("unit", models.ForeignKey(blank=True, db_column="unit_id", null=True, on_delete=django.db.models.deletion.SET_NULL, to="core.unit")),
            ],
            options={
                "db_table": "retro_adjustments",
                "indexes": [
                    models.Index(models.F("tenant"), models.F("target_period"), name="retro_target_idx"),
                    models.Index(models.F("tenant"), models.F("source_period"), name="retro_source_idx"),
                ],
            },
        ),
        migrations.RunSQL(COMMENTS, migrations.RunSQL.noop),
        migrations.RunSQL(RULES, DROP_RULES),
        migrations.RunSQL(CANCEL, DROP_CANCEL),
        migrations.RunSQL(LOCK, DROP_LOCK),
        migrations.RunSQL(GUARD, DROP_GUARD),
        migrations.RunSQL(POLICIES, DROP_POLICIES),
        migrations.RunSQL(PRIVILEGES, DROP_PRIVILEGES),
    ]
