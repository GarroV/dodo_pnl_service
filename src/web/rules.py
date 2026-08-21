"""Правила ведения правил расчёта (T090).

Что здесь и почему отдельно от представлений. Экран правил делает ровно три
вещи — показывает действующий пресет, объясняет, откуда взялось каждое
значение, и заводит новую версию. Все три обязаны соблюдать условия, которые
дороже разметки, поэтому они записаны здесь, а не разложены по шаблонам.

**Правило первое: экран правит слой партнёра, а не пресет страны.** Тело
`rule_presets` — общий справочник: у него нет `tenant_id`, он лежит в
`SHARED_TABLES` (`0004_rls`), и два партнёра одной страны читают одну и ту же
строку. Правка тела с экрана одного партнёра поменяла бы расчёт другому — молча
и задним числом. Поэтому правка всегда ложится строкой в `rule_overrides` на
уровне `tenant`. Это же и есть продуктовый принцип: страна поставляется готовым
набором, партнёр меняет только отличия.

**Правило второе: правка заводит новую версию с датой, а не переписывает
действующую.** Пресет собирается на дату (`core.rules.load_rules_at`), и расчёт
июня берёт версию, действовавшую в июне. Переписанное по месту значение
изменило бы прошлое: июнь, пересчитанный в августе, дал бы другие числа, чем
июнь, посчитанный в июне, — при том что ведомость уже на руках у людей
(D020, T026). Поэтому предыдущая версия **закрывается** датой начала новой, а
не затирается: границы полуоткрытые `[valid_from, valid_to)`, ровно как их
читает сборка пресета и как написано ограничение непересечения в базе.

**Правило третье: правку задним числом продукт принимает, а закрытый месяц не
переписывает** (D020, T121). Версия правила с датой внутри утверждённого месяца
заводится — отказа здесь больше нет, — а разницу считает и переносит
`payrun.retro`. Слова о том, что при этом произойдёт, берутся у справочников
(`web/directory.closed_month_warning`), а не пишутся своей копией: два
объяснения одного и того же разъедутся на первой правке.

**Правило четвёртое: пресет показывается в срезе роли (D023).** Тело правил
называет регистр учёта — у групп и у надбавок. Роль, которая регистра не видит,
не должна узнать о нём из экрана настроек: это тот же регистр и то же
разграничение, что в ведомости и в справочнике групп. Узел с чужим регистром не
показывается целиком — ни значением, ни путём, ни в истории версий.

**Правило пятое: правило действует помесячно, и это сказано словами** (T139,
issue #99). Расчёт берёт правила на месяц целиком, поэтому версия с датой внутри
месяца подействует только со следующего — в отличие от условий найма, где
середина месяца работает. Человек читает об этом дважды: на форме до правки и на
списке после неё (`monthly_help`, `effective_month_notice`).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from django.db import transaction
from django.utils.translation import gettext as _

from core.models import RuleOverride

# Уровень, на который ложится правка с экрана. Один и записан здесь, а не
# приезжает из формы: выбор уровня — это выбор, кого правило заденет, и делать
# его скрытым полем формы значило бы отдать наружу решение, которое объяснено
# выше как правило первое.
SCOPE = "tenant"

# Ключи, которыми пресет называет сам себя. Не настройки расчёта, а его имя,
# страна, валюта и дата начала действия — переопределять их поверх самих себя
# бессмысленно: сборка пресета выбирает строку `rule_presets` ДО того, как
# накладывает переопределения, и `valid_from`, заведённый поверх, ни на что не
# повлиял бы, но выглядел бы работающим.
#
# На экране их нет вовсе, а не «показаны и не правятся»: набор правил и страна
# уже подписаны над таблицей, и четыре строки-раздела из одного значения только
# мешали бы читать остальные сто тридцать.
IDENTITY = ("preset", "country", "currency", "valid_from", "title")

# Названия разделов пресета на языке страницы. Словарём, а не полем в теле
# правил: тело — это правила страны, и подписи интерфейса ему не принадлежат.
# Незнакомый раздел показывается своим ключом — молча пропасть он не должен.
SECTION_TITLES = {
    "constants": lambda: _("Константы страны"),
    "rates": lambda: _("Ставки налогов и взносов"),
    "hour_types": lambda: _("Типы часов"),
    "allowances": lambda: _("Надбавки"),
    "minimum_guarantee": lambda: _("Доплата до минимума"),
    "work_measures": lambda: _("Чем меряется работа"),
    "schemes": lambda: _("Схемы расчёта"),
    "groups": lambda: _("Группы сотрудников"),
    "calendar": lambda: _("Производственный календарь"),
    "variance": lambda: _("Пороги отчёта расхождений"),
}

# Откуда взялось значение — словами. Уровни те же, что в `payroll.presets.LEVELS`.
LEVEL_TITLES = {
    "country": lambda: _("правила страны"),
    "tenant": lambda: _("настройка партнёра"),
    "group": lambda: _("переопределение группы"),
    "employee": lambda: _("переопределение по человеку"),
}

# Пути, у которых значение выбирается из списка, а не набирается руками.
# Ключ — последний сегмент пути, значение — раздел пресета, который перечисляет
# допустимое. Список короткий намеренно: сюда попадает только то, где опечатка
# меняет деньги молча. `work_measure` — ровно такой случай (D032): движок чужую
# меру отвергает по имени, но узнаётся это на расчёте, а не при наборе.
CHOICES_FROM = {
    "work_measure": "work_measures",
    "scheme": "schemes",
}


class RuleInputRefused(Exception):
    """Введено не то. Отдельно от отказа по состоянию данных, как и в справочниках."""

    http_status = 400

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def section_title(name: str) -> str:
    maker = SECTION_TITLES.get(name)
    return maker() if maker is not None else name


def level_title(level: str) -> str:
    maker = LEVEL_TITLES.get(level)
    return maker() if maker is not None else level


# --- что видно роли -----------------------------------------------------------


def _node_ledger(node: Any) -> str | None:
    """Регистр учёта, который узел объявляет о себе. Не объявляет — None."""
    if isinstance(node, dict):
        value = node.get("ledger")
        if isinstance(value, str):
            return value
    return None


def hidden_paths(preset: dict, visible_ledgers) -> tuple[str, ...]:
    """Узлы правил, регистр которых роли не виден (D023).

    Ищется не список групп, а **любой узел, который называет регистр**: сегодня
    это группы и надбавки, завтра — что-нибудь ещё. Проверка «а какие сейчас
    разделы несут ledger» жила бы в голове у автора и не пережила бы новую
    страну; проверка «узел назвал регистр» переживает.
    """
    seen = set(visible_ledgers or [])
    hidden: list[str] = []

    def walk(node: dict, prefix: str) -> None:
        for key, value in node.items():
            path = f"{prefix}{key}"
            ledger = _node_ledger(value)
            if ledger is not None and ledger not in seen:
                hidden.append(path)
                continue
            if isinstance(value, dict):
                walk(value, path + ".")

    walk(preset, "")
    return tuple(hidden)


def is_visible(path: str, hidden: tuple[str, ...]) -> bool:
    """Виден ли путь роли. Скрытый узел прячет и всё, что внутри него."""
    return not any(path == item or path.startswith(item + ".") for item in hidden)


# --- разбор значения ----------------------------------------------------------


def kind_of(value: Any) -> str:
    """Каким полем правится значение. Тип берётся у действующего значения.

    Тип не спрашивается у человека и не угадывается по введённому: правило
    `pay_percent` — число и в июне, и в августе, а строка «0,65», попавшая в
    jsonb вместо числа, сломала бы расчёт не здесь и не сразу.
    """
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float, Decimal)):
        return "number"
    if isinstance(value, list):
        return "list"
    return "text"


def show(value: Any) -> str:
    """Значение в том виде, в каком его показывают и вводят обратно."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    if value is None:
        return ""
    return str(value)


