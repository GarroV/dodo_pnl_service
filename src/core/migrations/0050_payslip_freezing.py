"""Построчная заморозка ведомости: спорный сотрудник не держит остальных (T027).

**Что появляется.** Таблица заморозок и запрет менять числа замороженной
строки. Заморозка относится к паре «строка ведомости + спор», поэтому один
человек морозится независимо от остальных — ровно то, чего не даёт утверждение
периода, замораживающее расчёт целиком, и чего не даёт закрытие точки (T022),
работающее с часами, а не с расчётом.

**Почему триггеры, а не политики.** Решение 13 блока `timesheets` делит
механизмы так: **ввод данных** закрывается политиками (операционная отсечка
внутри партнёра, обслуживание обязано мочь писать), **уже посчитанные числа** —
триггерами (гарантия обязана действовать на любой путь записи, включая
суперпользователя и чистый SQL). Построчная заморозка — про посчитанные числа,
поэтому она из второй семьи, рядом с заморозкой утверждённого расчёта
(`0041`). Заморозка, которую снимает подключение другим пользователем,
заморозкой не является.

**Три запрета не объясняют один отказ тремя словами.** На данных расчёта
теперь два сторожа, и они вложены, а не спорят:

| путь записи | запрет |
|---|---|
| `timesheets`, `timesheet_days` | закрытие точки (T022) — заморозка строки часов не трогает вовсе |
| `payruns` | утверждение периода (T023) |
| `payslips`, `payslip_totals`, `pay_components` | утверждение периода → заморозка строки |
| `payslip_freezes` | право `payslip.freeze`, а новая заморозка — только в неутверждённом периоде |

Порядок сообщений задан **именами триггеров**: `before`-сторожей одной таблицы
Postgres вызывает в алфавитном порядке, и `payslips_frozen` (период) стоит
раньше `payslips_row_frozen` (строка). У утверждённого периода человек читает
про период — «строка заморожена» ничего не объясняет там, где заморожено всё.
Это проверяется тестом, а не подразумевается.

**Заморозка живёт внутри неутверждённого периода.** Новую заморозку в
утверждённом расчёте отвергает та же функция `payrun_is_frozen()`, на которой
стоит заморозка периода. А **снятие разрешено всегда**: оно ничего не
переписывает, и без этого замороженная строка застревала бы навсегда в любом
закрытом месяце, а обслуживание не смогло бы убрать за собой (тот же дефект,
что был у сида в issue #60).

**Право `payslip.freeze`** заводится вместе со своим экраном, как требует шапка
`0022` («политика, стоящая там, где никто не пишет, — догадка»). Отдельное, а не
чужое: отказ показывает название действия, и человек, нажавший «Заморозить
строку», прочитал бы «Утверждение периода не входит в права вашей роли».

**Чего эта миграция не делает.**

- Не морозит табель. Замороженная строка означает «не пересчитывать этого
  человека», а не «не править его часы»: правка входных данных задним числом —
  T026, и там она обязана давать помеченную дельту, а не отказ.
- Не требует заморозки перед утверждением и не запрещает утверждать период с
  замороженной строкой: спор не держит месяц — в этом вся задача.
- Не удаляет заморозок ни одним путём, кроме исчезновения самой строки
  ведомости: снятие — это пометка. Историю, которую можно стереть, историей
  называть нельзя.
"""
import django.db.models.deletion
from django.db import migrations, models

COMMENTS = """
comment on table payslip_freezes is
    'Заморозка строки ведомости: по сотруднику идёт спор. Действующая заморозка — строка с пустым released_at';
comment on column payslip_freezes.reason is
    'Из-за чего спор. Обязательна: заморозка без объяснения через месяц не читается никем';
comment on column payslip_freezes.frozen_by is
    'Кто заморозил: контекст приложения (app_user_id()). Пусто — сделано обслуживанием мимо приложения';
comment on column payslip_freezes.released_at is
    'Когда заморозку сняли. Пусто — заморозка действует, числа строки не меняются и пересчёт её обходит';
"""

RULES = """
-- Внешний ключ ставится руками, с `on delete cascade`: Django действие удаления
-- в схему не пишет, а каскад исполняет в Python. Если бы заморозку сносил он,
-- она исчезала бы **раньше** своей строки ведомости — и сторож строки видел бы
-- «не заморожено», то есть заморозка обходилась бы удалением через ORM.
alter table payslip_freezes
    add constraint payslip_freezes_payslip_fk
    foreign key (payslip_id) references payslips (id) on delete cascade;

-- Заморожена ли строка ведомости. security definer обязателен по той же
-- причине, что у `timesheet_closed()`: на самой таблице заморозок висит RLS, и
-- без него сторож читал бы заморозки глазами того, кого он ограничивает.
-- Невидимая заморозка означала бы «не заморожено» — то есть запрет снимался бы
-- сам собой у того, кто его не видит.
create or replace function payslip_is_frozen(p_payslip uuid)
returns boolean
language sql stable security definer
set search_path = public
as $$
    select exists (
        select 1 from payslip_freezes
         where payslip_id = p_payslip
           and released_at is null
    )
$$;

comment on function payslip_is_frozen(uuid) is
    'Заморожена ли строка ведомости. На этом стоит запрет менять её числа';
"""

