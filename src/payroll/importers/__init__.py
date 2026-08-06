"""Импортеры данных партнёров. Формат у каждого свой."""

from .plata_xlsx import ImportedRow, read_plata

__all__ = ["read_plata", "ImportedRow"]
