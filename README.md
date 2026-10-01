# Smart Electricity

Forecasting, battery planning, and FoxESS export control for a residential solar-and-battery system.

## Safety status

- **Rule-based premium export is the live control path.** It snapshots at 16:55 and executes at 17:00.
- **Joint economic optimiser is shadow-only.** Both its cheap-charge and export cron entries are paused.
- No code change may enable a FoxESS write path without an explicit reviewed decision and recorded rollback plan.

## Local development

1. Create a virtual environment with Python 3.12.
2. Install `requirements/runtime.txt`.
3. Copy `.env.example` to `.env` and `config/telegram.env.example` to `config/telegram.env` only on a trusted runtime machine.
4. Run tests with `PYTHONPATH=src python -m unittest discover -s tests`.

Production data, models, logs, secrets, virtual environments, and backups are intentionally excluded from Git. See `docs/` for the deployment and data-handling plan.
