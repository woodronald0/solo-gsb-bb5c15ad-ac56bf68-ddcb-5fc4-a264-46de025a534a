"""FEFO (first-expired, first-out) pick planning across parsed GS1 scans.

Each scan is fed through :func:`app.gs1.parse`; it must then contain:

* exactly one AI 01 (GTIN),
* exactly one AI 17 (expiry date),
* zero or one AI 10 (batch/lot) -- optional,
* exactly one weight element of the 310n (kg) / 320n (lb) family.

Any parser failure, missing/duplicated required AI or multiple weight
elements make the whole request fail with :class:`PickPlanError` whose
``detail`` names the offending scan index.  Valid scans are aggregated
into lots keyed by (GTIN, batch, expiry) in first-seen order; weights
are rebuilt with :class:`decimal.Decimal` straight from the original six
digits and the precision digit -- the float ``decimal_value`` produced
by the parser is never used for arithmetic.

Demands (one per GTIN; a repeated GTIN is rejected) are then fulfilled
in FEFO order: lots are sorted by the actual AI 17 date ascending --
day ``00`` denotes the last day of the month -- and lots sharing a date
keep their first-appearance order (the sort is stable).  The final lot
of a demand may be allocated partially.  All conversion, aggregation,
comparison and allocation arithmetic runs on :class:`Decimal` with the
fixed conversion factors from :mod:`app.summary`; rounding to six
places happens only when the HTTP response is rendered.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, localcontext

from .gs1 import GS1ParseError, Element, decode_weight, parse
from .summary import ARITHMETIC_PRECISION, ScanValidationError, _to_target

REQUIRED_AI_TEXT = (
    "exactly one AI 01 (GTIN), exactly one AI 17 (expiry date), "
    "at most one AI 10 (batch/lot), and exactly one weight AI 310n/320n"
)


class PickPlanError(Exception):
    """A scan or demand failed validation.

    ``detail`` is the complete 422 payload: ``scan_index`` (or
    ``demand_index``) plus the original parser/validation error under
    ``error``.
    """

    def __init__(self, detail: dict):
        super().__init__(detail["error"]["message"])
        self.detail = detail


@dataclass(frozen=True)
class Demand:
    """One requested pick: ``quantity`` of ``gtin`` in the target unit."""

    gtin: str
    quantity: Decimal


@dataclass
class _Lot:
    """Available weight aggregated per (GTIN, batch, expiry)."""

    gtin: str
    batch: str | None
    expiry: str  # raw AI 17 value, YYMMDD
    day: date  # actual expiry date (day "00" resolved to end of month)
    available: Decimal  # in the target unit


def _scan_failure(index: int, error: GS1ParseError) -> PickPlanError:
    """Wrap a parser/validation error with the offending scan index."""
    return PickPlanError({"scan_index": index, "error": error.to_dict()})


def _demand_failure(index: int, error: dict) -> PickPlanError:
    """Wrap a demand-level error with the offending demand index."""
    return PickPlanError({"demand_index": index, "error": error})


def _extract(
    index: int, elements: list[Element]
) -> tuple[str, str | None, str, Decimal, str]:
    """Pull (GTIN, batch or None, expiry, exact weight, weight unit)."""
    gtins = [e for e in elements if e.ai == "01"]
    expiries = [e for e in elements if e.ai == "17"]
    batches = [e for e in elements if e.ai == "10"]
    weights = [e for e in elements if e.unit is not None]

    if len(gtins) != 1:
        if not gtins:
            raise ScanValidationError(
                "MISSING_AI",
                f"scan {index}: missing required AI 01 (GTIN)",
                expected=REQUIRED_AI_TEXT,
            )
        raise ScanValidationError(
            "DUPLICATE_AI",
            f"scan {index}: AI 01 (GTIN) appears {len(gtins)} times; exactly one is required",
            expected="exactly one AI 01 (GTIN)",
            found=f"{len(gtins)} AI 01 elements",
            offset=gtins[1].offset,
        )

    if len(expiries) != 1:
        if not expiries:
            raise ScanValidationError(
                "MISSING_AI",
                f"scan {index}: missing required AI 17 (expiry date)",
                expected=REQUIRED_AI_TEXT,
            )
        raise ScanValidationError(
            "DUPLICATE_AI",
            f"scan {index}: AI 17 (expiry date) appears {len(expiries)} times; "
            "exactly one is required",
            expected="exactly one AI 17 (expiry date)",
            found=f"{len(expiries)} AI 17 elements",
            offset=expiries[1].offset,
        )

    if len(batches) > 1:
        raise ScanValidationError(
            "DUPLICATE_AI",
            f"scan {index}: AI 10 (batch/lot) appears {len(batches)} times; at most one is allowed",
            expected="at most one AI 10 (batch/lot)",
            found=f"{len(batches)} AI 10 elements",
            offset=batches[1].offset,
        )

    if len(weights) != 1:
        if not weights:
            raise ScanValidationError(
                "MISSING_AI",
                f"scan {index}: missing required weight AI 310n/320n",
                expected=REQUIRED_AI_TEXT,
            )
        ais = ", ".join(e.ai for e in weights)
        raise ScanValidationError(
            "DUPLICATE_AI",
            f"scan {index}: {len(weights)} weight elements ({ais}); "
            "exactly one 310n/320n is required",
            expected="exactly one weight AI 310n (kg) or 320n (lb)",
            found=ais,
            offset=weights[1].offset,
        )

    weight = weights[0]
    value = decode_weight(weight.value, int(weight.ai[3]))
    return (
        gtins[0].value,
        batches[0].value if batches else None,
        expiries[0].value,
        value,
        weight.unit,
    )


def _expiry_date(yymmdd: str) -> date:
    """Actual AI 17 date; day ``00`` resolves to the last day of the month.

    The parser has already validated the date, so only the GS1 day-00
    convention needs handling here.
    """
    year = 2000 + int(yymmdd[0:2])
    month = int(yymmdd[2:4])
    day = int(yymmdd[4:6])
    if day == 0:
        day = calendar.monthrange(year, month)[1]
    return date(year, month, day)


def _validate_demands(demands: list[Demand]) -> None:
    """Check every demand GTIN with the parser and reject duplicates."""
    seen: set[str] = set()
    for index, demand in enumerate(demands):
        try:
            parse(f"01{demand.gtin}")
        except GS1ParseError as exc:
            raise _demand_failure(index, exc.to_dict()) from exc
        if demand.gtin in seen:
            raise _demand_failure(
                index,
                {
                    "error": "DUPLICATE_GTIN",
                    "message": (
                        f"demand {index}: GTIN '{demand.gtin}' is requested by "
                        "more than one demand; merge the quantities into a "
                        "single demand"
                    ),
                    "expected": "each demand GTIN to appear at most once",
                    "found": demand.gtin,
                },
            )
        seen.add(demand.gtin)


def plan(scans: list[str], target_unit: str, demands: list[Demand]) -> dict:
    """Aggregate stock from ``scans`` and allocate it to ``demands`` FEFO-style.

    Returns ``{"target_unit": ..., "demands": [{gtin, quantity, fulfilled,
    shortage, allocations}, ...]}`` in demand order.  ``allocations`` lists
    ``{batch, expiry, quantity}`` per lot in pick order.  Every amount is an
    unrounded :class:`Decimal` already expressed in the target unit.
    """
    lots: dict[str, list[_Lot]] = {}  # GTIN -> lots in first-seen order
    by_key: dict[tuple[str, str | None, str], _Lot] = {}

    with localcontext() as ctx:
        ctx.prec = ARITHMETIC_PRECISION

        for index, scan in enumerate(scans):
            try:
                elements = parse(scan)
            except GS1ParseError as exc:
                raise _scan_failure(index, exc) from exc
            try:
                gtin, batch, expiry, weight, unit = _extract(index, elements)
            except ScanValidationError as exc:
                raise _scan_failure(index, exc) from exc

            key = (gtin, batch, expiry)
            lot = by_key.get(key)
            if lot is None:
                lot = _Lot(gtin, batch, expiry, _expiry_date(expiry), Decimal(0))
                by_key[key] = lot
                lots.setdefault(gtin, []).append(lot)
            lot.available += _to_target(weight, unit, target_unit)

        # FEFO: actual expiry date ascending; the stable sort keeps
        # first-appearance order for lots expiring on the same day.
        for gtin_lots in lots.values():
            gtin_lots.sort(key=lambda lot: lot.day)

        _validate_demands(demands)

        results = []
        for demand in demands:
            remaining = demand.quantity
            allocations = []
            for lot in lots.get(demand.gtin, []):
                if remaining <= 0:
                    break
                if lot.available <= 0:
                    continue
                taken = min(remaining, lot.available)
                lot.available -= taken
                remaining -= taken
                allocations.append(
                    {"batch": lot.batch, "expiry": lot.expiry, "quantity": taken}
                )
            results.append(
                {
                    "gtin": demand.gtin,
                    "quantity": demand.quantity,
                    "fulfilled": demand.quantity - remaining,
                    "shortage": remaining,
                    "allocations": allocations,
                }
            )

    return {"target_unit": target_unit, "demands": results}
