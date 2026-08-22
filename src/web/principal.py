"""
Кто сейчас работает: тенант, точки, видимые регистры.

Это и есть контракт блока `auth` наружу (`get_current_principal`). Всё, что
знают представления о правах, приходит отсюда — чтобы при замене входа менялся
источник, а не потребители.

Данные читаются обычной выборкой **под уже выставленным контекстом**: то, что
человек не видит по политикам базы, не попадёт ни сюда, ни в интерфейс. Роль и
членство — такие же строки под RLS, как и всё остальное.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from core.models import Membership, Unit

# Канонический порядок регистров берётся из формы ролей, а не пишется здесь
# вторым списком: два списка одного и того же расходятся молча — ровно тем
# способом, из-за которого `core/roles.py` и появился.
from core.roles import ALL_LEDGERS

from .format import CodedTitle, ledger_title
from .i18n import role_title

# Кладётся на запрос: страница спрашивает «кто вошёл» и в шапке, и в
# представлении, а лишний запрос к базе на каждый вопрос не нужен.
CACHE_ATTR = "_principal"


@dataclass(frozen=True)
class Principal:
    user_id: UUID
    tenant_id: UUID | None = None
    unit_ids: list[UUID] = field(default_factory=list)
    visible_ledgers: list[str] = field(default_factory=list)
    permissions: list[str] = field(default_factory=list)
    # Только для показа в шапке; в контракте блока auth этих полей нет.
    role_title: str = ""
    tenant_title: str = ""
    display_name: str = ""
    # Коды точек, которыми ограничен человек. Пусто — ограничения нет, показывать
    # в шапке нечего: объяснять надо срезанные данные, а не полные.
    units_title: str = ""


def get_current_principal(request) -> Principal | None:
    """Текущий пользователь или None, если никто не вошёл."""
    cached = getattr(request, CACHE_ATTR, False)
    if cached is not False:
        return cached

    who = _load(request)
    setattr(request, CACHE_ATTR, who)
    return who


def principal_for_user(user_id) -> Principal | None:
    """Кто это, без HTTP-запроса — для кода, у которого запроса нет.

    Нужно фоновой задаче: права и видимые регистры — свойство человека, и
    перечитывать их надо **в момент исполнения**, а не брать из очереди. Между
    нажатием кнопки и работой задачи роль могли поменять, и расчёт обязан идти
    по правам, действующим сейчас.

    Читается под уже выставленным контекстом того же человека: свою учётку он
    видит, чужую — нет, и подставить сюда чужой uuid бесполезно.
    """
    from core.models import User

    user = User.objects.filter(pk=user_id).first()
    if user is None:
        return None
    return _from_membership(user)


def _load(request) -> Principal | None:
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return None
    return _from_membership(user)


def _from_membership(user) -> Principal:
    """Кто это — по ВСЕМ его членствам, а не по первому попавшемуся (T170, D047).

    Ролей у человека может быть несколько, и права складываются. Так у партнёра
    и устроено: бухгалтер у части партнёров ведёт весь проект и тогда по сути
    ещё и администратор, а разделение обязанностей там, где партнёр его держит,
    ломать нельзя — значит роли даются набором, а не выдаются кому-то оптом.

    Складывать обязана база, и она это уже делает: `app_has_permission`,
    `app_visible_ledgers`, `app_unit_ids` написаны множеством по всем членствам.
    Здесь то же самое повторяется для показа — **по тем же правилам**, иначе
    экран обещал бы не то, что позволит база:

    * права и регистры — объединение;
    * точки — объединение, но членство **без** списка точек означает «все
      точки» и перекрывает ограничение остальных (так же считает `app_unit_ids`).
      Пересечение было бы «запрещать меньшее тому, кому разрешено большее»
      (D033) — тупик в работе, а не разграничение.

    Тенант берётся у первого членства: разграничение по партнёрам делает база, а
    человека сразу у двух партнёров в продукте сегодня нет. Появится — это будет
    переключатель партнёра, а не молчаливый выбор здесь.
    """
    display_name = user.full_name or user.username
    memberships = list(
        Membership.objects.select_related("role", "tenant")
        .filter(user_id=user.pk)
        # Порядок обязан быть определённым: без него база вправе отдать строки
        # как угодно, и роль в шапке менялась бы от запроса к запросу.
        .order_by("created_at", "id")
    )
    if not memberships:
        # Учётка есть, а членства нет: человека ещё не завели ни к одному
        # партнёру. Данных он не увидит — политики без членства пусты, — но и
        # выкидывать его со страницы незачем: пароль он поменять может.
        return Principal(user_id=user.pk, display_name=display_name)

    tenant_id = memberships[0].tenant_id
    mine = [m for m in memberships if m.tenant_id == tenant_id]

    # Пустой список точек у любого членства означает «все точки» и перекрывает
    # ограничение остальных — ровно так же, как это делает `app_unit_ids`.
    unlimited = any(not m.unit_ids for m in mine)
    unit_ids = [] if unlimited else _unique(
        unit for m in mine for unit in (m.unit_ids or [])
    )
    return Principal(
        user_id=user.pk,
        tenant_id=tenant_id,
        unit_ids=unit_ids,
        # Порядок регистров канонический, а не тот, в котором роли легли в базу:
        # он уходит в шапку строкой, и «дополнительный, официальный» у одного
        # человека против «официальный, дополнительный» у другого читалось бы
        # как разные наборы.
        visible_ledgers=[
            code
            for code in ALL_LEDGERS
            if any(code in (m.role.visible_ledgers or []) for m in mine)
        ],
        permissions=_unique(code for m in mine for code in (m.role.permissions or [])),
        # Роль называется на языке страницы, а не так, как её записали в базу
        # (T017). Название из базы — запасной вариант: партнёр вправе завести
        # свою роль, и перевода ей взять неоткуда.
        #
        # Ролей может быть несколько, и названы они все: отказ по правам
        # ссылается на роль дословно («… не входит в права вашей роли «Х»»), и
        # одно название из двух указывало бы человеку не на то.
        # Название несёт код роли — плашка в шапке красится по нему (раздел
        # «Роли» эталона: у каждого набора прав свой цвет). Код ставится только
        # когда роль одна: при двух ролях в одной плашке цвет одной из них врал
        # бы про вторую, и плашка остаётся нейтральной.
        role_title=_role_title_of(mine),
        tenant_title=mine[0].tenant.title,
        display_name=display_name,
        units_title=_units_title(unit_ids),
    )


def _role_title_of(memberships) -> str:
    """Название роли для шапки. Одна роль — с кодом, несколько — просто текст.

    Ролей может быть несколько, и названы они все: отказ по правам ссылается на
    роль дословно («… не входит в права вашей роли «Х»»), и одно название из
    двух указывало бы человеку не на то.
    """
    titles = _unique(role_title(m.role.code, m.role.title) for m in memberships)
    joined = " + ".join(titles)
    codes = _unique(m.role.code for m in memberships)
    return CodedTitle(joined, codes[0]) if len(codes) == 1 else joined


def _unique(values) -> list:
    """Список без повторов, в порядке первого появления.

    Не `set`: порядок ушёл бы в шапку и в тексты отказов и менялся бы от
    запроса к запросу.
    """
    seen: dict = {}
    for value in values:
        seen.setdefault(value, None)
    return list(seen)


def _units_title(unit_ids: list) -> str:
    """Коды точек для шапки. Пустой список — «все точки», подписывать нечего.

    Названия читаются обычной выборкой под теми же политиками: показать код
    точки, которую человеку видеть не положено, шапка не может физически.
    """
    if not unit_ids:
        return ""
    return ", ".join(
        Unit.objects.filter(pk__in=unit_ids).order_by("code").values_list("code", flat=True)
    )


def principal(request) -> dict:
    """Контекст-процессор: шапка на каждой странице показывает, кем вошли."""
    from .auth import dev_login_is_enabled

    who = get_current_principal(request)
    return {
        "principal": who,
        "visible_ledgers_title": (
            ", ".join(ledger_title(name) for name in who.visible_ledgers) if who else ""
        ),
        # То же самое списком: шапка рисует регистры метками их цвета (раздел
        # «Семантика регистров» эталона), а не серой строкой через запятую.
        # Строка выше остаётся — её берут гайд и заголовок страницы, где метки
        # неуместны. Названия те же и в том же порядке, поэтому расхождения
        # между двумя видами быть не может.
        "visible_ledgers": (
            [ledger_title(name) for name in who.visible_ledgers] if who else []
        ),
        # Метка в шапке обязана смотреть на флаг: иначе она обещает то, чего на
        # площадке нет.
        "dev_login": dev_login_is_enabled(),
    }
