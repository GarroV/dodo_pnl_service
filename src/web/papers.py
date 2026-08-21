"""Бумага с точки: накладная и чек, принесённые управляющим (T174, D047).

Модуль отвечает на один вопрос: **что такое принесённая бумага в данных** — и
чем она отличается от счёта, который бухгалтер уже разобрал.

## Зачем это отдельное состояние

Роль управляющего в сборе первички — донести бумагу, а не считать (D047). Он
видит накладную поставщика и чек, но не знает ни статьи расхода, ни периода
учёта, ни того, на какую точку в итоге ляжет расход. Продукт, который требует от
него эти поля, получает выбор наугад — то есть данные, которые бухгалтеру
придётся перепроверять целиком, и тогда проще было бы прислать фотографию в
мессенджере.

Поэтому бумага входит в продукт **необработанным вложением**:

| что есть | что этого не значит |
|---|---|
| документ `source_documents` с точкой и отметкой `handed_over_at` | что расход признан |
| файл в `document_files` — фотография или PDF | что кто-то её прочитал |
| сумма со слов управляющего в `total_amount` | что это сумма расхода |

**Ни одной строки `facts` у бумаги нет.** Отсюда и главное свойство: в P&L её
нет вовсе — не потому, что отчёт её отфильтровал, а потому что фильтровать
нечего. P&L собирается из фактов, и пока бухгалтер бумагу не разобрал, факта не
существует. Это сильнее любого признака «не подтверждено»: признак можно забыть
проверить в очередном отчёте, а несуществующей строки не увидит никто.

## Чем бумага отличается от документа, у которого строки не записались

Ничем, если не отметить её явно, — и это ровно тот молчаливый сбой, который в
проекте-предшественнике стоил дорого: **документ без строк выглядел
разобранным**. Поэтому у принесённой бумаги стоит `handed_over_at`, и инбокс
собирает по ней. Документ, у которого запись строк отвалилась, отметки не
получит и в инбоксе не появится — зато и деньги по нему не появятся нигде, а
отказ человек увидит на экране сразу.

## Что здесь не живёт

Своего `insert` в `facts` — ни одного: разбор бумаги идёт через
`suppliers.record_invoice`, то есть через ту же `upsert_fact`, где живут
идемпотентность, версионирование и защита закрытого месяца. Разбор не заводит
второй документ: внешний ключ бумаги остаётся её собственным, и `upsert_document`
по нему обновляет **ту же** шапку.

Проверок прав тоже нет: чужую точку отвергает политика `unit_visibility` на
`source_documents` (D014). Здесь только перевод её отказа в слова.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date

from django.db import Error as DatabaseError
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext as _

from core.models import DocumentFile, Fact, SourceDocument

from . import cash, suppliers

# Приставка внешнего ключа документа-бумаги. Своя, а не общая со счётом: ключ
# отвечает на вопрос «то же самое событие или другое», и бумага, принесённая с
# точки, не должна попадать в тот же ключ, что счёт, внесённый бухгалтером.
PAPER_PREFIX = "paper:"

# Виды бумаги, которые приносят с точки. Ровно два: накладная поставщика и чек.
# Строка выписки и расчёт зарплаты бумагой с точки не бывают — их приносит не
# человек, а импорт.
INVOICE, RECEIPT = "invoice", "receipt"
PAPER_KINDS = (INVOICE, RECEIPT)

# Сколько байт принимаем. Десять мегабайт — это фотография с телефона с запасом;
# ограничение стоит здесь, а не только в браузере, потому что браузер в запросе
# участвовать не обязан.
MAX_BYTES = 10 * 1024 * 1024

# Тип содержимого определяется по самим байтам. Слову из браузера верить нельзя:
# он присылает то, что ему сказала операционная система, и подменяется это в
# запросе одной строкой. Проверка по подписи заодно отвечает на вопрос «а точно
# ли это фотография»: файл с расширением .jpg и другим содержимым не пройдёт.
SIGNATURES = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"%PDF-", "application/pdf"),
)

# Что видно в браузере как картинка. Остальное отдаётся файлом на сохранение:
# HEIC десктопные браузеры не рисуют, а PDF показывать внутри страницы значило
# бы запускать его просмотрщик на чужом файле.
SHOWN_INLINE = ("image/jpeg", "image/png", "image/webp")

# Подписи со смещением: у WebP и HEIC опознавательный кусок лежит не с начала.
WEBP_AT = (8, b"WEBP")
HEIC_AT = (4, (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypmsf1", b"ftyphevc"))


class PaperRefused(suppliers.SupplierRefused):
    """Бумага не принята. Сообщение показывается человеку как есть.

    Наследник отказа очереди намеренно: его уже ловят и формы, и вызовы по HTTP,
    а отдельный класс без родства остался бы неперехваченным ровно там, где
    перехватывать некому, — то есть стал бы пятисоткой.
    """


def media_type_of(data: bytes) -> str:
    """Тип файла по его байтам. Пусто — такой файл продукт не принимает."""
    for signature, media_type in SIGNATURES:
        if data.startswith(signature):
            return media_type
    at, mark = WEBP_AT
    if data.startswith(b"RIFF") and data[at:at + len(mark)] == mark:
        return "image/webp"
    at, marks = HEIC_AT
    if any(data[at:at + len(mark)] == mark for mark in marks):
        return "image/heic"
    return ""


@dataclass(frozen=True)
class Handed:
    """Что случилось с бумагой: какой документ и завелась ли новая запись."""

    document_id: str
    action: str            # handed | updated


def hand_over(who, *, entry_key: str, kind: str, unit_id, on: date,
              counterparty=None, amount=None, note: str = "",
              file_name: str = "", data: bytes) -> Handed:
    """Принять бумагу с точки: шапка документа и файл. Ни одной строки в P&L.

    Порядок именно такой — сначала документ, потом файл, — и обе записи в одной
    транзакции. Бумага без файла бессмысленна: разбирать бухгалтеру будет
    нечего, а в инбоксе она будет выглядеть работой, которую кто-то уже сделал.
    Поэтому отвалившаяся запись файла обязана унести с собой и документ, а не
    оставить пустую строку в очереди.

    Точку отвергает база. Управляющий, подставивший в форму чужую точку, получит
    отказ политики `unit_visibility`, а не проверку списком: список в форме —
    удобство, и защита, написанная в двух местах, однажды разойдётся молча
    (D014).
    """
    if kind not in PAPER_KINDS:
        # Незнакомый вид — испорченная форма, а не выбор человека.
        raise PaperRefused(_("Выберите, что за бумага: накладная или чек."))
    if not data:
        raise PaperRefused(_("Файл не приложен: без фотографии разбирать нечего."))
    if len(data) > MAX_BYTES:
        raise PaperRefused(
            _("Файл больше %(limit)s МБ. Сфотографируйте бумагу ещё раз "
              "или уменьшите снимок.") % {"limit": MAX_BYTES // (1024 * 1024)}
        )
    media_type = media_type_of(data)
    if not media_type:
        raise PaperRefused(
            _("Такой файл продукт не примет: нужна фотография (JPEG, PNG, WebP, "
              "HEIC) или PDF.")
        )

    payload = {
        "tenant_id": str(who.tenant_id),
        "kind": kind,
        "source": suppliers.MANUAL_SOURCE,
        "external_id": PAPER_PREFIX + entry_key,
        "doc_date": on.isoformat(),
        "unit_id": str(unit_id) if unit_id else None,
        "counterparty_id": str(counterparty.id) if counterparty is not None else None,
        "total_amount": str(amount) if amount is not None else None,
        # Отметка «бумагу принесли»: по ней бумага стоит в инбоксе и по ней же
        # отличается от документа, у которого строки не записались.
        "handed_over_at": timezone.now().isoformat(),
        # Что человек сказал про бумагу словами. В `payload`, а не в отдельной
        # колонке: для документа, заведённого руками, полезная нагрузка
        # источника — это и есть то, что набрал человек.
        "payload": {"note": note, "file_name": file_name} if (note or file_name) else None,
    }

    with transaction.atomic():
        document_id = _write_paper(payload)
        known = DocumentFile.objects.filter(document_id=document_id).exists()
        DocumentFile.objects.update_or_create(
            document_id=document_id,
            defaults={
                "tenant_id": who.tenant_id,
                "media_type": media_type,
                "byte_size": len(data),
                "content": data,
                "sha256": hashlib.sha256(data).hexdigest(),
                "created_by": who.user_id,
            },
        )
    return Handed(document_id=document_id, action="updated" if known else "handed")


def _write_paper(payload: dict) -> str:
    """Шапка бумаги через `upsert_document`: тот же ключ — тот же документ.

    Значит повторная отправка формы (или второй нажатие на телефоне с плохой
    связью) не заводит вторую бумагу, а обновляет ту же. Отказ базы переводится
    в слова здесь: `42501` — чужая точка, `23514` — бумага без точки, и оба
    человек обязан прочитать словами, а не увидеть пятисотку.
    """
    import json

    from django.db import connection

    body = {name: value for name, value in payload.items() if value is not None}
    try:
        with connection.cursor() as cursor:
            cursor.execute("select upsert_document(%s::jsonb)", [json.dumps(body)])
            written = str(cursor.fetchone()[0])
            # Ключи Django отложенные, то есть проверяются на коммите — за
            # пределами этой точки сохранения. Тот же довод, что в
            # `suppliers._write_document`, и точно так же режим возвращается.
            cursor.execute("set constraints all immediate")
            cursor.execute("set constraints all deferred")
            return written
    except DatabaseError as refusal:
        state = getattr(getattr(refusal, "__cause__", None), "sqlstate", "") or ""
        if state == "42501":
            raise cash.UnitRefused() from refusal
        if state == "23514":
            # Ограничение `source_documents_paper_names_its_unit`: бумага обязана
            # назвать точку. Сюда попадает тот, кто отправил форму без точки, —
            # например, ведущий все точки партнёра, у которого поле пустое.
            raise PaperRefused(
                _("У бумаги должна быть точка: укажите, с какой она пришла.")
            ) from refusal
        if state == "23503":
            # Чужой контрагент и несуществующий отвечают одинаково: по ответу
            # нельзя понять, что строка существует у соседа (D023).
            raise suppliers.SupplierRefused(_("Контрагент не найден.")) from refusal
        raise


# --- чтение -------------------------------------------------------------------


def papers(who, *, only_waiting: bool = False) -> list[SourceDocument]:
    """Бумаги, принесённые с точек. Срез делает база, здесь только порядок.

    Ни одного условия про права: чужую точку отсекает политика
    `unit_visibility` на `source_documents`, поэтому управляющий видит свои
    бумаги, а бухгалтер — все (D014).
    """
    rows = (
        SourceDocument.objects.select_related("counterparty", "unit")
        .filter(handed_over_at__isnull=False)
        .order_by("-handed_over_at")
    )
    if only_waiting:
        # Разобрана — значит у документа появились строки. Отдельного признака
        # «разобрано» нет намеренно: он был бы вторым ответом на вопрос, на
        # который отвечает наличие факта, и разошёлся бы с ним на первом же
        # сторно.
        rows = rows.filter(fact__isnull=True)
    return list(rows)


def paper_or_none(document_id) -> SourceDocument | None:
    """Бумага по номеру — под политиками базы, и только бумага.

    Обычный счёт сюда не попадает: разбирать его нечем, а 404 на него — тот же
    ответ, что на чужой (D023).
    """
    return (
        SourceDocument.objects.select_related("counterparty", "unit")
        .filter(pk=document_id, handed_over_at__isnull=False)
        .first()
    )


def file_of(document) -> DocumentFile | None:
    """Сам файл бумаги. Байты выбираются только здесь — списки их не трогают."""
    return DocumentFile.objects.filter(document_id=document.id).first()


def files_of(documents) -> dict[str, dict]:
    """Что за файл у каждой бумаги — **без байтов**.

    Отдельным запросом с явным списком колонок, а не `select_related`: тот
    выбрал бы `content`, и список из двадцати бумаг притащил бы в память
    двадцать фотографий.
    """
    ids = [document.id for document in documents]
    return {
        str(row["document_id"]): row
        for row in DocumentFile.objects.filter(document_id__in=ids).values(
            "document_id", "media_type", "byte_size",
        )
    }


def lines_of(document) -> list[Fact]:
    """Строки, которыми бумагу разобрали. Пусто — бумага ждёт разбора."""
    return list(
        Fact.objects.select_related("expense_item", "pnl_item", "unit")
        .filter(document_id=document.id, superseded_at__isnull=True)
        .exclude(allocation="allocated")
        .order_by("created_at")
    )


def note_of(document) -> str:
    """Что управляющий сказал о бумаге словами."""
    return ((document.payload or {}).get("note") or "").strip()


def document_key(document) -> str:
    """Ключ, под которым разбор пишет строку: внешний ключ бумаги ЦЕЛИКОМ.

    Именно целиком, с приставкой. `upsert_document` ищет документ по
    `(тенант, источник, внешний id)`, и приставка — часть этого id: обрезав её,
    разбор завёл бы **второй** документ, а бумага осталась бы стоять в инбоксе с
    фотографией и без строк. Поймано приёмкой: разбор отвечал «учтено», а в P&L
    не появлялось ничего (`test_papers_screen`).
    """
    return document.external_id or ""


__all__ = [
    "INVOICE",
    "MAX_BYTES",
    "PAPER_KINDS",
    "PAPER_PREFIX",
    "RECEIPT",
    "SHOWN_INLINE",
    "Handed",
    "PaperRefused",
    "document_key",
    "file_of",
    "files_of",
    "hand_over",
    "lines_of",
    "media_type_of",
    "note_of",
    "paper_or_none",
    "papers",
]
