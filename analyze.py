# -*- coding: utf-8 -*-
"""
A股多因子选股评分 —— 读取 data/latest/*.csv，输出候选名单。
框架：质量(30%) + 估值(25%) + 业绩催化(25%) + 左侧位置(20%)，行业内相对比较。
输出：output/candidates.csv（全部打分）、output/report_data.json（报告用结构化数据）
"""
import json
import os

import numpy as np
import pandas as pd

IN = "data/latest"
OUT = "output"


def norm_code(s):
    return s.astype(str).str.extract(r"(\d{6})")[0]


def winsor_score(x, lo, hi):
    """线性映射到 0~1，lo 以下=0，hi 以上=1"""
    return ((x - lo) / (hi - lo)).clip(0, 1)


def load():
    spot = pd.read_csv(f"{IN}/spot.csv", dtype={"代码": str})
    spot["代码"] = norm_code(spot["代码"])
    tables = {"spot": spot}
    for name in ["yjyg", "yjkb", "yjbb_latest", "yjbb_base", "stock_industry_map", "industry_boards", "klines", "indices"]:
        path = f"{IN}/{name}.csv"
        if os.path.exists(path):
            df = pd.read_csv(path)
            for col in ["股票代码", "代码"]:
                if col in df.columns:
                    df[col] = norm_code(df[col])
            tables[name] = df
    meta = {}
    if os.path.exists(f"{IN}/meta.json"):
        meta = json.load(open(f"{IN}/meta.json", encoding="utf-8"))
    return tables, meta


def build_universe(t):
    df = t["spot"].copy()
    df = df[df["代码"].notna()]
    # 板块：沪深主板 + 创业板 + 科创板（排除北交所）
    df = df[df["代码"].str.match(r"^(60|00|30|68)")]
    # 排除 ST / 退市 / 新股（无60日涨跌幅视为上市未满）
    df = df[~df["名称"].astype(str).str.contains("ST|退", na=False)]
    df = df[~df["名称"].astype(str).str.match(r"^[NCU]")]
    # 流动性门槛：当日成交额 >= 3000万 且 流通市值 >= 20亿
    df["成交额"] = pd.to_numeric(df["成交额"], errors="coerce")
    df["流通市值"] = pd.to_numeric(df["流通市值"], errors="coerce")
    df = df[(df["成交额"] >= 3e7) & (df["流通市值"] >= 2e9)]
    for c in ["最新价", "涨跌幅", "换手率", "市盈率-动态", "市净率", "量比", "60日涨跌幅", "年初至今涨跌幅", "总市值"]:
        df[c] = pd.to_numeric(df[c], errors="coerce") if c in df.columns else np.nan
    return df


def merge_fundamentals(df, t):
    # 60日动量（腾讯K线子集）
    if "klines" in t:
        k = t["klines"].copy()
        k["代码"] = k["代码"].astype(str).str.zfill(6)
        df = df.drop(columns=[c for c in ["60日涨跌幅", "20日涨跌幅", "距60日高点"] if c in df.columns])
        df = df.merge(k.drop_duplicates("代码"), on="代码", how="left")
    # 行业映射
    if "stock_industry_map" in t:
        df = df.merge(t["stock_industry_map"].rename(columns={"代码": "代码"}), on="代码", how="left")
    if "行业" not in df.columns:
        df["行业"] = np.nan
    # 财务底座：上一完整期业绩报表
    if "yjbb_base" in t:
        base = t["yjbb_base"][["股票代码", "净资产收益率", "营业总收入-同比增长", "净利润-同比增长",
                               "销售毛利率", "每股经营现金流量", "每股收益", "所处行业"]].copy()
        base.columns = ["代码", "ROE基期", "营收同比基期", "净利同比基期", "毛利率", "每股现金流", "EPS基期", "东财行业"]
        df = df.merge(base.drop_duplicates("代码"), on="代码", how="left")
        df["行业"] = df["行业"].fillna(df.get("东财行业"))
    # 最新期业绩报表（已披露正式财报的，数据最权威）
    if "yjbb_latest" in t:
        lat = t["yjbb_latest"][["股票代码", "净资产收益率", "营业总收入-同比增长", "净利润-同比增长"]].copy()
        lat.columns = ["代码", "ROE新期", "营收同比新期", "净利同比新期"]
        df = df.merge(lat.drop_duplicates("代码"), on="代码", how="left")
    # 业绩快报
    if "yjkb" in t:
        kb = t["yjkb"][["股票代码", "净利润-同比增长", "营业收入-同比增长", "净资产收益率", "公告日期"]].copy()
        kb.columns = ["代码", "快报净利同比", "快报营收同比", "快报ROE", "快报公告日"]
        df = df.merge(kb.drop_duplicates("代码"), on="代码", how="left")
    # 业绩预告
    if "yjyg" in t:
        yg = t["yjyg"][["股票代码", "预告类型", "业绩变动幅度", "预测指标", "公告日期"]].copy()
        yg.columns = ["代码", "预告类型", "预告变动幅度", "预告指标", "预告公告日"]
        df = df.merge(yg.drop_duplicates("代码"), on="代码", how="left")
    # 统一“最可信”的成长与质量数据：正式财报新期 > 快报 > 底座
    def pick(*cols):
        s = pd.Series(np.nan, index=df.index)
        for c in cols:
            if c in df.columns:
                s = s.combine_first(pd.to_numeric(df[c], errors="coerce"))
        return s

    df["ROE"] = pick("ROE新期", "快报ROE", "ROE基期")
    df["净利同比"] = pick("净利同比新期", "快报净利同比", "净利同比基期")
    df["营收同比"] = pick("营收同比新期", "快报营收同比", "营收同比基期")
    for c in ["毛利率", "每股现金流", "EPS基期", "预告变动幅度", "快报净利同比"]:
        df[c] = pd.to_numeric(df[c], errors="coerce") if c in df.columns else np.nan
    if "预告类型" not in df.columns:
        df["预告类型"] = np.nan
    return df


