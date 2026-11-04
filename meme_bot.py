# meme_bot.py - PAPER-ONLY Solana memecoin bot. PRETEND MONEY. It never touches a wallet, a key or a real exchange.
# It reads public data (GeckoTerminal, RugCheck), decides with fixed rules, and logs pretend trades with realistic costs.
# Files: meme_state.json, meme_trades.csv, meme_equity.csv, meme_funnel.csv, meme_probe.json
# v2: wider stop + breakeven + trail that clears costs, no time-stopping winners, stricter entries,
#     risk-based sizing, loss-streak pause. See CHANGES.md. Still paper only.
import csv
import json
import math
import os
import subprocess
import time
from datetime import datetime, timedelta, timezone

import requests

START_CASH = 1000.0
# ---- v2 rules. Why each changed is in CHANGES.md ----
# Size by risk, not a flat %: lose about RISK_PCT of the account if the stop is hit (after costs).
RISK_PCT = 1.0              # % of account at risk per trade
MAX_SIZE_PCT = 0.10         # never more than 10% of the account in one coin
MAX_POSITIONS = 4
MAX_BUYS_PER_RUN = 1
# Exits. v1 trailed from +12% with a 10% drop, which could sell at +0.8% = a loss after ~3.6% costs.
STOP_PCT = 12.0             # v1: 6%, inside normal memecoin noise, so it got hit constantly
BREAKEVEN_AT_PCT = 15.0     # once up 15%, the stop moves to entry + costs: a winner can't turn into a loser
TRAIL_START_PCT = 25.0      # trailing switches on at +25% ...
TRAIL_DROP_PCT = 12.0       # ... and sells after a 12% fall from the peak (worst trail exit: +10% before costs)
TIME_LIMIT_H = 24           # only for dead trades that never reached BREAKEVEN_AT_PCT
MAX_HOURS = 72              # absolute cap for everything else
# Loss-streak brake: after this many losers in a row, buy nothing for PAUSE_HOURS.
LOSS_STREAK, PAUSE_HOURS = 3, 6
# Entry filters. v1 bought the biggest 1h pumps on coins 30 min old.
MIN_LIQ, MIN_H1_VOL = 60_000, 40_000
MIN_AGE_MIN, MAX_AGE_H = 120, 24   # skip the first 2 hours, when most rugs and dumps happen
MIN_FDV, MAX_FDV = 150_000, 20_000_000
MIN_H1_CHG, MAX_H1_CHG = 8.0, 40.0 # v1 allowed +120%: buying the top of a vertical candle
MAX_M5_CHG, MIN_M5_CHG = 10.0, -4.0  # not mid-spike, and not already rolling over
MIN_H6_CHG = 0.0            # the 6h trend must be up too, not a dead-cat bounce
MIN_BUYERS, MIN_SELLERS, MIN_BUY_SELL_RATIO = 100, 30, 1.2
MAX_BUYS_PER_BUYER = 4.0    # lots of buys from few wallets = bots / wash trading
RUG_LIQ_FRACTION = 0.30     # liquidity falling below 30% of entry = rug
NEW_PAGES = 5               # newest-pool pages scanned per run (20 pools each)
WATCH_MAX = 1500            # coins remembered while they mature (raised: coins now wait 2h+)
REFRESH_PER_RUN = 120       # remembered coins re-checked per run
WRITE_OFF_AFTER_MISSING = 8 # runs without data (about 2 hours) = total loss
FEE_PCT = 0.5               # DEX fee per side
BASE_SLIP_PCT = 1.0         # minimum slippage per side
NETWORK_FEE_USD = 0.25      # priority fee / tip per trade
GT = "https://api.geckoterminal.com/api/v2"
SOL_MINT = "So11111111111111111111111111111111111111112"
STATE, TRADES, EQUITY, FUNNEL, PROBE = "meme_state.json", "meme_trades.csv", "meme_equity.csv", "meme_funnel.csv", "meme_probe.json"
SHADOW = "meme_shadow.csv"
CHECKPOINTS_H = (1, 4, 24)    # shadow log: look again this many hours after a coin passes the filters
SHADOW_MAX = 300
HEADERS = {"Accept": "application/json;version=20230302", "User-Agent": "paper-meme-bot/1.0"}


def num(x):
    try:
        v = float(x)
        return None if math.isnan(v) else v
    except (TypeError, ValueError):
        return None


