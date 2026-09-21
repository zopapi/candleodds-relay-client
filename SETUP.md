# Setup guide

Step-by-step for running this client on Railway against your own Polymarket
account. Log lines quoted below are copied from real runs of this code.

> Railway's button and tab names are written from memory of its dashboard;
> expect small differences.

## What you need before starting

- A Polymarket account created on the website, **funded with USDC**, and **one
  tiny trade already made on the website** (this sets the on-chain trading
  approvals; the client checks for them and tells you if they're missing).
  Keep at least **$20** in the account: with the default `MAX_STAKE_USDC=5`,
  more than one signal can arrive at the same time and each is held about 15
  minutes.
- Your **private key**: Polymarket → wallet menu → *Export private key*
  (menu labels may change).
- Your **proxy wallet address**: the `0x…` deposit address on your Polymarket
  profile. It is **not** the address the private key derives to.
- A **Railway** account with a payment method, and a **GitHub** account.
- From the person running the relay: `RELAY_URL` and `RELAY_CLIENT_TOKEN`.

## 1. Get the code into your GitHub
On this repository's page click **Fork** and create the fork under your account.

## 2. Create the Railway service
Railway → **New Project** → **Deploy from GitHub repo** → pick your fork.
The first build will start and then **fail** with
`FATAL: missing environment variable(s): …`. That's expected: nothing is set
yet. The start command comes from `railway.json` (`python relay_client.py`);
there's nothing to type.

## 3. Set the variables — in this order
Service → **Variables** → **New Variable** for each. Paste values **exactly**:
no spaces, **no quote marks**.

| # | Name | Value | Notes |
|---|------|-------|-------|
| 1 | `RELAY_URL` | `https://…` from the relay operator | A trailing `/` is fine. |
| 2 | `RELAY_CLIENT_TOKEN` | the 64-character token | Wrong or revoked → `401`. |
| 3 | `DRY_RUN` | `true` | Only `true` / `false` are accepted; a typo like `flase` stops the program instead of guessing. Unset also means dry-run. |
| 4 | `MAX_STAKE_USDC` | `5` | Your own ceiling per signal; the smaller of this and the relay's cap wins. |
| 5 | `POLYMARKET_PROXY_WALLET` | your `0x…` deposit address | |
| 6 | `POLYMARKET_PRIVATE_KEY` | your exported key — **last, entered privately** | Redacted from logs; never sent to the relay. |

Do **not** set `RUN_MODE` yet. Then click **Deploy** to apply the variables.

## 4. Read the logs (Deployments → latest deploy → View logs)
Healthy start:
```
… Polymarket login OK for wallet 0x…
… balance 25.00 USDC; trading approvals set
… started: DRY RUN -- no orders will be placed; stake cap 5 USDC; relay https://…
… relay: ok halt=False signals=0 | pending orders=0 | DRY RUN
```
That last line repeats about once a minute — it means the token was accepted
and the relay is reachable.

If not:

| Log line | Meaning / fix |
|----------|---------------|
| `FATAL: missing environment variable(s): …` | A variable name is misspelt or unset. |
| `FATAL: relay rejected the token (401)…` | Wrong `RELAY_CLIENT_TOKEN`, wrong `RELAY_URL`, or access was revoked. |
| `FATAL: Polymarket login failed (…) … does not exist` | `POLYMARKET_PROXY_WALLET` isn't a real Polymarket wallet: use the deposit address from your profile. |
| `FATAL: Polymarket login failed …` (other) | Re-export the private key. |
| `trading approvals NOT set` | Fine in dry-run; **blocks going live**. Make one tiny trade on polymarket.com, then restart. |
| `FATAL: DRY_RUN='…' is not understood` | Set it to exactly `true` or `false`. |
| `relay: relay unreachable (…)` | Wrong `RELAY_URL`, or the relay is down. It keeps retrying. |

## 5. Prove your account can place and cancel an order (relay not involved)
1. Add variable `RUN_MODE` = `test-order` → Deploy. (Only the two Polymarket
   variables are needed for this.)
2. Logs show `1/6 … 6/6` and end with **`PASSED:`**. It places one tiny
   post-only bid far below the market (worst case a few cents) and cancels it.
   The process then exits; that is normal.
3. **Delete the `RUN_MODE` variable** and Deploy again, or the service will
   re-run the test instead of trading.

A `FAILED:` line says what to fix (login, approvals, funds).

## 6. Check it is receiving signals (still dry-run)
Signals only exist in the **first ~90 seconds of a 15-minute window**
(`:00 :15 :30 :45`) and only when the relay decides there is one, so there may
be none for hours. **No signal is not a fault.** The once-a-minute `relay: ok`
line is your proof of connection.

When a signal does arrive:
```
… DRY RUN: would BUY 9.09 btc up window 1789952994 @ limit 0.55 (cap 0.55, stake 5.0) -- no order sent
… reported cancelled btc up size=0.0 price=None (fill id 12)
```
That shows polling, pricing against the live order book, and reporting all
working — everything except the order call. (In dry-run the report is a
zero-size `cancelled`; the relay operator will see it on their side.)

## 7. Go live
Change `DRY_RUN` to `false` → Deploy. The log must say
`started: LIVE -- real orders will be placed`. (If approvals are missing it
refuses to start rather than trade.) On a real signal:
```
… PLACED BUY 9.09 btc up window … @ 0.55 (cap 0.55) order 0x…
… order 0x… closed: filled 9.09/9.09 @ 0.5300
… reported filled btc up size=9.09 price=0.53 (fill id 13)
```
Or, if the price ran away: `cancelling unfilled order … (ttl reached)` →
`reported cancelled …`. A partial fill is reported as `filled` with the size
actually bought.

## 8. Stopping
- **You stop:** set `DRY_RUN=true` and Deploy (or stop the service). The restart
  forgets any in-flight order, so nothing cancels it client-side: the exchange
  expires it within about 2 to 3 minutes, and its fill report may be lost. Best done
  between signal windows.
- The relay operator can also pause signals or revoke your access from their
  side. If that happens the log shows `relay: ok halt=… signals=0` or
  `relay REJECTED the token (401)`. On a relay-wide halt the client places
  nothing new and cancels unfilled orders; filled positions are kept.

---

## What the client does and doesn't do

- Places **one** marketable limit **BUY** per signal, capped at the relay's
  price, sized `min(relay stake, MAX_STAKE_USDC) / price`. If the live best ask
  is above the cap, the order rests instead of crossing and is cancelled at the
  signal's `ttl_sec` (60s).
- **Never sells.** No sell, market-order or position-closing code exists (a test
  enforces it). Positions are held until the market resolves.
- **Winnings are not redeemed by this program.** After a market resolves,
  winning shares must be turned into USDC. I haven't verified whether Polymarket
  does that automatically for your account type; if it doesn't, use *Claim* on
  polymarket.com.
- **Never orders a window twice:** it claims the signal before the order call,
  and refuses if the exchange already shows an open order or trade on that
  market, so a restart or redeploy can't double up.
- Executes only what the relay sends. No filter, no thresholds, no model.

## Known limitations

- A restart within ~3 minutes of placing an order can lose that order's fill
  report (nothing is stored on disk). The trade is real; the report is missing.
- A fill report that fails ambiguously (the relay may or may not have saved it)
  is **not retried**, because the relay doesn't de-duplicate; the full payload is
  logged as `ERROR fill report … enter manually`.
- `outcome` and `realized_pnl` are never reported.
- If the order call fails in a way that leaves the state unknown, it is logged
  `state unknown, NOT retried; check your Polymarket open orders`.

## What was and wasn't tested

Unit tests (`pytest`), live runs against the real Polymarket API with a
throwaway key (login, live book pricing, dry-run end to end, order rejection
handling, no secrets in logs), and `test_order.py` steps 1–6 on a real funded
account (2026-09-21).

**Not yet verified:** a real fill through this client, i.e. its exchange-confirmed
fill price/size accounting against real trade records.
