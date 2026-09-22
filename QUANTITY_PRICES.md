# Existing B2B quantity-price updates

`nf_mayorista_consultar` reads seller-owned ARS listings and all `/items/{id}/prices` records.
`nf_mayorista_fijar` requires explicit user authorization, the observed snapshot hash,
an operation ID, and exactly the existing quantity thresholds. Percent discounts are
converted to fixed ARS prices using the observed retail price and half-up cent rounding.
They are NOT persistent percentage rules: a later retail change requires another update.

Only existing standard B2B marketplace tiers without dates are supported. Other audiences,
classic variants, unknown conditions, absent tiers or mixed quantity offers fail closed.
POST changes only `/items/{id}/prices/standard/quantity`. No alternative write route or retry.
Retail item fingerprint and all non-quantity records are checked after a successful response.
Price IDs/metadata on other records are deliberately compared strictly: a mismatch requires
manual reconciliation, never a new operation ID. Locks are shared with retail-price edits.

Contract implementation reference (26 February 2026):
https://a2systems.co/blog/blog-2/actualizando-precios-mayoristas-en-mercadolibre-341
Official documentation could not be retrieved (403). Tests use synthetic HTTP responses;
live eligibility, API compatibility, and seller price changes are NOT certified by those tests.
The initial live read must succeed and show the intended quantity thresholds and audience.

Approved black-strap request: 5/17.18%, 10/19.29%, 25/21.46%, 50/23.57%, 100/25.01%.
At ARS 17990 these percentages produce exact cent amounts, slightly different from the
previous approximate rounded-to-ten-pesos table. Do not apply to other colors automatically.
Known linked listings MLA2055005535 and MLA3384701010 must be read and verified separately;
do not repeat a write if the linked listing already reflects the desired tiers.

Run: `python -m unittest test_quantity_prices -v`.