DROP_RULES = """
drop function if exists payslip_is_frozen(uuid);
alter table payslip_freezes drop constraint if exists payslip_freezes_payslip_fk;
"""

# --- сторожа самих чисел ------------------------------------------------------
# Две функции по образцу `0041`: у строки ведомости ключ свой, у итогов и
# компонентов — через неё. Разбор имени колонки в общей функции читался бы хуже.
FREEZE = """
create or replace function payslip_row_frozen()
returns trigger
language plpgsql
as $$
begin
    if payslip_is_frozen(old.id) then
        raise exception 'строка ведомости заморожена: % отклонён', tg_op
            using hint = 'снимите заморозку строки, если её числа надо менять';
    end if;

    if tg_op = 'DELETE' then
        return old;
    end if;
    return new;
end
$$;

create or replace function payslip_row_frozen_by_payslip()
returns trigger
language plpgsql
as $$
declare
    v_payslip uuid;
begin
    if tg_op = 'DELETE' then
        v_payslip := old.payslip_id;
    else
        v_payslip := new.payslip_id;
    end if;

    if payslip_is_frozen(v_payslip) then
        raise exception 'строка ведомости заморожена: % в «%» отклонён',
            tg_op, tg_table_name
            using hint = 'снимите заморозку строки, если её числа надо менять';
    end if;

    if tg_op = 'DELETE' then
        return old;
    end if;
    return new;
end
$$;

comment on function payslip_row_frozen() is
    'Запрещает менять и удалять замороженную строку ведомости';
comment on function payslip_row_frozen_by_payslip() is
    'Запрещает менять данные, привязанные к замороженной строке ведомости';

-- Имена сторожей значимы: `before`-триггеры одной таблицы Postgres вызывает в
-- алфавитном порядке, и сторож периода (`..._frozen`) обязан отвечать раньше
-- построчного (`..._row_frozen`). Иначе у утверждённого периода человек читал
-- бы про строку там, где заморожен весь расчёт.
--
-- `insert` в `payslips` не сторожится: замораживать нечего, заморозка ссылается
-- на существующую строку. У `pay_components` и `payslip_totals` наоборот —
-- вставка в замороженную строку меняет её числа так же, как правка.
create trigger payslips_row_frozen
    before update or delete on payslips
    for each row execute function payslip_row_frozen();

create trigger payslip_totals_row_frozen
    before insert or update or delete on payslip_totals
    for each row execute function payslip_row_frozen_by_payslip();

create trigger pay_components_row_frozen
    before insert or update or delete on pay_components
    for each row execute function payslip_row_frozen_by_payslip();
"""

DROP_FREEZE = """
drop trigger if exists pay_components_row_frozen on pay_components;
drop trigger if exists payslip_totals_row_frozen on payslip_totals;
drop trigger if exists payslips_row_frozen on payslips;
drop function if exists payslip_row_frozen_by_payslip();
drop function if exists payslip_row_frozen();
"""

# --- сторож самой заморозки ---------------------------------------------------
GUARD = """
create or replace function payslip_freeze_guard()
returns trigger
language plpgsql
as $$
declare
    v_payrun uuid;
    probe payslip_freezes%rowtype;
begin
    if tg_op = 'DELETE' then
        -- Каскад от удаления самой строки ведомости пропускается: он приходит
        -- глубже первого уровня триггеров, а прямое удаление — с первого.
        -- Тот же приём, что у журнала переходов расчёта (`0041`).
        if pg_trigger_depth() > 1 then
            return old;
        end if;
        raise exception 'заморозка строки снимается пометкой, а не удалением'
            using hint = 'проставьте released_at — история заморозок не стирается';
    end if;

    if tg_op = 'INSERT' then
        select payrun_id into v_payrun from payslips where id = new.payslip_id;
        -- Строку не видно — считаем расчёт замороженным: тот же довод, что у
        -- payrun_is_frozen(), отказ должен быть стороной ошибки по умолчанию.
        if payrun_is_frozen(v_payrun) then
            raise exception
                'период утверждён: замораживать строку в нём нечего, заморожен весь расчёт'
                using hint = 'откройте период заново, если расчёт надо менять';
        end if;
        return new;
    end if;

    -- Правится только снятие. Причина и автор заморозки — запись о споре, а не
    -- поле формы: переписанная задним числом, она врала бы правдоподобно.
    -- Снятие разрешено и в утверждённом периоде: оно ничего не переписывает, а
    -- без этого замороженная строка застряла бы в закрытом месяце навсегда.
    probe := new;
    probe.released_at := old.released_at;
    probe.released_by := old.released_by;
    if probe is distinct from old then
        raise exception 'заморозка строки: правится только снятие, прочие изменения отклонены';
    end if;
    return new;
end
$$;

comment on function payslip_freeze_guard() is
    'Заморозка строки: новая — только в неутверждённом периоде, правится только снятие, удаляется только вместе со строкой ведомости';

create trigger payslip_freezes_guard
    before insert or update or delete on payslip_freezes
    for each row execute function payslip_freeze_guard();
"""

