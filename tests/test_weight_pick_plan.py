"""End-to-end tests for the /weight-pick-plan endpoint."""

from decimal import Decimal

from fastapi.testclient import TestClient

from app.main import app
from app.pickplan import Demand, plan

client = TestClient(app)

GS = "\x1d"
GTIN_A = "09501101530003"  # valid Mod-10 check digit
GTIN_B = "00000000000017"


def pick_plan(scans, demands, target_unit=None):
    body = {"scans": scans, "demands": demands}
    if target_unit is not None:
        body["target_unit"] = target_unit
    return client.post("/weight-pick-plan", json=body)


def scan(gtin=GTIN_A, expiry="250131", batch=None, weight="3103002513", extra=""):
    """Build an element-string scan with GTIN, expiry, optional batch, weight."""
    parts = [f"01{gtin}", f"17{expiry}"]
    if extra:
        parts.append(extra)
    if batch is not None:
        parts.append(f"10{batch}{GS}")
    parts.append(weight)
    return "".join(parts)


def demand(gtin=GTIN_A, quantity=1):
    return {"gtin": gtin, "quantity": quantity}


# ---------------------------------------------------------------- happy path


def test_fefo_picks_earliest_expiry_first_regardless_of_scan_order():
    resp = pick_plan(
        [
            scan(expiry="250228", batch="LATE", weight="3103002000"),
            scan(expiry="250131", batch="EARLY", weight="3103001000"),
        ],
        [demand(quantity=2.5)],
    )
    assert resp.status_code == 200
    (result,) = resp.json()["demands"]
    assert [a["batch"] for a in result["allocations"]] == ["EARLY", "LATE"]
    assert [a["quantity"] for a in result["allocations"]] == [1.0, 1.5]
    assert result["fulfilled"] == 2.5
    assert result["shortage"] == 0.0


def test_day_00_counts_as_last_day_of_month():
    # Feb 2025 has 28 days: 250200 -> 2025-02-28, after 250227, before 250301.
    resp = pick_plan(
        [
            scan(expiry="250301", batch="MAR"),
            scan(expiry="250200", batch="FEB00"),
            scan(expiry="250227", batch="FEB27"),
        ],
        [demand(quantity=10)],
    )
    assert resp.status_code == 200
    (result,) = resp.json()["demands"]
    assert [a["batch"] for a in result["allocations"]] == ["FEB27", "FEB00", "MAR"]


def test_same_expiry_day_keeps_first_appearance_order():
    resp = pick_plan(
        [
            scan(expiry="250131", batch="FIRST"),
            scan(expiry="250131", batch="SECOND"),
            scan(expiry="250131", batch="THIRD"),
        ],
        [demand(quantity=10)],
    )
    assert resp.status_code == 200
    (result,) = resp.json()["demands"]
    assert [a["batch"] for a in result["allocations"]] == ["FIRST", "SECOND", "THIRD"]


def test_day_00_ties_with_explicit_last_day_in_first_seen_order():
    # 250200 and 250228 are the same actual day (2025-02-28).
    resp = pick_plan(
        [
            scan(expiry="250228", batch="EXPLICIT"),
            scan(expiry="250200", batch="IMPLICIT"),
        ],
        [demand(quantity=10)],
    )
    assert resp.status_code == 200
    (result,) = resp.json()["demands"]
    assert [a["batch"] for a in result["allocations"]] == ["EXPLICIT", "IMPLICIT"]


def test_last_lot_may_be_partially_allocated():
    resp = pick_plan(
        [
            scan(expiry="250131", batch="B1", weight="3103003000"),
            scan(expiry="250228", batch="B2", weight="3103004000"),
        ],
        [demand(quantity=5)],
    )
    assert resp.status_code == 200
    (result,) = resp.json()["demands"]
    assert result["allocations"] == [
        {"batch": "B1", "expiry": "250131", "quantity": 3.0},
        {"batch": "B2", "expiry": "250228", "quantity": 2.0},
    ]
    assert result["fulfilled"] == 5.0
    assert result["shortage"] == 0.0