def parse(raw: str, current: Any, label: str) -> Any:
    """Введённое, приведённое к типу действующего значения. Не приводится — отказ."""
    raw = (raw or "").strip()
    kind = kind_of(current)
    if kind == "bool":
        if raw in ("true", "1", "yes"):
            return True
        if raw in ("false", "0", "no"):
            return False
        raise RuleInputRefused(
            _("«%(label)s»: нужно «да» или «нет», а не «%(value)s».")
            % {"label": label, "value": raw}
        )
    if kind == "number":
        try:
            number = float(raw.replace(",", "."))
        except ValueError:
            raise RuleInputRefused(
                _("«%(label)s»: нужно число, а не «%(value)s».")
                % {"label": label, "value": raw}
            ) from None
        # Целое остаётся целым: `norm_hours = 176.0` в правилах читается как
        # ошибка ввода, хотя число то же.
        if isinstance(current, int) and not isinstance(current, bool) and number.is_integer():
            return int(number)
        return number
    if kind == "list":
        return [item.strip() for item in raw.split(",") if item.strip()]
    if not raw:
        raise RuleInputRefused(_("«%(label)s»: значение обязательно.") % {"label": label})
    return raw


def choices_for(preset: dict, path: str) -> list[tuple[str, str]]:
    """Допустимые значения пути, если они перечислены самим пресетом.

    Список берётся из тела правил, а не из константы в коде: новая мера работы
    или новая схема появляется у страны в YAML, и экран обязан предложить её,
    не дожидаясь правки интерфейса.
    """
    section = CHOICES_FROM.get(path.rsplit(".", 1)[-1])
    if section is None:
        return []
    body = preset.get(section)
    if not isinstance(body, dict):
        return []
    return [
        (code, (item or {}).get("title") or code if isinstance(item, dict) else code)
        for code, item in body.items()
    ]


