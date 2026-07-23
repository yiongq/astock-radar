# -*- coding: utf-8 -*-
"""
A股全市场数据抓取脚本 —— 在 GitHub Actions 上每个交易日收盘后运行。
数据源：akshare（东方财富/新浪等免费公开接口），无需任何 token。
输出：data/latest/*.csv + data/history/YYYYMMDD/spot.csv.gz
"""
import json
import os
import sys
import time
import datetime as dt

import pandas as pd
import akshare as ak

OUT = "data/latest"
HIST = "data/history"
TZ = dt.timezone(dt.timedelta(hours=8))  # 北京时间


def today():
    return dt.datetime.now(TZ).date()


def retry(fn, tries=4, wait=5, name=""):
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
        cal = retry(ak.tool_trade_date_hist_sina, name="trade_cal")
        dates = set(pd.to_datetime(cal["trade_date"]).dt.date)
        return d in dates
    except Exception as e:
        print(f"[warn] trade calendar unavailable ({e}), fallback to weekday check")
        return d.weekday() < 5


def quarter_ends(d, n=3):
    """返回截至今天最近 n 个季度末（含可能尚未披露完的最新一季），新→旧。"""
    ends = []
    y, q = d.year, (d.month - 1) // 3  # 当前所处季度的上一季末开始
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
    """业绩预告/快报/报表：从最新期往回找，返回 (df, period)；同时抓上一完整期作为底座。"""
    for p in periods:
        try:
            df = retry(lambda: fn(date=p), name=f"{name}({p})")
            if df is not None and len(df) > 0:
                print(f"[ok] {name} period={p} rows={len(df)}")
                return df, p
        except Exception as e:
            print(f"[warn] {name} period={p}: {e}")
    return pd.DataFrame(), None


def main():
    d = today()
    force = os.environ.get("FORCE_RUN") == "1"
    if not is_trade_day(d) and not force:
        print(f"{d} 不是交易日，跳过。")
        return 0

    os.makedirs(OUT, exist_ok=True)
    meta = {"trade_date": str(d), "fetched_at": dt.datetime.now(TZ).isoformat(), "tables": {}}

    # 1) 全市场实时快照（收盘后即当日收盘数据）
    spot = retry(ak.stock_zh_a_spot_em, name="spot")
    spot.to_csv(f"{OUT}/spot.csv", index=False)
    meta["tables"]["spot"] = len(spot)

    # 历史留档（压缩）
    hd = f"{HIST}/{d.strftime('%Y%m%d')}"
    os.makedirs(hd, exist_ok=True)
    spot.to_csv(f"{hd}/spot.csv.gz", index=False, compression="gzip")

    # 2) 行业板块行情
    try:
        boards = retry(ak.stock_board_industry_name_em, name="boards")
        boards.to_csv(f"{OUT}/industry_boards.csv", index=False)
        meta["tables"]["industry_boards"] = len(boards)
    except Exception as e:
        print(f"[warn] boards failed: {e}")
        boards = None

    # 3) 个股→行业映射（遍历板块成分，约90个请求）
    try:
        if boards is not None:
            rows = []
            for bname in boards["板块名称"].tolist():
                try:
                    cons = ak.stock_board_industry_cons_em(symbol=bname)
                    for c in cons["代码"].astype(str).tolist():
                        rows.append((c.zfill(6), bname))
                    time.sleep(0.25)
                except Exception as e:
                    print(f"[warn] cons {bname}: {e}")
            imap = pd.DataFrame(rows, columns=["代码", "行业"]).drop_duplicates("代码")
            imap.to_csv(f"{OUT}/stock_industry_map.csv", index=False)
            meta["tables"]["stock_industry_map"] = len(imap)
    except Exception as e:
        print(f"[warn] industry map failed: {e}")

    # 4) 业绩预告 / 业绩快报（最新期） + 业绩报表（最新可得期 & 上一完整期）
    periods = quarter_ends(d)
    print("candidate periods:", periods)

    yjyg, p1 = fetch_period_table(ak.stock_yjyg_em, periods, "yjyg")
    if len(yjyg):
        yjyg.to_csv(f"{OUT}/yjyg.csv", index=False)
        meta["tables"]["yjyg"] = {"rows": len(yjyg), "period": p1}

    yjkb, p2 = fetch_period_table(ak.stock_yjkb_em, periods, "yjkb")
    if len(yjkb):
        yjkb.to_csv(f"{OUT}/yjkb.csv", index=False)
        meta["tables"]["yjkb"] = {"rows": len(yjkb), "period": p2}

    yjbb_latest, p3 = fetch_period_table(ak.stock_yjbb_em, periods, "yjbb_latest")
    if len(yjbb_latest):
        yjbb_latest.to_csv(f"{OUT}/yjbb_latest.csv", index=False)
        meta["tables"]["yjbb_latest"] = {"rows": len(yjbb_latest), "period": p3}

    # 上一完整期（覆盖面广，作为财务底座）：取比 yjbb_latest 更早的下一期
    base_periods = [p for p in periods if p != p3]
    yjbb_base, p4 = fetch_period_table(ak.stock_yjbb_em, base_periods, "yjbb_base")
    if len(yjbb_base):
        yjbb_base.to_csv(f"{OUT}/yjbb_base.csv", index=False)
        meta["tables"]["yjbb_base"] = {"rows": len(yjbb_base), "period": p4}

    # 5) 指数日线（近一年，市场位置参考）
    try:
        idx_frames = []
        for sym, label in [("sh000001", "上证指数"), ("sz399001", "深证成指"), ("sz399006", "创业板指")]:
            df = retry(lambda s=sym: ak.stock_zh_index_daily_em(symbol=s), name=f"index {sym}")
            df = df.tail(250).copy()
            df["指数"] = label
            idx_frames.append(df)
            time.sleep(0.5)
        pd.concat(idx_frames).to_csv(f"{OUT}/indices.csv", index=False)
        meta["tables"]["indices"] = sum(len(f) for f in idx_frames)
    except Exception as e:
        print(f"[warn] indices failed: {e}")

    with open(f"{OUT}/meta.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print("DONE", json.dumps(meta, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
