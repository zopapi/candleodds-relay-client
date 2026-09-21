"""
relay_client.py -- executes trade signals from a signal relay on YOUR OWN
Polymarket account.

What it does, in a loop:
  1. polls  GET  <RELAY_URL>/signals/current   (header X-Client-Token)
  2. for each signal: places ONE marketable limit BUY on your account, capped
     at the price the relay sent, sized from the stake the relay sent
  3. watches the order; after ttl_sec (or when the relay says halt) cancels
     whatever has not filled
  4. reports the result back with  POST <RELAY_URL>/fills

What it never does:
  * no selling, ever -- filled positions are held until the market resolves
    (there is no sell code in this file; tests/test_client.py enforces that)
  * no decisions -- direction, price cap, stake and timing all come from the
    relay; this program only executes them
  * your private key never leaves this process (it is redacted from logs and
    is never sent to the relay)

DRY_RUN (default TRUE) gates exactly one thing: the order call. Everything
else -- polling, Polymarket login, book pricing, fill reporting -- runs live.
In dry-run the order is priced against the live book and logged, then a
zero-size "cancelled" report is sent so you can see the whole path work.

Environment variables: see .env.example.

Usage:
    python relay_client.py            # run the loop (what Railway runs)
    python relay_client.py --check    # one poll + login + balance, no orders
    RUN_MODE=test-order python relay_client.py   # one tiny place+cancel test, then exit
"""

import os
import sys
import time
from dataclasses import dataclass
from decimal import Decimal, ROUND_FLOOR

import requests
from dotenv import load_dotenv
from polymarket import RequestRejectedError, SecureClient, UnexpectedResponseError, UserInputError
from urllib3.exceptions import MaxRetryError

# The only order side this program ever uses.
BUY = "BUY"

# Sanity bounds on a signal's age. The relay already refuses to send stale
# signals; this only guards against a replayed/cached response or a badly
# skewed clock.
MAX_SIGNAL_AGE_SECONDS = 300
MAX_SIGNAL_FUTURE_SECONDS = 60

# Exchange-side expiry backstop on every order, in case this process dies
# before it can cancel. polymarket-client 0.10.0 refuses any expiration less
# than 180s out (found by running against the live SDK). The exchange accepted
# a 190s expiration and recorded it as sent. Polymarket's docs describe a
# one-minute GTD threshold that would shorten the real backstop to ~130s;
# that is not verified, so the backstop is somewhere between ~130s and 190s,
# either way longer than any relay ttl_sec.
MIN_GTD_SECONDS = 190
GTD_TTL_MARGIN_SECONDS = 125   # expiration >= ttl_sec + this, so backstop > ttl

MONITOR_TICK_SECONDS = 2
HEARTBEAT_SECONDS = 60
MAX_FINALIZE_ATTEMPTS = 10

CLOSED_STATUSES = {"MATCHED", "CANCELED", "CANCELLED", "EXPIRED", "INVALID"}


# ---------------------------------------------------------------------------
# Logging with secret redaction -- every line goes through redact()
# ---------------------------------------------------------------------------

_SECRETS = []


def set_secrets(*values):
    _SECRETS.clear()
    for v in values:
        if not v:
            continue
        _SECRETS.append(v)
        _SECRETS.append(v[2:] if v.startswith("0x") else "0x" + v)


def redact(text):
    text = str(text)
    for s in _SECRETS:
        text = text.replace(s, "[REDACTED]")
    return text


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}Z {redact(msg)}", flush=True)


