# Two-UE OAI/FlexRIC Runbook

This runbook describes the clean demo-paper path: first bring up the stack by
hand to prove each layer works, then use the automated runner for repeatable
measurements.

## Architecture

```text
Isaac Sim / factory relay
  -> host UE forwarder bound through oaitun_ue1
  -> OAI nrUE UE1
  -> OAI gNB RFsim
  -> OAI core / UPF
  -> edge-dev container
  -> YOLO inference
  -> result returned to factory controller

Background UE:
  oaitun_ue2 -> iperf3 uplink traffic -> edge-dev

Control:
  nearRT-RIC + static slice xApp -> maps UE RNTIs to PRB slices
```

## Preconditions

- OAI dev core exists and uses DNN `oai`, SST `1`, SD `FFFFFF`.
- `edge-dev` is reachable at `192.168.83.150`.
- UE tunnel subnet is `12.1.1.128/25`.
- OAI RAN tree exists at `/home/elahe/user/ORBIT3C_OAI_DEV/oai_ran/openairinterface5g`.
- FlexRIC is under `openair2/E2AP/flexric`.
- `best_n.pt` is available to the edge container.
- Isaac Sim can run the factory scene and publish camera frames.

## Build Checks

```bash
cd /home/elahe/user/ORBIT3C_OAI_DEV/oai_ran/openairinterface5g
cmake --build cmake_targets/ran_build/build --target nr-softmodem -j2

cd openair2/E2AP/flexric
cmake --build build --target xapp_orbit3c_static_slice_ctrl -j2
cmake --build build --target xapp_kpm_logger_orbit3c -j2
```

Only rebuild what changed. Python script changes do not require CMake.

## Manual Bring-Up

Use the manual path only for debugging and for demonstrating the individual
steps. Do not keep these manual terminals running when the automated runner is
allowed to start the stack.

### 1. Verify Core and Edge

```bash
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
docker exec -it edge-dev ip route | grep 12.1.1
docker exec -it edge-dev ip route add 12.1.1.128/25 via 192.168.83.201 dev eth0
iperf3 --version
docker exec -it edge-dev iperf3 --version
```

Expected core containers include `oai-amf-dev`, `oai-smf-dev`, `vpp-upf-dev`,
`mysql-dev`, and `edge-dev`.

### 2. Start nearRT-RIC

```bash
cd /home/elahe/user/ORBIT3C_OAI_DEV/oai_ran/openairinterface5g/openair2/E2AP/flexric
./build/examples/ric/nearRT-RIC
```

### 3. Start E2-Enabled gNB

```bash
cd /home/elahe/user/ORBIT3C_OAI_DEV/oai_ran/openairinterface5g
source oaienv
cd cmake_targets/ran_build/build

sudo ORBIT3C_ULSCH_GRANT_CSV=/home/elahe/user/ORBIT3C_OAI_DEV/logs/ulsch_grants.csv \
  ./nr-softmodem \
  -O /home/elahe/user/ORBIT3C_OAI_DEV/configs/gnb_orbit3c_dev_rfsim_e2.conf \
  --rfsim \
  --rfsimulator.[0].serveraddr server \
  --gNBs.[0].min_rxtxtime 6 \
  2>&1 | tee /home/elahe/user/ORBIT3C_OAI_DEV/logs/gnb_dev_rfsim_e2.log
```

Validate E2 setup:

```bash
grep -iE "E2|nearRT|RIC|setup|connected" \
  /home/elahe/user/ORBIT3C_OAI_DEV/logs/gnb_dev_rfsim_e2.log | tail -80
```

### 4. Start Both UEs in One Process

Do not start UE1 and UE2 as two independent `nr-uesoftmodem` processes. This
can cause tunnel conflicts because both processes may try to create
`oaitun_ue1`.

Verify the two subscribers exist:

```bash
docker exec mysql-dev mysql -utest -ptest oai_db \
  -e "select ueid, supi from AuthenticationSubscription where ueid in ('208950000000032','208950000000033');"
```

