# Development workflow

## Day-to-day change flow

1. Open this folder in VS Code.
2. Work on a focused branch, for example `fix/forecast-bias`.
3. Run **Run optimiser tests** from VS Code's Terminal > Run Task menu.
4. Review the dashboard locally with the `Dashboard (local)` debug launch configuration.
5. Commit and push the branch; merge only reviewed changes into `main`.
6. Deploy to the VM through a documented release step after tests and backup verification. Do not edit production as the primary development workflow.

## Local runtime

Create `.venv` with Python 3.12 and install `requirements/runtime.txt`. Choose the `.venv` interpreter when VS Code asks. The project settings then use it for tests and debugging.

## Production boundary

`main` is the source of truth. The Google Cloud VM is a deployment target, not the development workspace. It retains secrets, the live database, model artifacts, and FoxESS control credentials; these must not be copied into Git or a browser-hosted frontend.
