"""
Вход «на время стройки»: переключатель между пользователями сида.

Настоящей аутентификации ещё нет — она работа блока `auth`. Здесь заглушка,
которая делает ровно одно: кладёт в сессию код учётки из сида и отдаёт по нему
идентификатор пользователя для контекста базы.

Два свойства, без которых заглушку нельзя пускать даже в разработку:

- в сессии лежит **код** учётки, а не uuid: в контекст базы может попасть
  только один из трёх известных пользователей сида, а не любое число,
  подставленное в cookie;
- всё это выключается настройкой `DEV_LOGIN_ENABLED` (переменная окружения
  `DEV_LOGIN`). Выключено — страниц входа нет, контекст не выставляется вовсе.

Список учёток берётся из самой команды сида, а не переписывается сюда: иначе
он разъедется с базой при первой же правке сида.
"""
from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from django.conf import settings

from core.management.commands.seed_dev import ROLES, det_id

SESSION_KEY = "dev_user"


@dataclass(frozen=True)
class DevUser:
    """Учётка сида: код роли, как её показывать и чем она представляется базе."""

    code: str
    title: str
    ledgers: tuple[str, ...]
    unit: str | None

    @property
    def user_id(self) -> UUID:
        # Тот же det_id, что и в сиде: команда печатает эти же идентификаторы.
        return det_id("user", self.code)


DEV_USERS: dict[str, DevUser] = {
    code: DevUser(code=code, title=title, ledgers=tuple(ledgers), unit=unit)
    for code, title, ledgers, unit in ROLES
}


def is_enabled() -> bool:
    return bool(getattr(settings, "DEV_LOGIN_ENABLED", False))


def current_user(request) -> DevUser | None:
    if not is_enabled():
        return None
    return DEV_USERS.get(request.session.get(SESSION_KEY, ""))


def current_user_id(request) -> UUID | None:
    """Кем представляться базе. None — контекста нет, выборки будут пустыми."""
    user = current_user(request)
    return user.user_id if user else None


def login(request, code: str) -> DevUser:
    """Войти известной учёткой. Неизвестный код — отказ, а не тихий пропуск."""
    user = DEV_USERS[code]
    request.session[SESSION_KEY] = user.code
    return user


def logout(request) -> None:
    request.session.pop(SESSION_KEY, None)
