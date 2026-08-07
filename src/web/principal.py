"""
Кто сейчас работает: тенант, точки, видимые регистры.

Форма ответа — из контракта блока `auth` (`get_current_principal()`), чтобы при
появлении настоящего входа менялся только источник, а не потребители. Данные
читаются обычной выборкой **под уже выставленным контекстом**: то, что человек
не видит по политикам базы, не попадёт сюда и в интерфейс.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from uuid import UUID

from core.models import Membership

from .devauth import current_user
from .format import ledger_title


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


def current_principal(request) -> Principal | None:
    """Текущий пользователь или None, если контекст не выставлен."""
    user = current_user(request)
    if user is None:
        return None

    membership = (
        Membership.objects.select_related("role", "tenant")
        .filter(user_id=user.user_id)
        .first()
    )
    if membership is None:
        # Контекст есть, а членства нет: в базе нет сида либо учётка чужая.
        # Отдаём то, что знаем сами, — но без тенанта и без регистров.
        return Principal(user_id=user.user_id, role_title=user.title)

    return Principal(
        user_id=user.user_id,
        tenant_id=membership.tenant_id,
        unit_ids=list(membership.unit_ids or []),
        visible_ledgers=list(membership.role.visible_layers or []),
        permissions=list(membership.role.permissions or []),
        role_title=membership.role.title,
        tenant_title=membership.tenant.title,
    )


def principal(request) -> dict:
    """Контекст-процессор: шапка на каждой странице показывает, кем вошли."""
    who = current_principal(request)
    return {
        "principal": who,
        "visible_ledgers_title": (
            ", ".join(ledger_title(name) for name in who.visible_ledgers) if who else ""
        ),
    }