def score(df):
    # ---------- 质量 30 ----------
    q_roe = winsor_score(df["ROE"], 0, 20)                       # ROE 0~20%+
    q_gm = winsor_score(df["毛利率"], 10, 45)                     # 毛利率
    cash_ok = (df["每股现金流"] / df["EPS基期"].replace(0, np.nan)).clip(-2, 3)
    q_cash = winsor_score(cash_ok, 0.3, 1.2)                      # 现金流兑现度
    df["质量分"] = (q_roe.fillna(0.3) * 0.5 + q_gm.fillna(0.4) * 0.25 + q_cash.fillna(0.4) * 0.25) * 100

    # ---------- 估值 25（行业内分位，越低越好）----------
    pe = df["市盈率-动态"].where(df["市盈率-动态"] > 0)
    pb = df["市净率"].where(df["市净率"] > 0)
    df["_pe_pct"] = pe.groupby(df["行业"]).rank(pct=True)
    df["_pb_pct"] = pb.groupby(df["行业"]).rank(pct=True)
    v = (1 - df["_pe_pct"].fillna(0.85)) * 0.6 + (1 - df["_pb_pct"].fillna(0.5)) * 0.4
    v = v.where(pe.notna() | (df["净利同比"] > 0), v * 0.5)       # 亏损且无改善的估值分打折
    df["估值分"] = v * 100

    # ---------- 业绩催化 25 ----------
    type_map = {"预增": 1.0, "扭亏": 0.95, "略增": 0.55, "续盈": 0.4,
                "减亏": 0.35, "续亏": 0.05, "略减": 0.15, "预减": 0.05, "首亏": 0.0}
    c_type = df["预告类型"].map(type_map) if "预告类型" in df.columns else pd.Series(np.nan, index=df.index)
    mag = winsor_score(df["预告变动幅度"], 0, 150) if "预告变动幅度" in df.columns else 0
    c_yg = (c_type * 0.6 + mag * 0.4)
    c_kb = winsor_score(df["快报净利同比"], 0, 100) if "快报净利同比" in df.columns else pd.Series(np.nan, index=df.index)
    c_grow = winsor_score(df["净利同比"], -20, 80)
    df["催化分"] = (c_yg.combine_first(c_kb).combine_first(c_grow * 0.6)).fillna(0) * 100

    # ---------- 左侧位置 20（未被市场充分定价）----------
    m60 = pd.to_numeric(df["60日涨跌幅"], errors="coerce")
    pos = pd.Series(0.5, index=df.index)
    pos = pos.where(~m60.between(-30, 5), 1.0)     # 真左侧：近60日横盘或回调
    pos = pos.where(~m60.between(5, 20), 0.65)     # 温和启动
    pos = pos.where(~(m60 > 40), 0.1)              # 已拥挤
    pos = pos.where(~(m60 < -45), 0.25)            # 深跌需警惕基本面恶化
    pos = pos.where(m60.notna(), 0.5)              # 无K线数据 → 中性
    dd = pd.to_numeric(df.get("距60日高点"), errors="coerce")
    pos = pos + dd.between(-25, -8).fillna(False) * 0.1   # 距高点有折让加分
    heat = winsor_score(df["换手率"], 15, 30)       # 过热惩罚
    df["左侧分"] = (pos - heat * 0.3).clip(0, 1) * 100

    df["总分"] = (df["质量分"] * 0.30 + df["估值分"] * 0.25 +
                  df["催化分"] * 0.25 + df["左侧分"] * 0.20).round(2)

    # ---------- 红旗 ----------
    flags = []
    for _, r in df.iterrows():
        f = []
        if pd.notna(r.get("市净率")) and r["市净率"] < 1 and pd.notna(r.get("ROE")) and r["ROE"] < 3:
            f.append("疑似价值陷阱")
        if pd.notna(r.get("净利同比")) and r["净利同比"] < -50:
            f.append("净利大幅下滑")
        if pd.notna(r.get("换手率")) and r["换手率"] > 20:
            f.append("换手过热")
        if pd.notna(r.get("涨跌幅")) and r["涨跌幅"] > 7:
            f.append("当日已大涨")
        if pd.notna(r.get("60日涨跌幅")) and r["60日涨跌幅"] > 40:
            f.append("60日涨幅已高")
        flags.append("；".join(f))
    df["红旗"] = flags
    return df


