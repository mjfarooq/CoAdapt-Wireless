# CoAdapt Wireless Workbench

**Research and education for co-adaptive wireless control**

*Isaac Sim + OpenAirInterface workcell for co-adaptive wireless control*

CoAdapt Wireless Workbench supports research and education on co-adaptive
control in AI-native radio-access networks. It focuses on learned O-RAN control
under adaptive UE demand.

The initial release, **InspectCell**, couples a vision-inspection workcell in
NVIDIA Isaac Sim with an OpenAirInterface-based 5G setup. It exposes the central
loop studied in the project: the RAN changes allocation, the endpoint adapts
sensing or inference behavior, and the resulting demand, telemetry, and task
performance feed back into the next network decision.

Over the project, CoAdapt Wireless Workbench will grow from this seed workcell into a versioned
research and teaching platform with endpoint adaptation models, O-RAN telemetry
scripts, stability-map experiments, disclosure and allocation protocols,
strategic-UE scenarios, and course laboratories.

## Modules

| Module | Status | Description |
| --- | --- | --- |
| [InspectCell](modules/inspectcell/) | Initial release | Two-UE OAI/FlexRIC vision-inspection workcell in Isaac Sim, with static PRB slice control and the automated 192-configuration design-space sweep used by the demo paper. |

Future modules will add endpoint adaptation models, O-RAN telemetry scripts,
stability-map experiments, disclosure and allocation protocols, strategic-UE
scenarios, and course laboratories.

## Repository Layout

```text
.
├── README.md
├── .gitignore
└── modules/
    └── inspectcell/
        ├── README.md
        ├── requirements.txt
        ├── isaac.usd
        ├── best_n.pt
        ├── scripts/
        └── docs/
```

Each module is self-contained: its own README, dependencies, scripts, and
documentation live under `modules/<name>/`.

## Getting Started

Start with the module you want to run. For the initial release, see the
[InspectCell module README](modules/inspectcell/README.md) for scope, system
assumptions, the documentation roadmap, and the quick-start reproduction path.
