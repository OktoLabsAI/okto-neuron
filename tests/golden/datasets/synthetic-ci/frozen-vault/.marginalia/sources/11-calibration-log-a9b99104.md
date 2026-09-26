# Salinity Calibration Log

Salinity probes on every HTC buoy are calibrated on a six-month cycle by Nadia
Belmonte. Calibration restores the probe reading to within 0.1 PSU of a
reference standard.

| Date | Buoy | Drift before calibration |
|------|------|--------------------------|
| 2023-03-01 | MS-1 | 0.4 PSU |
| 2023-03-01 | MS-3 | 0.3 PSU |
| 2023-09-01 | MS-1 | 0.5 PSU |
| 2023-09-01 | MS-7 | 0.2 PSU |

The worst drift recorded in 2023 was 0.5 PSU on buoy MS-1 in September. After
each calibration the probe is sealed and returned to service the same day.

Nadia Belmonte reports to the buoy operations lead, Devran Oksuz. Calibration
results feed the Validate stage of the data pipeline, where readings drifting
more than 1.0 PSU from neighbors are dropped.
