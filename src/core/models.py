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

from django.contrib.auth.base_user import AbstractBaseUser, BaseUserManager
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


class UserManager(BaseUserManager):
    """Заведение учёток. Пароль всегда через `set_password` — хэшем, не текстом."""

    def create_user(self, username: str, password: str | None = None, **extra):
        if not username:
            raise ValueError("у учётки должен быть логин")
        user = self.model(username=username, **extra)
        # Пустой пароль — не «пускать без пароля»: set_password(None) даёт
        # непригодный хэш, с которым не сойдётся ни один ввод.
        user.set_password(password)
        user.save(using=self._db)
        return user


class User(AbstractBaseUser):
    """Учётка человека. Её `id` — тот самый, что уходит в контекст базы.

    Ключ общий с `memberships.user_id` намеренно: иначе появилась бы вторая
    таблица соответствий «учётка → пользователь тенанта», и однажды в ней
    оказалась бы не та строка. Внешнего ключа при этом нет: блок `db` оставил
    `memberships.user_id` голым uuid, чтобы схема не зависела от того, чем
    окажется вход (D013).

    Прав внутри учётки нет: что человек видит, решает роль в `memberships`, а
    применяют — политики базы. Поэтому `PermissionsMixin` не подмешан.
    """

    id = uuid_pk()
    username = models.TextField(unique=True)
    full_name = models.TextField(db_default="")
    email = models.TextField(db_default="")
    is_active = models.BooleanField(db_default=True)
    created_at = models.DateTimeField(db_default=now_default())

    USERNAME_FIELD = "username"
    REQUIRED_FIELDS: list[str] = []

    objects = UserManager()

    class Meta:
        db_table = "users"

    def __str__(self) -> str:
        return self.username


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
    # Выплата наличными — не особый случай, а канал: в таблице партнёра это
    # столбец «ISPLATA U KES». Отсюда берётся `payslips.to_cash`.
    cash_payout = models.DecimalField(max_digits=14, decimal_places=2, db_default=0)
    # Ручная правка бухгалтера («KOREKCIJA DO MINIMALCA»). Пусто и ноль — разные
    # вещи: пусто значит «правки не было», и тогда движок считает доплату до
    # минимума сам. Ноль значит «правка есть, и она нулевая».
    manual_correction = models.DecimalField(
        max_digits=14, decimal_places=2, null=True, blank=True
    )
    # След правки (D025). Держится ограничением ниже, а не формой: в табель
    # пишут интерфейс, импорт и фоновая задача — совпадать они не обязаны.
    correction_reason = models.TextField(null=True, blank=True)
    corrected_by = models.UUIDField(null=True, blank=True)
    corrected_at = models.DateTimeField(null=True, blank=True)
    source = models.TextField(db_default="manual")  # manual | dodo_is | import
    created_at = models.DateTimeField(db_default=now_default())

    class Meta:
        db_table = "timesheets"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "employee", "period", "unit"], name="timesheets_uniq"
            ),
            # Правка без «кто» и «почему» через полгода неотличима от ошибки
            # ввода — а объяснять её придётся именно тогда. `\S` вместо
            # «непусто»: пробел не причина, иначе проверка обходится одним
            # нажатием клавиши.
            #
            # Проверка на null обязательна и стоит отдельно: `null ~ '\S'` даёт
            # null, а CHECK пропускает всё, что не false. Без неё правка вообще
            # без причины проходила бы — проверено, тест был зелёным зря.
            models.CheckConstraint(
                condition=models.Q(manual_correction__isnull=True)
                | (
                    models.Q(corrected_by__isnull=False)
                    & models.Q(correction_reason__isnull=False)
                    & models.Q(correction_reason__regex=r"\S")
                ),
                name="timesheets_correction_trace_check",
            ),
        ]


