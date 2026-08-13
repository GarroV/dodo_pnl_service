"""Правило разнесения статьи расхода: как оно ведётся человеком (T111).

Правило отвечает на вопрос «расход юрлица целиком — как его разложить по
точкам»: поровну, пропорционально выручке или на одну фиксированную точку.
Ключ правила — **статья расхода**, потому что у расхода из кассы контрагента
нет и взяться ему неоткуда (разбор — миграция `0233`).

Три решения, ради которых этот модуль отдельный от экрана.

**Правило версионируется, а не переписывается.** Смена метода с середины года —
это новая версия с датой, а старая закрывается тем же днём. Иначе закрытый
месяц пересчитался бы новым правилом при первом же пересчёте, и июнь через
полгода дал бы другое число, чем июнь сегодня (D020).

**Задним числом в утверждённый месяц правило не заводится.** Отказ — тот же
самый, что у остальных версионированных правил справочника
(`refuse_if_touches_closed_month`), и с той же подсказкой: возьмите дату позже
или откройте месяц заново с причиной. Второй формулировки одного отказа быть не
должно.

**Пересчёт после правки идёт сразу, но только по открытым месяцам.** Правило,
которое поменяли и не применили, — это правило, о котором человек думает, что
оно работает. Закрытые месяцы пересчёт пропускает и называет вслух (см.
`cash.reallocate`).
"""
from __future__ import annotations

from datetime import date

from django.utils.translation import gettext as _

from core.models import AllocationRule

from .directory_views import BadInput

# Способы разнесения, которые предлагаются человеку. `ask` в списке есть
# намеренно: «спрашивать каждый раз» — это честный ответ «правило зависит от
# случая», и факт при нём остаётся ждать в списке нераспределённых, а не
# растекается по точкам наугад.
METHODS = ("even", "by_revenue", "fixed_unit", "ask")

METHOD_TITLES = {
    "even": _("Поровну между точками"),
    "by_revenue": _("Пропорционально выручке"),
    "fixed_unit": _("На одну точку"),
    "ask": _("Спрашивать каждый раз"),
}

NO_RULE = ""


def method_title(code: str) -> str:
    known = METHOD_TITLES.get(code)
    return str(known) if known is not None else code


def rules_of(item) -> list[AllocationRule]:
    """Все версии правила статьи — и действующая, и прошлые.

    История показывается человеку целиком: по ней видно, каким правилом посчитан
    закрытый месяц, а это первое, что спрашивают при расхождении в отчёте.
    """
    return list(
        AllocationRule.objects.filter(expense_item_id=item.id)
        .select_related("unit")
        .order_by("ledger", "-valid_from")
    )


def current_rule(item, ledger: str) -> AllocationRule | None:
    """Действующая версия правила статьи в этом регистре."""
    return (
        AllocationRule.objects.filter(
            expense_item_id=item.id, ledger=ledger, valid_to__isnull=True
        )
        .order_by("-valid_from")
        .first()
    )


def save_rule(who, item, *, ledger: str, method: str, unit_id, valid_from: date) -> bool:
    """Завести или сменить правило разнесения статьи. Возвращает «что-то менялось».

    Возврат нужен вызывающему, чтобы не гонять пересчёт зря: пересчёт на
    неизменившихся правилах и так ничего не пишет, но и говорить о нём человеку,
    который правил одно название статьи, незачем.
    """
    if method not in (*METHODS, NO_RULE):
        raise BadInput(_("«%(label)s»: такого варианта нет.") % {"label": _("Разнесение")})
    if method == "fixed_unit" and not unit_id:
        raise BadInput(
            _("Для разнесения «%(method)s» нужно выбрать точку.")
            % {"method": method_title("fixed_unit")}
        )

    current = current_rule(item, ledger)
    if method == NO_RULE:
        return _close(who, current, valid_from)

    if current is not None and _same(current, method, unit_id):
        return False

    # Отказа по дате здесь нет намеренно, и это не послабление, а общее правило
    # продукта (D020, доведено до экранов в T121): правку **с датой** продукт
    # принимает, а закрытый месяц ею не переписывает. У правила разнесения даты
    # версионируются, поэтому оно подчиняется общему правилу.
    #
    # Закрытый месяц защищён не отказом, а тем, что пересчёт его пропускает и
    # **называет вслух** (см. `cash.reallocate` и проверку
    # `test_recalculation_skips_a_closed_month_and_says_so`): молчаливый пропуск
    # читался бы как «пересчитано». Плюс сторож базы `facts_guard`, который
    # строку закрытого месяца тронуть не даст в любом случае.

    if current is not None:
        if valid_from < current.valid_from:
            raise BadInput(
                _(
                    "Правило уже действует с %(from)s: новая версия не может "
                    "начинаться раньше. Возьмите дату позже."
                ) % {"from": current.valid_from.isoformat()}
            )
        if current.valid_from == valid_from:
            # Тот же день — это правка самой версии, а не новая: две версии с
            # одной датой начала база не примет (`allocation_rules_item_no_overlap`),
            # да и ответа «какая из них действует» у них не было бы.
            current.method, current.unit_id = method, unit_id or None
            current.save()
            return True
        current.valid_to = valid_from
        current.save()

    AllocationRule.objects.create(
        tenant_id=who.tenant_id,
        expense_item_id=item.id,
        # Строка P&L берётся у статьи, а не спрашивается второй раз: у правила
        # она обязана совпадать со статьёй, иначе разнесённые дети попадали бы в
        # другую строку отчёта, чем сам расход.
        pnl_item_id=item.pnl_item_id,
        method=method,
        unit_id=unit_id or None,
        ledger=ledger,
        valid_from=valid_from,
        created_by=who.user_id,
    )
    return True


def _same(rule, method: str, unit_id) -> bool:
    return rule.method == method and str(rule.unit_id or "") == str(unit_id or "")


def _close(who, current, valid_from: date) -> bool:
    """Убрать правило — датой, а не удалением: закрытые месяцы на него ссылаются."""
    if current is None:
        return False
    # Отказа по дате нет по тому же доводу, что и при заведении версии выше:
    # правку с датой продукт принимает, закрытый месяц пересчётом не трогает и
    # говорит об этом словами.
    if valid_from <= current.valid_from:
        raise BadInput(
            _(
                "Правило действует с %(from)s: убрать его можно только более "
                "поздней датой."
            ) % {"from": current.valid_from.isoformat()}
        )
    current.valid_to = valid_from
    current.save()
    return True
