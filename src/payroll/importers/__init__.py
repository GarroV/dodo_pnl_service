"""Импортеры данных партнёров. Формат у каждого свой."""

from .plata_xlsx import Finding, ImportedRow, PlataFile, read_plata, read_plata_file

__all__ = ["read_plata", "read_plata_file", "ImportedRow", "PlataFile", "Finding"]
