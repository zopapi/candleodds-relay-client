"""
Tests for relay_client.py. The relay is a real local HTTP server (so requests'
retry/timeout behaviour is real); Polymarket is a recording fake that fails the
test on any call outside an allowlist or any non-BUY order.
"""

import json
import re
import socket
import sys
import threading
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import relay_client as rc  # noqa: E402
from polymarket import RequestRejectedError, UserInputError  # noqa: E402

TOKEN = "t" * 64
KEY = "0x" + "ab" * 32
NOW = 1_800_000_000
WINDOW = NOW - 20


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------

class FakeRelay:
    """Real HTTP server. signals_payload is what GET /signals/current returns;
    post_script is a list of status codes handed out to successive POST /fills."""

    def __init__(self):
        self.posts = []          # (headers, body)
        self.gets = 0
        self.get_status = 200
        self.signals_payload = {"halt": False, "signals": []}
        self.post_script = []
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                data = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                outer.gets += 1
                if self.headers.get("X-Client-Token") != TOKEN:
                    return self._send(401, {"detail": "invalid or inactive token"})
                if outer.get_status != 200:
                    return self._send(outer.get_status, {"detail": "boom"})
                self._send(200, outer.signals_payload)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.posts.append((dict(self.headers), body))
                code = outer.post_script.pop(0) if outer.post_script else 201
                self._send(code, {"id": len(outer.posts)} if code == 201 else {"detail": "err"})

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


class Items:
    def __init__(self, items):
        self._items = items

    def iter_items(self):
        return iter(self._items)


class FakePM:
    ALLOWED = {"get_order_book", "place_limit_order", "get_order", "cancel_order",
               "list_open_orders", "list_account_trades", "get_balance_allowance"}

    def __init__(self, clock=None):
        self.clock = clock
        self.calls = []
        self.open_orders = []          # objects with .id
        self.trades = []               # ClobTrade-like
        self.place_result = "accept"   # accept | reject | raise
        self.matched = Decimal(0)      # what get_order reports
        self.status = "LIVE"
        self.get_order_error = None
        self.exposure_error = None
        self.tick, self.min_size = "0.01", "5"
        self.asks = ["0.53"]
        self.order_counter = 0

    def __getattr__(self, name):
        raise AssertionError(f"client called a Polymarket method outside the allowlist: {name}")

    def _rec(self, name, **kw):
        assert name in self.ALLOWED, name
        self.calls.append((name, kw))

    def get_order_book(self, **kw):
        self._rec("get_order_book", **kw)
        return NS(tick_size=self.tick, min_order_size=self.min_size,
                  asks=[NS(price=p) for p in self.asks], bids=[])

    def list_open_orders(self, **kw):
        self._rec("list_open_orders", **kw)
        if self.exposure_error:
            raise self.exposure_error
        return Items(self.open_orders)

    def list_account_trades(self, **kw):
        self._rec("list_account_trades", **kw)
        return Items(self.trades)

    def place_limit_order(self, **kw):
        self._rec("place_limit_order", **kw)
        assert kw["side"] == "BUY", "the client must only ever place BUY orders"
        if kw.get("expiration") is not None and self.clock is not None:
            # polymarket-client 0.10.0: limit.py _MIN_EXPIRATION_BUFFER_S = 180
            if kw["expiration"] < self.clock() + 180:
                raise UserInputError("expiration must be at least 180 seconds in the future.")
        if self.place_result in ("http_400", "http_503"):
            raise RequestRejectedError("exchange said no", status=int(self.place_result[5:]))
        if self.place_result == "input_error":
            raise UserInputError("bad input")
        if self.place_result == "raise":
            raise ConnectionError("network died mid-order")
        if self.place_result == "reject":
            return NS(ok=False, code="X", message="not enough balance")
        self.order_counter += 1
        return NS(ok=True, order_id=f"0xorder{self.order_counter:04d}" + "0" * 20, status="live")

    def get_order(self, **kw):
        self._rec("get_order", **kw)
        if self.get_order_error:
            raise self.get_order_error
        return NS(size_matched=self.matched, status=self.status)

    def cancel_order(self, **kw):
        self._rec("cancel_order", **kw)
        self.status = "CANCELED"
        return NS(canceled=[kw["order_id"]])

    def n(self, name):
        return sum(1 for c, _ in self.calls if c == name)


