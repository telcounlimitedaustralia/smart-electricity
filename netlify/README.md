# Private read-only Netlify dashboard

This frontend is intentionally separate from the VM application. It can only request the allowlisted read APIs in `functions/live-data.mjs`; it contains no FoxESS control route, database connection, or secret.

## Production: Google sign-in

In the Netlify project UI:

1. Enable Identity, set registration to **Invite only**, and enable the Google external provider.
2. Set `SITE_ACCESS_MODE=production`.
3. Add the runtime-only environment variables below, scoped to Functions:
   - `VM_DASHBOARD_BASE_URL` (the HTTPS VM dashboard origin)
   - `VM_DASHBOARD_USERNAME`
   - `VM_DASHBOARD_PASSWORD`
4. Add approved Google users to Identity.

Do not commit any environment variable values. Require HTTPS on the VM before setting the production base URL.
