"""
The experiment-budget result.

Claim: an AI-native RAN can learn an adaptive slice's steerable response, and escape
degraded lock-in, from a handful of SLA-safe experiments. Passive demand-tracking never
escapes, and undirected (random) probing needs many more experiments than
disagreement-directed probing to reach the same reliability.

Setup. A slice sits in a low operating mode. It will move to a high-utility mode only if a
sustained allocation delivers service past an unknown reachability threshold theta_th. The
RAN does not know theta_th. It may run K bounded, SLA-safe experiments (probes), each held
long enough for the slice to respond, and each returning a noisy observation of whether the
mode flipped. After K experiments the RAN commits a sustaining allocation. It escapes if
that allocation clears the true threshold.

Three strategies:
  - directed : probe where the posterior is most split about theta_th (max disagreement);
               this is Bayesian bisection and localizes the threshold exponentially fast.
  - random   : probe at a uniformly random safe level.
  - passive  : no experiments; a demand-tracker allocates to the measured (low) load and
               never crosses the threshold. Escape probability is zero by construction.

Honest boundary: if theta_th exceeds the largest SLA-safe probe, the mode is unreachable and
no strategy escapes. We report escape among reachable slices and state the reachable fraction.

Run: python3 experiment_budget.py  ->  figures/fig_experiment_budget.png
"""

import os
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
FIGDIR = os.path.join(HERE, "figures")

# --- safe experiment range and observation model ---------------------------- #
SLA_FLOOR = 0.10
PROBE_MAX = 1.00                     # largest SLA-safe probe (resource ceiling)
P_DETECT = 0.90                      # P(observe flip | service crossed threshold)
P_FALSE = 0.05                       # P(observe flip | did not cross)
COMMIT_MARGIN = 0.08                 # margin added to the estimate when committing
CONF_TOL = 0.13                      # commit an elevated allocation only when this confident
GRID = np.linspace(SLA_FLOOR, PROBE_MAX + 0.10, 240)   # hypothesis grid over theta_th


def observe(theta_probe, theta_th, rng):
    """Noisy mode-flip observation from a probe at allocation theta_probe."""
    crossed = theta_probe >= theta_th
    p = P_DETECT if crossed else P_FALSE
    return rng.random() < p


def likelihood(theta_probe, flip):
    """P(observation | theta_th on grid) for a probe at theta_probe."""
    crossed = GRID <= theta_probe            # threshold below probe -> service crosses it
    p_flip = np.where(crossed, P_DETECT, P_FALSE)
    return p_flip if flip else (1.0 - p_flip)


def run_trial(K, strategy, rng):
    """One slice with a random reachability threshold. Returns (escaped, err, reachable)."""
    theta_th = rng.uniform(0.35, 1.05)                 # unknown to the RAN; may exceed PROBE_MAX
    reachable = theta_th <= PROBE_MAX
    post = np.ones_like(GRID) / len(GRID)              # uniform prior over theta_th

    for _ in range(K):
        if strategy == "directed":
            cdf = np.cumsum(post)
            theta_probe = float(GRID[np.searchsorted(cdf, 0.5)])   # posterior median = bisection
            theta_probe = min(max(theta_probe, SLA_FLOOR), PROBE_MAX)
        else:  # random
            theta_probe = rng.uniform(SLA_FLOOR, PROBE_MAX)
        flip = observe(theta_probe, theta_th, rng)
        post = post * likelihood(theta_probe, flip)
        post /= post.sum()

    theta_hat = float(np.sum(GRID * post))             # posterior mean estimate
    std = float(np.sqrt(np.sum(post * (GRID - theta_hat) ** 2)))
    # A controller commits an elevated (sustaining) allocation only once it has
    # localized the threshold. Otherwise it stays in the safe low mode and does
    # not escape, exactly as a demand-tracker would.
    confident = std < CONF_TOL
    commit = min(PROBE_MAX, theta_hat + COMMIT_MARGIN)
    escaped = reachable and confident and (commit >= theta_th)
    err = abs(theta_hat - theta_th) if reachable else np.nan
    return escaped, err, reachable


