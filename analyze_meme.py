# analyze_meme.py - read the bot's own logs and say what is working. Reads only; changes nothing.
#   python analyze_meme.py            (run in the folder with meme_trades.csv / meme_shadow.csv)
# 1) Trades: win rate, average win/loss, expectancy after costs, results by exit rule.
# 2) Shadow log: every coin that passed the filters, bought or not, re-checked 1h/4h/24h later.
#    Shows which filter ranges actually go up afterwards, so settings can be tuned on evidence.
import csv
import os
import sys
from collections import defaultdict

INF = float("inf")
COST_PCT = 3.6   # rough round-trip cost to subtract from shadow returns (fee + slippage both ways)


def rows(path):
    if not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def summary(rets, label):
    if not rets:
        return f"{label:<22} n=0"
    wins = [r for r in rets if r > 0]
    loss = [r for r in rets if r <= 0]
    aw = sum(wins) / len(wins) if wins else 0
    al = sum(loss) / len(loss) if loss else 0
    exp = sum(rets) / len(rets)
    return (f"{label:<22} n={len(rets):<4} win {len(wins)/len(rets)*100:5.1f}%  avg win {aw:+6.1f}%  "
            f"avg loss {al:+6.1f}%  expectancy {exp:+6.2f}%/trade")


def trades_report(path):
    t = rows(path)
    if not t:
        print(f"no {path} yet")
        return
    open_buys = {}
    results, by_rule = [], defaultdict(list)
    for r in t:
        coin = r["coin"]
        if r["side"] == "buy":
            open_buys[coin] = f(r["usd"])
        elif r["side"] == "sell" and coin in open_buys:
            spent = open_buys.pop(coin)
            got = f(r["usd"]) or 0
            if spent:
                ret = (got / spent - 1) * 100      # after ALL costs
                results.append(ret)
                by_rule[r["rule"]].append(ret)
    print("=== closed trades (after costs) ===")
    print(summary(results, "all"))
    for rule, rs in sorted(by_rule.items(), key=lambda kv: -len(kv[1])):
        print(summary(rs, f"  exit: {rule}"))
    if results:
        eq, peak, dd = 1.0, 1.0, 0.0
        for r in results:
            eq *= 1 + r / 100 * 0.08           # roughly position-size weighted
            peak, dd = max(peak, eq), max(dd, 1 - eq / peak)
        print(f"open now: {len(open_buys)}   rough account max drawdown: {dd*100:.1f}%")


def bucket(v, edges):
    if v is None:
        return "?"
    for lo, hi in zip(edges, edges[1:]):
        if lo <= v < hi:
            return f"{lo:g}+" if hi == INF else f"{lo:g}-{hi:g}"
    return f">={edges[-1]:g}"


def shadow_report(path):
    sh = rows(path)
    if not sh:
        print(f"\nno {path} yet")
        return
    print(f"\n=== shadow log: coins that passed the filters, return after costs (~{COST_PCT}% subtracted) ===")
    dims = {
        "chg1h_at_entry": [0, 10, 20, 40, 80, INF],
        "age_min": [0, 60, 120, 240, 720, INF],
        "liq_entry": [0, 30_000, 60_000, 150_000, 500_000, INF],
        "buyers": [0, 100, 200, 400, INF],
    }
    for h in ("1", "4", "24"):
        at = [r for r in sh if r["checkpoint_h"] == h and f(r["ret_pct"]) is not None]
        if not at:
            continue
        print(f"\n-- {h}h after passing --")
        print(summary([f(r["ret_pct"]) - COST_PCT for r in at], "all passed"))
        for dim, edges in dims.items():
            groups = defaultdict(list)
            for r in at:
                groups[bucket(f(r[dim]), edges)].append(f(r["ret_pct"]) - COST_PCT)
            for b, rs in sorted(groups.items(), key=lambda kv: float(kv[0].rstrip('+').split('-')[0]) if kv[0] != '?' else -1):
                if len(rs) >= 5:
                    print(summary(rs, f"  {dim[:10]} {b}"))
    print("\nRead it like this: a bucket with positive expectancy and n >= 30 is worth trading;"
          "\nnegative buckets are filters to tighten. Under ~30 samples is still mostly luck.")


if __name__ == "__main__":
    d = sys.argv[1] if len(sys.argv) > 1 else "."
    trades_report(os.path.join(d, "meme_trades.csv"))
    shadow_report(os.path.join(d, "meme_shadow.csv"))