Clean old UE state and start both UEs:

```bash
sudo pkill -f nr-uesoftmodem || true
sudo ip link del oaitun_ue1 2>/dev/null || true
sudo ip link del oaitun_ue2 2>/dev/null || true

cd /home/elahe/user/ORBIT3C_OAI_DEV/oai_ran/openairinterface5g
source oaienv
cd cmake_targets/ran_build/build

sudo ./nr-uesoftmodem \
  -O /home/elahe/user/ORBIT3C_OAI_DEV/configs/ue_orbit3c_dev_rfsim_2ue.conf \
  --rfsim \
  --num-ues 2 \
  -C 3619200000 \
  -r 106 \
  --numerology 1 \
  --ssb 516 \
  --band 78 \
  2>&1 | tee /home/elahe/user/ORBIT3C_OAI_DEV/logs/ue_2ue_dev_rfsim_e2.log
```

Validate both tunnels:

```bash
ip -4 -o addr show dev oaitun_ue1
ip -4 -o addr show dev oaitun_ue2
ping -I oaitun_ue1 -c 5 192.168.83.150
ping -I oaitun_ue2 -c 5 192.168.83.150
```

### 5. Start Application Relays Manually

Manual relays are useful for a live walk-through. The automated runner starts
and stops these itself during experiments.

Edge container:

```bash
docker exec -it edge-dev bash -lc '
cd /workspace
MODEL_PATH=best_n.pt RUN_SPEED=1.0 python3 edge_relay_oai_dev.py
'
```

Host UE forwarder:

```bash
cd /home/elahe/user/Co_design_codes/demo_two_ue_flexric_repo
sudo UE_BIND_IP=$(ip -4 -o addr show dev oaitun_ue1 | awk "{print \\$4}" | cut -d/ -f1) \
  EDGE_IP=192.168.83.150 \
  python3 scripts/ue_forwarder_multi_coco.py
```

Factory relay:

```bash
cd /home/elahe/user/Co_design_codes/demo_two_ue_flexric_repo
source /opt/ros/jazzy/setup.bash
python3 scripts/factory_relay_oai_dev.py
```

Isaac controller:

```python
/home/elahe/user/Co_design_codes/demo_two_ue_flexric_repo/scripts/isaac_experiment_controller_oai_dev.py
```

## Automated Measurement Path

Use this path for the paper measurements.

```bash
cd /home/elahe/user/Co_design_codes/demo_two_ue_flexric_repo
source /opt/ros/jazzy/setup.bash
sudo -v
sudo -n true && echo "sudo cache works"

unset ORBIT3C_SLICE_RNTI
unset ORBIT3C_BG_RNTI
export ORBIT3C_KPM_REPORT_MODE=ue
export ORBIT3C_KPM_SUB_DELAY_MS=500
export ORBIT3C_ULSCH_GRANT_CSV=/home/elahe/user/ORBIT3C_OAI_DEV/logs/ulsch_grants.csv

export ORBIT3C_BG_TUN_IFACE=oaitun_ue2
export ORBIT3C_BG_IPERF_MBIT=30
export ORBIT3C_BG_IPERF_DIRECTION=ul
export ORBIT3C_BG_IPERF_SEC=600

export ORBIT3C_AUTO_RECOVERY=1
export ORBIT3C_CORE_MONITOR=1
export ORBIT3C_STACK_STABILITY_WINDOW_SEC=30
export ORBIT3C_MAX_RECOVERY_RETRIES_PER_EXPERIMENT=1
export ORBIT3C_STARTUP_RECOVERY_ATTEMPTS=2
export ORBIT3C_SUDO_KEEPALIVE=1
export ORBIT3C_RNTI_TUNNEL_PROBE=1
export ORBIT3C_RNTI_PROBE_DURATION_SEC=4
export ORBIT3C_RNTI_PROBE_MIN_TBS_BYTES=1000
export ORBIT3C_RNTI_PROBE_DOMINANCE_RATIO=1.5
export ORBIT3C_STRICT_SLICE_VALIDATION=1
export ORBIT3C_SKIP_COMPLETED=1

export ORBIT3C_CORE_RECOVERY_MODE=recreate
export ORBIT3C_CORE_RECOVERY_SCOPE=full
export ORBIT3C_POST_CORE_RESTART_SLEEP_SEC=45
export ORBIT3C_RECOVERY_WAIT_TIMEOUT_SEC=240
export ORBIT3C_UPF_SESSION_WAIT_TIMEOUT_SEC=90
export ORBIT3C_UE_START_ATTEMPTS=2
export ORBIT3C_VALIDATE_UPF_SESSIONS=1
export ORBIT3C_RECOVERY_CONNECTIVITY_CHECK=tcp

python3 scripts/codesign_prb_slice_experiment_runner_v1.py
```

