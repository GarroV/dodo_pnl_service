"""
Движок начисления зарплаты.

Принцип: правила лежат в конфиге (пресете), движок не знает ничего про конкретную
страну. Расчет всегда разбирается на компоненты выплаты — у каждого свой слой учета,
канал выплаты и признак налогообложения. Это то, что потом уходит в P&L.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

D = Decimal


def d(x) -> Decimal:
    """Аккуратное приведение к Decimal — деньги не считаем во float."""
    if isinstance(x, Decimal):
        return x
    return D(str(x if x is not None else 0))


# --------------------------------------------------------------------------
# Входные данные
# --------------------------------------------------------------------------

@dataclass
class Employee:
    """Карточка сотрудника. Всё, что не задано, берется из группы, затем из страны."""
    ext_id: str
    name: str
    group: str
    scheme: str
    base_rate: Decimal              # cena rada
    coefficient: Decimal = D("1.0")
    layer: str | None = None        # переопределяет слой группы
    unit: str | None = None


@dataclass
class Timesheet:
    """Часы за период, разложенные по типам."""
    hours: dict[str, Decimal] = field(default_factory=dict)
    insured_hours: Decimal = D(0)   # база для взносов, может отличаться от суммы часов
    norm_hours: Decimal = D(176)
    deduction: Decimal = D(0)       # obustava
    cash_payout: Decimal = D(0)
    manual_correction: Decimal | None = None  # ручная правка, если движок не покрывает случай

    def get(self, kind: str) -> Decimal:
        return d(self.hours.get(kind, 0))


@dataclass
class Component:
    """Компонент выплаты — атом расчета. Из них собирается и ведомость, и P&L."""
    code: str
    title: str
    amount: Decimal
    layer: str
    channel: str = "bank"     # bank | cash
    taxable: bool = True

    def __repr__(self) -> str:
        return f"{self.code}={self.amount:.2f}"


@dataclass
class Payslip:
    employee: Employee
    components: list[Component] = field(default_factory=list)
    net: Decimal = D(0)
    gross: Decimal = D(0)
    tax: Decimal = D(0)
    contributions: Decimal = D(0)
    total_cost: Decimal = D(0)
    to_bank: Decimal = D(0)
    to_cash: Decimal = D(0)
    notes: list[str] = field(default_factory=list)

    def add(self, **kw) -> None:
        if d(kw.get("amount")) != 0:
            self.components.append(Component(**kw))


# --------------------------------------------------------------------------
# Движок
# --------------------------------------------------------------------------

class PayrollEngine:
    def __init__(self, preset: dict[str, Any]):
        self.p = preset
        self.const = {k: d(v) for k, v in preset["constants"].items()}
        self.rates = {k: d(v) for k, v in preset["rates"].items()}
        self.hour_types = preset["hour_types"]
        self.schemes = preset["schemes"]
        self.groups = preset.get("groups", {})

    # -- вспомогательное --------------------------------------------------

    def hourly_rate(self, e: Employee) -> Decimal:
        return d(e.base_rate) * d(e.coefficient)

    def layer_of(self, e: Employee) -> str:
        if e.layer:
            return e.layer
        return self.groups.get(e.group, {}).get("layer", "white")

    # -- начисление -------------------------------------------------------

    def accrue_hours(self, slip: Payslip, ts: Timesheet) -> Decimal:
        """Начисление по типам часов. Каждый тип — отдельный компонент."""
        e = slip.employee
        rate = self.hourly_rate(e)
        layer = self.layer_of(e)
        total = D(0)

        for kind, cfg in self.hour_types.items():
            hours = ts.get(kind)
            if hours == 0:
                continue
            pct = d(cfg["pay_percent"])
            amount = rate * pct * hours
            slip.add(
                code=f"hours.{kind}",
                title=cfg["title"],
                amount=amount,
                layer=layer,
            )
            total += amount
        return total

    def minimum_guarantee(self, slip: Payslip, ts: Timesheet) -> Decimal:
        """
        Доплата до гарантированного минимума за часы, оплаченные ниже полной ставки.
        Механика подтверждена на одном случае, вариант базы под вопросом.
        """
        cfg = self.p.get("minimum_guarantee", {})
        if not cfg.get("enabled"):
            return D(0)

        e = slip.employee
        rate = self.hourly_rate(e)
        if cfg.get("base") == "personal_rate":
            floor = rate
        else:
            floor = self.const["min_hourly_rate"]

        total = D(0)
        for kind in cfg.get("applies_to", []):
            hours = ts.get(kind)
            if hours == 0:
                continue
            paid = rate * d(self.hour_types[kind]["pay_percent"])
            if paid < floor:
                total += (floor - paid) * hours

        if total:
            slip.add(
                code="minimum_guarantee",
                title="Доплата до минимума",
                amount=total,
                layer=self.layer_of(e),
            )
        return total

    def allowances(self, slip: Payslip, ts: Timesheet, scheme: dict) -> Decimal:
        """Надбавки, не зависящие от ставки: топли оброк и подобные."""
        if scheme.get("allowances") == []:
            return D(0)

        worked = sum(
            (ts.get(k) for k, c in self.hour_types.items() if c.get("counts_as_worked")),
            D(0),
        )
        full_day = self.const["full_day_hours"]
        norm_days = self.const["reference_norm_hours"] / full_day

        total = D(0)
        for code, cfg in self.p.get("allowances", {}).items():
            amount = d(cfg["amount_per_norm"])

            # схема может переопределить способ пропорции
            mode = scheme.get("allowance_prorate", {}).get(code, cfg.get("prorate_by"))

            if mode == "worked_days":
                # надбавка за рабочий день: у полставки день короче, поэтому
                # тех же дней набирается вдвое меньшим числом часов
                hours_per_day = d(scheme.get("hours_per_day", full_day))
                amount = amount / norm_days * (worked / hours_per_day)

            elif mode == "worked_hours":
                amount = amount / self.const["reference_norm_hours"] * worked

            slip.add(
                code=code,
                title=cfg["title"],
                amount=amount,
                layer=cfg.get("layer", self.layer_of(slip.employee)),
                taxable=cfg.get("taxable", True),
            )
            total += amount
        return total

    # -- пересчет нето → бруто и взносы -----------------------------------

    def gross_up(self, slip: Payslip, ts: Timesheet, scheme: dict) -> None:
        method = scheme["gross_up"]["method"]
        net = slip.net
        tax_free = self.const["tax_free_monthly"]
        norm = self.const["reference_norm_hours"]
        tax_rate = self.rates["income_tax"]

        if method == "flat":
            slip.gross = net / self.rates["net_factor"]

        elif method == "net_minus_prorated_allowance":
            hours = ts.insured_hours
            divisor = d(scheme["gross_up"].get("hours_divisor", 1))
            credit = (tax_free * tax_rate / norm) * (hours / divisor)
            slip.gross = (net - credit) / self.rates["net_factor"]

        elif method == "min_base":
            share = ts.insured_hours / norm
            half_free = tax_free / 2 * share
            min_base = self.const["min_contribution_base"]
            min_base_part = min_base * self.rates["employee_contributions"] * share
            slip.gross = (net - half_free * tax_rate + min_base_part) / (1 - tax_rate)
            slip.tax = (slip.gross - half_free) * tax_rate

        else:
            raise ValueError(f"неизвестный метод пересчета: {method}")

    def contributions(self, slip: Payslip, ts: Timesheet, scheme: dict) -> None:
        cfg = scheme["contributions"]
        method = cfg["method"]

        if method == "employer_plus_withheld":
            # взносы работодателя + всё, что удержано с работника (налог + его взносы)
            slip.contributions = (
                slip.gross * self.rates["employer_contributions"] + (slip.gross - slip.net)
            )
            slip.total_cost = slip.net + slip.contributions

        elif method == "min_base_combined":
            share = ts.insured_hours / self.const["reference_norm_hours"]
            slip.contributions = (
                self.const["min_contribution_base"] * share * self.rates["combined_contributions"]
            )
            slip.total_cost = slip.net + slip.tax + slip.contributions

        elif method == "flat_rate":
            slip.contributions = slip.gross * self.rates[cfg["rate_key"]]
            slip.total_cost = slip.net + slip.contributions

        else:
            raise ValueError(f"неизвестный метод взносов: {method}")

    # -- главный вход -----------------------------------------------------

    def calculate(self, e: Employee, ts: Timesheet) -> Payslip:
        slip = Payslip(employee=e)
        scheme = self.schemes[e.scheme]

        # для полставки норма часов по умолчанию — половина месячной,
        # но фактически отработанное всегда важнее (увольнение, приём в середине месяца)
        if scheme.get("worked_hours_source") == "half_of_norm" and not ts.hours.get("regular"):
            ts.hours["regular"] = ts.norm_hours / 2

        earned = self.accrue_hours(slip, ts)

        if ts.manual_correction is not None:
            # если бухгалтер поставил правку руками — уважаем ее и помечаем
            slip.add(
                code="manual_correction",
                title="Ручная корректировка",
                amount=d(ts.manual_correction),
                layer=self.layer_of(e),
            )
            earned += d(ts.manual_correction)
            slip.notes.append("применена ручная корректировка")
        else:
            earned += self.minimum_guarantee(slip, ts)

        earned += self.allowances(slip, ts, scheme)

        slip.net = earned
        self.gross_up(slip, ts, scheme)
        self.contributions(slip, ts, scheme)

        slip.to_cash = d(ts.cash_payout)
        slip.to_bank = slip.net - d(ts.deduction) - slip.to_cash
        return slip
