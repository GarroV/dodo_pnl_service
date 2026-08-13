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

# Язык демо. Правило владельца: демо всегда англоязычное, независимо от языка
# продукта. Отсюда и берутся английские подписи правил — из самого пресета
# страны, который с T092 несёт все языки продукта сразу.
#
# Раньше здесь лежал словарь из двадцати английских подписей, который клался в
# базу переопределениями партнёра. Он больше не нужен и удалён намеренно: это
# была вторая правда рядом с пресетом, и расходиться они начали бы молча —
# поменянная подпись в пресете просто не доехала бы до демо. А главное, словарь
# **маскировал дефект**: демо выглядело английским даже тогда, когда сам продукт
# показывал колонки по-русски, и полтора месяца никто этого не видел.
DEMO_LANGUAGE = "en"

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

    # Пресет читается на языке демо: подписи групп ложатся в базу английскими
    # оттуда же, откуда движок берёт схему и регистр (T092). Второго списка
    # названий в этом файле больше нет.
    preset = load_preset(PRESET_CODE, DEMO_LANGUAGE)
    groups = {}
    for code, body in preset["groups"].items():
        groups[code] = models.EmployeeGroup.objects.create(
            id=det_id("group", code), tenant=tenant, code=code,
            title=body.get("title") or code,
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
