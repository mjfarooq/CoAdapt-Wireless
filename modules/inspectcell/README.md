# InspectCell

*Two-UE OAI/FlexRIC vision-inspection workcell — initial CoAdapt-NextG module*

InspectCell is the first scenario in [CoAdapt-NextG](../../README.md) and the
reproducible artifact for the demo-paper experiment path. It keeps the code and
instructions needed to run the two-UE OpenAirInterface RFsim/FlexRIC pipeline,
collect the measured design-space runs, and report the results used by the demo
paper.

The full research workspace contains training code, debug images, older
bandwidth-shaping experiments, and exploratory analysis. Those are intentionally
not included here.

## Scope

This module covers:

- industrial inspection UE on `oaitun_ue1`;
- background-traffic UE on `oaitun_ue2`;
- OpenAirInterface gNB/UE in RFsim mode;
- FlexRIC nearRT-RIC and static PRB slice-control xApp;
- edge inference, UE forwarding, factory relay, and Isaac Sim control;
- automated 192-configuration PRB/CPU/resolution/speed sweep;
- selected result summaries and figures for the demo paper.

It does not include:

- the full OAI/FlexRIC source tree;
- trained YOLO checkpoints;
- the Isaac Sim USD scene or NVIDIA Isaac Sim installation;
- raw per-run logs and debug images;
- older one-UE or Linux `tc` bandwidth-shaping experiments.

## Module Layout

```text
modules/inspectcell/
├── README.md
├── requirements.txt
├── isaac.usd
├── best_n.pt
├── scripts/
│   ├── isaac_experiment_controller_oai_dev.py
│   ├── factory_relay_oai_dev.py
│   ├── ue_forwarder_oai_dev.py
│   ├── edge_relay_oai_dev.py
│   ├── codesign_experiment_runner_v1.py
│   ├── codesign_prb_slice_experiment_runner_v1.py
│   ├── recover_oai_stack.sh
│   └── analyze_192_grid_resource_efficiency_cleaned.py
└── docs/
    ├── oai_dev_full_setup_guide.md
    ├── oai_dev_pipeline_setup.md
    ├── runbook_two_ue_flexric.md
    └── demo_paper_results.md
```

## System Assumptions

The commands in this module assume the same lab paths used during the
measurements:

```bash
export CO_DESIGN_DIR=/home/elahe/user/Co_design_codes/demo_two_ue_flexric_repo
export OAI_DEV_DIR=/home/elahe/user/ORBIT3C_OAI_DEV
export OAI_RAN_DIR=$OAI_DEV_DIR/oai_ran/openairinterface5g
export FLEXRIC_DIR=$OAI_RAN_DIR/openair2/E2AP/flexric
export LOG_DIR=$OAI_DEV_DIR/logs
```

If the module is cloned elsewhere, update those variables and any absolute
paths in the OAI config files or runner environment.

## Documentation Roadmap

Use the documents in this order when preparing a fresh demo-paper
reproduction:

1. [OAI-dev full setup guide](docs/oai_dev_full_setup_guide.md): one-time
   lab setup for the isolated OAI dev core, RFsim gNB/nrUE path, `edge-dev`
   container, and user-plane validation. This is the lowest-level bring-up
   guide.
2. [OAI-dev pipeline setup](docs/oai_dev_pipeline_setup.md): application
   pipeline bring-up for the industrial inspection traffic over the OAI user
   plane, including the edge relay, UE forwarder, factory relay, Isaac script,
   validation checks, and shutdown order.
3. [Two-UE OAI/FlexRIC runbook](docs/runbook_two_ue_flexric.md): canonical
   demo-paper execution path. Use this for the two-UE RFsim/FlexRIC experiment,
   static PRB slice-control xApp, smoke test, and full 192-configuration sweep.
4. [Demo paper results notes](docs/demo_paper_results.md): concise description
   of the selected result tables and paper-facing figures.

The first two guides document the OAI-dev environment and single-pipeline
bring-up. The paper measurements should be run from the two-UE FlexRIC runbook,
because that document includes the RNTI discovery, slice validation, background
UE traffic, and automated sweep controls used for the demo paper.

## Quick Start

1. For a new machine or rebuilt OAI environment, complete the one-time OAI-dev
   setup:

```bash
less docs/oai_dev_full_setup_guide.md
```

2. Bring up and validate the OAI-integrated inspection path:

```bash
less docs/oai_dev_pipeline_setup.md
```

3. Build or verify OAI/FlexRIC targets:

```bash
cd /home/elahe/user/ORBIT3C_OAI_DEV/oai_ran/openairinterface5g
cmake --build cmake_targets/ran_build/build --target nr-softmodem -j2

cd openair2/E2AP/flexric
cmake --build build --target xapp_orbit3c_static_slice_ctrl -j2
cmake --build build --target xapp_kpm_logger_orbit3c -j2
```

4. Start Isaac Sim, load the factory scene, and run:

```python
/home/elahe/user/Co_design_codes/demo_two_ue_flexric_repo/scripts/isaac_experiment_controller_oai_dev.py
```

5. From the host, run one smoke experiment:

```bash
cd /home/elahe/user/Co_design_codes/demo_two_ue_flexric_repo
source /opt/ros/jazzy/setup.bash
sudo -v

export ORBIT3C_START_STACK=1
export ORBIT3C_STARTUP_RECOVER_CORE=1
export ORBIT3C_AUTO_RECOVERY=1
export ORBIT3C_CORE_RECOVERY_MODE=recreate
export ORBIT3C_CORE_RECOVERY_SCOPE=full
export ORBIT3C_RNTI_TUNNEL_PROBE=1
export ORBIT3C_STRICT_SLICE_VALIDATION=1
export ORBIT3C_BG_TUN_IFACE=oaitun_ue2
export ORBIT3C_BG_IPERF_MBIT=30
export ORBIT3C_BG_IPERF_DIRECTION=ul
export ORBIT3C_ULSCH_GRANT_CSV=/home/elahe/user/ORBIT3C_OAI_DEV/logs/ulsch_grants.csv

python3 scripts/codesign_prb_slice_optimizer_runner_v1.py \
  --plan-json plans/default_rfsim_reproduce_plan.json
```

6. After the smoke run passes, run the full sweep:

```bash
python3 scripts/codesign_prb_slice_experiment_runner_v1.py
```

For detailed demo-paper setup, validation, and failure handling, see
[docs/runbook_two_ue_flexric.md](docs/runbook_two_ue_flexric.md).

## Most Important Experimental Rule

Do not manually assign stale RNTIs in a paper run. RNTIs can change after gNB,
UE, core, or RRC recovery. The defensible workflow is:

1. start both UEs in one `nr-uesoftmodem --num-ues 2` process;
2. verify both `oaitun_ue1` and `oaitun_ue2`;
3. probe each tunnel;
4. map the discovered `oaitun_ue1` RNTI to the industrial slice;
5. map the discovered `oaitun_ue2` RNTI to the background slice.

The automated runner does this when `ORBIT3C_RNTI_TUNNEL_PROBE=1` and
`ORBIT3C_STRICT_SLICE_VALIDATION=1`.
