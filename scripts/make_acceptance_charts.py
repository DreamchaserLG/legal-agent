"""验收测试图表：读取 outputs/acceptance/acceptance.json 生成 4 张图。"""
from __future__ import annotations

import json
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402

BASE = "outputs/acceptance"
C_TEXT = "#1F2937"
C_MUTED = "#6B7280"
C_BLUE = "#2563EB"
C_RED = "#DC2626"
C_GREEN = "#059669"
C_AMBER = "#D97706"
C_GRID = "#E5E7EB"

plt.rcParams.update(
    {
        "font.sans-serif": ["Microsoft YaHei", "SimHei"],
        "axes.unicode_minus": False,
        "figure.facecolor": "white",
        "axes.facecolor": "white",
        "axes.edgecolor": "#D1D5DB",
        "axes.labelcolor": C_TEXT,
        "text.color": C_TEXT,
        "xtick.color": C_MUTED,
        "ytick.color": C_MUTED,
        "axes.titleweight": "bold",
        "figure.dpi": 160,
    }
)


def load(name: str) -> dict:
    with open(os.path.join(BASE, name), encoding="utf-8") as fh:
        return json.load(fh)


def clean(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)


def fig1(d: dict, baseline_ms: float) -> None:
    b = d["B_plan"]
    db = d["C_perf"]["db_only"]["p50_ms"]
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 3.4), gridspec_kw={"width_ratios": [1, 1.35]})

    vals = [baseline_ms, db]
    bars = a1.bar(["修复前\n(join 内联)", "修复后\n(ANN 先行)"], vals, color=[C_RED, C_GREEN], width=0.45)
    a1.set_yscale("log")
    a1.set_ylabel("单查询耗时（毫秒，对数）")
    a1.set_title("同一查询：306,381 ms → %.1f ms" % db)
    for bar, v in zip(bars, vals):
        a1.text(bar.get_x() + bar.get_width() / 2, v * 1.4,
                f"{v:,.0f} ms" if v > 1000 else f"{v:.1f} ms",
                ha="center", fontsize=10, fontweight="bold")
    a1.set_ylim(1, baseline_ms * 40)
    a1.grid(axis="y", color=C_GRID, linewidth=0.8, which="both")
    a1.set_axisbelow(True)
    clean(a1)

    limits = sorted({r["limit"] for r in b["rows"]})
    for i, lim in enumerate(limits):
        costs = [r["total_cost"] for r in b["rows"] if r["limit"] == lim]
        xs = np.random.RandomState(7 + i).normal(i, 0.055, len(costs))
        a2.scatter(xs, costs, s=18, color=C_BLUE, alpha=0.75, zorder=2)
        a2.hlines(np.median(costs), i - 0.28, i + 0.28, color=C_RED, linewidth=1.6, zorder=3)
    a2.set_xticks(range(len(limits)))
    a2.set_xticklabels([f"limit={l}" for l in limits])
    a2.set_ylabel("计划总成本（cost）")
    a2.set_title(f"48 种过滤/limit 组合全部命中 HNSW（{b['hnsw_hit']}/{b['total_combinations']}）")
    a2.grid(axis="y", color=C_GRID, linewidth=0.8)
    a2.set_axisbelow(True)
    clean(a2)
    fig.tight_layout()
    fig.savefig(os.path.join(BASE, "fig1_fix_effect.png"))
    plt.close(fig)


