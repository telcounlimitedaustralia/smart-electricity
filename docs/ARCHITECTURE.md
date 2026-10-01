# Architecture

## Current production flow

FoxESS collector, weather collection, feature generation, forecasting, planning, dashboard rendering, and FoxESS execution run on one Google Cloud VM. Nginx exposes the Flask dashboard while Flask listens only on `127.0.0.1:8080`.

The live database is SQLite (`data/energy.db`). The ML model artifacts are local Joblib files under `models/`. These are runtime assets, not Git source.

## Target layout

- `src/`: backend, forecasting, planners, and device-control adapters.
- `dashboard/`: current server-rendered dashboard, to be separated into a frontend later.
- `tests/`: executable regression and acceptance tests.
- `config/`: tracked templates only; live credentials remain outside Git.
- `deployment/`: nginx, systemd, cron migration, and backup definitions.
- `docs/`: architecture, operations, data, models, and APIs.

## Target hosted dashboard

Netlify should host only a static/read-only frontend. The VM API must remain behind HTTPS, authentication, rate limiting, and a narrowly scoped CORS policy for the Netlify domain. It must never expose FoxESS credentials or a write-control endpoint to the browser.
