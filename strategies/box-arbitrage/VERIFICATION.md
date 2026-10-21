# Isolated verification

29 scenarios passed with a fake SDK and broker. No real orders.

Source SHA-256: f7ce21dd2c75918537cde36db05edfc24b8db942e6d2083100e65ce6b8f4119b

- full box: equal fills, shorts close before longs
- partial short fill: confirmed cancellation and exact remainder unwind
- fill races cancellation: all late fills accounted
- second long rejected: no short submitted, first long unwound
- accepted order with lost response: recovered without duplicate
- unknown absent response: no blind resubmission
- ambiguous recovered order: blocked instead of guessed
- restart after fill: journal recovered and exits only known inventory
- restart before fill accounting: pending order reconciled once
- terminal position-book lag: pending retained, fills not counted twice
- duplicate trade ID: deduplicated fill accounting
- foreign contract position: no order or adoption
- routing mode changed: new orders blocked
- stale quotes: rejected
- unsynchronized quote basket: rejected
- insufficient staged capital: entry blocked
- shutdown: closes shorts before long hedges
- 15:20 deadline: starts coordinated exit
- feed outage: unwind starts without pretending stale prices are current
- gross/net cashflow, turnover and conservative close buffer calculations
- scanner formula, same-expiry legs, lot sizing and depth capacity
- all four intermediate baskets receive margin preflight
- submission intent durably journaled before network call; key absent
- foreign working order: submission blocked
- second process cannot acquire the same account lock
- repeated exit rejections: halt with inventory retained; no duplicate loop
- run-loop exception between legs switches to unwind before further entry
- exact installed SDK nested depth callback parsed correctly
- elapsed capital funding is included in close P&L

These checks verify code paths under the supplied scenarios. They do not establish profitability,
live liquidity, exchange-level execution or actual broker margin availability.