def fig2(d: dict) -> None:
    sw = d["C_perf"]["limit_sweep"]
    db = d["C_perf"]["db_only"]
    cold = d["C_perf"]["cold_first"]["ms"]
    limits = sorted(sw, key=int)
    p50 = [sw[k]["p50_ms"] for k in limits]
    p95 = [sw[k]["p95_ms"] for k in limits]

    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 3.4), gridspec_kw={"width_ratios": [1.3, 1]})
    a1.plot(limits, p50, "o-", color=C_BLUE, label="端到端 P50（含 embedding + 建表检查）")
    a1.plot(limits, p95, "s--", color=C_AMBER, label="端到端 P95")
    a1.axhline(db["p50_ms"], color=C_GREEN, linestyle=":", linewidth=1.4,
               label=f"纯 SQL P50 {db['p50_ms']:.1f} ms")
    a1.set_yscale("log")
    a1.set_xlabel("请求条数 limit")
    a1.set_ylabel("毫秒（对数）")
    a1.set_title("延迟随 limit 线性增长；SQL 层始终在 10 ms 量级")
    a1.legend(frameon=False, fontsize=8, loc="lower right")
    a1.grid(color=C_GRID, linewidth=0.8, which="both")
    clean(a1)

    names = ["冷首查\n(索引页首次加载)", "端到端 P50\n(limit=24)", "纯 SQL P50"]
    vals = [cold, sw["24"]["p50_ms"], db["p50_ms"]]
    colors = [C_RED, C_BLUE, C_GREEN]
    bars = a2.barh(names, vals, color=colors, height=0.5)
    a2.set_xscale("log")
    a2.set_xlabel("毫秒（对数）")
    a2.set_title("682 ms 冷启动是索引页加载，不是查询本身")
    for bar, v in zip(bars, vals):
        a2.text(v * 1.15, bar.get_y() + bar.get_height() / 2, f"{v:.0f} ms",
                va="center", fontsize=9, fontweight="bold")
    a2.set_xlim(1, cold * 6)
    a2.grid(axis="x", color=C_GRID, linewidth=0.8, which="both")
    clean(a2)
    fig.tight_layout()
    fig.savefig(os.path.join(BASE, "fig2_latency.png"))
    plt.close(fig)


def fig3(d: dict) -> None:
    app = d["C_perf"]["concurrency"]
    sql = d["C_perf"]["concurrency_sql"]
    ws = ["1", "4", "8"]
    app_err = [app[w]["errors"] / app[w]["requests"] * 100 for w in ws]
    before_err = [0.0, 11 / 24 * 100, 17 / 24 * 100]
    app_qps = [app[w]["qps"] for w in ws]
    sql_qps = [sql[w]["qps"] for w in ws]

    fig, (a1, a2) = plt.subplots(1, 2, figsize=(10.5, 3.3))
    x = np.arange(len(ws))
    a1.bar(x - 0.18, before_err, 0.34, color=C_RED, label="修复前")
    a1.bar(x + 0.18, app_err, 0.34, color=C_GREEN, label="修复后")
    a1.set_xticks(x)
    a1.set_xticklabels([f"{w} 线程" for w in ws])
    a1.set_ylabel("失败率（%）")
    a1.set_title("schema 初始化加锁后：并发错误归零")
    a1.legend(frameon=False, fontsize=8)
    a1.grid(axis="y", color=C_GRID, linewidth=0.8)
    a1.set_axisbelow(True)
    for xi, v in zip(x - 0.18, before_err):
        if v > 0:
            a1.text(xi, v + 1.5, f"{v:.0f}%", ha="center", fontsize=9, color=C_RED, fontweight="bold")
    clean(a1)

    a2.plot(ws, sql_qps, "o-", color=C_GREEN, label="仅 SQL")
    a2.plot(ws, app_qps, "s--", color=C_RED, label="应用层")
    a2.set_ylabel("吞吐（QPS）")
    a2.set_title("修复后应用层与纯 SQL 并发吞吐")
    a2.legend(frameon=False, fontsize=8)
    a2.grid(color=C_GRID, linewidth=0.8)
    clean(a2)
    fig.tight_layout()
    fig.savefig(os.path.join(BASE, "fig3_concurrency.png"))
    plt.close(fig)


