const api = (view) => fetch(`/.netlify/functions/live-data?view=${encodeURIComponent(view)}`)
  .then(async response => {
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Unable to load live data");
    return data;
  });

const esc = value => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");
const num = (value, digits = 1) => value == null ? "—" : Number(value).toFixed(digits);
const kwh = (value, digits = 1) => value == null ? "—" : `${num(value, digits)} kWh`;
const pct = (value, digits = 0) => value == null ? "—" : `${num(value, digits)}%`;
const money = value => value == null ? "—" : `${Number(value) < 0 ? "−" : ""}$${Math.abs(Number(value)).toFixed(2)}`;
const stack = (primary, secondary = "") => `<span class="metric"><strong>${primary}</strong>${secondary ? `<small>${secondary}</small>` : ""}</span>`;
const battery = (kwhValue, percentValue) => stack(pct(percentValue, 1), kwh(kwhValue, 1));

function showLocalPreview() {
  document.querySelector("#updated").textContent = "Local layout preview — live data loads after Netlify deployment.";
  document.querySelector("#access").textContent = "Local preview";
  document.querySelector("#current").innerHTML = ["Solar now", "Home now", "Battery", "Today’s solar", "Today’s home use", "Grid import"]
    .map(label => `<article><span>${label}</span><strong>—</strong></article>`).join("");
  document.querySelector("#decision").textContent = "This local preview has no connection to your live energy data.";
  document.querySelector("#economics").innerHTML = "";
  document.querySelector("#plan").innerHTML = `<tr><td colspan="19">Live seven-day data will appear here after deployment.</td></tr>`;
  document.querySelector("#ml-performance").textContent = "Live forecast validation appears after deployment.";
}

function renderCurrent(status, today, capacity) {
  const fox = status.foxess || {};
  const soc = Number(status.battery?.soc || 0);
  const items = [
    ["Solar now", `${num(fox.pv_kw || 0, 2)} kW`, "live generation"],
    ["Home now", `${num(fox.load_kw || 0, 2)} kW`, "live consumption"],
    ["Battery", pct(soc, 1), kwh(capacity * soc / 100, 1)],
    ["Today’s solar", kwh(today.pv_kwh), "gross PV produced"],
    ["Today’s home use", kwh(today.load_kwh), "household consumption"],
    ["Grid import", kwh(today.import_kwh), "meter import"],
  ];
  document.querySelector("#current").innerHTML = items.map(([label, value, detail]) => `
    <article><span>${label}</span><strong>${value}</strong><small>${detail}</small></article>
  `).join("");
}

function renderDecision(optimiser) {
  const day = optimiser.days?.[0];
  if (!day) {
    document.querySelector("#decision").textContent = optimiser.error || "No plan is currently available.";
    return;
  }
  document.querySelector("#decision").innerHTML = `
    <div class="decision-grid">
      <div><span>Cheap energy bought</span><strong>${kwh(day.actual_simulated_charge_kwh)}</strong><small>10 AM–2 PM · shadow only</small></div>
      <div><span>Premium export</span><strong>${kwh(day.simulated_premium_export_kwh)}</strong><small>5 PM–9 PM · shadow only</small></div>
      <div><span>Battery at 5 PM</span><strong>${pct(day.soc_5pm, 1)}</strong><small>${kwh(day.battery_5pm_kwh)}</small></div>
      <div><span>Battery at midnight</span><strong>${pct(day.end_soc, 1)}</strong><small>${kwh(day.battery_end_kwh)}</small></div>
    </div>
    <p class="reason"><strong>Why:</strong> ${esc(day.reason)}</p>
  `;
}

function renderEconomics(optimiser) {
  const automation = optimiser.automation || {};
  const cards = [
    ["Joint plan cash value", money(optimiser.optimised_value), "earnings − imports − battery wear"],
    ["Without cheap charging", money(optimiser.baseline_value), "fair export-only comparison"],
    ["Cash improvement", money(optimiser.cash_improvement), "same seven-day forecast"],
    ["Horizon-adjusted improvement", money(optimiser.projected_improvement), "includes retained end energy"],
    ["Live automation", "Rule-based", automation.cheap_charge_enabled ? "cheap charge on" : "cheap charge off · export on"],
  ];
  document.querySelector("#economics").innerHTML = cards.map(([label, value, detail]) => `
    <article><span>${label}</span><strong>${value}</strong><small>${detail}</small></article>
  `).join("");
}

