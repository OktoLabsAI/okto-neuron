# Buoy Fleet Register

The Halverson Tidewater Collective operates a fleet of tide-and-salinity buoys
in the Maro Strait. Each buoy reports on a fixed cadence.

| Buoy | Deployed | Status | Sampling interval |
|------|----------|--------|-------------------|
| MS-1 | 2019-09-01 | active | 15 minutes |
| MS-2 | 2019-09-01 | decommissioned 2023-08-18 | 15 minutes |
| MS-3 | 2019-09-01 | active | 15 minutes |
| MS-7 | 2023-08-18 | active | 5 minutes |

As of the 2024 register, the fleet has **3 active buoys**. Buoy MS-7 replaced the
lost MS-2 and samples three times more often, at a 5-minute interval.

Each buoy carries one tide-pressure sensor and one salinity probe. The salinity
probes are calibrated by Nadia Belmonte every six months. The embedded firmware
on all buoy controllers was written by Obi Eze.

The buoys transmit over the Tidewater telemetry band. A single reading packet is
512 bytes. Buoy operations is led by Devran Oksuz.
