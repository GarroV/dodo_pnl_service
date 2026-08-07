"""
Схема данных: тенанты, оргструктура, справочники, зарплатные таблицы.

Мультитенантный сервис. Изоляция данных между партнёрами — через RLS
по `tenant_id`, а не через фильтры в коде: забытый фильтр обязан давать пустой
результат, а не чужие строки. Политики и функции контекста живут в миграциях
`0004_rls` и `0005_app_role` — Django ими управлять не умеет.

Сербия — первый тенант, а не архитектура: страновой специфики в схеме нет,
все правила живут в конфигурации.
"""
from __future__ import annotations

import uuid

from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import ArrayField, DateRangeField, RangeOperators
from django.db import models
from django.db.models.functions import Coalesce

from .fields import EnumField

# Названия типов создаёт миграция 0001_types — здесь только ссылки на них.
LEDGER = "ledger"
PAYOUT_CHANNEL = "payout_channel"
ALLOCATION_METHOD = "allocation_method"
PERIOD_STATUS = "period_status"
PAYRUN_STATUS = "payrun_status"
RULE_SCOPE = "rule_scope"


def now_default() -> models.Func:
    """`now()`, а не Now() из Django: тот разворачивается в statement_timestamp().

    Разница видна внутри одной транзакции с несколькими вставками, и в учётных
    данных нужна именно транзакционная отметка.
    """
    return models.Func(function="now")


def uuid_pk() -> models.UUIDField:
    """Первичный ключ, который умеет проставить сама база.

    Значение по умолчанию именно на стороне БД: часть данных приезжает
    миграциями и импортом на чистом SQL, минуя ORM.
    """
    return models.UUIDField(
        primary_key=True, db_default=models.Func(function="gen_random_uuid")
    )


def ledger_field(**kwargs) -> EnumField:
    return EnumField(db_type_name=LEDGER, **kwargs)


# --- Версионирование правил по датам -----------------------------------------
# Правила действуют с даты по дату, и двух версий на одну дату быть не может:
# расчёт взял бы одну из них молча и посчитал бы месяц не тем правилом. Инвариант
# держит база (D015) — ограничение EXCLUDE поверх btree_gist, а не дисциплина в
# коде: сюда пишет и импорт, и админка, и миграция данных.


class DateRange(models.Func):
    """`daterange(valid_from, valid_to, '[)')` — период действия версии правила."""

    function = "daterange"
    output_field = DateRangeField()


def validity_range() -> DateRange:
    """Период действия. Конец не входит: «по 1 июля» и «с 1 июля» — не пересечение.

    Так же читает границы код расчёта (`.exclude(valid_to__lte=period)`), и так
    оформляется обычный перевод сотрудника серединой месяца. Пустой `valid_to` —
    бесконечность: версия действует, пока её не закроют.
    """
    return DateRange("valid_from", "valid_to", models.Value("[)"))


# Пустой scope_id — это «нет уровня», то есть страна или партнёр целиком.
# В EXCLUDE сравнение идёт оператором `=`, а `null = null` даёт null, то есть
# «не совпало»: без приведения к константе ограничение молча не защищало бы
# самый частый уровень переопределений.
NO_SCOPE = uuid.UUID(int=0)


class Tenant(models.Model):
    """Партнёр. Единица изоляции данных: всё остальное ссылается сюда."""

    id = uuid_pk()
    code = models.TextField(unique=True)
    title = models.TextField()
    country_code = models.TextField()  # ISO 3166-1 alpha-2
    base_currency = models.TextField()  # валюта учёта, напр. RSD
    report_currency = models.TextField(db_default="EUR")
    created_at = models.DateTimeField(db_default=now_default())

    class Meta:
        db_table = "tenants"


class LegalEntity(models.Model):
    """Юрлицо партнёра. Бухгалтерия работает с ним, пиццерий для неё нет."""

    id = uuid_pk()
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    title = models.TextField()
    tax_number = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=now_default())

    class Meta:
        db_table = "legal_entities"


