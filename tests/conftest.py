from pathlib import Path

import pytest

from payroll import PayrollEngine, load_preset
from payroll.importers import read_plata

FIXTURES = Path(__file__).parent / "fixtures"
PLATA_JUNE = FIXTURES / "plata-2026-06.xlsx"


@pytest.fixture(scope="session")
def serbia_preset() -> dict:
    return load_preset("serbia-2026")


@pytest.fixture(scope="session")
def engine(serbia_preset) -> PayrollEngine:
    return PayrollEngine(serbia_preset)


@pytest.fixture(scope="session")
def june_rows():
    """Зарплатная таблица Сербии за июнь 2026 — эталон для сверки."""
    if not PLATA_JUNE.exists():
        pytest.skip(f"нет фикстуры {PLATA_JUNE}")
    return read_plata(PLATA_JUNE)
