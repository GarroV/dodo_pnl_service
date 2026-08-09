"""
Наполнение демо-базы: организация, люди, три месяца и посчитанные ведомости.

Это не второй `seed_dev`. Разница не в объёме, а в назначении: сид разработки
воспроизводит формат таблицы партнёра (восемь сербских листов, один месяц) и на
нём стоит вся приёмка стройки, а демо показывает **продукт** человеку, который
видит его впервые, — на английском, за три месяца, с закрытыми периодами и
живым отчётом расхождений. Свести их в одну команду значило бы, что каждая
правка ради показа двигает числа приёмки.

Порядок шагов важен и объяснён по месту. Главное, что нельзя переставить:
периоды считаются **по возрастанию месяцев**, а утверждаются сразу после своего
расчёта. Иначе перенос разницы задним числом и запрет на изменение утверждённого
расчёта начинают спорить друг с другом (та же ловушка, что описана в уборке
`seed_dev`, только с другой стороны).
"""
from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from django.conf import settings
from django.db import connection, transaction
from django.utils.timezone import now

from core import models
from core.rules import import_presets
from payrun.calc import calculate_period
from payrun.lifecycle import approve

from . import dataset
from .dataset import MONTHS, PEOPLE, UNITS, Month, Person
from .guard import require_demo_data

__all__ = ["ROLES", "TENANT_CODE", "det_id", "seed_demo"]

TENANT_CODE = "demo"
PRESET_CODE = "serbia-2026"
COUNTRY = "RS"

# Своё пространство имён для детерминированных id: демо и сид разработки не
# должны совпадать ссылками даже случайно — они живут в разных базах, и
# одинаковые id создали бы ложное впечатление, что это одни и те же данные.
NS = uuid.UUID("6f2b7f7e-0000-4000-8000-0000000000de")

ALL_LEDGERS = ["official", "supplementary", "internal"]

# Статьи P&L. По-английски, как и всё в демо: посетитель видит их в выгрузке.
PNL_ITEMS = [
    ("revenue", "Revenue", "revenue", 10),
    ("food_cost", "Food cost", "expense", 20),
    ("labour_cost", "Labour cost", "expense", 40),
    ("payroll_taxes", "Payroll taxes", "expense", 50),
    ("total", "Result", "subtotal", 90),
]

# Названия групп по-английски. Сами группы приезжают из пресета страны — оттуда
# же, откуда их берёт движок, — но заголовки в пресете русские, а демо всегда
# англоязычное (правило владельца). Переопределяется только подпись: схема
# расчёта и регистр учёта берутся из пресета как есть, иначе демо считало бы не
# по тем правилам, по которым считает продукт.
GROUP_TITLES = {
    "office": "Office",
    "management": "Unit managers",
    "kitchen": "Kitchen and counter",
    "doughmaker": "Dough maker",
    "couriers": "Couriers",
    "temporary": "Temporary contracts",
}

# Английские подписи правил страны. Кладутся в базу переопределениями на уровне
# партнёра — тем же механизмом, которым партнёр меняет любую другую величину
# (`rule_overrides`, путь + значение + дата начала действия).
#
# Почему именно так, а не правкой пресета или словарём в коде демо. Подпись
# компонента попадает в `pay_components.title` в момент расчёта: то, что видно в
# ведомости, следе и отчёте расхождений, — это слова, действовавшие тогда, когда
# месяц считали. Значит, английские подписи обязаны быть в **правилах**, и
# положить их надо до расчёта. Пресет страны при этом остаётся русским: он общий
# для всех партнёров Сербии, а англоязычен только демо-стенд.
#
# Границы у переопределения открыты слева: `valid_from` раньше первого месяца
# демо, `valid_to` пусто. Иначе закрытый месяц однажды объяснялся бы словами,
# которых в нём не было.
RULE_TITLES = {
    "hour_types.regular.title": "Worked hours",
    "hour_types.holiday.title": "Public holiday",
    "hour_types.vacation.title": "Annual leave",
    "hour_types.sick.title": "Sick leave",
    "hour_types.night.title": "Night hours",
    "hour_types.overtime.title": "Overtime",
    "allowances.meal_and_vacation_bonus.title": "Meal and vacation allowance",
    # Две подписи, которые до этого были зашиты в движке: он читает их из
    # правил с прежним русским умолчанием, чтобы демо могло их перевести (см.
    # payroll/engine.py).
    "minimum_guarantee.title": "Minimum wage top-up",
    "manual_correction.title": "Manual correction",
    "schemes.standard.title": "Full calculation",
    "schemes.half_time.title": "Half time",
    "schemes.half_time_min_base.title": "Half time, contributions from minimum base",
    "schemes.temporary.title": "Temporary contract",
    "schemes.direct.title": "Direct payout, no gross-up",
    "groups.office.title": "Office",
    "groups.management.title": "Unit managers",
    "groups.kitchen.title": "Kitchen and counter",
    "groups.doughmaker.title": "Dough maker",
    "groups.couriers.title": "Couriers",
    "groups.temporary.title": "Temporary contracts",
}

