"""Материал для тестов фактов: один способ собрать факт и один — записать его.

Отдельным модулем, а не копиями в двух файлах: тесты доступа и тесты поведения
пишут факты одинаково, и разъехавшиеся заготовки означали бы, что проверки
говорят о разных вещах, называя их одним словом.

Запись всегда идёт через `upsert_fact` — единственную точку записи факта в
схеме. Прямой `insert` в тесте проверял бы путь, которым продукт не ходит.
"""
from __future__ import annotations

from psycopg.types.json import Jsonb

from conftest import I_FOOD, JUNE, T1

# Название позиции обязательно, а смысла в тесте не несёт: одно на все факты.
TITLE = "Позиция документа"


def fact_payload(
    *,
    tenant: str = T1,
    unit: str | None = None,
    item: str = I_FOOD,
    ledger: str = "official",
    amount: str = "100.00",
    key: str = "fact-1",
    period: str = JUNE,
    allocation: str | None = None,
    counterparty: str | None = None,
    legal_entity: str | None = None,
    source: str = "manual",
    channel: str | None = None,
    currency: str = "RSD",
    title: str = TITLE,
    doc_date: str | None = None,
    document: str | None = None,
) -> dict:
    """Тело факта для `upsert_fact`.

    `allocation` по умолчанию выводится из точки: она известна — `direct`, не
    известна — `pending`. Это ровно то, что требует схема (`direct` обязан знать
    точку), и заодно избавляет каждый тест от повторения очевидного.
    """
    if allocation is None:
        allocation = "direct" if unit else "pending"
    payload = {
        "tenant_id": tenant,
        "period": period,
        "unit_id": unit,
        "pnl_item_id": item,
        "ledger": ledger,
        "amount": amount,
        "currency": currency,
        "title": title,
        "source": source,
        "dedup_key": key,
        "allocation": allocation,
        "counterparty_id": counterparty,
        "legal_entity_id": legal_entity,
        "channel": channel,
        "doc_date": doc_date,
        "document_id": document,
    }
    # Пустые ключи не отправляем: jsonb_populate_record поставил бы null поверх
    # значения по умолчанию — например, обнулил бы валюту тенанта.
    return {name: value for name, value in payload.items() if value is not None}


def upsert_fact(conn, payload: dict) -> tuple[str, str]:
    """Записать факт. Возвращает (id, действие): inserted | updated | unchanged."""
    return conn.execute(
        "select fact_id, action from upsert_fact(%s)", (Jsonb(payload),)
    ).fetchone()


def active_facts(conn, *, key: str | None = None) -> list[tuple]:
    """Действующие строки: заменённые в отчёты не попадают и здесь не нужны."""
    query = (
        "select dedup_key, unit_id::text, amount, allocation, revision from facts"
        " where superseded_at is null"
    )
    params: tuple = ()
    if key is not None:
        query += " and dedup_key like %s"
        params = (f"{key}%",)
    return conn.execute(query + " order by dedup_key", params).fetchall()
