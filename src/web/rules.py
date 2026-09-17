"""Правила ведения правил расчёта (T090).

Что здесь и почему отдельно от представлений. Экран правил делает ровно три
вещи — показывает действующий пресет, объясняет, откуда взялось каждое
значение, и заводит новую версию. Все три обязаны соблюдать условия, которые
дороже разметки, поэтому они записаны здесь, а не разложены по шаблонам.

Ответ на вопрос «кого правка заденет» живёт рядом, в `web/rule_targets.py`: у
него свои данные (группы и люди партнёра), своя видимость по регистрам и свои
отказы. Зависимость односторонняя — адресаты знают про правило, правило про
адресатов не знает и принимает уровень простыми значениями.

**Правило первое: правка ложится на выбранный уровень, а тело пресета страны
этим экраном не правится.** Уровней у сборки пресета четыре — страна, партнёр,
группа, человек, — и правка кладётся строкой в `rule_overrides` на любой из
трёх нижних (T165). Раньше здесь был зашит один `tenant`, и это записывалось
как правило; правилом оно не было — переопределение группы и человека база
умела, расчёт применял, а завести их было нечем.

Тело `rule_presets` при этом действительно остаётся в стороне: у него нет
`tenant_id`, он лежит в `SHARED_TABLES` (`0004_rls`), и два партнёра одной
страны читают одну и ту же строку. Правка тела с экрана одного партнёра
поменяла бы расчёт другому — молча и задним числом. Поэтому страна правится
отдельным экраном и отдельным правом (`web/rules_country.py`), а здесь —
только то, что принадлежит партнёру. Продуктовый принцип тот же: страна
поставляется готовым набором, партнёр меняет только отличия.

**Уровни складываются, а не заменяют друг друга.** Порядок один на продукт —
`payroll.presets.LEVELS`, и второго здесь не появляется: два порядка наложения
слоёв разъехались бы молча, и экран показывал бы не то, что посчитает расчёт.
Поэтому «сейчас действует» для группы и для человека собирается тем же кодом,
каким его собирает расчёт (`core.rules.RuleSet.preset`).

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

from core.models import RuleFrameAttempt, RuleOverride
from payroll import frames

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

# Разделы верхнего уровня, которых на экране правил нет и которые с него не
# правятся: имя пресета плюс рамка страны (T192).
#
# Рамка — не правило, а условие, при котором правило меняют, и переопределять её
# нельзя ни на одном уровне: рамка, которую двигает тот, кого она держит, рамкой
# быть перестаёт. Отказ здесь не нужен и был бы враньём про существование такого
# правила — раздела просто нет ни в списке, ни по адресу.
NOT_RULES = IDENTITY + (frames.FRAMES_KEY,)

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

# Регистр учёта — тоже выбор из списка, но список задаёт не страна, а система
# (`core.roles.ALL_LEDGERS`), и отбирается он по тому, что видит роль (D023).
# Поэтому отдельным ключом, а не строкой в `CHOICES_FROM`: там значения —
# разделы пресета, а здесь справочник продукта.
#
# Почему это вообще проверяется. `allowances.<код>.ledger` правится на любом
# уровне, а регистр из правила доезжает до строки расчёта. Свободный ввод давал
# два исхода, и оба плохие: опечатка («oficial») роняла бы расчёт периода на
# середине — тип-перечисление в базе такого значения не примет, — а осмысленный
# чужой регистр («internal» у роли, которая его не видит) заводил бы строки,
# которых сама эта роль потом не увидит.
LEDGER_KEY = "ledger"


class RuleInputRefused(Exception):
    """Введено не то. Отдельно от отказа по состоянию данных, как и в справочниках."""

    http_status = 400

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class RuleFrameRefused(RuleInputRefused):
    """Значение мягче правил страны (T192).

    Наследник, а не сосед: для того, кто смотрит на код ответа, «набрано не то»
    и «так менять нельзя» — одно событие, форма не принята. Отдельным классом —
    чтобы отличать его в проверках и не ловить рамку широким `except`.

    Отказ несёт с собой **несделанную запись** в журнал попыток. Записывает её
    не тот, кто отказал, а тот, кто отказ поймал, и это не разделение ради
    красоты: запись идёт внутри `saving()`, то есть внутри точки сохранения,
    и отказ, брошенный оттуда, унёс бы её с собой откатом. Ловят же отказ уже
    снаружи — см. `remember_attempt`.
    """

    def __init__(self, message: str, attempt: dict | None = None):
        super().__init__(message)
        self.attempt = attempt or {}


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


def parse(raw: str, current: Any, label: str, *, allowed: tuple = ()) -> Any:
    """Введённое, приведённое к типу действующего значения. Не приводится — отказ.

    `allowed` — то, что перечисляют сами правила страны (мера работы, схема
    расчёта). Проверка обязательна и живёт здесь, а не на форме: список на
    форме — это удобство, а не запрет, и запрос, собранный мимо неё, приносил бы
    в jsonb любую строку. Дальше её увидел бы расчёт — и либо отказал бы на
    середине периода, либо посчитал мимо правила.

    Это ровно тот дефект, который вчера нашли у меры оплаты: у схемы расчёта
    проверка на подмену запроса была, а у меры — нет, и правильное поведение
    держалось ни на чём. Здесь полей больше, поэтому проверка одна на все.
    """
    value = _coerce(raw, current, label)
    if allowed and value not in allowed:
        raise RuleInputRefused(
            _("«%(label)s»: значения «%(value)s» нет в правилах страны. "
              "Допустимо: %(allowed)s.")
            % {"label": label, "value": show(value), "allowed": ", ".join(map(str, allowed))}
        )
    return value


def _coerce(raw: str, current: Any, label: str) -> Any:
    """Приведение введённого к типу действующего значения, без проверки списком."""
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


def choices_for(preset: dict, path: str, *, who=None) -> list[tuple[str, str]]:
    """Допустимые значения пути. Пусто — значение набирается свободно.

    Два источника, и они разные по природе:

    * **тело правил** — мера работы, схема расчёта. Берутся из пресета, а не из
      константы в коде: новая мера или новая схема появляется у страны в YAML, и
      экран обязан предложить её, не дожидаясь правки интерфейса;
    * **справочник продукта** — регистр учёта. Его состав задаёт не страна, а
      сама система (`core.roles.ALL_LEDGERS`), и список отбирается по тому, что
      видит роль (D023): дать роли без внутреннего регистра поставить надбавку
      в него значило бы и рассказать о нём, и завести расчёт, строки которого
      этой же роли потом не видны.

    Без `who` регистры не перечисляются вовсе — не «все», а ни один: список
    «допустимого вообще» здесь превратился бы в разрешение, выданное по забытому
    параметру.
    """
    key = path.rsplit(".", 1)[-1]
    if key == LEDGER_KEY:
        return ledger_choices(who)
    section = CHOICES_FROM.get(key)
    if section is None:
        return []
    body = preset.get(section)
    if not isinstance(body, dict):
        return []
    return [
        (code, (item or {}).get("title") or code if isinstance(item, dict) else code)
        for code, item in body.items()
    ]


def ledger_choices(who) -> list[tuple[str, str]]:
    """Регистры учёта, которые видит роль, — в каноническом порядке.

    Порядок из `core.roles.ALL_LEDGERS`, а не из членства: набор роли — это
    множество, и порядок в нём случайный. Названия берутся у `web.format` —
    того же места, откуда их берут ведомость и справочники.
    """
    if who is None:
        return []
    from core.roles import ALL_LEDGERS

    from .format import ledger_title

    seen = set(who.visible_ledgers or [])
    return [(code, ledger_title(code)) for code in ALL_LEDGERS if code in seen]


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
            if not is_visible(path, hidden) or path.split(".")[0] in NOT_RULES:
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


def matching(rows: list[Leaf], query: str) -> list[Leaf]:
    """Правила, чей код содержит искомое. Пустой запрос — все.

    Ищется по **коду** правила, а не по подписи, и это не экономия. Код — то,
    чем правило адресуется расчёту (`hour_types.night.pay_percent`), он один на
    все языки и он же написан в строке таблицы. Поиск по подписи нашёл бы
    «Ночные» по-русски и не нашёл бы то же правило на сербском экране — то есть
    работал бы по-разному в зависимости от языка страницы.
    """
    wanted = (query or "").strip().lower()
    if not wanted:
        return rows
    return [leaf for leaf in rows if wanted in leaf.path.lower()]


def sections(preset, *, hidden: tuple[str, ...] = (), query: str = "") -> list[dict]:
    """Правила, разложенные по разделам: так их и читают.

    Порядок разделов задан здесь (`SECTION_TITLES`), а не приходит из данных, по
    той же причине: из базы он не приходит вовсе. Раздел, которого этот список
    не знает, показывается **после** известных и своим ключом — молча пропасть
    он не должен, иначе новая страна принесла бы правила, которых нет на экране.
    """
    known = list(SECTION_TITLES)
    grouped: dict[str, list[Leaf]] = {}
    for leaf in matching(leaves(preset, hidden=hidden), query):
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


# --- рамка страны -------------------------------------------------------------
#
# Правило шестое, и оно про то, чего продукт обещал, но не делал (T192, issue
# #182). Эталон модуля 17: «страна задаёт значение и рамку, юрлицо доопределяет
# внутри рамки… юрлицо может стать строже рамки, но никогда — мягче». У нас была
# только первая половина: значение страны есть, рамки нет, и процент больничного
# опускался с 0,65 до 0,10 обычной формой этого экрана.
#
# Сама рамка — данные страны (`payroll/frames.py`, раздел `frames` в теле
# пресета), а здесь три вещи, которые к ней добавляет продукт: слова отказа,
# запись отвергнутой попытки и подпись рамки на экране.
#
# Почему проверка стоит в `save_override`, а не в представлении. Дорога к записи
# переопределения одна, но входов на неё два: экран правил и форма группы в
# справочниках (`directory_views._save_measure`, там правится
# `groups.<код>.work_measure`). Проверка, поставленная на экране, второго входа
# не закрыла бы, а узнали бы мы об этом на правиле, у которого появится рамка.
# Тот же довод, по которому проверка списка допустимого переехала в `parse()`.

# Кто решает значение — словами эталона (колонка «Кто меняет»). «Сеть» из
# эталона у нас зовётся партнёром: своего слова «сеть» продукт не знает, и
# заводить его ради одной колонки значило бы завести второе имя тому же.
FRAME_OWNERS = {
    "lock": lambda: _("Закон"),
    "frame": lambda: _("Внутри рамки"),
    "free": lambda: _("Решает партнёр"),
}


def frame_at(preset: dict, path: str):
    """Рамка правила из тела страны. Нет рамки — None, и это разрешение."""
    return frames.frame_for(preset, path)


def frame_owner(preset: dict, path: str) -> str:
    """Кто решает значение этого правила — одним словом, для списка правил.

    Спрашивать можно и у собранного пресета: режим рамки переопределить нельзя
    ни на одном уровне, поэтому в собранном он тот же, что в теле страны. С
    ГРАНИЦЕЙ так нельзя — см. `_frame_block` в представлениях.
    """
    frame = frame_at(preset, path)
    return FRAME_OWNERS["free" if frame is None else frame.mode]()


def frame_mode(preset: dict, path: str) -> str:
    """Режим рамки одним словом: `lock`, `frame` или `free`.

    Отдельно от `frame_owner`, потому что у них разные читатели: слово читает
    человек, режим — разметка, которая этим словом красит метку. Собирать класс
    из переведённого слова нельзя: на английском экране он стал бы другим.
    """
    frame = frame_at(preset, path)
    return "free" if frame is None else frame.mode


def frame_help(frame, country_value: Any) -> str:
    """Что рамка разрешает — словами, ДО правки.

    Отказ после набранного значения — худший способ узнать о рамке: человек уже
    решил, что ставит, и переспорить его текстом отказа нельзя, можно только
    отнять работу. Поэтому те же границы стоят подсказкой под полем.
    """
    if frame is None or frame.mode == "free":
        return ""
    if frame.mode == "lock":
        return _(
            "Значение задано законом и на уровне партнёра не переопределяется. "
            "Источник: %(source)s."
        ) % {"source": frame.source}
    return _("Рамка страны: %(limits)s. Источник: %(source)s.") % {
        "limits": _frame_limits(frame, country_value),
        "source": frame.source,
    }


def _frame_limits(frame, country_value: Any) -> str:
    """Границы рамки одной фразой: «не ниже 1,26», «от 4 до 16».

    Стороны названы теми же словами, что и в отказе, и это одни и те же строки
    перевода: подсказка «не ниже 1,26» и отказ «Рамка — не ниже 1,26» обязаны
    совпасть буквально, иначе человек ищет разницу там, где её нет.
    """
    low = frame.bound("min", country_value)
    high = frame.bound("max", country_value)
    if low is not None and high is not None:
        return _("от %(low)s до %(high)s") % {"low": show(low), "high": show(high)}
    return _side(low if low is not None else high, "below" if low is not None else "above")


def _side(bound: Any, kind: str) -> str:
    """Одна сторона рамки словами."""
    if kind == "below":
        return _("не ниже %(bound)s") % {"bound": show(bound)}
    return _("не выше %(bound)s") % {"bound": show(bound)}


def frame_breach_now(frame, value: Any, country_value: Any) -> str:
    """Слова о том, что ДЕЙСТВУЮЩЕЕ значение уже мягче рамки. Нет — пусто.

    Рамка проверяется при записи, и заведённое вчера значение было тогда верным.
    Верным оно быть перестаёт, когда страна поднимает минимум: индексация
    минималки двигает рамку, а переопределение партнёра остаётся прежним — и
    расчёт продолжает считать по нему, ничего не сказав. Молчать об этом хуже
    всего: продукт знает обе величины и видит их рядом на одной странице.

    Чинить это правкой чужого значения нельзя (D020: закрытые месяцы считаны по
    нему), поэтому продукт говорит, а не исправляет.

    Сравнивается только значение, ОТЛИЧАЮЩЕЕСЯ от страны. Иначе запертое
    правило, которого никто не трогал, каждый раз объявлялось бы нарушением
    самого себя: у `lock` любое переопределение — нарушение, а отсутствие
    переопределения нарушением не является.
    """
    if frame is None or country_value is None or value == country_value:
        return ""
    breach = frames.check(frame, value, country_value)
    if breach is None:
        return ""
    if breach.kind == "locked":
        return _(
            "Сейчас действует %(value)s, хотя правило задано законом "
            "(%(source)s). Переопределение заведено раньше рамки, и расчёт "
            "берёт именно его — вернуть значение страны можно новой версией."
        ) % {"value": show(value), "source": breach.source}
    return _(
        "Сейчас действует %(value)s — мягче рамки страны (%(limit)s, "
        "%(source)s). Значение заведено раньше, чем рамка стала такой, и расчёт "
        "берёт именно его. Заведите новую версию внутри рамки."
    ) % {"value": show(value), "limit": _side(breach.bound, breach.kind),
         "source": breach.source}


def frame_refusal(breach, frame, path: str) -> str:
    """Отказ словами: чем не подошло значение и что поставить можно.

    Отказ обязан советовать выполнимое — тот же приём, что у
    `refuse_if_unversioned_touches_closed_month`. «Мягче страны» без границы и
    без стороны оставляет человека гадать, куда двигаться.
    """
    if breach.kind == "locked":
        return _(
            "«%(path)s» задано законом и на уровне партнёра не меняется. "
            "Источник: %(source)s. Поменять его можно только в правилах страны, "
            "а их ведёт администратор платформы."
        ) % {"path": path, "source": breach.source}
    if breach.kind == "below":
        advice = _("Поставить можно столько же или больше.")
    else:
        advice = _("Поставить можно столько же или меньше.")
    limit = _side(breach.bound, breach.kind)
    return _(
        "«%(path)s»: значение мягче правил страны. Рамка — %(limit)s "
        "(%(source)s). %(advice)s Попытка записана в журнал правила."
    ) % {"path": path, "limit": limit, "source": breach.source, "advice": advice}


def country_body_at(tenant_id, on_date: date) -> dict | None:
    """Тело правил страны партнёра, действовавшее на эту дату.

    Берётся версия на дату ПРАВКИ, а не на сегодня: рамка версионируется вместе
    со значением (индексация минималки двигает и то и другое), и версия, которую
    заводят задним числом, обязана мериться рамкой того времени.
    """
    from . import directory, rules_country

    version = rules_country.in_force_at(directory.country_of(tenant_id), on_date)
    return None if version is None else version.body


def refuse_if_softer(tenant_id, path: str, value: Any, *, valid_from: date,
                     scope_type: str, scope_id, actor_id, actor_name: str) -> None:
    """Не пустить значение мягче страны — и оставить след отказа.

    Запись попытки идёт ДО исключения и вне транзакции записи версии: у
    `_write_version` свой сейвпойнт, и отказ, брошенный изнутри, откатил бы его
    вместе с записью — журнал остался бы пустым ровно в тот момент, ради
    которого он заведён.

    Рамки нет — молчим. Это не «проверка не сработала», а прямое умолчание
    продукта: правило, у которого страна рамку не объявила, партнёр ставит как
    считает нужным.
    """
    body = country_body_at(tenant_id, valid_from)
    if body is None:
        return
    frame = frames.frame_for(body, path)
    if frame is None:
        return
    try:
        country_value = value_at(body, path)
    except KeyError:
        country_value = None
    breach = frames.check(frame, value, country_value)
    if breach is None:
        return

    raise RuleFrameRefused(frame_refusal(breach, frame, path), attempt={
        "tenant_id": tenant_id,
        "path": path,
        "scope_type": scope_type,
        "scope_id": scope_id,
        "wanted": value,
        "country_value": country_value,
        "frame": {"mode": frame.mode, "min": frame.minimum, "max": frame.maximum,
                  "source": frame.source},
        "valid_from": valid_from,
        "created_by": actor_id,
        "created_by_name": actor_name,
    })


def remember_attempt(refusal: Exception) -> None:
    """Записать отвергнутую попытку — из обработчика отказа, а не из проверки.

    Зовётся там, где отказ пойман: к этому месту точка сохранения `saving()`
    уже откачена, а транзакция запроса ещё жива, поэтому строка журнала
    доживает до конца запроса. Записанная на месте отказа, она уехала бы вместе
    с откатом — именно так первый заход этой задачи и вышел с пустым журналом
    при верных словах на экране.

    Обычный отказ ввода проходит мимо молча: у него записывать нечего, и
    развилка здесь дешевле двух почти одинаковых `except` у каждой формы.
    """
    attempt = getattr(refusal, "attempt", None)
    if attempt:
        RuleFrameAttempt.objects.create(**attempt)


def frame_attempts(tenant_id, path: str):
    """Отвергнутые попытки по этому правилу — от свежей к старой.

    Свежая первой, в отличие от версий правила: версии читают как историю
    значения слева направо, а попытки — как «что тут происходит сейчас».
    """
    return (
        RuleFrameAttempt.objects.filter(tenant_id=tenant_id, path=path)
        .order_by("-created_at")
    )


# --- история и правка ---------------------------------------------------------


def versions(tenant_id, path: str, *, scope_type: str | None = None, scope_id=None):
    """Версии переопределений по этому пути — от старой к новой.

    Без адресата — **все** уровни сразу, и это не удобство, а то же требование,
    что и везде в этом продукте: заведённое правило обязано быть видно. Пока
    история показывала только слой партнёра, версия, заведённая группе, не
    появлялась ни на одном экране — правило действовало, а прочитать о нём было
    негде.
    """
    rows = RuleOverride.objects.filter(tenant_id=tenant_id, path=path)
    if scope_type is not None:
        rows = rows.filter(scope_type=scope_type, scope_id=scope_id)
    return rows.order_by("valid_from", "scope_type")


@dataclass(frozen=True)
class RuleChange:
    changed: bool
    previous: RuleOverride | None


def save_override(tenant_id, path: str, value: Any, *, valid_from: date,
                  scope_type: str = "tenant", scope_id=None,
                  actor_id=None, actor_name: str = "",
                  effective: Any = None) -> RuleChange:
    """Завести новую версию правила с указанной даты на указанном уровне.

    `effective` — значение, действующее на эту дату сейчас **для этого же
    адресата** (собранное со всеми слоями до него включительно). Совпало —
    версия не заводится вовсе: иначе история обрастала бы строками «то же самое
    с другой даты», и настоящая смена правила терялась бы среди них. Тот же
    довод и то же поведение, что у условий найма.

    Сравнение обязано быть именно по адресату, а не по общей части. Пример, на
    котором это видно: партнёру ночные 1,26, группе курьеров задают 1,40.
    Сравнение с общей частью сказало бы «отличается» и завело бы версию — верно.
    А обратный случай: группе уже задано 1,40, человек вводит 1,40 ей же —
    сравнение с общей частью (1,26) сказало бы «отличается» и завело вторую
    версию того же значения. Поэтому `effective` считается для адресата.

    Уровень и объект приходят простыми значениями, а не объектом адресата
    (`web/rule_targets.Target`): иначе модуль правила зависел бы от модуля
    адресатов, а тот — от него, и зависимость перестала бы быть односторонней.
    Собирает пару вызывающий, он же и проверяет её.

    Закрывается предыдущая версия **того же уровня и того же объекта**: уровни
    складываются, а не спорят, и версия партнёра не должна закрываться правкой
    группы. Так же читает и ограничение непересечения в базе — оно включает
    `scope_type` и `scope_id` в ключ.

    **Рамка страны проверяется здесь, а не у вызывающих** (T192): входов на эту
    дорогу два — экран правил и форма группы в справочниках, — и проверка,
    поставленная на одном из них, второй оставила бы открытым.

    Порядок важен: сначала «ничего не изменилось», потом рамка. Иначе тот, кто
    сохранил форму запертого правила, ничего в ней не поменяв, получал бы отказ
    за бездействие, а в журнал попадала бы попытка, которой не было.
    """
    if effective is not None and value == effective:
        return RuleChange(changed=False, previous=None)
    refuse_if_softer(
        tenant_id, path, value, valid_from=valid_from, scope_type=scope_type,
        scope_id=scope_id, actor_id=actor_id, actor_name=actor_name,
    )
    return _write_version(
        tenant_id, path, value, valid_from=valid_from, scope_type=scope_type,
        scope_id=scope_id, actor_id=actor_id,
    )


@transaction.atomic
def _write_version(tenant_id, path: str, value: Any, *, valid_from: date,
                   scope_type: str, scope_id, actor_id) -> RuleChange:
    """Закрыть прежнюю версию и завести новую — одной транзакцией.

    Отделено от `save_override` ровно ради этой транзакции. Отвергнутая рамкой
    правка не должна оставлять за собой закрытую версию без пришедшей ей на
    смену, а записанная попытка не должна уезжать вместе с откатом сейвпойнта —
    см. `refuse_if_softer`.
    """
    current = (
        RuleOverride.objects.filter(
            tenant_id=tenant_id, scope_type=scope_type, scope_id=scope_id,
            path=path, valid_from__lte=valid_from,
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
        scope_type=scope_type,
        scope_id=scope_id,
        path=path,
        value=value,
        valid_from=valid_from,
        valid_to=current.valid_to if current is not None else None,
        created_by=actor_id,
    )
    return RuleChange(changed=True, previous=current)
