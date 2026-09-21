# polymarket-relay-client

Runs trade signals from a signal relay on **your own Polymarket account**.
It polls the relay, places one buy per signal on your account, reports fills
back, and holds to resolution. It makes no trading decisions and never sells.

- **Setup:** see [SETUP.md](SETUP.md) (Railway, step by step).
- `relay_client.py` — the client (`--check` = one poll + login + balance, no orders).
- `test_order.py` — places one tiny order far below the market and cancels it,
  to prove your account can trade. Locally: `python test_order.py --yes`.
  On Railway: set `RUN_MODE=test-order`.
- `tests/` — `pip install -r requirements-dev.txt && pytest`.

## Configuration

| Variable | Required | Meaning |
|---|---|---|
| `RELAY_URL` | yes | The relay's base URL. |
| `RELAY_CLIENT_TOKEN` | yes | Your token (sent as `X-Client-Token`). |
| `POLYMARKET_PRIVATE_KEY` | yes | Your exported key. Never leaves the process; redacted from logs. |
| `POLYMARKET_PROXY_WALLET` | yes | Your Polymarket deposit address. |
| `DRY_RUN` | no, default `true` | `true` prices and logs but never places an order. `false` trades. |
| `MAX_STAKE_USDC` | no, default `5` | Your own per-signal ceiling. |
| `POLL_INTERVAL_SECONDS` | no, default `5` | |
| `RUN_MODE` | no | `test-order` runs the account check once instead of the loop. |

`DRY_RUN` gates only the order call; polling, login, pricing and fill
reporting always run for real (in dry-run the report is a zero-size
`cancelled`).