class Unit(models.Model):
    """Точка = пиццерия. Расходы разносятся на неё, а не на юрлицо."""

    id = uuid_pk()
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    legal_entity = models.ForeignKey(
        LegalEntity, on_delete=models.SET_NULL, null=True, blank=True,
        db_column="legal_entity_id",
    )
    code = models.TextField()
    title = models.TextField()
    opened_at = models.DateField(null=True, blank=True)
    closed_at = models.DateField(null=True, blank=True)
    external_ids = models.JSONField(db_default={})  # id в Dodo IS и др.

    class Meta:
        db_table = "units"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "code"], name="units_tenant_code_uniq"),
        ]


class Role(models.Model):
    """Роль. Именно она решает, какие регистры учёта человек видит."""

    id = uuid_pk()
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, null=True, blank=True, db_column="tenant_id",
    )  # null = системная роль
    code = models.TextField()
    title = models.TextField()
    permissions = models.JSONField(db_default=[])
    visible_ledgers = ArrayField(ledger_field(), db_default=["official"])

    class Meta:
        db_table = "roles"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "code"], name="roles_tenant_code_uniq"),
        ]


class Membership(models.Model):
    """Кто, в каком тенанте, с какой ролью и по каким точкам."""

    id = uuid_pk()
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    # Пользователь живёт в блоке auth; здесь нарочно голый uuid, чтобы схема
    # не зависела от того, чем в итоге окажется учётка.
    user_id = models.UUIDField()
    role = models.ForeignKey(Role, on_delete=models.PROTECT, db_column="role_id")
    unit_ids = ArrayField(models.UUIDField(), null=True, blank=True)  # null = все точки
    created_at = models.DateTimeField(db_default=now_default())

    class Meta:
        db_table = "memberships"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "user_id", "role"], name="memberships_tenant_user_role_uniq"
            ),
        ]


class Calendar(models.Model):
    """Производственный календарь страны на месяц."""

    id = uuid_pk()
    country_code = models.TextField()
    period = models.DateField()  # первое число месяца
    norm_hours = models.DecimalField(max_digits=6, decimal_places=2)
    working_days = models.IntegerField()
    holidays = ArrayField(models.DateField(), db_default=[])

    class Meta:
        db_table = "calendars"
        constraints = [
            models.UniqueConstraint(
                fields=["country_code", "period"], name="calendars_country_period_uniq"
            ),
        ]


class PnlItem(models.Model):
    """Статья P&L. Единый справочник — цель проекта, поэтому дерево и общий уровень."""

    id = uuid_pk()
    tenant = models.ForeignKey(
        Tenant, on_delete=models.CASCADE, null=True, blank=True, db_column="tenant_id",
    )  # null = общий справочник
    parent = models.ForeignKey(
        "self", on_delete=models.CASCADE, null=True, blank=True, db_column="parent_id",
    )
    code = models.TextField()
    title = models.TextField()
    kind = models.TextField()
    sort_order = models.IntegerField(db_default=0)

    class Meta:
        db_table = "pnl_items"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "code"], name="pnl_items_tenant_code_uniq"),
            models.CheckConstraint(
                condition=models.Q(kind__in=["revenue", "expense", "subtotal"]),
                name="pnl_items_kind_check",
            ),
        ]


class Counterparty(models.Model):
    """Контрагент. Знание «как его пишут в разных системах» — это данные, не память."""

    id = uuid_pk()
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    title = models.TextField()
    tax_number = models.TextField(null=True, blank=True)
    aliases = ArrayField(models.TextField(), db_default=[])
    note = models.TextField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=now_default())

    class Meta:
        db_table = "counterparties"