def tier(df):
    """G1: 顶级且有硬催化+左侧+无红旗；G2: 强候选; G3: 观察。"""
    df = df.sort_values("总分", ascending=False).reset_index(drop=True)
    # 硬催化：业绩预告预增/扭亏，或快报净利同比 >= 50%
    hard_cat = pd.Series(False, index=df.index)
    if "预告类型" in df.columns:
        hard_cat |= df["预告类型"].isin(["预增", "扭亏"])
    if "快报净利同比" in df.columns:
        hard_cat |= pd.to_numeric(df["快报净利同比"], errors="coerce") >= 50
    strong_cat = df["催化分"] >= 60
    left = df["左侧分"] >= 60
    clean = df["红旗"] == ""
    quality = df["质量分"] >= 55
    df["评级"] = ""
    g1 = df.index[(df["总分"] >= df["总分"].quantile(0.997)) & hard_cat & (df["催化分"] >= 75)
                  & left & clean & quality][:3]
    df.loc[g1, "评级"] = "G1"
    g2 = df.index[(df["评级"] == "") & (df["总分"] >= df["总分"].quantile(0.995)) & strong_cat & clean][:8]
    df.loc[g2, "评级"] = "G2"
    g3 = df.index[(df["评级"] == "") & (df["总分"] >= df["总分"].quantile(0.99))][:15]
    df.loc[g3, "评级"] = "G3"
    return df


def main():
    os.makedirs(OUT, exist_ok=True)
    t, meta = load()
    df = build_universe(t)
    n_universe = len(df)
    df = merge_fundamentals(df, t)
    df = score(df)
    df = tier(df)

    keep = ["代码", "名称", "行业", "评级", "总分", "质量分", "估值分", "催化分", "左侧分",
            "最新价", "涨跌幅", "60日涨跌幅", "年初至今涨跌幅", "市盈率-动态", "市净率",
            "ROE", "净利同比", "营收同比", "毛利率", "预告类型", "预告变动幅度", "距60日高点", "20日涨跌幅",
            "快报净利同比", "换手率", "流通市值", "红旗"]
    keep = [c for c in keep if c in df.columns]
    df[keep].to_csv(f"{OUT}/candidates.csv", index=False)

    picks = df[df["评级"] != ""][keep]
    summary = {
        "meta": meta,
        "universe": int(n_universe),
        "counts": picks["评级"].value_counts().to_dict(),
        "picks": json.loads(picks.head(40).to_json(orient="records", force_ascii=False)),
    }
    # 行业概览（用自有数据按行业聚合）
    if df["行业"].notna().any():
        g = df.groupby("行业").agg(涨跌幅=("涨跌幅", "mean"), 家数=("代码", "count"))
        g = g[g["家数"] >= 5].round(2).reset_index()
        summary["board_top"] = json.loads(g.nlargest(5, "涨跌幅").to_json(orient="records", force_ascii=False))
        summary["board_bottom"] = json.loads(g.nsmallest(5, "涨跌幅").to_json(orient="records", force_ascii=False))
    with open(f"{OUT}/report_data.json", "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=2)
    print(f"universe={n_universe}  picks={summary['counts']}")
    print(picks.head(15).to_string())


if __name__ == "__main__":
    main()
