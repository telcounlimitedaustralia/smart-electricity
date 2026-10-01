# Operations and rollback

## Current schedules

| Time | Job | Control state |
|---|---|---|
| Every 5 minutes | FoxESS collection and feature refresh | Active |
| Hourly | Weather refresh | Active |
| Daily 08:05–09:35 | Backfill, training data, forecast and economic snapshots | Active |
| Sunday 08:30 | Load-model v2 retraining | Active |
| 16:55 | Rule-based strategy snapshot | Active |
| 17:00 | Rule-based FoxESS premium export executor | Active |
| 09:55 / 16:55 | Joint optimiser cheap-charge / export executor | Paused |

## Backup baseline

Before migration, a compressed backup was written to `/home/kaji_islam/backups/smart-electricity-pre-migration-20261001.tar.gz`, SHA-256 `f7879ed4704a31ebdb538f6c4c36e6a547d297ef1117106819959b7d657227c3`.

## Rollback

1. Stop the replacement service or cron entry.
2. Restore the previous known-good source and deployment configuration from the backup.
3. Restore the backed-up SQLite database only when a data rollback is intended.
4. Verify dashboard health locally on port 8080, then through nginx.
5. Confirm the FoxESS schedule by readback before enabling any control job.
