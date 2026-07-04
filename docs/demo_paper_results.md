# Demo Paper Results Notes

These notes summarize how to report the measured 192-configuration demo results
without overclaiming. The selected CSVs in `results_summary/` are copied from
the 162-carrier-PRB cleaned analysis output.

## Experimental Design

The demo uses 192 measured configurations:

```text
4 UL slice settings x 4 CPU allocations x 3 resolutions x 4 speeds
= 192 measured runs
```

Fixed parameters:

- DL slice percentage: 50%;
- background UE load: 30 Mbit/s uplink on `oaitun_ue2`;
- detector: `best_n.pt`;
- encoding: raw image payloads;
- carrier setting for these summaries: 162 PRBs.

The main metric is `correct_on_time_rate`: the fraction of attempted parts that
are both correctly detected and returned before the physical sorting deadline.

## Result Claims That Are Supported

The results support these claims:

- The artifact completes a full 192-point cross-layer sweep over wireless PRB
  share, edge CPU, image resolution, and conveyor speed.
- At low and moderate speeds, multiple configurations can satisfy the deadline,
  but their resource cost differs substantially.
- A joint policy can preserve the required correct-on-time rate with much lower
  resource cost than a fixed high-resource reference.
- At 2.0 m/s, the `correct_on_time_rate >= 0.5` constraint is not feasible in
  the measured grid; the demo should show this as a hard physical deadline
  stress case, not as a solved adaptation case.

Do not claim that joint co-design always improves raw production goodput. In
the current `production_goodput` summary, the joint choice ties the best
single-factor policy for most speeds and mainly improves resource efficiency
under a correct-on-time constraint.

## Paper-Ready Table

A compact table for the two-page demo paper can focus on the constrained
resource-efficiency result:

| Speed (m/s) | Fixed reference efficiency | Best single-factor efficiency | Joint efficiency | Joint config | Gain over best single |
|---:|---:|---:|---:|---|---:|
| 0.5 | 0.029 | 0.208 | 1.240 | UL15, CPU1, 1280x720 | 496.2% |
| 1.0 | 0.058 | 0.240 | 0.648 | UL25, CPU2, 1280x720 | 170.0% |
| 1.5 | 0.090 | 0.168 | 0.168 | UL75, CPU4, 1280x720 | 0.0% |
| 2.0 | Not feasible | Not feasible | Not feasible | Not feasible | N/A |

This table is derived from
`results_summary/paper_summary_table_efficiency_cot_ge_0p5.csv`.

## Suggested Text for the Results Paragraph

```text
We collected a 192-configuration design space spanning uplink PRB share, edge
CPU allocation, image resolution, and conveyor speed under a two-UE OAI RFsim
setup with FlexRIC-based slice control. The industrial UE carried the deadline-
critical inspection stream, while a second UE generated 30 Mbit/s uplink
background traffic. Across the measured grid, the best configuration changed
with conveyor speed: low-speed operation could preserve the correct-on-time
constraint with aggressive resource reduction, while higher-speed operation
required more CPU and wireless resources. With a correct-on-time threshold of
0.5, the joint policy improved resource-normalized production efficiency by
496% at 0.5 m/s and 170% at 1.0 m/s compared with the best single-knob policy.
At 1.5 m/s, the joint policy matched the best single-knob setting, and at
2.0 m/s no measured configuration reached the 0.5 correct-on-time threshold.
This is the behavior the demo exposes interactively: adaptation helps when the
measured design space contains feasible lower-cost choices, but physical
deadline pressure can still make the target infeasible.
```

## Suggested Replacement for the Demo Paper's Result Sentence

Replace broad language such as:

```text
the adaptive policy consistently improves correct-on-time performance compared
to fixed high-accuracy configurations
```

with:

```text
the measured design space shows that adaptation can reduce resource cost while
maintaining a target correct-on-time rate when feasible; under the strongest
deadline stress, no measured configuration satisfies the target, which the demo
uses to illustrate the physical limit of the pipeline.
```

This is more accurate for the current results.

## Figures to Use

Use at most one result figure in a two-page demo abstract. Good candidates:

- `figures/fig_paper_policy_comparison_cot_constrained_production_efficiency_cot_ge_0p5.png`
- `figures/pareto_resource_cost_vs_correct_on_time_rate.png`

For the demo paper, the constrained-efficiency policy comparison is more direct
than the full Pareto plot because it connects to the adaptation story in one
figure.
