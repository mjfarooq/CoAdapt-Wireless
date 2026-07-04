# OAI-Integrated Factory Inspection Pipeline Setup

> Artifact note: this guide documents the OAI-dev application pipeline bring-up
> for the industrial inspection traffic path. It is useful for validating the
> OAI user plane and relays. The canonical demo-paper two-UE FlexRIC workflow is
> [runbook_two_ue_flexric.md](runbook_two_ue_flexric.md).

This document describes how to run the current **OAI-dev setup** for the factory inspection pipeline.

The goal is to route the inspection traffic through the OAI 5G path:

```text
Factory relay / Isaac Sim
    ↓
UE forwarder on host
    ↓ through oaitun_ue1
OAI nrUE
    ↓ RFsim
OAI gNB
    ↓ N3 GTP-U
vpp-upf-dev
    ↓ N6
edge-dev container
    ↓
YOLO edge inference result back to factory
```

---

## 1. Current Components

### OAI-dev core containers

The dev core runs separately from the stable UERANSIM core. It uses different container names and networks so that the stable setup is not affected.

Expected dev containers:

```text
mysql-dev
oai-nrf-dev
oai-udr-dev
oai-udm-dev
oai-ausf-dev
oai-amf-dev
oai-smf-dev
vpp-upf-dev
oai-ext-dn-dev
edge-dev
```

Expected dev networks:

```text
orbit3c-dev-public-net  → 192.168.80.0/24  # N2/N4/SBI side
orbit3c-dev-access      → 192.168.82.0/24  # N3/GTP-U side
orbit3c-dev-core        → 192.168.83.0/24  # N6/external edge side
```

Expected bridge interfaces on the host:

```text
o3cdevpub   → 192.168.80.1
o3cdevacc   → 192.168.82.1
o3cdevcore  → 192.168.83.1
```

Important IPs:

```text
oai-amf-dev      = 192.168.80.132
vpp-upf-dev N4   = 192.168.80.201
vpp-upf-dev N3   = 192.168.82.201
vpp-upf-dev N6   = 192.168.83.201
oai-ext-dn-dev   = 192.168.83.135
edge-dev         = 192.168.83.150
OAI UE tunnel    = oaitun_ue1, usually 12.1.1.xxx
```

---

## 2. Project Files

Main code folder:

```bash
/home/elahe/user/Co_design_codes/
```

Original scripts:

```text
edge_relay_latency_ackfix_multi_neu_threaded_edge_roi_rectangular_roi_rotate_before_inference.py
ue_forwarder_multi_coco.py
factory_relay_latency_multi_neu_edge_roi_factory_visibility_updated_rectangular_roi.py
distance_based_spawning_replay_partial_visibility_gc10.py
```

OAI-dev scripts:

```text
edge_relay_oai_dev.py
ue_forwarder_oai_dev.py
factory_relay_oai_dev.py
distance_based_spawning_oai_dev.py
```

Key OAI-dev changes:

```text
edge_relay_oai_dev.py
- Runs inside edge-dev.
- Listens on 0.0.0.0:9100.
- Externally reachable at 192.168.83.150:9100.

ue_forwarder_oai_dev.py
- Runs on the host.
- Listens for factory messages on 0.0.0.0:9200.
- Forwards to 192.168.83.150:9100.
- Forces traffic through oaitun_ue1 using SO_BINDTODEVICE.
- Dynamically uses the current oaitun_ue1 path instead of hardcoding old 12.1.1.2.

factory_relay_oai_dev.py
- Runs on the host with ROS Jazzy Python.
- Sends to the local UE forwarder, usually 127.0.0.1:9200.
- Keeps ROS topics, camera frame selection, ROI visibility check, and CSV logging.

distance_based_spawning_oai_dev.py
- Runs inside Isaac Sim.
- Publishes inspection requests on /fortuna_inspection/request.
- Receives ACK/result from the factory relay.
```

---

## 3. One-Time Setup

### 3.1 Start or verify dev core

```bash
cd ~/user/ORBIT3C_OAI_DEV/dev_core/docker-compose-dev

docker compose -p orbit3c-dev -f docker-compose-orbit3c-dev.yaml up -d

docker ps | grep dev
```

Expected: all `*-dev` core containers should be `Up` and healthy.