class TimesheetDay(models.Model):
    """Из каких дней сложился месячный итог табеля.

    Зачем она есть, когда часы вводятся числом за месяц (D011): смысл подневного
    хранения не в подневном вводе, а в том, чтобы перевод сотрудника между
    точками посреди месяца не требовал переноса данных. Точка стоит на строке
    `timesheets`, поэтому человек на двух точках — это две строки; разрезать
    месяц по дате можно только имея дни.

    Инвариант: `timesheets.hours[тип]` равен сумме часов этого типа по дням.
    Держится единственной точкой записи (`timesheets.store`) — не триггером и не
    формой: писать в табель будут и экран, и импорт, и коннектор Dodo IS.

    Точки на дне намеренно нет: она есть у строки-родителя, и второе место для
    того же факта разъехалось бы с первым.
    """

    id = uuid_pk()
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    # db_constraint=False: внешний ключ создаётся руками в миграции — с
    # `on delete cascade` в самой базе. Django исполняет каскад в Python, то
    # есть удаление табеля мимо ORM (сид, обслуживание, `delete from`) оставило
    # бы дни сиротами.
    timesheet = models.ForeignKey(
        Timesheet, on_delete=models.CASCADE, db_column="timesheet_id",
        db_constraint=False, related_name="days",
    )
    work_date = models.DateField()
    hour_type = models.TextField()  # ключ из hour_types пресета страны
    hours = models.DecimalField(max_digits=6, decimal_places=2)
    # Откуда день взялся. `spread` — ровная раскладка месячного числа: настоящих
    # дат за ней нет, и когда придут подневные данные из Dodo IS, отличить их от
    # раскладки надо будет по этому полю, а не по догадке.
    source = models.TextField(db_default="spread")  # spread | manual | dodo_is
    created_at = models.DateTimeField(db_default=now_default())

    class Meta:
        db_table = "timesheet_days"
        indexes = [
            models.Index("tenant", "timesheet", name="timesheet_days_lookup_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "timesheet", "work_date", "hour_type"],
                name="timesheet_days_uniq",
            ),
            # Отрицательных часов не бывает. Проверка стоит в базе, потому что
            # писать сюда будут три разных пути, и договориться они не обязаны.
            models.CheckConstraint(
                condition=models.Q(hours__gte=0), name="timesheet_days_hours_check"
            ),
        ]


class TimesheetClosure(models.Model):
    """Часы точки за месяц закрыты: «я всё ввёл, больше не правьте» (T022).

    Почему отдельная таблица, а не колонка у точки. У `units.closed_at` другой
    смысл — пиццерия закрылась совсем; здесь же закрыт **месяц** этой точки, и
    июнь может быть закрыт, когда июль ещё вводят.

    Почему закрытие по точке, а не по периоду целиком: управляющий закрывает
    свою точку независимо от соседних (спека), иначе один спорный человек
    держал бы всю сеть.

    Действующим закрытием считается строка с пустым `reopened_at`. Открытие
    заново не удаляет строку, а помечает её: история закрытий — единственный
    ответ на вопрос «когда точка была готова», и стирать его нельзя.

    Запрет записи в закрытые часы держат политики базы (миграция `0031`), а не
    это объявление: в табель пишут экран, импорт и коннектор Dodo IS, и
    договариваться между собой они не обязаны.
    """

    id = uuid_pk()
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    unit = models.ForeignKey(Unit, on_delete=models.CASCADE, db_column="unit_id")
    period = models.DateField()
    closed_at = models.DateTimeField(db_default=now_default())
    closed_by = models.UUIDField(null=True, blank=True)
    # Пусто — закрытие действует. Заполнено — точку открыли заново, и часы
    # снова правятся.
    reopened_at = models.DateTimeField(null=True, blank=True)
    reopened_by = models.UUIDField(null=True, blank=True)

    class Meta:
        db_table = "timesheet_closures"
        indexes = [
            models.Index("tenant", "period", name="timesheet_closures_period_idx"),
        ]
        constraints = [
            # Одно действующее закрытие на «точку + месяц». Частичный индекс, а
            # не обычная уникальность: закрывать и открывать можно много раз, и
            # прошлые закрытия обязаны оставаться в истории.
            models.UniqueConstraint(
                fields=["tenant", "unit", "period"],
                condition=models.Q(reopened_at__isnull=True),
                name="timesheet_closures_active_uniq",
            ),
        ]


class Payrun(models.Model):
    """Расчёт за месяц целиком.

    Жизненный цикл: `draft` → `calculated` → `approved`, откат даёт `reopened`
    (миграция `0041_payrun_lifecycle`). Легальность перехода и неизменность
    утверждённого расчёта держит база триггерами, а не приложение.

    Статус периода (`Period.status`) — не то же самое: там учётный месяц тенанта
    целиком, вместе с платежами и выручкой. Утверждение зарплаты не должно
    замораживать весь месяц, поэтому у зарплаты свой статус.
    """

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


