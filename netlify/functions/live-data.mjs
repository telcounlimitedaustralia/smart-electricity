const ALLOWED_VIEWS = new Set([
  "status",
  "today",
  "history",
  "tomorrow",
  "export-plan",
  "strategy-validation",
  "ml-performance",
  "economic-optimizer",
]);

function json(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: {
      "content-type": "application/json; charset=utf-8",
      "cache-control": "no-store",
    },
  });
}

function basicAuth(username, password) {
  return `Basic ${Buffer.from(`${username}:${password}`).toString("base64")}`;
}

async function requireIdentity(request) {
  // The tester deployment is protected by Netlify Password Protection at the
  // edge. The production deployment has an explicit Identity JWT requirement.
  if (process.env.SITE_ACCESS_MODE === "tester") return null;

  const token = request.headers.get("authorization")?.replace(/^Bearer\s+/i, "");
  if (!token) throw new Error("Sign in is required.");

  const jwksUrl = process.env.NETLIFY_IDENTITY_JWKS_URL;
  if (!jwksUrl) throw new Error("Identity verification is not configured.");

  const { createRemoteJWKSet, jwtVerify } = await import("jose");
  const result = await jwtVerify(token, createRemoteJWKSet(new URL(jwksUrl)), {
    issuer: process.env.NETLIFY_IDENTITY_ISSUER,
    audience: process.env.NETLIFY_IDENTITY_AUDIENCE,
  });
  return result.payload;
}

export default async (request) => {
  if (request.method !== "GET") return json(405, { error: "Read-only endpoint" });

  const view = new URL(request.url).searchParams.get("view");
  // This contains no secret. It lets the same repository serve a Google
  // Identity production site and a password-protected tester site cleanly.
  if (view === "config") {
    return json(200, { accessMode: process.env.SITE_ACCESS_MODE || "production" });
  }
  if (!ALLOWED_VIEWS.has(view)) return json(404, { error: "Unknown read-only view" });

  try {
    await requireIdentity(request);

    const baseUrl = process.env.VM_DASHBOARD_BASE_URL;
    const username = process.env.VM_DASHBOARD_USERNAME;
    const password = process.env.VM_DASHBOARD_PASSWORD;
    if (!baseUrl || !username || !password) {
      return json(503, { error: "Dashboard gateway is not configured" });
    }

    const upstream = new URL(`/api/${view}`, baseUrl);
    const response = await fetch(upstream, {
      headers: { authorization: basicAuth(username, password) },
      signal: AbortSignal.timeout(15000),
    });
    const text = await response.text();

    return new Response(text, {
      status: response.status,
      headers: {
        "content-type": response.headers.get("content-type") || "application/json; charset=utf-8",
        "cache-control": "no-store",
      },
    });
  } catch (error) {
    const isAuthError = /sign in|required|Identity verification/i.test(error.message);
    return json(isAuthError ? 401 : 502, { error: isAuthError ? error.message : "Live data is temporarily unavailable" });
  }
};
