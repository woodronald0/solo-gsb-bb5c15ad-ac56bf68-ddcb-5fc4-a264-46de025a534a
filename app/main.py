"""FastAPI application exposing the GS1 element-string parser."""

from __future__ import annotations

import json
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .gs1 import GS1ParseError, parse
from .pickplan import Demand, PickPlanError, plan
from .summary import WeightSummaryError, summarize

app = FastAPI(
    title="GS1 Element String Parser",
    version="1.4.0",
    description=(
        "Parses GS1 input into structured, ordered AI elements. Accepts raw "
        "scan strings (FNC1 / ASCII 29 separated) and GS1 Digital Link URIs "
        "(http/https, AI/value pairs in path and query string). Supports AI "
        "01 (GTIN), 10 (batch/lot), 17 (expiry date), 21 (serial number) and "
        "the weight family 310n (net weight, kg) / 320n (net weight, lb), "
        "where n is 0-9 implied decimal places followed by a fixed 6-digit "
        "value; weight elements additionally carry decimal_value and unit. "
        "POST /weight-summary aggregates the net weights of many scans, "
        "grouped by GTIN and batch, using exact Decimal arithmetic. "
        "POST /weight-pick-plan aggregates stock by GTIN, batch and expiry "
        "and allocates it to per-GTIN demands in FEFO order."
    ),
)


class ParseRequest(BaseModel):
    data: str = Field(
        ...,
        title="Raw scan string or GS1 Digital Link URI",
        description=(
            "Either a raw GS1 element string as scanned (variable-length "
            "fields separated by ASCII 29, encoded in JSON as , or a "
            "GS1 Digital Link URI beginning with http:// or https://. "
            "Digital Link values are percent-decoded as UTF-8; path segments "
            "must form AI/value pairs; each AI may appear only once across "
            "path and query string."
        ),
        examples=[
            "01095011015300031725013110ABC12321SN0001",
            "https://id.gs1.org/01/09501101530003/17/250131?10=ABC123&21=SN0001",
        ],
    )


class Element(BaseModel):
    ai: str = Field(description="Application identifier, e.g. '01'")
    name: str = Field(description="Short element name, e.g. 'GTIN'")
    value: str = Field(
        description="Field value without AI and separators (raw digits for "
        "310n/320n, before applying the implied decimal point)"
    )
    offset: int = Field(description="Byte offset of the AI within the input")
    decimal_value: float | None = Field(
        default=None,
        description=(
            "310n/320n only: the 6-digit value interpreted with n implied "
            "decimal places. Absent for all other AIs."
        ),
    )
    unit: str | None = Field(
        default=None,
        description='310n/320n only: measurement unit, "kg" (310n) or "lb" (320n). '
        "Absent for all other AIs.",
    )


class ParseResponse(BaseModel):
    elements: list[Element]


class WeightSummaryRequest(BaseModel):
    scans: list[str] = Field(
        ...,
        description=(
            "Raw GS1 scan strings and/or GS1 Digital Link URIs, one per item. "
            "Each must contain exactly one AI 01, at most one AI 10 and "
            "exactly one weight AI 310n/320n."
        ),
        examples=[
            [
                "010950110153000310B13103002513",
                "010950110153000310B13202002513",
            ]
        ],
    )
    target_unit: Literal["kg", "lb"] = Field(
        default="kg",
        description='Unit the group totals are converted to: "kg" or "lb" (default "kg").',
    )


class WeightGroup(BaseModel):
    """OpenAPI shape of one group; totals are rendered as exact JSON numbers."""

    gtin: str = Field(description="AI 01 GTIN-14 shared by the group")
    batch: str | None = Field(
        description="AI 10 batch/lot; null for scans that carried no batch"
    )
    count: int = Field(description="Number of scans aggregated into the group")
    unit: Literal["kg", "lb"] = Field(description="Unit of total (the target unit)")
    total: float = Field(
        description=(
            "Sum of net weights converted to the target unit, rounded to six "
            "decimal places with round-half-even. Rendered as a JSON number "
            "(e.g. 2.513000); computed internally with Decimal, never float."
        )
    )


class WeightSummaryResponse(BaseModel):
    target_unit: Literal["kg", "lb"]
    groups: list[WeightGroup]


class DemandIn(BaseModel):
    gtin: str = Field(
        ...,
        description="AI 01 GTIN-14 to pick; validated with the same Mod-10 "
        "check digit rule as scanned GTINs. Each GTIN may appear in at most "
        "one demand.",
        examples=["09501101530003"],
    )
    quantity: Decimal = Field(
        ...,
        gt=0,
        description="Required net weight in the target unit. Computed with "
        "Decimal internally; rendered with six decimal places.",
        examples=[5.0],
    )


class WeightPickPlanRequest(BaseModel):
    scans: list[str] = Field(
        ...,
        description=(
            "Raw GS1 scan strings and/or GS1 Digital Link URIs, one per item. "
            "Each must contain exactly one AI 01, exactly one AI 17, at most "
            "one AI 10 and exactly one weight AI 310n/320n."
        ),
        examples=[
            [
                "01095011015300031725013110B13103002513",
                "01095011015300031725022810B23103004000",
            ]
        ],
    )
    target_unit: Literal["kg", "lb"] = Field(
        default="kg",
        description='Unit all amounts are converted to: "kg" or "lb" (default "kg").',
    )
    demands: list[DemandIn] = Field(
        ...,
        description="One demand per GTIN; a repeated GTIN is rejected with 422.",
    )


