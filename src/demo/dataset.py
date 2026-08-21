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
    "COUNTERPARTIES",
    "EXPENSE_ITEMS",
    "EXPENSES",
    "INVOICES",
    "MONTHS",
    "PAPERS",
    "PAYMENTS",
    "PEOPLE",
    "UNITS",
    "Expense",
    "ExpenseItem",
    "Invoice",
    "Month",
    "Paper",
    "Payment",
    "Person",
    "counterparties",
    "employment_versions",
    "expenses",
    "insured_types",
    "invoices",
    "months",
    "papers",
    "payments",
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


# --- расходы из кассы (T113) ---------------------------------------------------
#
# Зачем они в демо. Продукт умеет собирать не только зарплату: расход из кассы —
# вторая половина того, из чего складывается P&L, и без неё в отчёте дыра ровно
# там, где деньги тратятся мимо банка. Демо с одной зарплатой показывает
# половину продукта и молчит об этом.
#
# Набор написан явно, как и люди выше, и по той же причине: демо обязано
# пересобираться в **то же самое** после каждого сброса. Ни одного случайного
# числа, ни одного генератора.


@dataclass(frozen=True)
class ExpenseItem:
    """Статья расходов демо: то, чем человек называет трату.

    `spread` — метод разнесения расхода, внесённого без точки. `None` значит,
    что правила нет вовсе: такой расход остаётся нераспределённым и виден в
    списке «что мешает закрыть месяц». Это состояние в демо обязано быть — оно
    и есть ответ продукта на «сумма есть, точка ещё не решена».
    """

    code: str
    title: str
    pnl_code: str
    spread: str | None = None


EXPENSE_ITEMS = [
    ExpenseItem("water", "Water", "utilities"),
    ExpenseItem("electricity", "Electricity", "utilities"),
    ExpenseItem("waste", "Waste removal", "utilities"),
    # Аренда офиса приходит на юрлицо целиком и разносится поровну: показать
    # разнесение можно только на статье, у которой правило есть.
    ExpenseItem("office_rent", "Office rent", "rent", spread="even"),
    ExpenseItem("courier_fuel", "Courier fuel", "other_opex"),
    ExpenseItem("repairs", "Small repairs", "other_opex"),
    # А у этой правила нет намеренно — см. `spread` выше.
    ExpenseItem("marketing", "Marketing campaign", "other_opex"),
    # Накладная на сырьё — самая частая трата поставщику (T151/T153), а статьи
    # под неё до этой задачи в демо не было вовсе: счёт от Metro лёг бы на
    # статью, которой продукт не показывает.
    ExpenseItem("food_supplies", "Food supplies", "food_cost"),
]


@dataclass(frozen=True)
class Till:
    """Касса точки: коробка, из которой платят наличными (T145, D039).

    В демо их четыре, и это не полнота ради полноты: у NS1 их **две** — обычная
    и внутренняя, — потому что весь смысл кассы в том, что регистр учёта расхода
    следует из неё. С одной кассой на точку это выглядело бы как лишнее поле.
    """

    code: str
    unit: str
    ledger: str
    title: str


TILLS = [
    Till("BG1-main", "BG1", "official", "BG1 main till"),
    Till("NS1-main", "NS1", "official", "NS1 main till"),
    # Вторая касса той же точки, другого регистра: расход из неё уходит во
    # внутренний регистр сам, без выбора руками (D039).
    Till("NS1-side", "NS1", "internal", "NS1 side till"),
    Till("NS2-main", "NS2", "official", "NS2 main till"),
]


@dataclass(frozen=True)
class Expense:
    """Одна трата: когда, где, за что, из какого регистра и с чьих слов.

    `unit = None` — расход на всю сеть: точки у него нет, и дальше его судьбу
    решает правило статьи. Именно так вносят аренду и рекламу.

    `till` — из какой кассы платили (T145). Пусто — трата мимо кассы: так
    внесены расходы, у которых источник денег не наличные, и так же выглядят все
    строки, заведённые до появления справочника касс.

    `vat` — ставка НДС в процентах (T146, D042). Пусто — налог не выделен, и в
    P&L такая трата идёт полной суммой. Заполнено — в P&L по умолчанию едет
    сумма без налога, а полная достаётся отдельной кнопкой.
    """

    on: date
    unit: str | None
    item: str
    amount: Decimal
    ledger: str = "official"
    note: str = ""
    till: str | None = None
    vat: str | None = None


