# Reformulated Research Thrusts — Co-Adaptive Control in AI-Native Wireless Networks

Formal restructuring of the CAREER research plan around a single object, three
formal verbs, and two failure regimes, each with a matched control lever and
verified preliminary evidence.

## Central object

The demand a slice presents to an AI-native RAN is not an exogenous workload.
It is produced by hidden, stateful endpoint adaptation and is reshaped by the
network's own allocation and disclosure decisions. Call this **policy-induced
demand**, written R(theta, sigma), the post-adaptation load a slice generates
under allocation theta and disclosure sigma.

Policy-induced demand is the noun the entire project acts on. Thrust 1
identifies and characterizes it, Thrust 2 steers it, Thrust 3 hardens control
against its decay.

## Guiding question

How can an AI-native RAN identify the demand its own decisions induce, and
allocate and disclose so that the coupled RAN-endpoint loop reaches high-utility
operation instead of self-confirming lock-in or disclosure-driven oscillation,
while endpoint behavior remains unknown and drifting.

## The logic

The three thrusts follow a single dependency chain.

You cannot steer what you cannot identify, and you cannot claim a control
contribution without first establishing that standard control fails. **Thrust 1**
therefore does two things at once. It identifies policy-induced demand from
network-side telemetry, including the hard case of distinguishing demand that is
naturally low from demand that prior allocation suppressed. And it characterizes
the two ways a standard predict-then-allocate controller fails under that
demand, self-confirming lock-in and disclosure-driven oscillation. This thrust
delivers the object and the negative result that motivate everything after it.

The two failures have two different causes, so they need two matched levers.
**Thrust 2** builds both. Cultivation allocation spends bounded, SLA-safe
resource to reveal and sustain latent high-value demand, defeating lock-in.
Allocation-backed disclosure treats guidance credibility as a state the
controller can pay for with allocation, defeating oscillation. Together they
form a co-adaptive controller that reaches and holds high-utility operating
points.

The identified demand model and the credibility it rests on decay as endpoints
change. **Thrust 3** detects when the policy-induced demand model no longer
represents the deployed population, withdraws the model-dependent guarantees,
falls back to operation that is safe against both lock-in and oscillation, and
rebuilds the guarantees once the model is re-identified.

## Thrust 1. Characterizing policy-induced demand and its failure regimes

Establish policy-induced demand as an identifiable object and prove that
standard learned allocation fails on it in two characterizable ways.

**Task 1.1 — Identifying policy-induced demand from network telemetry.**
Represent the causal chain from allocation to delivered service to endpoint mode
to generated demand, rather than mapping allocation directly to load. Estimate,
from allocation-aware telemetry, the response quantities a controller needs, and
in particular distinguish a slice that is naturally low-demand from one that
prior allocation suppressed into a degraded mode. Passive telemetry cannot make
this distinction; a bounded, SLA-safe probe can.

**Task 1.2 — Characterizing the failure regimes.** Show that a
predict-then-allocate controller treating demand as exogenous converges to
self-confirming, degraded operating points, and bound the utility gap against a
controller that models the induced demand. Show that predictive network
disclosure that the allocation cannot back drives the coupled loop into
oscillation. Map both regimes over the engineering quantities that govern them,
endpoint sensitivity, resource slack, adaptation timing, and disclosure backing.

**Preliminary evidence in hand.** `modules/selfconfirming/` reproduces, at the
PRB level, the measured bistable response landscape, self-confirming lock-in
under a predict-then-allocate controller, the passive-versus-probe discrimination
of suppressed from genuine low demand, and disclosure-driven oscillation with its
utility collapse.

**Objection it defeats.** That the degraded equilibrium is a cartoon, that the
endpoint response cannot be identified from RAN telemetry, and that ignoring
induced demand is harmless.

## Thrust 2. Steering the co-adaptive loop through cultivation allocation and credible disclosure

Design the two control levers that defeat the two failures of Thrust 1, and
combine them into one co-adaptive controller.

**Task 2.1 — Cultivation allocation.** Extend constrained multi-agent allocation
so the controller treats its own influence on future demand as a decision
variable, spending bounded, SLA-safe resource to reveal and then sustain latent
high-value demand. The escape is reached at higher but justified resource,
because the high-value mode genuinely needs it, and the point is that the
demand-tracker never discovers that mode at all.

**Task 2.2 — Allocation-backed disclosure.** Design predictive guidance whose
credibility is a controllable state, updated from the historical agreement
between disclosed guidance and delivered service, and backed by allocation so
the promise can be made true. This prevents the oscillation of Thrust 1.
Credibility, not cadence, is what stabilizes the loop, so no update rate rescues
an unbacked promise.

**Preliminary evidence in hand.** Cultivation allocation escapes lock-in where
the demand-tracker locks in at equal starting conditions. Allocation-backed
disclosure holds high utility where an unbacked promise oscillates, at every
cadence tested.

**Objection it defeats.** That the guidance channel is vacuous, and that
response-aware allocation is just another constrained-RL retrain.

## Thrust 3. Hardening co-adaptive control against behavioral drift and model decay

Keep the guarantees of Thrust 2 honest as endpoint behavior evolves.

**Task 3.1 — Detecting model decay.** Monitor a policy-conditioned residual, the
deviation between observed behavior and what the current allocation and
disclosure should have induced, so that drift is detected against a moving,
policy-aware expectation rather than a static baseline.

**Task 3.2 — Confidence-aware fallback and recovery.** When confidence in the
demand model falls, withdraw the model-dependent guarantees and revert to
operation that is safe against both lock-in and oscillation, then rebuild the
guarantees once the model is re-identified. Treat model validity as a resource
that depletes and must be replenished.

**Preliminary evidence in hand.** The policy-conditioned residual crosses its
detection threshold well before the delay-violation rate moves, giving the
early-warning window that the confidence-aware fallback is designed to exploit.

**Objection it defeats.** That drift detection is generic change-point detection,
and that robustness is bolted on rather than tied to the same demand model.

## Through-line

One object, policy-induced demand. Three formal verbs, characterize, steer,
harden. Two failures, lock-in and oscillation, each with a matched lever,
cultivation allocation and allocation-backed disclosure. Every claim carries a
figure.

## Where performative prediction sits

Performative prediction is the lens that names why predict-then-allocate breaks,
the deployed policy reshaping the demand it later observes. It is not the
technical identity of the project. The contributions are on the network side,
identifying induced demand, allocating and disclosing to steer it, and hardening
against its decay, built on the PI's record in resource optimization, mechanism
design, and constrained multi-agent RL for O-RAN. The predictor is the thing
that breaks. The resource-side methods are the thing that fixes it.

## What changed from the previous framing

The previous framing kept assistance and disclosure as an optional late-stage
extension. The verified experiments show oscillation exists only in the
disclosure loop, so allocation-backed disclosure is not optional decoration. It
is the mechanism that defeats one of the two headline failures and it belongs in
the core of Thrust 2. The heavier information-design theory remains an extension.
