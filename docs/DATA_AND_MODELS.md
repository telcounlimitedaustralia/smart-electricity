# Data and model handling

## Runtime data

`data/energy.db` is the live SQLite store. It contains FoxESS history/live telemetry, weather, tariff, ML features/training data, forecast vintages, planner actions, and automation events. It is excluded from Git and must be backed up separately.

## Models

The production model directory contains `solar_candidate_v1/v2.joblib` and `load_candidate_v1/v2.joblib`. Model binaries are excluded from Git. Each released model should later be recorded in a model registry entry with training period, feature set, validation metrics, artifact checksum, and promotion decision.

## Historical copies

Files bearing `.before_*`, `.bak`, `.tmp`, or similar suffixes are production-era recovery copies. They are ignored by Git and must be reviewed before archival or deletion; they are not automatically treated as authoritative source.
