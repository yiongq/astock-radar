# -*- coding: utf-8 -*-
"""
历史复盘跟踪 —— 每日运行两件事：
  1) 把当日 G1/G2 候选登记到 data/track/records.csv（同一登记日+代码不重复登记）；
  2) 对已登记记录回填 T+5 / T+10 / T+20 收益（用 data/history 快照的收盘价）
     以及相对基准的超额收益（基准优先沪深300，缺失则用上证指数并标注），
     汇总生成 output/review.json（不满 30 条完结样本自动标"样本不足"）。
"""
import json
import os

import pandas as pd

IN = "data/latest"
HIST = "data/history"
OUT = "output"
TRACK_DIR = "data/track"
RECORDS = f"{TRACK_DIR}/records.csv"
HORIZONS = [5, 10, 20]
MIN_SAMPLE = 30

COLS = ["登记日", "代码", "名称", "评级", "总分", "入选价", "基准", "基准收盘",
        "T5价", "T5收益", "T5超额", "T10价", "T10收益", "T10超额",
        "T20价", "T20收益", "T20超额", "状态"]


def norm_code(s):
    return str(s).split(".")[0].zfill(6)


def hist_dates():
    if not os.path.isdir(HIST):
        return []
    return sorted(d for d in os.listdir(HIST)
                  if os.path.exists(f"{HIST}/{d}/spot.csv.gz"))


_snap_cache = {}


def snapshot(d):
    if d not in _snap_cache:
        df = pd.read_csv(f"{HIST}/{d}/spot.csv.gz", dtype={"代码": str})
        df["代码"] = df["代码"].map(norm_code)
        _snap_cache[d] = df.drop_duplicates("代码").set_index("代码")
    return _snap_cache[d]


def load_benchmark():
    """返回 (基准名, {yyyymmdd: close})。优先沪深300，否则上证指数。"""
    path = f"{IN}/indices.csv"
    if not os.path.exists(path):
        return None, {}
    idx = pd.read_csv(path)
    for name in ["沪深300", "上证指数"]:
        sub = idx[idx["指数"] == name]
        if len(sub):
            closes = {d.replace("-", ""): float(c)
                      for d, c in zip(sub["date"], sub["close"])}
            return name, closes
    return None, {}


def main():
    os.makedirs(TRACK_DIR, exist_ok=True)
    os.makedirs(OUT, exist_ok=True)

    if os.path.exists(RECORDS):
        rec = pd.read_csv(RECORDS, dtype={"代码": str, "登记日": str})
        rec["代码"] = rec["代码"].map(norm_code)
    else:
        rec = pd.DataFrame(columns=COLS)

    bench_name, bench = load_benchmark()

    # ---------- 1) 登记当日 G1/G2 ----------
    rd_path = f"{OUT}/report_data.json"
    n_new = 0
    if os.path.exists(rd_path):
        rd = json.load(open(rd_path, encoding="utf-8"))
        trade_date = str(rd.get("meta", {}).get("trade_date", "")).replace("-", "")
        existing = set(zip(rec["登记日"].astype(str), rec["代码"]))
        for p in rd.get("picks", []):
            if p.get("评级") not in ("G1", "G2") or not trade_date:
                continue
            code = norm_code(p["代码"])
            if (trade_date, code) in existing:
                # 同一交易日重复运行：刷新入选价/基准收盘（修复盘前运行留下的旧价）
                m = (rec["登记日"].astype(str) == trade_date) & (rec["代码"] == code)
                rec.loc[m, "入选价"] = float(p["最新价"])
                rec.loc[m, "评级"] = p.get("评级")
                rec.loc[m, "总分"] = p.get("总分")
                if bench.get(trade_date):
                    rec.loc[m, "基准收盘"] = bench[trade_date]
                continue
            rec = pd.concat([rec, pd.DataFrame([{
                "登记日": trade_date, "代码": code, "名称": p.get("名称"),
                "评级": p.get("评级"), "总分": p.get("总分"),
                "入选价": float(p["最新价"]),
                "基准": bench_name or "", "基准收盘": bench.get(trade_date, ""),
                "状态": "跟踪中",
            }])], ignore_index=True)
            n_new += 1

    # ---------- 1.5) 基准收盘缺失回填（登记时指数数据未到位的情况）----------
    for i, r in rec.iterrows():
        if str(r.get("基准收盘", "")) in ("", "nan") and str(r["登记日"]) in bench:
            rec.at[i, "基准收盘"] = bench[str(r["登记日"])]
            rec.at[i, "基准"] = bench_name

    # ---------- 2) 回填 T+N ----------
    dates = hist_dates()
    for i, r in rec.iterrows():
        if r.get("状态") == "已完结":
            continue
        d0 = str(r["登记日"])
        after = [d for d in dates if d > d0]
        entry = float(r["入选价"])
        b0 = float(r["基准收盘"]) if str(r.get("基准收盘", "")) not in ("", "nan") else None
        done = True
        for n in HORIZONS:
            col = f"T{n}"
            if str(rec.at[i, f"{col}收益"]) not in ("", "nan", "None"):
                continue  # 已回填
            if len(after) < n:
                done = False
                continue
            dn = after[n - 1]
            snap = snapshot(dn)
            if r["代码"] not in snap.index:
                done = False
                continue
            px = float(snap.loc[r["代码"], "最新价"])
            ret = (px / entry - 1) * 100
            rec.at[i, f"{col}价"] = round(px, 2)
            rec.at[i, f"{col}收益"] = round(ret, 2)
            if b0 and dn in bench:
                b_ret = (bench[dn] / b0 - 1) * 100
                rec.at[i, f"{col}超额"] = round(ret - b_ret, 2)
        if done:
            rec.at[i, "状态"] = "已完结"

    rec = rec.reindex(columns=COLS)
    rec.to_csv(RECORDS, index=False)

    # ---------- 3) 汇总 review.json ----------
    def stats(sub, n):
        s = pd.to_numeric(sub[f"T{n}收益"], errors="coerce").dropna()
        e = pd.to_numeric(sub[f"T{n}超额"], errors="coerce").dropna()
        if not len(s):
            return None
        return {"样本": int(len(s)), "胜率": round(float((s > 0).mean() * 100), 1),
                "平均收益": round(float(s.mean()), 2),
                "平均超额": round(float(e.mean()), 2) if len(e) else None}

    review = {"生成基于登记日样本数": int(len(rec)), "新登记": n_new,
              "基准": bench_name, "样本不足": len(rec[rec["状态"] == "已完结"]) < MIN_SAMPLE,
              "完结样本数": int((rec["状态"] == "已完结").sum()),
              "整体": {}, "按评级": {}}
    if bench_name != "沪深300":
        review["基准说明"] = "沪深300 数据未到位，暂用上证指数做基准"
    for n in HORIZONS:
        review["整体"][f"T{n}"] = stats(rec, n)
    for g in ("G1", "G2"):
        sub = rec[rec["评级"] == g]
        review["按评级"][g] = {f"T{n}": stats(sub, n) for n in HORIZONS}
    review["跟踪中"] = json.loads(
        rec[rec["状态"] == "跟踪中"].to_json(orient="records", force_ascii=False))

    with open(f"{OUT}/review.json", "w", encoding="utf-8") as f:
        json.dump(review, f, ensure_ascii=False, indent=2)
    print(f"track: 新登记 {n_new} 条，总记录 {len(rec)} 条，完结 {review['完结样本数']} 条，基准={bench_name}")


if __name__ == "__main__":
    main()