Check dev bridge IPs:

```bash
ip addr | grep -E "192.168.80.1|192.168.82.1|192.168.83.1" -B 2 -A 2
```

Expected:

```text
o3cdevpub   192.168.80.1
o3cdevacc   192.168.82.1
o3cdevcore  192.168.83.1
```

---

### 3.2 Create `edge-dev` container

If `edge-dev` already exists and works, skip to the route check below.

Create the container:

```bash
docker run -dit \
  --name edge-dev \
  --cap-add NET_ADMIN \
  --network orbit3c-dev-core \
  --ip 192.168.83.150 \
  -v /home/elahe/user/Co_design_codes:/workspace \
  python:3.12-slim \
  bash
```

Install network tools and Python packages:

```bash
docker exec -it edge-dev bash -lc "apt-get update && apt-get install -y iproute2 iputils-ping libxcb1 libx11-6 libglib2.0-0 libgl1"

docker exec -it edge-dev bash -lc "pip install ultralytics opencv-python-headless pandas pyyaml"
```

Add return route from edge-dev to the OAI UE subnet:

```bash
docker exec -it edge-dev ip route add 12.1.1.128/25 via 192.168.83.201 dev eth0
```

Check route:

```bash
docker exec -it edge-dev ip route
```

Expected route:

```text
12.1.1.128/25 via 192.168.83.201 dev eth0
```

If the route already exists, that is fine.

---

### 3.3 Check model file

Your edge relay expects the YOLO model file inside `/workspace`.

On host:

```bash
ls -lh /home/elahe/user/Co_design_codes/*.pt
```

Inside `edge-dev`:

```bash
docker exec -it edge-dev bash
cd /workspace
ls -lh *.pt
```

If `best_n.pt` is missing, copy it into:

```bash
/home/elahe/user/Co_design_codes/
```

---

## 4. Running the Full Pipeline

Use separate terminals. Keep the gNB and UE terminals running during the whole experiment.

---

### Terminal 1 — Start/check dev core

```bash
cd ~/user/ORBIT3C_OAI_DEV/dev_core/docker-compose-dev

docker compose -p orbit3c-dev -f docker-compose-orbit3c-dev.yaml up -d

docker ps | grep dev
```

If `edge-dev` is stopped:

```bash
docker start edge-dev
```

Check edge route:

```bash
docker exec -it edge-dev ip route
```

If the route is missing:

```bash
docker exec -it edge-dev ip route add 12.1.1.128/25 via 192.168.83.201 dev eth0
```

---

### Terminal 2 — Run OAI gNB

```bash
cd ~/user/ORBIT3C_OAI_DEV/oai_ran/openairinterface5g
source oaienv
cd cmake_targets/ran_build/build

sudo ./nr-softmodem \
  -O ~/user/ORBIT3C_OAI_DEV/configs/gnb_orbit3c_dev_rfsim.conf \
  --rfsim \
  --rfsimulator.[0].serveraddr server \
  --gNBs.[0].min_rxtxtime 6 \
  2>&1 | tee ~/user/ORBIT3C_OAI_DEV/logs/gnb_dev_rfsim.log
```

Good signs:

```text
Received NGSetupResponse from AMF
associated AMF 1
cell PLMN 208.95 ... is in service
Frame.Slot ...
```

Do not press `Ctrl+C` while testing.

---

### Terminal 3 — Run OAI nrUE

```bash
cd ~/user/ORBIT3C_OAI_DEV/oai_ran/openairinterface5g
source oaienv
cd cmake_targets/ran_build/build

sudo ./nr-uesoftmodem \
  -O ~/user/ORBIT3C_OAI_DEV/configs/ue_orbit3c_dev_rfsim.conf \
  --rfsim \
  -C 3619200000 \
  -r 106 \
  --numerology 1 \
  --ssb 516 \
  --band 78 \
  2>&1 | tee ~/user/ORBIT3C_OAI_DEV/logs/ue_dev_rfsim.log
```

Good signs:

```text
Received Registration Accept
Received PDU Session Establishment Accept
TUN Interface oaitun_ue1 successfully configured
```

Do not press `Ctrl+C` while testing.

Check the tunnel from another terminal:

```bash
ip addr show oaitun_ue1
```