class Clock:
    def __init__(self):
        self.t = float(NOW)

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def make_cfg(relay, dry_run=False, max_stake="5"):
    return rc.Config(relay_url=relay.url, token=TOKEN, private_key=KEY, wallet="0xwallet",
                     dry_run=dry_run, max_stake=Decimal(max_stake), poll_seconds=5)


def sig(symbol="btc", side="up", max_entry=0.556, stake=5.0, ttl=60, window=WINDOW):
    return {"symbol": symbol, "window_start_ts": window, "side": side, "max_entry": max_entry,
            "stake_usdc": stake, "ttl_sec": ttl, "token_id": f"tok-{symbol}-{side}",
            "condition_id": f"0xcond-{symbol}"}


@pytest.fixture
def relay():
    r = FakeRelay()
    yield r
    r.close()


@pytest.fixture
def env(relay):
    clock = Clock()
    pm = FakePM(clock)

    def build(**cfg_kw):
        return rc.Client(make_cfg(relay, **cfg_kw), pm, clock=clock, sleep=clock.sleep)

    return NS(relay=relay, pm=pm, clock=clock, build=build)


def taker_trade(order_id, size, price):
    return NS(status="CONFIRMED", taker_order_id=order_id, size=size, price=price, maker_orders=[])


# --------------------------------------------------------------------------
# Order path
# --------------------------------------------------------------------------

def test_dry_run_prices_live_but_never_places_and_still_reports(env):
    env.relay.signals_payload = {"halt": False, "signals": [sig()]}
    c = env.build(dry_run=True)
    c.cycle()
    assert env.pm.n("get_order_book") == 1            # pricing ran
    assert env.pm.n("place_limit_order") == 0         # the one gated call
    assert c.pending == []
    (_, body), = env.relay.posts                       # reporting ran
    assert body["status"] == "cancelled" and body["size"] == 0
    assert body["actual_fill_price"] is None and body["max_entry_sent"] == 0.556


def test_live_full_fill_reports_real_taker_price(env):
    env.relay.signals_payload = {"halt": False, "signals": [sig(max_entry=0.556, stake=5.0)]}
    c = env.build()
    c.cycle()
    (name, kw), = [x for x in env.pm.calls if x[0] == "place_limit_order"]
    assert kw["side"] == "BUY" and kw["post_only"] is False
    assert kw["price"] == Decimal("0.55")              # cap floored to the 0.01 tick
    assert kw["size"] == Decimal("9.09")               # 5 / 0.55 floored to 2dp
    assert kw["expiration"] == NOW + 190               # exchange-side backstop (SDK floor is 180)
    assert len(c.pending) == 1 and env.relay.posts == []

    oid = c.pending[0]["order_id"]
    env.pm.matched = Decimal("9.09")
    env.pm.trades = [taker_trade(oid, "9.09", "0.53")]  # filled better than the cap
    env.clock.sleep(2)
    c.cycle()
    (_, body), = env.relay.posts
    assert body["status"] == "filled" and body["size"] == 9.09
    assert body["actual_fill_price"] == pytest.approx(0.53)
    assert body["side"] == "up" and body["symbol"] == "btc" and body["window_start_ts"] == WINDOW
    assert c.pending == []


def test_no_fill_cancels_at_ttl_and_reports_cancelled(env):
    env.relay.signals_payload = {"halt": False, "signals": [sig(ttl=60)]}
    c = env.build()
    c.cycle()
    env.clock.sleep(30)
    c.cycle()
    assert env.pm.n("cancel_order") == 0 and env.relay.posts == []   # not yet
    env.clock.sleep(31)
    c.cycle()
    assert env.pm.n("cancel_order") == 1
    (_, body), = env.relay.posts
    assert body["status"] == "cancelled" and body["size"] == 0 and body["actual_fill_price"] is None


