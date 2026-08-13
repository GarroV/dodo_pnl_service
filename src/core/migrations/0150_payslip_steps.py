"""След расчёта хранится вместе с суммой (T056, issue #48).

**Что было.** В базе лежала только сумма: код, регистр, канал, признак
обложения. Объяснение суммы экран собирал заново — движком на **сегодняшних**
правилах. Пока правила не менялись, это давало то же самое; после первой правки
задним числом закрытый месяц объяснялся бы правилами, которых в нём не было, а
поменять сумму уже нельзя. Экран честно сличал пересобранное с сохранённым и
говорил «разошлось» — но это детектор беды, а не её отсутствие.

**Что сделано.** Шаги пишет тот же расчёт, который пишет суммы, одной
транзакцией с ними. Дальше они не меняются: пересчёт периода сносит строки
ведомости вместе с их шагами и пишет новые, а утверждённый период не
пересчитывается вовсе (T023).

**Почему хранить шаги, а не набор версий правил.** Вариант из issue #48 «сложить
в `payruns` id версий и пересобирать по ним» дешевле, но закрывает половину
случая. Версия ловит **новую строку** правила; правку **существующей** строки
задним числом она не ловит вовсе — id прежний, содержимое другое, и
пересобранный след поедет так же молча, как раньше. Хранение шага не зависит от
того, что случилось с правилом потом, и в этом весь смысл.

**Видимость — на самой строке шага, а не выведенная из компонента.** Шаг чужого
регистра это сразу и сумма, и правило, и человек (D023). Поэтому регистр
записан колонкой и политика стоит на нём — той же формы, что у
`pay_components`.

**Производные величины (бруто, налог, взносы, полная стоимость) регистра не
имеют:** они посчитаны по всем регистрам сразу. Их видит тот, кому видны все
регистры вообще, — ровно как `payslip_totals` (T071, миграция 0023), и по тому
же доводу. Условие роли, а не строки: политика, которая зависит от содержимого
строки, превращает наличие или отсутствие производной в поимённый список тех, у
кого есть выплаты в закрытом регистре. Именно это и закрывала 0023, и заводить
это заново на новой таблице нельзя.
"""
import django.db.models.deletion
from django.db import migrations, models

import core.fields

POLICIES = """
-- Внешний ключ руками, потому что нужен каскад **в базе**: Django исполняет
-- каскад в Python, а строки ведомости сносят и сид, и сброс демо, и уборка
-- тестов — каждый своим `delete from payslips` мимо ORM.
alter table payslip_steps
    add constraint payslip_steps_payslip_fk
    foreign key (payslip_id) references payslips (id) on delete cascade;

comment on table payslip_steps is
    'Шаги расчёта, сохранённые вместе с суммами: объяснение закрытого месяца не зависит от того, как менялись правила потом (T056)';
comment on column payslip_steps.ledger is
    'Регистр учёта шага. Пусто у производных величин: они посчитаны по всем регистрам сразу';
comment on column payslip_steps.input_values is
    'Числа и признаки, из которых собрана сумма. Decimal хранится строкой с пометкой типа — числом в JSON он стал бы float';

alter table payslip_steps enable row level security;
alter table payslip_steps force row level security;

create policy tenant_isolation on payslip_steps
    for all
    using (tenant_id in (select app_tenant_ids()))
    with check (tenant_id in (select app_tenant_ids()));

-- as restrictive: объединяется с изоляцией через AND, то есть реально сужает.
-- Две ветви одной политики, а не две политики: ограничивающие политики
-- складываются через AND, и вторая отрезала бы то, что пропускает первая.
create policy ledger_visibility on payslip_steps
    as restrictive for select
    using (
        case
            when payslip_steps.ledger is null
                then app_sees_every_ledger(payslip_steps.tenant_id)
            else payslip_steps.ledger = any (app_visible_ledgers(payslip_steps.tenant_id))
        end
    );
"""

DROP_POLICIES = """
drop policy if exists ledger_visibility on payslip_steps;
drop policy if exists tenant_isolation on payslip_steps;
alter table payslip_steps drop constraint if exists payslip_steps_payslip_fk;
"""


class Migration(migrations.Migration):
    dependencies = [("core", "0130_directory_permissions")]

    operations = [
        migrations.CreateModel(
            name="PayslipStep",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        db_default=models.Func(function="gen_random_uuid"),
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("position", models.IntegerField()),
                ("code", models.TextField()),
                ("title", models.TextField()),
                ("applied_value", models.DecimalField(decimal_places=2, max_digits=14)),
                ("ledger", core.fields.EnumField(blank=True, db_type_name="ledger", null=True)),
                ("kind", models.TextField(db_default="net")),
                ("input_values", models.JSONField(db_default={})),
                ("source_level", models.TextField(db_default="country")),
                ("rule_version_id", models.UUIDField(blank=True, null=True)),
                (
                    "payslip",
                    models.ForeignKey(
                        db_column="payslip_id",
                        db_constraint=False,
                        on_delete=django.db.models.deletion.DO_NOTHING,
                        to="core.payslip",
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
                "db_table": "payslip_steps",
                "indexes": [
                    models.Index(
                        models.F("tenant"),
                        models.F("payslip"),
                        models.F("position"),
                        name="payslip_steps_payslip_idx",
                    )
                ],
            },
        ),
        migrations.RunSQL(POLICIES, DROP_POLICIES),
    ]
