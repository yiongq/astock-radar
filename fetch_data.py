# -*- coding: utf-8 -*-
"""
A股全市场数据抓取 —— 在 GitHub Actions 上每个交易日收盘后运行。
数据源（全部免费、海外可访问，已实测）：
  - 全市场行情列表：新浪 Market_Center.getHQNodeData（含 PE/PB/市值/换手率）
  - 60日动量 K 线：腾讯 web.ifzq.gtimg.cn（对候选子集抓取）
  - 业绩预告/快报/报表：东方财富数据中心（经 akshare，海外可用）
  - 指数日线：腾讯
输出：data/latest/*.csv + data/history/YYYYMMDD/spot.csv.gz
"""
import datetime as dt
import json
import os
import sys
import time

import pandas as pd
import requests

OUT = "data/latest"
HIST = "data/history"
TZ = dt.timezone(dt.timedelta(hours=8))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36"
S_HEAD = {"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"}
T_HEAD = {"User-Agent": UA, "Referer": "https://gu.qq.com/"}


def today():
    return dt.datetime.now(TZ).date()


def retry(fn, tries=4, wait=4, name=""):
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa
            last = e
            print(f"[retry] {name} attempt {i+1} failed: {e}")
            time.sleep(wait * (i + 1))
    raise RuntimeError(f"{name} failed after {tries} tries: {last}")


def is_trade_day(d):
    try:
        import akshare as ak
        cal = retry(ak.tool_trade_date_hist_sina, name="trade_cal")
        dates = set(pd.to_datetime(cal["trade_date"]).dt.date)
        return d in dates
    except Exception as e:
        print(f"[warn] trade calendar unavailable ({e}), fallback to weekday check")
        return d.weekday() < 5


# ---------- 新浪全市场行情 ----------
def fetch_spot_sina():
    rows = []
    fails = 0
    for page in range(1, 130):
        url = ("https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
               f"Market_Center.getHQNodeData?page={page}&num=100&sort=symbol&asc=1&node=hs_a&symbol=&_s_r_a=init")
        try:
            r = requests.get(url, headers=S_HEAD, timeout=20)
            data = r.json()
            if not data:
                break
            rows.extend(data)
            fails = 0
        except Exception as e:
            fails += 1
            print(f"[warn] sina spot page {page}: {e}")
            if fails >= 5:
                break
            time.sleep(3)
            continue
        time.sleep(0.25)
    df = pd.DataFrame(rows)
    if len(df) < 3000:
        raise RuntimeError(f"sina spot too few rows: {len(df)}")
    out = pd.DataFrame({
        "代码": df["code"].astype(str).str.zfill(6),
        "symbol": df["symbol"],
        "名称": df["name"],
        "最新价": pd.to_numeric(df["trade"], errors="coerce"),
        "涨跌幅": pd.to_numeric(df["changepercent"], errors="coerce"),
        "昨收": pd.to_numeric(df["settlement"], errors="coerce"),
        "今开": pd.to_numeric(df["open"], errors="coerce"),
        "最高": pd.to_numeric(df["high"], errors="coerce"),
        "最低": pd.to_numeric(df["low"], errors="coerce"),
        "成交量": pd.to_numeric(df["volume"], errors="coerce"),
        "成交额": pd.to_numeric(df["amount"], errors="coerce"),
        "换手率": pd.to_numeric(df["turnoverratio"], errors="coerce"),
        "市盈率-动态": pd.to_numeric(df["per"], errors="coerce"),
        "市净率": pd.to_numeric(df["pb"], errors="coerce"),
        "总市值": pd.to_numeric(df["mktcap"], errors="coerce") * 1e4,
        "流通市值": pd.to_numeric(df["nmc"], errors="coerce") * 1e4,
    })
    return out


# ---------- 腾讯 60日K线（子集） ----------
def tencent_kline(symbol, bars=70):
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={symbol},day,,,{bars},qfq"
    r = requests.get(url, headers=T_HEAD, timeout=15)
    j = r.json()
    d = j["data"][symbol]
    arr = d.get("qfqday") or d.get("day")
    closes = [float(x[2]) for x in arr]
    return closes


def fetch_klines(spot, catalyst_codes, cap=1200):
    base = spot[~spot["名称"].astype(str).str.contains("ST|退", na=False)].copy()
    base = base[(base["成交额"] >= 3e7) & (base["流通市值"] >= 2e9)]
    picks = set(catalyst_codes) & set(base["代码"])
    by_amt = base.sort_values("成交额", ascending=False)["代码"].tolist()
    for c in by_amt:
        if len(picks) >= cap:
            break
        picks.add(c)
    sym = spot.set_index("代码")["symbol"]
    rows = []
    done = 0
    for code in picks:
        s = sym.get(code)
        if not s:
            continue
        try:
            closes = tencent_kline(s)
            if len(closes) >= 21:
                last = closes[-1]
                r60 = (last / closes[-61] - 1) * 100 if len(closes) >= 61 else None
                r20 = (last / closes[-21] - 1) * 100
                dd60 = (last / max(closes) - 1) * 100
                rows.append((code, r60, r20, dd60))
        except Exception as e:
            print(f"[warn] kline {s}: {e}")
        done += 1
        if done % 200 == 0:
            print(f"klines {done}/{len(picks)}")
        time.sleep(0.12)
    return pd.DataFrame(rows, columns=["代码", "60日涨跌幅", "20日涨跌幅", "距60日高点"])


