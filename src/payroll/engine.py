"""
Движок начисления зарплаты.

Принцип: правила лежат в конфиге (пресете), движок не знает ничего про конкретную
страну. Расчет всегда разбирается на компоненты выплаты — у каждого свой регистр учёта,
канал выплаты и признак налогообложения. Это то, что потом уходит в P&L.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from .presets import FILE_ORIGIN, Origin
from .trace import TraceStep

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
    ledger: str | None = None        # переопределяет регистр группы
    unit: str | None = None
    # Чем меряется работа этого человека: переопределяет меру группы (T164).
    # Пусто — как у группы, и до T164 иначе и не бывало.
    work_measure: str | None = None


@dataclass
class Timesheet:
    """Часы за период, разложенные по типам."""
    hours: dict[str, Decimal] = field(default_factory=dict)
    insured_hours: Decimal = D(0)   # база для взносов, может отличаться от суммы часов
    norm_hours: Decimal = D(176)
    deduction: Decimal = D(0)       # obustava
    cash_payout: Decimal = D(0)
    manual_correction: Decimal | None = None  # ручная правка, если движок не покрывает случай
    # Сдельная величина за месяц (D032). Что именно она означает — количество
    # единиц или сумму, — решает правило `work_measure` у группы, а не это поле:
    # у группы способ один, и второе поле означало бы два ответа на один вопрос.
    # У почасовой группы величина не читается вовсе.
    piece_value: Decimal = D(0)

    def get(self, kind: str) -> Decimal:
        return d(self.hours.get(kind, 0))


# --------------------------------------------------------------------------
# База для взносов: два правила о ней, которые нужны не только движку
# --------------------------------------------------------------------------
#
# `insured_hours` — самостоятельный вход расчета, а не сумма часов: в таблице
# бухгалтерии это отдельная колонка. Но интерфейсу и расчету периода нужно
# знать про нее две вещи, и обе объявлены пресетом, а не кодом. Живут они
# здесь, рядом с формулами, которые их читают: разъедутся — сразу видно.

# Методы, где база для взносов участвует в формуле. Остальные (`flat`, `none`)
# ее не читают вовсе, и устаревшая база у них ничего не портит.
_GROSS_UP_ON_INSURED = {"net_minus_prorated_allowance", "min_base"}
_CONTRIBUTIONS_ON_INSURED = {"min_base_combined"}


def insured_base(hours: dict, hour_types: dict) -> Decimal:
    """Сколько часов входит в базу для взносов по правилам страны.

    Флаг `insured` объявлен у каждого типа часов в пресете — «входит ли в базу
    для взносов». Тип без флага считается входящим: занизить базу молча дороже,
    чем лишний раз спросить. Типов, которых нет в правилах страны, в базе нет:
    движок их тоже не считает.
    """
    return sum(
        (d(hours.get(kind, 0)) for kind, cfg in hour_types.items()
         if (cfg or {}).get("insured", True)),
        D(0),
    )


# --------------------------------------------------------------------------
# Чем меряется работа группы (D032, ответ владельца на Q011)
# --------------------------------------------------------------------------
#
# Часы — не единственная мера: курьеру могут платить за доставки или фиксированной
# суммой, и какой из способов у партнёра настоящий, бухгалтер ещё не сказал.
# Поэтому способ — правило (`groups.<код>.work_measure`), а не ветка в коде, и
# живёт оно там же, где схема расчёта и регистр учёта группы.
#
# Разделение с «схемой расчёта» намеренное: схема отвечает на «как из нето
# получить бруто и взносы», мера — на «из чего берётся само нето». Свести их в
# одно значило бы заводить схему на каждое сочетание.
#
# С T164 у человека есть своя мера — в условиях найма, рядом со схемой и
# регистром. Порядок «человек сильнее группы» записан **здесь и только здесь**:
# спрашивают его трое — движок, расчёт периода (`payrun.calc.check_measures`) и
# экран табеля (`timesheets.grid.measure_of`), — и три копии одного правила
# разъехались бы молча. Разъезд выглядел бы так: экран предлагает вводить
# доставки, а расчёт считает часы.

HOURS = "hours"

# Откуда взялась мера, заданная человеку: из его условий найма. Уровень тот же
# `employee`, что у переопределения правила по человеку (`presets.LEVELS`), —
# интерфейс уже называет его словами «переопределение по сотруднику»
# (`web/views.LEVEL_TITLES`), и это ровно то, что здесь произошло. Версии у неё
# нет: версионируется строка условий найма, а не правило.
TERMS_ORIGIN = Origin(level="employee")


def work_measure(group: dict | None, *, employee: str | None = None) -> str:
    """Мера работы: своя у человека сильнее правила группы. Обе пусты — часы.

    Часы умолчанием, потому что так работали до появления правила вовсе: группа
    без `work_measure` и человек без своей меры обязаны считаться ровно как
    считались.
    """
    return employee or (group or {}).get("work_measure") or HOURS


def uses_insured_hours(scheme: dict) -> bool:
    """Читает ли эта схема расчета базу для взносов.

    Правится вместе с формулами `gross_up` и `contributions` ниже: появится
    метод, которому база нужна, — его имя добавляется в множества выше.
    """
    return (
        (scheme.get("gross_up") or {}).get("method") in _GROSS_UP_ON_INSURED
        or (scheme.get("contributions") or {}).get("method") in _CONTRIBUTIONS_ON_INSURED
    )


@dataclass
class Component:
    """Компонент выплаты — атом расчета. Из них собирается и ведомость, и P&L."""
    code: str
    title: str
    amount: Decimal
    ledger: str
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
    # След расчёта (D025). Пишется по ходу, тем же кодом, что считает суммы:
    # объяснение, собранное отдельно, разъехалось бы с расчётом молча.
    trace: list[TraceStep] = field(default_factory=list)

    def add(self, *, step: TraceStep | None = None, **kw) -> None:
        if d(kw.get("amount")) != 0:
            self.components.append(Component(**kw))
            if step is not None:
                self.trace.append(step)

    def derive(self, step: TraceStep) -> None:
        """Шаг производной величины: бруто, налог, взносы, полная стоимость.

        Заменяет прежний шаг того же назначения, а не копится рядом: пересчёт
        бруто после правки нето — это то же самое бруто, а не второе.
        """
        self.trace = [s for s in self.trace if s.contributes_to != step.contributes_to]
        self.trace.append(step)


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

    def step(self, code: str, title: str, value: Decimal, *, rule_path: str,
             inputs: dict[str, Any], contributes_to: str = "net",
             origin: Origin | None = None) -> TraceStep:
        """Шаг следа с версией правила, по которой посчитано.

        Откуда взялось значение, знает сам пресет: собранный из базы помнит, на
        каком уровне его переопределили и какой строкой. Пресет из файла версии
        не имеет — и шаг честно остаётся без неё.

        `origin` передаётся тогда, когда значение пришло **не из правил**: мера
        работы бывает задана человеку в условиях найма (T164), и пресет о ней не
        знает ничего. Спросить его в таком случае значило бы получить ответ
        «правило страны» про решение, которого страна не принимала, — то есть
        соврать ровно там, где след и нужен.
        """
        if origin is None:
            origin_of = getattr(self.p, "origin_of", None)
            origin = origin_of(rule_path) if origin_of else FILE_ORIGIN
        return TraceStep(
            rule_code=code, title=title, applied_value=value, rule_code_path=rule_path,
            input_values=inputs, rule_version_id=origin.version_id,
            source_level=origin.level, contributes_to=contributes_to,
        )

    def hourly_rate(self, e: Employee) -> Decimal:
        return d(e.base_rate) * d(e.coefficient)

    def monthly_measure(self, e: Employee) -> dict | None:
        """Правило меры, если человек на окладе. Иначе None.

        Окладная мера отличается от прочих одним: сумма живёт в условиях найма,
        а не вводится в табель каждый месяц (`monthly: true`).
        """
        cfg = (self.p.get("work_measures") or {}).get(self.measure_of(e))
        return cfg if cfg and cfg.get("monthly") else None

    def rate_parts(self, e: Employee, ts: Timesheet) -> tuple[Decimal, Decimal]:
        """Ставка и делитель для начислений этого месяца.

        Двумя числами, а не одним, ради точности. У окладника часовой ставки
        нет — она выводится из оклада и нормы месяца, — и деление, сделанное
        заранее, оставляет хвост: 90 000 ÷ 176 × 176 даёт 90 000,000…01, то
        есть человек с полной нормой получает не ровно свой оклад. Деление
        стоит последним, и тогда норма сокращается сама.

        У почасовика делитель единица, то есть поведение ровно прежнее.
        """
        if self.monthly_measure(e) is None:
            return self.hourly_rate(e), D(1)
        norm = d(ts.norm_hours)
        if norm <= 0:
            # Делить не на что. Молчать нельзя: нулевая ставка дала бы нулевое
            # начисление окладнику, и в ведомости это выглядело бы как «месяц
            # не отработан», а не как незаведённая норма месяца.
            raise ValueError(
                "у месяца нет нормы часов — часовую ставку оклада вывести не из чего"
            )
        return self.hourly_rate(e), norm

    def rate_of(self, e: Employee, ts: Timesheet) -> Decimal:
        """Часовая ставка человека этого месяца — для следа расчёта и подсказок."""
        rate, divisor = self.rate_parts(e, ts)
        return rate / divisor

    def ledger_of(self, e: Employee) -> str:
        if e.ledger:
            return e.ledger
        return self.groups.get(e.group, {}).get("ledger", "official")

    # -- начисление -------------------------------------------------------

    def accrue_hours(self, slip: Payslip, ts: Timesheet) -> Decimal:
        """Начисление по типам часов. Каждый тип — отдельный компонент."""
        e = slip.employee
        rate, divisor = self.rate_parts(e, ts)
        ledger = self.ledger_of(e)
        total = D(0)

        for kind, cfg in self.hour_types.items():
            hours = ts.get(kind)
            if hours == 0:
                continue
            pct = d(cfg["pay_percent"])
            # Деление последним: у оклада делитель — норма месяца, и сокращаться
            # она обязана точно, иначе полная норма даёт не ровно оклад.
            amount = rate * pct * hours / divisor
            slip.add(
                code=f"hours.{kind}",
                title=cfg["title"],
                amount=amount,
                ledger=ledger,
                step=self.step(
                    f"hours.{kind}", cfg["title"], amount,
                    rule_path=f"hour_types.{kind}.pay_percent",
                    inputs={
                        "hours": hours, "rate": rate / divisor, "pay_percent": pct,
                        "base_rate": d(e.base_rate), "coefficient": d(e.coefficient),
                    },
                ),
            )
            total += amount
        return total

    def accrue_flat_salary(self, slip: Payslip, cfg: dict) -> Decimal:
        """Оклад суммой: столько же, сколько бы часов ни стояло в табеле.

        Часы при этом не пропадают из виду: они остаются в табеле, входят в
        базу взносов и в отработанное для компенсации питания — не оплачивается
        по ним только сам оклад, потому что он уже месячный.
        """
        e = slip.employee
        amount = self.hourly_rate(e)  # оклад × коэффициент условий найма
        title = cfg.get("title") or "Оклад"
        slip.add(
            code="salary",
            title=title,
            amount=amount,
            ledger=self.ledger_of(e),
            step=self.step(
                "salary", title, amount,
                rule_path="work_measures.salary.proration",
                inputs={
                    "salary": d(e.base_rate), "coefficient": d(e.coefficient),
                    "proration": cfg.get("proration", "none"),
                },
            ),
        )
        return amount

    def measure_of(self, e: Employee) -> str:
        """Чем меряется работа этого человека: своя мера сильнее правила группы."""
        return work_measure(self.groups.get(e.group), employee=e.work_measure)

    def accrue_piecework(self, slip: Payslip, ts: Timesheet, measure: str) -> Decimal:
        """Начисление сдельной работы: количество × ставка либо сама сумма.

        Что означает величина в табеле, знает правило: `pay_per_unit`. При
        `true` ставка сотрудника читается как цена единицы, а не часа — второго
        поля под «цену доставки» намеренно нет: цена работы у человека одна, и
        две означали бы два ответа на вопрос, сколько он стоит.
        """
        cfg = (self.p.get("work_measures") or {}).get(measure)
        if cfg is None:
            # Опечатка в правиле не должна оборачиваться тихим расчётом по
            # часам: правило заведено, в списке видно, а деньги пришли бы мимо
            # него. Отказ ловит `payrun.calc.check_measures` и называет людей.
            raise ValueError(f"неизвестная мера работы: {measure}")

        e = slip.employee
        quantity = d(ts.piece_value)
        per_unit = bool(cfg.get("pay_per_unit"))
        rate = self.hourly_rate(e) if per_unit else None
        amount = quantity * rate if per_unit else quantity
        title = cfg.get("title") or measure

        slip.add(
            code=f"piecework.{measure}",
            title=title,
            amount=amount,
            ledger=self.ledger_of(e),
            step=self.step(
                f"piecework.{measure}", title, amount,
                # Путь — туда, где сделан ВЫБОР способа: именно его партнёр
                # переопределяет и именно его версию надо показать в следе.
                # Само `pay_per_unit` при этом в входах, чтобы объяснение
                # читалось целиком, а не по двум местам.
                rule_path=f"groups.{e.group}.work_measure",
                # Мера, заданная человеку в условиях найма (T164), — не правило
                # страны и не переопределение партнёра: её версия живёт в
                # `employment_terms`, а пресет о ней не знает. Путь при этом
                # остаётся тем же: это ровно то правило, которое условия найма
                # перебивают, — а уровень называет того, кто решил.
                origin=TERMS_ORIGIN if e.work_measure else None,
                inputs={
                    "measure": measure, "quantity": quantity,
                    "pay_per_unit": per_unit, "rate": rate,
                    "base_rate": d(e.base_rate), "coefficient": d(e.coefficient),
                },
            ),
        )
        return amount

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
        topped_up: list[str] = []
        for kind in cfg.get("applies_to", []):
            hours = ts.get(kind)
            if hours == 0:
                continue
            paid = rate * d(self.hour_types[kind]["pay_percent"])
            if paid < floor:
                total += (floor - paid) * hours
                topped_up.append(kind)

        if total:
            # Подпись берётся из правил, а умолчание оставлено прежним. Причина:
            # подпись компонента попадает в ведомость в момент расчёта, и
            # партнёру, который ведёт учёт на другом языке, поменять её иначе
            # нечем — все остальные подписи в этом движке уже приходят из
            # конфигурации (`cfg["title"]`), а эти две были единственными
            # зашитыми. Нужно демо-стенду, который по правилу владельца всегда
            # англоязычный; на расчёт не влияет ничем.
            title = cfg.get("title") or "Доплата до минимума"
            slip.add(
                code="minimum_guarantee",
                title=title,
                amount=total,
                ledger=self.ledger_of(e),
                step=self.step(
                    "minimum_guarantee", title, total,
                    rule_path="minimum_guarantee",
                    inputs={
                        # Какая именно база — открытый вопрос бухгалтеру (Q001),
                        # поэтому в следе видно и то, что применили, и почему.
                        "base": cfg.get("base"), "floor": floor, "rate": rate,
                        "hour_types": topped_up,
                        "hours": {k: ts.get(k) for k in topped_up},
                    },
                ),
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
                ledger=cfg.get("ledger", self.ledger_of(slip.employee)),
                taxable=cfg.get("taxable", True),
                step=self.step(
                    code, cfg["title"], amount,
                    rule_path=f"allowances.{code}.amount_per_norm",
                    inputs={
                        "amount_per_norm": d(cfg["amount_per_norm"]),
                        # По дням или по часам — у бухгалтерии два несовместимых
                        # подхода на разных листах (Q003). В следе видно, какой
                        # применён к этой строке.
                        "prorate_by": mode,
                        "worked_hours": worked,
                        "hours_per_day": d(scheme.get("hours_per_day", full_day)),
                        "norm_days": norm_days,
                    },
                ),
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
        rule_path = f"schemes.{slip.employee.scheme}.gross_up"
        inputs: dict[str, Any] = {"method": method, "net": net}

        if method == "flat":
            slip.gross = net / self.rates["net_factor"]
            inputs["net_factor"] = self.rates["net_factor"]

        elif method == "net_minus_prorated_allowance":
            hours = ts.insured_hours
            divisor = d(scheme["gross_up"].get("hours_divisor", 1))
            credit = (tax_free * tax_rate / norm) * (hours / divisor)
            slip.gross = (net - credit) / self.rates["net_factor"]
            inputs |= {
                "insured_hours": hours, "hours_divisor": divisor,
                "tax_free_monthly": tax_free, "reference_norm_hours": norm,
                "income_tax": tax_rate, "net_factor": self.rates["net_factor"],
                "credit": credit,
            }

        elif method == "min_base":
            share = ts.insured_hours / norm
            half_free = tax_free / 2 * share
            min_base = self.const["min_contribution_base"]
            min_base_part = min_base * self.rates["employee_contributions"] * share
            slip.gross = (net - half_free * tax_rate + min_base_part) / (1 - tax_rate)
            slip.tax = (slip.gross - half_free) * tax_rate
            inputs |= {
                "insured_hours": ts.insured_hours, "share": share,
                "half_tax_free": half_free, "min_contribution_base": min_base,
                "employee_contributions": self.rates["employee_contributions"],
                "income_tax": tax_rate,
            }
            slip.derive(self.step(
                "tax", "Налог на доход", slip.tax,
                rule_path=f"schemes.{slip.employee.scheme}.tax",
                inputs={"method": scheme.get("tax", {}).get("method", method),
                        "gross": slip.gross, "half_tax_free": half_free,
                        "income_tax": tax_rate},
                contributes_to="tax",
            ))

        elif method == "none":
            # Выплата без пересчёта: нето и есть вся сумма (см. схему `direct`).
            slip.gross = net

        else:
            raise ValueError(f"неизвестный метод пересчета: {method}")

        slip.derive(self.step("gross", "Бруто", slip.gross, rule_path=f"{rule_path}.method",
                              inputs=inputs, contributes_to="gross"))

    def contributions(self, slip: Payslip, ts: Timesheet, scheme: dict) -> None:
        cfg = scheme["contributions"]
        method = cfg["method"]
        inputs: dict[str, Any] = {"method": method, "gross": slip.gross, "net": slip.net}

        if method == "employer_plus_withheld":
            # взносы работодателя + всё, что удержано с работника (налог + его взносы)
            slip.contributions = (
                slip.gross * self.rates["employer_contributions"] + (slip.gross - slip.net)
            )
            slip.total_cost = slip.net + slip.contributions
            inputs |= {
                "employer_contributions": self.rates["employer_contributions"],
                "withheld": slip.gross - slip.net,
            }

        elif method == "min_base_combined":
            share = ts.insured_hours / self.const["reference_norm_hours"]
            slip.contributions = (
                self.const["min_contribution_base"] * share * self.rates["combined_contributions"]
            )
            slip.total_cost = slip.net + slip.tax + slip.contributions
            inputs |= {
                "insured_hours": ts.insured_hours, "share": share,
                "min_contribution_base": self.const["min_contribution_base"],
                "combined_contributions": self.rates["combined_contributions"],
            }

        elif method == "flat_rate":
            slip.contributions = slip.gross * self.rates[cfg["rate_key"]]
            slip.total_cost = slip.net + slip.contributions
            inputs |= {"rate_key": cfg["rate_key"], "rate": self.rates[cfg["rate_key"]]}

        elif method == "none":
            # Выплата мимо начислений: взносов нет, стоимость равна выплате.
            slip.contributions = D(0)
            slip.total_cost = slip.net

        else:
            raise ValueError(f"неизвестный метод взносов: {method}")

        path = f"schemes.{slip.employee.scheme}.contributions.method"
        slip.derive(self.step("contributions", "Взносы", slip.contributions,
                              rule_path=path, inputs=inputs, contributes_to="contributions"))
        slip.derive(self.step(
            "total_cost", "Полная стоимость", slip.total_cost, rule_path=path,
            inputs={"net": slip.net, "tax": slip.tax, "contributions": slip.contributions},
            contributes_to="total_cost",
        ))

    # -- главный вход -----------------------------------------------------

    def calculate(self, e: Employee, ts: Timesheet) -> Payslip:
        slip = Payslip(employee=e)
        scheme = self.schemes[e.scheme]

        measure = self.measure_of(e)
        monthly = self.monthly_measure(e)

        if monthly is not None and monthly.get("proration") == "none":
            # Оклад суммой: часы на него не влияют вовсе. Отдельной веткой, а не
            # пропорцией с коэффициентом 1: у такого оклада нет часовой ставки
            # даже выведенной, и надбавки за ночные считать не от чего.
            earned = self.accrue_flat_salary(slip, monthly)
        elif measure == HOURS or monthly is not None:
            # для полставки норма часов по умолчанию — половина месячной,
            # но фактически отработанное всегда важнее (увольнение, приём в середине месяца)
            if scheme.get("worked_hours_source") == "half_of_norm" and not ts.hours.get("regular"):
                ts.hours["regular"] = ts.norm_hours / 2

            earned = self.accrue_hours(slip, ts)
        else:
            earned = self.accrue_piecework(slip, ts, measure)
            # Часы у сдельной группы денег не дают. Молчать об этом нельзя:
            # человек видит в табеле 168 часов и ждёт за них начисления.
            if any(d(value) != 0 for value in ts.hours.values()):
                slip.notes.append(
                    "часы не оплачены: работа этой группы меряется сдельно"
                )

        if ts.manual_correction is not None:
            # если бухгалтер поставил правку руками — уважаем ее и помечаем.
            # Подпись — из правил с прежним умолчанием, по той же причине, что у
            # доплаты до минимума: это единственные две подписи движка, которые
            # нельзя было поменять конфигурацией.
            correction_title = (
                (self.p.get("manual_correction") or {}).get("title")
                or "Ручная корректировка"
            )
            slip.add(
                code="manual_correction",
                title=correction_title,
                amount=d(ts.manual_correction),
                ledger=self.ledger_of(e),
                # Правка руками — не правило, а ввод. Выдавать её в следе за
                # сработавшее правило нельзя: объяснять тогда будет нечего.
                step=TraceStep(
                    rule_code="manual_correction", title=correction_title,
                    applied_value=d(ts.manual_correction),
                    input_values={"amount": d(ts.manual_correction)},
                    rule_version_id=None, source_level="input",
                ),
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