EXPENSES = [
    # Июнь — закрытый месяц. Полный набор: по нему видно, что выгрузка «Строки
    # для P&L» содержит обе части — зарплату и траты.
    Expense(date(2026, 6, 5), "BG1", "water", D("6200.00"), note="June water bill",
            till="BG1-main", vat="20"),
    Expense(date(2026, 6, 5), "NS1", "water", D("4800.00"), note="June water bill",
            till="NS1-main", vat="20"),
    Expense(date(2026, 6, 5), "NS2", "water", D("5100.00"), note="June water bill"),
    Expense(date(2026, 6, 9), "BG1", "electricity", D("31400.00"), note="June power bill",
            till="BG1-main", vat="20"),
    Expense(date(2026, 6, 9), "NS1", "electricity", D("22750.00"), note="June power bill"),
    Expense(date(2026, 6, 9), "NS2", "electricity", D("19900.00"), note="June power bill"),
    Expense(date(2026, 6, 12), None, "office_rent", D("90000.00"), note="Head office, June"),
    # Регистр supplementary без своей кассы: у NS1 есть внутренняя касса
    # (NS1-side), у BG1 — нет. Дать этой строке till означало бы дать ей
    # официальный регистр (D039), а не тот, что показывает эта строка, поэтому
    # правильный ход здесь — комментарий, который не обещает кассу, а не касса.
    Expense(date(2026, 6, 18), "BG1", "courier_fuel", D("12400.00"),
            ledger="supplementary", note="Courier fuel, paid in cash"),
    # Регистр, которого управляющему не видно: демо показывает не рассказ про
    # разграничение доступа, а само разграничение.
    Expense(date(2026, 6, 22), "NS1", "repairs", D("25400.00"),
            ledger="internal", note="Door handle, cash, no receipt",
            # Регистр здесь не выбран руками — он приехал из кассы (D039).
            till="NS1-side"),

    # Июль — тоже закрытый.
    Expense(date(2026, 7, 6), "BG1", "water", D("6350.00"), note="July water bill"),
    Expense(date(2026, 7, 6), "NS1", "water", D("4950.00"), note="July water bill",
            till="NS1-main", vat="10"),
    Expense(date(2026, 7, 10), "BG1", "electricity", D("33800.00"), note="July power bill"),
    Expense(date(2026, 7, 10), "NS2", "electricity", D("21300.00"), note="July power bill"),
    Expense(date(2026, 7, 12), None, "office_rent", D("90000.00"), note="Head office, July"),
    Expense(date(2026, 7, 20), "NS2", "waste", D("6000.00"), note="Waste removal, July"),

    # Август — открытый месяц: его посетитель видит первым делом, ничего не
    # переключая. Касса и НДС здесь обязаны быть видны сразу — до Н7 обе
    # колонки на этом месяце были пустыми (сверка 8).
    Expense(date(2026, 8, 4), "NS1", "water", D("5050.00"), note="August water bill",
            # Льготная ставка на воду, как у July — но без кассы: НДС и оплата
            # из кассы независимы друг от друга, и обе строки показывают это.
            vat="10"),
    Expense(date(2026, 8, 4), "NS2", "water", D("5300.00"), note="August water bill"),
    Expense(date(2026, 8, 7), "BG1", "electricity", D("29900.00"), note="August power bill",
            till="BG1-main", vat="20"),
    Expense(date(2026, 8, 11), None, "office_rent", D("90000.00"), note="Head office, August"),
    Expense(date(2026, 8, 14), "NS1", "courier_fuel", D("9800.00"),
            till="NS1-main", note="Fuel paid from the till"),
    # Нераспределённое: правила у статьи нет, точки у расхода нет. Сумма висит и
    # **видна** — за этим и заведена. Молча пропавшая сумма это дыра в P&L,
    # которая не кричит.
    Expense(date(2026, 8, 19), None, "marketing", D("150000.00"),
            note="Summer campaign, whole network"),
]


