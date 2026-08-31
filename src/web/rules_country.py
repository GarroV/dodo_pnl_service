"""Правила страны: смотреть в продукте и заводить им новую версию (T165).

Зачем отдельный модуль рядом с `web/rules.py`. Там — слой партнёра: три уровня
переопределений, которые принадлежат одному тенанту и закрыты его правом
`rules.manage`. Здесь — **тело пресета страны**: ночные часы, больничные,
ставки взносов, пороги. Это общая для всех партнёров страны база, у неё нет
`tenant_id` (`SHARED_TABLES`, `0004_rls`), и у неё другое право и другая механика
версий. Один модуль на два разных владельца данных читался бы как один владелец.

**Почему это вообще появилось.** Тело приезжало в базу один раз командой
`manage.py load_presets` из YAML, то есть поднять минимальную зарплату Сербии
можно было только разработчиком и с доступом к серверу. Владелец 2026-08-18
искал эти правила в продукте и не нашёл.

**Кто вправе.** Не роль партнёра. Право на всю страну, выданное внутри одного
тенанта, разрешило бы партнёру менять расчёт соседу — довод миграции
`0180_rules_permissions`, и он в силе. Поэтому право лежит в отдельной таблице
`platform_admins`, которая из приложения не пишется вовсе, а выдаётся командой
`manage.py platform_admin` (миграция `0248`).

**Версия, а не правка по месту.** Тело не переписывается: правка заводит новую
строку `rule_presets` с датой начала действия, а прежняя закрывается этим же
днём. Сборка правил берёт версию, действовавшую в считаемом месяце
(`core.rules._in_force`), поэтому уже посчитанный июнь остаётся собранным из
июньской строки — байт в байт той же. Это то же правило, что у переопределений
партнёра, и та же причина: ведомость закрытого месяца уже на руках у людей.

**Подписи правил здесь не правятся.** `title` и `pnl_line` в теле страны несут
все языки сразу (`payroll.presets.LOCALIZED_KEYS`, T092), а экран показывает
один — тот, на котором открыта страница. Записать введённое строкой значило бы
затереть остальные языки, и заметили бы это на чужом языке и не сразу. Поэтому
подпись правила страны остаётся делом файла, и отказ говорит об этом словами.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import date
from typing import Any

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from core.models import RulePreset
from core.spaces import is_platform_admin
from payroll.presets import LOCALIZED_KEYS, set_path, to_jsonable

__all__ = [
    "CountryChange", "CountryRulesRefused", "CountryRulesUnchanged", "LOCALIZED_KEYS",
    "country_versions", "explain_refusal", "in_force_at", "may_edit", "save_country_value",
]


class CountryRulesRefused(Exception):
    """Правила страны этому человеку не заводить. Показывается как есть."""

    http_status = 403

    def __init__(self, message: str, status: int = 403):
        self.message = message
        self.http_status = status
        super().__init__(message)


class CountryRulesUnchanged(Exception):
    """Правило страны и так такое — версия не заведена, и это не ошибка.

    Отдельным исключением, а не отказом: человек ничего не сделал неправильно, и
    плашка ему нужна спокойная, а не «не сохранено». Тем же различием, каким
    отличаются `notice` и `error` на форме переопределений.
    """

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def may_edit(who) -> bool:
    """Вправе ли этот человек вести правила стран.

    Спрашивается у базы, а не у списка прав роли: право не партнёрское, в
    `memberships` его нет и быть не должно. Своя строка `platform_admins`
    человеку видна политикой, чужие — нет, поэтому вопрос честный и без
    обхода RLS.
    """
    return is_platform_admin(who.user_id if who is not None else None)


def explain_refusal() -> str:
    """Почему кнопки правки правил страны нет — словами, а не пустым местом.

    Кнопка, пропавшая без объяснения, читается как поломка продукта, а не как
    запрет (T072). Здесь вдобавок надо сказать, **где** эти правила живут:
    человек, не нашедший их в продукте, ровно это и спрашивает.
    """
    return _(
        "Правила страны — общая база для всех партнёров этой страны, поэтому их "
        "ведёт не партнёр, а администратор платформы. Для себя любое из них "
        "можно переопределить формой ниже: правило страны от этого не меняется."
    )


def in_force_at(country_code: str, on_date: date) -> RulePreset | None:
    """Версия правил страны, действующая на дату. Теми же границами, что расчёт."""
    return (
        RulePreset.objects.filter(country_code__iexact=country_code, valid_from__lte=on_date)
        .exclude(valid_to__lte=on_date)
        .order_by("-valid_from")
        .first()
    )


def refuse_if_localized(path: str) -> None:
    """Подпись правила страны правится в файле, а не здесь.

    Отказ, а не тихая запись: тело страны несёт подпись на всех языках сразу, и
    введённая строка вытеснила бы остальные. Увидел бы это тот, кто откроет
    продукт на другом языке, — то есть не автор правки и не сразу.
    """
    if path.rsplit(".", 1)[-1] in LOCALIZED_KEYS:
        raise CountryRulesRefused(
            _("Название правила страны заведено на всех языках сразу, и меняется "
              "оно вместе с пресетом страны, а не с этого экрана."),
            status=400,
        )


@dataclass(frozen=True)
class CountryChange:
    """Что стало с правилами страны: заведена ли версия и какая."""

    changed: bool
    version: RulePreset | None = None
    previous: RulePreset | None = None


@transaction.atomic
def save_country_value(country_code: str, path: str, value: Any, *, valid_from: date,
                       actor_id=None, effective: Any = None) -> CountryChange:
    """Завести новую версию правил страны с этой даты и этим значением.

    `effective` — значение, действующее в стране на эту дату сейчас. Совпало —
    версия не заводится: копия тела ради неизменившегося правила означала бы
    вторую версию с тем же смыслом, и найти в истории настоящую смену закона
    стало бы нельзя.

    Порядок записи важен: прежняя версия закрывается **до** вставки новой.
    Ограничение `rule_presets_no_overlap` немедленное, и обратный порядок
    отвергал бы правку на пересечении периодов, хотя правка верна.
    """
    refuse_if_localized(path)
    current = in_force_at(country_code, valid_from)
    if current is None:
        raise CountryRulesRefused(
            _("Правил этой страны на указанную дату в базе нет — менять нечего. "
              "Сначала загрузите пресет страны: python manage.py load_presets"),
            status=400,
        )
    if effective is not None and value == effective:
        return CountryChange(changed=False)

    body = copy.deepcopy(current.body)
    set_path(body, path, to_jsonable(value))
    now = timezone.now()
    # Тело называет свою дату начала действия само (`valid_from` в пресете), и
    # новая версия обязана называть свою. Оставленная прежней, она бы врала:
    # экран этих ключей не показывает (`rules.IDENTITY`), но пресет читают и
    # мимо экрана, а тело, которое говорит о себе не то, — это будущая находка
    # «почему в базе июньская дата у сентябрьской версии».
    set_path(body, "valid_from", valid_from.isoformat())

    if current.valid_from == valid_from:
        # Версия начинается тем же днём: отдельно от новой она не действовала ни
        # одного дня, а вторая строка с той же датой всё равно не прошла бы —
        # ключ `code + valid_from` уникален. Терять здесь нечего. Тот же приём и
        # тот же довод, что у переопределений партнёра.
        current.body = body
        current.edited_at = now
        current.edited_by = actor_id
        current.save(update_fields=["body", "edited_at", "edited_by"])
        return CountryChange(changed=True, version=current)

    tail = current.valid_to
    RulePreset.objects.filter(pk=current.pk).update(valid_to=valid_from)
    fresh = RulePreset.objects.create(
        code=current.code,
        title=current.title,
        country_code=current.country_code,
        body=body,
        valid_from=valid_from,
        valid_to=tail,
        edited_at=now,
        edited_by=actor_id,
    )
    return CountryChange(changed=True, version=fresh, previous=current)


def country_versions(country_code: str):
    """Версии правил страны — от старой к новой, для истории на экране."""
    return (
        RulePreset.objects.filter(country_code__iexact=country_code)
        .order_by("valid_from")
    )
