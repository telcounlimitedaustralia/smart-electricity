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
  const assumptions = optimiser.assumptions || {};
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
  document.querySelector("#assumptions").innerHTML = `<strong>Planning assumptions:</strong> ${num(assumptions.charge_efficiency_percent, 1)}% charge efficiency, ${num(assumptions.discharge_efficiency_percent, 1)}% discharge efficiency, ${num(assumptions.degradation_cents_per_battery_kwh, 1)}c battery-wear allowance per battery kWh, ${num(assumptions.solar_protection_percent, 0)}% protected solar, and ${num(assumptions.terminal_energy_value_cents_per_kwh, 1)}c/kWh retained-energy value.`;
}

function renderMl(performance) {
  const target = document.querySelector("#ml-performance");
  if (!performance.available) {
    target.innerHTML = `<p class="empty">${esc(performance.reason || "No validated forecast days yet.")}</p>`;
    return;
  }
  const metrics = performance.metrics || {};
  const latest = [...(performance.daily || [])].slice(-7).reverse();
  const metricCard = (label, metric, note) => {
    const coverage = `${metric?.comparable_days || 0}/${performance.validated_days} days`;
    const value = metric?.mae == null
      ? "Collecting"
      : metric.unit === "$" ? money(metric.mae) : kwh(metric.mae, 2);
    const bias = metric?.bias == null
      ? note
      : `${Number(metric.bias) < 0 ? "forecast low" : "forecast high"} by ${metric.unit === "$" ? money(Math.abs(metric.bias)) : kwh(Math.abs(metric.bias), 2)} avg`;
    return `<article><span>${label}</span><strong>${value}</strong><small>average error · ${coverage}</small><small>${bias}</small></article>`;
  };
  const pair = (forecast, actual, formatter = value => kwh(value)) => stack(
    `F ${formatter(forecast)}`,
    `A ${formatter(actual)}`
  );
  const errorText = (value, formatter = number => kwh(number)) => value == null
    ? "not recorded"
    : `${Number(value) > 0 ? "+" : ""}${formatter(value)}`;
  target.innerHTML = `
    <div class="audit-status"><span class="evidence-pill">${esc(performance.evidence_label)}</span><strong>${performance.validated_days} completed days</strong><span>Trend judgement starts after ${performance.data_quality?.minimum_days_for_trend || 7} days.</span></div>
    <div class="audit-cards">
      ${metricCard("Solar", metrics.solar, "gross PV")}
      ${metricCard("Home use", metrics.home_use, "ML point forecast")}
      ${metricCard("5–9 PM export", metrics.premium_export, "same premium window")}
      ${metricCard("Grid imports", metrics.grid_import, "new snapshots will add forecasts")}
      ${metricCard("5–9 PM revenue", metrics.premium_revenue, "same 28c window")}
    </div>
    <div class="scroll audit-scroll"><table class="audit-table"><thead><tr>
      <th>Date</th>
      <th>Solar<br><small>forecast / actual</small></th>
      <th>Home use<br><small>forecast / actual</small></th>
      <th>5–9 PM export<br><small>planned / actual</small></th>
      <th>Grid imports<br><small>forecast / actual</small></th>
      <th>5–9 PM revenue<br><small>forecast / actual</small></th>
      <th>Actual all-day result<br><small>export / revenue / net</small></th>
    </tr></thead><tbody>
      ${latest.map(row => `<tr>
        <td><strong>${esc(row.date)}</strong><small class="coverage-note">${num(row.premium_window_coverage_hours, 1)}h premium data</small></td>
        <td>${pair(row.solar_forecast_kwh, row.solar_actual_kwh)}<small class="audit-error">Error ${errorText(row.solar_error_kwh)}</small></td>
        <td>${pair(row.load_forecast_kwh, row.load_actual_kwh)}<small class="audit-error">Error ${errorText(row.load_error_kwh)}</small><small class="coverage-note">Protected ${kwh(row.protected_load_kwh)}</small></td>
        <td>${pair(row.premium_export_forecast_kwh, row.premium_export_actual_kwh)}<small class="audit-error">Error ${errorText(row.premium_export_error_kwh)}</small></td>
        <td>${pair(row.grid_import_forecast_kwh, row.grid_import_actual_kwh)}<small class="audit-error">Error ${row.grid_import_forecast_kwh == null ? "not recorded" : errorText(row.grid_import_forecast_kwh - row.grid_import_actual_kwh)}</small></td>
        <td>${pair(row.export_revenue_forecast, row.premium_revenue_actual, money)}<small class="audit-error">Error ${row.export_revenue_forecast == null || row.premium_revenue_actual == null ? "not recorded" : errorText(row.export_revenue_forecast - row.premium_revenue_actual, money)}</small></td>
        <td>${stack(kwh(row.total_export_actual_kwh), `${money(row.total_export_revenue_actual)} revenue · ${money(row.net_value_actual)} net`)}</td>
      </tr>`).join("")}
    </tbody></table></div>
    <div class="audit-notes">
      <strong>How to read this:</strong> F = forecast, A = actual. ${esc(performance.comparison_definition)}
      <span>${esc(performance.data_quality?.import_forecast_note || "")}</span>
    </div>
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