# Те же списки, но спрошенные не про правило, а про **человека** (T164). У схемы
# расчёта и меры работы в условиях найма своего пути правила нет: они лежат
# колонками в `employment_terms`. Список допустимого при этом обязан быть тем же
# самым, что у правила группы, — иначе на двух соседних экранах предлагались бы
# разные наборы схем, и разъехались бы они молча.
#
# Двумя именами, а не одним `choices_for(preset, "scheme")` в вызывающем: голый
# ключ вместо пути читается как опечатка, и следующий человек «починил» бы его.


def scheme_choices(preset: dict) -> list[tuple[str, str]]:
    """Схемы расчёта, которые перечисляют правила страны."""
    return choices_for(preset, "scheme")


def measure_choices(preset: dict) -> list[tuple[str, str]]:
    """Способы измерения работы, которые перечисляют правила страны."""
    return choices_for(preset, "work_measure")


# --- чтение действующего значения ---------------------------------------------


def value_at(preset: dict, path: str) -> Any:
    """Значение по пути через точку. Пути нет — KeyError, а не None.

    Разница существенная: `None` означал бы «правило есть, значение пустое», и
    опечатка в адресе давала бы форму правки несуществующего правила.
    """
    node: Any = preset
    for key in path.split("."):
        if not isinstance(node, dict) or key not in node:
            raise KeyError(path)
        node = node[key]
    return node


@dataclass(frozen=True)
class Leaf:
    """Одно правило: путь, значение и кто его задал."""

    path: str
    title: str
    value: str
    level: str
    valid_from: date | None
    editable: bool


def leaves(preset, *, hidden: tuple[str, ...] = ()) -> list[Leaf]:
    """Все правила пресета листьями, кроме тех, что называют сам пресет.

    Путь сортируется, а не берётся в порядке тела, и это не вкус. Тело приезжает
    из `jsonb`, а он порядок ключей **не хранит**: Postgres раскладывает их по
    длине и байтам. Порядок YAML, в котором правила написаны для человека, до
    экрана не доезжает вовсе — проверено на живом стенде, где разделы вышли
    вперемешку. Раз своего порядка нет, лучше предсказуемый алфавитный, чем
    случайный: по алфавиту правило хотя бы находится глазами.
    """
    rows: list[Leaf] = []

    def walk(node: dict, prefix: str) -> None:
        for key, value in sorted(node.items()):
            path = f"{prefix}{key}"
            if not is_visible(path, hidden) or path in IDENTITY:
                continue
            if isinstance(value, dict):
                walk(value, path + ".")
                continue
            where = preset.origin_of(path) if hasattr(preset, "origin_of") else None
            rows.append(
                Leaf(
                    path=path,
                    title=path.split(".")[-1],
                    value=show(value),
                    level=where.level if where else "country",
                    valid_from=where.valid_from if where else None,
                    editable=True,
                )
            )

    walk(dict(preset), "")
    return rows


def sections(preset, *, hidden: tuple[str, ...] = ()) -> list[dict]:
    """Правила, разложенные по разделам: так их и читают.

    Порядок разделов задан здесь (`SECTION_TITLES`), а не приходит из данных, по
    той же причине: из базы он не приходит вовсе. Раздел, которого этот список
    не знает, показывается **после** известных и своим ключом — молча пропасть
    он не должен, иначе новая страна принесла бы правила, которых нет на экране.
    """
    known = list(SECTION_TITLES)
    grouped: dict[str, list[Leaf]] = {}
    for leaf in leaves(preset, hidden=hidden):
        grouped.setdefault(leaf.path.split(".")[0], []).append(leaf)
    order = [name for name in known if name in grouped]
    order += sorted(name for name in grouped if name not in known)
    return [
        {"name": name, "title": section_title(name), "rows": grouped[name]} for name in order
    ]