class PickAllocation(BaseModel):
    """OpenAPI shape of one allocated lot; amounts are exact JSON numbers."""

    batch: str | None = Field(
        description="AI 10 batch/lot of the allocated lot; null when the "
        "scans carried no batch"
    )
    expiry: str = Field(description="Raw AI 17 value (YYMMDD) of the lot")
    quantity: float = Field(
        description="Weight allocated from this lot, in the target unit, "
        "rounded to six decimal places with round-half-even."
    )


class PickDemandResult(BaseModel):
    gtin: str = Field(description="GTIN-14 of the demand")
    quantity: float = Field(description="Requested weight in the target unit")
    fulfilled: float = Field(description="Allocated weight in the target unit")
    shortage: float = Field(
        description="Unallocated remainder: quantity - fulfilled (0 when the "
        "demand is fully covered)"
    )
    allocations: list[PickAllocation] = Field(
        description="Lots picked in FEFO order (actual AI 17 date ascending; "
        "day 00 counts as the last day of the month; same-day lots keep "
        "first-appearance order). The final lot may be partially allocated."
    )


class WeightPickPlanResponse(BaseModel):
    target_unit: Literal["kg", "lb"]
    demands: list[PickDemandResult]


SIX_PLACES = Decimal("0.000001")


class _RawNumber(str):
    """Fixed-point numeric text, e.g. '2.513000', emitted verbatim as JSON."""


def _dump(value: Any) -> str:
    """JSON serializer that splices :class:`_RawNumber` tokens raw.

    The stdlib encoder stringifies Decimal and FastAPI's jsonable_encoder
    turns it into a float; totals must instead appear as exact six-place
    JSON numbers such as ``2.513000``, so they are pre-formatted and
    spliced in here as raw tokens.
    """
    if isinstance(value, _RawNumber):
        return str(value)
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ",".join(_dump(v) for v in value) + "]"
    if isinstance(value, dict):
        return (
            "{"
            + ",".join(
                f"{json.dumps(str(k), ensure_ascii=False)}:{_dump(v)}"
                for k, v in value.items()
            )
            + "}"
        )
    raise TypeError(f"not JSON serializable: {type(value)!r}")


def _build_summary_response(result: dict) -> Response:
    """Quantize totals half-even to six places and render exact JSON."""
    payload = {
        "target_unit": result["target_unit"],
        "groups": [
            {
                "gtin": group["gtin"],
                "batch": group["batch"],
                "count": group["count"],
                "unit": group["unit"],
                "total": _RawNumber(
                    format(
                        group["total"].quantize(SIX_PLACES, rounding=ROUND_HALF_EVEN),
                        "f",
                    )
                ),
            }
            for group in result["groups"]
        ],
    }
    return Response(content=_dump(payload), media_type="application/json")


def _build_pick_plan_response(result: dict) -> Response:
    """Quantize all amounts half-even to six places and render exact JSON."""

    def amount(value: Decimal) -> _RawNumber:
        return _RawNumber(
            format(value.quantize(SIX_PLACES, rounding=ROUND_HALF_EVEN), "f")
        )

    payload = {
        "target_unit": result["target_unit"],
        "demands": [
            {
                "gtin": demand["gtin"],
                "quantity": amount(demand["quantity"]),
                "fulfilled": amount(demand["fulfilled"]),
                "shortage": amount(demand["shortage"]),
                "allocations": [
                    {
                        "batch": allocation["batch"],
                        "expiry": allocation["expiry"],
                        "quantity": amount(allocation["quantity"]),
                    }
                    for allocation in demand["allocations"]
                ],
            }
            for demand in result["demands"]
        ],
    }
    return Response(content=_dump(payload), media_type="application/json")


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/parse", response_model=ParseResponse, response_model_exclude_none=True)
def parse_scan(req: ParseRequest) -> ParseResponse:
    try:
        elements = parse(req.data)
    except GS1ParseError as exc:
        raise HTTPException(status_code=422, detail=exc.to_dict()) from exc
    return ParseResponse(elements=[Element(**vars(e)) for e in elements])


@app.post(
    "/weight-summary",
    response_model=WeightSummaryResponse,
)
def weight_summary(req: WeightSummaryRequest) -> Response:
    try:
        result = summarize(req.scans, req.target_unit)
    except WeightSummaryError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc
    return _build_summary_response(result)


@app.post(
    "/weight-pick-plan",
    response_model=WeightPickPlanResponse,
)
def weight_pick_plan(req: WeightPickPlanRequest) -> Response:
    try:
        result = plan(
            req.scans,
            req.target_unit,
            [Demand(gtin=d.gtin, quantity=d.quantity) for d in req.demands],
        )
    except PickPlanError as exc:
        raise HTTPException(status_code=422, detail=exc.detail) from exc
    return _build_pick_plan_response(result)
