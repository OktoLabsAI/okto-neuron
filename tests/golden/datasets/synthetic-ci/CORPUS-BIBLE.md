# CORPUS BIBLE - synthetic-ci

The single source of truth for this fully-invented corpus. Everything here is
fiction: no real people, places, orgs, or events. Safe to commit (no secrets, no
PII). This file documents the canon, the planted facts the deterministic CI gate
checks, the near-duplicate distractors that should fool a naive matcher, and the
absent traps the system must decline.

The fictional world: the **Halverson Tidewater Collective (HTC)**, a coastal-
sensor data cooperative that runs tide/salinity buoys in the Maro Strait and
publishes the open **Tidewater Index** dataset.

---

## 1. Canon (the invented facts, by entity)

### Organization
- Name: Halverson Tidewater Collective (HTC). Founded 2019.
- Headquarters: Brackish Harbor. Second (satellite) office: Cape Verriden.
- Charter ratified 2019-03-14 at the inaugural members' assembly.
- Three programs: Buoy Operations, Data Platform, Community Trust.
- Public dataset: the Tidewater Index, released under the Brackish Open Data
  Licence (BODL).

### People (canonical name -> variants / role)
| Canonical name | Variants | Role |
|----------------|----------|------|
| Marisol Quintero | "Mari" | Founding director, 2019-2023 (stepped down 2023-01-09) |
| Greta Sandoval | - | Second director, from 2023-01-09 (current) |
| Devran Oksuz | "Dev" | Buoy operations lead, continuously since 2019 |
| Priya Anand | "Priya Rao" (pre-2021 maiden name) | Data platform engineer -> data platform lead (promoted 2021-02-10) |
| Tomas Larsen | - | Community trust coordinator |
| Nadia Belmonte | - | Salinity calibration specialist; reports to Devran Oksuz |
| Obi Eze | - | Embedded-firmware contractor (buoy controllers) |
| Hana Vesely | - | Grants officer |
| Rashid Farouk | - | Vessel liaison, Cape Verriden office (from 2021-11-30) |
| Yusuf Karim | - | Junior data analyst, joined 2024-03-03 |

Co-reference is deliberate: **Mari = Marisol Quintero**, **Dev = Devran Oksuz**,
**Priya Rao = Priya Anand** (maiden name). These are the entity_resolution cases.

### Buoy fleet
- Original three buoys MS-1, MS-2, MS-3 deployed 2019-09-01, 15-minute interval.
- MS-2 lost in a storm and decommissioned 2023-08-18.
- MS-7 deployed 2023-08-18 as MS-2's replacement, 5-minute interval (samples 3x
  more often than the originals).
- As of the 2024 register: **3 active buoys** (MS-1, MS-3, MS-7).
- One reading packet = 512 bytes. Firmware on all controllers written by Obi Eze.

### Data platform
- Pipeline stages: Collect -> Validate -> Normalize -> Publish.
- Ingest volume: ~2,880 readings per day.
- Original schema co-authored by Devran Oksuz and Priya Anand (as Priya Rao).
- Salinity unit: v1 = parts per thousand (ppt); v2 = practical salinity units
  (PSU). v2 supersedes v1; ppt is no longer published.

### Licence (BODL)
- BODL v1 effective 2020-06-15 (attribution, no commercial redistribution).
- BODL v2 effective 2022-04-22 (adds commercial-use permission + share-alike).
- **BODL v2 supersedes v1. There is no BODL v3.** Current licence = v2.

### Membership
- Open to vessel owners; obligation = at least one tide reading per quarter.
- No fee. 142 active member vessels as of the 2024 roster.

### Grants (administered by Hana Vesely)
| Grant | Year | Amount (credits) |
|-------|------|------------------|
| Saltmarsh Fund | 2020 | 48,000 |
| Tidewater Open Data Grant | 2021 | 65,000 |
| Verriden Expansion Grant | 2021 | 30,000 |
| Mariner Resilience Grant | 2023 | 90,000 |

- Largest single grant: **Mariner Resilience Grant, 90,000 credits** (funded MS-7).
- Total across all four: 233,000 credits.

