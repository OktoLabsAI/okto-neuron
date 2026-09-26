# Tidewater Index Schema Changelog

This changelog records every published version of the Tidewater Index schema.

## v1 - published 2020-06-15

- Salinity field reported in parts per thousand (ppt).
- Tide height reported in centimeters.
- Licensed under Brackish Open Data Licence v1.
- Schema co-authored by Devran Oksuz and Priya Rao (now Priya Anand).

## v2 - published 2024-10-12

- Salinity field changed to practical salinity units (PSU). This supersedes the
  v1 ppt field; ppt is no longer published.
- Tide height still reported in centimeters (unchanged).
- Licensed under Brackish Open Data Licence v2.

The current published schema is v2. Anyone consuming salinity from the live
Tidewater Index should read it as PSU, not ppt. The ppt unit only ever applied
to the retired v1 schema.
