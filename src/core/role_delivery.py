"""Форма ролей из кода доезжает до поднятой базы (T169, issue #126).

Зачем это нужно. `core.roles.ROLE_SHAPES` — форма ролей продукта: какие
регистры учёта роль видит и что ей позволено. В базе роли лежат **данными**, и
заводит их сид — один раз, при пересоздании тенанта. Значит правка
`ROLE_SHAPES` не меняет уже работающую базу ничем: ни миграцией, ни при старте,
ни предупреждением. Цена заплачена дважды — T089 («администратор видит 8
сотрудников из 35») и «Расчёт периода не входит в права вашей роли» у
владельца: оба раза код был уже прав, стенд остался прежним, и увидели это
глазами, а не проверкой.

Миграции `0110`, `0220` и `0230` делали ровно это руками, по одному праву за
раз (`update roles set permissions = permissions || …`). Приём рабочий, но
каждую такую миграцию надо не забыть написать, а забытая молчит.

**Роль помнит, что ей поставил продукт.** Колонка `roles.shipped_shape` —
снимок формы, записанный в тот момент, когда форму ставил сам продукт: сид или
эта доставка. Без такой записи правку партнёра нельзя отличить от того, что код
ушёл вперёд: видно только «в базе одно, в коде другое», а кто из двоих прав —
неизвестно. Затирать правку партнёра — то же молчание, что и не доставлять,
только в другую сторону (экран `/roles/` появился с T171, и правки там уже
возможны).

Приём не свой. `kubectl` держит на объекте `last-applied-configuration` и
сравнивает три состояния — поставленное, живое и новое; `dpkg` держит хэш
поставленного конфига и молча заменяет только тот файл, которого человек не
касался, а тронутый показывает разъездом. Изобретать тут нечего.

**Четыре исхода на роль:**

| исход     | признак                        | что делаем                            |
|-----------|--------------------------------|---------------------------------------|
| `match`   | база == код                    | ничего (снимок дописываем, если нет)  |
| `behind`  | база == снимок, код впереди    | доставляем                            |
| `edited`  | база != снимок                 | **не трогаем**, показываем разъезд    |
| `unknown` | снимка нет, база != код        | **не трогаем**, показываем            |

`unknown` — это стенды, поднятые до появления колонки. Продукт не вправе решать
за человека, его там правка или наше отставание, поэтому показывает и ждёт.
Выход из ожидания один и явный: `manage.py roles_sync --adopt` объявляет
текущее состояние поставленным продуктом, после чего доставка едет как обычно.

**Почему проверяется, обходит ли роль RLS.** `roles` под `force row level
security`, и с `0242` запись в неё требует права `roles.manage`. Роль, которая
политики не обходит, увидела бы отсюда ноль строк, обновила бы ноль строк и
отчиталась бы «доставлять нечего» — зелено и неправда. Это тот же корень, что у
issue #44 и у проверки в конце миграции `0110`. Поэтому доставка сначала
спрашивает у базы, обходит ли текущая роль политики, и говорит словами, если
нет; а после записи перечитывает роль и падает, если запись не легла.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from django.db import transaction

from core.roles import ALL_LEDGERS, ROLE_SHAPES

__all__ = [
    "BEHIND",
    "EDITED",
    "MATCH",
    "UNKNOWN",
    "DeliveryRefused",
    "from_jsonb",
    "Report",
    "RoleState",
    "sync",
    "verdict",
]

MATCH = "match"
BEHIND = "behind"
EDITED = "edited"
UNKNOWN = "unknown"

# Порядок вывода: сначала то, что требует действия, в конце — то, что в порядке.
STATE_ORDER = (BEHIND, UNKNOWN, EDITED, MATCH)

STATE_WORDS = {
    MATCH: "совпадает с кодом",
    BEHIND: "отстала от кода",
    EDITED: "правлена человеком — продукт её не трогает",
    UNKNOWN: "снимка формы нет — что там, продукт не знает",
}


class DeliveryRefused(RuntimeError):
    """Доставка не легла в базу. Молчать про это нельзя — см. issue #44."""