### Role changes / supersedences (the time-sensitive spine)
1. Director: Marisol Quintero (2019 -> 2023-01-09) **superseded by** Greta Sandoval.
2. Priya Anand: data platform engineer **-> promoted to** data platform lead (2021-02-10).
3. Licence: BODL v1 **superseded by** BODL v2 (2022-04-22).
4. Buoy: MS-2 (decommissioned) **replaced by** MS-7 (2023-08-18).
5. Salinity unit: ppt (v1) **superseded by** PSU (v2, 2024-10-12).

---

## 2. Planted-fact ledger (what the gate checks)

Each planted fact has a verbatim anchor quote in a source file. These are the
gold targets for hard recall@k and extraction-completeness.

| # | Fact | Source file | Verbatim anchor (substring) |
|---|------|-------------|------------------------------|
| P1 | Founding director is Marisol Quintero | 00-charter.md | `The founding director of HTC was Marisol Quintero.` |
| P2 | HQ is Brackish Harbor | 00-charter.md | `HTC is headquartered in the town of Brackish Harbor.` |
| P3 | First three buoys deployed 2019-09-01 | 02-timeline.md | `**2019-09-01** - First three buoys deployed in the Maro Strait (buoys MS-1, MS-2, MS-3).` |
| P4 | Licence upgraded v1->v2 on 2022-04-22 | 02-timeline.md | `**2022-04-22** - Brackish Open Data Licence upgraded from v1 to v2.` |
| P5 | Director handover 2023-01-09 | 02-timeline.md | `**2023-01-09** - Marisol Quintero steps down as director; Greta Sandoval becomes the second director.` |
| P6 | 3 active buoys (2024 register) | 03-buoys.md | `As of the 2024 register, the fleet has **3 active buoys**.` |
| P7 | MS-7 samples at 5-minute interval | 03-buoys.md | `samples three times more often, at a 5-minute interval.` |
| P8 | ~2,880 readings/day | 04-data-platform.md | `The pipeline ingests roughly **2,880 readings per day** from the active fleet.` |
| P9 | Schema co-authored by Devran Oksuz + Priya Anand | 04-data-platform.md | `co-authored by Devran Oksuz and Priya` |
| P10 | Current salinity unit is PSU | 04-data-platform.md | `salinity is reported in practical salinity units (PSU).` |
| P11 | BODL v2 is current; no v3 | 05-licence.md | `The current licence covering the Tidewater Index is BODL v2. There is no BODL v3.` |
| P12 | BODL v2 supersedes v1 | 05-licence.md | `BODL v2 supersedes BODL v1.` |
| P13 | Largest grant: Mariner Resilience, 90,000 | 06-grants.md | `the Mariner Resilience Grant at` / `90,000 credits, awarded in 2023` |
| P14 | Mari = Marisol Quintero | 07-assembly-2019.md | `(Mari is Marisol Quintero; the early minutes use the short form` |
| P15 | Priya Rao -> Priya Anand | 07-assembly-2019.md | `Priya Rao` / `later publishes under the name Priya Anand.` |
| P16 | Greta Sandoval is current director | 08-handover-2023.md | `Greta Sandoval is the current director as of this record.` |
| P17 | MS-7 replaced MS-2 | 09-storm-report-2023.md | `A replacement buoy, MS-7, was deployed on 2023-08-18 to restore coverage.` |
| P18 | Grant funded MS-7 | 06-grants.md | `The Mariner Resilience Grant directly funded the deployment of replacement buoy` |
| P19 | Nadia Belmonte reports to Devran Oksuz | 11-calibration-log.md | `Nadia Belmonte reports to the buoy operations lead, Devran Oksuz.` |
| P20 | Dev = Devran Oksuz | 01-people.md | `Goes by "Dev" in field logs.` |
| P21 | 142 active member vessels | 10-membership.md | `As of the 2024 roster, HTC has **142 active member vessels**.` |
| P22 | v1 salinity was ppt | 13-schema-changelog.md | `Salinity field reported in parts per thousand (ppt).` |

---

## 3. Near-duplicate DISTRACTOR ledger

Planted similar-but-WRONG passages that a naive lexical/embedding match could
retrieve instead of the gold passage. Each is a verbatim substring; the gate's
distractor lists point at these so retrieving the distractor instead of the gold
target is a measurable miss.

