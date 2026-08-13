"""Правки задним числом: разница едет вперёд, закрытый месяц не трогают (T026).

**Что здесь считается правкой задним числом.** Не событие записи, а
**расхождение**: утверждённый месяц хранит одни числа, а сегодняшние данные дали
бы другие. Определение одно, и оно покрывает все случаи сразу — правку часов,
изменение правила расчёта, изменение условий найма, — потому что все они входы
одного и того же `calc.compute`.

Ловить на записи было бы неверно, и не из соображений удобства:

- сторожей понадобилось бы столько, сколько входов (`timesheets`,
  `timesheet_days`, `employment_terms`, `rule_presets`, `rule_overrides`,
  календарь), и каждый новый вход обязан не забыть про свой — то есть гарантия
  держалась бы на дисциплине, ровно на том, от чего уходит D014;
- **изменение правила вообще не является записью «в закрытый период».** Ставка
  правится в версионированной таблице, и какие месяцы задеты, из самой записи не
  видно: это зависит от `valid_from`, от условий найма каждого человека и от
  того, какие месяцы вообще утверждены.

Поэтому месяц считается заново **в памяти** (`calc.compute` ничего не пишет по
построению) и сравнивается с хранимым. Закрытый период при этом не может
измениться физически: сторож `payrun_frozen_guard` из T023 отвергает любую
запись в утверждённый расчёт, включая владельца таблиц.

**Накопление.** Разница считается как

    свежий расчёт − хранимый расчёт − уже перенесённое (неотменённое)

иначе вторая правка перенесла бы первую ещё раз. Отсюда же бесплатно получается
проверка: сразу после переноса расхождение равно нулю.

**Регистр** удерживается сам: разница считается покомпонентно, а регистр —
свойство компонента, а не человека. Правка, задевшая надбавку в официальном и
часы в дополнительном, даёт две строки в двух регистрах, и они не складываются.

**Сравниваются только сопоставимые величины — срез роли со срезом роли** (T085).
Хранимое отдаёт база, и отдаёт она его в срезе роли; свежий расчёт движок
считает целиком, потому что считает он деньги, а не видимость. Вычесть одно из
другого — значит получить в остатке ровно то, чего роли не видно: бухгалтеру
показывали чужие фамилии, суммы и названия регистров и при этом утверждали, что
закрытый месяц разошёлся, хотя не менялось ничего. Поэтому `visible_ledgers`
здесь обязательный аргумент, а не удобство: у вопроса «а что месяц дал бы
сегодня» нет ответа вообще, пока не сказано, кто спрашивает.

Фильтр стоит **в этом модуле, а не только в политиках базы**. Политики срежут
хранимое и без него, но свежий расчёт мимо них, а тот же код зовут и владельцем
схемы (тесты, обслуживание) — там политики не действуют вовсе. Одно место, где
срез задаётся явно, честнее двух половин гарантии.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from uuid import UUID

from django.db import transaction
from django.utils.translation import gettext as _
from django.utils.translation import gettext_noop

from core.models import PayComponent, Payrun, RetroAdjustment, Tenant

from .errors import PayrunRefused

__all__ = [
    "DELTA", "RECALCULATE", "Drift", "Line", "adjustments_for", "drift",
    "mode", "next_open_period", "pending", "post",
]

DELTA = "delta"
RECALCULATE = "recalculate"

MODE_TITLES = {
    DELTA: gettext_noop("разница переносится в текущий период"),
    RECALCULATE: gettext_noop("период открывается заново и пересчитывается"),
}

WRONG_MODE_REFUSAL = gettext_noop(
    "У этого партнёра правки задним числом ведутся пересчётом, а не переносом: "
    "закрытый период открывается заново с указанием причины и считается снова. "
    "Перенос разницы для него выключен настройкой."
)

# «В доступном вам срезе» — не оговорка ради осторожности, а точность: роль
# видит часть ведомости, и утверждать за неё, что не разошлось **ничего**,
# продукт не вправе (T085). Про существование остального при этом не сказано
# ни слова — срез роли она знает и так.
NOTHING_REFUSAL = gettext_noop(
    "Расхождений с сегодняшними данными в доступном вам срезе нет: переносить "
    "нечего. Возможно, разницу уже перенесли — посмотрите период-получатель."
)

NOT_APPROVED_REFUSAL = gettext_noop(
    "Перенос разницы делается только из закрытого месяца. "
    "Этот период ещё не утверждён — правьте данные и пересчитывайте его как обычно."
)


def month_after(period: date) -> date:
    """Следующий месяц. Первым числом, как все периоды в этом продукте."""
    if period.month == 12:
        return period.replace(year=period.year + 1, month=1, day=1)
    return period.replace(month=period.month + 1, day=1)


def month_title(period: date) -> str:
    """Месяц словами — тем же способом, что и везде в продукте (T017).

    Своего списка месяцев здесь больше нет. Он был третьим по счёту в
    репозитории, а с тремя языками стал бы девятым: девять списков, которые
    никто не удержит в согласии. Импорт из `web` — как у `jobs.py`, который уже
    так берёт контекст пользователя и права.
    """
    from web.i18n import month_title as titled

    return titled(period)


def mode(tenant_id: UUID) -> str:
    """Как партнёр ведёт правки задним числом. Настройка тенанта, не флаг сборки.

    Партнёра не видно — считаем режимом по умолчанию: решать за невидимого
    партнёра, что он ведёт учёт пересчётом, было бы догадкой.
    """
    value = (
        Tenant.objects.filter(pk=tenant_id).values_list("retro_mode", flat=True).first()
    )
    return value or DELTA


# --- строка разницы -----------------------------------------------------------


@dataclass(frozen=True)
class Line:
    """Одна строка разницы: чья, по какому компоненту, в каком регистре, сколько."""

    employee_id: UUID
    employee: str
    unit_id: UUID | None
    source_period: date
    code: str
    title: str
    amount: Decimal
    ledger: str
    channel: str = "bank"
    taxable: bool = True


@dataclass(frozen=True)
class Drift:
    """Расхождение закрытого месяца с сегодняшними данными.

    `error` — не пустая строка, если посчитать месяц заново не удалось (данные
    ушли так, что расчёт вообще не собирается). Это не то же самое, что «нет
    расхождений», и на экране должно читаться по-разному: молчание о неудачной
    сверке — то самое молчаливое умолчание, от которого продукт отказался.
    """

    lines: list[Line] = field(default_factory=list)
    error: str = ""

    def __bool__(self) -> bool:
        return bool(self.lines)

    @property
    def total(self) -> Decimal:
        return sum((line.amount for line in self.lines), Decimal(0))

    @property
    def employees(self) -> int:
        return len({line.employee_id for line in self.lines})

    @property
    def ledgers(self) -> list[str]:
        return sorted({line.ledger for line in self.lines})


def _stored(tenant_id: UUID, period: date, seen: set[str]) -> dict:
    """Что закрытый месяц хранит **своего**, без перенесённого в него извне.

    Разницы, приехавшие в этот месяц из ещё более ранних (`retro_source_period`
    заполнен), из сравнения исключаются: расчёт месяца их не воспроизводит и
    воспроизводить не должен — иначе они уезжали бы дальше по цепочке ещё раз.
    """
    rows: dict = {}
    for item in PayComponent.objects.filter(
        tenant_id=tenant_id,
        payslip__payrun__period=period,
        retro_source_period__isnull=True,
        ledger__in=sorted(seen),
    ).select_related("payslip__employee"):
        key = (item.payslip.employee_id, item.code, item.ledger)
        body = rows.setdefault(key, {
            "amount": Decimal(0),
            "employee": _name(item.payslip.employee),
            "unit_id": item.payslip.unit_id,
            "title": item.title,
            "channel": item.channel,
            "taxable": item.taxable,
        })
        body["amount"] += item.amount
    return rows


def _posted(tenant_id: UUID, period: date, seen: set[str]) -> dict:
    """Что из этого месяца уже перенесено вперёд и не отменено."""
    rows: dict = {}
    for item in RetroAdjustment.objects.filter(
        tenant_id=tenant_id, source_period=period, cancelled_at__isnull=True,
        ledger__in=sorted(seen),
    ):
        key = (item.employee_id, item.code, item.ledger)
        rows[key] = rows.get(key, Decimal(0)) + item.amount
    return rows


def _fresh(tenant_id: UUID, period: date, seen: set[str]) -> tuple[dict, str]:
    """Что месяц дал бы сегодня **в этом срезе**. Ничего не записывает.

    Движок считает месяц целиком: видимость регистров — не его дело, он считает
    деньги. Срез накладывается здесь, иначе сравнивать было бы нечего с чем
    (см. заголовок модуля).
    """
    # Импорт внутри: `calc` зовёт этот модуль, и на уровне файла вышел бы круг.
    from .calc import compute, money

    try:
        _, slips = compute(tenant_id, period)
    except PayrunRefused as refusal:
        return {}, refusal.message

    rows: dict = {}
    for case, slip in slips:
        for component in slip.components:
            if component.ledger not in seen:
                continue
            key = (case.employee_id, component.code, component.ledger)
            body = rows.setdefault(key, {
                "amount": Decimal(0),
                "employee": case.employee.name,
                "unit_id": case.unit_id,
                "title": component.title,
                "channel": component.channel,
                "taxable": component.taxable,
            })
            body["amount"] += money(component.amount)
    return rows, ""


def _name(employee) -> str:
    return f"{employee.last_name} {employee.first_name}".strip()


def _frozen_employees(tenant_id: UUID, period: date) -> set:
    """Люди, чьи числа держат по спору (T027): их разницу переносить нельзя.

    Заморозка означает «эти числа не меняются», и перенос разницы был бы тем же
    изменением, только зашедшим с другой стороны.
    """
    from core.models import PayslipFreeze

    return set(
        PayslipFreeze.objects.filter(
            tenant_id=tenant_id,
            payslip__payrun__period=period,
            released_at__isnull=True,
        ).values_list("payslip__employee_id", flat=True)
    )


def drift(tenant_id: UUID, period: date, *, visible_ledgers) -> Drift:
    """Чем сегодняшние данные расходятся с тем, что хранит закрытый месяц.

    Ответ даётся **в срезе спрашивающего**: `visible_ledgers` обязателен, и
    пустой список — это пустой ответ, а не «показать всё» (T085).

    Ничего не записывает. Это принципиально: вопрос задают **утверждённому**
    периоду, а он обязан остаться прежним до копейки.
    """
    seen = set(visible_ledgers or [])
    if not seen:
        return Drift()

    fresh, error = _fresh(tenant_id, period, seen)
    if error:
        return Drift(error=error)

    stored = _stored(tenant_id, period, seen)
    posted = _posted(tenant_id, period, seen)
    frozen = _frozen_employees(tenant_id, period)

    lines: list[Line] = []
    keys = sorted(set(fresh) | set(stored) | set(posted), key=lambda k: (str(k[0]), k[1], k[2]))
    for key in keys:
        employee_id, code, ledger = key
        if employee_id in frozen:
            continue
        body = fresh.get(key) or stored.get(key)
        amount = (
            fresh.get(key, {}).get("amount", Decimal(0))
            - stored.get(key, {}).get("amount", Decimal(0))
            - posted.get(key, Decimal(0))
        )
        if amount == 0 or body is None:
            continue
        lines.append(Line(
            employee_id=employee_id,
            employee=body["employee"],
            unit_id=body["unit_id"],
            source_period=period,
            code=code,
            title=body["title"],
            amount=amount,
            ledger=ledger,
            channel=body["channel"],
            taxable=body["taxable"],
        ))
    return Drift(lines=lines)


# --- куда переносить ----------------------------------------------------------


def next_open_period(tenant_id: UUID, source: date) -> date:
    """Первый после источника месяц, чей расчёт не утверждён.

    Месяц без расчёта считается неутверждённым — он и есть самый обычный
    получатель. Утверждённый принять разницу не может и не должен: ведомость по
    нему уже на руках, а база запись в него отвергнет (T023).

    Правило детерминированное, и месяц называется на экране словами: молча
    выбранный получатель — это сумма, появившаяся неизвестно где.
    """
    approved = set(
        Payrun.objects.filter(
            tenant_id=tenant_id, period__gt=source, status="approved"
        ).values_list("period", flat=True)
    )
    target = month_after(source)
    while target in approved:
        target = month_after(target)
    return target


# --- перенос ------------------------------------------------------------------


def post(*, tenant_id: UUID, source: date, actor_id, visible_ledgers) -> tuple[date, Drift]:
    """Перенести разницу закрытого месяца в первый неутверждённый.

    Возвращает месяц-получатель и то, что перенесено. Ничего не записывает в
    источник — по построению: перенос это `insert` в свою таблицу, и ни одного
    оператора по данным закрытого месяца здесь нет.

    **Переносится ровно свой срез** (T085). Прежде здесь стоял отказ «разница
    попадает в регистры учёта, недоступные вашей роли» с их перечислением — то
    есть сам запрет был сообщением о существовании чужого регистра и о том, что
    у этого партнёра в нём есть данные (D023). Теперь роль уносит видимое ей, а
    невидимое остаётся тому, кому оно видно: политика `retro_adjustments`
    отвергла бы чужую строку и без объяснения, но объяснять больше нечего —
    строки такой не возникает.
    """
    payrun = Payrun.objects.filter(tenant_id=tenant_id, period=source).first()
    if payrun is None or payrun.status != "approved":
        raise PayrunRefused(_(NOT_APPROVED_REFUSAL))

    if mode(tenant_id) != DELTA:
        raise PayrunRefused(_(WRONG_MODE_REFUSAL))

    found = drift(tenant_id, source, visible_ledgers=visible_ledgers)
    if found.error:
        raise PayrunRefused(found.error)
    if not found:
        raise PayrunRefused(_(NOTHING_REFUSAL))

    target = next_open_period(tenant_id, source)
    with transaction.atomic():
        RetroAdjustment.objects.bulk_create([
            RetroAdjustment(
                tenant_id=tenant_id, source_period=source, target_period=target,
                employee_id=line.employee_id, unit_id=line.unit_id,
                code=line.code, title=line.title, amount=line.amount,
                ledger=line.ledger, channel=line.channel, taxable=line.taxable,
                created_by=actor_id,
            )
            for line in found.lines
        ])
    return target, found


# --- что ждёт периода-получателя -----------------------------------------------


def adjustments_for(tenant_id: UUID, period: date) -> list[RetroAdjustment]:
    """Живые переносы, которые этот месяц обязан взять в свою ведомость."""
    return list(
        RetroAdjustment.objects.filter(
            tenant_id=tenant_id, target_period=period, cancelled_at__isnull=True
        ).order_by("source_period", "employee_id", "code")
    )


@dataclass(frozen=True)
class Pending:
    """Перенос, который в ведомости ещё не отражён — или отражён уже зря."""

    source_period: date
    live: Decimal
    shown: Decimal

    @property
    def title(self) -> str:
        return month_title(self.source_period)

    @property
    def cancelled(self) -> bool:
        """Показанное есть, а живого нет — источник пересчитали, перенос отменён."""
        return self.live == 0 and self.shown != 0


def pending(tenant_id: UUID, period: date) -> list[Pending]:
    """Расхождение между переносами и тем, что от них видно в ведомости.

    Одна проверка на оба направления, и это не экономия, а точность:

    - перенесли, но не пересчитали получателя — суммы в ведомости ещё нет;
    - источник пересчитали (перенос отменён триггером), но получателя опять не
      пересчитали — сумма в ведомости уже лишняя.

    Оба случая читаются одинаково — «пересчитайте период», — и оба обязаны быть
    сказаны вслух: ведомость, молча расходящаяся с данными, хуже лишней плашки.
    """
    live: dict = {}
    for item in adjustments_for(tenant_id, period):
        live[item.source_period] = live.get(item.source_period, Decimal(0)) + item.amount

    shown: dict = {}
    for source, amount in (
        PayComponent.objects.filter(
            tenant_id=tenant_id,
            payslip__payrun__period=period,
            retro_source_period__isnull=False,
        ).values_list("retro_source_period", "amount")
    ):
        shown[source] = shown.get(source, Decimal(0)) + amount

    return [
        Pending(source_period=source, live=live.get(source, Decimal(0)),
                shown=shown.get(source, Decimal(0)))
        for source in sorted(set(live) | set(shown))
        if live.get(source, Decimal(0)) != shown.get(source, Decimal(0))
    ]


def carried_in(tenant_id: UUID, period: date) -> list[date]:
    """Из каких закрытых месяцев в этой ведомости лежит разница."""
    return sorted({
        source
        for source in PayComponent.objects.filter(
            tenant_id=tenant_id,
            payslip__payrun__period=period,
            retro_source_period__isnull=False,
        ).values_list("retro_source_period", flat=True)
    })


def locked_out(tenant_id: UUID, period: date) -> bool:
    """Лежит ли разница за этот месяц в уже утверждённом периоде.

    Спрашивается у базы той же функцией, на которой стоит сам запрет
    (`retro_is_locked`), а не считается вторым выражением рядом: второе
    выражение об одном и том же однажды разойдётся с первым, и приложение начнёт
    обещать откат, который база отвергнет.
    """
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute("select retro_is_locked(%s, %s)", [str(tenant_id), period])
        return bool(cursor.fetchone()[0])


LOCKED_REFUSAL = gettext_noop(
    "Разница за этот месяц уже перенесена в утверждённый период и выплачена. "
    "Открыть его заново нельзя: пересчёт означал бы заплатить дважды."
)


def refuse_if_locked(tenant_id: UUID, period: date) -> None:
    """Отказать словами, если откат месяца привёл бы к двойному счёту.

    База отвергнет откат и без этого — сторожем `payrun_retro_lock`, то есть на
    любом пути записи. Здесь только объяснение: у ошибки триггера нет ни слова о
    том, что делать дальше.
    """
    if locked_out(tenant_id, period):
        raise PayrunRefused(_(LOCKED_REFUSAL))
