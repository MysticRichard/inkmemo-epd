from __future__ import annotations

from datetime import datetime
from pathlib import Path

from openpyxl import load_workbook

from ..models import Grid, MergeRange


def read_xlsx(path: Path) -> Grid:
    workbook = load_workbook(path, read_only=False, data_only=True)
    try:
        sheet = workbook.active
        values = [list(row) for row in sheet.iter_rows(values_only=True)]
        max_row = len(values)
        max_col = max((len(row) for row in values), default=0)
        merges = [
            MergeRange(
                row_from=merged.min_row - 1,
                row_to=merged.max_row - 1,
                col_from=merged.min_col - 1,
                col_to=merged.max_col - 1,
            )
            for merged in sheet.merged_cells.ranges
        ]
        return Grid(
            values=values,
            merges=[merge for merge in merges if merge.row_from < max_row and merge.col_from < max_col],
            document_title=path.stem,
            sheet_name=sheet.title,
            fetched_at=datetime.now().astimezone(),
        )
    finally:
        workbook.close()
