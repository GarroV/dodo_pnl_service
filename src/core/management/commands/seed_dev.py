"""
Наполнение чистой базы тестовыми данными для разработки.

Люди здесь выдуманы. Настоящая таблица партнёра не загружается вообще до
готового MVP (решение D028): данные берутся из обезличенной фикстуры
`tests/fixtures/plata-sample.xlsx` — те же восемь листов и четыре схемы расчёта,
32 придуманных человека.

Команда идемпотентна: гонять можно сколько угодно раз, тенант пересобирается
целиком. Идентификаторы детерминированные (uuid5), поэтому ссылки на данные
сида не протухают между прогонами.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import NamedTuple

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction
from django.utils.timezone import now

from core import models
from core.rules import import_presets
from payroll import load_preset
from payroll.importers import read_plata

TENANT_CODE = "rs-dev"
PERIOD = date(2026, 6, 1)
PRESET_CODE = "serbia-2026"

# Пространство имён для детерминированных id. Своё, чтобы сид одного проекта
# не пересекался с сидом другого.
NS = uuid.UUID("6f2b7f7e-0000-4000-8000-000000000001")

# Точки, на которых обкатываем. Три, а не две: на трёх видно распределение
# копеек при делении расходов поровну, на двух оно не проявляется.
UNITS = [
    ("BG1", "Beograd 1"),
    ("NS1", "Novi Sad Bulevar"),
    ("NS2", "Novi Sad Dunavska"),
]

# Статьи общего справочника: минимум, на котором зарплата раскладывается в P&L.
PNL_ITEMS = [
    ("revenue", "Выручка", "revenue", 10),
    ("food_cost", "Себестоимость", "expense", 20),
    ("labour_cost", "Зарплата", "expense", 40),
    ("payroll_taxes", "Налоги с зарплаты", "expense", 50),
    ("total", "Результат", "subtotal", 90),
]

class SeedRole(NamedTuple):
    """Роль сида: что человек видит, где и что ему позволено делать."""

    code: str
    title: str
    ledgers: list[str]
    unit: str | None  # None — все точки тенанта
    permissions: list[str]


# Четыре роли спеки. Видимость регистров — то, ради чего вообще нужна роль:
# она разворачивается в ключи контекста, а применяют их политики базы.
# Права (`permissions`) — намерения ролей; сегодня на видимое влияет только
# набор регистров, остальное подключится вместе с циклом периода.
ROLES = [
    SeedRole(
        "director", "Оперативный директор",
        ["official", "supplementary", "internal"], None,
        # `payslip.freeze` — заморозка спорной строки ведомости (T027): её
        # ставит тот, кто ведёт месяц, а не управляющий точки. `retro.post` —
        # перенос разницы за закрытый месяц (T026), по тому же доводу.
        [
            "timesheet.edit", "payrun.calculate", "period.approve",
            "period.reopen", "payslip.freeze", "retro.post",
        ],
    ),
    SeedRole(
        "accountant", "Бухгалтер",
        ["official"], None,
        [
            "timesheet.edit", "payrun.calculate", "period.approve",
            "payslip.freeze", "retro.post",
        ],
    ),
    SeedRole(
        "manager", "Управляющий точки",
        ["official", "supplementary"], "NS1",
        ["timesheet.edit", "unit.close"],
    ),
    SeedRole(
        "admin", "Администратор сети",
        ["official"], None,
        ["directory.manage", "rules.manage", "roles.manage"],
    ),
]

# Пароль учёток сида: данные для разработки, не секрет — той же природы, что
# пароль базы в `.env.example`. Меняется переменной SEED_USER_PASSWORD.
SEED_PASSWORD = settings.DEV_LOGIN_PASSWORD


@dataclass(frozen=True)
class Extra:
    """Выдуманный человек, которого нет в обезличенной таблице.

    Зачем такие вообще нужны: фикстура покрывает только два регистра из трёх
    (офис и кухня) и не содержит ни выплат наличными, ни ручных правок. То есть
    сид сам по себе не показывал бы ни скрытие внутреннего регистра, ни след
    корректировки — при сверке первой очереди строку внутреннего регистра
    пришлось вставлять руками (T045, T051).
    """

    ext_id: str
    first_name: str
    last_name: str
    group: str
    # Пусто — значит «как у группы»: схема берётся из пресета, а не дублируется
    # в условиях найма. Заполняется только там, где человек считается иначе,
    # чем его группа (в таблице партнёра так бывает внутри кухни).
    scheme: str | None
    unit: str
    base_rate: Decimal
    hours: Decimal
    cash_payout: Decimal = Decimal(0)
    manual_correction: Decimal | None = None
    correction_reason: str = ""


# Курьеры идут по схеме своей группы (`direct` в пресете, T053): обход, который
# раньше подставлял им чужую схему на условиях найма, больше не нужен.
EXTRA_EMPLOYEES = [
    Extra("dev-courier-1", "Марко", "Курир", "couriers", None, "BG1",
          Decimal("420.00"), Decimal(168), cash_payout=Decimal("12000.00")),
    Extra("dev-courier-2", "Ана", "Курир", "couriers", None, "NS1",
          Decimal("420.00"), Decimal(120), cash_payout=Decimal("8000.00")),
    Extra("dev-correction-1", "Джордже", "Исправка", "kitchen", "standard", "NS2",
          Decimal("390.00"), Decimal(150), manual_correction=Decimal("1200.00"),
          correction_reason="доплата до минималца, письмо бухгалтера от 30.06"),
]


def det_id(*parts: str) -> uuid.UUID:
    """Детерминированный id: тот же сид — те же ссылки."""
    return uuid.uuid5(NS, "/".join(parts))


def unit_for_sheet(sheet: str) -> str:
    """Лист таблицы партнёра → точка.

    В таблице лист = «где и по какой схеме считают», а не «какая пиццерия»,
    поэтому соответствие приблизительное и живёт только в сиде.
    """
    name = " ".join(sheet.split()).upper()
    if name.startswith("BG"):
        return "BG1"
    if name.startswith("NS 2"):
        return "NS2"
    return "NS1"


class Command(BaseCommand):
    help = "Наполнить базу выдуманными данными для разработки (тенант rs-dev)"

    def add_arguments(self, parser) -> None:
        parser.add_argument(
            "--fixture",
            default=str(settings.BASE_DIR / "tests" / "fixtures" / "plata-sample.xlsx"),
            help="обезличенная таблица-источник",
        )

    def handle(self, *args, **options) -> None:
        fixture = Path(options["fixture"])
        if not fixture.exists():
            raise CommandError(
                f"нет файла {fixture} — пересоздайте: python tools/make_fixture.py"
            )

        rows = read_plata(fixture)
        if not rows:
            raise CommandError(f"из {fixture} не прочиталось ни одной строки")

        preset = load_preset(PRESET_CODE)

        with transaction.atomic():
            self._wipe()
            # Правила расчёта живут в таблице, а не в файле (T011). Сид обязан
            # положить их туда: без пресета в базе посчитать период нечем.
            import_presets()
            tenant = self._org()
            items = self._pnl_items()
            groups = self._groups(tenant, preset, items)
            self._roles_and_users(tenant)
            self._calendar(preset)
            employees = self._employees(tenant, groups, rows)
            employees += self._extra_employees(tenant, groups)

        self.stdout.write(
            self.style.SUCCESS(
                f"Сид готов: тенант {TENANT_CODE}, {len(UNITS)} точки, "
                f"{employees} сотрудников, период {PERIOD:%Y-%m}"
            )
        )
        self.stdout.write(f"Учётки для разработки (пароль у всех: {SEED_PASSWORD}):")
        for role_def in ROLES:
            self.stdout.write(
                f"  {role_def.code:<11} {role_def.title}: "
                f"регистры {','.join(role_def.ledgers)}"
                + (f", точка {role_def.unit}" if role_def.unit else ", все точки")
            )

    # --- шаги -----------------------------------------------------------

    def _wipe(self) -> None:
        """Снести данные тенанта в порядке зависимостей.

        Явным списком, а не каскадом ORM: часть связей PROTECT, и порядок
        удаления должен быть виден глазами, а не выводиться из настроек.
        """
        tenants = models.Tenant.objects.filter(code=TENANT_CODE)
        if not tenants.exists():
            return
        # Утверждённый расчёт база не даёт ни менять, ни удалять (T023), и это
        # держит триггер — то есть правило действует и на владельца схемы.
        # Уборка поэтому сначала открывает период заново, объяснив зачем:
        # причина обязательна (T025) и тоже держится триггером. Без этого сид
        # падал на любой базе, где расчёт остался утверждённым (issue #62).
        # Ослаблять сторожей нельзя: они написаны ровно ради того, чтобы
        # утверждённый расчёт не менялся ни одним путём записи.
        approved = models.Payrun.objects.filter(tenant__in=tenants, status="approved")
        if approved.exists():
            with connection.cursor() as cursor:
                cursor.execute(
                    "select set_config('app.transition_reason', %s, true)",
                    ["уборка тестовых данных"],
                )
                approved.update(status="reopened")

        # Замороженные строки ведомости база не даёт ни менять, ни удалять
        # (T027), и держит это триггер — то есть правило действует и на
        # владельца схемы. Уборка поэтому сначала снимает заморозки: иначе сид
        # падал бы на чужом споре вместо того, чтобы убрать за ним. Снятие
        # разрешено в любом состоянии периода как раз ради этого случая.
        models.PayslipFreeze.objects.filter(
            tenant__in=tenants, released_at__isnull=True
        ).update(released_at=now())
        for model in (
            models.PayComponent, models.Payslip, models.Payrun, models.Timesheet,
            models.EmploymentTerm, models.Employee, models.EmployeeGroup,
            models.Membership, models.Role, models.Period, models.AllocationRule,
            models.Counterparty, models.Unit, models.LegalEntity,
        ):
            model.objects.filter(tenant__in=tenants).delete()
        models.PnlItem.objects.filter(tenant__in=tenants).delete()
        tenants.delete()
        # Учётки живут вне тенанта, поэтому сносятся отдельно и только свои:
        # чужие в этой базе не наши, и трогать их сид не вправе.
        models.User.objects.filter(
            pk__in=[det_id("user", role.code) for role in ROLES]
        ).delete()

    def _org(self) -> models.Tenant:
        tenant = models.Tenant.objects.create(
            id=det_id("tenant", TENANT_CODE),
            code=TENANT_CODE,
            title="Dodo Serbia (тестовые данные)",
            country_code="RS",
            base_currency="RSD",
            report_currency="EUR",
        )
        entity = models.LegalEntity.objects.create(
            id=det_id("legal_entity", TENANT_CODE), tenant=tenant, title="Dodo RS d.o.o."
        )
        for code, title in UNITS:
            models.Unit.objects.create(
                id=det_id("unit", code), tenant=tenant, legal_entity=entity,
                code=code, title=title, opened_at=date(2023, 1, 1),
            )
        models.Period.objects.create(
            id=det_id("period", str(PERIOD)), tenant=tenant, period=PERIOD, status="open"
        )
        return tenant

    def _pnl_items(self) -> dict[str, models.PnlItem]:
        """Общий справочник статей: tenant_id пустой, одинаков для всей сети."""
        items = {}
        for code, title, kind, order in PNL_ITEMS:
            items[code], _ = models.PnlItem.objects.get_or_create(
                tenant=None, code=code,
                defaults={"id": det_id("pnl_item", code), "title": title,
                          "kind": kind, "sort_order": order},
            )
        return items

    def _groups(self, tenant, preset: dict, items: dict) -> dict[str, models.EmployeeGroup]:
        """Группы берём из того же пресета, по которому считает движок.

        Иначе регистр учёта и схема в базе разъедутся с расчётом, и разошлись бы
        молча — это ровно тот класс ошибок, ради которого правила лежат в конфиге.
        """
        groups = {}
        for code, body in preset["groups"].items():
            groups[code] = models.EmployeeGroup.objects.create(
                id=det_id("group", code), tenant=tenant, code=code,
                title=body.get("title", code), scheme=body.get("scheme", "standard"),
                ledger=body.get("ledger", "official"), pnl_item=items["labour_cost"],
            )
        return groups

    def _roles_and_users(self, tenant) -> None:
        """Роли, учётки и членства.

        Учётка и членство — разные вещи: `users` хранит, чем человек доказывает
        личность, `memberships` — у какого партнёра и с какой ролью он работает.
        Ключ у них общий (`det_id("user", code)`), потому что этот же uuid
        уходит в контекст базы: вторая таблица соответствий здесь была бы
        лишним местом, где однажды окажется не та строка.
        """
        units = {u.code: u for u in models.Unit.objects.filter(tenant=tenant)}
        for role_def in ROLES:
            role = models.Role.objects.create(
                id=det_id("role", role_def.code), tenant=tenant, code=role_def.code,
                title=role_def.title, visible_ledgers=role_def.ledgers,
                permissions=role_def.permissions,
            )
            user = models.User.objects.create_user(
                username=role_def.code,
                password=SEED_PASSWORD,
                id=det_id("user", role_def.code),
                full_name=role_def.title,
            )
            models.Membership.objects.create(
                id=det_id("membership", role_def.code), tenant=tenant,
                user_id=user.pk, role=role,
                unit_ids=[units[role_def.unit].id] if role_def.unit else None,
            )

    def _calendar(self, preset: dict) -> None:
        body = preset.get("calendar", {}).get(f"{PERIOD:%Y-%m}")
        if not body:
            return
        models.Calendar.objects.update_or_create(
            country_code="RS", period=PERIOD,
            defaults={
                "norm_hours": body["norm_hours"],
                "working_days": 22,
                "holidays": body.get("holidays", []),
            },
        )

    def _employees(self, tenant, groups: dict, rows: list) -> int:
        units = {u.code: u for u in models.Unit.objects.filter(tenant=tenant)}
        seen: set[str] = set()

        for row in rows:
            ext_id = row.employee.ext_id
            if ext_id in seen:
                # В таблице человек может встретиться дважды (перевод между
                # листами). Для сида берём первое вхождение и не выдумываем.
                continue
            seen.add(ext_id)

            first, _, last = row.name.partition(" ")
            employee = models.Employee.objects.create(
                id=det_id("employee", ext_id), tenant=tenant, external_id=ext_id,
                first_name=first, last_name=last or first, hired_at=date(2025, 1, 1),
            )
            unit = units[unit_for_sheet(row.sheet)]
            models.EmploymentTerm.objects.create(
                id=det_id("term", ext_id), tenant=tenant, employee=employee,
                group=groups[row.group], unit=unit,
                base_rate=row.employee.base_rate, coefficient=row.employee.coefficient,
                # Схема задана листом таблицы и внутри одной группы разная,
                # поэтому живёт на условиях найма, а не на группе.
                scheme=row.scheme,
                valid_from=date(2025, 1, 1),
            )
            models.Timesheet.objects.create(
                id=det_id("timesheet", ext_id), tenant=tenant, employee=employee,
                unit=unit, period=PERIOD,
                insured_hours=row.timesheet.insured_hours,
                norm_hours=row.timesheet.norm_hours,
                # Decimal в jsonb уходит строкой: числа с плавающей точкой
                # в деньгах и часах не используем нигде.
                hours={k: str(v) for k, v in row.timesheet.hours.items()},
                deduction=row.timesheet.deduction,
                cash_payout=row.timesheet.cash_payout,
                # Обезличенная фикстура правок не содержит, но путь один и тот
                # же: придёт правка из таблицы — приедет со следом импорта.
                manual_correction=row.timesheet.manual_correction,
                correction_reason=(
                    "перенесено из таблицы бухгалтерии при импорте"
                    if row.timesheet.manual_correction is not None else None
                ),
                corrected_by=(
                    det_id("user", "director")
                    if row.timesheet.manual_correction is not None else None
                ),
                source="import",
            )
        return len(seen)

    def _extra_employees(self, tenant, groups: dict) -> int:
        """Люди, дописанные к фикстуре: третий регистр, наличные и правка.

        Причина, почему они здесь, а не в самой фикстуре: фикстура собирается из
        настоящей таблицы партнёра обезличиванием (`tools/make_fixture.py`), и
        дописать в неё выдуманных курьеров нельзя, не имея на руках оригинала.
        """
        units = {u.code: u for u in models.Unit.objects.filter(tenant=tenant)}
        director = det_id("user", "director")

        for extra in EXTRA_EMPLOYEES:
            employee = models.Employee.objects.create(
                id=det_id("employee", extra.ext_id), tenant=tenant,
                external_id=extra.ext_id, first_name=extra.first_name,
                last_name=extra.last_name, hired_at=date(2025, 1, 1),
            )
            models.EmploymentTerm.objects.create(
                id=det_id("term", extra.ext_id), tenant=tenant, employee=employee,
                group=groups[extra.group], unit=units[extra.unit],
                base_rate=extra.base_rate, coefficient=1,
                scheme=extra.scheme, valid_from=date(2025, 1, 1),
            )
            models.Timesheet.objects.create(
                id=det_id("timesheet", extra.ext_id), tenant=tenant, employee=employee,
                unit=units[extra.unit], period=PERIOD,
                insured_hours=extra.hours, norm_hours=176,
                hours={"regular": str(extra.hours)},
                cash_payout=extra.cash_payout,
                manual_correction=extra.manual_correction,
                correction_reason=extra.correction_reason or None,
                corrected_by=director if extra.manual_correction is not None else None,
                source="manual",
            )
        return len(EXTRA_EMPLOYEES)