# С какой даты действуют английские подписи. Раньше самого раннего месяца демо:
# правило, начавшее действовать в середине стенда, показало бы часть месяцев
# по-русски.
TITLES_FROM = date(2020, 1, 1)

# Роли демо. Коды те же, что у продукта, — на них стоят политики базы и права;
# по-английски здесь только подписи. Набор регистров и точка повторяют роли
# сида разработки намеренно: демо должно показывать ту же разницу видимости,
# которую партнёр получит у себя, а не выдуманную.
ROLES = [
    (
        "director", "Operations director", ALL_LEDGERS, None,
        [
            "timesheet.edit", "payrun.calculate", "period.approve",
            "period.reopen", "payslip.freeze", "retro.post", "unit.close",
        ],
    ),
    (
        "accountant", "Accountant", ["official"], None,
        [
            "timesheet.edit", "payrun.calculate", "period.approve",
            "payslip.freeze", "retro.post",
        ],
    ),
    (
        "manager", "Unit manager (Novi Sad Bulevar)",
        ["official", "supplementary"], "NS1",
        ["timesheet.edit", "unit.close"],
    ),
    (
        "admin", "Network administrator", ["official"], None,
        ["directory.manage", "rules.manage", "roles.manage"],
    ),
]


def det_id(*parts: str) -> uuid.UUID:
    """Детерминированный id: каждый сброс демо даёт те же ссылки.

    Не мелочь: ссылку на конкретную ведомость показывают заказчику, и после
    ночного сброса она обязана открывать ту же страницу, а не 404.
    """
    return uuid.uuid5(NS, "/".join(parts))


def demo_password() -> str:
    """Пароль демо-учёток.

    Секретом не является и не притворяется: в демо-базе нет ничего, кроме
    выдуманных людей, а вход и так открыт одной кнопкой. Переменная нужна не для
    тайны, а чтобы на площадке пароль можно было сменить, не пересобирая образ.
    """
    return getattr(settings, "DEMO_USER_PASSWORD", None) or "demo-only-not-a-secret"


def seed_demo(*, log=None) -> dict:
    """Наполнить демо-базу целиком. Идемпотентна: тенант пересобирается.

    Предохранитель по данным вызывается **здесь**, а не только в команде: это
    единственная точка, через которую наполнение попадает в базу, и проверка
    обязана стоять на ней, а не на одном из путей к ней. Проверка адресов живёт
    в команде — там известно окружение.
    """
    say = log or (lambda *_: None)

    with transaction.atomic():
        # Раньше первой записи: в базе с чужим партнёром наполнение демо не
        # выполняется вовсе (см. demo.guard).
        require_demo_data(connection, TENANT_CODE)
        wipe()
        import_presets()
        tenant = _org()
        _english_titles(tenant)
        items = _pnl_items()
        groups = _groups(tenant, items)
        _roles_and_users(tenant)
        _calendar()
        people = _people(tenant, groups)
        say(f"организация и {people} сотрудников готовы")

    # Расчёт каждого месяца — своей транзакцией, а не общей: `calculate_period`
    # открывает свою и утверждает результат отдельным переходом, у которого
    # своя проверка цикла. Одна транзакция на всё склеила бы состояния, которых
    # в жизни не бывает.
    calculated = []
    for month in MONTHS:
        outcome = calculate_period(
            tenant_id=tenant.id, period=month.period, visible_ledgers=ALL_LEDGERS
        )
        say(f"{month.period:%Y-%m}: посчитано строк {outcome.slips}")
        if month.approved:
            _approve(tenant, month)
            say(f"{month.period:%Y-%m}: утверждён")
        calculated.append(month.period)

    return {
        "tenant": tenant,
        "people": people,
        "periods": calculated,
        "password": demo_password(),
    }


