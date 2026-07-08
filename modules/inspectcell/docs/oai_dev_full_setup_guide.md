# OAI Dev Setup Guide for the Industrial Inspection Pipeline

> Artifact note: this guide preserves the full lab setup procedure used to
> create the isolated OAI-dev environment. It contains machine-specific paths
> from the measurement host. For demo-paper experiments, use this for one-time
> environment bring-up, then run the paper workflow from
> [runbook_two_ue_flexric.md](runbook_two_ue_flexric.md).

This guide documents the **full OAI setup we used for the OAI-dev industrial inspection pipeline**, including:

- creating a separate OAI dev core so the stable UERANSIM setup is not broken,
- configuring dev Docker networks and containers,
- running OAI gNB in RFsim mode,
- running OAI nrUE and creating `oaitun_ue1`,
- creating the `edge-dev` container on the dev N6 network,
- running the edge relay, UE forwarder, factory relay, and Isaac spawning script,
- verifying that image packets actually pass through the OAI user-plane path,
- optionally adding communication-resource / PRB-like bandwidth limits.

This guide assumes your main project paths are:

```bash
~/user/ORBIT3C_OAI_DEV
~/user/OAI/oai-cn5g-fed/docker-compose
/home/elahe/user/Co_design_codes
```

---

## 0. Final Target Architecture

The target OAI-dev architecture is:

```text
Isaac / Factory relay
    ↓ TCP to UE forwarder
UE forwarder on host
    ↓ bound to oaitun_ue1
OAI nrUE
    ↓ RFsim
OAI gNB
    ↓ N3 / GTP-U
vpp-upf-dev
    ↓ N6
edge-dev container
    ↓
YOLO edge inference result
```

The working dev IP plan is:

```text
Dev public / N2 / N4 / SBI network: 192.168.80.0/24
Dev access / N3 network:           192.168.82.0/24
Dev core / N6 network:             192.168.83.0/24

oai-amf-dev:       192.168.80.132
oai-smf-dev:       192.168.80.133
vpp-upf-dev N4:    192.168.80.201
vpp-upf-dev N3:    192.168.82.201
vpp-upf-dev N6:    192.168.83.201
oai-ext-dn-dev:    192.168.83.135
edge-dev:          192.168.83.150
OAI UE tunnel:     12.1.1.x / oaitun_ue1
```

The dev slice/DNN is:

```text
DNN = oai
SST = 1
SD  = FFFFFF
```

---

## 1. Why We Created a Separate OAI Dev Core

The existing stable setup used UERANSIM and these networks:

```text
demo-oai-public-net = 192.168.70.0/24
oai-public-access  = 192.168.72.0/24
oai-public-core    = 192.168.73.0/24
```

That stable setup used:

```text
DNN = default
SST = 222
SD  = 00007B
```

OAI nrUE rejected `nssai_sst = 222`, because the OAI UE config accepted SST values in the range 1–4. Therefore, we created a **separate dev core** with:

```text
DNN = oai
SST = 1
SD  = FFFFFF
```

This avoids breaking the working UERANSIM setup.

---

## 2. Find the Original Stable OAI Compose File

To locate the original Compose file used by the stable core:

```bash
docker inspect oai-amf --format='{{ index .Config.Labels "com.docker.compose.project.working_dir" }}'
docker inspect oai-amf --format='{{ index .Config.Labels "com.docker.compose.project.config_files" }}'
docker inspect oai-amf --format='{{ index .Config.Labels "com.docker.compose.project" }}'
```

In our setup, the stable core was launched from:

```text
/home/elahe/user/OAI/oai-cn5g-fed/docker-compose
```

with:

```text
docker-compose-basic-vpp-nrf.yaml
```

---

## 3. Backup the Stable Core Configs

Before modifying anything, save stable configuration files:

```bash
BACKUP_DIR=~/user/ORBIT3C_OAI_DEV/backups/stable_core_$(date +%Y%m%d_%H%M)
mkdir -p "$BACKUP_DIR"

docker cp oai-smf:/openair-smf/etc/config.yaml "$BACKUP_DIR/smf_config.yaml"
docker cp vpp-upf:/openair-upf/etc/upf_profile.json "$BACKUP_DIR/upf_profile.json"
docker cp vpp-upf:/openair-upf/etc/init.conf "$BACKUP_DIR/upf_init.conf"
docker cp ueransim:/ueransim/etc "$BACKUP_DIR/ueransim_etc"

echo "Saved stable backup to: $BACKUP_DIR"
```