def normalize(ledgers, permissions) -> dict[str, list[str]]:
    """Форма в одном виде: отсортированные множества.

    Сортировка нужна для сравнения, а не для красоты. В базе порядок прав какой
    угодно — `0110` дописывала право в конец списка, — и сравнение списками
    объявило бы «правлена человеком» роль, у которой форма ровно та же, просто
    в другом порядке. Множество, а не список, по той же причине: дубль в
    `permissions` формы не меняет.
    """
    return {
        "visible_ledgers": sorted(set(ledgers or ())),
        "permissions": sorted(set(permissions or ())),
    }


def product_shape(code: str) -> dict[str, list[str]]:
    """Форма, которую ставит сегодняшний код."""
    shape = ROLE_SHAPES[code]
    return normalize(shape.ledgers, shape.permissions)


def from_jsonb(raw):
    """Значение колонки `jsonb`, прочитанной курсором Django.

    Курсор Django отдаёт `jsonb` **строкой**, а не готовым объектом: бэкенд
    Postgres ставит на соединение свой загрузчик (`set_json_loads(lambda x: x)`),
    потому что разбирать json — дело поля `JSONField`, у которого может быть свой
    декодер. Сырой psycopg на том же запросе вернул бы список, и это ровно тот
    случай, когда «проверено в psql, работает» ничего не значит.

    Молча это не ломается, ломается неузнаваемо: `set("[\\"unit.close\\"]")` —
    это множество символов, и диф прав начинает показывать запятые и скобки.
    Найдено первым же прогоном именно так.
    """
    if isinstance(raw, bytes):
        raw = raw.decode()
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except ValueError:
            return None
    return raw


def snapshot_of(raw) -> dict[str, list[str]] | None:
    """Снимок из базы в том же виде. Не словарь — считаем, что снимка нет.

    «Мусор в колонке» и «снимка нет» ведут себя одинаково намеренно: оба
    означают, что продукт не знает, что он ставил, — и оба обязаны привести к
    исходу `unknown`, то есть к «не трогаем и показываем».
    """
    value = from_jsonb(raw)
    if not isinstance(value, dict):
        return None
    return normalize(value.get("visible_ledgers"), value.get("permissions"))


def verdict(*, db: dict, shipped: dict | None, wanted: dict) -> str:
    """Один из четырёх исходов. Вся логика решения — здесь, и только здесь."""
    if db == wanted:
        return MATCH
    if shipped is None:
        return UNKNOWN
    if shipped == db:
        return BEHIND
    return EDITED


def diff_lines(db: dict, wanted: dict) -> list[str]:
    """Чем база отличается от кода — построчно, знаками `+` и `-`."""
    lines: list[str] = []
    for key, label in (("visible_ledgers", "регистры"), ("permissions", "права")):
        was, will_be = set(db[key]), set(wanted[key])
        added, removed = sorted(will_be - was), sorted(was - will_be)
        if added:
            lines.append(f"+ {label}: {', '.join(added)}")
        if removed:
            lines.append(f"- {label}: {', '.join(removed)}")
    return lines


@dataclass(frozen=True)
class RoleState:
    """Роль базы и вердикт по ней."""

    role_id: str
    tenant: str
    code: str
    title: str
    state: str
    db: dict
    shipped: dict | None
    wanted: dict

    @property
    def diff(self) -> list[str]:
        return diff_lines(self.db, self.wanted)

    def __str__(self) -> str:
        return f"{self.tenant}/{self.code} «{self.title}»: {STATE_WORDS[self.state]}"


@dataclass
class Report:
    """Что доставка увидела и что сделала."""

    ready: bool = True             # колонка снимка есть, механизм применим
    bypasses_rls: bool = True     # текущая роль базы видит все строки
    states: list[RoleState] = field(default_factory=list)
    delivered: list[RoleState] = field(default_factory=list)
    adopted: list[RoleState] = field(default_factory=list)
    recorded: list[RoleState] = field(default_factory=list)
    foreign: int = 0               # роли, которых в форме продукта нет вовсе

    def by_state(self, state: str) -> list[RoleState]:
        return [item for item in self.states if item.state == state]

    @property
    def in_sync(self) -> bool:
        """Все роли продукта совпадают с кодом.

        Считается по состоянию **после** доставки: вопрос «база и код сходятся?»
        задаётся не про историю, а про сейчас.
        """
        return all(item.state == MATCH for item in self.states)


_COLUMN_SQL = """
select 1
  from information_schema.columns
 where table_schema = current_schema()
   and table_name = 'roles'
   and column_name = 'shipped_shape'
"""

