"""Правила ведения правил расчёта (T090).

Что здесь и почему отдельно от представлений. Экран правил делает ровно три
вещи — показывает действующий пресет, объясняет, откуда взялось каждое
значение, и заводит новую версию. Все три обязаны соблюдать условия, которые
дороже разметки, поэтому они записаны здесь, а не разложены по шаблонам.

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

from core.models import RuleOverride

# Уровни, на которые экран умеет положить правку, от слабого к сильному (T165).
# Те же имена, что в `payroll.presets.LEVELS`, и тот же порядок применения:
# второго порядка здесь не заводится — он разъехался бы с первым молча.
#
# Раньше здесь стоял один `tenant`, и это было записано как «правило первое:
# экран правит слой партнёра». Половина того правила верна и сейчас — тело
# пресета СТРАНЫ с этого экрана не правится (для него отдельный экран и
# отдельное право, см. `web/rules_country.py`). Но вторая половина была не
# правилом, а недоделкой: `rule_overrides` умеет `group` и `employee`, сборка
# пресета их читает, расчёт применяет — и только экран не давал их завести.
# Значит коэффициент ночных часов одной группе или одному человеку задать было
# нечем, при том что в базе для этого всё было готово.
EDITABLE_SCOPES = ("tenant", "group", "employee")

# Уровень по умолчанию: тот, на который ложится правка, если человек уровня не
# выбрал. Партнёр, а не группа — правка «для всех» должна быть той, которую
# получаешь, ничего не выбрав.
DEFAULT_SCOPE = "tenant"

# Уровни, у которых есть объект: правило адресуется не тенанту целиком, а
# конкретной группе или конкретному человеку.
SCOPED_LEVELS = ("group", "employee")

# Разделы правил, которые ниже партнёра НЕ действуют, и почему именно.
#
# Это не вкус и не осторожность: переопределение, которое заведено, видно в
# списке и не участвует в расчёте, хуже отсутствующего — человек думает, что
# задал правило. Поэтому такие уровни отвергаются на входе и со словами, а не
# принимаются и молча игнорируются.
#
# Список короткий и каждая строка называет читателя, по которому проверено:
#
# * `groups` — правило адресуется **кодом группы** (`groups.<код>.…`). На
#   уровне человека такое переопределение перестало бы применяться сразу после
#   перевода его в другую группу: путь остался бы прежним, а код группы стал бы
#   другим, и расчёт молча вернулся бы к мере новой группы. Это ровно тот довод,
#   по которому мера работы человека заведена колонкой условий найма, а не
#   переопределением (D032, миграция `0247`). На уровне группы путь спорил бы
#   сам с собой: `scope_id` называет одну группу, а `<код>` в пути — другую.
#   Свойства человека, которые нужно менять поимённо (схема расчёта, регистр,
#   мера работы), живут в условиях найма и правятся там.
# * `variance` — пороги отчёта расхождений читаются только из общей части
#   (`reports/variance.py`: `thresholds_from((rules.base or {}).get(SECTION))`).
#   Строка на уровне группы до отчёта не доедет вовсе.
TENANT_ONLY_SECTIONS = {
    "groups": lambda: _(
        "Правила группы адресуются её кодом, поэтому ниже партнёра они не "
        "действуют: после перевода человека в другую группу такое "
        "переопределение молча перестало бы применяться. Схема расчёта, регистр "
        "и мера работы конкретного человека задаются в его условиях найма."
    ),
    "variance": lambda: _(
        "Пороги отчёта расхождений читаются один раз на партнёра, поэтому "
        "версия для группы или человека в отчёт не попала бы вовсе."
    ),
}

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

# Как называется уровень в выборе «кому задаём». Отдельно от `LEVEL_TITLES`, и
# это не дубль: там ответ на вопрос «откуда взялось действующее значение»
# («настройка партнёра»), здесь — выбор адресата будущей правки («всему
# партнёру»). Одна формулировка на два разных вопроса читалась бы как ошибка.
SCOPE_TITLES = {
    "tenant": lambda: _("всему партнёру"),
    "group": lambda: _("группе"),
    "employee": lambda: _("одному человеку"),
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


def scope_title(scope_type: str) -> str:
    maker = SCOPE_TITLES.get(scope_type)
    return maker() if maker is not None else scope_type


# --- кому адресована правка ---------------------------------------------------
#
# Уровень приезжает из формы, и это не противоречит осторожности, с которой он
# раньше был зашит константой. Выбор уровня — решение человека («ночные всем или
# только курьерам»), и отдавать его наружу можно ровно потому, что вход
# проверяется: уровень из перечисленных, объект существует у ЭТОГО партнёра, его
# регистр роли виден, а раздел правил на этом уровне вообще действует. Всё, что
# не прошло, отвергается со словами.


@dataclass(frozen=True)
class Target:
    """Адресат правки: уровень и объект, если у уровня объект есть."""

    scope_type: str
    scope_id: Any = None
    title: str = ""

    @property
    def key(self) -> str:
        """Как адресат выглядит в форме и в адресе: `tenant`, `group:<uuid>`."""
        return self.scope_type if self.scope_id is None else f"{self.scope_type}:{self.scope_id}"


TENANT_TARGET = Target(scope_type="tenant")


def section_of(path: str) -> str:
    return path.split(".")[0]


def scope_refusal(path: str, scope_type: str) -> str:
    """Почему этот раздел правил на этом уровне не заводится. Пусто — заводится."""
    if scope_type not in SCOPED_LEVELS:
        return ""
    maker = TENANT_ONLY_SECTIONS.get(section_of(path))
    return maker() if maker is not None else ""


def scopes_for(path: str) -> tuple[str, ...]:
    """Уровни, на которые это правило можно положить."""
    return tuple(level for level in EDITABLE_SCOPES if not scope_refusal(path, level))


def group_targets(who) -> list[Target]:
    """Группы партнёра, регистр которых роли виден (D023).

    Фильтр по регистру тот же, что в справочнике групп, и по тому же доводу:
    название группы в списке уровней — это сообщение о том, что группа
    существует, ничем не отличающееся от её строки в справочнике. Дать роли
    выбрать группу, которой она не видит, значило бы рассказать о ней списком.
    """
    from core.models import EmployeeGroup

    return [
        Target(scope_type="group", scope_id=row.id, title=row.title or row.code)
        for row in EmployeeGroup.objects.filter(
            tenant_id=who.tenant_id, ledger__in=list(who.visible_ledgers)
        ).order_by("title", "code")
    ]


def employee_targets(who, on_date: date) -> list[Target]:
    """Люди партнёра, регистр которых роли виден на эту дату.

    Регистр человека берётся у действующей версии условий найма — своей, а не
    унаследованной от группы, если своя задана: тем же правилом, каким его
    считает справочник и расчёт.

    Список не листается, как и в справочнике сотрудников, и по той же причине,
    что записана там: тридцать человек листаются глазами, три тысячи — нет.
    Долг общий у двух экранов, и решаться он должен один раз для обоих, а не
    отдельным способом здесь.
    """
    from core.models import Employee

    from . import directory

    people = list(Employee.objects.filter(tenant_id=who.tenant_id)
                  .order_by("last_name", "first_name"))
    seen = set(who.visible_ledgers)
    rows = []
    for person in people:
        term = directory.term_at(who.tenant_id, person.id, on_date)
        # Человека без условий найма на эту дату показываем: регистра он не
        # называет, значит и скрывать нечего, а правило ему завести можно —
        # подействует, когда условия появятся.
        if term is not None and (term.ledger or term.group.ledger) not in seen:
            continue
        name = f"{person.last_name} {person.first_name}".strip() or person.external_id
        rows.append(Target(scope_type="employee", scope_id=person.id, title=name))
    return rows


def targets_for(who, path: str, on_date: date, *, chosen: str = "") -> list[dict]:
    """Все адресаты правки этого правила — готовым списком для `select.html`.

    Одним списком, а не двумя полями: уровень и объект — это один ответ на один
    вопрос «кому», и разбивать его на «выберите уровень» плюс «выберите объект»
    значило бы дать собрать пару, которой нет (уровень «группе» и пустой
    объект). Пустой объект здесь невозможен по устройству.

    Уровень назван в самой подписи («группе · Курьеры»), а не отдельной
    подгруппой списка: подгруппы у общего компонента выбора нет, а заводить её
    ради одного экрана значило бы править то, чем пользуются десять других.
    """
    allowed = scopes_for(path)
    rows: list[dict] = []
    if "tenant" in allowed:
        rows.append({"code": TENANT_TARGET.key, "title": scope_title("tenant")})
    known: list[Target] = []
    if "group" in allowed:
        known += group_targets(who)
    if "employee" in allowed:
        known += employee_targets(who, on_date)
    rows += [
        {"code": t.key, "title": f"{scope_title(t.scope_type)} · {t.title}"} for t in known
    ]
    picked = chosen or DEFAULT_SCOPE
    for row in rows:
        row["selected"] = row["code"] == picked
    return rows


def target_from(who, raw: str, *, path: str, on_date: date) -> Target:
    """Адресат правки из значения формы. Не разобралось — отказ, а не партнёр.

    Молчаливая подстановка партнёра была бы худшим из исходов: человек выбрал
    группу, а правило легло на всех, и узнал бы он об этом по деньгам.

    Чужой объект, скрытый роли и несуществующий отвечают **одинаково** (D023):
    по разнице в ответах можно было бы перебрать, какие группы у партнёра есть.
    """
    raw = (raw or "").strip() or DEFAULT_SCOPE
    scope_type, _sep, scope_key = raw.partition(":")
    if scope_type not in EDITABLE_SCOPES:
        raise RuleInputRefused(
            _("Уровень «%(level)s» не из тех, на которые кладутся правила.")
            % {"level": raw}
        )
    refused = scope_refusal(path, scope_type)
    if refused:
        raise RuleInputRefused(refused)
    if scope_type == "tenant":
        if scope_key:
            # «Партнёру целиком» объекта не имеет: пришедший объект означает, что
            # форму собрал кто-то другой, и принять её значило бы записать
            # строку, которую сборка пресета не знает, как применить.
            raise RuleInputRefused(_("У уровня «всему партнёру» объекта нет."))
        return TENANT_TARGET

    known = group_targets(who) if scope_type == "group" else employee_targets(who, on_date)
    for target in known:
        if str(target.scope_id) == scope_key:
            return target
    raise RuleInputRefused(
        _("Не нашлось того, кому адресована правка. Выберите из списка.")
    )


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


# --- история и правка ---------------------------------------------------------


def versions(tenant_id, path: str, *, target: Target | None = None):
    """Версии переопределений по этому пути — от старой к новой.

    Без адресата — **все** уровни сразу, и это не удобство, а то же требование,
    что и везде в этом продукте: заведённое правило обязано быть видно. Пока
    история показывала только слой партнёра, версия, заведённая группе, не
    появлялась ни на одном экране — правило действовало, а прочитать о нём было
    негде.
    """
    rows = RuleOverride.objects.filter(tenant_id=tenant_id, path=path)
    if target is not None:
        rows = rows.filter(scope_type=target.scope_type, scope_id=target.scope_id)
    return rows.order_by("valid_from", "scope_type")


def titles_of_targets(who, on_date: date) -> dict:
    """Имя объекта по его id — для истории версий и для списка правил.

    Спрашивается один раз на страницу, а не по строке: групп и людей у партнёра
    десятки, а версий правила бывает больше, и запрос на каждую строку был бы
    тем самым N+1, из-за которого экраны потом «необъяснимо медленные».
    """
    found = {}
    for target in group_targets(who) + employee_targets(who, on_date):
        found[(target.scope_type, target.scope_id)] = target.title
    return found


def visible_only(rows, visible: dict):
    """Строки переопределений, объект которых роли виден (D023).

    Уровень партнёра проходит всегда — у него объекта нет. Строка группы или
    человека, которых роль не видит, не показывается **и не считается**: иначе
    экран сообщал бы «у одной группы своё значение», то есть ровно то, что
    скрывается. Оговорки «показано не всё» здесь тоже нет — она называла бы
    существование скрытого, тем же доводом, что в справочнике групп.
    """
    return [
        row for row in rows
        if row.scope_type not in SCOPED_LEVELS
        or (row.scope_type, row.scope_id) in visible
    ]


def override_counts(tenant_id, on_date: date, visible: dict) -> dict:
    """Сколько переопределений ниже партнёра действует на дату — по путям.

    Зачем это списку правил. Список показывает общую часть — страну плюс
    партнёра, — потому что именно её видят все. Версия, заведённая одной группе,
    в общую часть не входит, и без этой пометки её не было бы видно **ни на
    одном** экране: правило действует, деньги считаются по нему, а найти его
    нельзя. Молчаливое переопределение хуже отсутствующего.

    Считается на дату теми же границами, что у сборки пресета:
    `valid_from <= дата < valid_to`.
    """
    rows = (
        RuleOverride.objects.filter(tenant_id=tenant_id, valid_from__lte=on_date)
        .exclude(valid_to__lte=on_date)
        .filter(scope_type__in=SCOPED_LEVELS)
    )
    counts: dict[str, dict[str, int]] = {}
    for row in visible_only(rows, visible):
        at_path = counts.setdefault(row.path, {})
        at_path[row.scope_type] = at_path.get(row.scope_type, 0) + 1
    return counts


def counts_words(at_path: dict) -> str:
    """«2 группы · 1 человек» — словами, потому что число само ничего не значит."""
    parts = []
    groups = at_path.get("group", 0)
    people = at_path.get("employee", 0)
    if groups:
        parts.append(
            _("%(count)s группе") % {"count": groups} if groups == 1
            else _("%(count)s группам") % {"count": groups}
        )
    if people:
        parts.append(
            _("%(count)s человеку") % {"count": people} if people == 1
            else _("%(count)s людям") % {"count": people}
        )
    return " · ".join(parts)


@dataclass(frozen=True)
class RuleChange:
    changed: bool
    previous: RuleOverride | None


@transaction.atomic
def save_override(tenant_id, path: str, value: Any, *, valid_from: date,
                  target: Target = TENANT_TARGET,
                  actor_id=None, effective: Any = None) -> RuleChange:
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

    Закрывается предыдущая версия **того же уровня и того же объекта**: уровни
    складываются, а не спорят, и версия партнёра не должна закрываться правкой
    группы. Так же читает и ограничение непересечения в базе — оно включает
    `scope_type` и `scope_id` в ключ.
    """
    if effective is not None and value == effective:
        return RuleChange(changed=False, previous=None)

    current = (
        RuleOverride.objects.filter(
            tenant_id=tenant_id, scope_type=target.scope_type, scope_id=target.scope_id,
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
        scope_type=target.scope_type,
        scope_id=target.scope_id,
        path=path,
        value=value,
        valid_from=valid_from,
        valid_to=current.valid_to if current is not None else None,
        created_by=actor_id,
    )
    return RuleChange(changed=True, previous=current)