Verify the stable setup still works:

```bash
docker exec -it ueransim ping -I uesimtun0 -c 3 192.168.73.135
```

---

## 4. Create the Dev Core Compose Folder

Create a dev copy of the OAI core compose directory:

```bash
mkdir -p ~/user/ORBIT3C_OAI_DEV/dev_core

cp -r ~/user/OAI/oai-cn5g-fed/docker-compose \
      ~/user/ORBIT3C_OAI_DEV/dev_core/docker-compose-dev

cd ~/user/ORBIT3C_OAI_DEV/dev_core/docker-compose-dev

cp docker-compose-basic-vpp-nrf.yaml docker-compose-orbit3c-dev.yaml
```

Make a backup of the dev compose file:

```bash
cp docker-compose-orbit3c-dev.yaml docker-compose-orbit3c-dev.yaml.bak
```

---

## 5. Modify the Dev Compose File

Open:

```bash
gedit ~/user/ORBIT3C_OAI_DEV/dev_core/docker-compose-dev/docker-compose-orbit3c-dev.yaml
```

Or use `sed` as below.

### 5.1 Rename Container Names

Inside the dev compose file, change container names:

```bash
cd ~/user/ORBIT3C_OAI_DEV/dev_core/docker-compose-dev

sed -i 's/container_name: "mysql"/container_name: "mysql-dev"/g' docker-compose-orbit3c-dev.yaml
sed -i 's/container_name: "oai-udr"/container_name: "oai-udr-dev"/g' docker-compose-orbit3c-dev.yaml
sed -i 's/container_name: "oai-udm"/container_name: "oai-udm-dev"/g' docker-compose-orbit3c-dev.yaml
sed -i 's/container_name: "oai-ausf"/container_name: "oai-ausf-dev"/g' docker-compose-orbit3c-dev.yaml
sed -i 's/container_name: "oai-nrf"/container_name: "oai-nrf-dev"/g' docker-compose-orbit3c-dev.yaml
sed -i 's/container_name: "oai-amf"/container_name: "oai-amf-dev"/g' docker-compose-orbit3c-dev.yaml
sed -i 's/container_name: "oai-smf"/container_name: "oai-smf-dev"/g' docker-compose-orbit3c-dev.yaml
sed -i 's/container_name: "vpp-upf"/container_name: "vpp-upf-dev"/g' docker-compose-orbit3c-dev.yaml
sed -i 's/container_name: "oai-ext-dn"/container_name: "oai-ext-dn-dev"/g' docker-compose-orbit3c-dev.yaml
```

Do **not** rename internal service keys unless needed. With `docker compose -p orbit3c-dev`, service names can remain internally consistent.

### 5.2 Change Subnets

Replace old stable subnets with dev subnets:

```bash
sed -i 's/192.168.70/192.168.80/g' docker-compose-orbit3c-dev.yaml
sed -i 's/192.168.72/192.168.82/g' docker-compose-orbit3c-dev.yaml
sed -i 's/192.168.73/192.168.83/g' docker-compose-orbit3c-dev.yaml
```

### 5.3 Rename Docker Networks

```bash
sed -i 's/name: demo-oai-public-net/name: orbit3c-dev-public-net/g' docker-compose-orbit3c-dev.yaml
sed -i 's/name: oai-public-access/name: orbit3c-dev-access/g' docker-compose-orbit3c-dev.yaml
sed -i 's/name: oai-public-core/name: orbit3c-dev-core/g' docker-compose-orbit3c-dev.yaml
```

### 5.4 Use Short Linux Bridge Names

Docker bridge interface names must be short. Use:

```bash
sed -i 's/com.docker.network.bridge.name: "demo-oai"/com.docker.network.bridge.name: "o3cdevpub"/g' docker-compose-orbit3c-dev.yaml
sed -i 's/com.docker.network.bridge.name: "cn5g-access"/com.docker.network.bridge.name: "o3cdevacc"/g' docker-compose-orbit3c-dev.yaml
sed -i 's/com.docker.network.bridge.name: "cn5g-core"/com.docker.network.bridge.name: "o3cdevcore"/g' docker-compose-orbit3c-dev.yaml
```

Do not use long bridge names such as `orbit3c-dev-access`; they can cause:

```text
failed to create network ... numerical result out of range
```

### 5.5 Change Dev DNN/Slice for OAI nrUE

