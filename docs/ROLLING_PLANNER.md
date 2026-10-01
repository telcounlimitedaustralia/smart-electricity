# Rolling seven-day planner

## Purpose

The planner selects cheap grid charging, battery retention, and premium battery
export across the complete next seven days. It maximises:

`export revenue - import cost - degradation cost`

Avoided expensive imports are not counted twice: they appear as lower import
cost than the no-action baseline. The reported avoided-import value is the
difference between that fair baseline and the selected strategy.

## Safety rules

- FoxESS physical floor: 10% (4.2 kWh of a 42 kWh battery).
- Planner buffer: configurable 5% (2.1 kWh) above the physical floor.
- No selected plan may create additional pre-10 AM import above its no-action
  baseline.
- Grid charge and premium export are limited to 10 kW and their tariff windows.
- Charge and discharge efficiencies are both configurable; current defaults are
  95%.

## What is new

`src/rolling_planner.py` is pure simulation code. It has no database writes and
no FoxESS API imports. It provides:

- forecast-derived effective solar start rather than a fixed morning time;
- forecast-derived overnight battery requirement;
- hourly energy, tariff, efficiency, site-power, import/export, and SOC flows;
- configurable degradation cost;
- a joint day-by-day search of cheap-charge plus premium-export actions;
- an explicit no-action baseline for avoided-import reporting.
- named conservative, aggressive, and hybrid strategy comparisons that show
  import cost, export revenue, degradation cost, safety eligibility, and net
  incremental value side-by-side.

## Current integration status

Existing project components remain the data source: `forecast_days()` supplies
seven-day hourly load forecasts, weather/radiation supplies hourly solar inputs,
`tariff.py` supplies AGL rates, and `forecast_vintages` records forecast errors.
`historical_backtest.py` replays actual five-minute FoxESS data as hourly solar
and load intervals using the same maximum-ten-minute gap guard as the existing
actuals reporter. It remains shadow-only; it does not connect to FoxESS control.
