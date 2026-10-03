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
const friendlyDate = value => {
  const date = new Date(`${value}T12:00:00`);
  return Number.isNaN(date.getTime())
    ? String(value || "")
    : date.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
};
const friendlyBasis = value => {
  const known = {
    ACTUAL_SO_FAR_PLUS_REMAINING_FORECAST: "Actual so far + remaining forecast",
    WEATHER_FORECAST: "Full-day weather forecast",
    FULL_DAY_FORECAST: "Full-day forecast",
  };
  const raw = String(value || "");
  return known[raw] || raw.replaceAll("_", " ").toLowerCase().replace(/^./, letter => letter.toUpperCase());
};

function showLocalPreview() {
  document.querySelector("#updated").textContent = "Local layout preview — live data loads after Netlify deployment.";
  document.querySelector("#access").textContent = "Local preview";
  document.querySelector("#current").innerHTML = ["Solar now", "Home now", "Battery", "Today’s solar", "Today’s home use", "Grid import"]
    .map(label => `<article><span>${label}</span><strong>—</strong></article>`).join("");
  document.querySelector("#decision").textContent = "This local preview has no connection to your live energy data.";
  document.querySelector("#economics").innerHTML = "";
  document.querySelector("#plan").innerHTML = `<tr><td colspan="20">Live seven-day data will appear here after deployment.</td></tr>`;
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
  const observedCharge = Number(day.observed_grid_charge_kwh || 0);
  const plannedCharge = Number(day.actual_simulated_charge_kwh || 0);
  const observedCard = observedCharge >= 0.05
    ? `<div><span>Grid charge already observed</span><strong>${kwh(observedCharge)}</strong><small>${kwh(day.observed_grid_stored_kwh)} stored · actual today</small></div>`
    : "";
  document.querySelector("#decision").innerHTML = `
    <div class="decision-grid">
      ${observedCard}
      <div><span>${observedCharge >= 0.05 ? "Additional cheap charge" : "Cheap energy to buy"}</span><strong>${kwh(plannedCharge)}</strong><small>remaining shadow recommendation</small></div>
      <div><span>Premium export</span><strong>${kwh(day.simulated_premium_export_kwh)}</strong><small>5 PM–9 PM · shadow only</small></div>
      <div><span>Battery at 5 PM</span><strong>${pct(day.soc_5pm, 1)}</strong><small>${kwh(day.battery_5pm_kwh)}</small></div>
      <div><span>Battery at 5 PM without today’s grid charge</span><strong>${pct(day.solar_only_5pm_soc, 1)}</strong><small>${kwh(day.solar_only_5pm_kwh)} · counterfactual</small></div>
      <div><span>Required after 9 PM</span><strong>${pct(day.required_reserve_soc, 1)}</strong><small>${kwh(day.required_reserve_kwh)} until recharge</small></div>
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

function renderPlan(optimiser, rulePlan) {
  const days = optimiser.days || [];
  const assumptions = optimiser.assumptions || {};
  const premiumRate = Number(optimiser.tariffs?.premium_fit_cents || 28);
  const ruleByDate = new Map((rulePlan.plans || []).map(day => [day.date, day]));
  document.querySelector("#plan").innerHTML = days.map((day, index) => `
    ${(() => {
      const old = ruleByDate.get(day.date) || {};
      const oldExport = Number(old.recommended_export_kwh ?? day.baseline_export_kwh ?? 0);
      const oldRevenue = Number(old.potential_revenue ?? (oldExport * premiumRate / 100));
      const newExport = Number(day.simulated_premium_export_kwh ?? day.recommended_export_kwh ?? 0);
      const newRevenue = newExport * premiumRate / 100;
      const uplift = newRevenue - oldRevenue;
      const charge = Number(day.actual_simulated_charge_kwh ?? day.recommended_charge_kwh ?? 0);
      const observedCharge = Number(day.observed_grid_charge_kwh || 0);
      const totalCharge = charge + observedCharge;
      const chargeDisplay = observedCharge >= 0.05
        ? `${kwh(observedCharge)} actual + ${kwh(charge)} remaining`
        : kwh(charge);
      const chargeDetail = observedCharge >= 0.05
        ? `${kwh(day.observed_grid_stored_kwh)} actually stored from grid`
        : `${kwh(day.stored_from_grid_kwh)} planned stored`;
      const decision = totalCharge > 0 && newExport > 0
        ? "BUY + EXPORT"
        : totalCharge > 0 ? "BUY + HOLD" : newExport > 0 ? "SOLAR EXPORT" : "HOLD";
      const recharge = day.next_recharge_timestamp
        ? new Date(day.next_recharge_timestamp).toLocaleString(undefined, {
            weekday: "short", hour: "numeric", minute: "2-digit"
          })
        : "Forecast end";
      const rechargeType = String(day.next_recharge_type || "FORECAST_HORIZON")
        .replaceAll("_", " ").toLowerCase();
      return `<tr class="${index === 0 ? "today-row" : ""}">
        <td class="date-cell"><strong>${index === 0 ? "Today · " : ""}${esc(friendlyDate(day.date))}</strong><small>${esc(friendlyBasis(day.solar_basis))}</small></td>
        <td class="cell-forecast">${stack(kwh(day.solar_kwh), `protected ${kwh(day.protected_solar_kwh)}`)}</td>
        <td class="cell-forecast">${stack(esc(old.solar_rating || "—"), esc(day.confidence || "—"))}</td>
        <td class="cell-rule">${stack(kwh(oldExport), old.export === "NO" ? "retain battery" : "rule plan")}</td>
        <td class="cell-rule">${stack(money(oldRevenue), "premium only")}</td>
        <td class="cell-shadow">${stack(chargeDisplay, chargeDetail)}</td>
        <td class="cell-shadow">${stack(money(day.import_cost), observedCharge >= 0.05 ? "observed + remaining plan" : "all planned imports")}</td>
        <td class="cell-shadow">${stack(kwh(newExport), decision)}</td>
        <td class="cell-shadow">${stack(pct(day.battery_start_soc, 1), index === 0 ? `${kwh(day.battery_start_kwh)} · live calculation start` : `${kwh(day.battery_start_kwh)} · simulated day start`)}</td>
        <td class="cell-shadow">${stack(pct(day.solar_only_5pm_soc, 1), `${kwh(day.solar_only_5pm_kwh)} · excludes observed grid charge`)}</td>
        <td class="cell-shadow">${battery(day.battery_5pm_kwh, day.soc_5pm)}</td>
        <td class="cell-shadow">${battery(day.required_reserve_kwh, day.required_reserve_soc)}</td>
        <td class="cell-shadow">${stack(esc(recharge), esc(rechargeType))}</td>
        <td class="cell-shadow">${battery(day.battery_end_kwh, day.end_soc)}</td>
        <td class="cell-shadow ${Number(day.pre_10am_import_kwh || 0) <= 0.05 ? "positive" : "negative"}">${stack(kwh(day.pre_10am_import_kwh), "forecast")}</td>
        <td class="cell-shadow">${battery(Number(optimiser.battery_capacity_kwh || 42) * Number(assumptions.battery_floor_percent || 10) / 100, assumptions.battery_floor_percent || 10)}</td>
        <td class="cell-comparison">${stack(money(newRevenue), "premium only")}</td>
        <td class="cell-comparison ${uplift >= 0 ? "positive" : "negative"}">${stack(money(uplift), "premium revenue")}</td>
        <td class="cell-comparison ${Number(day.daily_improvement) >= 0 ? "positive" : "negative"}">${stack(money(day.daily_improvement), "vs fair no-charge baseline")}</td>
        <td class="reason-cell"><span class="confidence ${String(day.confidence || "").toLowerCase()}">${esc(decision)}</span>${esc(day.reason)}</td>
      </tr>`;
    })()}
  `).join("");
  document.querySelector("#assumptions").innerHTML = `<strong>Planning assumptions:</strong> ${num(assumptions.charge_efficiency_percent, 1)}% charge efficiency, ${num(assumptions.discharge_efficiency_percent, 1)}% discharge efficiency, ${num(assumptions.degradation_cents_per_battery_kwh, 1)}c battery-wear allowance per battery kWh, full ML solar forecast, 10% FoxESS battery floor, and ${num(assumptions.terminal_energy_value_cents_per_kwh, 1)}c/kWh retained-energy value.`;
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
    const [status, today, optimiser, rulePlan, performance] = await Promise.all([
      api("status"), api("today"), api("economic-optimizer"), api("export-plan"), api("ml-performance"),
    ]);
    document.querySelector("#updated").textContent = `Updated ${new Date(status.time).toLocaleString()}`;
    const capacity = Number(optimiser.battery_capacity_kwh || 42);
    renderCurrent(status, today, capacity);
    renderDecision(optimiser);
    renderEconomics(optimiser);
    renderPlan(optimiser, rulePlan);
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
