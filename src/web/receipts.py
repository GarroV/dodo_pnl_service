"""Чек к расходу наличными: приложить, найти, отдать (T184).

Модуль 6 эталона стоит на мысли «наличный расход — два независимых факта».
Первый — деньги вышли из кассы; он есть в продукте с T109. Второй — бумага, по
которой трату можно подтвердить; до этой задачи его не было вовсе: сумма из чека
вводилась руками, а сам чек нигде не оставался.

**Чек живёт при записи расхода, а не при строке факта.** Ключ — `entry_key`, из
которого собран `facts.dedup_key`. Правка расхода заводит новую версию факта с
новым `id`, и привязка к `id` осиротела бы на первой правке суммы — молча,
потому что сама правка прошла бы успешно. Разбор этого решения целиком — в
шапке миграции `0259` и в `core.models.CashReceipt`.

**Проверок прав здесь ни одной.** Кому виден и кому доступен на запись чек,
решает политика `follows_its_expense`: она зовёт сам факт, а значит чек режется
по точке, регистру и кассе ровно так же, как расход. Своя проверка рядом была бы
вторым ответом на тот же вопрос — тем, который однажды разойдётся с первым молча
(D014). Здесь — только разбор файла и перевод отказа базы в слова.

**Тип файла определяется по байтам**, как у бумаги с точки: разбор берётся у
`web.papers` целиком, а не переписывается. Две копии списка подписей форматов
разъехались бы на первом же добавленном формате, и телефон, чью фотографию
принял один экран, получил бы отказ на другом.
"""
from __future__ import annotations

import hashlib

from django.db import Error as DatabaseError
from django.utils.translation import gettext as _

from core.models import CashReceipt

from . import papers

# Что предлагать в окне выбора файла. Список тот же, что принимает разбор, —
# иначе браузер показывал бы человеку файлы, которые продукт отвергнет.
ACCEPT = "image/jpeg,image/png,image/webp,image/heic,application/pdf"


class ReceiptRefused(Exception):
    """Чек не принят: не тот файл, пустой или слишком большой.

    422, а не 400: запрос разобран и понят, не принято именно его содержимое.
    Тот же код и по тому же доводу, что у отказа по книге Excel в отчётах.
    """

    http_status = 422

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


def attach(who, entry_key: str, *, data: bytes, file_name: str = "") -> None:
    """Приложить чек к записи расхода. Второй чек заменяет первый.

    Замена, а не второй ряд: две фотографии одного расхода с разными суммами
    означали бы, что бухгалтер сверяет число неизвестно с чем. Переснятый чек —
    это тот же чек, снятый лучше.

    Запись идёт под политиками: чек к расходу, которого человек не видит, база
    не примет (`42501`), и здесь это переводится в слова. Разбирать причину
    подробнее нельзя — «чужая точка», «чужой регистр» и «такого расхода нет»
    обязаны отвечать одинаково (D023).
    """
    if not data:
        raise ReceiptRefused(_("Файл не приложен: прикладывать нечего."))
    if len(data) > papers.MAX_BYTES:
        raise ReceiptRefused(
            _("Файл больше %(limit)s МБ. Сфотографируйте чек ещё раз или "
              "уменьшите снимок.") % {"limit": papers.MAX_BYTES // (1024 * 1024)}
        )
    media_type = papers.media_type_of(data)
    if not media_type:
        raise ReceiptRefused(
            _("Такой файл продукт не примет: нужна фотография (JPEG, PNG, WebP, "
              "HEIC) или PDF.")
        )

    try:
        CashReceipt.objects.update_or_create(
            tenant_id=who.tenant_id, entry_key=entry_key,
            defaults={
                "media_type": media_type,
                "byte_size": len(data),
                "content": data,
                "sha256": hashlib.sha256(data).hexdigest(),
                "file_name": file_name or None,
                "created_by": who.user_id,
            },
        )
    except DatabaseError as refusal:
        state = getattr(getattr(refusal, "__cause__", None), "sqlstate", "") or ""
        if state == "42501":
            raise ReceiptRefused(
                _("К этому расходу чек приложить нельзя.")
            ) from refusal
        raise


def of(entry_key: str):
    """Чек записи расхода вместе с байтами; нет — `None`.

    Отдельно от `presence_of`, потому что байты весят: список расходов
    спрашивает только «есть ли», и тянуть в него сорок фотографий было бы
    десятками мегабайт на экран.
    """
    return CashReceipt.objects.filter(entry_key=entry_key).first()


def presence_of(keys) -> dict[str, dict]:
    """Ключ записи → сведения о чеке, без содержимого файла.

    Один запрос на весь список, а не по запросу на строку: реестр за месяц — это
    сотни строк, и запрос в цикле превратил бы экран в сотню походов в базу.
    """
    keys = [key for key in keys if key]
    if not keys:
        return {}
    found = CashReceipt.objects.filter(entry_key__in=keys).only(
        "entry_key", "media_type", "byte_size", "file_name",
    )
    return {
        row.entry_key: {
            "media_type": row.media_type,
            "byte_size": row.byte_size,
            "file_name": row.file_name or "",
        }
        for row in found
    }


def file_name_for(fact, kept) -> str:
    """Имя файла на сохранение: без него браузер предложит номер строки.

    Собирается из даты расхода, а не берётся у загруженного файла: `IMG_2481.jpg`
    из телефона не говорит ни о чём, а чужое имя в кавычках заголовка пришлось бы
    ещё и вычищать от кавычек.
    """
    stamp = fact.doc_date.isoformat() if fact.doc_date else "cash"
    suffix = {
        "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
        "image/heic": "heic", "application/pdf": "pdf",
    }.get(kept.media_type, "bin")
    return f"cek-{stamp}.{suffix}"