Expected:

```text
inet 12.1.1.xxx/24
```

Test edge reachability:

```bash
ping -I oaitun_ue1 -c 3 192.168.83.150
```

---

### Terminal 4 — Run edge relay inside edge-dev

```bash
docker exec -it edge-dev bash
cd /workspace
python edge_relay_oai_dev.py
```

Good sign:

```text
[EDGE] Listening on 0.0.0.0:9100
```

If OpenCV import fails with `libxcb.so.1`, install the missing system libraries:

```bash
apt-get update
apt-get install -y libxcb1 libx11-6 libglib2.0-0 libgl1
```

Test imports:

```bash
python - <<'PY'
import cv2
import torch
from ultralytics import YOLO
print("cv2:", cv2.__version__)
print("torch:", torch.__version__)
print("cuda available:", torch.cuda.is_available())
print("imports ok")
PY
```

It is okay if `cuda available` is `False`; that only means CPU inference inside the container.

---

### Terminal 5 — Run UE forwarder on host

Run this on the host, not inside Docker.

```bash
cd /home/elahe/user/Co_design_codes
sudo python3 ue_forwarder_oai_dev.py
```

Good signs:

```text
[UE-FWD] Listening on 0.0.0.0:9200
[UE-FWD] Connecting to edge 192.168.83.150:9100
[UE-FWD] Edge socket local endpoint: 12.1.1.xxx:...
[UE-FWD] ACK from edge
[UE-FWD] Result from edge
```

The dev forwarder should force edge traffic through `oaitun_ue1`.

---

### Terminal 6 — Optional OAI traffic verification

To prove traffic goes through OAI N3/GTP-U:

```bash
sudo tcpdump -i o3cdevacc -n udp port 2152
```

During image transmission, expected traffic:

```text
192.168.82.1 ↔ 192.168.82.201
```

To check N6/edge side:

```bash
ip addr show oaitun_ue1
```

Find the current UE IP, for example `12.1.1.131`, then:

```bash
sudo tcpdump -i o3cdevcore -n "host 192.168.83.150 or host 12.1.1.131"
```

---

### Terminal 7 — Run factory relay on host

The factory relay uses ROS, so avoid Anaconda Python. ROS Jazzy `rclpy` is built for Python 3.12. If you run it with Anaconda Python 3.13, you may get `No module named rclpy._rclpy_pybind11`.

Use a clean ROS terminal:

```bash
conda deactivate
conda deactivate
source /opt/ros/jazzy/setup.bash
```

If you use your ROS virtual environment:

```bash
source ~/ros_vision_venv/bin/activate
```

Check Python:

```bash
which python3
python3 --version
```

Avoid:

```text
/home/elahe/anaconda3/bin/python3
Python 3.13.x
```

Run factory relay:

```bash
cd /home/elahe/user/Co_design_codes
python3 factory_relay_oai_dev.py
```

Good signs:

```text
Factory relay started
Connecting to UE forwarder at 127.0.0.1:9200
Published ACK locally
Published result locally
Latency row saved
```

---

### Terminal 8 — Run Isaac spawning script

Inside Isaac Sim Script Editor or your Isaac Python execution method:

```python
exec(open("/home/elahe/user/Co_design_codes/distance_based_spawning_oai_dev.py").read())
```

This script publishes inspection requests and receives ACK/result through ROS topics:

```text
/fortuna_inspection/request
/fortuna_inspection/ack
/fortuna_inspection/result
```

---

## 5. Correctness Checklist

The pipeline is correct if all of the following are true:

### UE tunnel exists

```bash
ip addr show oaitun_ue1
```

Expected:

```text
inet 12.1.1.xxx/24
```

### Edge is reachable through OAI UE tunnel

```bash
ping -I oaitun_ue1 -c 3 192.168.83.150
```

Expected:

```text
0% packet loss
```

### UE forwarder logs show ACK/result

Expected:

```text
[UE-FWD] Connecting to edge 192.168.83.150:9100
[UE-FWD] Edge socket local endpoint: 12.1.1.xxx:...
[UE-FWD] ACK from edge
[UE-FWD] Result from edge
```

### Edge relay logs show request/result

Expected:

```text
[EDGE] Client connected
[EDGE] Received message type=request
[EDGE] ACK sent
[EDGE] Result sent
```

