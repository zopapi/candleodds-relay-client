# relay client

Runs trade signals from a signal relay on **your own Polymarket account**.
It polls the relay, places one buy per signal on your account, reports fills
back, holds to resolution, and redeems winnings afterwards. It makes no trading
decisions and never sells.

> **Deploy on Railway in the EU West region.** Other regions can authenticate
> fine and then fail on order placement, in a way that looks like a bug in this
> program rather than a region block. Set the region under the service's
> **Settings** before your first deploy. (Observed by the relay operator; the
> exact region name in Railway's dashboard may be worded differently.)

> **Use a wallet dedicated to this program.** The redemption sweep redeems
> every winning position on the account it's given, not just the ones it
> placed. If you also trade by hand on the same wallet, it will redeem those
> too. Fund the account with only what you're putting behind these signals, or
> turn the sweep off with `REDEEM_CHECK_EVERY_SECONDS=0`.

- **Setup:** see [SETUP.md](SETUP.md) (Railway, step by step).
- `relay_client.py` — the client (`--check` = one poll + login + balance, no orders).
- `test_order.py` — places one tiny order far below the market and cancels it,
  to prove your account can trade. Locally: `python test_order.py --yes`.
  On Railway: set `RUN_MODE=test-order`.
- `tests/` — `pip install -r requirements-dev.txt && pytest`.

## What it does

1. `GET {RELAY_URL}/signals/current` with header `X-Client-Token`.
2. For each signal, places one **marketable** limit BUY capped at the relay's
   `max_entry`, sized `min(stake_usdc, MAX_STAKE_USDC) / price`. It is cancelled
   if unfilled after `ttl_sec`.
3. If the relay says `halt: true`: no new orders, and every unfilled order is
   cancelled. Positions that already filled are kept.
4. Reports each finished order (filled or cancelled) with `POST {RELAY_URL}/fills`.
5. Every 60s, redeems positions that have resolved in your favour (see the
   dedicated-wallet note above). Worthless positions are skipped, and at most 5
   are redeemed per sweep.

There is no sell code at all. Redeeming a resolved position pays it out; it is
not a sale.

## Configuration

| Variable | Required | Meaning |
|---|---|---|
| `RELAY_URL` | yes | The relay's base URL. |
| `RELAY_CLIENT_TOKEN` | yes | Your token (sent as `X-Client-Token`). |
| `POLYMARKET_PRIVATE_KEY` | yes | Your exported key. Never leaves the process; redacted from logs. |
| `POLYMARKET_PROXY_WALLET` | yes | Your Polymarket deposit address. |
| `DRY_RUN` | no, default `true` | `true` prices and logs but places no order and redeems nothing. `false` trades. |
| `MAX_STAKE_USDC` | no, default `5` | Your own per-signal ceiling. |
| `POLL_INTERVAL_SECONDS` | no, default `5` | |
| `REDEEM_CHECK_EVERY_SECONDS` | no, default `60` | Redemption sweep interval; `0` turns it off, otherwise at least `10`. |
| `RUN_MODE` | no | `test-order` runs the account check once instead of the loop. |

`DRY_RUN` gates the two calls that change your account (placing an order and
redeeming a position). Polling, login, pricing, fill reporting and listing
redeemable positions always run for real. In dry-run the fill report is a
zero-size `cancelled` and the sweep logs what it *would* redeem.

## License

[MIT](LICENSE)
