"""Пересобрать снимки бумаг для демо-стенда (T174).

Зачем инструмент, а не генерация на ходу. Бумага с точки — это **файл**, и
демо обязано показывать настоящий файл: карточка рисует снимок картинкой, а
инбокс обещает бухгалтеру, что с него читаются поставщик, дата и сумма. Пустой
прямоугольник это обещание не выполняет, а нарисовать читаемый текст в момент
наполнения нечем: растрового шрифта в продукте нет, а `woff2` из статики без
библиотеки не растеризуется.

Поэтому снимки лежат в репозитории готовыми файлами (`src/demo/fixtures/`), а
этот инструмент их пересобирает — тем же способом, каким `tools/make_fixture.py`
пересобирает обезличенный образец зарплатной таблицы. Данные в них
**придуманные**: несуществующая пиццерия, несуществующий поставщик, круглые
суммы. Ничего партнёрского в репозиторий не попадает.

```bash
python tools/make_demo_scans.py
```

PDF собирается здесь целиком (Helvetica — один из четырнадцати шрифтов, которые
обязан знать любой просмотрщик, поэтому файла шрифта не нужно). PNG получается
растеризацией того же PDF системным `sips` — он есть в macOS, и это единственное
место, где инструмент от неё зависит. Продукт и его тесты от `sips` не зависят
вовсе: они читают уже собранные файлы.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "src" / "demo" / "fixtures"

# Ширина страницы в пунктах — A4. Высота считается по содержимому: лист A4
# целиком дал бы полстраницы пустоты, а в карточке снимок ограничен шириной
# колонки — то есть пустой низ съел бы ровно ту высоту, на которой читается
# текст.
WIDTH = 595
MARGIN = 56

# Шаг строки заметно больше кегля: это скан, а не вёрстка, и плотный текст на
# растре в 150 точек слипся бы.
LEAD = 17

# Чем набрана строка. Колонки сумм набраны **моношириной** (Courier — тоже один
# из четырнадцати обязательных шрифтов): в пропорциональном Helvetica столбец
# «Amount», выровненный пробелами, разъезжается, и бумага перестаёт выглядеть
# напечатанной кассой или складом.
# Итог набран тем же кеглем моноширины, только жирным: другой кегль сбил бы
# столбец сумм, а итог, не стоящий под столбцом, сверить глазами нельзя.
FONTS = {
    "head": ("/F2", 19), "sub": ("/F1", 11),
    "body": ("/F3", 10.5), "total": ("/F4", 10.5),
}

# Ширины колонок таблицы в знаках. Строки собираются по ним, а не набираются
# пробелами руками: выровненный руками столбец разъезжается на первой же правке
# названия товара, и заметно это только глазами на картинке.
COLUMNS = (30, 5, 12, 13)


def row(article: str, qty: str, price: str, amount: str) -> tuple[str, int, str]:
    """Строка таблицы: название слева, числа по правому краю своих колонок."""
    name, count, rate, total = COLUMNS
    return ("body", 0, f"{article:<{name}}{qty:>{count}}"
                       f"{price:>{rate}}{amount:>{total}}")


def escape(text: str) -> str:
    """Экранировать то, что в PDF-строке значит не себя."""
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def step(kind: str) -> float:
    """Насколько строка этого вида опускает курсор вниз."""
    return LEAD if kind in ("body", "rule") else LEAD + 8


def page_height(lines: list[tuple[str, int, str]]) -> int:
    """Высота листа под ровно это содержимое, с полями сверху и снизу."""
    return round(64 + sum(step(kind) for kind, _, _ in lines) + MARGIN * 0.5)


def page_stream(lines: list[tuple[str, int, str]], height: int) -> bytes:
    """Содержимое страницы: строки текста и горизонтальные линейки.

    Строка — это `(вид, отступ, текст)`. Вид `rule` рисует линейку и текст
    игнорирует: на накладной есть разлиновка, и без неё колонка сумм читается
    хуже, чем на настоящей бумаге.
    """
    out = [b"0.12 0.12 0.12 rg"]
    y = height - 64
    for kind, indent, text in lines:
        if kind == "rule":
            out.append(
                f"0.55 w 0.55 0.55 0.55 RG {MARGIN} {y + 11} m "
                f"{WIDTH - MARGIN} {y + 11} l S 0.12 0.12 0.12 rg".encode()
            )
            y -= step(kind)
            continue
        font, size = FONTS[kind]
        out.append(
            f"BT {font} {size} Tf {MARGIN + indent} {y} Td "
            f"({escape(text)}) Tj ET".encode()
        )
        y -= step(kind)
    return b"\n".join(out)


def pdf(lines: list[tuple[str, int, str]]) -> bytes:
    """Однострочный PDF-писатель: каталог, страница, поток и четыре шрифта.

    Смещения объектов считаются по факту сборки, а не прикидываются: `xref` с
    неверным смещением открывается в одном просмотрщике и не открывается в
    другом, причём молча — файл просто «повреждён».
    """
    height = page_height(lines)
    stream = page_stream(lines, height)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {WIDTH} {height}] "
        f"/Resources << /Font << /F1 5 0 R /F2 6 0 R /F3 7 0 R /F4 8 0 R >> >> "
        f"/Contents 4 0 R >>".encode(),
        b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream
        + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Courier-Bold >>",
    ]

    body, offsets = b"%PDF-1.4\n", []
    for number, payload in enumerate(objects, start=1):
        offsets.append(len(body))
        body += f"{number} 0 obj\n".encode() + payload + b"\nendobj\n"

    start = len(body)
    table = f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    table += b"".join(f"{offset:010} 00000 n \n".encode() for offset in offsets)
    trailer = (f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\n"
               f"startxref\n{start}\n%%EOF\n").encode()
    return body + table + trailer


def rasterise(source: bytes) -> bytes:
    """PDF → PNG системным `sips`. Нет его — инструмент честно падает.

    Молча положить в демо PDF вместо снимка нельзя: карточка показала бы кнопку
    «Скачать файл» там, где обещан снимок, и ветка с картинкой осталась бы в
    демо непоказанной.
    """
    with tempfile.TemporaryDirectory() as room:
        incoming = Path(room) / "scan.pdf"
        outgoing = Path(room) / "scan.png"
        incoming.write_bytes(source)
        done = subprocess.run(
            ["sips", "-s", "format", "png", "-s", "dpiHeight", "150",
             "-s", "dpiWidth", "150", "--resampleWidth", "1000",
             str(incoming), "--out", str(outgoing)],
            capture_output=True, text=True,
        )
        if done.returncode != 0:
            raise SystemExit(f"sips не справился: {done.stdout}{done.stderr}")
        return outgoing.read_bytes()


# Накладная поставщика: её управляющий фотографирует чаще всего. Суммы сходятся
# по столбцу нарочно — бухгалтер в демо должен мочь сверить итог глазами, как он
# делает это с настоящей бумагой.
DELIVERY_NOTE = [
    ("head", 0, "METRO CASH & CARRY d.o.o."),
    ("sub", 0, "Autoput za Novi Sad 120, Beograd  /  PIB 100 555 777"),
    ("rule", 0, ""),
    ("head", 0, "DELIVERY NOTE  No. DN-8814"),
    ("body", 0, "Date: 14.08.2026"),
    ("body", 0, "Buyer: Dodo Novi Sad d.o.o.  /  Novi Sad Bulevar (NS1)"),
    ("body", 0, "Payment: 15 days from delivery"),
    ("rule", 0, ""),
    row("Article", "Qty", "Price", "Amount"),
    row("Mozzarella block 10 kg", "6", "2 400.00", "14 400.00"),
    row("Tomato sauce 5 l", "4", "650.00", "2 600.00"),
    row("Flour type 500, 25 kg", "3", "800.00", "2 400.00"),
    row("Olive oil 5 l", "1", "1 250.00", "1 250.00"),
    row("Paper napkins, 12 packs", "2", "450.00", "900.00"),
    ("rule", 0, ""),
    ("total", 0, f"{'TOTAL, RSD':<{sum(COLUMNS) - 13}}{'21 550.00':>13}"),
    ("body", 0, "VAT 10% included:  1 959.09"),
    ("rule", 0, ""),
    ("body", 0, "Delivered by: M. Jovanovic          Received by: ____________"),
    ("sub", 0, "Demo document. Not a real invoice."),
]

# Чек: бумага, по которой денег больше не будет — их отдали на месте. Он же
# вторая ветка карточки: PDF отдаётся файлом на сохранение, а не рисуется
# картинкой, и обе ветки в демо должны быть видны.
#
# Товары в нём — тоже сырьё, и поставщик тот же. Это не лень: у разобранной
# бумаги в демо есть статья расхода, а статьи под упаковку в наборе нет — чек за
# коробки лёг бы на «Food supplies», то есть демо показывало бы P&L, в котором
# упаковка сидит в себестоимости продуктов.
CASH_RECEIPT = [
    ("head", 0, "METRO CASH & CARRY d.o.o."),
    ("sub", 0, "Autoput za Novi Sad 120, Beograd  /  PIB 100 555 777"),
    ("rule", 0, ""),
    ("head", 0, "CASH RECEIPT  No. 004512"),
    ("body", 0, "Date: 09.08.2026   Time: 11:24"),
    ("body", 0, "Unit: Novi Sad Bulevar (NS1)"),
    ("rule", 0, ""),
    row("Mozzarella block 10 kg", "2", "2 400.00", "4 800.00"),
    row("Tomato sauce 5 l", "3", "650.00", "1 950.00"),
    row("Basil, fresh 100 g", "5", "300.00", "1 500.00"),
    ("rule", 0, ""),
    ("total", 0, f"{'PAID IN CASH, RSD':<{sum(COLUMNS) - 13}}{'8 250.00':>13}"),
    ("body", 0, "VAT 10% included:  750.00"),
    ("sub", 0, "Demo document. Not a real receipt."),
]


def main() -> int:
    FIXTURES.mkdir(parents=True, exist_ok=True)

    scan = FIXTURES / "delivery-note.png"
    scan.write_bytes(rasterise(pdf(DELIVERY_NOTE)))
    print(f"{scan.relative_to(ROOT)}: {scan.stat().st_size} байт")

    receipt = FIXTURES / "cash-receipt.pdf"
    receipt.write_bytes(pdf(CASH_RECEIPT))
    print(f"{receipt.relative_to(ROOT)}: {receipt.stat().st_size} байт")
    return 0


if __name__ == "__main__":
    sys.exit(main())