```bash
sed -i 's/SNSSAI_SD=00007B/SNSSAI_SD=FFFFFF/g' docker-compose-orbit3c-dev.yaml
sed -i 's/SNSSAI_SST=222/SNSSAI_SST=1/g' docker-compose-orbit3c-dev.yaml
sed -i 's/DNN=default/DNN=oai/g' docker-compose-orbit3c-dev.yaml
```

### 5.6 Change External DN Return Route

The dev DNN `oai` uses the UE subnet:

```text
12.1.1.128/25
```

Update the route in `oai-ext-dn-dev`:

```bash
sed -i 's/ip route add 12.1.1.0\/24 via 192.168.83.201/ip route add 12.1.1.128\/25 via 192.168.83.201/g' docker-compose-orbit3c-dev.yaml
```

### 5.7 Check the Edited Compose File

```bash
grep -nE "container_name:|orbit3c-dev|192.168.80|192.168.82|192.168.83|SNSSAI|DNN=|ip route add|vpp-upf|oai-smf|oai-amf|mysql" docker-compose-orbit3c-dev.yaml
```

Check that old names/IPs are gone:

```bash
grep -nE "demo-oai-public-net|oai-public-access|oai-public-core|192.168.70|192.168.72|192.168.73|container_name: \"mysql\"|container_name: \"oai-amf\"|container_name: \"vpp-upf\"" docker-compose-orbit3c-dev.yaml
```

The second command should return nothing.

---

## 6. Start the Dev OAI Core

Start the dev core with a separate Compose project name:

```bash
cd ~/user/ORBIT3C_OAI_DEV/dev_core/docker-compose-dev

docker compose -p orbit3c-dev -f docker-compose-orbit3c-dev.yaml up -d
```

Check:

```bash
docker ps | grep dev
docker network ls | grep orbit3c
ip addr | grep -E "192.168.80.1|192.168.82.1|192.168.83.1" -B 2 -A 2
```

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
```

Expected bridge IPs:

```text
o3cdevpub  = 192.168.80.1
o3cdevacc  = 192.168.82.1
o3cdevcore = 192.168.83.1
```

---

## 7. Create the Dev gNB Config

Copy the working gNB RFsim config:

```bash
cp ~/user/ORBIT3C_OAI_DEV/configs/gnb_orbit3c_rfsim.conf \
   ~/user/ORBIT3C_OAI_DEV/configs/gnb_orbit3c_dev_rfsim.conf
```

Open it:

```bash
gedit ~/user/ORBIT3C_OAI_DEV/configs/gnb_orbit3c_dev_rfsim.conf
```

Set the dev AMF IP:

```conf
amf_ip_address = ({ ipv4 = "192.168.80.132"; });
```

Set network interfaces:

```conf
NETWORK_INTERFACES :
{
    GNB_IPV4_ADDRESS_FOR_NG_AMF = "192.168.80.1/24";
    GNB_IPV4_ADDRESS_FOR_NGU    = "192.168.82.1/24";
    GNB_PORT_FOR_S1U            = 2152;
};
```

Keep PLMN/TAC consistent with your core:

```text
MCC = 208
MNC = 95
TAC = 0xa000
```

---

## 8. Run OAI gNB

Open **Terminal 1**:

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

Expected good signs:

```text
Received NGSetupResponse from AMF
Received NGAP_REGISTER_GNB_CNF
cell PLMN 208.95 ... is in service
```

Check RFsim server is listening:

```bash
sudo ss -tulpn | grep 4043
```

Expected:

```text
*:4043 users:(("nr-softmodem",...))
```

Keep this terminal running.

---

## 9. Create the Dev OAI nrUE Config

Create or edit:

```bash
gedit ~/user/ORBIT3C_OAI_DEV/configs/ue_orbit3c_dev_rfsim.conf
```

Use values compatible with the dev core:

```conf
uicc0 = {
  imsi = "208950000000032";
  key = "0C0A34601D4F07677303652C0462535B";
  opc = "63bfa50ee6523365ff14c1f45f88737d";

  pdu_sessions = (
    {
      pdu_session_id = 10;
      pdu_session_type = "IPv4";
      dnn = "oai";
      nssai_sst = 1;
      nssai_sd = "FFFFFF";
    }
  );
};

