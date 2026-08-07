"""
Ведомость перестаёт выдавать скрытый регистр вычитанием (T050, issue #42).

**Что было.** Ограничивающая политика висела на `pay_components`, а у `payslips`
признака регистра не было вовсе — при том что `net`, `gross`, `tax`,
`contributions`, `total_cost`, `to_bank`, `to_cash` посчитаны по всем регистрам
сразу. Один экран или выгрузка с `net` — и скрытая часть восстанавливается
вычитанием. На компонентах требование D023 («ни строк, ни следа в итогах»)
соблюдалось, на ведомости — нет.

**Что сделано.** Суммарные поля вынесены в отдельную таблицу `payslip_totals`
(один к одному), и ограничивающая политика стоит на ней: итоги видны только
тому, кому видны **все** регистры этой строки. Сама строка `payslips` остаётся
видимой всем в тенанте — это идентификация (кто, где, за какой расчёт), чисел в
ней больше нет.

**Почему не иначе.** Три варианта из issue #42:

1. *Суммы в разрезе регистра.* Отклонено: `gross`, `tax` и взносы считаются от
   общего нето по схеме страны и линейно между регистрами не делятся. Разделить
   их — значит придумать бухгалтерию, которой у партнёра нет.
2. *Отказ от суммарных полей в пользу агрегатов по компонентам.* Отклонено:
   `gross`, `tax` и взносы компонентами не являются и из них не выводятся —
   данные просто потерялись бы.
3. *Ограничивающая политика.* Выбрано — но **не на самих `payslips`**. Возражение
   из issue («у строки регистров может быть два») снимается тем, что хранится
   набор регистров и итоги видны, когда видны все они. А вот прятать саму строку
   `payslips` нельзя, и это выяснилось прогоном: ведомость собирается запросом
   к `pay_components` с присоединением `payslips`, поэтому скрытая строка
   утаскивает за собой и **официальные** компоненты смешанного сотрудника —
   у бухгалтера пропала надбавка за питание у кухни, то есть официальный расход,
   который она обязана видеть. Вертикальное разделение таблицы — обычный приём
   там, где нужна защита на уровне колонок, которой в Postgres нет.

**Почему набор регистров наполняет триггер.** Пишут в ведомость расчёт, импорт и
правки; надежда, что каждый из них не забудет проставить поле, — та самая
дисциплина в коде, от которой уходит D014. Триггер работает на любом пути
записи, включая чистый SQL мимо ORM.

**Почему инкрементально, а не пересчётом.** Пересчёт означал бы `select ... from
pay_components` внутри триггера, а на компонентах висит своя RLS: роль увидела
бы только видимые ей строки, набор вышел бы неполным, и политика пропустила бы
то, что должна закрыть. Обходить это `security definer` — значит поставить
защиту на то, что функция создана суперпользователем. Инкремент таких вопросов
не задаёт: массив только пополняется, ошибка возможна лишь в сторону «спрятали
лишнее», а сбрасывается он пересозданием ведомости, что расчёт и делает.
"""
import django.contrib.postgres.fields
import django.db.models.deletion
from django.db import migrations, models

import core.fields
import core.models

TRIGGER = """
create or replace function payslip_ledgers_add()
returns trigger
language plpgsql
as $$
begin
    -- Только пополнение: читать pay_components отсюда нельзя, на них своя RLS,
    -- и набор вышел бы неполным именно у той роли, от которой мы закрываемся.
    update payslips
       set ledgers = (
               select array_agg(distinct value order by value)
                 from unnest(ledgers || new.ledger) as value
           )
     where id = new.payslip_id
       and not (new.ledger = any (ledgers));
    return null;
end
$$;

comment on function payslip_ledgers_add() is
    'Пополняет payslips.ledgers регистром нового компонента: на этом наборе стоит видимость итогов';

create trigger pay_components_ledgers_add
    after insert or update of ledger, payslip_id on pay_components
    for each row execute function payslip_ledgers_add();

comment on column payslips.ledgers is
    'Регистры учёта, из которых собрана строка. Заполняется триггером; по нему видны итоги';

comment on table payslip_totals is
    'Итоги строки ведомости. Отдельно от payslips: посчитаны по всем регистрам сразу и видны только тому, кому видны все они';

alter table payslip_totals enable row level security;
alter table payslip_totals force row level security;

create policy tenant_isolation on payslip_totals
    for all
    using (tenant_id in (select app_tenant_ids()))
    with check (tenant_id in (select app_tenant_ids()));

-- Итоги видны, только если видны ВСЕ регистры строки: нето и бруто посчитаны по
-- всем сразу, и показ хотя бы одного числа выдал бы скрытое вычитанием.
-- Подзапрос к payslips безопасен: на самой ведомости политики видимости
-- регистров нет, поэтому RLS не обрежет его молча до «ничего не нашли».
create policy ledger_visibility on payslip_totals
    as restrictive for select
    using (exists (
        select 1 from payslips p
         where p.id = payslip_totals.payslip_id
           and p.ledgers <@ app_visible_ledgers(p.tenant_id)
    ));
"""

DROP_TRIGGER = """
drop policy if exists ledger_visibility on payslip_totals;
drop policy if exists tenant_isolation on payslip_totals;
drop trigger if exists pay_components_ledgers_add on pay_components;
drop function if exists payslip_ledgers_add();
"""

MONEY = ["net", "gross", "tax", "contributions", "total_cost", "to_bank", "to_cash"]


class Migration(migrations.Migration):
    dependencies = [("core", "0008_correction_comments")]

    operations = [
        migrations.AddField(
            model_name="payslip",
            name="ledgers",
            field=django.contrib.postgres.fields.ArrayField(
                base_field=core.fields.EnumField(db_type_name="ledger"), db_default=[]
            ),
        ),
        migrations.CreateModel(
            name="PayslipTotals",
            fields=[
                (
                    "payslip",
                    models.OneToOneField(
                        db_column="payslip_id",
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        serialize=False,
                        to="core.payslip",
                    ),
                ),
                *[
                    (name, models.DecimalField(db_default=0, decimal_places=2, max_digits=14))
                    for name in MONEY
                ],
                (
                    "tenant",
                    models.ForeignKey(
                        db_column="tenant_id",
                        on_delete=django.db.models.deletion.CASCADE,
                        to="core.tenant",
                    ),
                ),
            ],
            options={"db_table": "payslip_totals"},
        ),
        *[migrations.RemoveField(model_name="payslip", name=name) for name in MONEY],
        migrations.RunSQL(TRIGGER, DROP_TRIGGER),
    ]
