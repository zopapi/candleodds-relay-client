"""
One-shot check that YOUR Polymarket account can trade: login, funds,
approvals, order placement and cancellation -- with almost no risk.

It places one tiny BUY far below the market price (about half the current
best bid, minimum size) on the live BTC 15-minute market, as a post-only
order, confirms it is resting on the book, then cancels it. It cannot cross
the spread, so it will not fill under normal conditions, and worst case it
would cost the minimum order size at that low price (a few cents).

Needs POLYMARKET_PRIVATE_KEY and POLYMARKET_PROXY_WALLET (RELAY_* not needed).
Requires --yes so it can never run by accident.

Usage:
    python test_order.py --yes              # locally
    RUN_MODE=test-order  (Railway variable) # hosted: the normal service start
                                            # command runs this once instead of the
                                            # trading loop, then exits 0 (so the host
                                            # doesn't restart it). Read PASSED/FAILED
                                            # in the logs, then remove the variable.
"""

import os
import sys
import time
from decimal import Decimal, ROUND_FLOOR

from dotenv import load_dotenv
from polymarket import PublicClient

import relay_client as rc


_exit_zero = False


def finish(ok, msg):
    rc.log(("PASSED: " if ok else "FAILED: ") + msg)
    sys.exit(0 if (ok or _exit_zero) else 1)


def run(exit_zero=False):
    """Runs the whole check and exits the process. exit_zero=True is for hosted
    runs, where a non-zero exit would make the host retry (and re-place) it."""
    global _exit_zero
    _exit_zero = exit_zero
    try:
        _run()
    except SystemExit:
        raise
    except Exception as e:
        # Never leave a non-technical user staring at a raw traceback.
        finish(False, f"unexpected error ({type(e).__name__}): {e}. If an order was placed, "
                      "check your Polymarket open orders and cancel it there.")


def _run():
    load_dotenv()
    key =(os.environ.get("POLYMARKET_PRIVATE_KEY") or "").strip()
    wallet = (os.environ.get("POLYMARKET_PROXY_WALLET") or "").strip()
    if not key or not wallet:
        finish(False, "POLYMARKET_PRIVATE_KEY and POLYMARKET_PROXY_WALLET must be set")
    rc.set_secrets(key)

    rc.log("1/6 logging in to Polymarket...")
    try:
        pm = rc.SecureClient.create(private_key=key, wallet=wallet)
    except Exception as e:
        finish(False, f"login failed ({type(e).__name__}): {e}. Re-check the exported "
                      "private key and that the wallet is your Polymarket deposit address.")
    rc.log(f"     logged in for wallet {wallet}")

    rc.log("2/6 checking funds and trading approvals...")
    try:
        usdc, approved = rc.collateral_status(pm)
    except Exception as e:
        finish(False, f"balance/approval check failed ({type(e).__name__}): {e}")
    rc.log(f"     balance {usdc:.2f} USDC; approvals {'set' if approved else 'NOT set'}")
    if usdc <= 0:
        rc.log("     WARNING: balance is 0 -- the order below is expected to be rejected")
    if not approved:
        finish(False, "trading approvals are not set. Place one tiny trade on the Polymarket "
                      "website (that sets them), then run this again.")

    rc.log("3/6 finding the current BTC 15-minute market...")
    public = PublicClient()
    now = int(time.time())
    window = now - (now % 900)
    market = None
    for ts in (window, window + 900):
        try:
            market = public.get_market(slug=f"btc-updown-15m-{ts}")
            slug = f"btc-updown-15m-{ts}"
            break
        except Exception:
            continue
    if market is None:
        finish(False, "could not find the current BTC 15-minute market on Polymarket; try again in a minute")
    token_id = market.outcomes.yes.token_id
    rc.log(f"     {slug}")

    rc.log("4/6 reading the book and placing a deep post-only bid...")
    book = pm.get_order_book(token_id=token_id)
    tick = Decimal(str(book.tick_size))
    bids = [Decimal(str(l.price)) for l in (book.bids or [])]
    ref = min(bids) if bids else Decimal("0.20")
    price = max(tick, (ref / 2).quantize(tick, rounding=ROUND_FLOOR))
    size = Decimal(str(book.min_order_size))
    rc.log(f"     price={price} size={size} (worst case cost {price * size} USDC)")
    resp = pm.place_limit_order(token_id=token_id, price=price, size=size, side=rc.BUY, post_only=True)
    if not resp.ok:
        finish(False, f"order rejected: {resp.code}: {resp.message}")
    order_id = resp.order_id
    rc.log(f"     placed: order {order_id[:16]}... status={resp.status}")

    rc.log("5/6 confirming it is resting on the book...")
    time.sleep(3)
    order = pm.get_order(order_id=order_id)
    rc.log(f"     status={order.status} matched={order.size_matched}")

    rc.log("6/6 cancelling...")
    cancel = pm.cancel_order(order_id=order_id)
    rc.log(f"     cancelled: {cancel.canceled}")
    finish(True, "login, funds, approvals, order placement, status and cancellation all work")


if __name__ == "__main__":
    if "--yes" not in sys.argv:
        print("This places one tiny, deep out-of-the-money, post-only order and then "
              "cancels it.\nRe-run with --yes to proceed.")
    else:
        run()