rfsimulator :
{
  serveraddr = "127.0.0.1";
  serverport = 4043;
  options = ();
  modelname = "AWGN";
};
```

Important:

```text
nssai_sst must be 1 for this dev OAI nrUE setup.
DNN must be oai.
SD must match FFFFFF.
```

---

## 10. Run OAI nrUE

Open **Terminal 2**:

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

Expected good signs:

```text
Received Registration Accept
Received PDU Session Establishment Accept
TUN Interface oaitun_ue1 successfully configured
```

Verify:

```bash
ip addr show oaitun_ue1
```

Expected:

```text
inet 12.1.1.xxx/24
```

Keep this terminal running.

---

## 11. Verify Dev User Plane to oai-ext-dn-dev

Test:

```bash
ping -I oaitun_ue1 -c 3 192.168.83.135
```

Check UPF session:

```bash
docker exec -it vpp-upf-dev /openair-upf/bin/vppctl show upf session
```

Expected in UPF session:

```text
Local F-TEID IPv4: 192.168.82.201
UE IP address: 12.1.1.xxx
Outer Header Creation ... IP: 192.168.82.1
```

Check N3 traffic:

```bash
sudo tcpdump -i o3cdevacc -n udp port 2152
```

Expected traffic between:

```text
192.168.82.1 ↔ 192.168.82.201
```

---

## 12. Create the edge-dev Container

Create a separate edge container on the dev N6/core network:

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

Install basic network tools:

```bash
docker exec -it edge-dev bash -lc "apt-get update && apt-get install -y iproute2 iputils-ping"
```

Add the return route to the UE subnet:

```bash
docker exec -it edge-dev ip route add 12.1.1.128/25 via 192.168.83.201 dev eth0
```

Check:

```bash
docker exec -it edge-dev ip route
```

Expected route:

```text
12.1.1.128/25 via 192.168.83.201 dev eth0
```

Test:

```bash
ping -I oaitun_ue1 -c 3 192.168.83.150
```

---

## 13. Install Edge Python Dependencies

Inside `edge-dev`:

```bash
docker exec -it edge-dev bash
cd /workspace
```

Install packages:

```bash
pip install ultralytics opencv-python-headless pandas pyyaml
```

If OpenCV import fails with:

```text
ImportError: libxcb.so.1: cannot open shared object file
```

install system libraries:

```bash
apt-get update
apt-get install -y libxcb1 libx11-6 libglib2.0-0 libgl1
```

Verify imports:

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

If `cuda available: False`, inference will run on CPU. That is acceptable for validating the pipeline, but inference delay will be higher than GPU.

---

## 14. Run the Edge Relay

Inside `edge-dev`:

```bash
cd /workspace
ls -lh *.pt
python edge_relay_oai_dev.py
```

Expected:

```text
[EDGE] Listening on 0.0.0.0:9100
```

Keep this terminal running.

---

## 15. Run the UE Forwarder

Open **Terminal 3** on the host:

```bash
cd /home/elahe/user/Co_design_codes
sudo python3 ue_forwarder_oai_dev.py
```

Expected:

```text
[UE-FWD] Listening on 0.0.0.0:9200
```

The dev UE forwarder should:

```text
listen on 0.0.0.0:9200
connect to edge-dev at 192.168.83.150:9100
force traffic through oaitun_ue1
```

Keep this terminal running.

---

## 16. Run the Factory Relay

Open **Terminal 4** on the host.

Use ROS Jazzy Python, not Anaconda Python 3.13:

```bash
conda deactivate
conda deactivate
source /opt/ros/jazzy/setup.bash
```

If you use your ROS venv:

```bash
source ~/ros_vision_venv/bin/activate
```

Check Python:

```bash
which python3
python3 --version
```

It should be Python 3.12, not Anaconda 3.13.

Run:

```bash
cd /home/elahe/user/Co_design_codes
python3 factory_relay_oai_dev.py
```

Expected:

```text
Factory relay started
```

Keep this terminal running.

---

## 17. Run the Isaac Spawning Script

In Isaac Sim Script Editor:

```python
exec(open("/home/elahe/user/Co_design_codes/distance_based_spawning_oai_dev.py").read())
```

This script publishes requests to:

```text
/fortuna_inspection/request
```

and listens to:

```text
/fortuna_inspection/ack
/fortuna_inspection/result
```

---

## 18. Verify the Full Pipeline

During a real run, check:

### 18.1 UE forwarder terminal

Expected:

```text
[UE-FWD] Host connection from ...
[UE-FWD] Connecting to edge 192.168.83.150:9100
[UE-FWD] Edge socket local endpoint: 12.1.1.xxx:...
[UE-FWD] ACK from edge
[UE-FWD] Result from edge
```

### 18.2 Edge relay terminal

Expected:

```text
[EDGE] Client connected
[EDGE] Received message type=request
[EDGE] ACK sent
[EDGE] Result sent
```

### 18.3 OAI N3 traffic

```bash
sudo tcpdump -i o3cdevacc -n udp port 2152
```

Expected:

```text
192.168.82.1 ↔ 192.168.82.201
```

This proves traffic uses the OAI gNB/UPF path.

### 18.4 CSV output

Factory CSV should contain rows with:

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

---

## 19. Optional: Emulate PRB / Communication Resource Limits

This does not directly change the OAI scheduler, but it emulates reduced radio resource availability by limiting throughput on the UE tunnel.

Check default:

```bash
sudo tc qdisc show dev oaitun_ue1
```

Default may show:

```text
qdisc fq_codel ...
```

Apply a 20 Mbps limit:

```bash
sudo tc qdisc replace dev oaitun_ue1 root tbf rate 20mbit burst 256kbit latency 600ms
sudo tc qdisc show dev oaitun_ue1
```

Run the pipeline and save the CSV:

```bash
cp /home/elahe/user/Co_design_codes/latency_results_factory_oai_dev_multi_best_n_1.0.csv \
   /home/elahe/user/Co_design_codes/latency_results_oai_dev_bw_20mbit.csv
