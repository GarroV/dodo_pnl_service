"""Запись следа расчёта в базу и чтение его обратно (T056, issue #48).

След пишет тот же расчёт, который пишет суммы, одним куском с ними: объяснение,
записанное отдельным проходом, разъехалось бы с числами ровно тогда, когда это
важнее всего — на закрытом месяце.

**Почему входы шага упакованы, а не сложены в JSON как есть.** Во входах лежат
`Decimal`: ставка 371,00, процент 1,10, коэффициент 0,701. Обычный JSON знает
только float, и 0,701 вернулся бы как 0,7009999999999999 — объяснение денег
поехало бы в третьем знаке. Поэтому Decimal хранится строкой с пометкой типа, а
не числом. Пометка нужна, чтобы отличить число от строки, которая просто похожа
на число: во входах есть и настоящие строки («worked_days», «min_hourly_rate»),
и угадывание по виду однажды превратило бы одну из них в сумму.
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any

__all__ = ["pack_inputs", "store_steps", "unpack_inputs"]

# Ключ пометки. Начинается с доллара, потому что ключей движка такого вида нет и
# не будет: они английские имена величин (`hours`, `rate`, `pay_percent`).
DECIMAL_TAG = "$dec"


def pack(value: Any) -> Any:
    """Значение входа в JSON-совместимом виде, без потери точности."""
    if isinstance(value, Decimal):
        return {DECIMAL_TAG: str(value)}
    if isinstance(value, dict):
        return {key: pack(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [pack(item) for item in value]
    return value


def unpack(value: Any) -> Any:
    """Обратное преобразование: помеченное число снова становится Decimal."""
    if isinstance(value, dict):
        tagged = value.get(DECIMAL_TAG)
        if tagged is not None and len(value) == 1:
            return Decimal(tagged)
        return {key: unpack(item) for key, item in value.items()}
    if isinstance(value, list):
        return [unpack(item) for item in value]
    return value


def pack_inputs(values: dict[str, Any]) -> dict[str, Any]:
    return {key: pack(value) for key, value in (values or {}).items()}


def unpack_inputs(values: dict[str, Any]) -> dict[str, Any]:
    return {key: unpack(value) for key, value in (values or {}).items()}


def rows_for(tenant_id, payslip, slip, components, money):
    """Строки следа одной ведомости: сначала слагаемые, потом производные.

    `components` — компоненты **той же** строки в том же порядке, в котором их
    сложил движок: регистр шага берётся из его компонента, потому что движок
    кладёт их парой (`Payslip.add`), и парность закреплена тестом
    `test_every_component_has_a_step_behind_it`. Записанный регистр дальше живёт
    сам — на нём стоит политика видимости, и выводить его заново при каждом
    чтении значило бы завести второй источник истины для доступа.

    `money` передаётся, а не импортируется: округление шага обязано быть тем же
    самым, каким записана сумма, и единственный способ не дать им разъехаться —
    брать одну функцию, а не две одинаковые.
    """
    from core.models import PayslipStep

    net_steps = [step for step in slip.trace if step.contributes_to == "net"]
    ledgers = [component.ledger for component in components]
    if len(net_steps) != len(ledgers):
        # Молча записать «сколько получилось» нельзя: шаги и компоненты уехали
        # бы друг относительно друга, и объяснение приписалось бы чужой сумме.
        raise ValueError(
            f"шагов {len(net_steps)}, а компонентов {len(ledgers)} — "
            "след не сходится с ведомостью"
        )

    ordered = [*zip(net_steps, ledgers, strict=True)] + [
        (step, None) for step in slip.trace if step.contributes_to != "net"
    ]
    return [
        PayslipStep(
            tenant_id=tenant_id, payslip=payslip, position=position,
            code=step.rule_code, title=step.title,
            applied_value=money(step.applied_value),
            ledger=ledger,
            kind=step.contributes_to,
            input_values=pack_inputs(step.input_values),
            source_level=step.source_level,
            rule_version_id=step.rule_version_id,
        )
        for position, (step, ledger) in enumerate(ordered)
    ]


def store_steps(tenant_id, rows, slips, money) -> int:
    """Записать след всех строк ведомости. Возвращает число шагов.

    Отдельного удаления прежних шагов здесь нет намеренно: строки ведомости
    пересоздаются целиком, а шаг привязан к строке внешним ключом с каскадом.
    Своя уборка была бы вторым правилом жизни следа рядом с первым — и однажды
    разошлась бы с ним.
    """
    from core.models import PayslipStep

    steps = [
        step
        for row, (_case, slip) in zip(rows, slips, strict=True)
        for step in rows_for(tenant_id, row, slip, slip.components, money)
    ]
    PayslipStep.objects.bulk_create(steps)
    return len(steps)