class AllocationRule(models.Model):
    """Как разносить расход по точкам. Версионируется: закрытый период не ломаем."""

    id = uuid_pk()
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    counterparty = models.ForeignKey(
        Counterparty, on_delete=models.CASCADE, db_column="counterparty_id"
    )
    pnl_item = models.ForeignKey(PnlItem, on_delete=models.PROTECT, db_column="pnl_item_id")
    method = EnumField(db_type_name=ALLOCATION_METHOD)
    unit = models.ForeignKey(
        Unit, on_delete=models.CASCADE, null=True, blank=True, db_column="unit_id",
    )  # для fixed_unit
    ledger = ledger_field(db_default="official")
    valid_from = models.DateField()
    valid_to = models.DateField(null=True, blank=True)
    created_by = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=now_default())

    class Meta:
        db_table = "allocation_rules"
        indexes = [
            models.Index(
                "tenant", "counterparty", models.F("valid_from").desc(),
                name="allocation_rules_lookup_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=~models.Q(method="fixed_unit") | models.Q(unit__isnull=False),
                name="allocation_rules_fixed_unit_check",
            ),
            models.CheckConstraint(
                condition=models.Q(valid_to__isnull=True)
                | models.Q(valid_to__gt=models.F("valid_from")),
                name="allocation_rules_period_check",
            ),
            # Одному контрагенту — одно действующее правило разнесения в каждом
            # регистре: двух ответов на вопрос «куда относить счёт от EPS в
            # марте» быть не должно. Регистр входит в ключ намеренно — один и тот
            # же поставщик может оплачиваться и официально, и из кассы, и это
            # разные строки P&L, а не спорные версии одного правила. Отсюда
            # следует, что искать правило нужно по паре «контрагент + регистр».
            ExclusionConstraint(
                name="allocation_rules_no_overlap",
                expressions=[
                    ("tenant", RangeOperators.EQUAL),
                    ("counterparty", RangeOperators.EQUAL),
                    ("ledger", RangeOperators.EQUAL),
                    (validity_range(), RangeOperators.OVERLAPS),
                ],
            ),
        ]


class FxRate(models.Model):
    """Курс на дату. Консолидация по сети идёт по курсу конца месяца."""

    id = uuid_pk()
    base_currency = models.TextField()
    quote_currency = models.TextField()
    rate_date = models.DateField()
    rate = models.DecimalField(max_digits=18, decimal_places=8)

    class Meta:
        db_table = "fx_rates"
        constraints = [
            models.UniqueConstraint(
                fields=["base_currency", "quote_currency", "rate_date"], name="fx_rates_uniq"
            ),
        ]


class Period(models.Model):
    """Учётный месяц тенанта и его состояние."""

    id = uuid_pk()
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    period = models.DateField()
    status = EnumField(db_type_name=PERIOD_STATUS, db_default="open")
    closed_at = models.DateTimeField(null=True, blank=True)
    closed_by = models.UUIDField(null=True, blank=True)

    class Meta:
        db_table = "periods"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "period"], name="periods_tenant_period_uniq"),
        ]


# --- Зарплата ----------------------------------------------------------------


class RulePreset(models.Model):
    """Готовый набор правил страны. Новая страна = новый пресет, не новый код."""

    id = uuid_pk()
    code = models.TextField()  # 'serbia-2026'
    title = models.TextField()
    country_code = models.TextField()
    body = models.JSONField()  # то, что сейчас в YAML
    valid_from = models.DateField()
    valid_to = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "rule_presets"
        constraints = [
            models.UniqueConstraint(
                fields=["code", "valid_from"], name="rule_presets_code_valid_from_uniq"
            ),
            # Пресет на дату должен собираться однозначно: две версии
            # `serbia-2026` на один июнь — это два разных расчёта одного месяца.
            ExclusionConstraint(
                name="rule_presets_no_overlap",
                expressions=[
                    ("code", RangeOperators.EQUAL),
                    (validity_range(), RangeOperators.OVERLAPS),
                ],
            ),
        ]