| For question about... | WRONG passage (verbatim) | Source | Why it fools | Right answer |
|-----------------------|--------------------------|--------|--------------|--------------|
| Founding director | `**Greta Sandoval** - second director of HTC, from 2023.` | 01-people.md | Same "director of HTC" phrasing, wrong person | Marisol Quintero (P1) |
| Headquarters | `Cape Verriden is a satellite office, not the headquarters.` | 12-cape-verriden.md | Mentions HQ + the other location | Brackish Harbor (P2) |
| Active buoy count | `As of the 2024 roster, HTC has **142 active member vessels**.` | 10-membership.md | Same "As of the 2024 ... active" shape, different noun | 3 active buoys (P6) |
| Largest grant | `Total grant funding across all four grants is 233,000 credits.` | 06-grants.md | Bigger number, but it's a total not a single grant | 90,000 (P13) |
| Readings/day | `A single reading packet is\n512 bytes.` | 03-buoys.md | "reading" + a number, but a packet size not a daily rate | 2,880/day (P8) |
| MS-7 interval | `\| MS-1 \| 2019-09-01 \| active \| 15 minutes \|` | 03-buoys.md | Buoy table row with an interval, wrong buoy | 5 minutes (P7) |
| Current licence ver | `**BODL v1** - effective 2020-06-15.` | 05-licence.md | Talks about a BODL version, the superseded one | BODL v2 (P11/P12) |
| Current salinity unit | `Tidewater Index v1 reported salinity in parts per thousand (ppt).` | 04-data-platform.md | States a salinity unit, the retired one | PSU (P10) |
| Current director (pit) | `Marisol Quintero (founding director since 2019) stepped down effective\n2023-01-09.` | 08-handover-2023.md | Names the prior director prominently | Greta Sandoval (P16) |
| MS-2 replacement | `\| MS-2 \| 2019-09-01 \| decommissioned 2023-08-18 \| 15 minutes \|` | 03-buoys.md | The decommissioned buoy's own row | MS-7 (P17) |
| Grant that funded MS-7 | `The Verriden Expansion Grant paid for the field office where Rashid Farouk\nserves as vessel liaison.` | 06-grants.md | "Grant ... paid for ..." different target | Mariner Resilience Grant (P18) |

The sharpest supersedence trap is the salinity unit: v1=ppt and v2=PSU both live
in the corpus, so a point_in_time question (v1 era) and a supersedence question
(today) have OPPOSITE correct answers from near-identical sentences.

---

## 4. Absent-trap ledger (negative controls)

Facts that do NOT exist in the corpus. The system must decline / say "not in the
notes" rather than fabricate. Each trap is adjacent to a real passage that makes
fabrication tempting.

| Trap question | Why it's absent | Adjacent real fact (verbatim) | Source |
|---------------|-----------------|-------------------------------|--------|
| What does BODL v3 add over v2? | There is no v3 | `The current licence covering the Tidewater Index is BODL v2. There is no BODL v3.` | 05-licence.md |
| Who is the third director after Greta? | No third director exists | `There is no further\ndirectorship change after 2023.` | 08-handover-2023.md |
| What is the annual membership fee? | Membership has no fee | `Membership does not require a fee.` | 10-membership.md |

These three are the `negative_control: true` questions in questions.yaml.

---

## 5. Multi-hop wiring (cross-file references)

The multi_hop questions each require joining facts from >= 2 files:

- **Grant -> replacement buoy:** 09-storm-report-2023.md (MS-7 replaced MS-2) +
  06-grants.md (Mariner Resilience Grant funded MS-7).
- **Schema authors -> their programs:** 04-data-platform.md (co-authors) +
  01-people.md (their current lead roles).
- **Calibrator -> manager:** 11-calibration-log.md (Nadia reports to Devran) +
  01-people.md (Devran is buoy operations lead).

---

## 6. Provenance

- Author: synthetic, generated for the deterministic CI eval gate.
- Frozen: 2026-06-16.
- All names/places/events are invented for testing. Any resemblance to real
  entities is coincidental.
- Every quote in this bible and in questions.yaml is a verbatim substring of its
  cited source file (grep -F / exact-substring verified, 0 mismatches).
