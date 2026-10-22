# Project Instructions

- This repository is a Home Assistant custom integration for Zonneplan Nexus using the modified Solis S6-EH3P10K-H-ZP inverter.
- Treat battery writes as safety-critical. Never enable control by default, guess entity IDs, or assume units/ranges; verify them against the user's actual Home Assistant entities and tests.
- Keep the integration compatible with Home Assistant patterns and test all dispatch logic offline before any live-control option is introduced.
- The first scaffold is read-only and exposes configured input sensor values for diagnostics.