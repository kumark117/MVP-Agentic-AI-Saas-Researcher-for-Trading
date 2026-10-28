"""CSV formula-injection neutraliser (R035; security checklist section 16).

A cell that a spreadsheet could execute (starting with =, +, -, @, tab or carriage return)
is prefixed with a single quote so it is shown as text. Numbers produced by our own code
are written as plain decimals and left alone.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Sequence
from decimal import Decimal
from typing import Any

DANGEROUS = ("=", "+", "-", "@", "\t", "\r")


def neutralise(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
        return str(value)  # trusted numeric, may be negative (e.g. a debit)
    s = str(value)
    return "'" + s if s.startswith(DANGEROUS) else s


def to_csv(header: Sequence[str], rows: Iterable[Sequence[Any]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, quoting=csv.QUOTE_MINIMAL, lineterminator="\r\n")
    w.writerow([neutralise(h) for h in header])
    for row in rows:
        w.writerow([neutralise(v) for v in row])
    return buf.getvalue().encode("utf-8-sig")  # BOM: spreadsheets detect UTF-8
