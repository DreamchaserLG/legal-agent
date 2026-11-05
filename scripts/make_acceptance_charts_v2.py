"""第二轮验收图表（修复后复测）。"""
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


def load(name):
    with open(os.path.join(BASE, name), encoding="utf-8") as fh:
        return json.load(fh)


def clean(ax):
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)


def fig_before_after(acc, rv):
    fig, axes = plt.subplots(1, 4, figsize=(13, 3.1))
    titles = ["并发失败率\n(4 线程)", "法条分区填充\n(3 查询平均)", "Recall@10\n(4 查询平均)", "纯 SQL P50"]
    before = [46.0, 0.0, 42.5, 11.57]
    after_fail = acc["C_perf"]["concurrency"]["4"]["errors"] / 24 * 100
    fills = [f["items"] / 24 for f in rv["filtered_fill"] if f["source_filter"] == "law"]
    after = [after_fail, sum(fills) / len(fills) * 100, rv["summary"]["500"]["recall_mean"] * 100,
             acc["C_perf"]["db_only"]["p50_ms"]]
    units = ["%", "%", "%", "ms"]

    for ax, title, b, a, u in zip(axes, titles, before, after, units):
        bars = ax.bar(["修复前", "修复后"], [b, a], color=[C_MUTED, C_GREEN], width=0.55)
        ax.set_title(title, fontsize=10)
        top = max(b, a) or 1
        ax.set_ylim(0, top * 1.35)
        for bar, v in zip(bars, [b, a]):
            ax.text(bar.get_x() + bar.get_width() / 2, v + top * 0.04,
                    f"{v:.0f}{u}" if u == "%" else f"{v:.1f}{u}",
                    ha="center", fontsize=10, fontweight="bold",
                    color=C_RED if (u == "%" and v > 20) or (u == "ms" and v > 40) else C_TEXT)
        ax.grid(axis="y", color=C_GRID, linewidth=0.8)
        ax.set_axisbelow(True)
        clean(ax)
    fig.suptitle("四项指标的前后对比：三项显著改善，SQL 延迟为 ef=500 付出的代价", fontsize=11,
                 fontweight="bold", y=1.02)
    fig.tight_layout()
    fig.savefig(os.path.join(BASE, "fig6_before_after.png"), bbox_inches="tight")
    plt.close(fig)


def fig_recall(rv):
    qs = rv["queries"]
    efs = ["80", "200", "500"]
    x = np.arange(len(qs))
    w = 0.25
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11.5, 3.4), gridspec_kw={"width_ratios": [1.5, 1]})
    for i, ef in enumerate(efs):
        vals = [q["ef"][ef]["recall"] for q in qs]
        a1.bar(x + (i - 1) * w, vals, w, color=[C_MUTED, C_AMBER, C_GREEN][i], label=f"ef_search={ef}")
    a1.set_xticks(x)
    a1.set_xticklabels([q["id"] for q in qs], fontsize=8)
    a1.set_ylim(0, 1.1)
    a1.set_ylabel("Recall@10")
    a1.set_title("ef 从 80 提到 500：含 0.0 的查询全部恢复")
    a1.legend(frameon=False, fontsize=8)
    a1.grid(axis="y", color=C_GRID, linewidth=0.8)
    a1.set_axisbelow(True)
    clean(a1)

    means = [rv["summary"][k]["recall_mean"] for k in efs]
    mins = [rv["summary"][k]["recall_min"] for k in efs]
    a2.plot(efs, means, "o-", color=C_BLUE, label="均值")
    a2.plot(efs, mins, "s--", color=C_RED, label="最低值")
    a2.set_ylim(0, 1.05)
    a2.set_ylabel("Recall@10")
    a2.set_title("均值 0.425 → 0.925")
    for xv, m in zip(efs, means):
        a2.text(xv, m + 0.04, f"{m:.3f}", ha="center", fontsize=9, fontweight="bold")
    a2.legend(frameon=False, fontsize=8, loc="lower right")
    a2.grid(color=C_GRID, linewidth=0.8)
    clean(a2)
    fig.tight_layout()
    fig.savefig(os.path.join(BASE, "fig7_recall_ef.png"))
    plt.close(fig)


def fig_filtered(acc, rv):
    fills = [f for f in rv["filtered_fill"] if f["source_filter"] == "law"]
    names = [f["id"] for f in fills]
    items = [f["items"] for f in fills]
    ms = [f["ms"] for f in fills]

    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 3.3))
    bars = a1.bar(names, items, color=[C_RED if v < 24 else C_GREEN for v in items], width=0.5)
    a1.axhline(24, color=C_MUTED, linestyle=":", linewidth=1)
    a1.text(1.55, 24.6, "请求 24 条", fontsize=8, color=C_MUTED, ha="right")
    a1.set_ylim(0, 30)
    a1.set_ylabel("返回条数")
    a1.set_title("法条分区：2/3 填满，1/3 仍卡在 5 条")
    for bar, v in zip(bars, items):
        a1.text(bar.get_x() + bar.get_width() / 2, v + 0.6, str(v), ha="center",
                fontsize=11, fontweight="bold", color=C_TEXT)
    a1.grid(axis="y", color=C_GRID, linewidth=0.8)
    a1.set_axisbelow(True)
    clean(a1)

    hy = acc["E_traces"]["hybrid"]
    labels, law_v, case_v, lex = [], [], [], []
    for t in hy:
        labels.append(t["id"])
        lv = cv = lx = 0
        for s in t["steps"]:
            if s["stage"].startswith("04_partition"):
                d = s["detail"]
                lx += d.get("lexical_ms", 0)
                if "law" in s["stage"]:
                    lv += d.get("vector_ms", 0)
                else:
                    cv += d.get("vector_ms", 0)
        law_v.append(lv)
        case_v.append(cv)
        lex.append(lx)
    x = np.arange(len(labels))
    a2.bar(x, law_v, 0.5, color=C_RED, label="法条分区向量检索")
    a2.bar(x, case_v, 0.5, bottom=law_v, color=C_BLUE, label="判例分区向量检索")
    a2.bar(x, lex, 0.5, bottom=np.array(law_v) + np.array(case_v), color="#A7F3D0", label="词法检索")
    a2.set_yscale("log")
    a2.set_xticks(x)
    a2.set_xticklabels(labels, fontsize=8)
    a2.set_ylabel("毫秒（对数）")
    a2.set_title("端到端 5.8–7.7 s 中，法条分区占 3.5–6.9 s")
    a2.legend(frameon=False, fontsize=7.5)
    a2.grid(axis="y", color=C_GRID, linewidth=0.8, which="both")
    a2.set_axisbelow(True)
    clean(a2)
    fig.tight_layout()
    fig.savefig(os.path.join(BASE, "fig8_filtered_cost.png"))
    plt.close(fig)


if __name__ == "__main__":
    acc = load("acceptance.json")
    rv = load("recall_after_ef500.json")
    fig_before_after(acc, rv)
    fig_recall(rv)
    fig_filtered(acc, rv)
    print("charts written")
