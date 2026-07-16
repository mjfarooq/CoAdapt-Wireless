"""
Two-panel proposal overview.

Left  : bistable allocation plane. The reduced-order response R_hat(theta) and the
        allocation rule theta = pi(R_hat) intersect at a low stable point (self-
        confirming lock-in), an unstable tipping point, and a high stable point.
        R1 identifies the curve and regions, R2 shifts operation across the tipping
        point into the high-utility basin, R3 is response drift shifting the curve.
        This plane cannot depict oscillation, since two monotone curves give only
        monotone stability.

Right : the second failure, which lives in the disclosure loop, not the allocation
        plane. A predictive promise the allocation cannot back drives a limit cycle
        and collapses utility; the same signal, backed by allocation, holds high.

Run:  python3 overview_figure.py   ->  figures/fig_overview.png
"""

import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch

from predictive_regimes import run_disclosure

HERE = os.path.dirname(os.path.abspath(__file__))
FIGDIR = os.path.join(HERE, "figures")

BLUE, RED, ORANGE, PURPLE, GREEN = "#1f4e9c", "#c0392b", "#e67e22", "#6c3483", "#1e8449"


def sig(t, lo, hi, t0, k):
    return lo + (hi - lo) / (1.0 + np.exp(-k * (t - t0)))


def intersections(x, a, b):
    f = a - b
    idx = np.where(np.diff(np.sign(f)))[0]
    return [(x[i], 0.5 * (a[i] + b[i])) for i in idx]


def left_panel(ax):
    theta = np.linspace(0, 12, 500)
    R = sig(theta, 1.0, 9.0, 5.0, 1.3)          # reduced-order slice response
    L = 0.75 * theta + 0.9                        # allocation from measured load
    Rd = sig(theta, 1.0, 9.0, 3.1, 1.5)           # drifted response (R3)

    pts = intersections(theta, R, L)
    (ax_x, ax_y), (bx, by), (cx, cy) = pts[0], pts[1], pts[2]

    ax.axvspan(0, bx, color=RED, alpha=0.06)
    ax.axvspan(bx, 12, color=GREEN, alpha=0.07)
    ax.axvline(bx, color="0.5", ls=":", lw=1)

    ax.plot(theta, Rd, "--", color=ORANGE, lw=2, alpha=0.9)
    ax.plot(theta, R, "-", color=BLUE, lw=2.6, label=r"reduced-order slice response  $\hat R(\theta)$")
    ax.plot(theta, L, ":", color=RED, lw=2.4, label=r"allocation from measured load  $\theta=\pi(\hat R)$")

    ax.scatter([ax_x], [ax_y], s=130, color=RED, zorder=5, edgecolor="k", linewidth=0.6)
    ax.annotate("low", (ax_x, ax_y), xytext=(ax_x + 0.2, ax_y - 0.9), color=RED, fontsize=11, weight="bold")
    ax.scatter([bx], [by], s=120, facecolor="white", edgecolor="k", linewidth=1.6, zorder=5)
    ax.annotate("unstable\n(tipping point)", (bx, by), xytext=(bx + 0.35, by - 1.7), fontsize=9.5)
    ax.scatter([cx], [cy], s=240, marker="*", color=GREEN, zorder=5, edgecolor="k", linewidth=0.5)
    ax.annotate("high", (cx, cy), xytext=(cx - 0.4, cy + 0.5), color=GREEN, fontsize=11, weight="bold")

    ax.annotate(r"R1: identify $\hat R(\theta)$" + "\nand operating regions",
                (8.4, 8.6), color=BLUE, fontsize=11, ha="center")
    ax.annotate(r"R3: response drift" + "\n" + r"shifts $\hat R(\theta)$",
                (2.0, 6.2), color=ORANGE, fontsize=10.5, ha="center")
    ax.annotate("", xy=(3.0, 5.4), xytext=(2.2, 5.9),
                arrowprops=dict(arrowstyle="->", color=ORANGE, lw=1.6))

    arr = FancyArrowPatch((bx - 2.2, by - 0.6), (bx + 2.2, by - 0.6),
                          arrowstyle="<->", mutation_scale=16, color=PURPLE, lw=2)
    ax.add_patch(arr)
    ax.annotate("R2: shift operation into\nthe high-utility basin",
                (bx, by - 2.4), color=PURPLE, fontsize=10.5, ha="center")

    ax.set_xlabel(r"allocated radio resources,  $\theta$", fontsize=12)
    ax.set_ylabel("post-adaptation slice load", fontsize=12)
    ax.set_title("A. Allocation plane: lock-in, escape, drift", fontsize=12)
    ax.set_xlim(0, 12); ax.set_ylim(0, 10.5)
    ax.set_xticks([]); ax.set_yticks([])
    ax.legend(fontsize=9.5, loc="upper left", framealpha=0.95)


def right_panel(ax):
    osc = run_disclosure(period=6, backed=False, steps=140)
    stab = run_disclosure(period=6, backed=True, steps=140)
    ax.plot(osc["util"], color=RED, lw=1.6, label="disclosure not backed")
    ax.plot(stab["util"], color=GREEN, lw=1.8, label="allocation-backed")
    ax.set_title("B. Disclosure loop: oscillation and its cure", fontsize=12)
    ax.set_xlabel("control interval", fontsize=12)
    ax.set_ylabel("inspection utility", fontsize=12)
    ax.set_ylim(-0.05, 1.1)
    ax.grid(alpha=0.3)
    ax.legend(fontsize=9.5, loc="center right", framealpha=0.95)
    ax.annotate("promise the allocation\ncannot back  ->  limit cycle",
                (70, 0.44), color=RED, fontsize=9.5, ha="center")


def main():
    os.makedirs(FIGDIR, exist_ok=True)
    fig, ax = plt.subplots(1, 2, figsize=(14.5, 5.4), gridspec_kw={"width_ratios": [1.25, 1]})
    left_panel(ax[0])
    right_panel(ax[1])
    fig.suptitle("Policy-induced demand has two failure modes in different spaces",
                 y=1.01, fontsize=13)
    fig.tight_layout()
    out = os.path.join(FIGDIR, "fig_overview.png")
    fig.savefig(out, dpi=160, bbox_inches="tight")
    print("figure written:", out)


if __name__ == "__main__":
    main()