def test_partial_fill_then_cancel_reports_actual_size(env):
    env.relay.signals_payload = {"halt": False, "signals": [sig(ttl=60)]}
    c = env.build()
    c.cycle()
    oid = c.pending[0]["order_id"]
    env.pm.matched = Decimal("3")                       # order status knows 3 shares
    env.pm.trades = [taker_trade(oid, "2", "0.50")]     # feed has only itemized 2
    env.clock.sleep(61)
    c.cycle()
    (_, body), = env.relay.posts
    assert body["status"] == "filled" and body["size"] == 3.0
    # 2 @ 0.50 from the feed + 1 unitemized share priced at the 0.55 limit
    assert body["actual_fill_price"] == pytest.approx((2 * 0.50 + 1 * 0.55) / 3)


def test_halt_cancels_unfilled_and_blocks_new_entries(env):
    env.relay.signals_payload = {"halt": False, "signals": [sig(symbol="btc")]}
    c = env.build()
    c.cycle()
    assert env.pm.n("place_limit_order") == 1
    # relay now says halt AND (as the real relay does) still lists a fresh signal
    env.relay.signals_payload = {"halt": True, "signals": [sig(symbol="eth")]}
    env.clock.sleep(2)
    c.cycle()
    assert env.pm.n("cancel_order") == 1
    assert env.pm.n("place_limit_order") == 1           # eth NOT ordered
    assert env.relay.posts and env.relay.posts[0][1]["status"] == "cancelled"


def test_stake_clamped_to_local_cap(env):
    env.pm.min_size = "1"
    env.relay.signals_payload = {"halt": False, "signals": [sig(stake=50.0, max_entry=0.50)]}
    env.build(max_stake="2").cycle()
    (_, kw), = [x for x in env.pm.calls if x[0] == "place_limit_order"]
    assert kw["size"] == Decimal("4.00")                # 2 / 0.50, not 50 / 0.50


def test_exchange_rejection_is_reported_as_no_fill(env):
    env.pm.place_result = "reject"
    env.relay.signals_payload = {"halt": False, "signals": [sig()]}
    c = env.build()
    c.cycle()
    assert c.pending == []
    assert env.relay.posts[0][1]["status"] == "cancelled"


def test_below_min_order_size_is_skipped_not_ordered(env):
    env.pm.min_size = "50"
    env.relay.signals_payload = {"halt": False, "signals": [sig()]}
    env.build().cycle()
    assert env.pm.n("place_limit_order") == 0


# --------------------------------------------------------------------------
# Never double-order
# --------------------------------------------------------------------------

def test_same_signal_on_every_poll_orders_once(env):
    env.relay.signals_payload = {"halt": False, "signals": [sig()]}
    c = env.build()
    for _ in range(4):
        c.cycle()
        env.clock.sleep(2)
    assert env.pm.n("place_limit_order") == 1


def test_restart_does_not_reorder_when_exchange_shows_open_order(env):
    env.pm.open_orders = [NS(id="0xleftover")]
    env.relay.signals_payload = {"halt": False, "signals": [sig()]}
    env.build().cycle()                                  # a "fresh process": empty memory
    assert env.pm.n("place_limit_order") == 0


def test_restart_does_not_reorder_when_exchange_shows_a_trade(env):
    env.pm.trades = [taker_trade("0xold", "9", "0.5")]
    env.relay.signals_payload = {"halt": False, "signals": [sig()]}
    env.build().cycle()
    assert env.pm.n("place_limit_order") == 0