class RuleOverride(models.Model):
    """Переопределение поверх пресета: страна → партнёр → группа → человек."""

    id = uuid_pk()
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    scope_type = EnumField(db_type_name=RULE_SCOPE)
    scope_id = models.UUIDField(null=True, blank=True)  # id группы или сотрудника
    path = models.TextField()  # 'hour_types.night.pay_percent'
    value = models.JSONField()
    valid_from = models.DateField()
    valid_to = models.DateField(null=True, blank=True)
    created_by = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=now_default())

    class Meta:
        db_table = "rule_overrides"
        indexes = [
            models.Index(
                "tenant", "scope_type", "scope_id", models.F("valid_from").desc(),
                name="rule_overrides_lookup_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(valid_to__isnull=True)
                | models.Q(valid_to__gt=models.F("valid_from")),
                name="rule_overrides_period_check",
            ),
            # Ключ — «что переопределяем и на каком уровне»: одно правило одного
            # уровня не может иметь двух значений на одну дату. Разные уровни
            # (страна и группа) спорить друг с другом имеют право — их разводит
            # сборка пресета, а не это ограничение.
            ExclusionConstraint(
                name="rule_overrides_no_overlap",
                expressions=[
                    ("tenant", RangeOperators.EQUAL),
                    ("scope_type", RangeOperators.EQUAL),
                    (
                        Coalesce("scope_id", models.Value(NO_SCOPE),
                                 output_field=models.UUIDField()),
                        RangeOperators.EQUAL,
                    ),
                    ("path", RangeOperators.EQUAL),
                    (validity_range(), RangeOperators.OVERLAPS),
                ],
            ),
        ]


class EmployeeGroup(models.Model):
    """Группа сотрудников: схема расчёта и регистр учёта по умолчанию."""

    id = uuid_pk()
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    code = models.TextField()
    title = models.TextField()
    scheme = models.TextField()  # ключ схемы из пресета
    ledger = ledger_field(db_default="official")
    pnl_item = models.ForeignKey(
        PnlItem, on_delete=models.SET_NULL, null=True, blank=True, db_column="pnl_item_id",
    )

    class Meta:
        db_table = "employee_groups"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "code"], name="employee_groups_tenant_code_uniq"
            ),
        ]


class Employee(models.Model):
    """Сотрудник. ФИО между системами не совпадают, поэтому ключ — внешний id."""

    id = uuid_pk()
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    external_id = models.TextField()  # сквозной ключ, напр. JMBG
    first_name = models.TextField()
    last_name = models.TextField()
    external_ids = models.JSONField(db_default={})  # id в Dodo IS, бухпрограмме
    hired_at = models.DateField(null=True, blank=True)
    dismissed_at = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "employees"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "external_id"], name="employees_tenant_external_id_uniq"
            ),
        ]


class EmploymentTerm(models.Model):
    """Условия найма на период. Версионируются: перевод и смена ставки — обычное дело."""

    id = uuid_pk()
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, db_column="employee_id")
    group = models.ForeignKey(EmployeeGroup, on_delete=models.PROTECT, db_column="group_id")
    unit = models.ForeignKey(
        Unit, on_delete=models.SET_NULL, null=True, blank=True, db_column="unit_id",
    )
    base_rate = models.DecimalField(max_digits=12, decimal_places=4)
    coefficient = models.DecimalField(max_digits=8, decimal_places=4, db_default=1)
    scheme = models.TextField(null=True, blank=True)  # переопределяет схему группы
    ledger = ledger_field(null=True, blank=True)  # переопределяет регистр группы
    valid_from = models.DateField()
    valid_to = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "employment_terms"
        indexes = [
            models.Index(
                "tenant", "employee", models.F("valid_from").desc(),
                name="employment_terms_lookup_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(valid_to__isnull=True)
                | models.Q(valid_to__gt=models.F("valid_from")),
                name="employment_terms_period_check",
            ),
            # У человека в один момент одни условия найма. Расчёт берёт версию,
            # действующую в месяце (`calc.collect_cases`); две версии на один
            # месяц дали бы правдоподобный, но неверный расчёт и промолчали.
            ExclusionConstraint(
                name="employment_terms_no_overlap",
                expressions=[
                    ("tenant", RangeOperators.EQUAL),
                    ("employee", RangeOperators.EQUAL),
                    (validity_range(), RangeOperators.OVERLAPS),
                ],
            ),
        ]


