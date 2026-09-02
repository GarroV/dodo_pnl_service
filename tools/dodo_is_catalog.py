"""Снять каталог Dodo IS API из документации — машинно, а не глазами.

Прежний разбор делался по коллекции Postman, и это дважды подвело: коллекция
неполна (финансовых методов в ней нет вовсе) и в ней нет примеров ответов —
из-за чего километраж курьера объявили отсутствующим, хотя он лежит в поле
`totalTripsDistance` смены сотрудника.

Сайт документации — SPA, и `curl` получает пустую оболочку. Но данные лежат в
открытом API Stoplight, без авторизации, и оттуда снимается вся спека: пути,
scope, параметры, поля ответов.

    python tools/dodo_is_catalog.py            # обновить docs/dodo-is-catalog.json
    python tools/dodo_is_catalog.py --check    # только сверить, ничего не писать
    python tools/dodo_is_catalog.py --grep пробег

`--check` возвращает ненулевой код, если каталог в репозитории разошёлся с
документацией: значит у них что-то поменялось и наш конспект пора перечитать.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "docs" / "dodo-is-catalog.json"

# Рабочее пространство docs.dodois.io. Идентификаторы получены из самой страницы
# (она ходит в этот же API), поэтому здесь они — константы, а не секрет.
WORKSPACE = "d2s6NzUzMjk"
API = "https://stoplight.io/api/v1"

# Проекты, которые касаются сбора первичных данных. Остальные (рейтинги, найм,
# контролинг, принтер этикеток, IoT) к P&L отношения не имеют — см.
# docs/dodo-is-api.md, раздел «Что есть, но нам не нужно».
PROJECTS = {
    "dodo-is": "cHJqOjExMTA4MQ",
    "accounting": "cHJqOjI2Nzg4Ng",
    "auth": "cHJqOjI5OTQxNg",
    "inventory": "cHJqOjI1NDI3MQ",
}

PAUSE = 0.12  # чужой сервис; выкачка идёт в один поток и без спешки


def fetch(url: str) -> Any:
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "dodo-pnl-service"}
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


def operations(items: list[dict]) -> list[tuple[str, str, str]]:
    """Обойти оглавление проекта и собрать все операции с их разделом."""

    def walk(nodes: list[dict], section: str = "") -> Any:
        for node in nodes:
            title = node.get("title") or ""
            if node.get("type") == "http_operation":
                yield section, node["slug"], title
            if node.get("items"):
                yield from walk(node["items"], title if not section else section)

    return list(walk(items))


def resolve(value: Any, bundled: dict, depth: int = 0) -> Any:
    """Развернуть ссылки Stoplight: сервера и security лежат отдельным блоком."""
    if depth > 14:
        return {}
    if isinstance(value, dict):
        ref = value.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/__bundled__/"):
            return resolve(bundled.get(ref.split("/")[-1], {}), bundled, depth + 1)
        return {k: resolve(v, bundled, depth + 1) for k, v in value.items() if k != "__bundled__"}
    if isinstance(value, list):
        return [resolve(v, bundled, depth + 1) for v in value]
    return value


def fields(schema: Any, prefix: str = "", depth: int = 0) -> list[str]:
    """Плоский список полей ответа: `shifts[].totalTripsDistance` и так далее.

    Именно этого не хватало в коллекции Postman — по путям нельзя понять, какие
    данные метод отдаёт на самом деле.
    """
    if depth > 6 or not isinstance(schema, dict):
        return []
    found: list[str] = []
    for name, value in (schema.get("properties") or {}).items():
        if not isinstance(value, dict):
            continue
        kind = value.get("type")
        description = " ".join((value.get("description") or "").split())
        found.append(
            f"{prefix}{name}: {kind} — {description}"
            if description
            else f"{prefix}{name}: {kind}"
        )
        if kind == "object":
            found += fields(value, f"{prefix}{name}.", depth + 1)
        elif kind == "array" and isinstance(value.get("items"), dict):
            found += fields(value["items"], f"{prefix}{name}[].", depth + 1)
    return found


def scopes_of(security: Any) -> list[str]:
    found: set[str] = set()
    for group in security or []:
        for scheme in group if isinstance(group, list) else [group]:
            if not isinstance(scheme, dict):
                continue
            for flow in (scheme.get("flows") or {}).values():
                found.update((flow.get("scopes") or {}).keys())
    return sorted(found)


def collect() -> dict:
    catalog: dict[str, Any] = {"projects": {}}
    for name, project_id in PROJECTS.items():
        toc = fetch(f"{API}/projects/{project_id}/table-of-contents")
        rows = []
        for section, slug, title in operations(toc["items"]):
            node = fetch(f"{API}/projects/{project_id}/nodes/{slug}")
            data = node.get("data")
            data = json.loads(data) if isinstance(data, str) else (data or {})
            bundled = data.get("__bundled__") or {}
            servers = [s.get("url") for s in resolve(data.get("servers") or [], bundled)]
            query = [
                {
                    "name": p.get("name"),
                    "required": bool(p.get("required")),
                    "description": " ".join((p.get("description") or "").split()),
                }
                for p in (resolve(data.get("request") or {}, bundled).get("query") or [])
            ]
            answer: list[str] = []
            for response in resolve(data.get("responses") or [], bundled):
                if str(response.get("code")) != "200":
                    continue
                for content in response.get("contents") or []:
                    answer += fields(content.get("schema") or {})
            rows.append(
                {
                    "section": section,
                    "method": (data.get("method") or "").upper(),
                    "path": data.get("path") or "",
                    "title": title,
                    "scopes": scopes_of(resolve(data.get("security") or [], bundled)),
                    "servers": [s for s in servers if s],
                    "query": query,
                    "response": answer,
                    "description": " ".join((data.get("description") or "").split()),
                }
            )
            time.sleep(PAUSE)
        rows.sort(key=lambda r: (r["path"], r["method"]))
        catalog["projects"][name] = rows
        print(f"  {name}: {len(rows)} методов", file=sys.stderr)
    return catalog


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="сверить и не писать")
    parser.add_argument("--grep", metavar="СЛОВО", help="искать по каталогу, ничего не выкачивая")
    args = parser.parse_args()

    if args.grep:
        if not CATALOG.exists():
            print("Каталога нет — сначала прогоните без ключей.", file=sys.stderr)
            return 2
        needle = args.grep.lower()
        catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
        hits = 0
        for project, rows in catalog["projects"].items():
            for row in rows:
                where = []
                if needle in (row["title"] + row["path"] + row["description"]).lower():
                    where.append("метод")
                where += [f for f in row["response"] if needle in f.lower()]
                where += [
                    f"?{q['name']}: {q['description']}"
                    for q in row["query"]
                    if needle in (q["name"] + q["description"]).lower()
                ]
                if where:
                    hits += 1
                    print(f"\n{row['method']} {row['path']}  [{project}] — {row['title']}")
                    print(f"  scope: {', '.join(row['scopes']) or '—'}")
                    for line in where[:12]:
                        print(f"    {line}")
        if not hits:
            print(f"Ничего не нашлось по «{args.grep}».")
        return 0

    print("Снимаю каталог из документации…", file=sys.stderr)
    try:
        fresh = collect()
    except (urllib.error.URLError, TimeoutError) as error:
        print(f"Документация недоступна: {error}", file=sys.stderr)
        return 2

    text = json.dumps(fresh, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    if args.check:
        if not CATALOG.exists():
            print("Каталога в репозитории нет.", file=sys.stderr)
            return 1
        if CATALOG.read_text(encoding="utf-8") == text:
            total = sum(len(rows) for rows in fresh["projects"].values())
            print(f"Каталог совпадает с документацией: {total} методов.")
            return 0
        old = json.loads(CATALOG.read_text(encoding="utf-8"))
        was = {(p, r["method"], r["path"]) for p, rows in old["projects"].items() for r in rows}
        now = {(p, r["method"], r["path"]) for p, rows in fresh["projects"].items() for r in rows}
        print("Каталог разошёлся с документацией.")
        for key in sorted(now - was):
            print(f"  появилось: {key[1]} {key[2]}  [{key[0]}]")
        for key in sorted(was - now):
            print(f"  исчезло:   {key[1]} {key[2]}  [{key[0]}]")
        if was == now:
            print("  пути те же, изменились параметры или поля ответов")
        print("\nПерепрогоните без --check и перечитайте docs/dodo-is-api.md.")
        return 1

    CATALOG.write_text(text, encoding="utf-8")
    total = sum(len(rows) for rows in fresh["projects"].values())
    print(f"Записал {CATALOG.relative_to(ROOT)}: {total} методов.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
