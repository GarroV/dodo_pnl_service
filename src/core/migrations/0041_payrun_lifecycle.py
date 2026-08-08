"""Жизненный цикл расчёта периода: переходы, заморозка, журнал (T023).

**Что здесь появляется.** Статусы расчёта перестают быть надписью: разрешённых
переходов ровно четыре, всё остальное отвергает база, утверждённый расчёт
не меняется ни одним путём записи, а каждый переход попадает в журнал.

**Почему триггеры, а не политики RLS.** Политики не действуют на
суперпользователя вообще, а без `force` — и на владельца таблиц. Для видимости
это приемлемо (администрирование данных — сид, дамп, обслуживание), а для
«утверждённое не меняется» — нет: гарантия держалась бы на том, каким
пользователем подключились. Триггер действует на всех, включая суперпользователя.

**Разрешённые переходы.**

| откуда | куда | когда |
|---|---|---|
| — (создание) | `draft` | завели расчёт |
| `draft` | `calculated` | расчёт прошёл |
| `calculated` | `approved` | утвердили (T025) |
| `approved` | `reopened` | откат (T025) |
| `reopened` | `calculated` | пересчитали после отката |

- `reopened` не схлопывается в `draft`: иначе по состоянию не отличить «ещё не
  считали» от «утверждали и открыли обратно», а открытие периода обязано быть
  видно.
- `reopened → approved` запрещён намеренно. Открыли — пересчитайте, прежде чем
  утверждать снова: иначе повторное утверждение накрыло бы числа, посчитанные
  до правки входных данных, и расхождение уехало бы в закрытый период молча.
  Лишний шаг дешевле неверного утверждённого месяца.
- **Пересчёт переходом не является.** Он двигает `calculated_at` и оставляет
  статус, поэтому правило разделено надвое: статус не меняется — можно всё,
  кроме как в утверждённом расчёте; статус меняется — только по таблице выше.
- `paid` в типе есть с самого начала, экрана выплаты нет ни в одной задаче
  очереди. Переход в него отвергается, а не пропускается «на будущее»: догадка
  о ненаписанном экране — это второй источник истины о том, как устроен цикл.

**Один черновик на период** отдельным ограничением не заводится: пара «тенант +
месяц» уникальна с самого начала (`payruns_tenant_period_uniq`), то есть расчёт
за период вообще один — черновик тем более. Частичный индекс `where status =
'draft'` не сработал бы никогда, а мёртвое ограничение врёт следующему читателю
не меньше устаревшей доки.

**Чего эта миграция не делает.**

- **Не морозит `timesheets`.** Правка входных данных задним числом — предмет
  T026, и там она обязана давать помеченную дельту, а не отказ.
- **Не заводит политик на права `period.approve` / `period.reopen`.** Запись в
  `payruns` сегодня закрыта правом `payrun.calculate` (миграция `0022`), то есть
  утвердить может тот, кто умеет считать. Это меньше, чем нужно, и это часть
  T025 — вместе с её экраном, как и требует шапка `0022`: политика, стоящая
  там, где никто не пишет, — не защита, а догадка.

**Нумерация с 0040** — третья очередь строится параллельно несколькими блоками
в разных копиях репозитория, номера разведены заранее (`0030+` — `timesheets`,
`0040+` — `payrun`). Два листа в графе миграций Django ломают слияние.
"""
import django.db.models.deletion
from django.db import migrations, models

import core.fields