def test_shortage_when_stock_runs_out():
    resp = pick_plan(
        [scan(expiry="250131", batch="B1", weight="3103004000")],
        [demand(quantity=10)],
    )
    assert resp.status_code == 200
    (result,) = resp.json()["demands"]
    assert result["allocations"] == [
        {"batch": "B1", "expiry": "250131", "quantity": 4.0}
    ]
    assert result["fulfilled"] == 4.0
    assert result["shortage"] == 6.0


def test_empty_scans_produce_full_shortage():
    resp = pick_plan([], [demand(quantity=2.5), demand(gtin=GTIN_B, quantity=1)])
    assert resp.status_code == 200
    body = resp.json()
    assert body["target_unit"] == "kg"
    assert body["demands"] == [
        {
            "gtin": GTIN_A,
            "quantity": 2.5,
            "fulfilled": 0.0,
            "shortage": 2.5,
            "allocations": [],
        },
        {
            "gtin": GTIN_B,
            "quantity": 1.0,
            "fulfilled": 0.0,
            "shortage": 1.0,
            "allocations": [],
        },
    ]


def test_demand_without_matching_gtin_gets_full_shortage():
    resp = pick_plan([scan()], [demand(gtin=GTIN_B, quantity=3)])
    assert resp.status_code == 200
    (result,) = resp.json()["demands"]
    assert result["gtin"] == GTIN_B
    assert result["fulfilled"] == 0.0
    assert result["shortage"] == 3.0
    assert result["allocations"] == []


def test_stock_aggregated_by_gtin_batch_and_expiry():
    # Same GTIN+batch+expiry merges into one lot; a different expiry of the
    # same batch is a separate lot picked later.
    resp = pick_plan(
        [
            scan(expiry="250131", batch="B1", weight="3103001000"),
            scan(expiry="250131", batch="B1", weight="3103002000"),
            scan(expiry="250228", batch="B1", weight="3103004000"),
        ],
        [demand(quantity=10)],
    )
    assert resp.status_code == 200
    (result,) = resp.json()["demands"]
    assert result["allocations"] == [
        {"batch": "B1", "expiry": "250131", "quantity": 3.0},
        {"batch": "B1", "expiry": "250228", "quantity": 4.0},
    ]


def test_multiple_demands_served_independently():
    resp = pick_plan(
        [
            scan(gtin=GTIN_A, expiry="250131", weight="3103001000"),
            scan(gtin=GTIN_B, expiry="250228", weight="3103002000"),
        ],
        [demand(gtin=GTIN_A, quantity=1), demand(gtin=GTIN_B, quantity=1.5)],
    )
    assert resp.status_code == 200
    results = resp.json()["demands"]
    assert [r["gtin"] for r in results] == [GTIN_A, GTIN_B]
    assert results[0]["fulfilled"] == 1.0
    assert results[1]["fulfilled"] == 1.5
    assert results[1]["shortage"] == 0.0


def test_scans_without_batch_allocate_with_null_batch():
    resp = pick_plan([scan(weight="3103002513")], [demand(quantity=2)])
    assert resp.status_code == 200
    (result,) = resp.json()["demands"]
    assert result["allocations"] == [
        {"batch": None, "expiry": "250131", "quantity": 2.0}
    ]


def test_ai_21_is_allowed_but_ignored():
    resp = pick_plan(
        [f"01{GTIN_A}1725013110B1{GS}21SN0001{GS}3103002513"],
        [demand(quantity=2.513)],
    )
    assert resp.status_code == 200
    (result,) = resp.json()["demands"]
    assert result["fulfilled"] == 2.513
    assert result["allocations"][0]["batch"] == "B1"


def test_digital_link_scans_accepted():
    resp = pick_plan(
        ["https://id.gs1.org/01/09501101530003/17/250131/3103/002513?10=B1"],
        [demand(quantity=2.513)],
    )
    assert resp.status_code == 200
    (result,) = resp.json()["demands"]
    assert result["fulfilled"] == 2.513
    assert result["allocations"] == [
        {"batch": "B1", "expiry": "250131", "quantity": 2.513}
    ]


