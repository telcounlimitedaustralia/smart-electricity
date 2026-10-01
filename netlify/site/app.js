const api = (view) => fetch(`/.netlify/functions/live-data?view=${encodeURIComponent(view)}`, {
  headers: window.netlifyIdentity?.currentUser()?.token?.access_token
    ? { Authorization: `Bearer ${window.netlifyIdentity.currentUser().token.access_token}` }
    : {},
}).then(async r => { const data = await r.json(); if (!r.ok) throw new Error(data.error || "Unable to load live data"); return data; });

const kwh = value => value == null ? "—" : `${Number(value).toFixed(1)} kWh`;
const pct = value => value == null ? "—" : `${Number(value).toFixed(0)}%`;

function showLocalPreview() {
  document.querySelector("#updated").textContent = "Local layout preview — live data loads after Netlify deployment.";
  document.querySelector("#access").textContent = "Local preview";
  document.querySelector("#current").innerHTML = ["Solar now", "Home now", "Battery", "Today’s solar", "Today’s home use", "Grid import"]
    .map(label => `<article><span>${label}</span><strong>—</strong></article>`).join("");
  document.querySelector("#decision").textContent = "This local preview has no connection to your live energy data.";
  document.querySelector("#plan").innerHTML = `<tr><td colspan="8">Live seven-day data will appear here after the protected Netlify site is deployed.</td></tr>`;
}

async function load() {
  document.querySelector("#message").textContent = "";
  try {
    const [status, today, optimiser] = await Promise.all([api("status"), api("today"), api("economic-optimizer")]);
    const fox = status.foxess;
    document.querySelector("#updated").textContent = `Updated ${new Date(status.time).toLocaleString()}`;
    document.querySelector("#current").innerHTML = [
      ["Solar now", `${Number(fox.pv_kw || 0).toFixed(2)} kW`], ["Home now", `${Number(fox.load_kw || 0).toFixed(2)} kW`],
      ["Battery", pct(status.battery.soc)], ["Today’s solar", kwh(today.pv_kwh)], ["Today’s home use", kwh(today.load_kwh)], ["Grid import", kwh(today.import_kwh)],
    ].map(([label, value]) => `<article><span>${label}</span><strong>${value}</strong></article>`).join("");
    const first = optimiser.days?.[0];
    document.querySelector("#decision").textContent = first?.reason || optimiser.error || "No plan is currently available.";
    document.querySelector("#plan").innerHTML = (optimiser.days || []).map(day => `<tr><td>${day.date}</td><td>${kwh(day.solar_kwh)}</td><td>${kwh(day.ml_load_kwh)}</td><td>${kwh(day.recommended_charge_kwh)}</td><td>${kwh(day.recommended_export_kwh)}</td><td>${pct(day.soc_5pm)} → ${pct(day.end_soc)}</td><td>${kwh(day.grid_import_kwh)}</td><td>$${Number(day.net_value || 0).toFixed(2)}</td></tr>`).join("");
  } catch (error) { document.querySelector("#message").textContent = error.message; }
}

async function identityReady() {
  const login = document.querySelector("#login");
  if (location.protocol === "file:") {
    showLocalPreview();
    document.querySelector("#refresh").disabled = true;
    document.querySelector("#refresh").title = "Live data is available after Netlify deployment.";
    return;
  }
  let config;
  try { config = await fetch("/.netlify/functions/live-data?view=config").then(r => r.json()); } catch (_) { config = { accessMode: "production" }; }
  if (config.accessMode === "tester") {
    document.querySelector("#access").textContent = "Tester access · read-only";
    load();
    return;
  }
  if (!window.netlifyIdentity) { document.querySelector("#message").textContent = "Google sign-in is not configured yet."; return; }
  window.netlifyIdentity.on("init", user => { login.hidden = Boolean(user); document.querySelector("#access").textContent = user ? user.email : "Google sign-in required"; if (user) load(); });
  window.netlifyIdentity.on("login", () => { window.netlifyIdentity.close(); load(); });
  window.netlifyIdentity.init();
  login.onclick = () => window.netlifyIdentity.open();
}

document.querySelector("#refresh").onclick = load;
window.addEventListener("load", identityReady);