class PayrunTransition(models.Model):
    """Журнал жизненного цикла расчёта: откуда, куда, кто и почему.

    Заполняется **триггером базы**, а не приложением (миграция
    `0041_payrun_lifecycle`). Иначе журнал зависел бы от того, что каждый путь
    записи не забыл дописать строку, — то есть от дисциплины в коде, ровно от
    того, от чего уходит D014.

    Ключ последовательный, а не uuid, намеренно: журнал упорядочен, и порядок —
    часть данных. У всех записей одной транзакции `now()` одинаковый, поэтому по
    времени порядок переходов не восстанавливается.

    `reason` и `actor_id` есть уже сейчас, хотя обязательными их сделает только
    откат (T025): место для причины и автора должно быть в модели с самого
    начала, иначе первый же откат потребовал бы переделки схемы.
    """

    id = models.BigAutoField(primary_key=True)
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    # db_constraint=False: внешний ключ ставится руками в миграции, с
    # `on delete cascade`. Django действие удаления в схему не пишет (каскад он
    # исполняет в Python), а журнал обязан исчезать вместе с расчётом даже
    # тогда, когда расчёт сносят чистым SQL.
    payrun = models.ForeignKey(
        Payrun, on_delete=models.CASCADE, db_column="payrun_id", db_constraint=False
    )
    # Пусто только у самой первой записи: у создания расчёта предыдущего статуса нет.
    from_status = EnumField(db_type_name=PAYRUN_STATUS, null=True, blank=True)
    to_status = EnumField(db_type_name=PAYRUN_STATUS)
    actor_id = models.UUIDField(null=True, blank=True)
    reason = models.TextField(null=True, blank=True)
    at = models.DateTimeField(db_default=now_default())

    class Meta:
        db_table = "payrun_transitions"
        indexes = [
            models.Index("tenant", "payrun", name="payrun_transitions_payrun_idx"),
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
    notes = ArrayField(models.TextField(), db_default=[])
    # Колонки `ledgers` здесь нет намеренно, хотя в базе она есть. Это набор
    # регистров учёта, из которых собрана строка: его заполняет триггер по
    # компонентам (миграция `0009_payslip_ledgers`), и на нём стоит видимость
    # итогов расчёта. Приложению он не нужен ни в одной роли, а показывать его
    # нельзя — по нему поимённо видно, у кого есть выплаты в закрытых регистрах
    # (T065). Поэтому роль `app_user` привилегии на эту колонку не имеет
    # (миграция `0021_payslip_ledgers_hidden`), а из модели поле убрано: ORM
    # выбирает колонки поимённо и иначе спрашивала бы закрытую.

    class Meta:
        db_table = "payslips"
        constraints = [
            models.UniqueConstraint(
                fields=["payrun", "employee"], name="payslips_payrun_employee_uniq"
            ),
        ]


class PayslipTotals(models.Model):
    """Итоги строки ведомости: нето, бруто, налоги, взносы, что на карту и в кассу.

    Отдельная таблица, а не колонки в `payslips`, потому что это единственный
    способ закрыть их по регистрам учёта: защиты на уровне колонок в Postgres
    нет, а прятать саму строку ведомости нельзя — вместе с ней у роли пропадают
    и **видимые** ей компоненты смешанного сотрудника (ведомость собирается
    присоединением к `payslips`). Итоги посчитаны по всем регистрам сразу,
    поэтому видны только тому, кому видны все регистры строки: политика
    `ledger_visibility` в миграции `0009_payslip_ledgers`.
    """

    payslip = models.OneToOneField(
        Payslip, on_delete=models.CASCADE, primary_key=True, db_column="payslip_id"
    )
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    net = models.DecimalField(max_digits=14, decimal_places=2, db_default=0)
    gross = models.DecimalField(max_digits=14, decimal_places=2, db_default=0)
    tax = models.DecimalField(max_digits=14, decimal_places=2, db_default=0)
    contributions = models.DecimalField(max_digits=14, decimal_places=2, db_default=0)
    total_cost = models.DecimalField(max_digits=14, decimal_places=2, db_default=0)
    to_bank = models.DecimalField(max_digits=14, decimal_places=2, db_default=0)
    to_cash = models.DecimalField(max_digits=14, decimal_places=2, db_default=0)

    class Meta:
        db_table = "payslip_totals"


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
