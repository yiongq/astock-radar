# -*- coding: utf-8 -*-
"""
每日交易计划生成 —— 读取 output/report_data.json（G1/G2 候选）与
data/history/*/spot.csv.gz（每日行情快照），按固定规则输出 output/trade_plan.json。

规则（固定，不随会话变化）：
  - ATR：用历史快照逐日计算真实波幅 TR = max(最高-最低, |最高-昨收|, |最低-昨收|)，
    目标窗口 14 个交易日；历史不足 14 天时用现有天数并在结果中标注 atr_days。
  - 买入区间：现价 - 0.5*ATR ~ 现价 + 0.2*ATR
  - 止损价：买入区间下沿 - 2*ATR
  - 第一目标：现价 + 3*ATR
  - 仓位：固定风险预算法，单笔风险预算 = 总资金的 RISK_BUDGET（默认 1%），
    仓位比例 = 风险预算 / 止损幅度，上限 15%。
  - 时间止损：10 个交易日（配合 track.py 的 T+10 跟踪）。
"""
import glob
import json
import os

import pandas as pd

IN = "data/latest"
HIST = "data/history"
OUT = "output"
ATR_WINDOW = 14          # 目标 ATR 窗口
RISK_BUDGET = float(os.environ.get("RISK_BUDGET", "0.01"))   # 单笔风险占总资金比例
POS_CAP = 0.15           # 单票仓位上限
TIME_STOP_DAYS = 10      # 时间止损（交易日）


def norm_code(s):
    return str(s).split(".")[0].zfill(6)


def load_history():
    """返回按日期升序的 [(yyyymmdd, DataFrame)]，DataFrame 以代码为索引。"""
    days = []
    for d in sorted(os.listdir(HIST)) if os.path.isdir(HIST) else []:
        path = f"{HIST}/{d}/spot.csv.gz"
        if os.path.exists(path):
            df = pd.read_csv(path, dtype={"代码": str})
            df["代码"] = df["代码"].map(norm_code)
            df = df.drop_duplicates("代码").set_index("代码")
            days.append((d, df))
    return days


def atr_for(code, hist_days):
    """对单只股票用最近 ATR_WINDOW 天快照计算 ATR，返回 (atr, 实际天数)。"""
    trs = []
    for _, df in hist_days[-ATR_WINDOW:]:
        if code not in df.index:
            continue
        r = df.loc[code]
        try:
            hi, lo, pc = float(r["最高"]), float(r["最低"]), float(r["昨收"])
        except (ValueError, KeyError, TypeError):
            continue
        if hi <= 0 or lo <= 0 or pc <= 0:
            continue
        trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
    if not trs:
        return None, 0
    return sum(trs) / len(trs), len(trs)


def main():
    os.makedirs(OUT, exist_ok=True)
    rd_path = f"{OUT}/report_data.json"
    if not os.path.exists(rd_path):
        raise SystemExit("缺少 output/report_data.json，请先运行 analyze.py")
    rd = json.load(open(rd_path, encoding="utf-8"))
    trade_date = rd.get("meta", {}).get("trade_date", "")
    hist_days = load_history()

    plans = []
    for p in rd.get("picks", []):
        if p.get("评级") not in ("G1", "G2"):
            continue
        code = norm_code(p["代码"])
        price = float(p["最新价"])
        atr, n = atr_for(code, hist_days)
        if not atr or price <= 0:
            plans.append({"代码": code, "名称": p.get("名称"), "评级": p.get("评级"),
                          "现价": price, "说明": "历史快照不足，无法计算 ATR，本日不给出计划"})
            continue
        buy_lo = round(price - 0.5 * atr, 2)
        buy_hi = round(price + 0.2 * atr, 2)
        stop = round(buy_lo - 2 * atr, 2)
        target1 = round(price + 3 * atr, 2)
        stop_pct = (price - stop) / price
        pos = round(min(RISK_BUDGET / stop_pct, POS_CAP), 4) if stop_pct > 0 else 0.0
        plans.append({
            "代码": code, "名称": p.get("名称"), "评级": p.get("评级"),
            "现价": price, "ATR": round(atr, 3), "atr_days": n,
            "买入区间": [buy_lo, buy_hi], "止损价": stop, "止损幅度": round(stop_pct, 4),
            "第一目标": target1, "建议仓位": pos, "时间止损_交易日": TIME_STOP_DAYS,
        })

    out = {
        "trade_date": trade_date,
        "规则": {"ATR目标窗口": ATR_WINDOW, "风险预算": RISK_BUDGET, "仓位上限": POS_CAP,
                 "买入区间": "现价-0.5ATR ~ 现价+0.2ATR", "止损": "区间下沿-2ATR",
                 "第一目标": "现价+3ATR", "时间止损": f"{TIME_STOP_DAYS}个交易日"},
        "atr_days_available": len(hist_days),
        "plans": plans,
    }
    with open(f"{OUT}/trade_plan.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"trade_plan: {len(plans)} 条（trade_date={trade_date}, 历史快照 {len(hist_days)} 天）")


if __name__ == "__main__":
    main()
