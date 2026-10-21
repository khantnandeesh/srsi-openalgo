# OpenAlgo box arbitrage strategy

Paste box_arbitrage.py into a NEW Python strategy. Do not replace the SRSI strategy.
No process or schedule has been started.

## Defaults
NIFTY / NFO / NRML, 1 lot (actual contract lot size from option chain).
Nearest valid expiry, pinned when a box is reserved.
100- or 200-point boxes within 5 strikes of ATM.
Maximum configured capital Rs 50,000; one attempted box per trading day.
Entry 09:20-14:30 IST; market exits start at 15:20 IST.
The code submits normal orders; OpenAlgo controls Live/Analyze routing.
It reads routing status to prevent recovering a journal into the wrong venue,
and never toggles mode.

## Important operating details
The strategy opens both longs before the shorts. Incomplete entries unwind.
Cancellation must be confirmed; a cancellation request alone is not a fill result.
Shorts close before longs. No SmartOrder account flattening is used.
A disk journal, account lock and cumulative per-order fills support restart recovery.
Recovery exits known inventory, rather than resuming an interrupted entry.
Use these contracts exclusively: broker books do not reliably identify strategy ownership.
If submission outcome is unknown, the code searches new matching orders once per poll.
With no match or multiple matches, it blocks further submissions and logs attention;
it never treats absence from the book as proof that an order failed.
External/manual trading, routing-mode changes, repeated exit failures and book mismatches
require operator reconciliation. Do not delete/edit an active journal to bypass a halt.
Use the same credentials and persistent journal directory across restarts.
State location: /home/ubuntu/openalgo/strategies/box-arbitrage/state.
The OpenAlgo scheduler may kill a process before graceful unwind completes; restart recovery
is necessary. No software can guarantee a four-order atomic fill.

## Valuation and limitations
This is an INTRADAY execution strategy. The fixed strike-width payoff applies at expiry;
an early close may lose money. It scans for estimated expiry edge after funding and costs,
then monitors executable close P&L and exits at a profit target, loss threshold,
feed-depth outage, restart recovery, or 15:20.
The loss threshold is a trigger, not a guaranteed maximum loss.
Costs are configurable budgets, not an audited fee/tax calculator.
Margin API support is required for all intermediate baskets; unavailable data blocks entries.
Depth receipt freshness does not prove exchange freshness: some broker adapters stamp receipt
time or cache depth. Simulator fills do not prove live fills at displayed depth.
A lack of genuine executable opportunities can mean NO TRADES; the strategy never forces one.
Long boxes that are only fairly priced should fail the edge filter.
API outages and unresolved submission outcomes may require manual intervention.

## Validation performed
Python AST syntax and installed SDK method/signature checks.
No trading strategy was run and no order was submitted.
29 isolated fake-broker checks passed. See isolated-verification-results.json.
They cover partial fills, cancellation races, unknown submissions, duplicate orders,
restart recovery, account isolation, capital, fees, funding, scanner filters and SDK depth parsing.
Analyze-mode end-to-end fills against the actual server have not been run.
Perform partial-fill, cancellation-race, timeout, restart and mode-switch checks in Analyze
before considering deployment with real money.

## Configuration
Edit the USER CONFIGURATION section. EXPIRY="" discovers nearest expiry; alternatively set
a valid DDMMMYY date. Change WIDTHS, LOTS, capital, loss threshold and cost budgets as required.
For SENSEX, set UNDERLYING="SENSEX", INDEX_EXCHANGE="BSE_INDEX", EXCHANGE="BFO".
OPENALGO_API_KEY is read from the environment or OpenAlgo .env.