class Timesheet(models.Model):
    """Часы за период. Сначала руками, позже из Dodo IS."""

    id = uuid_pk()
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, db_column="employee_id")
    unit = models.ForeignKey(
        Unit, on_delete=models.SET_NULL, null=True, blank=True, db_column="unit_id",
    )
    period = models.DateField()
    insured_hours = models.DecimalField(max_digits=8, decimal_places=2, db_default=0)
    norm_hours = models.DecimalField(max_digits=8, decimal_places=2)
    hours = models.JSONField(db_default={})  # {regular: 176, sick: 20, ...}
    deduction = models.DecimalField(max_digits=14, decimal_places=2, db_default=0)
    source = models.TextField(db_default="manual")  # manual | dodo_is | import
    created_at = models.DateTimeField(db_default=now_default())

    class Meta:
        db_table = "timesheets"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "employee", "period", "unit"], name="timesheets_uniq"
            ),
        ]


class Payrun(models.Model):
    """Расчёт за месяц целиком."""

    id = uuid_pk()
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    period = models.DateField()
    preset = models.ForeignKey(
        RulePreset, on_delete=models.PROTECT, null=True, blank=True, db_column="preset_id",
    )
    status = EnumField(db_type_name=PAYRUN_STATUS, db_default="draft")
    calculated_at = models.DateTimeField(null=True, blank=True)
    approved_by = models.UUIDField(null=True, blank=True)

    class Meta:
        db_table = "payruns"
        constraints = [
            models.UniqueConstraint(fields=["tenant", "period"], name="payruns_tenant_period_uniq"),
        ]


class Payslip(models.Model):
    """Строка ведомости по сотруднику."""

    id = uuid_pk()
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    payrun = models.ForeignKey(Payrun, on_delete=models.CASCADE, db_column="payrun_id")
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, db_column="employee_id")
    unit = models.ForeignKey(
        Unit, on_delete=models.SET_NULL, null=True, blank=True, db_column="unit_id",
    )
    net = models.DecimalField(max_digits=14, decimal_places=2, db_default=0)
    gross = models.DecimalField(max_digits=14, decimal_places=2, db_default=0)
    tax = models.DecimalField(max_digits=14, decimal_places=2, db_default=0)
    contributions = models.DecimalField(max_digits=14, decimal_places=2, db_default=0)
    total_cost = models.DecimalField(max_digits=14, decimal_places=2, db_default=0)
    to_bank = models.DecimalField(max_digits=14, decimal_places=2, db_default=0)
    to_cash = models.DecimalField(max_digits=14, decimal_places=2, db_default=0)
    notes = ArrayField(models.TextField(), db_default=[])

    class Meta:
        db_table = "payslips"
        constraints = [
            models.UniqueConstraint(
                fields=["payrun", "employee"], name="payslips_payrun_employee_uniq"
            ),
        ]


class PayComponent(models.Model):
    """Атом расчёта. Из компонентов собирается и ведомость, и строки P&L."""

    id = uuid_pk()
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    payslip = models.ForeignKey(Payslip, on_delete=models.CASCADE, db_column="payslip_id")
    code = models.TextField()  # 'hours.regular', 'minimum_guarantee'
    title = models.TextField()
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    ledger = ledger_field()
    channel = EnumField(db_type_name=PAYOUT_CHANNEL, db_default="bank")
    taxable = models.BooleanField(db_default=True)

    class Meta:
        db_table = "pay_components"
        indexes = [
            models.Index("tenant", "payslip", name="pay_components_payslip_idx"),
            models.Index("tenant", "code", name="pay_components_code_idx"),
        ]