# --- правило переходов -------------------------------------------------------
# Одно место, откуда его берут и триггер, и приложение. Второй список в Python
# разъехался бы с этим молча — и интерфейс предлагал бы то, что база отвергнет.
RULES = """
create or replace function payrun_next_statuses(p_from payrun_status)
returns payrun_status[]
language sql immutable
as $$
    select case p_from
        when 'draft'      then array['calculated']::payrun_status[]
        when 'calculated' then array['approved']::payrun_status[]
        when 'approved'   then array['reopened']::payrun_status[]
        when 'reopened'   then array['calculated']::payrun_status[]
        else '{}'::payrun_status[]
    end
$$;

comment on function payrun_next_statuses(payrun_status) is
    'Куда расчёт периода может перейти из этого статуса. Единственный источник истины о цикле';

-- Заморожен ли расчёт. Условие написано «от обратного» намеренно: невидимый
-- расчёт считается замороженным. Прямое `status = approved` при невидимой
-- строке дало бы false, то есть **разрешило** бы запись — отказ должен быть
-- стороной ошибки по умолчанию. Заодно про чужой расчёт ничего не узнать: он
-- отвечает «заморожен» независимо от настоящего статуса.
create or replace function payrun_is_frozen(p_payrun uuid)
returns boolean
language sql stable
set search_path = public
as $$
    select not exists (
        select 1 from payruns where id = p_payrun and status <> 'approved'
    )
$$;

comment on function payrun_is_frozen(uuid) is
    'Утверждён ли расчёт (или недоступен). На этом стоит запрет записи в утверждённый период';
"""

DROP_RULES = """
drop function if exists payrun_is_frozen(uuid);
drop function if exists payrun_next_statuses(payrun_status);
"""

# --- сторож самого расчёта ---------------------------------------------------
GUARD = """
create or replace function payrun_guard()
returns trigger
language plpgsql
as $$
declare
    probe payruns%rowtype;
begin
    if tg_op = 'INSERT' then
        if new.status <> 'draft' then
            raise exception
                'расчёт периода заводится черновиком, статус «%» при создании отклонён',
                new.status
                using hint = 'переводите по одному: draft → calculated → approved';
        end if;
        return new;
    end if;

    if tg_op = 'DELETE' then
        if old.status = 'approved' then
            raise exception 'период утверждён: удаление расчёта отклонено'
                using hint = 'откройте период заново, если расчёт надо менять';
        end if;
        return old;
    end if;

    -- Статус не меняется — это не переход, а обычная правка (пересчёт двигает
    -- calculated_at). В утверждённом расчёте её не бывает.
    if new.status is not distinct from old.status then
        if old.status = 'approved' then
            raise exception 'период утверждён: изменение расчёта отклонено'
                using hint = 'откройте период заново, если расчёт надо менять';
        end if;
        return new;
    end if;

    if not (new.status = any (payrun_next_statuses(old.status))) then
        raise exception 'переход расчёта «%» → «%» не разрешён', old.status, new.status
            using hint = 'разрешены: draft → calculated → approved → reopened → calculated';
    end if;

    -- Уход из утверждённого — только смена статуса. Правка «заодно» прошла бы
    -- мимо заморозки: одним оператором и открыли, и переписали.
    if old.status = 'approved' then
        probe := new;
        probe.status := old.status;
        if probe is distinct from old then
            raise exception 'период утверждён: откат меняет только статус, прочие правки отклонены';
        end if;
    end if;

    return new;
end
$$;

comment on function payrun_guard() is
    'Легальность перехода расчёта и неизменность утверждённого. Триггер, а не политика: действует и на суперпользователя';

create trigger payruns_guard
    before insert or update or delete on payruns
    for each row execute function payrun_guard();
"""

DROP_GUARD = """
drop trigger if exists payruns_guard on payruns;
drop function if exists payrun_guard();
"""