function renderPlan(optimiser) {
  const days = optimiser.days || [];
  document.querySelector("#plan").innerHTML = days.map((day, index) => `
    <tr class="${index === 0 ? "today-row" : ""}">
      <td class="date-cell"><strong>${index === 0 ? "Today · " : ""}${esc(day.date)}</strong><small>${esc(day.solar_basis || "")}</small></td>
      <td>${stack(kwh(day.solar_kwh), `protected ${kwh(day.protected_solar_kwh)}`)}</td>
      <td>${stack(kwh(day.ml_load_kwh), "ML point")}</td>
      <td>${stack(kwh(day.safe_ml_load_kwh), `+${num(day.load_buffer_kwh, 1)} kWh protection`)}</td>
      <td>${battery(day.battery_start_kwh, day.battery_start_soc)}</td>
      <td>${stack(kwh(day.baseline_export_kwh), "comparison only")}</td>
      <td>${stack(kwh(day.actual_simulated_charge_kwh), "meter-side AC")}</td>
      <td>${battery(day.stored_from_grid_kwh, day.stored_from_grid_pct)}</td>
      <td>${battery(day.battery_5pm_kwh, day.soc_5pm)}</td>
      <td>${stack(kwh(day.simulated_premium_export_kwh), "meter-side AC")}</td>
      <td>${battery(day.battery_used_for_export_kwh, day.battery_used_for_export_pct)}</td>
      <td>${battery(day.battery_end_kwh, day.end_soc)}</td>
      <td>${stack(kwh(day.grid_import_kwh), `${kwh(day.household_import_kwh)} home`)}</td>
      <td>${stack(money(day.import_cost), "all imports")}</td>
      <td>${stack(money(day.export_revenue), "all exports")}</td>
      <td>${stack(money(day.degradation_cost), "planning allowance")}</td>
      <td>${stack(money(day.net_value), "daily cash")}</td>
      <td class="${Number(day.daily_improvement) >= 0 ? "positive" : "negative"}">${stack(money(day.daily_improvement), "vs fair baseline")}</td>
      <td class="reason-cell"><span class="confidence ${String(day.confidence || "").toLowerCase()}">${esc(day.confidence)}</span>${esc(day.reason)}</td>
    </tr>
  `).join("");
}

function renderMl(performance) {
  const target = document.querySelector("#ml-performance");
  if (!performance.available) {
    target.innerHTML = `<p class="empty">${esc(performance.reason || "No validated forecast days yet.")}</p>`;
    return;
  }
  const accuracy = Number(performance.ml_mae_kwh) <= 1 ? "High" : Number(performance.ml_mae_kwh) <= 3 ? "Moderate" : "Low";
  const latest = [...(performance.daily || [])].slice(-7).reverse();
  target.innerHTML = `
    <div class="audit-cards">
      <article><span>Validated days</span><strong>${performance.validated_days}</strong><small>forecast vs actual</small></article>
      <article><span>ML average error</span><strong>${kwh(performance.ml_mae_kwh, 2)}</strong><small class="${accuracy.toLowerCase()}">${accuracy} accuracy</small></article>
      <article><span>Protected-plan error</span><strong>${kwh(performance.safe_ml_mae_kwh, 2)}</strong><small>includes safety buffer</small></article>
      <article><span>ML bias</span><strong>${kwh(performance.ml_bias_kwh, 2)}</strong><small>${Number(performance.ml_bias_kwh) < 0 ? "usually predicts low" : "usually predicts high"}</small></article>
    </div>
    <div class="scroll audit-scroll"><table class="audit-table"><thead><tr><th>Date</th><th>Actual home use</th><th>ML forecast</th><th>Protected forecast</th><th>ML error</th><th>Better forecast</th></tr></thead><tbody>
      ${latest.map(row => `<tr><td>${esc(row.date)}</td><td>${kwh(row.actual_kwh)}</td><td>${kwh(row.ml_kwh)}</td><td>${kwh(row.safe_ml_kwh)}</td><td>${kwh(row.ml_error_kwh)}</td><td>${esc(row.winner)}</td></tr>`).join("")}
    </tbody></table></div>
  `;
}

async function load() {
  document.querySelector("#message").textContent = "";
  document.querySelector("#refresh").disabled = true;
  try {
    const [status, today, optimiser, performance] = await Promise.all([
      api("status"), api("today"), api("economic-optimizer"), api("ml-performance"),
    ]);
    document.querySelector("#updated").textContent = `Updated ${new Date(status.time).toLocaleString()}`;
    const capacity = Number(optimiser.battery_capacity_kwh || 42);
    renderCurrent(status, today, capacity);
    renderDecision(optimiser);
    renderEconomics(optimiser);
    renderPlan(optimiser);
    renderMl(performance);
  } catch (error) {
    document.querySelector("#message").textContent = error.message;
  } finally {
    document.querySelector("#refresh").disabled = false;
  }
}

function ready() {
  if (location.protocol === "file:") {
    showLocalPreview();
    document.querySelector("#refresh").disabled = true;
    return;
  }
  document.querySelector("#access").textContent = "Public · read-only";
  load();
}

document.querySelector("#refresh").onclick = load;
window.addEventListener("load", ready);
