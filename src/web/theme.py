"""Тема оформления: светлая, тёмная или как в системе (issue #164, T193).

Страница эталона «Тёмная тема» описывает своё устройство прямо: «ни один
компонент не переписан; в `tokens.css` добавлен блок `[data-theme="dark"]`,
который переопределяет значения — разметка про тему не знает». Токены перенесены
вместе с дизайн-системой; здесь только ручка, которой их включают.

**Выбор хранится в cookie и ставится сервером** в атрибут `<html data-theme>`.
Не в `localStorage` и не скриптом на странице: скрипт применяет тему уже после
загрузки, и человек каждый раз видит вспышку светлого перед тёмным. Плюс
страница обязана открываться правильной и без скриптов вовсе.

**Три состояния, а не два.** «Как в системе» — умолчание и полноценный ответ:
пока человек не попросил, продукт не спорит с настройкой машины. Тогда атрибута
нет вовсе, и работает `@media (prefers-color-scheme: dark)` из `tokens.css`.

**Значение проверяется по списку.** Оно уезжает прямо в разметку страницы, и
принять сюда что угодно значило бы позволить дописать в тег `<html>` чужое.
"""
from __future__ import annotations

from django.http import HttpResponseRedirect
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

COOKIE = "theme"

# Светлая тоже названа явно: человеку, у которого система тёмная, а работать
# удобнее на светлом, нужен способ это сказать. Без неё «как в системе» и
# «светлая» слились бы в один вариант, и он бы не сработал.
THEMES = ("system", "light", "dark")
DEFAULT = "system"

# Год: выбор темы — не сессионная мелочь, человек делает его один раз.
COOKIE_MAX_AGE = 365 * 24 * 60 * 60


def current(request) -> str:
    """Что выбрал этот читатель. Неизвестное значение читается как умолчание."""
    chosen = request.COOKIES.get(COOKIE, DEFAULT)
    return chosen if chosen in THEMES else DEFAULT


def theme(request) -> dict:
    """Контекст-процессор: атрибут для тега `<html>` и состояние переключателя.

    Атрибут пуст при «как в системе» — именно отсутствие атрибута и включает
    `prefers-color-scheme`. Ставить `data-theme="system"` нельзя: такого
    значения в `tokens.css` нет, и правило тёмной темы просто не сработает.
    """
    chosen = current(request)
    return {
        "theme_attr": "" if chosen == DEFAULT else chosen,
        "theme_current": chosen,
        "themes": [{"code": code, "current": code == chosen} for code in THEMES],
    }


@require_POST
def set_theme(request):
    """Запомнить выбор темы и вернуть человека туда, где он был.

    Только POST: это запись выбора, и по ссылке из чужого письма она случаться
    не должна. Адрес возврата проверяется — иначе форма превращается в открытый
    редирект на чужой сайт.
    """
    chosen = request.POST.get("theme", DEFAULT)
    if chosen not in THEMES:
        chosen = DEFAULT

    back = request.POST.get("next") or "/"
    if not url_has_allowed_host_and_scheme(
        back, allowed_hosts={request.get_host()}, require_https=request.is_secure(),
    ):
        back = "/"

    answer = HttpResponseRedirect(back)
    if chosen == DEFAULT:
        # «Как в системе» — это снятие выбора, а не третье значение в хранилище.
        answer.delete_cookie(COOKIE)
    else:
        answer.set_cookie(
            COOKIE, chosen, max_age=COOKIE_MAX_AGE, samesite="Lax",
            secure=request.is_secure(), httponly=False,
        )
    return answer
