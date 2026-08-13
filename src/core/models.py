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
# Состояние фоновой задачи расчёта. Тип создаёт миграция 0046_payrun_jobs.
PAYRUN_JOB_STATUS = "payrun_job_status"
RULE_SCOPE = "rule_scope"
# Как партнёр ведёт правки задним числом. Тип создаёт миграция 0060.
RETRO_MODE = "retro_mode"
# Словари фактов. Типы создаёт миграция 0230_facts.
FACT_SOURCE = "fact_source"
DOCUMENT_KIND = "document_kind"
FACT_ALLOCATION = "fact_allocation"
BATCH_STATUS = "batch_status"


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
    # Как партнёр ведёт правки задним числом (T026, D020). Настройка тенанта, а
    # не переменная окружения: партнёры ведут учёт по-разному, а партнёрская и
    # страновая специфика в этом продукте живёт в конфигурации, не в коде.
    # `delta` — разница переносится в текущий период, `recalculate` — период
    # открывается заново и пересчитывается. Права откатить период настройка не
    # отбирает ни в одном значении: обратимость гарантирована отдельно (D021).
    retro_mode = EnumField(db_type_name=RETRO_MODE, db_default="delta")
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
            # `transfer` — перевод между кассой и банком, пополнение кассы
            # (миграция 0230). Это тоже событие, которое надо накапливать ради
            # сверки наличных, но ни расход, ни выручка: из P&L такие статьи
            # исключены по `kind`, а не по забывчивости каждого отчёта.
            models.CheckConstraint(
                condition=models.Q(kind__in=["revenue", "expense", "subtotal", "transfer"]),
                name="pnl_items_kind_check",
            ),
        ]