# Обходит ли текущая роль политики. Именно эти два атрибута и означают обход
# `force row level security`; владение таблицей его не отменяет (проверено на
# живой базе при постройке `0242`).
_BYPASS_SQL = """
select coalesce(bool_or(rolsuper or rolbypassrls), false)
  from pg_roles
 where rolname = current_user
"""

# `left join`, а не `join`: тенант под своей политикой чтения, и роль не должна
# пропасть из отчёта из-за того, что не видно её партнёра.
_READ_SQL = """
select r.id::text, coalesce(t.code, '(общая)'), r.code, r.title,
       r.visible_ledgers, r.permissions, r.shipped_shape
  from roles r
  left join tenants t on t.id = r.tenant_id
 where r.code = any(%s)
 order by coalesce(t.code, ''), r.code
"""

_COUNT_FOREIGN_SQL = "select count(*) from roles where not (code = any(%s))"

_WRITE_SQL = """
update roles
   set visible_ledgers = %s::ledger[],
       permissions     = %s::jsonb,
       shipped_shape   = %s::jsonb
 where id = %s::uuid
"""

_RECORD_SQL = "update roles set shipped_shape = %s::jsonb where id = %s::uuid"

_REREAD_SQL = """
select visible_ledgers, permissions, shipped_shape
  from roles where id = %s::uuid
"""


def _ledger_literal(values) -> str:
    """Массив регистров литералом Postgres.

    Литералом, а не списком-параметром: приведение `text[] → ledger[]` держится
    на неявном приведении элементов, а литерал `'{…}'::ledger[]` разбирает сам
    тип — то есть работает независимо от того, каким типом драйвер отправил
    параметр. Значения при этом сверяются со словарём: строка собирается, и
    сверка здесь не формальность.
    """
    unknown = sorted(set(values) - set(ALL_LEDGERS))
    if unknown:
        raise ValueError(f"неизвестные регистры учёта: {', '.join(unknown)}")
    return "{" + ",".join(values) + "}"


def _read(cursor) -> list[tuple]:
    cursor.execute(_READ_SQL, [list(ROLE_SHAPES)])
    return cursor.fetchall()


def _state_of(row) -> RoleState:
    role_id, tenant, code, title, ledgers, permissions, shipped_raw = row
    db = normalize(ledgers, from_jsonb(permissions))
    shipped = snapshot_of(shipped_raw)
    wanted = product_shape(code)
    return RoleState(
        role_id=role_id, tenant=tenant, code=code, title=title,
        state=verdict(db=db, shipped=shipped, wanted=wanted),
        db=db, shipped=shipped, wanted=wanted,
    )


def _verify(cursor, item: RoleState) -> None:
    """Перечитать роль и убедиться, что запись легла.

    Проверяется результат, а не факт выполнения оператора: под политиками
    `update` меняет ноль строк молча, и «миграция прошла» ничего не значит
    (`0110`, issue #44).
    """
    cursor.execute(_REREAD_SQL, [item.role_id])
    row = cursor.fetchone()
    if row is None:
        raise DeliveryRefused(
            f"роль {item.tenant}/{item.code} исчезла из выборки после записи — "
            "запись отвергнута политиками"
        )
    ledgers, permissions, shipped_raw = row
    if normalize(ledgers, from_jsonb(permissions)) != item.wanted:
        raise DeliveryRefused(
            f"форма роли {item.tenant}/{item.code} не доехала: в базе осталось "
            f"{normalize(ledgers, from_jsonb(permissions))}, код ставит {item.wanted}. "
            "Скорее всего запись отрезана политиками — роль базы, которой "
            "выполняется доставка, обязана обходить RLS"
        )
    if snapshot_of(shipped_raw) != item.wanted:
        raise DeliveryRefused(
            f"снимок формы роли {item.tenant}/{item.code} не записан — "
            "следующая доставка примет её за правку человека"
        )