# --- шаги ---------------------------------------------------------------------


def wipe() -> None:
    """Снести данные демо-тенанта.

    Обычный путь сброса — пересоздание базы из эталона (D016), и уборка ему не
    нужна. Она нужна повторному `seed_demo` на уже наполненной базе: без неё
    команда падала бы на первом же уникальном ключе, и «пересобрать демо» стало
    бы «сначала удалить базу руками».

    Порядок повторяет уборку сида разработки, и по тем же причинам: утверждённый
    расчёт держит триггер (его нужно открыть с причиной), переносы разницы не
    дают открыть месяц-источник (поэтому от позднего месяца к раннему),
    замороженные строки не удаляются, пока заморозка не снята.
    """
    tenants = models.Tenant.objects.filter(code=TENANT_CODE)
    if not tenants.exists():
        return

    approved = models.Payrun.objects.filter(tenant__in=tenants, status="approved")
    if approved.exists():
        with connection.cursor() as cursor:
            for payrun_id in list(
                approved.order_by("-period").values_list("id", flat=True)
            ):
                cursor.execute(
                    "select set_config('app.transition_reason', %s, true)",
                    ["пересборка демо-стенда"],
                )
                models.Payrun.objects.filter(pk=payrun_id).update(status="reopened")

    models.RetroAdjustment.objects.filter(tenant__in=tenants).delete()
    models.PayslipFreeze.objects.filter(
        tenant__in=tenants, released_at__isnull=True
    ).update(released_at=now())

    for model in (
        models.PayComponent, models.Payslip, models.Payrun, models.Timesheet,
        models.EmploymentTerm, models.Employee, models.EmployeeGroup,
        models.Membership, models.Role, models.Period, models.AllocationRule,
        models.Counterparty, models.Unit, models.LegalEntity,
        # Английские подписи правил — тоже данные тенанта, и повторный прогон
        # обязан их пересоздать, а не наткнуться на прежние.
        models.RuleOverride,
    ):
        model.objects.filter(tenant__in=tenants).delete()
    models.PnlItem.objects.filter(tenant__in=tenants).delete()
    tenants.delete()
    models.User.objects.filter(
        pk__in=[det_id("user", role[0]) for role in ROLES]
    ).delete()


def _org() -> models.Tenant:
    tenant = models.Tenant.objects.create(
        id=det_id("tenant", TENANT_CODE),
        code=TENANT_CODE,
        title="Dodo Serbia (demo)",
        country_code=COUNTRY,
        base_currency="RSD",
        report_currency="EUR",
    )
    entities = {
        title: models.LegalEntity.objects.create(
            id=det_id("legal_entity", title), tenant=tenant,
            title=title, tax_number=tax_number,
        )
        for title, tax_number in dataset.LEGAL_ENTITIES
    }
    for code, title, entity_title in UNITS:
        models.Unit.objects.create(
            id=det_id("unit", code), tenant=tenant,
            legal_entity=entities[entity_title],
            code=code, title=title, opened_at=date(2023, 1, 1),
        )
    for month in MONTHS:
        models.Period.objects.create(
            id=det_id("period", str(month.period)), tenant=tenant,
            period=month.period,
            # Состояние учётного месяца — не то же, что состояние расчёта
            # зарплаты, но в демо они согласованы: закрытый месяц показан
            # закрытым и в списке периодов.
            status="closed" if month.approved else "open",
            closed_at=now() if month.approved else None,
        )
    return tenant


def _english_titles(tenant) -> None:
    """Английские подписи правил — переопределениями на уровне партнёра.

    Ставится до расчёта: подпись попадает в строку ведомости в момент счёта, и
    положенная позже она не догонит уже посчитанные месяцы.
    """
    for path, title in RULE_TITLES.items():
        models.RuleOverride.objects.create(
            id=det_id("rule_override", path), tenant=tenant,
            scope_type="tenant", scope_id=None,
            path=path, value=title, valid_from=TITLES_FROM,
        )


def _pnl_items() -> dict[str, models.PnlItem]:
    items = {}
    for code, title, kind, order in PNL_ITEMS:
        items[code], _ = models.PnlItem.objects.get_or_create(
            tenant=None, code=code,
            defaults={"id": det_id("pnl_item", code), "title": title,
                      "kind": kind, "sort_order": order},
        )
    return items