def test_exposure_check_failure_fails_closed_then_retries_next_poll(env):
    env.pm.exposure_error = TimeoutError("api down")
    env.relay.signals_payload = {"halt": False, "signals": [sig()]}
    c = env.build()
    c.cycle()
    assert env.pm.n("place_limit_order") == 0
    env.pm.exposure_error = None                          # API recovers within the signal's life
    env.clock.sleep(5)
    c.cycle()
    assert env.pm.n("place_limit_order") == 1


def test_order_call_that_raises_is_never_retried(env):
    env.pm.place_result = "raise"
    env.relay.signals_payload = {"halt": False, "signals": [sig()]}
    c = env.build()
    for _ in range(3):
        c.cycle()
        env.clock.sleep(2)
    assert env.pm.n("place_limit_order") == 1             # state unknown => never re-sent


def test_stale_and_malformed_signals_are_ignored(env):
    old = sig(window=NOW - 1000)
    bad = sig(); bad["side"] = "sideways"
    missing = sig(); del missing["token_id"]
    env.relay.signals_payload = {"halt": False, "signals": [old, bad, missing]}
    env.build().cycle()
    assert env.pm.n("place_limit_order") == 0


# --------------------------------------------------------------------------
# Hold to resolution: there is no sell path
# --------------------------------------------------------------------------

def test_source_has_no_sell_path():
    src = (Path(__file__).resolve().parent.parent / "relay_client.py").read_text()
    code = "\n".join(l for l in src.splitlines() if not l.strip().startswith("#"))
    assert not re.search(r"""['"]SELL['"]""", code)
    assert not re.search(r"place_market_order|create_market_order|cancel_all|split_position|merge_", code)
    assert 'BUY = "BUY"' in src and code.count("place_limit_order(") == 1


# --------------------------------------------------------------------------
# Relay auth / availability
# --------------------------------------------------------------------------

def test_401_does_not_crash_the_loop(env):
    c = env.build()
    c.cfg.token = "wrong"
    c.cycle()                                              # logs, keeps running
    assert "REJECTED" in c.last_status


def test_relay_5xx_does_not_crash_and_local_ttl_still_cancels(env):
    env.relay.signals_payload = {"halt": False, "signals": [sig(ttl=60)]}
    c = env.build()
    c.cycle()
    env.relay.get_status = 500
    env.clock.sleep(61)
    c.cycle()
    assert "unreachable" in c.last_status
    assert env.pm.n("cancel_order") == 1                   # relay down != orders left resting


# --------------------------------------------------------------------------
# Fill-report retry semantics (the relay has no de-duplication)
# --------------------------------------------------------------------------

def _report(c):
    return c.report_fill(sig(), Decimal("1"), Decimal("0.5"))


def test_report_retries_gateway_errors_then_succeeds(env):
    env.relay.post_script = [503, 502, 201]
    assert _report(env.build()) is True
    assert len(env.relay.posts) == 3


def test_report_does_not_retry_a_500(env):
    env.relay.post_script = [500, 201]
    assert _report(env.build()) is False
    assert len(env.relay.posts) == 1                       # might have been recorded


def test_report_does_not_retry_a_4xx(env):
    env.relay.post_script = [400]
    assert _report(env.build()) is False
    assert len(env.relay.posts) == 1


def test_report_does_not_retry_after_a_read_timeout(env):
    c = env.build()
    calls = []

    class H:
        def post(self, *a, **k):
            calls.append(1)
            raise requests.ReadTimeout("slow")

    c.http = H()
    assert _report(c) is False
    assert len(calls) == 1


def test_report_retries_when_relay_is_unreachable(env):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()  # closed port
    c = env.build()
    c.cfg.relay_url = f"http://127.0.0.1:{port}"
    n = []
    real_post = c.http.post
    c.http.post = lambda *a, **k: (n.append(1), real_post(*a, **k))[1]
    assert _report(c) is False
    assert len(n) == 4                                     # connect failures prove nothing was sent


# --------------------------------------------------------------------------
# Config + secrets
# --------------------------------------------------------------------------

