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
    return json(502, { error: "Live data is temporarily unavailable" });
  }
};
