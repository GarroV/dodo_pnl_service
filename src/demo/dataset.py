"""
Кто работает в демо и что у них в табеле. Чистые данные, без Django и без базы.

Почему набор написан здесь, а не взят из обезличенной фикстуры, которой
пользуется сид разработки. Фикстура собрана обезличиванием настоящей таблицы
партнёра: имена в ней сербские, листы сербские, месяц один. Демо по правилу
владельца **всегда англоязычное** и обязано показывать три месяца сразу, два из
них закрытых. То есть от фикстуры пришлось бы взять только формат — а формат
дешевле описать явно, чем выводить из чужого файла (D028: ни одной настоящей
строки партнёра в демо не попадает физически, потому что источник другой).

Почему без Faker. Генератор имён дал бы новых людей при обновлении библиотеки, а
демо обязано пересобираться в **то же самое** после каждого сброса: ссылка на
человека, показанная заказчику, не должна протухнуть к следующему утру. Тридцать
имён списком — это тридцать строк, дешевле зависимости и надёжнее её.

Что набор обязан показывать (Definition of Done блока):

* два юрлица и три точки — чтобы было видно, что продукт не про одну пиццерию;
* все четыре схемы расчёта (`standard`, `half_time`, `half_time_min_base`,
  `temporary`) плюс прямая выплата курьерам;
* все три регистра учёта — иначе не показать главное свойство продукта: что
  бухгалтер видит не то же, что директор;
* два закрытых месяца и один открытый — отчёт расхождений сравнивает месяц с
  предыдущим, и на одном месяце ему нечего показать.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

__all__ = [
    "MONTHS",
    "PEOPLE",
    "UNITS",
    "Month",
    "Person",
    "employment_versions",
    "insured_types",
    "months",
    "timesheet_for",
]

D = Decimal


@dataclass(frozen=True)
class Month:
    """Месяц демо-стенда и его состояние."""

    period: date
    norm_hours: Decimal
    working_days: int
    # Закрытый месяц = расчёт утверждён. Открытый — посчитан, но не утверждён:
    # посетителю должно быть что утвердить и что пересчитать.
    approved: bool


# Три месяца, и ровно в таком составе. Два закрытых нужны не для красоты: отчёт
# расхождений сравнивает период с предыдущим, поэтому на одном месяце он пуст, а
# на двух — показывает только один переход. Три дают и закрытую пару (июнь→июль),
# и живую (июль→август), которую посетитель может пересчитать сам.
MONTHS = [
    Month(date(2026, 6, 1), D(176), 22, approved=True),
    Month(date(2026, 7, 1), D(176), 22, approved=True),
    Month(date(2026, 8, 1), D(168), 21, approved=False),
]


def months() -> list[Month]:
    return list(MONTHS)


# Точка → юрлицо. Два юрлица, потому что у партнёра так и есть: бухгалтерия
# работает с юрлицом, а расходы разносятся на пиццерию.
UNITS = [
    # код, название, юрлицо
    ("BG1", "Belgrade Central", "Dodo Belgrade d.o.o."),
    ("NS1", "Novi Sad Bulevar", "Dodo Novi Sad d.o.o."),
    ("NS2", "Novi Sad Dunavska", "Dodo Novi Sad d.o.o."),
]

LEGAL_ENTITIES = [
    ("Dodo Belgrade d.o.o.", "100 111 222"),
    ("Dodo Novi Sad d.o.o.", "100 333 444"),
]


@dataclass(frozen=True)
class Person:
    """Выдуманный сотрудник демо-стенда.

    `key` — внешний идентификатор, и он же ключ сверки с таблицей бухгалтера:
    сверка сопоставляет строки по «Имя Фамилия» из файла (см.
    `payroll.importers.plata_xlsx`). Держать здесь что-то другое означало бы,
    что демо-сверка не сходится ни с чем.
    """

    first: str
    last: str
    group: str
    unit: str
    base_rate: Decimal
    coefficient: Decimal
    # Пусто — схема группы. Заполняется там, где человек считается иначе, чем
    # его группа: в таблице партнёра так бывает внутри кухни.
    scheme: str | None = None
    # Месяц, с которого человек в штате. Нужен ровно один такой, чтобы в отчёте
    # расхождений было видно появившегося сотрудника, а не только изменившегося.
    hired: date = date(2025, 1, 1)
    # Повышение ставки с указанного месяца: условия найма версионированы по
    # датам, и демо обязано это показывать (иначе версионирование — слово в
    # документации).
    raise_from: date | None = None
    raised_rate: Decimal | None = None

    @property
    def key(self) -> str:
        return f"{self.first} {self.last}"


# Тридцать человек. Раскладка не случайная: она подобрана так, чтобы каждая
# схема расчёта и каждый регистр учёта были представлены не одной строкой (одна
# строка не показывает ни итогов, ни распределения), и чтобы у управляющего
# точки NS1 было что смотреть — его роль видит только свою точку.
PEOPLE: list[Person] = [
    # --- офис: официальный регистр, полный расчёт -----------------------------
    Person("Olivia", "Bennett", "office", "BG1", D("980.00"), D("1.60")),
    Person("Daniel", "Foster", "office", "BG1", D("870.00"), D("1.40")),
    Person("Sophie", "Marsh", "office", "NS1", D("790.00"), D("1.30")),
    # --- управляющие точек: официальный регистр -------------------------------
    Person("Nathan", "Cole", "management", "BG1", D("720.00"), D("1.35")),
    Person("Emma", "Wilder", "management", "NS1", D("720.00"), D("1.35")),
    Person("Lucas", "Hart", "management", "NS2", D("710.00"), D("1.30")),
    # --- кухня и касса: дополнительный регистр, полный расчёт -----------------
    Person("Mia", "Sullivan", "kitchen", "BG1", D("520.00"), D("1.10")),
    Person("Ethan", "Brooks", "kitchen", "BG1", D("510.00"), D("1.05")),
    Person("Chloe", "Ramsey", "kitchen", "BG1", D("495.00"), D("1.00")),
    Person("Owen", "Fletcher", "kitchen", "NS1", D("530.00"), D("1.10")),
    Person("Ava", "Norton", "kitchen", "NS1", D("505.00"), D("1.05")),
    Person("Leo", "Sanders", "kitchen", "NS1", D("480.00"), D("1.00")),
    Person("Grace", "Holloway", "kitchen", "NS2", D("515.00"), D("1.05")),
    Person("Felix", "Barnes", "kitchen", "NS2", D("490.00"), D("1.00")),
    Person(
        # Повышение ставки с августа: показывает версионирование условий найма.
        "Nora", "Whitfield", "kitchen", "NS2", D("470.00"), D("1.00"),
        raise_from=date(2026, 8, 1), raised_rate=D("560.00"),
    ),
    # --- кухня на полставке: схема half_time ----------------------------------
    Person("Jonah", "Pierce", "kitchen", "BG1", D("470.00"), D("1.00"), scheme="half_time"),
    Person("Ruby", "Callahan", "kitchen", "NS1", D("465.00"), D("1.00"), scheme="half_time"),
    Person("Theo", "Grant", "kitchen", "NS2", D("460.00"), D("1.00"), scheme="half_time"),
    # --- полставки со взносами от минимальной базы: схема half_time_min_base --
    Person("Isla", "Mercer", "kitchen", "BG1", D("455.00"), D("1.00"),
           scheme="half_time_min_base"),
    Person("Caleb", "Rowe", "kitchen", "NS1", D("450.00"), D("1.00"),
           scheme="half_time_min_base"),
    Person("Zoe", "Ashby", "kitchen", "NS2", D("450.00"), D("1.00"),
           scheme="half_time_min_base"),
    # --- тестомейкеры: дополнительный регистр ---------------------------------
    Person("Adam", "Quinn", "doughmaker", "BG1", D("540.00"), D("1.15")),
    Person("Lily", "Osborne", "doughmaker", "NS1", D("535.00"), D("1.15")),
    # --- временные работы: официальный регистр, схема temporary ---------------
    Person("Victor", "Lane", "temporary", "BG1", D("420.00"), D("1.00")),
    Person("Hannah", "Reed", "temporary", "NS1", D("420.00"), D("1.00")),
    Person(
        # Принят в июле: в отчёте расхождений июль↔июнь видно появившегося
        # человека, а не только изменившегося.
        "Marcus", "Doyle", "temporary", "NS2", D("415.00"), D("1.00"),
        hired=date(2026, 7, 1),
    ),
    # --- курьеры: внутренний регистр, прямая выплата ---------------------------
    Person("Jasper", "Nolan", "couriers", "BG1", D("430.00"), D("1.00")),
    Person("Elena", "Vance", "couriers", "BG1", D("430.00"), D("1.00")),
    Person("Milo", "Frost", "couriers", "NS1", D("425.00"), D("1.00")),
    Person("Iris", "Calloway", "couriers", "NS2", D("425.00"), D("1.00")),
]

# Схемы, у которых рабочее время — половина нормы. Список, а не проверка по
# имени в коде расчёта: схема здесь только выбирает, сколько часов ставить в
# табель, а считает по ней движок по правилам страны.
HALF_TIME_SCHEMES = {"half_time", "half_time_min_base"}

# Виды часов, которые входят в базу для взносов. Тот же состав, что в пресете
# страны (`hour_types.*.insured`); расхождение здесь означало бы отказ расчёта
# «база взносов не сходится с часами» на ровном месте.
INSURED = ("regular", "holiday", "vacation", "sick")


def insured_types() -> tuple[str, ...]:
    return INSURED


@dataclass(frozen=True)
class Event:
    """Что случилось у человека в конкретном месяце.

    События — не украшение набора, а то единственное, ради чего в демо есть
    отчёт расхождений: без них все месяцы одинаковы, отчёт честно пуст, и
    посетитель видит пустой экран вместо продукта.
    """

    vacation: Decimal = Decimal(0)
    sick: Decimal = Decimal(0)
    holiday: Decimal = Decimal(0)
    cash_payout: Decimal = Decimal(0)
    deduction: Decimal = Decimal(0)
    manual_correction: Decimal | None = None
    correction_reason: str = ""


# Что происходит по месяцам. Ключ — «Имя Фамилия» и месяц.
EVENTS: dict[tuple[str, str], Event] = {
    # Июнь — спокойный месяц: он служит точкой отсчёта для июля.
    ("Mia Sullivan", "2026-06"): Event(vacation=D(40)),
    ("Jasper Nolan", "2026-06"): Event(cash_payout=D("14000.00")),
    ("Elena Vance", "2026-06"): Event(cash_payout=D("11500.00")),
    ("Milo Frost", "2026-06"): Event(cash_payout=D("9800.00")),
    ("Iris Calloway", "2026-06"): Event(cash_payout=D("10200.00")),

    # Июль: отпуск ушёл у одного и появился у другого, добавился больничный.
    ("Owen Fletcher", "2026-07"): Event(vacation=D(80)),
    ("Felix Barnes", "2026-07"): Event(sick=D(56)),
    ("Jasper Nolan", "2026-07"): Event(cash_payout=D("15200.00")),
    ("Elena Vance", "2026-07"): Event(cash_payout=D("12100.00")),
    ("Milo Frost", "2026-07"): Event(cash_payout=D("13400.00")),
    ("Iris Calloway", "2026-07"): Event(cash_payout=D("9900.00")),

    # Август — открытый месяц: здесь и правка руками со следом, и удержание.
    ("Grace Holloway", "2026-08"): Event(vacation=D(64)),
    ("Leo Sanders", "2026-08"): Event(sick=D(40)),
    ("Ava Norton", "2026-08"): Event(holiday=D(8)),
    (
        "Ruby Callahan", "2026-08"
    ): Event(
        manual_correction=D("1800.00"),
        correction_reason="top-up to the statutory minimum, accountant's note of 31.08",
    ),
    ("Ethan Brooks", "2026-08"): Event(deduction=D("2500.00")),
    ("Jasper Nolan", "2026-08"): Event(cash_payout=D("16100.00")),
    ("Elena Vance", "2026-08"): Event(cash_payout=D("12800.00")),
    ("Milo Frost", "2026-08"): Event(cash_payout=D("11700.00")),
    ("Iris Calloway", "2026-08"): Event(cash_payout=D("10500.00")),
}


@dataclass(frozen=True)
class SheetRow:
    """Табель одного человека за месяц — ровно то, что ложится в базу."""

    person: Person
    period: date
    hours: dict[str, Decimal]
    insured_hours: Decimal
    norm_hours: Decimal
    deduction: Decimal = Decimal(0)
    cash_payout: Decimal = Decimal(0)
    manual_correction: Decimal | None = None
    correction_reason: str = ""
    unit: str = ""
    events: Event = field(default_factory=Event)


def employed(person: Person, month: Month) -> bool:
    return person.hired <= month.period


def timesheet_for(person: Person, month: Month) -> SheetRow | None:
    """Табель человека за месяц. None — в этом месяце он ещё не работал.

    Отработанные часы считаются как «норма минус то, чего человек в этом месяце
    не работал». Иначе отпуск прибавлялся бы к полному месяцу, и у человека
    выходило бы больше нормы — правдоподобное неверное число, которое расчёт
    принял бы молча.
    """
    if not employed(person, month):
        return None

    scheme = person.scheme or ""
    norm = month.norm_hours / 2 if scheme in HALF_TIME_SCHEMES else month.norm_hours
    event = EVENTS.get((person.key, f"{month.period:%Y-%m}"), Event())

    away = event.vacation + event.sick + event.holiday
    regular = norm - away
    if regular < 0:
        # Данные демо пишем мы сами, поэтому это не «бывает»: это опечатка в
        # событиях. Молча урезать её значит показать стенд, который не совпадает
        # с описанным здесь замыслом.
        raise ValueError(
            f"{person.key} {month.period:%Y-%m}: событий больше, чем норма часов"
        )

    hours = {
        "regular": regular,
        "holiday": event.holiday,
        "vacation": event.vacation,
        "sick": event.sick,
    }
    return SheetRow(
        person=person,
        period=month.period,
        hours=hours,
        insured_hours=sum((hours[k] for k in INSURED), Decimal(0)),
        norm_hours=norm,
        deduction=event.deduction,
        cash_payout=event.cash_payout,
        manual_correction=event.manual_correction,
        correction_reason=event.correction_reason,
        unit=person.unit,
        events=event,
    )


def employment_versions(person: Person) -> list[tuple[date, date | None, Decimal]]:
    """Версии условий найма: (действуют с, действуют по, ставка).

    Одна версия у большинства и две у того, кому подняли ставку. Границы
    полуоткрытые `[from, to)` — так же читает расчёт (см. `core.rules`).
    """
    if person.raise_from is None or person.raised_rate is None:
        return [(person.hired, None, person.base_rate)]
    return [
        (person.hired, person.raise_from, person.base_rate),
        (person.raise_from, None, person.raised_rate),
    ]


def rate_at(person: Person, period: date) -> Decimal:
    """Ставка, действующая в этом месяце."""
    for valid_from, valid_to, rate in employment_versions(person):
        if valid_from <= period and (valid_to is None or period < valid_to):
            return rate
    return person.base_rate
