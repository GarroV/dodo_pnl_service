"""Печатные формы: платёжная ведомость и расчётный листок (T187, issue #184).

Экран объясняет расчёт, бумага его закрепляет. Ведомость подписывают сотрудники
при получении денег, расчётный листок отдают человеку на руки. Отсюда три
решения, на которых держится модуль.

**Разбиение на листы считает продукт, а не браузер.** Браузер умеет разложить
таблицу по страницам сам, но он не умеет сказать, сколько их вышло, — а на
подписной бумаге «Лист 2 из 3» несёт смысл: по нему замечают потерянный лист.
Поэтому лист здесь величина известная: 210 × 297 мм, поля, высота строки и
высота подвала объявлены в миллиметрах, вместимость выводится из них
арифметикой, и та же арифметика проверяется тестом. Разметка берёт эти же
миллиметры переменными CSS, чтобы двум правдам о высоте листа неоткуда было
взяться (`web/static/web/print.css`).

**Числа на бумаге сходятся между собой.** «Начислено − удержано = к выплате»
верно построчно и в итоге по построению: удержано не спрашивается второй
выборкой, а выводится из тех же итогов строки (`net − to_bank − to_cash`). Второй
источник того же числа однажды разошёлся бы с первым, и разбирались бы с этим не
на экране, а с сотрудником у кассы.

**Что нельзя собрать честно — не собирается вовсе.** «Начислено», «удержано» и
«к выплате» посчитаны по строке ведомости целиком и живут в `payslip_totals`,
закрытой своей политикой (T050): роль видит их, только если видит всю строку.
Роли с неполным набором регистров ведомость показала бы часть людей и итог,
который ни с чем не сходится, — то есть документ, которым нельзя пользоваться,
но который выглядит настоящим. Разрезу по регистру эти числа не принадлежат
вовсе: налог и взносы считаются по строке целиком (тот же довод, что в T141).
В обоих случаях документ не печатается, а называет причину — свою для каждого
случая (T120, T134, T141: одна фраза на все причины была бы неправдой в двух
случаях из трёх).

**Чего в формах нет и почему.** Номера банковского счёта, даты выплаты и
серийного номера документа в схеме нет, а схему в этой волне ведёт другой блок.
Выдуманный счёт на расчётном листке хуже отсутствующего: по нему человек пойдёт
искать деньги. Поэтому номер документа выводится из периода (`VD-2026-06`), а
счёта и даты выплаты на форме нет — см. журнал блока.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any
from uuid import UUID

from django.utils.translation import gettext as _
from django.utils.translation import gettext_noop

# =============================================================================
# Геометрия листа
# =============================================================================
# Миллиметры, а не «на глаз влезло»: те же числа стоят переменными в
# `web/static/web/print.css`, и тест сверяет их между собой. Разъедься они —
# продукт напишет «Лист 1 из 2», а браузер напечатает три, из которых один
# пустой, и заметит это тот, кто уже понёс бумагу на подпись.

SHEET_WIDTH_MM = 210
SHEET_MM = 297
MARGIN_TOP_MM = 14
MARGIN_BOTTOM_MM = 12
# Шапка документа: название партнёра, заголовок, разрез и номер. На листах
# продолжения она короче — там достаточно напомнить, чей это документ.
HEAD_FIRST_MM = 27
HEAD_NEXT_MM = 12
TABLE_HEAD_MM = 8
ROW_MM = 5.5
# Подвал: итоговая строка, сумма прописью, три подписи и мелкий шрифт. Стоит
# только на последнем листе — и, если не влезает, уезжает на свой собственный.
FOOT_MM = 52

USABLE_MM = SHEET_MM - MARGIN_TOP_MM - MARGIN_BOTTOM_MM


def capacity(*, first: bool, last: bool) -> int:
    """Сколько строк влезет на лист такого рода."""
    left = (
        USABLE_MM
        - (HEAD_FIRST_MM if first else HEAD_NEXT_MM)
        - TABLE_HEAD_MM
        - (FOOT_MM if last else 0)
    )
    return max(int(left // ROW_MM), 0)


@dataclass(frozen=True)
class Leaf:
    """Один лист бумаги: свои строки и знание о своём месте в пачке."""

    rows: list
    first: bool
    last: bool
    number: int
    of: int


def paginate(rows: list) -> list[Leaf]:
    """Разложить строки по листам так, чтобы ничего не обрезалось.

    Подвал не делит лист со строками, которым не осталось места: наивное
    разбиение поставило бы итог, сумму прописью и подписи поверх последних
    строк — и они пропали бы молча. Если строки заняли лист целиком, подвал
    уезжает на отдельный, пустой от строк.
    """
    laid: list[tuple[list, bool, bool]] = []
    taken = 0
    first = True
    while True:
        left = len(rows) - taken
        if left <= capacity(first=first, last=True):
            laid.append((rows[taken:], first, True))
            break
        room = capacity(first=first, last=False)
        laid.append((rows[taken:taken + room], first, False))
        taken += room
        first = False

    return [
        Leaf(rows=body, first=head, last=foot, number=number, of=len(laid))
        for number, (body, head, foot) in enumerate(laid, start=1)
    ]


# =============================================================================
# Отказы: почему документ не собран
# =============================================================================
# Кодом, а не готовой фразой, — по образцу T141: код называет положение дел, а
# слова к нему подбираются там, где известен язык читателя. Один и тот же код
# объясняет и страницу печати, и подпись рядом с кнопкой на экране: две
# формулировки одного и того же разъехались бы молча.

BY_CUT = "cut"                  # выбран разрез: «к выплате» не принадлежит регистру
TOTALS_WITHHELD = "withheld"    # итоги строк роли не отданы
NOT_CALCULATED = "absent"       # печатать нечего: расчёта нет

REFUSALS = {
    BY_CUT: gettext_noop(
        "Ведомость печатается по всему расчёту, а не по разрезу: «начислено», "
        "«удержано» и «к выплате» считаются по строке ведомости целиком и "
        "регистру не принадлежат. Снимите разрез и откройте форму снова."
    ),
    # Ни сумм, ни имён, ни названия регистра: это факт о правах, а не о данных
    # (D023). Он верен и тогда, когда скрывать нечего, — вычесть из него нельзя.
    TOTALS_WITHHELD: gettext_noop(
        "Эта форма не собирается для вашей роли: суммы «к выплате» считаются по "
        "строке ведомости целиком, а итоги расчёта вашей роли не отданы. "
        "Собранный документ показал бы часть людей и итог, который ни с чем не "
        "сходится, — возьмите форму у того, кому отдан весь расчёт."
    ),
    NOT_CALCULATED: gettext_noop(
        "Печатать нечего: расчёт этого месяца ещё не выполнен. Посчитайте "
        "период, и форма соберётся."
    ),
}


def refusal_text(code: str) -> str:
    """Слова к причине отказа на языке читателя."""
    return _(REFUSALS[code]) if code in REFUSALS else ""


def payout_refusal(*, has_rows: bool, whole_run: bool, cut: str) -> str:
    """Код причины, по которой ведомость не печатается, или пусто.

    Порядок проверок не произвольный, и он тот же, что у надписи о налогах
    (T141): **разрез решает раньше прав**. Иначе роль, открывшая разрез, читала
    бы про права там, где дело в разрезе, — то есть экран и бумага объясняли бы
    одно и то же по-разному.

    Права решают раньше наличия строк: у роли, которой итоги не отданы, строк
    не бывает вовсе, и «расчёт не выполнен» было бы прямой неправдой.
    """
    if cut:
        return BY_CUT
    if not whole_run:
        return TOTALS_WITHHELD
    if not has_rows:
        return NOT_CALCULATED
    return ""


# =============================================================================
# Платёжная ведомость
# =============================================================================


@dataclass(frozen=True)
class PayoutRow:
    """Строка ведомости: человек, его часы и три суммы, которые сходятся.

    `accrued` — то, что человек заработал по расчёту (нето: в Сербии
    договариваются о сумме на руки, и движок считает бруто от неё, а не
    наоборот). `held` — вычтенное при выплате. `paid` — то, что уходит
    человеку, на счёт и наличными вместе, и именно за него он расписывается.
    """

    number: int
    employee: str
    position: str
    unit: str
    hours: Decimal
    accrued: Decimal
    held: Decimal
    paid: Decimal


@dataclass(frozen=True)
class Payout:
    """Платёжная ведомость целиком: шапка, строки, итоги и листы."""

    entity: str
    tax_number: str
    period: date
    currency: str
    units: list[str]
    number: str
    rows: list[PayoutRow]
    hours: Decimal
    accrued: Decimal
    held: Decimal
    paid: Decimal
    people: int
    leaves: list[Leaf]
    # Кто посчитал и кто утвердил — подписи под документом. Пусто, если ещё
    # никто: пустая линия для подписи от руки честнее выдуманного имени.
    calculated_by: str
    approved_by: str

    def __bool__(self) -> bool:
        return bool(self.rows)


def document_number(kind: str, period: date) -> str:
    """Номер документа, выведенный из периода.

    Реестра номеров в схеме нет, а схему в этой волне ведёт другой блок.
    Выведенный номер честнее выдуманного порядкового: он повторяется при
    повторной печати того же месяца и ничего не обещает о регистрации.
    """
    return f"{kind}-{period:%Y-%m}"


def _hours_of(row) -> Decimal:
    """Часы строки табеля — все виды вместе.

    То же правило, что у истории человека (`payrun.person._hours_of`): документ
    отвечает на «сколько времени человек отдал работе», а не на «что вошло в
    базу для взносов». Правило продублировано осознанно — модуль чужого блока
    отсюда не правится, — и закреплено тестом, чтобы разъезд был красным, а не
    молчаливым.
    """
    return sum((Decimal(str(value)) for value in (row.hours or {}).values()), Decimal(0))


def month_hours(tenant_id: UUID, period: date) -> dict[UUID, tuple[Decimal, Decimal]]:
    """Часы и норма месяца по сотрудникам: {employee_id: (отработано, норма)}.

    Строк табеля у человека бывает две — перевод между точками посреди месяца
    даёт по строке на точку. Часы складываются, норма берётся наибольшей: это
    норма календаря, одна на месяц, а не свойство точки.
    """
    from core.models import Timesheet

    found: dict[UUID, tuple[Decimal, Decimal]] = {}
    for row in Timesheet.objects.filter(tenant_id=tenant_id, period=period):
        worked, norm = found.get(row.employee_id, (Decimal(0), Decimal(0)))
        found[row.employee_id] = (worked + _hours_of(row), max(norm, row.norm_hours))
    return found


def positions(tenant_id: UUID, period: date) -> dict[UUID, str]:
    """Должность каждого сотрудника на этот месяц.

    Условия найма версионированы, поэтому берётся то, что действовало в месяце,
    а не последнее заведённое: у уволенного в июне в июльской бумаге должности
    быть не должно, а в июньской — должна.
    """
    from core.models import EmploymentTerm

    found: dict[UUID, tuple[date, str]] = {}
    terms = EmploymentTerm.objects.filter(
        tenant_id=tenant_id, valid_from__lte=_month_end(period)
    ).select_related("position")
    for term in terms:
        if term.valid_to is not None and term.valid_to < period:
            continue
        title = term.position.title if term.position_id else ""
        known = found.get(term.employee_id)
        if known is None or known[0] <= term.valid_from:
            found[term.employee_id] = (term.valid_from, title)
    return {employee: title for employee, (_from, title) in found.items()}


def _month_end(period: date) -> date:
    return (
        date(period.year + 1, 1, 1) if period.month == 12
        else date(period.year, period.month + 1, 1)
    )


def _people(ids: set[UUID]) -> dict[UUID, str]:
    """Имена тех, кто подписывает документ снизу."""
    from core.models import User

    if not ids:
        return {}
    return {
        user.id: (user.full_name or user.username)
        for user in User.objects.filter(id__in=ids)
    }


def build_payout(tenant_id: UUID, period: date) -> Payout:
    """Собрать платёжную ведомость периода.

    Ни одного фильтра по регистру и по точке здесь нет: их ставит база (D014).
    Выборка идёт от `payslip_totals`, и это и есть граница видимости — итоги
    видны роли, только если ей видна вся строка (T050). Роль, которой они не
    отданы, получает пустую ведомость, и представление отвечает ей отказом с
    названной причиной, а не документом с дырами.
    """
    from core.models import Payrun, PayslipTotals, Tenant

    tenant = Tenant.objects.filter(id=tenant_id).first()
    hours = month_hours(tenant_id, period)
    roles = positions(tenant_id, period)

    totals = list(
        PayslipTotals.objects.filter(
            tenant_id=tenant_id, payslip__payrun__period=period
        ).select_related("payslip__employee", "payslip__unit__legal_entity")
    )
    totals.sort(
        key=lambda row: (
            row.payslip.employee.last_name, row.payslip.employee.first_name
        )
    )

    rows = []
    for number, row in enumerate(totals, start=1):
        slip = row.payslip
        worked, _norm = hours.get(slip.employee_id, (Decimal(0), Decimal(0)))
        # Удержанное не спрашивается второй выборкой: оно выводится из тех же
        # итогов строки. Иначе «начислено − удержано = к выплате» держалось бы
        # на согласии двух источников, а не на арифметике.
        paid = row.to_bank + row.to_cash
        rows.append(PayoutRow(
            number=number,
            employee=f"{slip.employee.last_name} {slip.employee.first_name}".strip(),
            position=roles.get(slip.employee_id, ""),
            unit=(slip.unit.title or slip.unit.code) if slip.unit_id else "",
            hours=worked,
            accrued=row.net,
            held=row.net - paid,
            paid=paid,
        ))

    units = sorted({row.unit for row in rows if row.unit})
    entity, tax_number = _entity_of(tenant, [row.payslip for row in totals])
    payrun = Payrun.objects.filter(tenant_id=tenant_id, period=period).first()
    signed = _people({
        who for who in (
            payrun.approved_by if payrun else None,
            _who_calculated(tenant_id, period),
        ) if who is not None
    })

    return Payout(
        entity=entity,
        tax_number=tax_number,
        period=period,
        currency=tenant.base_currency if tenant else "",
        units=units,
        number=document_number("VD", period),
        rows=rows,
        # Итоги складываются из показанных строк, а не берутся отдельной
        # выборкой: итог, больший суммы строк, выдал бы скрытое вычитанием.
        hours=sum((row.hours for row in rows), Decimal(0)),
        accrued=sum((row.accrued for row in rows), Decimal(0)),
        held=sum((row.held for row in rows), Decimal(0)),
        paid=sum((row.paid for row in rows), Decimal(0)),
        people=len({row.employee for row in rows}),
        leaves=paginate(rows),
        calculated_by=signed.get(_who_calculated(tenant_id, period), ""),
        approved_by=signed.get(payrun.approved_by if payrun else None, ""),
    )


def _who_calculated(tenant_id: UUID, period: date):
    """Кто запускал расчёт последним — его имя стоит под «расчёт составил»."""
    from core.models import PayrunJob

    job = (
        PayrunJob.objects.filter(tenant_id=tenant_id, period=period)
        .order_by("-created_at").first()
    )
    return job.requested_by if job else None


def _entity_of(tenant, slips) -> tuple[str, str]:
    """Чьё это юридическое лицо и его налоговый номер.

    У партнёра точек бывает несколько и юридических лиц тоже. Пока лицо одно —
    в шапке стоит оно с налоговым номером; как только их больше, вместо номера
    не ставится ничего: чужой ПИБ на подписной ведомости — это документ не того
    юридического лица.
    """
    seen = {
        slip.unit.legal_entity for slip in slips
        if slip.unit_id and slip.unit.legal_entity_id
    }
    if len(seen) == 1:
        only = next(iter(seen))
        return only.title, only.tax_number or ""
    return (tenant.title if tenant else ""), ""


# =============================================================================
# Расчётный листок
# =============================================================================


@dataclass(frozen=True)
class SlipLine:
    """Позиция листка: за что начислено, как посчитано и сколько."""

    code: str
    title: str
    formula: str
    amount: Decimal


@dataclass(frozen=True)
class Slip:
    """Расчётный листок одного человека за месяц."""

    entity: str
    tax_number: str
    period: date
    currency: str
    employee: str
    position: str
    unit: str
    hours: Decimal
    norm_hours: Decimal
    number: str
    lines: list[SlipLine]
    accrued: Decimal
    held: Decimal
    to_bank: Decimal
    to_cash: Decimal
    gross: Decimal
    tax: Decimal
    contributions: Decimal

    @property
    def paid(self) -> Decimal:
        return self.to_bank + self.to_cash


class SlipNotFound(LookupError):
    """Строки нет или она не видна — для человека это одно и то же."""


class SlipWithheld(LookupError):
    """Строка видна, а её итоги роли не отданы: листок собрать не из чего."""


def build_slip(tenant_id: UUID, payslip_id) -> Slip:
    """Собрать расчётный листок по строке ведомости.

    Итоги спрашиваются у `payslip_totals` и ничем не досчитываются: роль, у
    которой они закрыты, обязана получить отказ, а не листок с нулями. Ноль на
    бумаге читается как «начислено ноль», то есть как утверждение о деньгах.
    """
    from core.models import PayComponent, PayslipStep, PayslipTotals, Tenant

    totals = (
        PayslipTotals.objects
        .filter(tenant_id=tenant_id, payslip_id=payslip_id)
        .select_related("payslip__employee", "payslip__unit__legal_entity", "payslip__payrun")
        .first()
    )
    if totals is None:
        # Строки нет вовсе, или она видна, а её итоги — нет. Различаем по самой
        # строке: 404 на чужую строку обязан быть неотличим от 404 на
        # несуществующую (как у следа расчёта), а вот у своей строки с
        # закрытыми итогами причина называется словами.
        from core.models import Payslip

        if Payslip.objects.filter(tenant_id=tenant_id, pk=payslip_id).exists():
            raise SlipWithheld(payslip_id)
        raise SlipNotFound(payslip_id)

    slip = totals.payslip
    period = slip.payrun.period
    tenant = Tenant.objects.filter(id=tenant_id).first()
    worked, norm = month_hours(tenant_id, period).get(
        slip.employee_id, (Decimal(0), Decimal(0))
    )

    formulas = {
        step.code: step
        for step in PayslipStep.objects.filter(
            tenant_id=tenant_id, payslip_id=payslip_id
        ).order_by("position")
    }
    lines = [
        SlipLine(
            code=component.code,
            title=component.title,
            formula=_formula_of(formulas.get(component.code)),
            amount=component.amount,
        )
        for component in PayComponent.objects.filter(
            tenant_id=tenant_id, payslip_id=payslip_id
        ).order_by("code")
    ]

    paid = totals.to_bank + totals.to_cash
    return Slip(
        entity=_entity_of(tenant, [slip])[0],
        tax_number=_entity_of(tenant, [slip])[1],
        period=period,
        currency=tenant.base_currency if tenant else "",
        employee=f"{slip.employee.last_name} {slip.employee.first_name}".strip(),
        position=positions(tenant_id, period).get(slip.employee_id, ""),
        unit=(slip.unit.title or slip.unit.code) if slip.unit_id else "",
        hours=worked,
        norm_hours=norm,
        number=document_number("RL", period),
        lines=lines,
        # Начислено — сумма показанных позиций, а не отдельное поле: листок
        # обязан объяснять своё же число, иначе объяснять нечего.
        accrued=sum((line.amount for line in lines), Decimal(0)),
        held=totals.net - paid,
        to_bank=totals.to_bank,
        to_cash=totals.to_cash,
        gross=totals.gross,
        tax=totals.tax,
        contributions=totals.contributions,
    )


def _formula_of(step: Any) -> str:
    """Как посчитана позиция — числами, по которым сумма повторяется.

    Пусто, если шага не сохранилось: выдуманная формула на листке хуже
    отсутствующей, по ней человек попробует сойтись и не сойдётся.
    """
    if step is None:
        return ""
    values = step.input_values or {}
    rate = values.get("rate") or values.get("base_rate")
    if step.applied_value is not None and rate:
        return f"{_plain(step.applied_value)} × {_plain(rate)}"
    return ""


def _plain(value) -> str:
    """Число как оно есть, без разделителей: формула читается калькулятором."""
    if isinstance(value, dict):
        value = value.get("value", "")
    text = str(value)
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text