def sweep(strategy, Ks, trials=1500, seed=0):
    rng = np.random.default_rng(seed)
    esc_rate, esc_lo, esc_hi, err_med = [], [], [], []
    for K in Ks:
        esc, err = [], []
        for _ in range(trials):
            e, r, reach = run_trial(K, strategy, rng)
            if reach:
                esc.append(e); err.append(r)
        esc = np.array(esc, float)
        m = esc.mean()
        # bootstrap CI
        bs = [esc[rng.integers(0, len(esc), len(esc))].mean() for _ in range(300)]
        esc_rate.append(m); esc_lo.append(np.percentile(bs, 5)); esc_hi.append(np.percentile(bs, 95))
        err_med.append(np.median(err))
    return (np.array(esc_rate), np.array(esc_lo), np.array(esc_hi), np.array(err_med))


def main():
    os.makedirs(FIGDIR, exist_ok=True)
    Ks = list(range(0, 13))
    d = sweep("directed", Ks)
    r = sweep("random", Ks)

    # reachable fraction (for honest reporting)
    rng = np.random.default_rng(7)
    reach_frac = np.mean([(rng.uniform(0.35, 1.05) <= PROBE_MAX) for _ in range(20000)])

    # smallest K at which directed escape exceeds 0.9
    k90 = next((K for K, v in zip(Ks, d[0]) if v >= 0.90), None)
    kr90 = next((K for K, v in zip(Ks, r[0]) if v >= 0.90), None)

    print("=" * 68)
    print("EXPERIMENT-BUDGET RESULT")
    print("=" * 68)
    print(f"reachable fraction of slices (theta_th <= probe ceiling) = {reach_frac:.2f}")
    print(f"passive demand-tracking escape probability = 0.00 (never crosses threshold)")
    print(f"directed escape prob by K: {[round(x,2) for x in d[0]]}")
    print(f"random   escape prob by K: {[round(x,2) for x in r[0]]}")
    print(f"directed reaches 0.90 escape at K = {k90}")
    print(f"random   reaches 0.90 escape at K = {kr90}")
    print(f"directed localization error by K: {[round(x,3) for x in d[3]]}")
    print("=" * 68)

    fig, ax = plt.subplots(1, 2, figsize=(13, 4.6))

    ax[0].axhline(0.0, color="#7f8c8d", ls="--", lw=1.5, label="passive demand-tracking")
    ax[0].fill_between(Ks, d[1], d[2], color="#27ae60", alpha=0.18)
    ax[0].plot(Ks, d[0], "-o", color="#27ae60", ms=4, label="disagreement-directed")
    ax[0].fill_between(Ks, r[1], r[2], color="#c0392b", alpha=0.15)
    ax[0].plot(Ks, r[0], "-s", color="#c0392b", ms=4, label="random probing")
    if k90 is not None:
        ax[0].annotate(f"escape > 0.9\nat K = {k90}", (k90, 0.9),
                       xytext=(k90 + 1.2, 0.62), fontsize=9,
                       arrowprops=dict(arrowstyle="->", color="#27ae60"))
    ax[0].set_title("A. A handful of safe experiments escapes lock-in")
    ax[0].set_xlabel("number of SLA-safe experiments  $K$")
    ax[0].set_ylabel("probability of escaping lock-in")
    ax[0].set_ylim(-0.05, 1.05); ax[0].grid(alpha=0.3); ax[0].legend(fontsize=9, loc="center right")

    ax[1].semilogy(Ks, d[3], "-o", color="#27ae60", ms=4, label="disagreement-directed")
    ax[1].semilogy(Ks, r[3], "-s", color="#c0392b", ms=4, label="random probing")
    ax[1].set_title("B. Directed experiments localize the response\nexponentially faster")
    ax[1].set_xlabel("number of SLA-safe experiments  $K$")
    ax[1].set_ylabel("reachability-threshold error")
    ax[1].grid(alpha=0.3, which="both"); ax[1].legend(fontsize=9)

    fig.suptitle("Learning to steer demand within a bounded, SLA-safe experiment budget", y=1.02, fontsize=12)
    fig.tight_layout()
    out = os.path.join(FIGDIR, "fig_experiment_budget.png")
    fig.savefig(out, dpi=160, bbox_inches="tight")
    print("figure written:", out)


if __name__ == "__main__":
    main()