# --- с какого месяца версия подействует ---------------------------------------
#
# Правило пятое, и оно про молчание (issue #99). Расчёт берёт правила на месяц
# целиком: `payrun.rules.select_rules(tenant, country, period)`, где `period` —
# первое число месяца. Значит версия с датой внутри месяца на этот месяц не
# влияет **вовсе**: она начинает действовать со следующего.
#
# У условий найма это не так: там версия ищется по `valid_from <= конец месяца`,
# и середина месяца работает (`payrun.calc.collect_cases`). Продуктом это не
# противоречие — правило действует помесячно, ставка человека с даты, — но поле
# «Действует с» на двух соседних экранах называется одинаково, а означает
# разное, и прочитать об этом человеку было негде. Проверено фактически: правка
# с 15 июня при утверждённом июне не даёт ни одной строки расхождения, та же
# правка с 1 июня даёт 35.
#
# Отсюда и слова: одна фраза на форме (до правки) и одна на списке (после), обе
# считаются из даты и живут здесь, а не в шаблоне и не в представлении. Копия на
# каждом экране разъехалась бы с первой правкой — тот же довод, что у
# `directory.closed_month_warning`.


def effective_month(valid_from: date) -> date:
    """С какого месяца версия правила начнёт действовать на самом деле."""
    if valid_from.day == 1:
        return valid_from
    if valid_from.month == 12:
        return date(valid_from.year + 1, 1, 1)
    return date(valid_from.year, valid_from.month + 1, 1)


def monthly_help() -> str:
    """Подсказка под полем «Действует с» — до правки.

    Прежняя говорила только про месяцы **до** даты, то есть ровно про то, о чём
    человек не спрашивал, и умалчивала про месяц, в который он метит.
    """
    return _(
        "С этой даты расчёт берёт новое значение, месяцы до неё считаются "
        "по-прежнему. Правила берутся на месяц целиком, поэтому дата внутри "
        "месяца подействует только со следующего: чтобы правка сработала на "
        "весь месяц, ставьте первое число."
    )


def effective_month_notice(valid_from: date) -> str:
    """Что случилось — после правки. Пусто, если дата и так первое число.

    Предупреждение не по делу обесценивает предупреждение по делу, поэтому
    молчим там, где версия работает ровно так, как человек и ожидал.
    """
    starts = effective_month(valid_from)
    if starts == valid_from:
        return ""
    from .i18n import month_title

    return _(
        "Версия заведена с %(date)s, а действовать начнёт с расчёта за "
        "%(starts)s: правила берутся на месяц целиком. %(inside)s останется "
        "посчитанным по-прежнему — нужна была правка с начала месяца, заведите "
        "версию с %(first)s."
    ) % {
        "date": valid_from.isoformat(),
        "starts": month_title(starts),
        "inside": month_title(valid_from.replace(day=1)),
        "first": valid_from.replace(day=1).isoformat(),
    }


# --- история и правка ---------------------------------------------------------


def versions(tenant_id, path: str):
    """Версии настройки партнёра по этому пути — от старой к новой."""
    return (
        RuleOverride.objects.filter(tenant_id=tenant_id, scope_type=SCOPE, path=path)
        .order_by("valid_from")
    )


@dataclass(frozen=True)
class RuleChange:
    changed: bool
    previous: RuleOverride | None


@transaction.atomic
def save_override(tenant_id, path: str, value: Any, *, valid_from: date,
                  actor_id=None, effective: Any = None) -> RuleChange:
    """Завести новую версию правила с указанной даты.

    `effective` — значение, действующее на эту дату сейчас (собранное со всеми
    слоями). Совпало — версия не заводится вовсе: иначе история обрастала бы
    строками «то же самое с другой даты», и настоящая смена правила терялась бы
    среди них. Тот же довод и то же поведение, что у условий найма.
    """
    if effective is not None and value == effective:
        return RuleChange(changed=False, previous=None)

    current = (
        RuleOverride.objects.filter(
            tenant_id=tenant_id, scope_type=SCOPE, path=path, valid_from__lte=valid_from
        )
        .exclude(valid_to__lte=valid_from)
        .order_by("valid_from")
        .last()
    )
    if current is not None:
        if current.valid_from == valid_from:
            # Версия начинается тем же днём: отдельно от новой она не
            # действовала ни одного дня, а вторая строка с той же датой всё
            # равно не прошла бы — пересечение периодов запрещено ограничением
            # `rule_overrides_no_overlap`. Терять здесь нечего.
            current.value = value
            current.created_by = actor_id
            current.save(update_fields=["value", "created_by"])
            return RuleChange(changed=True, previous=None)
        RuleOverride.objects.filter(pk=current.pk).update(valid_to=valid_from)

    RuleOverride.objects.create(
        tenant_id=tenant_id,
        scope_type=SCOPE,
        scope_id=None,
        path=path,
        value=value,
        valid_from=valid_from,
        valid_to=current.valid_to if current is not None else None,
        created_by=actor_id,
    )
    return RuleChange(changed=True, previous=current)
