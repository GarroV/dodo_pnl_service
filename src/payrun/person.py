"""История выплат по одному человеку: месяц за месяцем и из чего сложилось (T166).

**Зачем это есть.** Карточка сотрудника показывала только историю условий найма —
ставку и группу. Денег человека не было ни на одном экране: `Payslip` нигде не
фильтровался по человеку вовсе. Владелец 18.08.2026: «нужен будет функционал
чтобы можно было посмотреть статистику по отдельному человеку». Вопрос, на
который отвечает этот модуль, — «сколько этот человек получал по месяцам и
почему столько».

**Главное решение: итог собирается из компонентов, а не из итогов расчёта.** То
же самое, на чём стоит ведомость (`payrun.sheet`), и по той же причине. У
`payslip_totals` своя ограничивающая политика: числа там посчитаны по всем
регистрам сразу, и видит их только тот, кому видны **все регистры вообще** —
причём независимо от содержимого строки (`app_sees_every_ledger`, миграция
`0023`). Сумма же по месяцу здесь равна сумме **показанных** строк: роль с
урезанным набором регистров видит итог ровно по тем строкам, которые ей отдала
база. Это и есть инвариант, ради которого модуль написан именно так: показать
сумму, собранную из строк, которых смотрящий прочитать не может, — утечка через
агрегат, и она хуже прямого показа, потому что её не видно глазами.

**Этот экран — первый читатель `payslip_totals` в продукте.** До него в таблицу
только писали (сказано прямо в миграции `0023`), и это стоит помнить: политика у
неё устроена так, что состояний ровно два — «итоги есть у всех строк» и «итогов
нет ни у одной». Третьего, по которому кого-то опознают по отсутствию числа, не
существует, и появиться оно не должно (T071). Поэтому производные числа тут
никогда не собираются частично: месяц либо отдаёт их целиком, либо не отдаёт
вовсе.

**Месяцы берутся из видимых компонентов, а не из табеля.** Табель регистром не
режется — часы не свойство регистра, — поэтому список месяцев, собранный по
нему, назвал бы и месяц, в котором у человека были только скрытые от роли
выплаты. Само появление такого месяца (пусть с прочерком вместо суммы) — это
сообщение «здесь есть деньги, которых тебе не видно», то есть та же утечка на
один бит, что и счётчик скрытых строк в `web.runslice`. Часы поэтому
**приписываются** к уже найденным месяцам, а своих месяцев не приносят.

**Производные величины (бруто, налог, взносы, полная стоимость, каналы выплаты)
не пересобираются здесь ни из чего.** Они берутся только из `payslip_totals` и
только целиком: нет строки — значит, роли её не отдали, и на экране прочерк.
Считать их из компонентов нельзя (`gross` и налог не являются компонентами и из
них не выводятся — разбор в миграции `0009`), а брать наличную часть из табеля
(`timesheets.cash_payout`, откуда она в расчёт и попадает) — прямая утечка:
табель посчитан по всему человеку и политики регистров на нём нет.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from uuid import UUID

from .sheet import Column, _column_key, _ledger_key

__all__ = ["History", "Month", "Piece", "Totals", "build_history"]


@dataclass(frozen=True)
class Totals:
    """Итоги строки ведомости за месяц — те, что посчитаны по всем регистрам.

    Отдельным объектом, а не полями в `Month`, намеренно: они приходят и
    исчезают **вместе**. Отсутствие здесь означает не «ноль», а «роли итоги
    расчёта не отданы», и различить это на экране можно только если пустота одна
    на всю группу чисел.
    """

    net: Decimal
    gross: Decimal
    tax: Decimal
    contributions: Decimal
    total_cost: Decimal
    to_bank: Decimal
    to_cash: Decimal


@dataclass(frozen=True)
class Month:
    """Один месяц человека: что начислено по видимым строкам и что вокруг."""

    period: date
    payslip_id: UUID | None
    unit: str
    # Сумма видимых компонентов месяца. Не `payslip_totals.net`: см. шапку.
    accrued: Decimal
    # Вклад каждого видимого регистра. Пара, а не словарь: порядок регистров на
    # экране обязан быть одним и тем же от месяца к месяцу.
    by_ledger: list[tuple[str, Decimal]]
    # Часы табеля этого месяца и норма. `None` — табеля нет вовсе, и это не
    # ноль: «часов не вносили» и «ноль часов» на экране обязаны отличаться так
    # же, как отличаются в базе.
    hours: Decimal | None
    norm_hours: Decimal | None
    totals: Totals | None
    frozen: bool
    freeze_reason: str
    # Доля от самого большого месяца истории, 0–100. Считается здесь, а не в
    # разметке: это арифметика по тем же видимым суммам, и второй её источник
    # разъехался бы с числами в таблице.
    share: int


@dataclass(frozen=True)
class Piece:
    """Строка разбора «из чего складывается»: месяц × регистр × происхождение.

    Регистр в ключе строки по той же причине, что и в ведомости (D023): регистр
    — свойство компонента, а не человека, и у одного месяца их бывает два. Слив
    их в одну строку, экран показал бы сумму, у которой нет одного регистра, —
    то есть число, которое нельзя объяснить.

    Месяц-источник тоже в ключе: разница за закрытый месяц (T026) обязана
    остаться отдельной строкой, иначе перенос по `hours.regular` сольётся с
    обычной колонкой того же кода и станет невидимым.
    """

    period: date
    ledger: str
    retro_source: date | None
    amounts: dict[str, Decimal]
    total: Decimal
    payslip_id: UUID | None

    @property
    def is_retro(self) -> bool:
        return self.retro_source is not None


@dataclass(frozen=True)
class History:
    """Вся видимая история человека. Пустая — значит выплат этой роли не видно."""

    # Свежий месяц первым: историю человека читают с последнего месяца.
    months: list[Month]
    columns: list[Column]
    pieces: list[Piece]
    column_totals: dict[str, Decimal]
    # Итоги по всей истории. Складываются те же показанные строки — второй
    # выборки «итого по человеку» здесь нет и быть не должно.
    accrued: Decimal
    hours: Decimal | None

    def __bool__(self) -> bool:
        return bool(self.months)


def _hours_of(row) -> Decimal:
    """Сколько часов в строке табеля — все виды вместе.

    Складываются значения как есть, без выбора «зачётных»: экран отвечает на
    «сколько времени человек отдал работе», а не на «что вошло в базу для
    взносов» (для этого у табеля есть своя колонка `insured_hours`).
    """
    return sum(
        (Decimal(str(value)) for value in (row.hours or {}).values()), Decimal(0)
    )


def _timesheet_hours(tenant_id: UUID, employee_id: UUID) -> dict[date, tuple[Decimal, Decimal]]:
    """Часы и норма по месяцам. Складываются строки, потому что их бывает две.

    Перевод человека между точками посреди месяца даёт две строки табеля — по
    одной на точку (`core.models.TimesheetDay`). Взять первую значило бы показать
    половину месяца как весь месяц.

    Норма при этом складываться не должна: это норма календаря, одна на месяц, а
    не свойство точки. Берётся наибольшая из строк — у полного месяца она у всех
    одна и та же, а у неполной строки бывает меньше.
    """
    from core.models import Timesheet

    found: dict[date, tuple[Decimal, Decimal]] = {}
    for row in Timesheet.objects.filter(tenant_id=tenant_id, employee_id=employee_id):
        worked, norm = found.get(row.period, (Decimal(0), Decimal(0)))
        found[row.period] = (worked + _hours_of(row), max(norm, row.norm_hours))
    return found


def build_history(tenant_id: UUID, employee_id: UUID, *, who) -> History:
    """История выплат человека — всё, что видно этой роли, и ничего больше.

    Ни одного фильтра по регистру и по точке здесь нет: их ставит база. Забытый
    фильтр в новом экране обязан давать пустоту, а не чужие данные (D014), и
    единственный способ это сохранить — не писать второй отбор рядом с
    политиками.

    **Почему `who` обязателен, хотя для запросов он не нужен.** Разбор дифа
    свежим взглядом доказал прогоном, а не рассуждением: под ролью управляющего
    точки база **отдаёт** суммы официального регистра по его собственной точке.
    Политики режут по регистру и по точке, но не по праву вести расчёт — и это
    решение осознанное, его причина записана в миграции `0022`: «право править
    табель — не право его видеть, администратор сети обязан видеть данные,
    которые не правит».

    Значит на оси «кому вообще открыты суммы расчёта» база не подстрахует, и до
    этой правки запрет держался ровно одной строкой в одном представлении.
    Появился бы второй вызов — выгрузка, API, фоновая задача, — и роль без права
    молча получила бы настоящие суммы. Теперь функцию нельзя позвать, не назвав
    смотрящего, и она сама отказывает: защита переехала из вызывающего в
    вызываемое. Это не замена политике (её вопрос отдельный, issue заведён), а
    отказ от предположения, что вызов один и он всегда правильный.
    """
    from web import permissions

    permissions.check(who, permissions.PAYRUN_CALCULATE)
    from core.models import PayComponent, PayslipFreeze, PayslipTotals

    components = list(
        PayComponent.objects.filter(
            tenant_id=tenant_id, payslip__employee_id=employee_id
        ).select_related("payslip", "payslip__payrun", "payslip__unit")
    )

    # Итоги — по строкам ведомости, и строки может не оказаться: политика
    # `ledger_visibility` (миграция `0023`) отдаёт их только тому, кому видны все
    # регистры учёта вообще. Пустота здесь — ответ, а не пробел, и она одна на
    # все месяцы: у роли с неполным набором регистров итогов не будет ни у
    # одного месяца, и по отсутствию числа никого нельзя опознать.
    totals = {
        row.payslip_id: Totals(
            net=row.net, gross=row.gross, tax=row.tax,
            contributions=row.contributions, total_cost=row.total_cost,
            to_bank=row.to_bank, to_cash=row.to_cash,
        )
        for row in PayslipTotals.objects.filter(
            tenant_id=tenant_id, payslip__employee_id=employee_id
        )
    }
    freezes = {
        row.payslip_id: row
        for row in PayslipFreeze.objects.filter(
            tenant_id=tenant_id,
            payslip__employee_id=employee_id,
            released_at__isnull=True,
        )
    }
    hours = _timesheet_hours(tenant_id, employee_id)

    titles: dict[str, str] = {}
    pieces: dict[tuple[date, str, date | None], dict] = {}
    by_month: dict[date, dict] = {}

    for item in components:
        period = item.payslip.payrun.period
        titles.setdefault(item.code, item.title)

        piece = pieces.setdefault(
            (period, item.ledger, item.retro_source_period),
            {"amounts": {}, "payslip_id": item.payslip_id},
        )
        piece["amounts"][item.code] = (
            piece["amounts"].get(item.code, Decimal(0)) + item.amount
        )

        month = by_month.setdefault(period, {
            "payslip_id": item.payslip_id,
            "unit": item.payslip.unit.code if item.payslip.unit_id else "",
            "accrued": Decimal(0),
            "by_ledger": {},
        })
        month["accrued"] += item.amount
        month["by_ledger"][item.ledger] = (
            month["by_ledger"].get(item.ledger, Decimal(0)) + item.amount
        )

    columns = [Column(code, titles[code]) for code in sorted(titles, key=_column_key)]

    # Полоса динамики мерится самым большим месяцем истории, а не абсолютной
    # шкалой: у одного человека месяцы отличаются на проценты, и полоса от нуля
    # до «сколько бывает вообще» не показала бы ничего.
    top = max((body["accrued"] for body in by_month.values()), default=Decimal(0))

    months = [
        Month(
            period=period,
            payslip_id=body["payslip_id"],
            unit=body["unit"],
            accrued=body["accrued"],
            by_ledger=sorted(
                body["by_ledger"].items(), key=lambda pair: _ledger_key(pair[0])
            ),
            hours=hours[period][0] if period in hours else None,
            norm_hours=hours[period][1] if period in hours else None,
            totals=totals.get(body["payslip_id"]),
            frozen=body["payslip_id"] in freezes,
            freeze_reason=(
                freezes[body["payslip_id"]].reason
                if body["payslip_id"] in freezes else ""
            ),
            share=(
                int(body["accrued"] / top * 100) if top > 0 else 0
            ),
        )
        for period, body in sorted(by_month.items(), reverse=True)
    ]

    ordered_pieces = [
        Piece(
            period=period, ledger=ledger, retro_source=source,
            amounts=body["amounts"],
            total=sum(body["amounts"].values(), Decimal(0)),
            payslip_id=body["payslip_id"],
        )
        # Свежий месяц первым — как в таблице месяцев; внутри месяца порядок
        # регистров тот же, что в ведомости, а разница идёт сразу за своей
        # обычной строкой (пустой источник сортируется первым).
        for (period, ledger, source), body in sorted(
            pieces.items(),
            key=lambda item: (
                -item[0][0].toordinal(),
                _ledger_key(item[0][1]),
                item[0][2] or date.min,
            ),
        )
    ]

    column_totals: dict[str, Decimal] = {}
    for piece in ordered_pieces:
        for code, amount in piece.amounts.items():
            column_totals[code] = column_totals.get(code, Decimal(0)) + amount

    worked = [month.hours for month in months if month.hours is not None]

    return History(
        months=months,
        columns=columns,
        pieces=ordered_pieces,
        column_totals=column_totals,
        accrued=sum((month.accrued for month in months), Decimal(0)),
        hours=sum(worked, Decimal(0)) if worked else None,
    )
