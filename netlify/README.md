# Private read-only Netlify dashboard

This frontend is intentionally separate from the VM application. It can only request the allowlisted read APIs in `functions/live-data.mjs`; it contains no FoxESS control route, database connection, or secret.

## Public read-only site

In the Netlify project UI, add the runtime-only environment variables below, scoped to Functions:
   - `VM_DASHBOARD_BASE_URL` (the HTTPS VM dashboard origin)
   - `VM_DASHBOARD_USERNAME`
   - `VM_DASHBOARD_PASSWORD`

The browser receives only allowlisted GET responses. The VM remains protected by its dedicated gateway credential. Do not commit any environment variable values. Require HTTPS on the VM before setting the production base URL.