### OAI N3 packets are visible

```bash
sudo tcpdump -i o3cdevacc -n udp port 2152
```

Expected:

```text
192.168.82.1 ↔ 192.168.82.201
```

### CSV files are saved

Factory CSV should include values such as:

```text
factory_send_ms
factory_time_to_ack_ms
factory_roundtrip_ms
edge_decode_ms
edge_process_ms
edge_total_ms
inference_ms
predicted_decision
predicted_class
status
```

Edge CSV should include inference/ROI fields such as:

```text
inference_ms
edge_decode_ms
edge_process_ms
edge_total_ms
edge_roi_found
edge_roi_status
predicted_class
predicted_confidence
```

---

## 6. Common Errors and Fixes

### Error: `rclpy._rclpy_pybind11` missing

Cause: using Anaconda Python 3.13 instead of ROS Jazzy Python 3.12.

Fix:

```bash
conda deactivate
conda deactivate
source /opt/ros/jazzy/setup.bash
source ~/ros_vision_venv/bin/activate  # if needed
python3 factory_relay_oai_dev.py
```

---

### Error: `cv2` missing inside edge-dev

Fix:

```bash
docker exec -it edge-dev bash
pip install ultralytics opencv-python-headless pandas pyyaml
```

---

### Error: `ImportError: libxcb.so.1`

Fix inside `edge-dev`:

```bash
apt-get update
apt-get install -y libxcb1 libx11-6 libglib2.0-0 libgl1
```

---

### Error: ping to edge-dev fails

Check edge route:

```bash
docker exec -it edge-dev ip route
```

If missing:

```bash
docker exec -it edge-dev ip route add 12.1.1.128/25 via 192.168.83.201 dev eth0
```

If route add gives `Operation not permitted`, recreate edge-dev with NET_ADMIN:

```bash
docker rm -f edge-dev

docker run -dit \
  --name edge-dev \
  --cap-add NET_ADMIN \
  --network orbit3c-dev-core \
  --ip 192.168.83.150 \
  -v /home/elahe/user/Co_design_codes:/workspace \
  python:3.12-slim \
  bash
```

---

### Error: `best_n.pt` not found

Check model location:

```bash
ls -lh /home/elahe/user/Co_design_codes/*.pt
docker exec -it edge-dev bash -lc "ls -lh /workspace/*.pt"
```

Copy model into the mounted folder:

```bash
cp /path/to/best_n.pt /home/elahe/user/Co_design_codes/
```

---

### No GTP-U packets on `o3cdevacc`

Check:

```bash
ip addr show oaitun_ue1
ping -I oaitun_ue1 -c 3 192.168.83.150
```

Make sure:

```text
gNB terminal is still running
UE terminal is still running
UE forwarder is using ue_forwarder_oai_dev.py
EDGE_IP is 192.168.83.150
traffic is bound to oaitun_ue1
```

---

## 7. Shutdown Order

When finished:

```text
1. Stop Isaac spawning script / wait until done.
2. Stop factory relay with Ctrl+C.
3. Stop UE forwarder with Ctrl+C.
4. Stop edge relay with Ctrl+C.
5. Stop OAI nrUE with Ctrl+C.
6. Stop OAI gNB with Ctrl+C.
7. Optional: stop dev core containers.
```

Optional dev core shutdown:

```bash
cd ~/user/ORBIT3C_OAI_DEV/dev_core/docker-compose-dev

docker compose -p orbit3c-dev -f docker-compose-orbit3c-dev.yaml down
```

Do not shut down the stable UERANSIM core unless you intentionally want to stop it.

---

## 8. Notes for Experiments

- Use `tcpdump -i o3cdevacc -n udp port 2152` during real image transmission to prove the traffic uses the OAI path.
- Plain `ping 192.168.83.150` may use the Docker bridge directly; use `ping -I oaitun_ue1` for proof.
- Keep gNB and UE terminals running throughout the experiment.
- The UE IP may change after restart, but `ue_forwarder_oai_dev.py` should use the interface path rather than relying on a fixed UE IP.
- CPU inference inside `edge-dev` is acceptable for functional validation. Later, recreate `edge-dev` with NVIDIA Docker if GPU inference is needed.
