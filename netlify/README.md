# Private read-only Netlify dashboard

This frontend is intentionally separate from the VM application. It can only request the allowlisted read APIs in `functions/live-data.mjs`; it contains no FoxESS control route, database connection, or secret.

## Production: Google sign-in

In the Netlify project UI:

1. Enable Identity and the Google external provider.
2. Set `SITE_ACCESS_MODE=production`.
3. Add the runtime-only environment variables below, scoped to Functions:
   - `VM_DASHBOARD_BASE_URL` (the HTTPS VM dashboard origin)
   - `VM_DASHBOARD_USERNAME`
   - `VM_DASHBOARD_PASSWORD`
   - `NETLIFY_IDENTITY_JWKS_URL`
   - `NETLIFY_IDENTITY_ISSUER`
   - `NETLIFY_IDENTITY_AUDIENCE`
4. Add approved Google users to Identity.

## Tester site: shared password

Create a second Netlify site from the same Git branch and set `SITE_ACCESS_MODE=tester`. Enable Netlify Password Protection for that site. It is deliberately a shared-access, lower-trust site; keep it read-only and use a strong password. Add only the first three VM environment variables above.

Do not commit any environment variable values. Require HTTPS on the VM before setting the production base URL.