def expenses() -> list[Expense]:
    return list(EXPENSES)


# --- поставщики: контрагенты, счета, платежи (T151-T153) -----------------------
#
# Зачем они в демо. Без этого раздела демо не знает о четвёртой очереди ничего:
# ни одного контрагента, ни одного счёта, ни одной строки в инбоксе. Definition
# of Done блока говорит прямо: демо обязано показывать контрагентов,
# неоплаченный счёт и непустой инбокс — три состояния продукта, которые нельзя
# увидеть на пустом справочнике.
#
# Ключ Dodo IS у всех контрагентов пуст — намеренно. Поле заведено пустым и
# заранее (T150): сведение со справочником поставщиков Dodo IS случится только
# в шестой очереди, и заполненное поле сегодня соврало бы про то, что оно уже
# случилось.
COUNTERPARTIES = [
    "EPS Elektro",
    "City Water Utility",
    "Metro Cash & Carry",
    "Papirus Packaging",
]


def counterparties() -> list[str]:
    return list(COUNTERPARTIES)


@dataclass(frozen=True)
class Invoice:
    """Счёт поставщика: три даты и, может быть, строка без статьи (T151).

    `key` — внешний id документа и хвост ключа идемпотентности его строки: та
    же форма, которой продукт пишет счёт (`web.suppliers.record_invoice`), а не
    придуманная рядом вторая. `doc_date` и `period` разведены нарочно: счёт за
    июнь приходит в июле, и без разницы между ними в демо не видно главного
    свойства продукта.

    `item = None` — статья ещё не выбрана. Строка получает служебную статью
    «Не разобрано» и встаёт в инбокс классификации (T152) — состояние,
    Definition of Done требует показать явно, а не подразумевать.
    """

    key: str
    counterparty: str
    unit: str
    item: str | None
    amount: Decimal
    doc_date: date
    period: date
    vat: str | None = None
    number: str = ""
    note: str = ""


# Регистр у всех пяти — официальный: это формальные счета от поставщиков,
# оплаченные банком, а не наличные из кассы (у наличных — свой раздел выше).
INVOICES = [
    # №1 — главное свойство продукта целиком в одной строке: бумага пришла в
    # июле, а расход лёг в июнь, потому что счёт за июньское электричество.
    # Месяц при этом уже закрыт — ровно так, как приходят счета в жизни.
    Invoice(
        "demo-inv-1", "EPS Elektro", "BG1", "electricity", D("41200.00"),
        doc_date=date(2026, 7, 3), period=date(2026, 6, 1), vat="20",
        number="INV-1042", note="Electricity for June, invoice arrived in July",
    ),
    # №2 — частичная оплата: остаток не «оплачен/нет», а число (см. платежи
    # ниже). Заодно единственный счёт со статьёй `food_supplies` — самой частой
    # тратой поставщику, для которой в демо до этой задачи статьи не было.
    Invoice(
        "demo-inv-2", "Metro Cash & Carry", "NS1", "food_supplies", D("96000.00"),
        doc_date=date(2026, 7, 15), period=date(2026, 7, 1), vat="10",
        number="INV-2077", note="Food delivery, July",
    ),
    # №3 — обязательство на конец открытого месяца: платежа нет вовсе. Это не
    # недосмотр демо, а состояние, которое партнёр обязан видеть на экране
    # счетов каждый день.
    Invoice(
        "demo-inv-3", "City Water Utility", "NS2", "water", D("7400.00"),
        doc_date=date(2026, 8, 6), period=date(2026, 8, 1), vat="20",
        number="INV-3015",
    ),
    # №4 — без статьи. Точка у счёта есть (BG1), поэтому в «нераспределённых»
    # (`facts_unallocated`) он не встанет — он встаёт в инбокс классификации
    # (T152), и это два разных списка про два разных недостатка данных.
    Invoice(
        "demo-inv-4", "Papirus Packaging", "BG1", None, D("18600.00"),
        doc_date=date(2026, 8, 11), period=date(2026, 8, 1),
        number="INV-4021",
    ),
    Invoice(
        "demo-inv-5", "EPS Elektro", "NS1", "electricity", D("23900.00"),
        doc_date=date(2026, 8, 7), period=date(2026, 8, 1), vat="20",
        number="INV-5033",
    ),
]


