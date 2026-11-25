# Amazon FBA Inventory Adjustment Reason Codes and Header Aliases

Use this reference when reviewing Seller Central Inventory Adjustments or Inventory Ledger Detail exports alongside `scripts/reconcile_fba.py`.

## Core Adjustment Reason Codes

| Code | Meaning | Audit Action | Claim Type |
| --- | --- | --- | --- |
| `M` | Inventory misplaced in fulfillment center | Eligible after netting against `F` (Found) and `reimbursements.tsv` | `fc_lost_damaged` |
| `E` | Inventory damaged at fulfillment center (warehouse fault) | Eligible after offsetting `reimbursements.tsv` | `fc_lost_damaged` |
| `D` | Inventory disposed/damaged in fulfillment center | Eligible after offsetting `reimbursements.tsv` | `fc_lost_damaged` |
| `Q` | Inventory damaged by warehouse or inbound handling | Eligible after offsetting `reimbursements.tsv` | `fc_lost_damaged` |
| `F` | Inventory found in fulfillment center | Offsets `M` (and `E` misplaced) units per FNSKU before claiming | Offset only |
| `N` | Transfer from holding account | Non-claimable internal transfer | Ignore |
| `P` | Unsellable inventory status change | Verify disposition; not auto-claimed | Ignore |
| `R` | Removal order discrepancy / lost in removal | Eligible under 15-to-75-day removal window | `removal` |

## Localized US, EU, and UK Column Aliases

`scripts/reconcile_fba.py` normalizes headers (lowercase, spaces and hyphens converted to underscores) using these alias groups:

- `fnsku`: `fnsku`, `fn_sku`, `fulfillment_network_sku`, `asin` (fallback only when FNSKU is absent)
- `msku`: `msku`, `sku`, `merchant_sku`, `seller_sku`
- `transaction_item_id`: `transaction_item_id`, `transaction_id`, `reference_id`, `amazon_order_id`, `order_id`, `shipment_id`
- `reason_code`: `reason`, `reason_code`, `adjustment_reason`, `event_type`, `disposition`
- `quantity`: `quantity`, `quantity_adjusted`, `qty`, `units`, `quantity_reimbursed_total`, `quantity_reimbursed_cash`, `quantity_reimbursed_inventory`
- `event_date`: `adjusted_date`, `event_date`, `date`, `approval_date`, `return_date`, `refund_date`, `posted_date`
- `fulfillment_center_id`: `fulfillment_center_id`, `fulfillment_center`, `fc`, `warehouse_id`, `location`

## Netting Order of Operations

1. **Lost vs. Found netting**: Match `F` (Found) units against `M` (Misplaced/Lost) units within the same `(fnsku, fulfillment_center_id)` first, then apply any leftover `F` surplus across the same `fnsku` so cross-FC finds never generate false claims.
2. **Reimbursement offsetting**: Subtract already-reimbursed units from `reimbursements.tsv` by exact `transaction_item_id` first, then by `fnsku` for remaining warehouse lost/damaged reimbursements.
3. **Window gating**: Compute `days_elapsed = audit_date - event_date` and classify remaining net unclaimed units as `FILE_NOW_URGENT` (<= 14 days left), `FILE_NOW`, `WAIT_TOO_EARLY`, or `EXPIRED_WINDOW`.
