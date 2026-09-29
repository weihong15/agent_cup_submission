---
on_kill_switch: keep_all        # inventory is two stablecoins; no end state - the organisers stop the bot
cancel_open_orders: true
---
# Stable churn shutdown

No one is watching and no one needs to be asked. Inventory is stablecoins - benign to hold. Do not cross the book to
"flatten" a healthy 50/50 split: it only pays the spread.

1. Confirm no orders of ours rest: the last STATUS state is CLOSING, STOOD_DOWN or KILLED, and `manage_bots(action="status")`
   shows the controller stopped.
2. If the stop came mid-race and one coin is depegging (mid < 0.9960 or > 1.0040), deploy `exit_usd1usdt` with
   `close_base_share` toward the healthy coin before you finish.
3. Journal the final line: volume, maker share, fees (fee_bp_maker / fee_bp_taker), pnl, base_share, mode.