def invoices() -> list[Invoice]:
    return list(INVOICES)


@dataclass(frozen=True)
class Payment:
    """Оплата счёта — своим событием и своей датой (T151).

    Период платежа считается от `on` (месяц денег), а не от периода счёта:
    ровно так же, как это делает `web.suppliers.pay`. Держать эту логику
    здесь второй раз было бы лишним — она в одну строку и живёт в `seed.py`,
    рядом с записью.
    """

    key: str
    invoice_key: str
    amount: Decimal
    on: date


PAYMENTS = [
    # Целиком, банком — то же электричество, что и дата документа/периода
    # развели: деньги ушли в июле, уже после того, как счёт лёг в закрытый июнь.
    Payment("demo-pay-1", "demo-inv-1", D("41200.00"), date(2026, 7, 8)),
    # Частичная: 96000.00 счёта, 50000.00 оплачено — остаток 46000.00 виден
    # числом, а не текстом «частично».
    Payment("demo-pay-2", "demo-inv-2", D("50000.00"), date(2026, 8, 5)),
    Payment("demo-pay-3", "demo-inv-5", D("23900.00"), date(2026, 8, 12)),
]


def payments() -> list[Payment]:
    return list(PAYMENTS)


@dataclass(frozen=True)
class Paper:
    """Бумага, принесённая управляющим с точки (T174, D047).

    Показывает состояние, которого в демо до этой задачи не было вовсе:
    **необработанное вложение**. Управляющий сфотографировал накладную и скинул
    её — поставщика, статью и период назначает потом бухгалтер, а пока не
    назначил, суммы в P&L нет. Не «не подтверждено» флагом, а нет: у бумаги ноль
    строк учёта, и отчёту нечего показать физически.

    `item is None` — бумага ждёт разбора: строка учёта не пишется вовсе, и
    бумага стоит в инбоксе. Заполненные `item` и `period` — бумага разобрана, и
    её сумма в P&L уже есть. В наборе есть и то, и другое: одного состояния
    хватило бы, чтобы показать экран, но не хватило бы, чтобы показать разницу.

    `file` — имя готового файла в `src/demo/fixtures`. Файл лежит в
    репозитории, а не рисуется в момент наполнения: демо обещает, что с бумаги
    читаются поставщик, дата и сумма, а нарисовать читаемый текст наполнению
    нечем (см. `tools/make_demo_scans.py`).
    """

    key: str
    kind: str                  # invoice — накладная, receipt — чек
    unit: str
    counterparty: str | None
    stated: Decimal | None
    doc_date: date
    file: str
    note: str
    item: str | None = None
    period: date | None = None


# Обе бумаги — с NS1, и это не случайность: в демо точку ведёт именно
# управляющий NS1, и посетитель, переключившийся на его роль, обязан увидеть
# свои бумаги, а не пустой список.
PAPERS = [
    # №1 — то, ради чего задача: накладная принесена, но не разобрана. Стоит в
    # инбоксе, суммы 21 550.00 в P&L нет. Файл — снимок, поэтому карточка
    # показывает саму бумагу картинкой.
    Paper(
        "demo-paper-1", "invoice", "NS1", "Metro Cash & Carry", D("21550.00"),
        doc_date=date(2026, 8, 14), file="delivery-note.png",
        note="Delivery note from the warehouse, brought in by the shift manager",
    ),
    # №2 — та же бумага после разбора: у неё появилась статья и период, и сумма
    # 8 250.00 теперь в P&L. Файл — PDF, то есть вторая ветка карточки: его
    # отдают на сохранение, а не рисуют в странице.
    Paper(
        "demo-paper-2", "receipt", "NS1", "Metro Cash & Carry", D("8250.00"),
        doc_date=date(2026, 8, 9), file="cash-receipt.pdf",
        note="Cash receipt, bought on the spot",
        item="food_supplies", period=date(2026, 8, 1),
    ),
]


def papers() -> list[Paper]:
    return list(PAPERS)
