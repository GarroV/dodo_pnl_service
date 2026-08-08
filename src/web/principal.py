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

from .format import ledger_title

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
    display_name = user.full_name or user.username
    membership = (
        Membership.objects.select_related("role", "tenant")
        .filter(user_id=user.pk)
        .first()
    )
    if membership is None:
        # Учётка есть, а членства нет: человека ещё не завели ни к одному
        # партнёру. Данных он не увидит — политики без членства пусты, — но и
        # выкидывать его со страницы незачем: пароль он поменять может.
        return Principal(user_id=user.pk, display_name=display_name)

    unit_ids = list(membership.unit_ids or [])
    return Principal(
        user_id=user.pk,
        tenant_id=membership.tenant_id,
        unit_ids=unit_ids,
        visible_ledgers=list(membership.role.visible_ledgers or []),
        permissions=list(membership.role.permissions or []),
        role_title=membership.role.title,
        tenant_title=membership.tenant.title,
        display_name=display_name,
        units_title=_units_title(unit_ids),
    )


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
        # Метка в шапке обязана смотреть на флаг: иначе она обещает то, чего на
        # площадке нет.
        "dev_login": dev_login_is_enabled(),
    }
