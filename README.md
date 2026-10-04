# Smart Electricity

Forecasting, battery planning, and FoxESS export control for a residential solar-and-battery system.

## Safety status

- **The guarded joint economic optimiser is the live control path.** It applies
  verified 10:05-13:55 import and 17:05-20:55 export decisions, with audited
  dashboard switches and Telegram confirmation.
- **The legacy rule executor remains installed for rollback but is write-blocked**
  while `FOXESS_CONTROL_MODE=joint`.
- No code change may enable a FoxESS write path without an explicit reviewed decision and recorded rollback plan.

## Local development

1. Create a virtual environment with Python 3.12.
2. Install `requirements/runtime.txt`.
3. Copy `.env.example` to `.env` and `config/telegram.env.example` to `config/telegram.env` only on a trusted runtime machine.
4. Run tests with `PYTHONPATH=src python -m unittest discover -s tests`.

Production data, models, logs, secrets, virtual environments, and backups are intentionally excluded from Git. See `docs/` for the deployment and data-handling plan.