The runner starts/restarts nearRT-RIC, gNB, both UEs, background iperf, edge
relay, UE forwarder, factory relay, and the Isaac command sequence. It records
per-run logs under its configured results directory.

## Full 192-Configuration Grid

The measured grid used for the demo paper is:

```text
UL slice percentage: 15, 25, 50, 75
DL slice percentage: 50
Edge CPU allocation: 1, 2, 4, 8 cores
Image resolution: 320x180, 640x360, 1280x720
Conveyor speed: 0.5, 1.0, 1.5, 2.0 m/s
Background traffic: 30 Mbit/s uplink on oaitun_ue2
Model: best_n.pt
Encoding: raw
Total: 4 x 1 x 4 x 3 x 4 = 192 runs
```

## Optional Larger Carrier PRB Count

For a 162-PRB RFsim run:

```bash
python3 scripts/prepare_oai_prb_count_configs.py --n-rb 162 --name nrb_162

export ORBIT3C_N_RB=162
export ORBIT3C_DL_FREQ=3629280000
export ORBIT3C_CONFIG_DIR=/home/elahe/user/Co_design_codes/automated_experiments/generated_oai_configs/nrb_162
export ORBIT3C_SKIP_COMPLETED=0

export ORBIT3C_GNB_CMD="sudo -n ORBIT3C_ULSCH_GRANT_CSV=/home/elahe/user/ORBIT3C_OAI_DEV/logs/ulsch_grants.csv ./nr-softmodem -O ${ORBIT3C_CONFIG_DIR}/gnb_orbit3c_dev_rfsim_e2.conf --rfsim --rfsimulator.[0].serveraddr server --gNBs.[0].min_rxtxtime 6 2>&1 | tee /home/elahe/user/ORBIT3C_OAI_DEV/logs/gnb_dev_rfsim_e2.log"
export ORBIT3C_UE_CMD="sudo -n ./nr-uesoftmodem -O ${ORBIT3C_CONFIG_DIR}/ue_orbit3c_dev_rfsim_2ue.conf --rfsim --num-ues 2 -C ${ORBIT3C_DL_FREQ} -r ${ORBIT3C_N_RB} --numerology 1 --ssb 516 --band 78 2>&1 | tee /home/elahe/user/ORBIT3C_OAI_DEV/logs/ue_2ue_dev_rfsim_e2.log"
```

Run one smoke experiment before a full sweep:

```bash
python3 scripts/codesign_prb_slice_optimizer_runner_v1.py \
  --plan-json plans/default_rfsim_reproduce_plan.json
```

## Validation Checklist

Before collecting data:

- both subscribers exist in `mysql-dev`;
- `oaitun_ue1` and `oaitun_ue2` exist;
- both tunnels can reach `192.168.83.150`;
- edge container has a return route to `12.1.1.128/25`;
- E2 setup request/response appears in the gNB log;
- tunnel probing maps `oaitun_ue1` and `oaitun_ue2` to distinct active RNTIs;
- strict slice validation passes before Isaac starts.

Do not report a run as a two-UE slicing result if any of these checks fail.