def _groups(tenant, items: dict) -> dict[str, models.EmployeeGroup]:
    """Группы — из пресета страны, подписи — английские.

    Схема и регистр берутся из того же пресета, по которому считает движок:
    второй источник истины здесь означал бы, что демо показывает не тот регистр,
    в котором лежит строка.
    """
    from payroll import load_preset

    preset = load_preset(PRESET_CODE)
    groups = {}
    for code, body in preset["groups"].items():
        groups[code] = models.EmployeeGroup.objects.create(
            id=det_id("group", code), tenant=tenant, code=code,
            title=GROUP_TITLES.get(code, code),
            scheme=body.get("scheme", "standard"),
            ledger=body.get("ledger", "official"),
            pnl_item=items["labour_cost"],
        )
    return groups


def _roles_and_users(tenant) -> None:
    units = {u.code: u for u in models.Unit.objects.filter(tenant=tenant)}
    password = demo_password()
    for code, title, ledgers, unit, permissions in ROLES:
        role = models.Role.objects.create(
            id=det_id("role", code), tenant=tenant, code=code, title=title,
            visible_ledgers=list(ledgers), permissions=list(permissions),
        )
        user = models.User.objects.create_user(
            username=code, password=password,
            id=det_id("user", code), full_name=title,
        )
        models.Membership.objects.create(
            id=det_id("membership", code), tenant=tenant,
            user_id=user.pk, role=role,
            unit_ids=[units[unit].id] if unit else None,
        )


def _calendar() -> None:
    """Производственный календарь на все три месяца.

    Пресет страны знает только июнь, а демо показывает три месяца. Норму месяца
    видно на странице периода, и «норма 176» в августе, где её 168, — ровно то
    правдоподобное неверное число, от которого продукт отказывается везде.
    """
    for month in MONTHS:
        models.Calendar.objects.update_or_create(
            country_code=COUNTRY, period=month.period,
            defaults={
                "norm_hours": month.norm_hours,
                "working_days": month.working_days,
                "holidays": [],
            },
        )


def _people(tenant, groups: dict) -> int:
    units = {u.code: u for u in models.Unit.objects.filter(tenant=tenant)}
    director = det_id("user", "director")

    for person in PEOPLE:
        employee = models.Employee.objects.create(
            id=det_id("employee", person.key), tenant=tenant,
            external_id=person.key,
            first_name=person.first, last_name=person.last,
            hired_at=person.hired,
        )
        for index, (valid_from, valid_to, rate) in enumerate(
            dataset.employment_versions(person)
        ):
            models.EmploymentTerm.objects.create(
                id=det_id("term", person.key, str(index)), tenant=tenant,
                employee=employee, group=groups[person.group],
                unit=units[person.unit],
                base_rate=rate, coefficient=person.coefficient,
                scheme=person.scheme, valid_from=valid_from, valid_to=valid_to,
            )
        for month in MONTHS:
            row = dataset.timesheet_for(person, month)
            if row is None:
                continue
            models.Timesheet.objects.create(
                id=det_id("timesheet", person.key, f"{month.period:%Y-%m}"),
                tenant=tenant, employee=employee, unit=units[person.unit],
                period=month.period,
                insured_hours=row.insured_hours,
                norm_hours=row.norm_hours,
                # Decimal в jsonb уходит строкой: чисел с плавающей точкой в
                # деньгах и часах в этом продукте нет нигде.
                hours={k: str(v) for k, v in row.hours.items()},
                deduction=row.deduction,
                cash_payout=row.cash_payout,
                manual_correction=row.manual_correction,
                correction_reason=row.correction_reason or None,
                # Правка руками обязана иметь автора (D025): правка без следа —
                # это сумма, которую через полгода никто не объяснит.
                corrected_by=director if row.manual_correction is not None else None,
                source="manual",
            )
    return len(PEOPLE)


def _approve(tenant, month: Month) -> None:
    payrun = models.Payrun.objects.filter(
        tenant_id=tenant.id, period=month.period
    ).first()
    approve(payrun, actor_id=det_id("user", "director"))


def person_by_key(key: str) -> Person | None:
    for person in PEOPLE:
        if person.key == key:
            return person
    return None


def money(value) -> Decimal:
    return Decimal(str(value or 0))