```

Other examples:

```bash
sudo tc qdisc replace dev oaitun_ue1 root tbf rate 50mbit burst 256kbit latency 400ms
sudo tc qdisc replace dev oaitun_ue1 root tbf rate 10mbit burst 256kbit latency 800ms
sudo tc qdisc replace dev oaitun_ue1 root tbf rate 5mbit  burst 256kbit latency 1000ms
```

Remove limit:

```bash
sudo tc qdisc del dev oaitun_ue1 root
```

Expected effect:

```text
Lower bandwidth → larger factory_time_to_ack_ms
Lower bandwidth → larger factory_roundtrip_ms
Lower bandwidth → more late_for_sort_edge
Inference_ms should remain mostly similar
```

---

## 20. Clean Shutdown

Stop the application terminals with `Ctrl+C` in this order:

```text
1. Isaac spawning script if still running
2. Factory relay
3. UE forwarder
4. Edge relay
5. OAI UE
6. OAI gNB
```

Stop dev containers if needed:

```bash
cd ~/user/ORBIT3C_OAI_DEV/dev_core/docker-compose-dev
docker compose -p orbit3c-dev -f docker-compose-orbit3c-dev.yaml down
```

If you want to keep the dev core ready for later, do not run `down`; just stop the application terminals.

---

## 21. Common Errors and Fixes

### Error: `nssai_sst: 222 invalid value`

Use:

```text
nssai_sst = 1
nssai_sd = FFFFFF
dnn = oai
```

in the OAI UE config.

### Error: `connect() to 127.0.0.1:4043 failed`

The gNB RFsim server is not running or not listening. Start gNB first and check:

```bash
sudo ss -tulpn | grep 4043
```

### Error: Docker network `numerical result out of range`

Bridge interface name is too long. Use short bridge names:

```text
o3cdevpub
o3cdevacc
o3cdevcore
```

### Error: `No module named rclpy._rclpy_pybind11`

You are using the wrong Python, likely Anaconda Python 3.13. Use ROS Jazzy Python 3.12:

```bash
conda deactivate
source /opt/ros/jazzy/setup.bash
python3 factory_relay_oai_dev.py
```

### Error: `ImportError: libxcb.so.1`

Inside `edge-dev`:

```bash
apt-get update
apt-get install -y libxcb1 libx11-6 libglib2.0-0 libgl1
```

### Ping to `edge-dev` fails

Check route inside `edge-dev`:

```bash
docker exec -it edge-dev ip route
```

Add if missing:

```bash
docker exec -it edge-dev ip route add 12.1.1.128/25 via 192.168.83.201 dev eth0
```

### Plain ping works but OAI path may be bypassed

Always verify with:

```bash
sudo tcpdump -i o3cdevacc -n udp port 2152
```

If GTP-U appears during image transmission, traffic is using OAI.

---

## 22. Recommended Terminal Layout

Use this order:

```text
Terminal 1: OAI gNB
Terminal 2: OAI nrUE
Terminal 3: edge-dev edge relay
Terminal 4: UE forwarder
Terminal 5: factory relay
Terminal 6: tcpdump verification
Terminal 7: Isaac Sim / spawning script
```

Keep gNB and UE running for the entire experiment.
