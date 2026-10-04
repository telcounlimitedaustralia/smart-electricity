# Operations and rollback

## Current schedules

| Time | Job | Control state |
|---|---|---|
| Every 5 minutes | FoxESS collection and feature refresh | Active |
| Hourly | Weather refresh | Active |
| Daily 08:05–09:35 | Backfill, training data, forecast and economic snapshots | Active |
| Sunday 08:30 | Load-model v2 retraining | Active |
| 16:55 | Rule-based strategy snapshot | Installed for rollback |
| 17:00 | Rule-based FoxESS premium export executor | Write-blocked by joint mode |
| 09:55 / 16:55 | Joint optimiser cheap-charge / export executor | Active, guarded |

## Guarded joint-controller rollout

The production sequence is defined in `deployment/foxess-joint-controller.cron`:

| Time | Action |
|---|---|
| 09:45 | Freeze a fresh optimiser plan |
| 09:55 | Apply or clear the 10:05-13:55 ForceCharge period; notify Telegram after verified read-back |
| 14:00 | Reconcile the forecast after the shoulder window; no control write |
| 16:45 | Freeze a fresh plan using current SOC and forecast |
| 16:55 | Apply or clear the 17:05-20:55 ForceDischarge period; notify Telegram after verified read-back |
| 21:00 | Remove controller-owned periods and verify the remaining schedule |

Production writes require `FOXESS_CONTROL_MODE=joint`. In that mode the legacy
rule executor exits successfully without writing, even if an old cron entry is
still present. Joint execution refuses a plan older than 20 minutes or an SOC
reading older than 10 minutes, preserves unrelated schedule groups, verifies
every write, and restores the original schedule if verification fails.

Activation is deliberately fail-closed. From the VM, run
`bash deployment/activate-joint-controller.sh`. It refuses tracked local
changes, updates to the reviewed branch, runs the full tests, backs up the
crontab and environment, creates a fresh plan, performs two no-write FoxESS
dry runs, switches the exclusive controller mode, and installs the marked cron
block. Use `bash deployment/rollback-joint-controller.sh` to clear owned
schedule periods, restore rule ownership, and remove that cron block.

The authenticated VM dashboard exposes three audited operator switches: master,
10:05-13:55 charge, and 17:05-20:55 export. OFF is immediate: the database gate
is changed first, then the matching FoxESS schedule is removed and independently
read back. ON permits only the next fresh scheduled decision; it never replays an
old plan. The public Netlify dashboard remains read-only and has no control route.

## Backup baseline

Before migration, a compressed backup was written to `/home/kaji_islam/backups/smart-electricity-pre-migration-20261001.tar.gz`, SHA-256 `f7879ed4704a31ebdb538f6c4c36e6a547d297ef1117106819959b7d657227c3`.

## Rollback

1. Stop the replacement service or cron entry.
2. Restore the previous known-good source and deployment configuration from the backup.
3. Restore the backed-up SQLite database only when a data rollback is intended.
4. Verify dashboard health locally on port 8080, then through nginx.
5. Confirm the FoxESS schedule by readback before enabling any control job.