def http_json(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            print(f"data: {r.status_code} from {url.split('?')[0][:90]}")
            return None
        return r.json()
    except Exception as e:
        print(f"data: failed {url.split('?')[0][:90]} ({e})")
        return None


def append_csv(path, header, row):
    if os.path.exists(path):                 # columns changed since v1: keep the old file, start a fresh one
        with open(path, newline="") as f:
            old = next(csv.reader(f), None)
        if old is not None and old != header:
            os.replace(path, path.replace(".csv", "_v1.csv"))
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow(row)


def load_state():
    try:
        with open(STATE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def parse_pool(item):
    """One GeckoTerminal pool -> flat dict, or None if it can't be read."""
    try:
        a = item["attributes"]
        base = (item.get("relationships", {}).get("base_token", {}).get("data", {}) or {}).get("id", "")
        h1 = (a.get("transactions") or {}).get("h1") or {}
        return {"pool": a["address"], "name": a.get("name", "?"), "mint": base.split("_", 1)[-1] if base else None,
                "price": num(a.get("base_token_price_usd")), "liq": num(a.get("reserve_in_usd")), "fdv": num(a.get("fdv_usd")),
                "vol1h": num((a.get("volume_usd") or {}).get("h1")), "chg1h": num((a.get("price_change_percentage") or {}).get("h1")),
                "chg5m": num((a.get("price_change_percentage") or {}).get("m5")),
                "chg6h": num((a.get("price_change_percentage") or {}).get("h6")),
                "buys": num(h1.get("buys")), "sells": num(h1.get("sells")), "buyers": num(h1.get("buyers")), "sellers": num(h1.get("sellers")),
                "created": a.get("pool_created_at")}
    except Exception:
        return None


def age_minutes(created, now):
    try:
        t = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
        return (now - t).total_seconds() / 60
    except Exception:
        return None


def rug_ok(mint, get=http_json):
    """RugCheck summary. Fail closed: anything unreadable counts as NOT ok."""
    if not mint:
        return False, "no mint"
    d = get(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary")
    if not isinstance(d, dict) or "risks" not in d or not isinstance(d["risks"], list):
        return False, "unreadable"
    bad = [str(r.get("name", "?")) for r in d["risks"] if isinstance(r, dict) and str(r.get("level", "")).lower() == "danger"]
    return (not bad), (", ".join(bad) if bad else "clean")


def cost_pct(size_usd, liq):
    impact = (size_usd / (liq / 2)) * 100 if liq else 100.0
    return FEE_PCT + max(BASE_SLIP_PCT, 2 * impact)


def round_trip_pct(size_usd, liq):
    """Buy + sell cost as % of the position, network fees included."""
    return 2 * cost_pct(size_usd, liq) + (2 * NETWORK_FEE_USD / size_usd * 100 if size_usd else 0)


def position_size(total, cash, liq):
    """Size so a stop-out loses about RISK_PCT of the account after costs, capped at MAX_SIZE_PCT."""
    usd = min(total * MAX_SIZE_PCT, cash)
    for _ in range(3):                       # costs depend on size, so settle it in a few passes
        loss_frac = (STOP_PCT + round_trip_pct(usd, liq)) / 100
        usd = min(total * RISK_PCT / 100 / loss_frac, total * MAX_SIZE_PCT, cash)
    return usd


# A trailing exit must clear costs even in the worst case, or the "profit" stop is really a loss stop (the v1 bug).
assert (1 + TRAIL_START_PCT / 100) * (1 - TRAIL_DROP_PCT / 100) - 1 > 0.04, "trail settings can sell below costs"


def equity(s, prices):
    return s["cash"] + sum(p["qty"] * prices.get(k, p.get("last", p["avg"])) for k, p in s["positions"].items())


def log_trade(now, coin, side, qty, price, fee, usd, rule, why):
    append_csv(TRADES, ["time_utc", "coin", "side", "qty", "price", "fee_usd", "usd", "rule", "reason"],
               [now.isoformat(timespec="seconds"), coin, side, f"{qty:.8g}", f"{price:.10g}", round(fee, 4), round(usd, 2), rule, why])


def sell(s, key, price, liq, now, rule, why):
    p = s["positions"].pop(key)
    gross = p["qty"] * price
    pct = cost_pct(gross, liq) if price > 0 else 0
    fee = gross * pct / 100 + (NETWORK_FEE_USD if price > 0 else 0)
    proceeds = max(gross - fee, 0)
    s["cash"] += proceeds
    s["cooldown"].append(key)
    pnl = (price / p["avg"] - 1) * 100 if p["avg"] else 0
    spent = p.get("cost_usd")
    net = (proceeds / spent - 1) * 100 if spent else None
    # loss-streak brake: count consecutive net losers, pause new buys after LOSS_STREAK of them
    lost = (net if net is not None else pnl) < 0
    s["streak"] = s.get("streak", 0) + 1 if lost else 0
    if s["streak"] >= LOSS_STREAK:
        s["paused_until"] = (now + timedelta(hours=PAUSE_HOURS)).isoformat(timespec="seconds")
        s["streak"] = 0
        print(f"{LOSS_STREAK} losers in a row: no new buys until {s['paused_until']}")
    after = f", {net:+.1f}% after costs" if net is not None else ""
    log_trade(now, p["name"], "sell", p["qty"], price, fee, proceeds, rule, f"{why} ({pnl:+.1f}% before costs{after})")


def fetch_candidates(get=http_json):
    pools, answered = {}, False
    urls = [f"{GT}/networks/solana/new_pools?page={k}" for k in range(1, NEW_PAGES + 1)] + [f"{GT}/networks/solana/trending_pools"]
    raw_first = None
    for u in urls:
        d = get(u)
        if not isinstance(d, dict):
            continue
        answered = True
        for it in d.get("data", []) or []:
            if raw_first is None:
                raw_first = it
            p = parse_pool(it)
            if p and p["price"]:
                pools[p["pool"]] = p
        time.sleep(2.2)
    return pools, raw_first, answered


def fetch_held(addrs, get=http_json):
    out = {}
    for k in range(0, len(addrs), 30):
        chunk = addrs[k:k + 30]
        d = get(f"{GT}/networks/solana/pools/multi/{','.join(chunk)}")
        if isinstance(d, dict):
            for it in d.get("data", []) or []:
                p = parse_pool(it)
                if p and p["price"]:
                    out[p["pool"]] = p
        time.sleep(2.2)
    return out


def sol_price(get=http_json):
    d = get(f"{GT}/simple/networks/solana/token_price/{SOL_MINT}")
    try:
        return num(d["data"]["attributes"]["token_prices"][SOL_MINT])
    except Exception:
        return None


def check_exits(s, held, cands, prices, now, count_missing=True):
    """Stop loss, trailing stop, rug and time limit, using the prices we can actually see (not the stop level)."""
    for key in list(s["positions"]):
        p = s["positions"][key]
        cur = held.get(key) or cands.get(key)
        if not cur:
            if not count_missing:
                continue          # fast checks never write a coin off: one missed look is not a vanished coin
            p["missing"] = p.get("missing", 0) + 1
            if p["missing"] >= WRITE_OFF_AFTER_MISSING:
                sell(s, key, 0.0, 0, now, "vanished", "no data for about 2 hours, written off as a total loss")
            continue
        p["missing"] = 0
        px, liq = cur["price"], cur["liq"] or 0
        p["last"] = px
        prices[key] = px
        p["peak"] = max(p["peak"], px)
        chg = (px / p["avg"] - 1) * 100
        peak_gain = (p["peak"] / p["avg"] - 1) * 100
        drop = (1 - px / p["peak"]) * 100
        held_h = (now - datetime.fromisoformat(p["bought"])).total_seconds() / 3600
        # once up BREAKEVEN_AT_PCT, the floor is entry + round-trip costs, so the trade can't end as a loser
        be_floor = p.get("rt_cost", 3.5) + 0.5
        reached_be = peak_gain >= BREAKEVEN_AT_PCT
        if p["liq"] and liq < RUG_LIQ_FRACTION * p["liq"]:
            sell(s, key, px, liq, now, "rug", f"liquidity collapsed to ${liq:,.0f}")
        elif chg <= -STOP_PCT:
            sell(s, key, px, liq, now, "stop loss", f"price {chg:+.1f}% from entry")
        elif peak_gain >= TRAIL_START_PCT and drop >= TRAIL_DROP_PCT:
            sell(s, key, px, liq, now, "trailing stop", f"fell {drop:.1f}% from its +{peak_gain:.0f}% peak")
        elif reached_be and chg <= be_floor:
            sell(s, key, px, liq, now, "breakeven stop", f"was up {peak_gain:.0f}%, fell back to {chg:+.1f}%")
        elif not reached_be and held_h >= TIME_LIMIT_H:
            sell(s, key, px, liq, now, "time limit", f"held {held_h:.0f}h and never reached +{BREAKEVEN_AT_PCT:.0f}%")
        elif held_h >= MAX_HOURS:
            sell(s, key, px, liq, now, "max hold", f"held {held_h:.0f}h")


def shadow_register(s, passed, now):
    """Remember every coin that passes the filters (bought or not) so we can see how it did later."""
    sh = s.setdefault("shadow", {})
    for c in passed:
        if c["pool"] in sh or len(sh) >= SHADOW_MAX:
            continue
        sh[c["pool"]] = {"name": c["name"], "t": now.isoformat(timespec="seconds"), "entry": c["price"], "liq": c["liq"],
                         "vol1h": c["vol1h"], "chg1h": c["chg1h"], "buyers": c["buyers"], "age": age_minutes(c["created"], now),
                         "bought": 0, "done": []}


def shadow_step(s, now, get=http_json):
    """Log the price of shadow coins at each checkpoint (1h, 4h, 24h). Only ever reads prices, never trades."""
    sh = s.get("shadow") or {}
    due = []
    for k, e in sh.items():
        hrs = (now - datetime.fromisoformat(e["t"])).total_seconds() / 3600
        if any(h <= hrs and h not in e["done"] for h in CHECKPOINTS_H):
            due.append(k)
    if not due:
        return 0
    got = fetch_held(due[:60], get)
    logged = 0
    for k in due[:60]:
        e = sh[k]
        hrs = (now - datetime.fromisoformat(e["t"])).total_seconds() / 3600
        cur = got.get(k)
        for h in CHECKPOINTS_H:
            if h > hrs or h in e["done"]:
                continue
            if cur is None and hrs < h + 2:
                continue                  # one missed look is not a dead coin: try again next run
            px = cur["price"] if cur else 0.0
            append_csv(SHADOW, ["time_utc", "name", "pool", "checkpoint_h", "entry_price", "price", "ret_pct", "liq_entry", "liq_now",
                                "chg1h_at_entry", "vol1h", "buyers", "age_min", "bought"],
                       [now.isoformat(timespec="seconds"), e["name"], k, h, f"{e['entry']:.10g}", f"{px:.10g}",
                        round((px / e["entry"] - 1) * 100, 2) if e["entry"] else "", round(e["liq"] or 0), round((cur or {}).get("liq") or 0),
                        round(e["chg1h"] or 0, 1), round(e["vol1h"] or 0), int(e["buyers"] or 0),
                        round(e["age"]) if e["age"] is not None else "", e["bought"]])
            e["done"].append(h)
            logged += 1
    for k in [k for k, e in sh.items() if len(e["done"]) == len(CHECKPOINTS_H)]:
        del sh[k]
    return logged


def run_once(get=http_json, rug=rug_ok, now=None, pause=True):
    now = now or datetime.now(timezone.utc)
    s = load_state() or {"cash": START_CASH, "positions": {}, "cooldown": [], "bench": None, "start": now.isoformat(timespec="seconds")}
    sp = sol_price(get)
    if s["bench"] is None and sp:
        s["bench"] = {"sol_qty": START_CASH / sp}
    fresh, raw_first, answered = fetch_candidates(get)
    if raw_first is not None and not os.path.exists(PROBE):
        with open(PROBE, "w") as f:
            json.dump(raw_first, f, indent=1)
    # remember every coin we see, then re-check the ones that have had time to prove themselves
    watch = s.setdefault("watch", {})
    for k, c in fresh.items():
        if k not in watch:
            a0 = age_minutes(c["created"], now)
            watch[k] = (now - timedelta(minutes=a0)).isoformat(timespec="seconds") if a0 is not None else now.isoformat(timespec="seconds")
    for k in list(watch):
        a0 = (now - datetime.fromisoformat(watch[k])).total_seconds() / 60
        if a0 > MAX_AGE_H * 60:
            del watch[k]
    if len(watch) > WATCH_MAX:                # over the cap: forget the oldest first
        for k in sorted(watch, key=lambda k: watch[k])[:len(watch) - WATCH_MAX]:
            del watch[k]
    mature = sorted((k for k in watch if k not in fresh and k not in s["positions"]
                     and (now - datetime.fromisoformat(watch[k])).total_seconds() / 60 >= MIN_AGE_MIN),
                    key=lambda k: watch[k], reverse=True)[:REFRESH_PER_RUN]
    got = fetch_held(list(s["positions"]) + mature, get) if (s["positions"] or mature) else {}
    held = {k: v for k, v in got.items() if k in s["positions"]}
    cands = {**fresh, **{k: v for k, v in got.items() if k not in s["positions"]}}
    if not answered:
        print("no data this run (feed unreachable), stopping safely (nothing traded)")
        return
    prices = {}
    check_exits(s, held, cands, prices, now, count_missing=True)
    # ---- the funnel: why coins are rejected ----
    ages = [a1 for a1 in (age_minutes(c["created"], now) for c in cands.values()) if a1 is not None]
    if ages:
        print(f"ages of coins seen: youngest {min(ages):.0f} min, oldest {max(ages)/60:.1f} h, remembered {len(watch)}")
    f = dict(scanned=len(cands), young_old=0, low_liq=0, low_vol=0, bad_fdv=0, weak_mom=0, overheated=0, few_buyers=0,
             sell_pressure=0, owned_or_cooldown=0, rug_check=0, passed=0, bought=0,
             spiking=0, rolling_over=0, weak_trend=0, wash_trading=0, paused=0)
    passed = []
    for key, c in cands.items():
        age = age_minutes(c["created"], now)
        if key in s["positions"] or key in s["cooldown"]:
            f["owned_or_cooldown"] += 1
        elif age is None or not (MIN_AGE_MIN <= age <= MAX_AGE_H * 60):
            f["young_old"] += 1
        elif (c["liq"] or 0) < MIN_LIQ:
            f["low_liq"] += 1
        elif (c["vol1h"] or 0) < MIN_H1_VOL:
            f["low_vol"] += 1
        elif not (MIN_FDV <= (c["fdv"] or 0) <= MAX_FDV):
            f["bad_fdv"] += 1
        elif c["chg1h"] is None or c["chg1h"] < MIN_H1_CHG:
            f["weak_mom"] += 1
        elif c["chg1h"] > MAX_H1_CHG:
            f["overheated"] += 1
        elif (c["buyers"] or 0) < MIN_BUYERS:
            f["few_buyers"] += 1
        elif (c["sellers"] or 0) < MIN_SELLERS or (c["sells"] or 0) == 0 or (c["buys"] or 0) / c["sells"] < MIN_BUY_SELL_RATIO:
            f["sell_pressure"] += 1
        elif (c["buys"] or 0) / max(c["buyers"] or 1, 1) > MAX_BUYS_PER_BUYER:
            f["wash_trading"] += 1
        elif c["chg5m"] is not None and c["chg5m"] > MAX_M5_CHG:
            f["spiking"] += 1
        elif c["chg5m"] is not None and c["chg5m"] < MIN_M5_CHG:
            f["rolling_over"] += 1
        elif c["chg6h"] is not None and c["chg6h"] < MIN_H6_CHG:
            f["weak_trend"] += 1
        else:
            passed.append(c)
    # v1 ranked by the biggest 1h pump (buying tops). Rank by real participation instead: unique buyers per $ of liquidity.
    passed.sort(key=lambda c: (c["buyers"] or 0) / max(c["liq"] or 1, 1), reverse=True)
    paused = s.get("paused_until") and now < datetime.fromisoformat(s["paused_until"])
    f["passed"] = len(passed)
    shadow_register(s, passed, now)
    if paused and passed:
        f["paused"] = len(passed)
        print(f"loss-streak pause until {s['paused_until']}: not buying")
    for c in ([] if paused else passed[:8]):
        if len(s["positions"]) >= MAX_POSITIONS or f["bought"] >= MAX_BUYS_PER_RUN:
            break
        ok, why = rug(c["mint"], get) if rug is rug_ok else rug(c["mint"])
        if not ok:
            f["rug_check"] += 1
            continue
        total = equity(s, prices)
        usd = position_size(total, s["cash"], c["liq"])
        if usd < 10:
            break
        pct = cost_pct(usd, c["liq"])
        fee = usd * pct / 100 + NETWORK_FEE_USD
        qty = (usd - fee) / c["price"]
        s["cash"] -= usd
        s["positions"][c["pool"]] = {"name": c["name"], "qty": qty, "avg": c["price"], "peak": c["price"], "last": c["price"],
                                     "liq": c["liq"], "bought": now.isoformat(timespec="seconds"), "missing": 0,
                                     "cost_usd": usd, "rt_cost": round(round_trip_pct(usd, c["liq"]), 2)}
        prices[c["pool"]] = c["price"]
        log_trade(now, c["name"], "buy", qty, c["price"], fee, usd, "momentum",
                  f"+{c['chg1h']:.0f}% in 1h, liquidity ${c['liq']:,.0f}, {int(c['buyers'])} buyers, rug check {why}")
        f["bought"] += 1
        s["shadow"].get(c["pool"], {})["bought"] = 1
    s["cooldown"] = s["cooldown"][-500:]
    shadow_step(s, now, get)
    eq = equity(s, prices)
    hold = s["bench"]["sol_qty"] * sp if (s["bench"] and sp) else ""
    append_csv(EQUITY, ["time_utc", "equity_usd", "cash_usd", "btc_price", "btc_hold_usd", "bot_vs_hold_pct"],
               [now.isoformat(timespec="seconds"), round(eq, 2), round(s["cash"], 2), round(sp, 2) if sp else "",
                round(hold, 2) if hold != "" else "", round((eq / hold - 1) * 100, 2) if hold != "" else ""])
    append_csv(FUNNEL, ["time_utc"] + list(f), [now.isoformat(timespec="seconds")] + list(f.values()))
    with open(STATE, "w") as fh:
        json.dump(s, fh, indent=1)
    print(f"scanned {f['scanned']} | passed filters {f['passed']} | bought {f['bought']} | open {len(s['positions'])} | equity ${eq:,.2f}"
          + (f" vs SOL hold ${hold:,.2f}" if hold != "" else ""))
    print("rejected: " + ", ".join(f"{k}={v}" for k, v in f.items() if k not in ("scanned", "passed", "bought") and v))


def guard_check(get=http_json, now=None):
    """Fast look at the open positions only: apply the exit rules, save, trade nothing new. Returns number of open positions."""
    now = now or datetime.now(timezone.utc)
    s = load_state()
    if not s or not s["positions"]:
        return 0
    got = fetch_held(list(s["positions"]), get)
    if got:                                   # feed down = skip this look, never sell on missing data
        prices = {}
        check_exits(s, got, {}, prices, now, count_missing=False)
        with open(STATE, "w") as fh:
            json.dump(s, fh, indent=1)
    return len(s["positions"])


def push_files():
    """Inside GitHub Actions only: commit the data files so Atlas stays fresh and a cut-off run loses nothing."""
    if os.environ.get("SAVE_EACH_SCAN") != "1":
        return
    try:
        names = (STATE, TRADES, EQUITY, FUNNEL, PROBE, SHADOW)
        have = [f for f in names + tuple(n.replace(".csv", "_v1.csv") for n in names) if os.path.exists(f)]
        for cmd in (["git", "add"] + have, ["git", "commit", "-m", "meme paper run"], ["git", "pull", "--rebase", "-X", "theirs"], ["git", "push"]):
            subprocess.run(cmd, check=False, capture_output=True, timeout=60)
    except Exception as e:
        print(f"save skipped ({e})")


def run_loop(minutes, scan_every=15, look_every=30, get=http_json, rug=rug_ok):
    """Stay awake for `minutes`: a full scan every `scan_every` minutes, a quick stop-loss look every `look_every` seconds in between."""
    end = time.time() + minutes * 60
    next_scan = 0.0
    while time.time() < end:
        t0 = time.time()
        if t0 >= next_scan:
            run_once(get=get, rug=rug)
            push_files()
            next_scan = t0 + scan_every * 60
        else:
            guard_check(get)
        time.sleep(max(1.0, look_every - (time.time() - t0)))


if __name__ == "__main__":
    mins = float(os.environ.get("LOOP_MINUTES", "0") or 0)
    if mins > 0:
        run_loop(mins)
    else:
        run_once()
