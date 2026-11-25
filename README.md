# FBA Reimbursement Window Auditor

Offline Python reconciliation auditor for **Amazon Seller Central FBA** exports that nets warehouse Lost (`M`, `E`) vs. Found (`F`) inventory adjustments, offsets already-paid reimbursements, checks unreturned customer refunds, enforces Amazon claim filing windows, and outputs prioritized Seller Support case drafts capped at 5 Transaction Item IDs per ticket.

Built for **Amazon FBA brand operators, marketplace finance/ops leads, and e-commerce accounting teams** preventing missed recovery deadlines and Seller Central policy warnings.

## Quick Install

```bash
npx skills add ameer-agent-dev/fba-reimbursement-window-auditor --skill fba-reimbursement-window-auditor
```

*(Single-repo shorthand: `npx skills add ameer-agent-dev/fba-reimbursement-window-auditor`)*

## Required & Optional Inputs

- **Mandatory** *(both required before any claim draft is generated)*:
  - `inventory_adjustments.tsv` (or Seller Central Inventory Ledger Detail TSV/CSV).
  - `reimbursements.tsv` (used to offset paid claims and prevent duplicate filing).
- **Optional**:
  - `customer_returns.tsv` and `refunds.tsv` (for 45-to-105-day unreturned refund audits).
  - `sourcing_costs.csv` (FNSKU/MSKU unit cost for estimated USD recovery ranking).
  - `--audit-date YYYY-MM-DD` (defaults to today's ISO date).

## Example Prompts

- **Normal run**:
  > "Reconcile `./exports/inventory_adjustments.tsv` and `./exports/reimbursements.tsv` along with `./exports/customer_returns.tsv`, `./exports/refunds.tsv`, and `./sourcing_costs.csv` as of `2026-11-26`, highlight claims expiring in the next 14 days, and batch Seller Support tickets."
- **Edge case (missing reimbursements file, premature <45-day customer refunds, and UK/EU date formats)**:
  > "Audit our UK ledger export `./uk_ledger_detail.csv` against `./uk_reimbursements.tsv` and `./uk_refunds.tsv`. Several Lost (`M`) items were later Found (`F`) at another FC, and half of the customer refunds happened only 20 days ago."

## Expected Output Files

- `fba_reimbursement_audit.csv`: Line-by-line discrepancy ledger (`claim_type, fnsku, msku, transaction_item_id, event_date, days_elapsed, window_status, net_unclaimed_qty, est_recovery_usd, days_until_expiry`) classified as `FILE_NOW_URGENT` (`<= 14` days left), `FILE_NOW`, `WAIT_TOO_EARLY`, or `EXPIRED_WINDOW`.
- `fba_audit_summary.md`: Executive recovery breakdown by status, urgent expiring FNSKUs, and exact Day-45 unlock dates for `WAIT_TOO_EARLY` customer returns.
- `seller_support_cases/case_<type>_<batch>.md`: Ready-to-file Seller Support ticket drafts containing only eligible `FILE_NOW_URGENT` and `FILE_NOW` claims, sorted by highest USD recovery, capped at 5 Transaction Item IDs per ticket, with a proof-of-ownership invoice checklist.

## Bundled Scripts, References & Assets

| Path | Purpose |
| :--- | :--- |
| `SKILL.md` | Mandatory dual-report gate, netting workflow, and Seller Central batching rules |
| `scripts/reconcile_fba.py` | Normalizes US/UK/EU encodings & dates, nets Lost/Found & reimbursements, classifies windows, and writes outputs (`--help` supported) |
| `references/fba_policy_windows.yaml` | Amazon filing window bounds (FC lost/damaged 1-60d, customer returns 45-105d, removals 15-75d) and urgency thresholds |
| `references/adjustment_reason_codes.md` | Seller Central adjustment codes (`M`, `E`, `D`, `Q`, `F`) and localized column header aliases |
| `assets/seller_support_case_template.md` | Policy-aligned Seller Support ticket template and invoice verification checklist |

### Python Dependencies

Uses **Python 3 standard library only** (zero external `pip` packages required; runs 100% offline with zero Seller Central API calls).

## Autonomy & Safety Boundary

- **Autonomous local operations**: Parses local TSV/CSV exports strictly as untrusted data, enforces the mandatory `reimbursements.tsv` gate so paid items are never double-claimed, and withholds `WAIT_TOO_EARLY` items from ticket drafts to prevent automatic denials.
- **Human sign-off required**: Outputs local CSVs and Markdown case drafts only. Never opens or submits tickets inside Amazon Seller Central automatically (`disable-model-invocation: true` recommended if browser or Seller Central automation is present). Sellers must verify packing slips/invoices before manual submission.

## License

[MIT](LICENSE)