DROP_GUARD = """
drop trigger if exists payslip_freezes_guard on payslip_freezes;
drop function if exists payslip_freeze_guard();
"""

# --- кто вправе морозить ------------------------------------------------------
# Те же три слоя, что у закрытия точек (`0031`): тенант, видимость, право.
POLICIES = """
alter table payslip_freezes enable row level security;
-- force обязателен: без него политики не действуют на владельца таблиц, а
-- миграции и обслуживание ходят как раз им.
alter table payslip_freezes force row level security;

create policy tenant_isolation on payslip_freezes
    for all
    using (tenant_id in (select app_tenant_ids()))
    with check (tenant_id in (select app_tenant_ids()));

-- Видно ту заморозку, чья строка ведомости видна. Правила видимости точки и
-- сотрудника стоят на самих `payslips`, и второй их копии здесь не заводится:
-- разъехавшиеся копии одного правила и есть тот способ, которым доступ ломается
-- незаметно (D014).
create policy payslip_visibility on payslip_freezes
    as restrictive for all
    using (exists (select 1 from payslips p where p.id = payslip_id))
    with check (exists (select 1 from payslips p where p.id = payslip_id));

-- Форма как в `0022`: `with check` без `using`, чтобы отказ был громким
-- (`new row violates row-level security policy`), а не тихим «изменено 0 строк».
-- Приложение проверяет право до записи и объясняет словами; база — гарантия.
create policy payslip_freeze_insert on payslip_freezes
    as restrictive for insert
    with check (app_has_permission(tenant_id, 'payslip.freeze'));

create policy payslip_freeze_update on payslip_freezes
    as restrictive for update
    with check (app_has_permission(tenant_id, 'payslip.freeze'));

-- Политики на `delete` нет намеренно: удаление заморозки запрещено сторожем
-- целиком, любым путём и любому пользователю. Политика рядом с ним была бы
-- вторым правилом об одном и том же — и однажды разошлась бы с первым.
"""

DROP_POLICIES = """
drop policy if exists payslip_freeze_update on payslip_freezes;
drop policy if exists payslip_freeze_insert on payslip_freezes;
drop policy if exists payslip_visibility on payslip_freezes;
drop policy if exists tenant_isolation on payslip_freezes;
alter table payslip_freezes no force row level security;
alter table payslip_freezes disable row level security;
"""


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0044_merge_20260808_1149'),
    ]

    operations = [
        migrations.CreateModel(
            name='PayslipFreeze',
            fields=[
                ('id', models.UUIDField(db_default=models.Func(function='gen_random_uuid'), primary_key=True, serialize=False)),
                ('reason', models.TextField()),
                ('frozen_at', models.DateTimeField(db_default=models.Func(function='now'))),
                ('frozen_by', models.UUIDField(blank=True, null=True)),
                ('released_at', models.DateTimeField(blank=True, null=True)),
                ('released_by', models.UUIDField(blank=True, null=True)),
                ('payslip', models.ForeignKey(db_column='payslip_id', db_constraint=False, on_delete=django.db.models.deletion.DO_NOTHING, to='core.payslip')),
                ('tenant', models.ForeignKey(db_column='tenant_id', on_delete=django.db.models.deletion.CASCADE, to='core.tenant')),
            ],
            options={
                'db_table': 'payslip_freezes',
                'indexes': [models.Index(models.F('tenant'), models.F('payslip'), name='payslip_freezes_payslip_idx')],
                'constraints': [models.UniqueConstraint(condition=models.Q(('released_at__isnull', True)), fields=('tenant', 'payslip'), name='payslip_freezes_active_uniq'), models.CheckConstraint(condition=models.Q(('reason__regex', '^\\s*$'), _negated=True), name='payslip_freezes_reason_not_blank')],
            },
        ),
        migrations.RunSQL(COMMENTS, migrations.RunSQL.noop),
        migrations.RunSQL(RULES, DROP_RULES),
        migrations.RunSQL(FREEZE, DROP_FREEZE),
        migrations.RunSQL(GUARD, DROP_GUARD),
        migrations.RunSQL(POLICIES, DROP_POLICIES),
    ]
