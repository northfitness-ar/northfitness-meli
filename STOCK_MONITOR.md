# Stock y reposición

The monitor now shows private per-variant Full and warehouse coverage, outstanding
purchase orders by stage, and a warehouse journal. No code in this feature changes
ML listing quantities, prices, advertisements, or purchases.

Inventory is stored privately in the existing `inventory_snapshot` configuration.
Extra fields: row `name`, `variant`, `aliases`, `estimated`, `warehouse_as_of`,
`full_target_days`; top-level `purchase_orders` (id, name, status, nullable eta,
items [{sku,quantity}]), `full_as_of`, and `assumptions`.
Do not commit the business snapshot or shipment tracking to this public repository.
The former replenishment planner declines this richer snapshot instead of ignoring
its estimates and outstanding orders.

Full is read using seller-verified items and unique inventory identifiers through
`GET /inventories/{inventory_id}/stock/fulfillment` (official documentation:
https://developers.mercadolibre.com.ar/envios-fulfillment). Errors remain unknown;
a dated screenshot value is shown separately. Stock for shared listings is counted
once per inventory ID. The current stock view is independent of sales date filters.

Warehouse starts at a dated physical count or explicitly labeled estimate.
Subsequent paid non-Full sales deduct units; cancellations never infer a physical
return. Receive, count, sale and transfer entries are append-only and carry an actor,
UUID, revision and timestamp. Receipts linked to a purchase reduce its outstanding
quantity. Transfers only deduct warehouse, never increase Full locally. Previously
reserved transfers must not be deducted again. Counts older than the 28-day sales
window require a new count rather than an incomplete ledger. Missing mappings,
unknown shipping and negative balances are surfaced, not treated as certainty.

Coverage uses complete days: 7/14/28-day rates weighted 60/30/10, an 85%-of-last-week
floor during growth, and a bounded, shrunk weekday factor. Observed all-day outages
are excluded; historical outages are not invented. Month-phase and separate
advertising/price causal factors are not applied without adequate comparable data.
Pending orders are never available inventory. All outstanding orders reduce the
buying quantity, and timing remains a separate warning. Supplier lead/safety/target
settings are explicitly initial planning assumptions, not confirmed delivery dates.

Email: daily digest after 09:00 Argentina, checked every five minutes by the monitor
worker. Requires existing NF_SMTP_HOST, NF_SMTP_PORT (465 default), NF_SMTP_USER,
NF_SMTP_PASSWORD, NF_SMTP_FROM and NF_ALERT_EMAIL_TO. For this deployment the intended
recipient is the NF mailbox confirmed by the owner; configure it in the hosting
secret/environment settings, never in code or chat. Without SMTP configuration the
UI states that emails are pending. A digest is reserved before sending; uncertain
SMTP delivery is flagged and not automatically retried. No false 'email enabled'
claim is made by this release. Startup remains single-process as existing deployment.

Validation: targeted stock, monitor and authentication pytest suites; existing JS
UI harnesses; stock JS syntax. Tests cover duplicate Full inventory IDs, failed ML
reads, physical returns, receipt idempotency/overreceipt, count resets, transfers,
revision conflicts, outage forecasting and unconfigured email.

## Full reconciliation and reserved warehouse stock

Read-only tools `nf_full_consultar`, `nf_full_operaciones` and `nf_full_resumen`
expose seller-verified inventory details, conditions of unavailable units and
paginated operations. Operations accept a maximum31-day date interval, with an
exclusive end date; every page verifies seller and inventory IDs. Stock UI includes
unique Full inventories and their linked publications, and reads a separate page
of recent inbound receptions. A missing/forbidden response remains unknown.

The official local-seller API documents stock and operations, not the complete
Seller Center collection calendar or capacity planning. This release does not
claim automatic import of future collections or associate a reception with a
collection merely because quantities match. Declared collections retain source
and date; receipts remain a reconciliation aid, not an automatic ledger write.

Private snapshot `full_collections`: id, name, status (`reserved`, `awaiting_stock`,
`unverified`), optional date, source, items [{sku,quantity}]. Per-row
`warehouse_reserved` must equal the quantities of baseline reserved collections.
`warehouse_available` remains the FREE quantity: introducing a new reservation
requires moving that quantity from free to reserved in the same versioned snapshot.
Existing reservations must not be deducted again. Imported snapshots retain their
original as_of and already netted free balances; changing as_of requires reconciling
the event journal rather than blindly rebasing it.

Display physical warehouse = free + remaining reserved. Total coverage includes
reserved stock still physically present, excludes new purchases and dispatched
transit. The separate Full coverage still uses only currently sellable Full stock.
`collection_dispatch` records removal of reserved units, bounded by the remaining
reservation and idempotent request ID. It reduces reserved physical units without
deducting free warehouse again or locally increasing Full. The reserved collection
selector in the movement form identifies the batch. Dispatched stock is explicitly
shown as pending reconciliation against Full; no time-based automatic receipt.
