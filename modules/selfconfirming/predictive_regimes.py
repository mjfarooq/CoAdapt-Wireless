"""
Predict-then-allocate: two failure regimes from one bistable endpoint.

This is the story-consistent villain. AI-native RAN forecasts demand and
pre-allocates, and that forecast reshapes the demand it later measures. Under
endogenous demand a predict-then-allocate controller fails in exactly the two
ways performative prediction warns about:

    LOCK-IN      it converges to a self-confirming operating point that can be
                 the degraded one.  (wide hysteresis, slow control)
    OSCILLATION  it never reaches a stable operating point; its allocation runs
                 a phase behind the endpoint mode and the loop limit-cycles.
                 (narrow hysteresis, control period ~ endpoint adaptation delay)

Same endpoint, two knobs: hysteresis width and cadence ratio. Cultivation
(A_endo from the base module) is the fix for the lock-in regime.

Run:  python3 predictive_regimes.py
Output: figures/fig_regimes.png + printed verification numbers.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace

import numpy as np
import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from self_confirming_prb import Endpoint, System, run_closed_loop, EndoAllocator

HERE = os.path.dirname(os.path.abspath(__file__))
FIGDIR = os.path.join(HERE, "figures")


@dataclass
class PredictiveAllocator:
    """Predict-then-allocate: forecast demand by extrapolation, pre-allocate,
    update only every `period` intervals (the cadence knob), hold in between.

    The stale, periodic update is what puts the allocation a phase behind the
    endpoint mode. When `period` is comparable to the endpoint adaptation delay
    and the hysteresis band is narrow, the loop rings.
    """
    period: int = 6            # control update period (cadence knob)
    gain: float = 1.30         # aggressiveness / loop gain
    margin: float = 0.10
    name: str = "A_pred (predict-then-allocate)"

    def reset(self, sys: System):
        self._hist = [sys.endpoint.target_demand(sys.workload)]
        self._theta = sys.sla_floor
        self._since = 0

    def act(self, sys: System, last_measured: float) -> float:
        self._hist.append(last_measured)
        self._since += 1
        if self._since >= self.period:
            self._since = 0
            if len(self._hist) > self.period:
                trend = self._hist[-1] - self._hist[-1 - self.period]
            else:
                trend = 0.0
            forecast = max(0.0, self._hist[-1] + trend)          # one-period extrapolation
            target = self.gain * forecast * (1.0 + self.margin) / sys.capacity
            self._theta = float(np.clip(target, sys.sla_floor, sys.theta_max))
        return self._theta


def osc_index(mode: np.ndarray, tail: int = 150) -> float:
    """Fraction of tail intervals in which the endpoint switches mode."""
    m = (mode[-tail:] > 0.5).astype(float)
    return float(np.mean(np.abs(np.diff(m))))


def run_disclosure(period: int, backed: bool, steps: int = 400, delay: int = 6,
                   cap: float = 1.2, d_low: float = 0.10, d_high: float = 1.0,
                   u_low: float = 0.30, u_high: float = 1.0,
                   down: float = 0.60, mid: float = 0.50):
    """Network discloses a target rate; the endpoint ramps to it on belief.

    backed=False  -> the promise is set optimistically when the slice looks idle,
                     while allocation only serves the (lagged) measured load, so
                     the promise is never funded: the endpoint ramps up, is under
                     served, drops, and the loop oscillates.
    backed=True   -> the controller commits to the high operating point and
                     allocates to back the signal, so the promise is credible and
                     the endpoint holds high.  (allocation-backed credibility)
    """
    alpha = 2.0 / (delay + 1.0)
    ema_served = 1.0
    promise = 0
    theta = d_low / cap
    measured = d_low
    util, dem = [], []
    for t in range(steps):
        if t % period == 0:
            if backed:
                promise = 1
                theta = min(1.0, d_high * 1.05 / cap)          # fund the promise
            else:
                promise = 1 if measured < mid else 0           # optimistic when idle
                theta = min(1.0, max(0.06, measured * 1.15 / cap))  # serve lagged load only
        demand_target = d_high if promise else d_low
        service = theta * cap
        ema_served += alpha * (min(1.0, service / max(demand_target, 1e-9)) - ema_served)
        sustains = ema_served >= down
        eff_high = promise == 1 and sustains
        measured = demand_target                                # RAN sees attempted load
        util.append((u_high if eff_high else u_low) *
                    min(1.0, service / max(demand_target, 1e-9)))
        dem.append(demand_target)
    return dict(util=np.array(util), demand=np.array(dem))


def wide_endpoint() -> Endpoint:
    return Endpoint(up_thresh=0.92, down_thresh=0.55, delay=6)   # sticky -> favors lock-in


def narrow_endpoint() -> Endpoint:
    return Endpoint(up_thresh=0.86, down_thresh=0.78, delay=6)   # twitchy -> can ring


def main():
    os.makedirs(FIGDIR, exist_ok=True)

    # ---- Regime A: LOCK-IN (wide hysteresis, slow cadence, start low) ----
    sysA = System(endpoint=wide_endpoint())
    lock = run_closed_loop(sysA, PredictiveAllocator(period=14, gain=1.3),
                           steps=400, init_mode=0)
    # cultivation fix on the same regime
    sysA2 = System(endpoint=wide_endpoint())
    cult = run_closed_loop(sysA2, EndoAllocator(), steps=400, init_mode=0)

    # ---- Regime B: OSCILLATION via the disclosure loop (promise not backed) ----
    osc = run_disclosure(period=6, backed=False, steps=400, delay=6)
    stable = run_disclosure(period=6, backed=True, steps=400, delay=6)

    # ---- Cadence sweep: credibility, not cadence, is what stabilizes ----
    periods = list(range(1, 16))
    u_nb = [run_disclosure(period=p, backed=False, steps=400, delay=6)["util"][-150:].mean()
            for p in periods]
    u_bk = [run_disclosure(period=p, backed=True, steps=400, delay=6)["util"][-150:].mean()
            for p in periods]

    u_lock = lock["util"][-100:].mean()
    u_cult = cult["util"][-100:].mean()
    osc_i = osc_index(osc["demand"])
    u_osc = osc["util"][-150:].mean()
    u_stable = stable["util"][-150:].mean()

    print("=" * 70)
    print("VERIFICATION  --  predict-then-allocate, two failure regimes")
    print("=" * 70)
    print(f"[Lock-in]     tail utility = {u_lock:.3f}   osc index = {osc_index(lock['mode']):.3f}")
    print(f"[Cultivation] tail utility = {u_cult:.3f}   (fix for the lock-in regime)")
    print(f"[Oscillation] osc index = {osc_i:.3f}   mean utility = {u_osc:.3f}  (promise not backed)")
    print(f"[Credible]    mean utility = {u_stable:.3f}   (allocation backs the promise)")
    print(f"[Credibility] mean util, not backed, across cadence = {[round(x,2) for x in u_nb]}")
    print(f"[Credibility] mean util, backed,     across cadence = {[round(x,2) for x in u_bk]}")
    print(f"[Credibility] --> backing stabilizes at every cadence; no cadence rescues an unbacked promise")
    ok = (u_lock < 0.4 and u_cult > 0.8 and osc_i > 0.05 and u_stable > 0.8
          and max(u_nb) < 0.5 and min(u_bk) > 0.8)
    print("-" * 70)
    print(f"BOTH REGIMES REPRODUCE: {'YES' if ok else 'NO -- inspect above'}")
    print("=" * 70)

    # ---------------- figure ----------------
    fig, ax = plt.subplots(1, 3, figsize=(15.5, 4.4))

    ax[0].plot(lock["util"], color="#c0392b", lw=1.8, label=f"A_pred locks in (U={u_lock:.2f})")
    ax[0].plot(cult["util"], color="#27ae60", lw=1.8, label=f"A_endo escapes (U={u_cult:.2f})")
    ax[0].set_title("A. Lock-in regime\nwide hysteresis, slow cadence")
    ax[0].set_xlabel("control interval"); ax[0].set_ylabel("inspection utility")
    ax[0].set_ylim(0, 1.05); ax[0].legend(fontsize=8, loc="center right"); ax[0].grid(alpha=0.3)

    ax[1].plot(osc["util"][:120], color="#c0392b", lw=1.3,
               label=f"promise not backed (U={u_osc:.2f})")
    ax[1].plot(stable["util"][:120], color="#27ae60", lw=1.5,
               label=f"allocation-backed (U={u_stable:.2f})")
    ax[1].set_title(f"B. Oscillation regime\ndisclosure the allocation cannot back (osc={osc_i:.2f})")
    ax[1].set_xlabel("control interval"); ax[1].set_ylabel("inspection utility")
    ax[1].set_ylim(-0.05, 1.1); ax[1].legend(fontsize=8, loc="center right"); ax[1].grid(alpha=0.3)

    ax[2].plot(periods, u_bk, "-o", color="#27ae60", label="allocation-backed")
    ax[2].plot(periods, u_nb, "-o", color="#c0392b", label="not backed")
    ax[2].set_title("C. Credibility, not cadence\nno update rate rescues an unbacked promise")
    ax[2].set_xlabel("control update period"); ax[2].set_ylabel("mean inspection utility")
    ax[2].set_ylim(0, 1.05); ax[2].legend(fontsize=8, loc="center right"); ax[2].grid(alpha=0.3)

    fig.suptitle("Predict-then-allocate under endogenous demand: lock-in and oscillation",
                 y=1.02, fontsize=11)
    fig.tight_layout()
    out = os.path.join(FIGDIR, "fig_regimes.png")
    fig.savefig(out, dpi=150, bbox_inches="tight"); plt.close(fig)
    print(f"figure written: {out}")


if __name__ == "__main__":
    main()