# --- заморозка данных утверждённого расчёта ----------------------------------
# Две функции вместо одной с аргументом: у ведомости ключ расчёта свой, у итогов
# и компонентов — через строку ведомости. Разбор имени колонки в общей функции
# читался бы хуже, чем два коротких сторожа.
FREEZE = """
create or replace function payrun_frozen_by_payrun()
returns trigger
language plpgsql
as $$
declare
    v_payrun uuid;
begin
    if tg_op = 'DELETE' then
        v_payrun := old.payrun_id;
    else
        v_payrun := new.payrun_id;
    end if;

    if payrun_is_frozen(v_payrun) then
        raise exception 'период утверждён: % в «%» отклонён', tg_op, tg_table_name
            using hint = 'откройте период заново, если данные расчёта надо менять';
    end if;

    if tg_op = 'DELETE' then
        return old;
    end if;
    return new;
end
$$;

create or replace function payrun_frozen_by_payslip()
returns trigger
language plpgsql
as $$
declare
    v_payslip uuid;
    v_payrun uuid;
begin
    if tg_op = 'DELETE' then
        v_payslip := old.payslip_id;
    else
        v_payslip := new.payslip_id;
    end if;

    select payrun_id into v_payrun from payslips where id = v_payslip;

    -- Строку ведомости не видно — расчёт считается замороженным: см. довод
    -- у payrun_is_frozen(), отказ должен быть стороной ошибки по умолчанию.
    if payrun_is_frozen(v_payrun) then
        raise exception 'период утверждён: % в «%» отклонён', tg_op, tg_table_name
            using hint = 'откройте период заново, если данные расчёта надо менять';
    end if;

    if tg_op = 'DELETE' then
        return old;
    end if;
    return new;
end
$$;

comment on function payrun_frozen_by_payrun() is
    'Запрещает запись в утверждённый расчёт: таблицы, где ключ расчёта в самой строке';
comment on function payrun_frozen_by_payslip() is
    'Запрещает запись в утверждённый расчёт: таблицы, привязанные к строке ведомости';

create trigger payslips_frozen
    before insert or update or delete on payslips
    for each row execute function payrun_frozen_by_payrun();

create trigger payslip_totals_frozen
    before insert or update or delete on payslip_totals
    for each row execute function payrun_frozen_by_payslip();

create trigger pay_components_frozen
    before insert or update or delete on pay_components
    for each row execute function payrun_frozen_by_payslip();
"""

DROP_FREEZE = """
drop trigger if exists pay_components_frozen on pay_components;
drop trigger if exists payslip_totals_frozen on payslip_totals;
drop trigger if exists payslips_frozen on payslips;
drop function if exists payrun_frozen_by_payslip();
drop function if exists payrun_frozen_by_payrun();
"""