def sync(connection, *, apply: bool = True, adopt: bool = False, say=None) -> Report:
    """Свести форму ролей в базе с кодом и рассказать, что вышло.

    `apply=False` — только вердикт, ни одной записи (это `--check`).
    `adopt=True` — объявить текущее состояние ролей `unknown` поставленным
    продуктом и довезти их. Только по явному требованию человека: угадывать за
    него, правка это или отставание, продукт не вправе.
    """
    talk = say or (lambda *_: None)
    report = Report()

    with connection.cursor() as cursor:
        cursor.execute(_COLUMN_SQL)
        if cursor.fetchone() is None:
            # Схема старше механизма: так бывает при `migrate core <номер>` до
            # `0245`. Молчать нельзя, падать не за что.
            report.ready = False
            talk(
                "Роли: снимка формы в схеме ещё нет (миграция 0245 не накатана) — "
                "доставка пропущена."
            )
            return report

        cursor.execute(_BYPASS_SQL)
        report.bypasses_rls = bool(cursor.fetchone()[0])
        if not report.bypasses_rls:
            # Главная ловушка этого механизма. Без обхода политик «ноль строк»
            # значит «не видно», а не «нечего делать», и отчёт был бы ложью.
            talk(
                "Роли: роль базы не обходит RLS — сколько ролей на самом деле в "
                "таблице, отсюда не видно, и «доставлять нечего» здесь ничего не "
                "доказывает. Доставку выполняет владелец схемы (MIGRATION_DB_USER)."
            )

        cursor.execute(_COUNT_FOREIGN_SQL, [list(ROLE_SHAPES)])
        report.foreign = cursor.fetchone()[0]

        report.states = [_state_of(row) for row in _read(cursor)]

        if not apply:
            return report

        with transaction.atomic(using=connection.alias):
            # Снимок там, где база и код и так совпадают. Видимого изменения
            # нет — это бухгалтерия механизма: после неё роль становится
            # отслеживаемой, и следующая правка кода до неё доедет.
            for item in report.states:
                if item.state == MATCH and item.shipped != item.wanted:
                    cursor.execute(_RECORD_SQL, [_json(item.wanted), item.role_id])
                    report.recorded.append(item)

            if adopt:
                for item in report.by_state(UNKNOWN):
                    cursor.execute(_RECORD_SQL, [_json(item.db), item.role_id])
                    report.adopted.append(item)

            # Перечитываем: после принятия часть ролей стала `behind`.
            report.states = [_state_of(row) for row in _read(cursor)]

            for item in report.by_state(BEHIND):
                # Пишется форма в том порядке, в каком её задал код, а не
                # отсортированная: порядок регистров человек читает в шапке
                # («регистры: официальный, дополнительный, внутренний»), а
                # порядок прав — на экране ролей. Сортировка нужна сравнению,
                # а не базе, и путать эти две вещи нельзя.
                shape = ROLE_SHAPES[item.code]
                cursor.execute(
                    _WRITE_SQL,
                    [
                        _ledger_literal(shape.ledgers),
                        _json(list(shape.permissions)),
                        _json(item.wanted),
                        item.role_id,
                    ],
                )
                _verify(cursor, item)
                report.delivered.append(item)

            report.states = [_state_of(row) for row in _read(cursor)]

    return report


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def describe(report: Report) -> list[str]:
    """Отчёт словами. Одни и те же слова у команды и у `migrate`.

    Две формулировки одного отчёта разъехались бы на первой правке, и человек
    читал бы про одно и то же разное — тот же довод, что у `web/permissions.py`.
    """
    if not report.ready:
        return ["Роли: снимка формы в схеме нет, доставка пропущена."]

    lines: list[str] = []
    for item in report.delivered:
        lines.append(f"довезено: {item.tenant}/{item.code} «{item.title}»")
        lines.extend(f"    {line}" for line in item.diff)
    for item in report.adopted:
        lines.append(f"принято как поставленное продуктом: {item.tenant}/{item.code}")
    for state in (BEHIND, UNKNOWN, EDITED):
        for item in report.by_state(state):
            lines.append(str(item))
            lines.extend(f"    {line}" for line in item.diff)
            if state == UNKNOWN:
                lines.append(
                    "    выход: manage.py roles_sync --adopt (объявить это "
                    "поставленным продуктом) либо правка на экране /roles/"
                )
    if report.recorded:
        lines.append(
            f"снимок формы записан для {len(report.recorded)} рол(и/ей), "
            "совпадавших с кодом: изменений на экранах нет"
        )
    if not lines:
        lines.append(f"роли совпадают с кодом ({len(report.states)} шт.)")
    if report.foreign:
        lines.append(
            f"мимо доставки: {report.foreign} рол(ь/и) не из формы продукта — "
            "их ведёт партнёр"
        )
    return lines