# ---------- 业绩表（东财数据中心，海外可用） ----------
def quarter_ends(d, n=3):
    ends = []
    y, q = d.year, (d.month - 1) // 3
    for _ in range(n + 1):
        if q == 0:
            y, q = y - 1, 4
        m = q * 3
        day = 31 if m in (3, 12) else 30
        e = dt.date(y, m, day)
        if e < d:
            ends.append(e.strftime("%Y%m%d"))
        q -= 1
    return ends[: n + 1]


def fetch_period_table(fn, periods, name):
    for p in periods:
        try:
            df = retry(lambda: fn(date=p), name=f"{name}({p})", tries=3)
            if df is not None and len(df) > 0:
                print(f"[ok] {name} period={p} rows={len(df)}")
                return df, p
        except Exception as e:
            print(f"[warn] {name} period={p}: {e}")
    return pd.DataFrame(), None


# ---------- 指数（腾讯） ----------
def fetch_indices():
    frames = []
    for sym, label in [("sh000001", "上证指数"), ("sz399001", "深证成指"), ("sz399006", "创业板指")]:
        try:
            url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,,,260,"
            j = requests.get(url, headers=T_HEAD, timeout=15).json()
            arr = j["data"][sym].get("day") or j["data"][sym].get("qfqday")
            df = pd.DataFrame(arr, columns=["date", "open", "close", "high", "low", "volume"] + (["x"] * (len(arr[0]) - 6) if arr and len(arr[0]) > 6 else []))
            df = df[["date", "open", "close", "high", "low", "volume"]]
            df["指数"] = label
            frames.append(df)
            time.sleep(0.3)
        except Exception as e:
            print(f"[warn] index {sym}: {e}")
    return pd.concat(frames) if frames else pd.DataFrame()


def main():
    d = today()
    force = os.environ.get("FORCE_RUN") == "1"
    if not is_trade_day(d) and not force:
        print(f"{d} 不是交易日，跳过。")
        return 0

    os.makedirs(OUT, exist_ok=True)
    meta = {"trade_date": str(d), "fetched_at": dt.datetime.now(TZ).isoformat(), "tables": {}}

    # 1) 全市场行情（新浪）
    spot = retry(fetch_spot_sina, name="spot_sina", tries=3)
    meta["tables"]["spot"] = len(spot)
    print(f"spot rows: {len(spot)}")

    # 2) 业绩预告/快报/报表（东财数据中心 via akshare）
    import akshare as ak
    periods = quarter_ends(d)
    print("periods:", periods)
    catalyst_codes = set()

    yjyg, p1 = fetch_period_table(ak.stock_yjyg_em, periods, "yjyg")
    if len(yjyg):
        yjyg.to_csv(f"{OUT}/yjyg.csv", index=False)
        meta["tables"]["yjyg"] = {"rows": len(yjyg), "period": p1}
        catalyst_codes |= set(yjyg["股票代码"].astype(str).str.zfill(6))

    yjkb, p2 = fetch_period_table(ak.stock_yjkb_em, periods, "yjkb")
    if len(yjkb):
        yjkb.to_csv(f"{OUT}/yjkb.csv", index=False)
        meta["tables"]["yjkb"] = {"rows": len(yjkb), "period": p2}
        catalyst_codes |= set(yjkb["股票代码"].astype(str).str.zfill(6))

    yjbb_latest, p3 = fetch_period_table(ak.stock_yjbb_em, periods, "yjbb_latest")
    if len(yjbb_latest):
        yjbb_latest.to_csv(f"{OUT}/yjbb_latest.csv", index=False)
        meta["tables"]["yjbb_latest"] = {"rows": len(yjbb_latest), "period": p3}

    base_periods = [p for p in periods if p != p3]
    yjbb_base, p4 = fetch_period_table(ak.stock_yjbb_em, base_periods, "yjbb_base")
    if len(yjbb_base):
        yjbb_base.to_csv(f"{OUT}/yjbb_base.csv", index=False)
        meta["tables"]["yjbb_base"] = {"rows": len(yjbb_base), "period": p4}

    # 3) 60日动量（腾讯K线：催化股 + 高流动性股，上限1200只）
    klines = fetch_klines(spot, catalyst_codes)
    klines.to_csv(f"{OUT}/klines.csv", index=False)
    meta["tables"]["klines"] = len(klines)

    # 4) 指数
    idx = fetch_indices()
    if len(idx):
        idx.to_csv(f"{OUT}/indices.csv", index=False)
        meta["tables"]["indices"] = len(idx)

    # 5) 落盘 spot + 历史留档
    spot.drop(columns=["symbol"]).to_csv(f"{OUT}/spot.csv", index=False)
    hd = f"{HIST}/{d.strftime('%Y%m%d')}"
    os.makedirs(hd, exist_ok=True)
    spot.drop(columns=["symbol"]).to_csv(f"{hd}/spot.csv.gz", index=False, compression="gzip")

    with open(f"{OUT}/meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print("DONE", json.dumps(meta, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
