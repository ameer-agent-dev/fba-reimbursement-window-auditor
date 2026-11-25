#!/usr/bin/env python3
"""Deterministic Amazon FBA reimbursement window reconciliation engine.

Zero external dependencies (Python 3 standard library only).
Reads local Amazon Seller Central TSV/CSV exports (Inventory Adjustments /
Ledger Detail, Reimbursements, and optional Customer Returns, Refunds, and
Sourcing Costs), nets Lost vs. Found units and prior reimbursements, enforces
Amazon claim windows from references/fba_policy_windows.yaml, and writes:
  - fba_reimbursement_audit.csv
  - fba_audit_summary.md
  - seller_support_cases/case_<type>_<batch>.md

Exit codes:
  0 - Reconciliation completed cleanly
  2 - Missing required input files or invalid arguments
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any


DEFAULT_POLICY: dict[str, Any] = {
    "policy_version": "2026-amazon-fba-reimbursement-windows",
    "urgent_days_remaining": 14,
    "max_ids_per_case": 5,
    "windows": {
        "fc_lost_damaged": {
            "label": "Fulfillment Center Lost or Damaged",
            "min_days": 1,
            "max_days": 60,
            "policy_reference": "FBA Inventory Reimbursement Policy - Fulfillment Center Operations (60-day claim window)",
        },
        "customer_return": {
            "label": "Unreturned Customer Refund",
            "min_days": 45,
            "max_days": 105,
            "policy_reference": "FBA Customer Returns Reimbursement Policy (45-to-105-day claim window)",
        },
        "removal": {
            "label": "FBA Removal Order Discrepancy",
            "min_days": 15,
            "max_days": 75,
            "policy_reference": "FBA Removal Order Reimbursement Policy (15-to-75-day claim window)",
        },
    },
}

HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "fnsku": ("fnsku", "fn_sku", "fulfillment_network_sku", "asin"),
    "msku": ("msku", "sku", "merchant_sku", "seller_sku"),
    "transaction_item_id": (
        "transaction_item_id",
        "transaction_id",
        "reference_id",
        "amazon_order_id",
        "order_id",
        "shipment_id",
    ),
    "reason_code": ("reason_code", "reason", "adjustment_reason", "event_type", "disposition"),
    "quantity": ("quantity", "quantity_adjusted", "qty", "units"),
    "qty_reimb_total": ("quantity_reimbursed_total",),
    "qty_reimb_cash": ("quantity_reimbursed_cash",),
    "qty_reimb_inv": ("quantity_reimbursed_inventory",),
    "event_date": (
        "event_date",
        "adjusted_date",
        "date",
        "approval_date",
        "refund_date",
        "return_date",
        "posted_date",
    ),
    "fulfillment_center_id": (
        "fulfillment_center_id",
        "fulfillment_center",
        "fc",
        "warehouse_id",
        "location",
    ),
    "amount_per_unit": ("amount_per_unit", "unit_cost_usd", "unit_cost", "cost", "manufacturing_cost"),
    "amount_total": ("amount_total", "reimbursement_amount", "refund_amount"),
    "order_id": ("amazon_order_id", "order_id"),
}

LOST_CODES = {"M", "E"}
DAMAGED_CODES = {"D", "Q"}
FOUND_CODES = {"F"}
REMOVAL_CODES = {"R", "REMOVAL"}


def normalize_header(raw: str) -> str:
    cleaned = raw.strip().lstrip("\ufeff").lower()
    cleaned = re.sub(r"[^a-z0-9]+", "_", cleaned)
    return cleaned.strip("_")


def load_policy_yaml(path: Path | None) -> dict[str, Any]:
    """Parse the simple nested key-value YAML in references/fba_policy_windows.yaml."""
    policy = json.loads(json.dumps(DEFAULT_POLICY))
    if path is None or not path.is_file():
        return policy

    lines = path.read_text(encoding="utf-8").splitlines()
    in_windows = False
    current_window: str | None = None

    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        if ":" not in stripped:
            continue
        key, val = stripped.split(":", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")

        if indent == 0:
            current_window = None
            if key == "windows":
                in_windows = True
                continue
            in_windows = False
            if key in ("urgent_days_remaining", "max_ids_per_case") and val.isdigit():
                policy[key] = int(val)
            elif val:
                policy[key] = val
        elif in_windows and indent == 2 and not val:
            current_window = key
            policy["windows"].setdefault(current_window, {})
        elif in_windows and current_window and indent >= 4:
            if key in ("min_days", "max_days") and val.isdigit():
                policy["windows"][current_window][key] = int(val)
            else:
                policy["windows"][current_window][key] = val

    return policy


def read_delimited_file(path: Path) -> tuple[list[dict[str, str]], str]:
    """Read TSV or CSV with UTF-8/Latin-1 fallback and normalized header keys."""
    raw_bytes = path.read_bytes()
    text = ""
    encoding_used = "utf-8"
    for enc in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            text = raw_bytes.decode(enc)
            encoding_used = enc
            break
        except UnicodeDecodeError:
            continue

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return [], encoding_used

    header_line = lines[0]
    if header_line.count("\t") >= max(header_line.count(","), header_line.count(";")):
        delimiter = "\t"
    elif header_line.count(";") > header_line.count(","):
        delimiter = ";"
    else:
        delimiter = ","

    reader = csv.DictReader(lines, delimiter=delimiter)
    rows: list[dict[str, str]] = []
    for raw_row in reader:
        norm_row: dict[str, str] = {}
        for k, v in raw_row.items():
            if k is None:
                continue
            norm_row[normalize_header(k)] = (v or "").strip()
        if any(norm_row.values()):
            rows.append(norm_row)
    return rows, encoding_used


def pick_field(row: dict[str, str], canonical: str, default: str = "") -> str:
    for alias in HEADER_ALIASES.get(canonical, (canonical,)):
        val = row.get(alias, "")
        if val != "":
            return val
    return default


def parse_date(raw: str) -> date | None:
    text = raw.strip()
    if not text:
        return None
    # Strip trailing timezone or time component for ISO dates
    iso_match = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if iso_match:
        y, m, d = int(iso_match.group(1)), int(iso_match.group(2)), int(iso_match.group(3))
        return date(y, m, d)

    slash_match = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{4})", text)
    if slash_match:
        a, b, y = int(slash_match.group(1)), int(slash_match.group(2)), int(slash_match.group(3))
        if a > 12:
            return date(y, b, a)  # DD/MM/YYYY
        if b > 12:
            return date(y, a, b)  # MM/DD/YYYY
        return date(y, a, b)  # Default US MM/DD/YYYY

    for fmt in ("%d-%b-%Y", "%d %b %Y", "%b %d, %Y", "%Y%m%d"):
        try:
            return datetime.strptime(text.split("T")[0].strip(), fmt).date()
        except ValueError:
            continue
    return None


def parse_int(raw: str, default: int = 0) -> int:
    text = raw.strip().replace(",", "")
    if not text:
        return default
    try:
        return int(float(text))
    except ValueError:
        return default


def parse_float(raw: str, default: float = 0.0) -> float:
    text = re.sub(r"[^0-9.\-]", "", raw.strip())
    if not text:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def load_sourcing_costs(path: Path | None) -> dict[str, float]:
    costs: dict[str, float] = {}
    if path is None or not path.is_file():
        return costs
    rows, _ = read_delimited_file(path)
    for row in rows:
        cost_val = parse_float(pick_field(row, "amount_per_unit"), 0.0)
        if cost_val <= 0:
            continue
        fnsku = pick_field(row, "fnsku")
        msku = pick_field(row, "msku")
        if fnsku:
            costs[fnsku.upper()] = cost_val
        if msku:
            costs[msku.upper()] = cost_val
    return costs


def classify_window(
    claim_type: str,
    event_dt: date,
    audit_dt: date,
    policy: dict[str, Any],
) -> tuple[int, str, int, date]:
    win = policy["windows"][claim_type]
    min_days = int(win["min_days"])
    max_days = int(win["max_days"])
    urgent_thresh = int(policy.get("urgent_days_remaining", 14))

    days_elapsed = (audit_dt - event_dt).days
    days_until_expiry = max_days - days_elapsed
    unlock_date = event_dt + timedelta(days=min_days)

    if days_elapsed < min_days:
        status = "WAIT_TOO_EARLY"
    elif days_elapsed > max_days:
        status = "EXPIRED_WINDOW"
    elif days_until_expiry <= urgent_thresh:
        status = "FILE_NOW_URGENT"
    else:
        status = "FILE_NOW"
    return days_elapsed, status, days_until_expiry, unlock_date


def render_case_draft(
    template_text: str,
    claim_type: str,
    batch_num: int,
    items: list[dict[str, Any]],
    audit_dt: date,
    policy: dict[str, Any],
) -> str:
    win = policy["windows"][claim_type]
    total_units = sum(int(it["net_unclaimed_qty"]) for it in items)
    total_usd = sum(float(it["est_recovery_usd"]) for it in items)

    lines = [
        "| Transaction Item ID | FNSKU | MSKU | Event Date | Net Qty | Est. Recovery (USD) | Days Left | Status |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for it in items:
        lines.append(
            f"| {it['transaction_item_id']} | {it['fnsku']} | {it['msku']} | "
            f"{it['event_date']} | {it['net_unclaimed_qty']} | "
            f"${it['est_recovery_usd']:.2f} | {it['days_until_expiry']} | {it['window_status']} |"
        )
    table_md = "\n".join(lines)

    rendered = template_text
    replacements = {
        "{{CLAIM_TYPE_LABEL}}": str(win["label"]),
        "{{BATCH_NUM}}": f"{batch_num:02d}",
        "{{ITEM_COUNT}}": str(len(items)),
        "{{POLICY_REFERENCE}}": str(win["policy_reference"]),
        "{{AUDIT_DATE}}": audit_dt.isoformat(),
        "{{TOTAL_UNITS}}": str(total_units),
        "{{TOTAL_RECOVERY_USD}}": f"{total_usd:.2f}",
        "{{ITEMS_TABLE}}": table_md,
    }
    for k, v in replacements.items():
        rendered = rendered.replace(k, v)
    return rendered


def reconcile(
    adjustments_path: Path,
    reimbursements_path: Path,
    output_dir: Path,
    audit_dt: date,
    returns_path: Path | None = None,
    refunds_path: Path | None = None,
    costs_path: Path | None = None,
    policy_path: Path | None = None,
    template_path: Path | None = None,
    default_unit_cost: float = 0.0,
) -> dict[str, Any]:
    policy = load_policy_yaml(policy_path)
    costs = load_sourcing_costs(costs_path)
    warnings: list[str] = []

    adj_rows, adj_enc = read_delimited_file(adjustments_path)
    reimb_rows, reimb_enc = read_delimited_file(reimbursements_path)

    # Index already-reimbursed units by transaction_item_id, order_id, and fnsku
    reimb_by_tx: dict[str, int] = defaultdict(int)
    reimb_by_order: dict[str, int] = defaultdict(int)
    reimb_by_fnsku: dict[str, int] = defaultdict(int)
    reimb_unit_value_by_fnsku: dict[str, float] = {}

    for row in reimb_rows:
        qty = parse_int(pick_field(row, "qty_reimb_total"), 0)
        if qty == 0:
            qty = parse_int(pick_field(row, "qty_reimb_cash"), 0) + parse_int(
                pick_field(row, "qty_reimb_inv"), 0
            )
        if qty == 0:
            qty = abs(parse_int(pick_field(row, "quantity"), 0))
        if qty <= 0:
            continue

        tx_id = pick_field(row, "transaction_item_id")
        order_id = pick_field(row, "order_id")
        fnsku = pick_field(row, "fnsku").upper()
        reason = pick_field(row, "reason_code").lower()

        if order_id:
            reimb_by_order[order_id] += qty
        if tx_id and tx_id != order_id:
            reimb_by_tx[tx_id] += qty
        if fnsku and "customer" not in reason:
            reimb_by_fnsku[fnsku] += qty

        apu = parse_float(pick_field(row, "amount_per_unit"), 0.0)
        if apu > 0 and fnsku:
            reimb_unit_value_by_fnsku[fnsku] = apu

    # Parse inventory adjustments into lost, damaged, removal, and found pools
    lost_events: list[dict[str, Any]] = []
    damaged_events: list[dict[str, Any]] = []
    removal_events: list[dict[str, Any]] = []
    found_by_fc: dict[tuple[str, str], int] = defaultdict(int)

    for idx, row in enumerate(adj_rows, start=1):
        code = pick_field(row, "reason_code").strip().upper()
        fnsku = pick_field(row, "fnsku").upper()
        msku = pick_field(row, "msku")
        fc = pick_field(row, "fulfillment_center_id", "UNKNOWN").upper()
        tx_id = pick_field(row, "transaction_item_id")
        dt = parse_date(pick_field(row, "event_date"))
        raw_qty = parse_int(pick_field(row, "quantity"), 0)
        qty = abs(raw_qty)

        if not fnsku or qty == 0 or dt is None:
            if fnsku and qty != 0 and dt is None:
                warnings.append(f"Row {idx} in adjustments skipped: unparseable date.")
            continue

        event = {
            "fnsku": fnsku,
            "msku": msku,
            "fc": fc,
            "transaction_item_id": tx_id or f"UNASSIGNED-ROW-{idx}",
            "reason_code": code,
            "event_date": dt,
            "qty": qty,
        }
        if code in FOUND_CODES:
            found_by_fc[(fnsku, fc)] += qty
        elif code in LOST_CODES:
            lost_events.append(event)
        elif code in DAMAGED_CODES:
            damaged_events.append(event)
        elif code in REMOVAL_CODES:
            removal_events.append(event)

    # Sort events chronologically (oldest first) for deterministic FIFO netting
    lost_events.sort(key=lambda e: (e["event_date"], e["transaction_item_id"]))
    damaged_events.sort(key=lambda e: (e["event_date"], e["transaction_item_id"]))
    removal_events.sort(key=lambda e: (e["event_date"], e["transaction_item_id"]))

    # Step 1: Offset Lost (M, E) against Found (F) in same (fnsku, fc)
    for ev in lost_events:
        key = (ev["fnsku"], ev["fc"])
        avail = found_by_fc.get(key, 0)
        if avail > 0 and ev["qty"] > 0:
            offset = min(avail, ev["qty"])
            ev["qty"] -= offset
            found_by_fc[key] -= offset

    # Step 2: Apply any remaining Found (F) surplus across the same FNSKU (cross-FC finds)
    found_by_fnsku: dict[str, int] = defaultdict(int)
    for (fnsku, _fc), rem in found_by_fc.items():
        if rem > 0:
            found_by_fnsku[fnsku] += rem

    for ev in lost_events:
        if ev["qty"] <= 0:
            continue
        avail = found_by_fnsku.get(ev["fnsku"], 0)
        if avail > 0:
            offset = min(avail, ev["qty"])
            ev["qty"] -= offset
            found_by_fnsku[ev["fnsku"]] -= offset

    # Step 3: Offset Lost, Damaged, and Removal against already-paid reimbursements
    def apply_reimbursements(events: list[dict[str, Any]]) -> None:
        # Pass A: exact transaction_item_id match
        for ev in events:
            if ev["qty"] <= 0:
                continue
            tx = ev["transaction_item_id"]
            if tx and reimb_by_tx.get(tx, 0) > 0:
                offset = min(reimb_by_tx[tx], ev["qty"])
                ev["qty"] -= offset
                reimb_by_tx[tx] -= offset
                if reimb_by_fnsku.get(ev["fnsku"], 0) > 0:
                    reimb_by_fnsku[ev["fnsku"]] = max(0, reimb_by_fnsku[ev["fnsku"]] - offset)
        # Pass B: FNSKU-level reimbursement pool
        for ev in events:
            if ev["qty"] <= 0:
                continue
            avail = reimb_by_fnsku.get(ev["fnsku"], 0)
            if avail > 0:
                offset = min(avail, ev["qty"])
                ev["qty"] -= offset
                reimb_by_fnsku[ev["fnsku"]] -= offset

    apply_reimbursements(damaged_events)
    apply_reimbursements(lost_events)
    apply_reimbursements(removal_events)

    # Step 4: Optional unreturned customer refunds reconciliation
    customer_return_events: list[dict[str, Any]] = []
    if refunds_path is not None and refunds_path.is_file():
        returned_by_order: dict[str, int] = defaultdict(int)
        if returns_path is not None and returns_path.is_file():
            ret_rows, _ = read_delimited_file(returns_path)
            for row in ret_rows:
                oid = pick_field(row, "order_id") or pick_field(row, "transaction_item_id")
                rqty = abs(parse_int(pick_field(row, "quantity"), 1))
                if oid:
                    returned_by_order[oid] += rqty

        ref_rows, _ = read_delimited_file(refunds_path)
        for idx, row in enumerate(ref_rows, start=1):
            oid = pick_field(row, "order_id") or pick_field(row, "transaction_item_id")
            fnsku = pick_field(row, "fnsku").upper()
            msku = pick_field(row, "msku")
            dt = parse_date(pick_field(row, "event_date"))
            qty = abs(parse_int(pick_field(row, "quantity"), 1))
            if not oid or dt is None or qty <= 0:
                continue
            returned = returned_by_order.get(oid, 0)
            reimbursed = reimb_by_order.get(oid, 0) + reimb_by_tx.get(oid, 0)
            net_qty = max(0, qty - returned - reimbursed)
            if net_qty <= 0:
                continue
            refund_amt = parse_float(pick_field(row, "amount_total"), 0.0)
            unit_val = (refund_amt / qty) if refund_amt > 0 else 0.0
            customer_return_events.append(
                {
                    "fnsku": fnsku or msku.upper() or "UNKNOWN",
                    "msku": msku,
                    "transaction_item_id": oid,
                    "event_date": dt,
                    "qty": net_qty,
                    "override_unit_value": unit_val,
                }
            )

    # Build unified audit rows
    audit_rows: list[dict[str, Any]] = []

    def estimate_unit_cost(fnsku: str, msku: str, override_val: float = 0.0) -> float:
        if fnsku.upper() in costs:
            return costs[fnsku.upper()]
        if msku and msku.upper() in costs:
            return costs[msku.upper()]
        if override_val > 0:
            return override_val
        if fnsku.upper() in reimb_unit_value_by_fnsku:
            return reimb_unit_value_by_fnsku[fnsku.upper()]
        return default_unit_cost

    def append_events(events: list[dict[str, Any]], claim_type: str) -> None:
        for ev in events:
            if ev["qty"] <= 0:
                continue
            days_elapsed, status, days_left, unlock_dt = classify_window(
                claim_type, ev["event_date"], audit_dt, policy
            )
            unit_cost = estimate_unit_cost(
                ev["fnsku"], ev["msku"], float(ev.get("override_unit_value", 0.0))
            )
            est_usd = round(unit_cost * ev["qty"], 2)
            audit_rows.append(
                {
                    "claim_type": claim_type,
                    "fnsku": ev["fnsku"],
                    "msku": ev["msku"],
                    "transaction_item_id": ev["transaction_item_id"],
                    "event_date": ev["event_date"].isoformat(),
                    "days_elapsed": days_elapsed,
                    "window_status": status,
                    "net_unclaimed_qty": ev["qty"],
                    "est_recovery_usd": est_usd,
                    "days_until_expiry": days_left,
                    "earliest_unlock_date": unlock_dt.isoformat(),
                }
            )

    append_events(damaged_events, "fc_lost_damaged")
    append_events(lost_events, "fc_lost_damaged")
    append_events(removal_events, "removal")
    append_events(customer_return_events, "customer_return")

    # Sort audit rows: urgent first, then FILE_NOW, WAIT_TOO_EARLY, EXPIRED_WINDOW; highest USD first
    status_order = {
        "FILE_NOW_URGENT": 0,
        "FILE_NOW": 1,
        "WAIT_TOO_EARLY": 2,
        "EXPIRED_WINDOW": 3,
    }
    audit_rows.sort(
        key=lambda r: (
            status_order.get(r["window_status"], 9),
            -float(r["est_recovery_usd"]),
            int(r["days_until_expiry"]),
            r["transaction_item_id"],
        )
    )

    # Write outputs
    output_dir.mkdir(parents=True, exist_ok=True)
    cases_dir = output_dir / "seller_support_cases"
    cases_dir.mkdir(parents=True, exist_ok=True)

    csv_path = output_dir / "fba_reimbursement_audit.csv"
    csv_fields = [
        "claim_type",
        "fnsku",
        "msku",
        "transaction_item_id",
        "event_date",
        "days_elapsed",
        "window_status",
        "net_unclaimed_qty",
        "est_recovery_usd",
        "days_until_expiry",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=csv_fields)
        writer.writeheader()
        for row in audit_rows:
            writer.writerow({k: row[k] for k in csv_fields})

    # Load case template
    if template_path is not None and template_path.is_file():
        template_text = template_path.read_text(encoding="utf-8")
    else:
        template_text = (
            "# Seller Support Case Draft: {{CLAIM_TYPE_LABEL}} (Batch {{BATCH_NUM}})\n\n"
            "Audit Date: {{AUDIT_DATE}} | Policy: {{POLICY_REFERENCE}}\n\n"
            "{{ITEMS_TABLE}}\n"
        )

    # Group FILE_NOW_URGENT and FILE_NOW items by claim_type, max 5 Transaction Item IDs per ticket
    max_per_case = int(policy.get("max_ids_per_case", 5))
    eligible_by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in audit_rows:
        if row["window_status"] in ("FILE_NOW_URGENT", "FILE_NOW"):
            eligible_by_type[row["claim_type"]].append(row)

    case_files: list[str] = []
    for claim_type, items in sorted(eligible_by_type.items()):
        items.sort(
            key=lambda r: (
                -float(r["est_recovery_usd"]),
                int(r["days_until_expiry"]),
                r["transaction_item_id"],
            )
        )
        batches = math.ceil(len(items) / max_per_case)
        for b in range(batches):
            chunk = items[b * max_per_case : (b + 1) * max_per_case]
            case_name = f"case_{claim_type}_{b + 1:02d}.md"
            case_fp = cases_dir / case_name
            case_fp.write_text(
                render_case_draft(template_text, claim_type, b + 1, chunk, audit_dt, policy),
                encoding="utf-8",
            )
            case_files.append(str(case_fp))

    # Build summary counts and fba_audit_summary.md
    counts_by_status = {
        "FILE_NOW_URGENT": 0,
        "FILE_NOW": 0,
        "WAIT_TOO_EARLY": 0,
        "EXPIRED_WINDOW": 0,
    }
    units_by_status = {k: 0 for k in counts_by_status}
    total_recoverable_usd = 0.0
    wait_items: list[dict[str, Any]] = []

    for row in audit_rows:
        st = row["window_status"]
        counts_by_status[st] = counts_by_status.get(st, 0) + 1
        units_by_status[st] = units_by_status.get(st, 0) + int(row["net_unclaimed_qty"])
        if st in ("FILE_NOW_URGENT", "FILE_NOW"):
            total_recoverable_usd += float(row["est_recovery_usd"])
        elif st == "WAIT_TOO_EARLY":
            wait_items.append(row)

    total_recoverable_usd = round(total_recoverable_usd, 2)

    summary_lines = [
        "# FBA Reimbursement Window Audit Summary",
        "",
        f"- **Audit Reference Date:** {audit_dt.isoformat()}",
        f"- **Total Eligible Recoverable Value (USD):** ${total_recoverable_usd:.2f}",
        f"- **Seller Support Case Drafts Generated:** {len(case_files)} (max {max_per_case} Transaction Item IDs per ticket)",
        "",
        "## Discrepancy Counts by Window Status",
        "",
        "| Window Status | Line Items | Net Unclaimed Units |",
        "| --- | --- | --- |",
    ]
    for st in ("FILE_NOW_URGENT", "FILE_NOW", "WAIT_TOO_EARLY", "EXPIRED_WINDOW"):
        summary_lines.append(f"| {st} | {counts_by_status[st]} | {units_by_status[st]} |")

    if wait_items:
        summary_lines.extend(
            [
                "",
                "## Held Items (WAIT_TOO_EARLY - Do Not File Yet)",
                "",
                "| Claim Type | Transaction Item ID | FNSKU | Event Date | Days Elapsed | Earliest Unlock Date |",
                "| --- | --- | --- | --- | --- | --- |",
            ]
        )
        for item in wait_items:
            summary_lines.append(
                f"| {item['claim_type']} | {item['transaction_item_id']} | {item['fnsku']} | "
                f"{item['event_date']} | {item['days_elapsed']} | {item['earliest_unlock_date']} |"
            )

    summary_path = output_dir / "fba_audit_summary.md"
    summary_path.write_text("\n".join(summary_lines) + "\n", encoding="utf-8")

    return {
        "audit_date": audit_dt.isoformat(),
        "encodings": {"adjustments": adj_enc, "reimbursements": reimb_enc},
        "total_open_discrepancies": len(audit_rows),
        "counts_by_status": counts_by_status,
        "units_by_status": units_by_status,
        "total_recoverable_usd": total_recoverable_usd,
        "cases_generated": len(case_files),
        "output_files": {
            "audit_csv": str(csv_path),
            "summary_md": str(summary_path),
            "case_drafts": case_files,
        },
        "warnings": warnings,
    }


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    default_policy = script_dir.parent / "references" / "fba_policy_windows.yaml"
    default_template = script_dir.parent / "assets" / "seller_support_case_template.md"

    parser = argparse.ArgumentParser(
        prog="reconcile_fba.py",
        description="Deterministically reconcile Amazon Seller Central FBA TSV/CSV exports and enforce reimbursement claim windows.",
        epilog="Exit codes: 0 = completed cleanly, 2 = missing required input files or invalid arguments.",
    )
    parser.add_argument("--adjustments", required=True, help="Path to inventory_adjustments.tsv or Inventory Ledger Detail")
    parser.add_argument("--reimbursements", required=True, help="Path to reimbursements.tsv")
    parser.add_argument("--returns", help="Optional path to customer_returns.tsv")
    parser.add_argument("--refunds", help="Optional path to refunds.tsv")
    parser.add_argument("--costs", help="Optional path to sourcing_costs.csv (FNSKU/MSKU unit costs)")
    parser.add_argument("--audit-date", default=date.today().isoformat(), help="Audit reference date YYYY-MM-DD (default: today)")
    parser.add_argument("--policy", default=str(default_policy), help="Path to fba_policy_windows.yaml")
    parser.add_argument("--template", default=str(default_template), help="Path to seller_support_case_template.md")
    parser.add_argument("--default-unit-cost", type=float, default=0.0, help="Fallback unit cost USD if missing from sourcing_costs.csv")
    parser.add_argument("--output-dir", required=True, help="Directory to write audit CSV, summary Markdown, and case drafts")
    args = parser.parse_args()

    adj_path = Path(args.adjustments).resolve()
    reimb_path = Path(args.reimbursements).resolve()
    if not adj_path.is_file():
        print(f"Error: adjustments file not found: {adj_path}", file=sys.stderr)
        return 2
    if not reimb_path.is_file():
        print(f"Error: reimbursements file not found: {reimb_path}", file=sys.stderr)
        return 2

    audit_dt = parse_date(args.audit_date)
    if audit_dt is None:
        print(f"Error: invalid --audit-date format: {args.audit_date}", file=sys.stderr)
        return 2

    result = reconcile(
        adjustments_path=adj_path,
        reimbursements_path=reimb_path,
        output_dir=Path(args.output_dir).resolve(),
        audit_dt=audit_dt,
        returns_path=Path(args.returns).resolve() if args.returns else None,
        refunds_path=Path(args.refunds).resolve() if args.refunds else None,
        costs_path=Path(args.costs).resolve() if args.costs else None,
        policy_path=Path(args.policy).resolve() if args.policy else None,
        template_path=Path(args.template).resolve() if args.template else None,
        default_unit_cost=args.default_unit_cost,
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