def fatal(msg):
    log(f"FATAL: {msg}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

@dataclass
class Config:
    relay_url: str
    token: str
    private_key: str
    wallet: str
    dry_run: bool
    max_stake: Decimal
    poll_seconds: float


def load_config(env):
    missing = [n for n in ("RELAY_URL", "RELAY_CLIENT_TOKEN", "POLYMARKET_PRIVATE_KEY",
                           "POLYMARKET_PROXY_WALLET") if not (env.get(n) or "").strip()]
    if missing:
        fatal("missing environment variable(s): " + ", ".join(missing))

    raw_dry = (env.get("DRY_RUN") or "true").strip().lower()
    if raw_dry in ("true", "1", "yes", "on"):
        dry_run = True
    elif raw_dry in ("false", "0", "no", "off"):
        dry_run = False
    else:
        # A typo must never silently go live (or silently stay dry).
        fatal(f"DRY_RUN={raw_dry!r} is not understood -- use exactly true or false")

    try:
        max_stake = Decimal((env.get("MAX_STAKE_USDC") or "5").strip())
        poll_seconds = float((env.get("POLL_INTERVAL_SECONDS") or "5").strip())
    except Exception:
        fatal("MAX_STAKE_USDC / POLL_INTERVAL_SECONDS must be numbers")
    if max_stake <= 0 or poll_seconds < 1:
        fatal("MAX_STAKE_USDC must be > 0 and POLL_INTERVAL_SECONDS must be >= 1")

    return Config(
        relay_url=env["RELAY_URL"].strip().rstrip("/"),
        token=env["RELAY_CLIENT_TOKEN"].strip(),
        private_key=env["POLYMARKET_PRIVATE_KEY"].strip(),
        wallet=env["POLYMARKET_PROXY_WALLET"].strip(),
        dry_run=dry_run,
        max_stake=max_stake,
        poll_seconds=poll_seconds,
    )


# ---------------------------------------------------------------------------
# Polymarket helpers (shared with test_order.py)
# ---------------------------------------------------------------------------

def connect_polymarket(private_key, wallet):
    try:
        return SecureClient.create(private_key=private_key, wallet=wallet)
    except Exception as e:
        fatal(f"Polymarket login failed ({type(e).__name__}): {e}. Check that "
              "POLYMARKET_PRIVATE_KEY is the exported key and "
              "POLYMARKET_PROXY_WALLET is your Polymarket deposit address "
              "(not the address the key derives to).")


def collateral_status(pm):
    """Returns (usdc_balance, approved). The SDK reports base units (6 decimals)."""
    ba = pm.get_balance_allowance(asset_type="COLLATERAL")
    allow = ba.allowances or {}
    vals = list(allow.values()) if isinstance(allow, dict) else list(allow)
    approved = any(Decimal(str(v or 0)) > 0 for v in vals)
    return Decimal(ba.balance) / Decimal(10 ** 6), approved


class RelayAuthError(Exception):
    pass


def _never_sent(exc):
    """True only when the failure proves the request never reached the relay
    (could not connect / resolve). A connection dropped AFTER sending is
    ambiguous -- the relay may have recorded it -- so that is not retried."""
    if isinstance(exc, requests.ConnectTimeout):
        return True
    return (isinstance(exc, requests.ConnectionError)
            and bool(exc.args) and isinstance(exc.args[0], MaxRetryError))


# ---------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------

class Client:
    def __init__(self, cfg, pm, http=None, clock=time.time, sleep=time.sleep):
        self.cfg = cfg
        self.pm = pm
        self.http = http or requests.Session()
        self.clock = clock
        self.sleep = sleep
        self.claimed = set()     # (symbol, window_start_ts, side) already acted on
        self.pending = []        # orders placed, not yet finalized
        self.last_heartbeat = 0.0
        self.last_status = "starting"

    # -- relay ----------------------------------------------------------------

    def _headers(self):
        return {"X-Client-Token": self.cfg.token}

    def poll(self):
        """Returns (halt, signals). Raises RelayAuthError on 401."""
        r = self.http.get(f"{self.cfg.relay_url}/signals/current",
                          headers=self._headers(), timeout=(5, 10))
        if r.status_code == 401:
            raise RelayAuthError(r.text[:200])
        r.raise_for_status()
        data = r.json()
        if not isinstance(data, dict) or "halt" not in data or not isinstance(data.get("signals"), list):
            raise ValueError(f"unexpected relay response shape: {str(data)[:200]}")
        return bool(data["halt"]), data["signals"]

    def report_fill(self, sig, fill, price):
        """POST /fills. The relay has no de-duplication, so a request that may
        already have been processed is NEVER retried: only failures that prove
        the request never reached it (connect errors, proxy 502/503/504) are.
        A report that ultimately fails is logged in full so it can be entered
        by hand."""
        status = "filled" if fill > 0 else "cancelled"
        body = {
            "symbol": sig["symbol"],
            "window_start_ts": int(sig["window_start_ts"]),
            "side": sig["side"],
            "max_entry_sent": float(sig["max_entry"]),
            "actual_fill_price": float(price) if (fill > 0 and price is not None) else None,
            "size": float(fill),
            "status": status,
        }
        delay = 1.0
        for attempt in range(1, 5):
            try:
                r = self.http.post(f"{self.cfg.relay_url}/fills", json=body,
                                   headers=self._headers(), timeout=(5, 10))
            except requests.RequestException as e:
                if _never_sent(e):
                    log(f"fill report attempt {attempt}: could not connect ({type(e).__name__}); retrying")
                    self.sleep(delay)
                    delay *= 2
                    continue
                log(f"ERROR fill report failed, NOT retried (may have been recorded): "
                    f"{type(e).__name__} -- payload {body}")
                return False
            if r.status_code in (502, 503, 504):
                log(f"fill report attempt {attempt}: relay unavailable ({r.status_code}); retrying")
                self.sleep(delay)
                delay *= 2
                continue
            if r.status_code == 201:
                log(f"reported {status} {sig['symbol']} {sig['side']} size={body['size']} "
                    f"price={body['actual_fill_price']} (fill id {r.json().get('id')})")
                return True
            log(f"ERROR fill report rejected: HTTP {r.status_code} {r.text[:200]} -- payload {body}")
            return False
        log(f"ERROR fill report not delivered -- enter manually: {body}")
        return False

    # -- signals -> orders ----------------------------------------------------

    @staticmethod
    def _valid(sig):
        try:
            return (
                sig["side"] in ("up", "down")
                and 0 < float(sig["max_entry"]) < 1
                and float(sig["stake_usdc"]) > 0
                and int(sig["ttl_sec"]) > 0
                and int(sig["window_start_ts"]) > 0
                and str(sig["symbol"])
                and str(sig["token_id"])
            )
        except (KeyError, TypeError, ValueError):
            return False

    def already_exposed(self, sig):
        """True if this account already has an open order or any trade on this
        market. This is what stops a restart (Railway redeploy, crash) from
        ordering the same window twice -- the exchange is the memory, not a
        local file. Fails closed: any error means 'do not order'."""
        for _ in self.pm.list_open_orders(token_id=sig["token_id"]).iter_items():
            return True
        if sig.get("condition_id"):
            trades = self.pm.list_account_trades(market=sig["condition_id"])
        else:
            trades = self.pm.list_account_trades(token_id=sig["token_id"])
        for i, t in enumerate(trades.iter_items()):
            if i >= 50:
                break
            if t.status != "FAILED":
                return True
        return False

    def price_order(self, token_id, max_entry, stake):
        """Floor the cap to the book's tick; shares = stake / limit, floored to
        2dp. Returns (limit, shares, best_ask, reason); reason set => skip."""
        book = self.pm.get_order_book(token_id=token_id)
        tick = Decimal(str(book.tick_size))
        limit = (Decimal(str(max_entry)) / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
        if limit < tick:
            return None, None, None, f"cap_below_tick (max_entry={max_entry}, tick={tick})"
        shares = (stake / limit).quantize(Decimal("0.01"), rounding=ROUND_FLOOR)
        min_size = Decimal(str(book.min_order_size))
        asks = [Decimal(str(l.price)) for l in (book.asks or [])]
        best_ask = min(asks) if asks else None
        if shares < min_size:
            return None, None, best_ask, f"below_min_order_size ({shares} < {min_size})"
        return limit, shares, best_ask, None

    def handle_signal(self, sig):
        if not self._valid(sig):
            log(f"ignoring malformed signal: {sig}")
            return
        key = (sig["symbol"], int(sig["window_start_ts"]), sig["side"])
        if key in self.claimed:
            return
        self.claimed.add(key)   # claim BEFORE any order call: never order a window twice

        label = f"{sig['symbol']} {sig['side']} window {sig['window_start_ts']}"
        age = self.clock() - int(sig["window_start_ts"])
        if age > MAX_SIGNAL_AGE_SECONDS or age < -MAX_SIGNAL_FUTURE_SECONDS:
            log(f"skip {label}: signal age {age:.0f}s is outside sanity bounds (check the clock)")
            return

        stake = min(Decimal(str(sig["stake_usdc"])), self.cfg.max_stake)
        if stake < Decimal(str(sig["stake_usdc"])):
            log(f"{label}: relay stake {sig['stake_usdc']} clamped to local MAX_STAKE_USDC {stake}")

        try:
            if self.already_exposed(sig):
                log(f"skip {label}: this account already has an order/trade on this market")
                return
            limit, shares, best_ask, reason = self.price_order(sig["token_id"], sig["max_entry"], stake)
        except Exception as e:
            # Transient (network/API): nothing was ordered, so let the next poll retry.
            self.claimed.discard(key)
            log(f"{label}: pre-order check failed ({type(e).__name__}: {e}); will retry next poll")
            return
        if reason:
            log(f"skip {label}: {reason}")
            return

        rest_note = "" if (best_ask is not None and best_ask <= limit) else \
            f" (best ask {best_ask} is above the cap -- order will rest, not cross)"

        if self.cfg.dry_run:
            log(f"DRY RUN: would BUY {shares} {label} @ limit {limit} (cap {sig['max_entry']}, "
                f"stake {stake}){rest_note} -- no order sent")
            self.report_fill(sig, Decimal(0), None)
            return

        ttl = int(sig["ttl_sec"])
        try:
            resp = self.pm.place_limit_order(
                token_id=sig["token_id"], price=limit, size=shares, side=BUY,
                post_only=False,
                expiration=int(self.clock()) + max(MIN_GTD_SECONDS, ttl + GTD_TTL_MARGIN_SECONDS),
            )
        except UserInputError as e:
            # Rejected by the SDK before anything was signed or sent.
            log(f"ERROR {label}: order NOT sent -- the SDK refused it: {e}")
            return
        except RequestRejectedError as e:
            status = getattr(e, "status", None)
            if status is None or not (400 <= int(status) < 500):
                log(f"ERROR {label}: exchange error {status}: {e} -- state unknown, "
                    f"NOT retried; check your Polymarket open orders")
                return
            # A 4xx is a definitive rejection: nothing was placed.
            log(f"{label}: order rejected by exchange (HTTP {status}): {e}")
            self.report_fill(sig, Decimal(0), None)
            return
        except Exception as e:
            # The order may or may not exist. Stay claimed so it is never retried.
            log(f"ERROR {label}: order call raised {type(e).__name__}: {e} -- "
                f"state unknown, NOT retried; check your Polymarket open orders")
            return
        if not resp.ok:
            log(f"{label}: order rejected by exchange: {resp.code}: {resp.message}")
            self.report_fill(sig, Decimal(0), None)
            return

        log(f"PLACED BUY {shares} {label} @ {limit} (cap {sig['max_entry']}){rest_note} "
            f"order {resp.order_id}")
        self.pending.append({
            "sig": sig, "order_id": resp.order_id, "token_id": sig["token_id"],
            "condition_id": sig.get("condition_id"), "limit": limit, "shares": shares,
            "expires_at": self.clock() + ttl, "attempts": 0,
        })

    # -- order monitoring -----------------------------------------------------

    def fill_truth(self, p):
        """Exchange-confirmed (fill_shares, avg_price). The trades feed gives
        the price (we are usually the taker on a marketable order, but a
        resting remainder can fill as maker, so both are counted); the order's
        size_matched is cross-checked and wins when larger. Raises only when
        neither source is usable, so the caller defers instead of guessing."""
        order_id = p["order_id"]
        total = Decimal(0)
        notional = Decimal(0)
        feed_ok = True
        try:
            if p["condition_id"]:
                pages = self.pm.list_account_trades(market=p["condition_id"])
            else:
                pages = self.pm.list_account_trades(token_id=p["token_id"])
            for i, t in enumerate(pages.iter_items()):
                if i >= 300:
                    break
                if t.status == "FAILED":
                    continue
                if t.taker_order_id == order_id:
                    total += Decimal(str(t.size))
                    notional += Decimal(str(t.size)) * Decimal(str(t.price))
                for mo in (t.maker_orders or []):
                    if mo.order_id == order_id:
                        total += Decimal(str(mo.matched_amount))
                        notional += Decimal(str(mo.matched_amount)) * Decimal(str(mo.price))
        except UnexpectedResponseError as e:
            log(f"trades feed malformed for order {order_id[:16]}: {e} -- using order status")
            feed_ok = False
            total = notional = Decimal(0)
        price = (notional / total) if total > 0 else None

        try:
            matched = Decimal(str(self.pm.get_order(order_id=order_id).size_matched or 0))
        except Exception:
            matched = None    # can 404 once an order has expired; the trades sum stands
        if not feed_ok and matched is None:
            raise UnexpectedResponseError(f"cannot confirm fill for order {order_id}")

        fill = total
        if matched is not None and matched > fill:
            # Anything the feed hasn't itemized yet is priced at the limit: a BUY
            # can never have filled above it.
            limit = p["limit"]
            price = ((fill * price + (matched - fill) * limit) / matched
                     if fill > 0 and price is not None else limit)
            fill = matched
        if fill > 0 and price is None:
            price = p["limit"]
        return fill, price

    def _is_open(self, p):
        """True/False, or None if it could not be determined."""
        try:
            order = self.pm.get_order(order_id=p["order_id"])
        except Exception:
            try:
                return any(o.id == p["order_id"] for o in
                           self.pm.list_open_orders(token_id=p["token_id"]).iter_items())
            except Exception as e:
                log(f"order check failed for {p['order_id'][:16]}: {type(e).__name__}; retrying")
                return None
        matched = Decimal(str(order.size_matched or 0))
        if matched >= p["shares"] or str(order.status).upper() in CLOSED_STATUSES:
            return False
        return True

    def monitor(self, halt):
        for p in list(self.pending):
            is_open = self._is_open(p)
            if is_open is None:
                continue
            if is_open and (halt or self.clock() >= p["expires_at"]):
                why = "relay halt" if halt else "ttl reached"
                log(f"cancelling unfilled order {p['order_id'][:16]} ({why})")
                try:
                    self.pm.cancel_order(order_id=p["order_id"])
                except Exception as e:
                    # Do not finalize while the order may still be live.
                    log(f"cancel failed for {p['order_id'][:16]}: {type(e).__name__}: {e}; retrying")
                    continue
                is_open = False
            if is_open:
                continue
            try:
                fill, price = self.fill_truth(p)
            except Exception as e:
                p["attempts"] += 1
                if p["attempts"] >= MAX_FINALIZE_ATTEMPTS:
                    log(f"ERROR could not confirm fill for order {p['order_id']} after "
                        f"{p['attempts']} tries -- NOT reported; check your Polymarket account")
                    self.pending.remove(p)
                else:
                    log(f"fill check failed ({type(e).__name__}); retrying")
                continue
            log(f"order {p['order_id'][:16]} closed: filled {fill}/{p['shares']}"
                + (f" @ {price:.4f}" if fill > 0 else ""))
            self.report_fill(p["sig"], fill, price)
            self.pending.remove(p)

    # -- main loop ------------------------------------------------------------

    def cycle(self):
        halt = False
        try:
            halt, signals = self.poll()
            status = f"ok halt={halt} signals={len(signals)}"
            if halt:
                # No new entries; cancel anything unfilled. Filled positions ride.
                signals = []
        except RelayAuthError as e:
            signals = []
            status = "relay REJECTED the token (401) -- is it correct / still active?"
        except Exception as e:
            signals = []
            status = f"relay unreachable ({type(e).__name__}: {e})"
        if status != self.last_status or self.clock() - self.last_heartbeat >= HEARTBEAT_SECONDS:
            log(f"relay: {status} | pending orders={len(self.pending)} | "
                f"{'DRY RUN' if self.cfg.dry_run else 'LIVE'}")
            self.last_status = status
            self.last_heartbeat = self.clock()
        for sig in signals:
            self.handle_signal(sig)
        self.monitor(halt)

    def run(self):
        while True:
            try:
                self.cycle()
            except Exception as e:
                log(f"ERROR in cycle ({type(e).__name__}): {e}")
            self.sleep(MONITOR_TICK_SECONDS if self.pending else self.cfg.poll_seconds)

    def check(self):
        """--check: one relay poll, Polymarket login and balance. No orders."""
        halt, signals = self.poll()
        log(f"relay OK: token accepted, halt={halt}, {len(signals)} signal(s) right now "
            f"(0 is normal -- signals only exist in the first ~90s of a 15-minute window)")
        usdc, approved = collateral_status(self.pm)
        log(f"Polymarket OK: balance {usdc:.2f} USDC, trading approvals "
            f"{'set' if approved else 'NOT set'}")
        if not approved:
            log("  -> place one tiny trade on the Polymarket website to set approvals, then re-run")
        mode = "DRY RUN (no orders will be placed)" if self.cfg.dry_run else "LIVE (orders WILL be placed)"
        log(f"mode: {mode}; per-signal stake cap: {self.cfg.max_stake} USDC")
        return 0 if approved else 1


def main(argv):
    load_dotenv()
    if (os.environ.get("RUN_MODE") or "run").strip().lower() == "test-order":
        # One-off account check (see test_order.py); needs only the Polymarket
        # key + wallet. Exits when done -- it never enters the trading loop.
        import test_order
        test_order.run(exit_zero=True)
    cfg = load_config(os.environ)
    set_secrets(cfg.private_key, cfg.token)
    pm = connect_polymarket(cfg.private_key, cfg.wallet)
    client = Client(cfg, pm)

    if "--check" in argv:
        try:
            return client.check()
        except RelayAuthError:
            fatal("relay rejected the token (401). Check RELAY_CLIENT_TOKEN.")
        except Exception as e:
            fatal(f"check failed ({type(e).__name__}): {e}")

    log(f"Polymarket login OK for wallet {cfg.wallet}")
    usdc, approved = collateral_status(pm)
    log(f"balance {usdc:.2f} USDC; trading approvals {'set' if approved else 'NOT set'}")
    if not approved and not cfg.dry_run:
        fatal("trading approvals are not set on this account. Place one tiny trade on "
              "the Polymarket website (that sets them), then restart.")
    try:
        client.poll()
    except RelayAuthError:
        fatal("relay rejected the token (401). Check RELAY_CLIENT_TOKEN, and that RELAY_URL is right.")
    except Exception as e:
        log(f"WARNING: relay not reachable yet ({type(e).__name__}: {e}); will keep trying")

    log(f"started: {'DRY RUN -- no orders will be placed' if cfg.dry_run else 'LIVE -- real orders will be placed'}; "
        f"stake cap {cfg.max_stake} USDC; relay {cfg.relay_url}")
    client.run()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
