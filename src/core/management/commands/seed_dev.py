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
from datetime import date
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from core import models
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

# Роли спеки. Видимость регистров — то, ради чего вообще нужна роль.
ROLES = [
    ("director", "Оперативный директор", ["white", "grey", "black"], None),
    ("accountant", "Бухгалтер", ["white"], None),
    ("manager", "Управляющий точки", ["white", "grey"], "NS1"),
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
            tenant = self._org()
            items = self._pnl_items()
            groups = self._groups(tenant, preset, items)
            self._roles_and_users(tenant)
            self._calendar(preset)
            employees = self._employees(tenant, groups, rows)

        self.stdout.write(
            self.style.SUCCESS(
                f"Сид готов: тенант {TENANT_CODE}, {len(UNITS)} точки, "
                f"{employees} сотрудников, период {PERIOD:%Y-%m}"
            )
        )
        self.stdout.write("Пользователи для разработки (id для app.user_id):")
        for code, title, ledgers, unit in ROLES:
            self.stdout.write(
                f"  {det_id('user', code)}  {title}: регистры {','.join(ledgers)}"
                + (f", точка {unit}" if unit else ", все точки")
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
        for model in (
            models.PayComponent, models.Payslip, models.Payrun, models.Timesheet,
            models.EmploymentTerm, models.Employee, models.EmployeeGroup,
            models.Membership, models.Role, models.Period, models.AllocationRule,
            models.Counterparty, models.Unit, models.LegalEntity,
        ):
            model.objects.filter(tenant__in=tenants).delete()
        models.PnlItem.objects.filter(tenant__in=tenants).delete()
        tenants.delete()

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
                layer=body.get("layer", "white"), pnl_item=items["labour_cost"],
            )
        return groups

    def _roles_and_users(self, tenant) -> None:
        units = {u.code: u for u in models.Unit.objects.filter(tenant=tenant)}
        for code, title, ledgers, unit_code in ROLES:
            role = models.Role.objects.create(
                id=det_id("role", code), tenant=tenant, code=code, title=title,
                visible_layers=ledgers,
            )
            models.Membership.objects.create(
                id=det_id("membership", code), tenant=tenant,
                user_id=det_id("user", code), role=role,
                unit_ids=[units[unit_code].id] if unit_code else None,
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
                source="import",
            )
        return len(seen)
