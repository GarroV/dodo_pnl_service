"""Подписи компонентов выплаты на языке страницы (T092).

Подпись вида часов и надбавки — данные партнёра, а не строка интерфейса: состава
видов код не знает, страна заводит свои переопределением, а не релизом. Поэтому
переводит их не gettext, а сами правила: `title` в пресете несёт все языки
продукта, и `core.rules` сворачивает его к языку страницы. Полный разбор — в
журнале блока `web`, T092.

Здесь решается вторая половина задачи: **ведомость показывает не то, что лежит в
правилах сейчас, а то, что записано в `pay_components.title`** — подпись,
действовавшую в момент расчёта. Она одна, на языке правил, и языку страницы не
подчиняется. Поэтому экран берёт подпись заново по коду компонента — из правил
**того же периода**, а не сегодняшних.

Почему это не переписывание истории. Правила версионированы по датам наравне со
ставками: правила июня — это те самые правила, по которым июнь и посчитан.
Меняется только язык, которым они названы. А вот если кода в правилах больше
нет (партнёр убрал вид часов), подпись берётся хранимая: показать пустоту или
голый код значило бы стереть объяснение уже закрытого месяца.

Список подписей никуда не выводится целиком и выводиться не должен: он собран из
правил, а правила знают о регистрах учёта, которых роли может быть не видно
(D023). Наружу отдаётся только `label(код, хранимая)` — ответ про тот код,
который уже на экране.
"""
from __future__ import annotations

from datetime import date
from uuid import UUID

# Компоненты, у которых узел правил свой, а не список: подпись лежит прямо в нём.
SINGLES = ("minimum_guarantee", "manual_correction")


def titles_of(preset) -> dict[str, str]:
    """Код компонента → подпись из правил, уже на языке страницы.

    Коды собраны так же, как их составляет движок (`payroll.engine`): часы —
    `hours.<вид>`, сдельная работа — `piecework.<мера>`, надбавка — своим ключом.
    Перечислять их здесь приходится потому, что код компонента в правилах не
    хранится: его составляет расчёт. Разъехаться этим двум местам не даёт
    проверка `tests/test_rule_titles.py`.
    """
    found: dict[str, str] = {}

    def take(prefix: str, node) -> None:
        for code, body in (node or {}).items():
            title = (body or {}).get("title") if isinstance(body, dict) else None
            if isinstance(title, str) and title.strip():
                found[f"{prefix}{code}"] = title

    take("hours.", preset.get("hour_types"))
    take("piecework.", preset.get("work_measures"))
    take("", preset.get("allowances"))
    for code in SINGLES:
        body = preset.get(code)
        title = (body or {}).get("title") if isinstance(body, dict) else None
        if isinstance(title, str) and title.strip():
            found[code] = title
    return found


def component_titles(tenant_id: UUID, country_code: str, period: date) -> dict[str, str]:
    """Подписи компонентов по правилам этого периода. Правил нет — пустой ответ.

    Пустой ответ означает «показывай, что записано»: страница периода обязана
    открываться и тогда, когда правила страны из базы удалили, — иначе один
    несвязанный сбой уносил бы с собой всю ведомость.
    """
    try:
        from core.rules import load_rules_at

        return titles_of(load_rules_at(tenant_id, country_code, period).base)
    except Exception:  # noqa: BLE001 — правил может не быть, это не поломка страницы
        return {}


def labeller(tenant_id: UUID, country_code: str, period: date):
    """Готовая функция «код + хранимая подпись → что показать».

    Отдаётся функцией, а не словарём, чтобы правила читались один раз на
    страницу: тридцать строк ведомости — это тридцать одинаковых запросов, если
    спрашивать на каждую.
    """
    titles = component_titles(tenant_id, country_code, period)

    def label(code: str, stored: str = "") -> str:
        return titles.get(code) or stored or code

    return label