class ExpenseItem(models.Model):
    """Статья расходов партнёра: то, что человек выбирает, внося трату (T108).

    **Зачем отдельно от `pnl_items`.** Строка P&L — это то, что видно в отчёте
    («Коммунальные»), а статья — то, чем оперирует бухгалтер, внося расход
    («вода», «электричество», «вывоз мусора»). Их несколько на одну строку
    отчёта, и складывать их в один справочник значило бы либо раздувать P&L до
    сотни строк, либо терять подробность внесения. Поэтому статья **ссылается**
    на строку P&L, а не заменяет её.

    **Справочник поставляется пустым, и это решение, а не недоделка.** Список
    статей придёт с файла бухгалтера Сербии (вопрос Q015). Выдуманный список
    означал бы, что одна и та же трата у нас и у неё называется по-разному, — и
    вскрылось бы это на первой сборке P&L, когда сходиться уже поздно.

    **Названия — на языках интерфейса, ключами из `settings.LANGUAGES`.** Это
    исключение из правила «данные не переводятся» (`web/i18n.py`), и оно
    оправдано составом читателей: одну и ту же статью выбирает сербский
    бухгалтер и читает русскоязычный оперативный директор. Название точки такой
    пары читателей не имеет, а статья имеет. Хранится словарём, а не тремя
    колонками: четвёртый язык интерфейса не должен требовать миграции колонок.

    **Действует с даты, закрывается датой.** Привязка статьи к строке P&L — это
    правило разнесения трат по отчёту, а правки правил задним числом ломают
    закрытый месяц (D020). Версий у привязки нет — она одна на всю историю
    статьи, — поэтому правка, задевающая утверждённый месяц, отклоняется
    словами (`web/directory.refuse_if_unversioned_touches_closed_month`), тем же
    отказом, что схема расчёта группы. Закончилась статья — ставится `valid_to`,
    а не удаление: закрытые месяцы на неё ссылаются.
    """

    id = uuid_pk()
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    code = models.TextField()
    # Ключи — коды языков интерфейса как есть (`ru`, `en`, `sr-latn`), без
    # своего словаря соответствий: второй список языков рядом с настройками
    # разъехался бы с ними молча.
    titles = models.JSONField(db_default={})
    pnl_item = models.ForeignKey(PnlItem, on_delete=models.PROTECT, db_column="pnl_item_id")
    valid_from = models.DateField()
    valid_to = models.DateField(null=True, blank=True)
    created_at = models.DateTimeField(db_default=now_default())
    created_by = models.UUIDField(null=True, blank=True)

    class Meta:
        db_table = "expense_items"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "code"], name="expense_items_tenant_code_uniq"
            ),
            # Статья без единого названия — строка, которую человек не сможет
            # выбрать глазами: в списке она будет пустой. Пустое название
            # запрещено базой, а не только формой, потому что писать сюда будут
            # и загрузка файла бухгалтера, и будущий API.
            models.CheckConstraint(
                condition=~models.Q(titles={}), name="expense_items_titles_not_empty"
            ),
            models.CheckConstraint(
                condition=models.Q(valid_to__isnull=True)
                | models.Q(valid_to__gt=models.F("valid_from")),
                name="expense_items_validity",
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
    """Как разносить расход по точкам. Версионируется: закрытый период не ломаем.

    **Ключей два, заполнен ровно один** (`allocation_rules_one_key`, миграция
    `0233`). Контрагент — ключ фактуры поставщика: она приходит на юрлицо
    целиком, статью в ней никто не выбирал. Статья расходов — ключ ручного
    расхода из кассы: контрагента у него нет и взяться ему неоткуда, а «аренда
    офиса» и «реклама на сеть» разносятся по-разному, и знает об этом именно
    статья. Обе разновидности живут одной таблицей: правило — это метод, точка
    для `fixed_unit`, регистр и период действия, и две таблицы означали бы две
    копии версионирования по датам.
    """

    id = uuid_pk()
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    counterparty = models.ForeignKey(
        Counterparty, on_delete=models.CASCADE, db_column="counterparty_id",
        null=True, blank=True,
    )
    # Второй ключ правила. `PROTECT`: статья не удаляется вовсе (закрывается
    # датой), но правило, оставшееся без статьи, разносило бы неизвестно что.
    expense_item = models.ForeignKey(
        "ExpenseItem", on_delete=models.PROTECT, db_column="expense_item_id",
        null=True, blank=True,
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
            # То же самое для второго ключа. Одним ограничением это не
            # выражается: `=` сравнивает null с null как «не совпало», поэтому
            # правила по статье не мешали бы друг другу вовсе, а правила по
            # контрагенту — друг другу мешают. Ровно один ключ у правила
            # заполнен, поэтому каждое ограничение работает на своей половине.
            ExclusionConstraint(
                name="allocation_rules_item_no_overlap",
                expressions=[
                    ("tenant", RangeOperators.EQUAL),
                    ("expense_item", RangeOperators.EQUAL),
                    ("ledger", RangeOperators.EQUAL),
                    (validity_range(), RangeOperators.OVERLAPS),
                ],
            ),
            # Ровно один ключ: правило без ключа не найдётся никогда, правило с
            # двумя ключами нашлось бы дважды и разнесло бы факт по спорному.
            models.CheckConstraint(
                condition=(
                    models.Q(counterparty__isnull=True, expense_item__isnull=False)
                    | models.Q(counterparty__isnull=False, expense_item__isnull=True)
                ),
                name="allocation_rules_one_key",
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
    # Сдельная величина за месяц (D032): число доставок либо фиксированная сумма.
    # Что именно — решает правило `work_measure` у группы сотрудника, а не эта
    # колонка: у группы способ ровно один, и две колонки означали бы два ответа
    # на один вопрос, которые однажды разъедутся. У почасовой группы величина не
    # читается расчётом вовсе.
    #
    # По дням не раскладывается, в отличие от часов (D011): у дня своей сдельной
    # величины нет — за месячным числом доставок не стоит ни одной настоящей
    # даты, и раскладка выдумала бы их. Придёт подневный источник из Dodo IS —
    # придёт вместе со своими датами.
    piece_value = models.DecimalField(max_digits=14, decimal_places=2, db_default=0)
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
    #
    # DO_NOTHING — не «ничего не происходит», а «Django в это не вмешивается»:
    # каскад исполняет база настоящим внешним ключом. С `CASCADE` Django чистил
    # журнал сам, отдельным `delete`, и для триггера это было прямое удаление
    # истории, а не каскад — он отказывал, и `seed_dev` падал на любой базе, где
    # период хоть раз утверждали (миграция `0043`). «Журнал только пополняется»
    # и «журнал исчезает вместе с расчётом» уживаются только так.
    payrun = models.ForeignKey(
        Payrun, on_delete=models.DO_NOTHING, db_column="payrun_id", db_constraint=False
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


class PayrunJob(models.Model):
    """Задание на расчёт периода: кто запустил, чем занято сейчас, чем кончилось.

    Отдельная строка, а не поля в `payruns`, по двум причинам. Первая: задание
    существует и тогда, когда расчёта ещё нет (нажали кнопку на пустом периоде),
    и тогда, когда расчёт отказался считаться. Вторая, решающая: отметки о ходе
    работы пишутся по **отдельному соединению** — расчёт идёт одной транзакцией,
    и всё, что записано внутри неё, снаружи не видно до конца. Значит, таблица
    прогресса не должна попадать в эту транзакцию вовсе, иначе канал прогресса
    встанет на её же блокировке (см. алиас `progress` в настройках).

    Незавершённое задание на период ровно одно — этим держится идемпотентность
    запуска: частичный уникальный индекс `payrun_jobs_active_uniq` не даёт
    завести второе, поэтому второе нажатие кнопки не порождает второй расчёт.
    """

    id = uuid_pk()
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    period = models.DateField()
    status = EnumField(db_type_name=PAYRUN_JOB_STATUS, db_default="queued")
    # Кто нажал кнопку. Он же — тот, чьим контекстом задача ходит в базу: у
    # фоновой задачи нет запроса, и «от имени системы» она работать не должна.
    requested_by = models.UUIDField(null=True, blank=True)
    # `background` — задача ушла в очередь; `inline` — посчитано прямо в запросе
    # (очередь выключена или её не оказалось). Различие видно человеку на
    # экране: подменять одно другим молча нельзя.
    background = models.BooleanField(db_default=True)
    # Идентификатор задачи в очереди — чтобы задание можно было найти в
    # django_q, когда что-то пошло не так.
    task_id = models.TextField(null=True, blank=True)
    # Чем задача занята сейчас, словами для человека, плюс счётчик, если этап
    # считается штуками (сотрудники).
    stage = models.TextField(db_default="")
    done = models.IntegerField(db_default=0)
    total = models.IntegerField(db_default=0)
    # Отказ или поломка: тот же текст, что человек увидел бы синхронно.
    error = models.TextField(db_default="")
    details = models.JSONField(db_default=[])
    created_at = models.DateTimeField(db_default=now_default())
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "payrun_jobs"
        indexes = [
            models.Index(
                "tenant", "period", models.F("created_at").desc(),
                name="payrun_jobs_period_idx",
            ),
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


class PayslipFreeze(models.Model):
    """Строка ведомости заморожена: по этому человеку идёт спор (T027).

    Зачем это отдельно от утверждения периода. Утверждение морозит расчёт
    целиком, а спор обычно идёт по одному человеку — и он не должен держать
    остальных: «хочу закрыть месяц, пока разбираемся с одним» (спека). Поэтому
    заморозка построчная, а утверждение периода её поглощает: внутри
    утверждённого расчёта морозить нечего, там заморожено всё.

    Действующей заморозкой считается строка с пустым `released_at`. Снятие не
    удаляет её, а помечает: «почему морозили и кто» — единственная запись о
    споре, и стирать её нельзя. Замораживать и снимать можно много раз.

    Причина обязательна (`payslip_freezes_reason_not_blank`): заморозка без
    объяснения через месяц не читается никем. У снятия причины нет — оно
    ничего не переписывает, а возвращает человека в общий порядок (то же
    решение, что у открытия точки в T022).

    Сами числа замороженной строки держат **триггеры** (миграция `0050`), а не
    это объявление: пересчёт ходит в `payslips`, `payslip_totals` и
    `pay_components` тремя разными путями, и договариваться между собой они не
    обязаны.
    """

    id = uuid_pk()
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    # db_constraint=False + DO_NOTHING по образцу `PayrunTransition`: внешний
    # ключ ставится руками с `on delete cascade`, чтобы заморозка исчезала
    # вместе со своей строкой ведомости **средствами базы**. Если бы каскад
    # исполнял Django, он сносил бы заморозку раньше строки — и сторож строки
    # видел бы «не заморожено», то есть заморозка обходилась бы удалением
    # ведомости через ORM.
    payslip = models.ForeignKey(
        Payslip, on_delete=models.DO_NOTHING, db_column="payslip_id", db_constraint=False
    )
    reason = models.TextField()
    frozen_at = models.DateTimeField(db_default=now_default())
    frozen_by = models.UUIDField(null=True, blank=True)
    # Пусто — заморозка действует, числа строки не меняются и пересчёт её обходит.
    released_at = models.DateTimeField(null=True, blank=True)
    released_by = models.UUIDField(null=True, blank=True)

    class Meta:
        db_table = "payslip_freezes"
        indexes = [
            models.Index("tenant", "payslip", name="payslip_freezes_payslip_idx"),
        ]
        constraints = [
            # Одна действующая заморозка на строку. Частичный индекс, а не
            # обычная уникальность: спор может вернуться, и прошлые заморозки
            # обязаны оставаться в истории.
            models.UniqueConstraint(
                fields=["tenant", "payslip"],
                condition=models.Q(released_at__isnull=True),
                name="payslip_freezes_active_uniq",
            ),
            # Пробелы вместо объяснения — то же самое, что пустота.
            models.CheckConstraint(
                condition=~models.Q(reason__regex=r"^\s*$"),
                name="payslip_freezes_reason_not_blank",
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
    # Пусто — обычная сумма этого месяца. Заполнено — это разница за указанный
    # закрытый месяц, перенесённая сюда (T026). Пометка живёт колонкой, а не
    # знанием одного экрана: ведомость и строки P&L собираются из компонентов,
    # и без неё любой следующий потребитель принял бы июньскую разницу за
    # июльскую выплату.
    retro_source_period = models.DateField(null=True, blank=True)

    class Meta:
        db_table = "pay_components"
        indexes = [
            models.Index("tenant", "payslip", name="pay_components_payslip_idx"),
            models.Index("tenant", "code", name="pay_components_code_idx"),
        ]


class PayslipStep(models.Model):
    """Шаг расчёта, **сохранённый вместе с суммой** (T056, issue #48).

    Зачем таблица. След расчёта раньше пересобирался движком по сегодняшним
    правилам. Пока правила не менялись, это давало то же самое; после первой же
    правки задним числом закрытый месяц объяснялся бы правилами, которых в нём
    не было, — а изменить сумму ведомости уже нельзя. Расхождение между числом и
    его объяснением — худшее, что можно показать бухгалтеру, поэтому объяснение
    пишется тем же расчётом, который пишет сумму, и дальше не меняется.

    Из трёх вариантов issue #48 выбран первый — хранить шаги. Второй (хранить
    набор id версий правил и пересобирать по ним) закрывает только половину
    случая: правку **строки** правила задним числом версия не ловит, id
    прежний, а содержимое другое. Третий (запретить правку строк, участвовавших
    в утверждённом расчёте) решает вопрос дисциплиной над данными, а хранение
    решает его данными.

    Регистр учёта здесь свой, а не выведенный из компонента: на нём стоит
    видимость (D023), и второй источник истины для регистра означал бы показ
    шага не тому человеку. У производных величин (бруто, налог, взносы, полная
    стоимость) регистра нет — они посчитаны по всем регистрам сразу, и видит их
    тот же, кто видит `payslip_totals`: политика у них общая по смыслу и по
    доводу (T071).
    """

    id = uuid_pk()
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    # db_constraint=False + DO_NOTHING по образцу `PayslipFreeze`: внешний ключ
    # ставится руками с `on delete cascade`, чтобы шаги исчезали вместе со своей
    # строкой ведомости **средствами базы**. Django исполняет каскад в Python и
    # до чистого SQL не дотягивается, а строки ведомости сносят и сид, и сброс
    # демо, и уборка тестов — каждый своим `delete from payslips`. Надежда, что
    # все они не забудут снести ещё и шаги, — ровно та дисциплина в коде, от
    # которой этот продукт уходит (D014).
    payslip = models.ForeignKey(
        Payslip, on_delete=models.DO_NOTHING, db_column="payslip_id", db_constraint=False
    )
    # Порядок шагов — тот, в котором их сложил движок: объяснение читают сверху
    # вниз, и перестановка шагов ломает его так же, как перестановка строк формулы.
    position = models.IntegerField()
    code = models.TextField()   # 'hours.regular', 'minimum_guarantee'
    title = models.TextField()
    applied_value = models.DecimalField(max_digits=14, decimal_places=2)
    # Пусто — производная величина: она про строку целиком, а не про регистр.
    ledger = ledger_field(null=True, blank=True)
    # net — слагаемое нето; gross / tax / contributions / total_cost — следствие.
    kind = models.TextField(db_default="net")
    # Числа и признаки, из которых собрана сумма. Decimal хранится строкой с
    # пометкой типа (`payrun.steps`): числом в JSON он стал бы float и объяснение
    # поехало бы на копейку там, где вся ценность в точности.
    input_values = models.JSONField(db_default={})
    # Уровень, с которого пришло правило, и версия строки правила. Версия может
    # быть пустой: правило приходит и из тела пресета, у которого своей строки нет.
    source_level = models.TextField(db_default="country")
    rule_version_id = models.UUIDField(null=True, blank=True)

    class Meta:
        db_table = "payslip_steps"
        indexes = [
            models.Index("tenant", "payslip", "position", name="payslip_steps_payslip_idx"),
        ]


class RetroAdjustment(models.Model):
    """Перенос разницы из закрытого месяца в текущий (T026).

    **Это вход периода-получателя, а не строка его ведомости.** Пересчёт
    пересобирает ведомость целиком (`calc._store` сносит строки и пишет заново),
    поэтому дельта, приписанная к готовой ведомости, не пережила бы первого же
    пересчёта. Хранится вход — материализуется в `pay_components` при расчёте
    получателя, ровно так же, как часы табеля.

    **Строка — на компонент, а не на человека.** Регистр учёта это свойство
    компонента, и «разница ложится в тот же регистр» (D020) получается по
    построению: правка, задевшая надбавку в официальном и часы в дополнительном,
    даёт две строки в двух регистрах, и они не складываются ни на каком шаге.

    **Отменяется, но не удаляется.** Пересчёт месяца-источника отменяет его
    переносы триггером `payruns_retro_cancel` — иначе разница посчиталась бы
    дважды. Отмена пометкой, а не удалением: история, которую можно стереть,
    историей не является. Руками отмену не поставить — политика
    `only_from_trigger` пропускает запись только изнутри триггера.
    """

    id = uuid_pk()
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    # Закрытый месяц, за который посчитана разница, и месяц, куда она едет.
    # Оба датами, а не ссылками на `payruns`: перенос переживает и пересчёт
    # получателя, и отсутствие у него расчёта вовсе.
    source_period = models.DateField()
    target_period = models.DateField()
    # db_constraint=False + DO_NOTHING по образцу `PayslipFreeze`: внешний ключ
    # ставится руками с `on delete cascade`, чтобы перенос исчезал вместе с
    # сотрудником средствами базы. Django каскад исполняет в Python, а сторож
    # переноса прямое удаление отвергает — каскад он пропускает по глубине.
    employee = models.ForeignKey(
        Employee, on_delete=models.DO_NOTHING, db_column="employee_id", db_constraint=False
    )
    # Точка исходной строки: по ней управляющий видит перенос по своему человеку
    # и не видит чужого.
    unit = models.ForeignKey(
        Unit, on_delete=models.SET_NULL, null=True, blank=True, db_column="unit_id",
    )
    code = models.TextField()
    title = models.TextField()
    # Разница, а не сумма: бывает отрицательной, если сегодняшние данные дают
    # меньше, чем записано в закрытом месяце.
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    ledger = ledger_field()
    channel = EnumField(db_type_name=PAYOUT_CHANNEL, db_default="bank")
    taxable = models.BooleanField(db_default=True)
    created_at = models.DateTimeField(db_default=now_default())
    created_by = models.UUIDField(null=True, blank=True)
    # Пусто — перенос действует. Заполнено — источник пересчитали, и разница
    # вошла в сам закрытый месяц; материализовать её ещё раз значило бы
    # заплатить дважды.
    cancelled_at = models.DateTimeField(null=True, blank=True)
    cancelled_reason = models.TextField(db_default="", blank=True)

    class Meta:
        db_table = "retro_adjustments"
        indexes = [
            models.Index("tenant", "target_period", name="retro_target_idx"),
            models.Index("tenant", "source_period", name="retro_source_idx"),
        ]


# --- Факты -------------------------------------------------------------------
# Единственное место, куда стекаются все первичные финансовые события: выручка
# из Dodo IS, сырьё и списания, фактуры поставщиков, зарплата, наличные из
# кассы, переводы, налоги. Каждый следующий модуль пишет сюда, а не заводит своё
# хранилище — иначе P&L собирался бы из нескольких правд.
#
# Это НЕ двойная запись. Управленческий учёт: одна строка = одно событие с
# разрезами. Единственное требование к прослеживаемости — от строки P&L дойти до
# первичного документа и понять, откуда взялась сумма.
#
# Схема перенесена миграцией `0230_facts` из `db/migrations/0004_facts.sql` —
# 1159 строк SQL, проверенных на живой базе до переноса. Всё, чего Django не
# видит (проверки с `date_trunc`, отложенный внешний ключ, представления,
# функции разнесения, политики), живёт там же в `RunSQL`, а не здесь.


class FactBatch(models.Model):
    """Одна загрузка = одна партия.

    Нужна, чтобы отвечать на вопрос «откуда взялись эти триста строк» и чтобы
    при разборе кривого импорта было за что взяться. Без партии единственным
    способом отличить одну выгрузку от другой было бы время создания строк —
    то есть догадка.
    """

    id = uuid_pk()
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    source = EnumField(db_type_name=FACT_SOURCE)
    # Имя файла, период выгрузки, id запроса — что угодно, по чему человек
    # опознает загрузку.
    external_ref = models.TextField(null=True, blank=True)
    status = EnumField(db_type_name=BATCH_STATUS, db_default="running")
    started_at = models.DateTimeField(db_default=now_default())
    finished_at = models.DateTimeField(null=True, blank=True)
    stats = models.JSONField(db_default={})  # сколько вставлено, изменено, пропущено
    created_by = models.UUIDField(null=True, blank=True)

    class Meta:
        db_table = "fact_batches"
        indexes = [
            models.Index(
                "tenant", "source", models.F("started_at").desc(),
                name="fact_batches_lookup_idx",
            ),
        ]


class SourceDocument(models.Model):
    """Первичный документ: фактура, чек, строка выписки, расчёт зарплаты.

    Прослеживаемость идёт по цепочке «строка P&L → факт → документ → файл».
    Позиций у документа своей таблицей нет: позиция и есть факт (`document` +
    `line_no`). Накладная из Метро с едой и канцелярией — два факта с разными
    статьями, а не один документ с непонятной статьёй.

    Идемпотентность на уровне документа — ключ `(тенант, источник, внешний id)`:
    повторная выгрузка выписки не плодит документов.
    """

    id = uuid_pk()
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")
    legal_entity = models.ForeignKey(
        LegalEntity, on_delete=models.SET_NULL, null=True, blank=True,
        db_column="legal_entity_id",
    )
    counterparty = models.ForeignKey(
        Counterparty, on_delete=models.SET_NULL, null=True, blank=True,
        db_column="counterparty_id",
    )
    kind = EnumField(db_type_name=DOCUMENT_KIND)
    source = EnumField(db_type_name=FACT_SOURCE)
    external_id = models.TextField()  # id в системе-источнике
    doc_number = models.TextField(null=True, blank=True)
    doc_date = models.DateField()
    # Период учёта по умолчанию для позиций. Отдельно от даты документа: счёт за
    # электричество за июнь приходит в июле, и отчёт строится по периоду.
    period = models.DateField(null=True, blank=True)
    currency = models.TextField(null=True, blank=True)
    total_amount = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    # Не поменялся — разбирать заново нечего.
    content_hash = models.TextField(null=True, blank=True)
    file_url = models.TextField(null=True, blank=True)
    payload = models.JSONField(db_default={})  # сырой ответ источника, как пришёл
    batch = models.ForeignKey(
        FactBatch, on_delete=models.SET_NULL, null=True, blank=True, db_column="batch_id",
    )
    created_at = models.DateTimeField(db_default=now_default())
    created_by = models.UUIDField(null=True, blank=True)

    class Meta:
        db_table = "source_documents"
        indexes = [
            models.Index("tenant", models.F("doc_date").desc(), name="source_docs_date_idx"),
            models.Index("tenant", "counterparty", name="source_docs_counterparty_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["tenant", "source", "external_id"], name="source_documents_external_uniq"
            ),
        ]


class Fact(models.Model):
    """Строка факта = позиция документа.

    Пять решений, без которых схема не читается (полностью — в шапке миграции
    `0230_facts` и в `docs/backlog-facts.md`):

    1. **Позиция, а не документ.** Отдельной таблицы позиций нет: связь даёт
       `document` + `line_no`.
    2. **`doc_date` и `period` — разные поля.** Отчёт строится по `period`,
       всегда первое число месяца.
    3. **Разнесение выражено в данных.** Фактура приходит `pending` без точки,
       правило порождает детей `allocated` со ссылкой на правило, долю и
       родителя; родитель получает `split` и из P&L исключается — иначе двойной
       счёт.
    4. **Правка — заменой версии, не на месте.** `superseded_at` +
       `superseded_by` + `revision`; отчёты смотрят только на действующие
       строки. Закрытый период защищён триггером `facts_guard`.
    5. **Идемпотентность на `dedup_key`**, который считает источник.
       Уникальность только среди действующих строк, поэтому история версий не
       конфликтует сама с собой.

    Писать в таблицу напрямую не надо: единственная точка записи — функция
    `upsert_fact(jsonb)`, там же живут идемпотентность и версионирование.
    """

    id = uuid_pk()
    tenant = models.ForeignKey(Tenant, on_delete=models.CASCADE, db_column="tenant_id")

    # --- обязательные разрезы
    period = models.DateField()  # первое число месяца
    doc_date = models.DateField(null=True, blank=True)  # может быть в другом месяце
    unit = models.ForeignKey(
        Unit, on_delete=models.PROTECT, null=True, blank=True, db_column="unit_id",
    )
    legal_entity = models.ForeignKey(
        LegalEntity, on_delete=models.PROTECT, null=True, blank=True,
        db_column="legal_entity_id",
    )
    pnl_item = models.ForeignKey(PnlItem, on_delete=models.PROTECT, db_column="pnl_item_id")
    # Статья расходов, которую человек выбрал, внося трату (T108). Пусто у всего,
    # что статьи не имеет: зарплатных строк, выручки из коннектора, переводов.
    # Строка P&L при этом заполнена всегда — она берётся из статьи, но остаётся
    # у факта своей колонкой: отчёт не должен зависеть от того, переназвали ли
    # статью после закрытия месяца.
    expense_item = models.ForeignKey(
        "ExpenseItem", on_delete=models.PROTECT, null=True, blank=True,
        db_column="expense_item_id",
    )
    ledger = ledger_field(db_default="official")
    counterparty = models.ForeignKey(
        Counterparty, on_delete=models.SET_NULL, null=True, blank=True,
        db_column="counterparty_id",
    )

    # --- суммы
    # Знак: обычно положительный, отрицательный = возврат или исправление.
    # Доход это или расход, задаёт статья, а не знак.
    amount = models.DecimalField(max_digits=18, decimal_places=2)
    currency = models.TextField()
    amount_report = models.DecimalField(max_digits=18, decimal_places=2, null=True, blank=True)
    report_currency = models.TextField(null=True, blank=True)
    # Курс приколачивается к факту: закрытый месяц не должен ехать при
    # обновлении справочника курсов.
    fx_rate = models.DecimalField(max_digits=18, decimal_places=8, null=True, blank=True)
    fx_rate_date = models.DateField(null=True, blank=True)

    # --- натуральные показатели: сырьё, списания, упаковка
    quantity = models.DecimalField(max_digits=18, decimal_places=4, null=True, blank=True)
    uom = models.TextField(null=True, blank=True)

    title = models.TextField()  # что это: наименование позиции
    note = models.TextField(null=True, blank=True)

    # Канал денег — для сверки кассы, а не для P&L: «надбавка кешем» это обычный
    # факт с `channel = 'cash'`. Пусто = движения денег нет (начисление).
    channel = EnumField(db_type_name=PAYOUT_CHANNEL, null=True, blank=True)

    # --- откуда пришло
    source = EnumField(db_type_name=FACT_SOURCE)
    source_ref = models.TextField(null=True, blank=True)  # id строки в источнике
    document = models.ForeignKey(
        SourceDocument, on_delete=models.SET_NULL, null=True, blank=True, db_column="document_id",
    )
    line_no = models.IntegerField(null=True, blank=True)
    batch = models.ForeignKey(
        FactBatch, on_delete=models.SET_NULL, null=True, blank=True, db_column="batch_id",
    )

    # Ключ идемпотентности. Считает его источник, и он обязан быть устойчивым
    # между загрузками: не по времени и не по номеру строки в файле.
    dedup_key = models.TextField()

    # --- разнесение по точкам
    allocation = EnumField(db_type_name=FACT_ALLOCATION, db_default="direct")
    allocation_rule = models.ForeignKey(
        AllocationRule, on_delete=models.SET_NULL, null=True, blank=True,
        db_column="allocation_rule_id",
    )
    allocation_share = models.DecimalField(
        max_digits=9, decimal_places=6, null=True, blank=True
    )
    # db_constraint=False: внешний ключ ставится руками в миграции, с
    # `on delete cascade` в самой базе. Django исполняет каскад в Python, а дети
    # обязаны исчезать вместе с родителем и тогда, когда родителя сносят чистым
    # SQL, — иначе в отчёте останутся суммы от строки, которой больше нет.
    parent_fact = models.ForeignKey(
        "self", on_delete=models.DO_NOTHING, null=True, blank=True,
        db_column="parent_fact_id", db_constraint=False, related_name="children",
    )

    # --- версионирование
    revision = models.IntegerField(db_default=1)
    superseded_at = models.DateTimeField(null=True, blank=True)
    # Голый uuid, а не внешний ключ Django: ссылка на заменившую строку
    # отложенная (`deferrable initially deferred`), потому что она ставится до
    # вставки новой версии — иначе не обойти уникальность `dedup_key`.
    # Отложенные ключи Django объявлять не умеет, ключ ставится в миграции.
    superseded_by = models.UUIDField(null=True, blank=True)

    created_at = models.DateTimeField(db_default=now_default())
    created_by = models.UUIDField(null=True, blank=True)

    class Meta:
        db_table = "facts"
        indexes = [
            models.Index(
                "tenant", "period", "unit", condition=models.Q(superseded_at__isnull=True),
                name="facts_period_unit",
            ),
            models.Index(
                "tenant", "period", "pnl_item", condition=models.Q(superseded_at__isnull=True),
                name="facts_period_item",
            ),
            models.Index("tenant", "document", name="facts_document"),
            models.Index(
                "parent_fact", condition=models.Q(superseded_at__isnull=True), name="facts_parent"
            ),
            # Что мешает закрыть месяц: суммы без точки. Отдельным индексом,
            # потому что этот список смотрят в конце каждого периода.
            models.Index(
                "tenant", "period",
                condition=models.Q(allocation="pending", superseded_at__isnull=True),
                name="facts_pending",
            ),
        ]
        constraints = [
            # Ключ уникален только среди действующих строк: история версий
            # иначе конфликтовала бы сама с собой.
            models.UniqueConstraint(
                fields=["tenant", "dedup_key"],
                condition=models.Q(superseded_at__isnull=True),
                name="facts_dedup_active",
            ),
        ]