def test_default_target_unit_is_kg():
    resp = pick_plan([scan()], [demand(quantity=1)])
    assert resp.status_code == 200
    assert resp.json()["target_unit"] == "kg"


def test_lb_stock_allocated_against_kg_demand():
    # 25.13 lb * 0.45359237 = 11.3987762581 kg available
    resp = pick_plan([scan(weight="3202002513")], [demand(quantity=11)])
    assert resp.status_code == 200
    (result,) = resp.json()["demands"]
    assert result["fulfilled"] == 11.0
    assert result["allocations"][0]["quantity"] == 11.0
    # 11.3987762581 - 11 = 0.3987762581 remains unallocated, no shortage
    assert result["shortage"] == 0.0


def test_kg_stock_allocated_against_lb_demand():
    # 1 kg = 2.2046226218... lb; demand 2 lb is fully covered.
    resp = pick_plan([scan(weight="3100000001")], [demand(quantity=2)], "lb")
    assert resp.status_code == 200
    body = resp.json()
    assert body["target_unit"] == "lb"
    (result,) = body["demands"]
    assert result["fulfilled"] == 2.0
    assert result["shortage"] == 0.0


def test_mixed_unit_stock_converted_before_allocation():
    # 2.513 kg + 25.13 lb = 13.9117762581 kg; demand 13.9 kg is covered.
    resp = pick_plan(
        [
            scan(expiry="250131", weight="3103002513"),
            scan(expiry="250131", weight="3202002513"),
        ],
        [demand(quantity=13.9)],
    )
    assert resp.status_code == 200
    (result,) = resp.json()["demands"]
    assert result["fulfilled"] == 13.9
    assert result["shortage"] == 0.0
    # both scans share GTIN/batch(None)/expiry -> one aggregated lot
    assert len(result["allocations"]) == 1
    assert result["allocations"][0]["quantity"] == 13.9


# ------------------------------------------------- exact Decimal arithmetic


def test_amounts_are_exact_decimals_before_response_rounding():
    result = plan(
        [scan(weight="3202002513")],
        "kg",
        [Demand(gtin=GTIN_A, quantity=Decimal("11"))],
    )
    (entry,) = result["demands"]
    assert entry["fulfilled"] == Decimal("11")
    assert entry["allocations"][0]["quantity"] == Decimal("11")
    assert not isinstance(entry["fulfilled"], float)


def test_tiny_weights_accumulate_exactly_for_allocation():
    # 0.0000004 kg per scan; three scans = 0.0000012 kg, demand 0.000001 kg.
    result = plan(
        [scan(weight="3107000004")] * 3,
        "kg",
        [Demand(gtin=GTIN_A, quantity=Decimal("0.000001"))],
    )
    (entry,) = result["demands"]
    assert entry["fulfilled"] == Decimal("0.000001")
    assert entry["shortage"] == Decimal("0")
    assert entry["allocations"][0]["quantity"] == Decimal("0.000001")


def test_amounts_rendered_as_six_place_json_numbers():
    resp = pick_plan([scan(weight="3103002513")], [demand(quantity=2.513)])
    text = resp.text
    assert '"quantity":2.513000' in text
    assert '"fulfilled":2.513000' in text
    assert '"shortage":0.000000' in text
    assert '"quantity":"2.513000"' not in text


def test_half_even_rounding_only_at_response():
    # demand 0.0000005 kg fulfilled exactly -> renders half-even to 0.000000
    resp = pick_plan([scan(weight="3107000005")], [demand(quantity=0.0000005)])
    assert '"fulfilled":0.000000' in resp.text
    # 0.0000015 -> half-even rounds up to 0.000002
    resp = pick_plan([scan(weight="3107000015")], [demand(quantity=0.0000015)])
    assert '"fulfilled":0.000002' in resp.text


def test_quantity_echoed_with_six_places():
    resp = pick_plan([], [demand(quantity=5)])
    assert '"quantity":5.000000' in resp.text
    assert resp.json()["demands"][0]["quantity"] == 5.0


# ---------------------------------------------------------------- scan errors


def test_parser_error_reports_scan_index_and_original_error():
    resp = pick_plan([scan(), "0109501101530004"], [demand(quantity=1)])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 1
    assert detail["error"]["error"] == "INVALID_CHECK_DIGIT"