BASE_ENV = {"RELAY_URL": "https://r.example/", "RELAY_CLIENT_TOKEN": "tok",
            "POLYMARKET_PRIVATE_KEY": KEY, "POLYMARKET_PROXY_WALLET": "0xw"}


def test_dry_run_defaults_to_true_and_url_is_normalised():
    cfg = rc.load_config(dict(BASE_ENV))
    assert cfg.dry_run is True and cfg.relay_url == "https://r.example"


@pytest.mark.parametrize("v,expected", [("false", False), ("FALSE", False), ("0", False),
                                        ("true", True), ("on", True)])
def test_dry_run_parsing(v, expected):
    assert rc.load_config({**BASE_ENV, "DRY_RUN": v}).dry_run is expected


@pytest.mark.parametrize("v", ["flase", "", "maybe"])
def test_dry_run_typo_is_fatal_or_defaults_dry(v):
    if v == "":
        assert rc.load_config({**BASE_ENV, "DRY_RUN": v}).dry_run is True   # blank => safe default
    else:
        with pytest.raises(SystemExit):
            rc.load_config({**BASE_ENV, "DRY_RUN": v})


def test_missing_variable_is_fatal():
    e = dict(BASE_ENV); del e["POLYMARKET_PRIVATE_KEY"]
    with pytest.raises(SystemExit):
        rc.load_config(e)


def test_secrets_are_redacted_in_logs(capsys):
    rc.set_secrets(KEY, TOKEN)
    rc.log(f"login with {KEY} and {KEY[2:]} token {TOKEN}")
    out = capsys.readouterr().out
    assert KEY not in out and KEY[2:] not in out and TOKEN not in out and "[REDACTED]" in out
    rc.set_secrets()


def test_expiration_backstop_covers_long_ttls_and_respects_sdk_floor(env):
    env.relay.signals_payload = {"halt": False, "signals": [sig(ttl=300)]}
    env.build().cycle()
    (_, kw), = [x for x in env.pm.calls if x[0] == "place_limit_order"]
    assert kw["expiration"] == NOW + 425                # ttl + 125: backstop outlives the ttl


def test_sdk_refusing_the_order_is_logged_as_not_sent_and_not_retried(env, capsys):
    env.pm.place_result = "input_error"
    env.relay.signals_payload = {"halt": False, "signals": [sig()]}
    c = env.build()
    for _ in range(2):
        c.cycle(); env.clock.sleep(2)
    assert env.pm.n("place_limit_order") == 1
    assert "order NOT sent" in capsys.readouterr().out


def test_exchange_4xx_is_a_definitive_rejection_reported_as_no_fill(env):
    env.pm.place_result = "http_400"
    env.relay.signals_payload = {"halt": False, "signals": [sig()]}
    c = env.build()
    c.cycle()
    assert c.pending == [] and env.relay.posts[0][1]["status"] == "cancelled"


def test_exchange_5xx_is_state_unknown_never_retried_never_reported(env):
    env.pm.place_result = "http_503"
    env.relay.signals_payload = {"halt": False, "signals": [sig()]}
    c = env.build()
    for _ in range(2):
        c.cycle(); env.clock.sleep(2)
    assert env.pm.n("place_limit_order") == 1 and env.relay.posts == []


def test_run_mode_test_order_dispatches_and_never_enters_the_loop(monkeypatch):
    import test_order
    called = {}
    monkeypatch.setenv("RUN_MODE", "test-order")
    for k in ("RELAY_URL", "RELAY_CLIENT_TOKEN"):
        monkeypatch.delenv(k, raising=False)          # not needed in this mode
    monkeypatch.setattr(test_order, "run", lambda exit_zero: (called.update(z=exit_zero), sys.exit(0)))
    monkeypatch.setattr(rc, "connect_polymarket", lambda *a: pytest.fail("loop must not start"))
    with pytest.raises(SystemExit) as e:
        rc.main([])
    assert called == {"z": True} and e.value.code == 0