# --- журнал переходов --------------------------------------------------------
JOURNAL = """
-- Внешний ключ ставится руками: Django действие удаления в схему не пишет
-- (каскад он исполняет в Python), а журнал обязан исчезать вместе с расчётом
-- и тогда, когда расчёт сносят чистым SQL. Истории без расчёта не бывает.
alter table payrun_transitions
    add constraint payrun_transitions_payrun_fk
    foreign key (payrun_id) references payruns (id) on delete cascade;

comment on table payrun_transitions is
    'Журнал жизненного цикла расчёта: откуда, куда, кто и почему. Пишется триггером, не приложением';
comment on column payrun_transitions.from_status is
    'Статус до перехода. Пусто только у создания расчёта';
comment on column payrun_transitions.actor_id is
    'Кто перевёл: контекст приложения (app_user_id()). Пусто — перевод сделан администрированием мимо приложения';
comment on column payrun_transitions.reason is
    'Зачем перевели. Приезжает разовой настройкой транзакции app.transition_reason; при откате станет обязательной (T025)';

create or replace function payrun_log_transition()
returns trigger
language plpgsql
as $$
declare
    v_reason text;
begin
    if tg_op = 'UPDATE' and new.status is not distinct from old.status then
        return null;
    end if;

    v_reason := nullif(current_setting('app.transition_reason', true), '');
    -- Причина одноразовая: иначе вторая смена статуса в той же транзакции
    -- унаследовала бы чужое объяснение, и журнал соврал бы правдоподобно.
    perform set_config('app.transition_reason', '', true);

    insert into payrun_transitions
        (tenant_id, payrun_id, from_status, to_status, actor_id, reason)
    values (
        new.tenant_id, new.id,
        case when tg_op = 'UPDATE' then old.status end,
        new.status, app_user_id(), v_reason
    );
    return null;
end
$$;

comment on function payrun_log_transition() is
    'Пишет журнал переходов расчёта. Триггером, а не приложением: перехода без записи не бывает ни на одном пути';

create trigger payruns_log_transition
    after insert or update on payruns
    for each row execute function payrun_log_transition();

-- Журнал только пополняется. Историю, которую можно переписать, историей
-- называть нельзя. Каскад от удаления самого расчёта пропускается: он приходит
-- глубже первого уровня триггеров, а прямой delete — с первого.
create or replace function payrun_transitions_append_only()
returns trigger
language plpgsql
as $$
begin
    if tg_op = 'DELETE' and pg_trigger_depth() > 1 then
        return old;
    end if;
    raise exception 'журнал переходов периода только пополняется: % отклонён', tg_op;
end
$$;

comment on function payrun_transitions_append_only() is
    'Запрещает правку и прямое удаление записей журнала переходов';

create trigger payrun_transitions_no_change
    before update or delete on payrun_transitions
    for each row execute function payrun_transitions_append_only();

alter table payrun_transitions enable row level security;
alter table payrun_transitions force row level security;

create policy tenant_isolation on payrun_transitions
    for all
    using (tenant_id in (select app_tenant_ids()))
    with check (tenant_id in (select app_tenant_ids()));

-- Запись в журнал рождается только внутри триггера. Без этого роль приложения
-- могла бы дописать в собственную историю переход, которого не было: право
-- insert у неё есть на все таблицы продукта.
create policy only_from_trigger on payrun_transitions
    as restrictive for insert
    with check (pg_trigger_depth() > 0);
"""

DROP_JOURNAL = """
drop policy if exists only_from_trigger on payrun_transitions;
drop policy if exists tenant_isolation on payrun_transitions;
alter table payrun_transitions no force row level security;
alter table payrun_transitions disable row level security;
drop trigger if exists payrun_transitions_no_change on payrun_transitions;
drop function if exists payrun_transitions_append_only();
drop trigger if exists payruns_log_transition on payruns;
drop function if exists payrun_log_transition();
alter table payrun_transitions drop constraint if exists payrun_transitions_payrun_fk;
"""


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0040_payrun_reopened"),
    ]

    operations = [
        migrations.CreateModel(
            name="PayrunTransition",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                (
                    "from_status",
                    core.fields.EnumField(
                        blank=True, db_type_name="payrun_status", null=True
                    ),
                ),
                ("to_status", core.fields.EnumField(db_type_name="payrun_status")),
                ("actor_id", models.UUIDField(blank=True, null=True)),
                ("reason", models.TextField(blank=True, null=True)),
                ("at", models.DateTimeField(db_default=models.Func(function="now"))),
                (
                    "payrun",
                    models.ForeignKey(
                        db_column="payrun_id",
                        db_constraint=False,
                        on_delete=django.db.models.deletion.CASCADE,
                        to="core.payrun",
                    ),
                ),
                (
                    "tenant",
                    models.ForeignKey(
                        db_column="tenant_id",
                        on_delete=django.db.models.deletion.CASCADE,
                        to="core.tenant",
                    ),
                ),
            ],
            options={
                "db_table": "payrun_transitions",
                "indexes": [
                    models.Index(
                        models.F("tenant"),
                        models.F("payrun"),
                        name="payrun_transitions_payrun_idx",
                    )
                ],
            },
        ),
        migrations.RunSQL(RULES, DROP_RULES),
        migrations.RunSQL(GUARD, DROP_GUARD),
        migrations.RunSQL(FREEZE, DROP_FREEZE),
        migrations.RunSQL(JOURNAL, DROP_JOURNAL),
    ]