def test_missing_expiry_rejected():
    resp = pick_plan([f"01{GTIN_A}10B1{GS}3103002513"], [demand(quantity=1)])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 0
    assert detail["error"]["error"] == "MISSING_AI"
    assert "17" in detail["error"]["message"]


def test_duplicate_expiry_rejected():
    resp = pick_plan(
        [f"01{GTIN_A}17250131172502283103002513"], [demand(quantity=1)]
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 0
    assert detail["error"]["error"] == "DUPLICATE_AI"
    assert "17" in detail["error"]["message"]


def test_missing_gtin_rejected():
    resp = pick_plan(["172501313103002513"], [demand(quantity=1)])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 0
    assert detail["error"]["error"] == "MISSING_AI"
    assert "01" in detail["error"]["message"]


def test_missing_weight_rejected():
    resp = pick_plan([f"01{GTIN_A}17250131"], [demand(quantity=1)])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 0
    assert detail["error"]["error"] == "MISSING_AI"
    assert "310n/320n" in detail["error"]["message"]


def test_duplicate_weight_rejected():
    resp = pick_plan(
        [f"01{GTIN_A}1725013131030025133202002513"], [demand(quantity=1)]
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 0
    assert detail["error"]["error"] == "DUPLICATE_AI"
    assert "3103" in detail["error"]["found"]
    assert "3202" in detail["error"]["found"]


def test_duplicate_batch_rejected():
    resp = pick_plan(
        [f"01{GTIN_A}1725013110A{GS}10B{GS}3103002513"], [demand(quantity=1)]
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 0
    assert detail["error"]["error"] == "DUPLICATE_AI"


def test_invalid_expiry_date_surfaces_parser_error():
    resp = pick_plan([scan(expiry="251332")], [demand(quantity=1)])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 0
    assert detail["error"]["error"] == "INVALID_DATE"


def test_first_failing_scan_index_reported():
    resp = pick_plan(
        [scan(), scan(), "99ABC"],
        [demand(quantity=1)],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["scan_index"] == 2
    assert detail["error"]["error"] == "UNSUPPORTED_AI"


# -------------------------------------------------------------- demand errors


def test_duplicate_demand_gtin_rejected():
    resp = pick_plan(
        [scan()],
        [demand(quantity=1), demand(quantity=2)],
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["demand_index"] == 1
    assert detail["error"]["error"] == "DUPLICATE_GTIN"
    assert detail["error"]["found"] == GTIN_A


def test_invalid_demand_gtin_check_digit_rejected():
    resp = pick_plan([scan()], [demand(gtin="09501101530004", quantity=1)])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["demand_index"] == 0
    assert detail["error"]["error"] == "INVALID_CHECK_DIGIT"


def test_malformed_demand_gtin_rejected():
    resp = pick_plan([scan()], [demand(gtin="123", quantity=1)])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["demand_index"] == 0
    assert detail["error"]["error"] == "FIELD_TOO_SHORT"


def test_zero_and_negative_quantity_rejected():
    for bad in (0, -1):
        resp = pick_plan([scan()], [demand(quantity=bad)])
        assert resp.status_code == 422


# ---------------------------------------------------- request model validation


def test_missing_demands_field_is_422():
    resp = client.post("/weight-pick-plan", json={"scans": []})
    assert resp.status_code == 422


def test_missing_scans_field_is_422():
    resp = client.post(
        "/weight-pick-plan", json={"demands": [demand(quantity=1)]}
    )
    assert resp.status_code == 422


def test_unknown_target_unit_is_422():
    resp = pick_plan([scan()], [demand(quantity=1)], target_unit="stone")
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "target_unit"]


def test_non_string_scan_entry_is_422():
    resp = client.post(
        "/weight-pick-plan",
        json={"scans": [123], "demands": [demand(quantity=1)]},
    )
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "scans", 0]


def test_empty_demands_list_returns_empty_plan():
    resp = pick_plan([scan()], [])
    assert resp.status_code == 200
    assert resp.json() == {"target_unit": "kg", "demands": []}