def fig4(_d: dict) -> None:
    rv = load("recall_vs_annlimit.json")
    fixed = load("recall_after_fix.json")
    ef = load("ef_sweep.json")
    qs = rv["queries"]

    fig, (a1, a2) = plt.subplots(1, 2, figsize=(12, 3.7), gridspec_kw={"width_ratios": [1.5, 1]})
    x = np.arange(len(qs))
    w = 0.34
    before = [q["ann"]["96"]["recall_at_10"] for q in qs]
    fixed_by_id = {q["id"]: q for q in fixed["queries"]}
    after = [fixed_by_id[q["id"]]["ann"]["96"]["recall_at_10"] for q in qs]
    a1.bar(x - w / 2, before, w, color=C_RED, label="修复前 ef≈96")
    a1.bar(x + w / 2, after, w, color=C_GREEN, label="修复后 ef=500")
    a1.set_xticks(x)
    a1.set_xticklabels([q["id"] for q in qs], fontsize=8)
    a1.set_ylim(0, 1.08)
    a1.set_ylabel("Recall@10")
    a1.set_title("ann_limit=96：Recall@10 均值 0.475 → 0.925")
    a1.legend(frameon=False, fontsize=8, loc="lower left")
    a1.grid(axis="y", color=C_GRID, linewidth=0.8)
    a1.set_axisbelow(True)
    clean(a1)

    for i, (qid, rows) in enumerate(ef.items()):
        xs = [r["ef"] for r in rows]
        ys = [r["recall"] for r in rows]
        a2.plot(xs, ys, "o-" if i == 0 else "s--", color=[C_RED, C_BLUE][i], label=qid)
    a2.set_xscale("log")
    a2.set_xticks([40, 80, 200, 500, 1000])
    a2.set_xticklabels(["40", "80", "200", "500", "1000"])
    a2.set_ylim(-0.05, 1.08)
    a2.set_xlabel("hnsw.ef_search")
    a2.set_ylabel("Recall@10")
    a2.set_title("ef=80 时一条查询完全落空，ef=500 恢复")
    a2.legend(frameon=False, fontsize=8, loc="lower right")
    a2.grid(color=C_GRID, linewidth=0.8)
    clean(a2)
    fig.tight_layout()
    fig.savefig(os.path.join(BASE, "fig4_quality.png"))
    plt.close(fig)


def fig5() -> None:
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 3.3))
    ns = ["前 1,000", "前 5,000", "前 20,000"]
    law = [0, 0, 2]
    case = [1000, 5000, 19998]
    x = np.arange(3)
    a1.bar(x, case, 0.55, color=C_MUTED, label="案例块")
    a1.bar(x, law, 0.55, bottom=case, color=C_AMBER, label="法条块")
    a1.set_yscale("log")
    a1.set_ylim(500, 200000)
    a1.set_xticks(x)
    a1.set_xticklabels(ns)
    a1.set_ylabel("近邻数量（对数）")
    a1.set_title("查询的近邻圈几乎全是判例：前 5,000 名中 0 条法条")
    a1.legend(frameon=False, fontsize=8)
    a1.grid(axis="y", color=C_GRID, linewidth=0.8, which="both")
    a1.set_axisbelow(True)
    for xi, c, l in zip(x, case, law):
        a1.text(xi, c * 3.2, f"{c:,} 判例 / {l} 法条", ha="center", fontsize=8,
                color=C_TEXT, fontweight="bold")
    clean(a1)

    labels = ["修复前\nlaw", "修复后\nlaw"]
    vals = [0, 24]
    bars = a2.bar(labels, vals, color=[C_RED, C_GREEN], width=0.5)
    a2.set_ylim(0, 30)
    a2.set_ylabel("返回条数（请求 24）")
    a2.set_title("20,000 候选有界扩展：法条 0 → 24")
    for bar, v in zip(bars, vals):
        a2.text(bar.get_x() + bar.get_width() / 2, v + 0.8, str(v), ha="center",
                fontsize=11, fontweight="bold", color=C_RED if v == 0 else C_TEXT)
    a2.grid(axis="y", color=C_GRID, linewidth=0.8)
    a2.set_axisbelow(True)
    clean(a2)
    fig.tight_layout()
    fig.savefig(os.path.join(BASE, "fig5_partition.png"))
    plt.close(fig)


if __name__ == "__main__":
    d = load("acceptance.json")
    baseline = 306380.6
    p = "outputs/hnsw_fullscale/plan_compare.json"
    if os.path.exists(p):
        with open(p, encoding="utf-8") as fh:
            baseline = json.load(fh)["current_app_sql(现状)"]["ms"]
    fig1(d, baseline)
    fig2(d)
    fig3(d)
    fig4(d)
    fig5()
    print("charts written")
