# Data Platform Overview

The Data Platform program runs the ingest pipeline that turns raw buoy packets
into the published Tidewater Index.

## Pipeline stages

1. **Collect** - telemetry packets land in the intake queue.
2. **Validate** - malformed or out-of-range readings are dropped.
3. **Normalize** - units are converted to the canonical schema.
4. **Publish** - the rolling Tidewater Index is written to the open data mirror.

The pipeline ingests roughly **2,880 readings per day** from the active fleet.
The data platform is led by Priya Anand, who took over the lead role in 2021.

## Salinity units

Tidewater Index v1 reported salinity in parts per thousand (ppt). Starting with
Tidewater Index v2, salinity is reported in practical salinity units (PSU). The
unit switch is the single biggest breaking change between v1 and v2.

The original Tidewater Index schema was co-authored by Devran Oksuz and Priya
Anand. The schema is published alongside the data under the Brackish Open Data
Licence v2.

Yusuf Karim maintains the validation rules under Priya Anand's direction.
