import sqlite3
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

from sklearn.ensemble import ExtraTreesRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error


DB = Path.home() / "smart-electricity" / "data" / "energy.db"
MODEL_DIR = Path.home() / "smart-electricity" / "models"
MODEL_DIR.mkdir(exist_ok=True)

MODEL_FILE = MODEL_DIR / "load_candidate_v2.joblib"

TEST_DAYS = 28

SAFETY_FACTOR = 1.10


BASE_FEATURES = [
    "hour",
    "day_of_week",
    "month",
    "is_weekend",
    "temperature",
    "apparent_temperature",
    "cloud_cover",
    "precipitation",
]

LAG_FEATURES = [
    "load_same_hour_yesterday",
    "load_same_hour_7d",
    "previous_day_total_load",
    "rolling_7d_daily_load",
]

FEATURES = BASE_FEATURES + LAG_FEATURES


def load_data():

    conn = sqlite3.connect(DB)

    df = pd.read_sql_query("""
        SELECT
            timestamp,
            hour,
            day_of_week,
            month,
            is_weekend,
            load_kwh,
            temperature,
            apparent_temperature,
            cloud_cover,
            precipitation
        FROM ml_training_hourly
        ORDER BY timestamp
    """, conn)

    conn.close()

    df["dt"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("dt").reset_index(drop=True)

    return df


def add_lag_features(df):

    x = df.copy()

    # Calendar-keyed lags are essential. Row shifts silently point at the
    # wrong clock hour after any missing observation and do not match the
    # timestamp-keyed lookup used by the production predictor.
    load_by_time = x.set_index("dt")["load_kwh"]
    x["load_same_hour_yesterday"] = x["dt"].map(
        load_by_time.shift(freq="24h")
    )
    x["load_same_hour_7d"] = x["dt"].map(
        load_by_time.shift(freq="168h")
    )

    x["date"] = x["dt"].dt.date

    daily = (
        x.groupby("date")["load_kwh"]
        .sum()
        .rename("daily_load")
        .reset_index()
    )

    # Only information from completed previous days.
    daily["previous_day_total_load"] = (
        daily["daily_load"].shift(1)
    )

    daily["rolling_7d_daily_load"] = (
        daily["daily_load"]
        .shift(1)
        .rolling(
            window=7,
            min_periods=3
        )
        .mean()
    )

    x = x.merge(
        daily[
            [
                "date",
                "previous_day_total_load",
                "rolling_7d_daily_load",
            ]
        ],
        on="date",
        how="left"
    )

    return x


def metrics(actual, predicted):

    error = predicted - actual

    return {
        "mae": mean_absolute_error(
            actual,
            predicted
        ),
        "rmse": np.sqrt(
            mean_squared_error(
                actual,
                predicted
            )
        ),
        "bias": np.mean(error)
    }


def daily_results(df, prediction_col):

    temp = df.copy()

    daily = temp.groupby("date").agg(
        actual=("load_kwh", "sum"),
        predicted=(prediction_col, "sum")
    )

    daily["error"] = (
        daily["predicted"] - daily["actual"]
    )

    return {
        "table": daily,
        "mae": daily["error"].abs().mean(),
        "rmse": np.sqrt(
            np.mean(daily["error"] ** 2)
        ),
        "bias": daily["error"].mean()
    }


def main():

    df = load_data()
    df = add_lag_features(df)

    df = df.dropna(
        subset=FEATURES + ["load_kwh"]
    ).copy()

    latest_day = df["dt"].max().normalize()
    test_start_dt = latest_day - pd.Timedelta(days=TEST_DAYS - 1)
    train_end_dt = test_start_dt - pd.Timedelta(hours=1)
    test_end_dt = latest_day + pd.Timedelta(hours=23)

    train = df[
        df["dt"] <= train_end_dt
    ].copy()

    test = df[
        (df["dt"] >= test_start_dt)
        &
        (df["dt"] <= test_end_dt)
    ].copy()

    print()
    print("LOAD ML CANDIDATE V2")
    print("====================")
    print("Architecture: weather + calendar + load history")
    print("Training rows:", len(train))
    print("Test rows:    ", len(test))

    # Current baseline
    profile = (
        train.groupby("hour")["load_kwh"]
        .mean()
        * SAFETY_FACTOR
    )

    test["baseline_prediction"] = (
        test["hour"].map(profile)
    )

    # ML
    model = ExtraTreesRegressor(
        n_estimators=600,
        min_samples_leaf=5,
        max_features=0.85,
        random_state=42,
        n_jobs=-1
    )

    print()
    print("Training ExtraTrees load model...")

    model.fit(
        train[FEATURES],
        train["load_kwh"]
    )

    test["ml_prediction"] = np.maximum(
        0.0,
        model.predict(test[FEATURES])
    )

    baseline = metrics(
        test["load_kwh"],
        test["baseline_prediction"]
    )

    ml = metrics(
        test["load_kwh"],
        test["ml_prediction"]
    )

    baseline_daily = daily_results(
        test,
        "baseline_prediction"
    )

    ml_daily = daily_results(
        test,
        "ml_prediction"
    )

    print()
    print("HOURLY TEST RESULTS")
    print("===================")

    print(
        f"{'Metric':<15}"
        f"{'Baseline':>14}"
        f"{'ML v2':>14}"
    )

    for name, key in [
        ("MAE kWh", "mae"),
        ("RMSE kWh", "rmse"),
        ("Bias kWh", "bias"),
    ]:
        print(
            f"{name:<15}"
            f"{baseline[key]:>14.3f}"
            f"{ml[key]:>14.3f}"
        )

    print()
    print("DAILY TEST RESULTS")
    print("==================")

    print(
        f"{'Metric':<15}"
        f"{'Baseline':>14}"
        f"{'ML v2':>14}"
    )

    for name, key in [
        ("Daily MAE", "mae"),
        ("Daily RMSE", "rmse"),
        ("Daily Bias", "bias"),
    ]:
        print(
            f"{name:<15}"
            f"{baseline_daily[key]:>14.2f}"
            f"{ml_daily[key]:>14.2f}"
        )

    b = baseline_daily["table"]
    m = ml_daily["table"]

    comparison = pd.DataFrame({
        "actual": b["actual"],
        "baseline": b["predicted"],
        "ml_v2": m["predicted"]
    })

    comparison["baseline_error"] = (
        comparison["baseline"]
        - comparison["actual"]
    )

    comparison["ml_v2_error"] = (
        comparison["ml_v2"]
        - comparison["actual"]
    )

    print()
    print("DAILY COMPARISON")
    print("================")
    print(comparison.round(2).to_string())

    baseline_score = baseline_daily["mae"]
    ml_score = ml_daily["mae"]

    improvement = (
        (baseline_score - ml_score)
        / baseline_score
        * 100
    )

    bias_safe = abs(
        ml_daily["bias"]
    ) <= 2.0

    # Additional safety metric:
    # worst daily underprediction.
    worst_underprediction = min(
        comparison["ml_v2_error"]
    )

    # One-sided empirical buffers for planning.  Positive values represent
    # actual load above the point prediction.  These are calibration values,
    # not Gaussian confidence intervals.
    under_errors = (
        comparison["actual"]
        - comparison["ml_v2"]
    )
    underprediction_p80 = max(
        0.0,
        float(under_errors.quantile(0.80))
    )
    underprediction_p90 = max(
        0.0,
        float(under_errors.quantile(0.90))
    )
    underprediction_p95 = max(
        0.0,
        float(under_errors.quantile(0.95))
    )

    # For now flag, rather than automatically reject,
    # if one day is underestimated by >8 kWh.
    high_load_risk = (
        worst_underprediction < -8.0
    )

    status = (
        "CANDIDATE_WINS"
        if ml_score < baseline_score
        and bias_safe
        else "BASELINE_WINS"
    )

    # Compare the challenger with the deployed incumbent on the identical
    # rolling holdout. Promotion requires a measurable improvement; if the
    # incumbent cannot be evaluated, retain the baseline guardrail above.
    incumbent_daily_mae = None
    incumbent = None
    if MODEL_FILE.exists():
        try:
            incumbent = joblib.load(MODEL_FILE)
            incumbent_prediction = np.maximum(
                0.0,
                incumbent["model"].predict(test[FEATURES])
            )
            incumbent_test = test.copy()
            incumbent_test["incumbent_prediction"] = incumbent_prediction
            incumbent_daily_mae = daily_results(
                incumbent_test,
                "incumbent_prediction"
            )["mae"]
            if ml_score > incumbent_daily_mae * 0.99:
                status = "INCUMBENT_RETAINED"
        except Exception as exc:
            print("Incumbent comparison unavailable:", exc)

    print()
    print("MODEL DECISION")
    print("==============")
    print(
        f"Baseline daily MAE:     "
        f"{baseline_score:.2f} kWh"
    )
    print(
        f"ML v2 daily MAE:        "
        f"{ml_score:.2f} kWh"
    )
    print(
        f"Improvement:            "
        f"{improvement:.1f}%"
    )
    print(
        f"ML v2 daily bias:       "
        f"{ml_daily['bias']:.2f} kWh/day"
    )
    print(
        f"Worst underprediction:  "
        f"{worst_underprediction:.2f} kWh"
    )
    print(
        "High-load risk:         "
        + ("YES" if high_load_risk else "NO")
    )
    print(
        "Bias guardrail:         "
        + ("PASS" if bias_safe else "FAIL")
    )
    print("Result:                 ", status)

    if incumbent_daily_mae is not None:
        print(
            f"Incumbent daily MAE:     {incumbent_daily_mae:.2f} kWh"
        )

    # After a successful chronological evaluation, refit on every available
    # complete row so tomorrow's forecasts learn from the newest history.
    if status == "CANDIDATE_WINS":
        model.fit(df[FEATURES], df["load_kwh"])

    package = {
        "model": model,
        "features": FEATURES,
        "version": "load_candidate_v2",
        "architecture":
            "weather_calendar_load_lags",
        "baseline_daily_mae":
            baseline_score,
        "ml_daily_mae":
            ml_score,
        "ml_daily_bias":
            ml_daily["bias"],
        "worst_daily_underprediction":
            worst_underprediction,
        "underprediction_buffer_p80":
            underprediction_p80,
        "underprediction_buffer_p90":
            underprediction_p90,
        "underprediction_buffer_p95":
            underprediction_p95,
        "high_load_risk":
            high_load_risk,
        "improvement_percent":
            improvement,
        "status":
            status,
        "train_end": train_end_dt.isoformat(),
        "test_start": test_start_dt.isoformat(),
        "test_end": test_end_dt.isoformat(),
        "incumbent_daily_mae": incumbent_daily_mae,
    }

    if status == "CANDIDATE_WINS":
        joblib.dump(package, MODEL_FILE)

    print()
    print("Candidate saved:" if status == "CANDIDATE_WINS" else "Incumbent retained:")
    print(MODEL_FILE)
    print()
    print(
        "SHADOW ONLY - export planner unchanged."
    )


if __name__ == "__main__":
    main()
