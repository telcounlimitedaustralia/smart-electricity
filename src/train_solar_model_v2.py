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

MODEL_FILE = MODEL_DIR / "solar_candidate_v2.joblib"

TRAIN_END = "2026-08-31T23:00"
TEST_START = "2026-09-01T00:00"
TEST_END = "2026-09-26T23:00"

SOLAR_CAPACITY_KW = 9.7
PV_FACTOR = 0.78

# Prevent ML correction from making extreme changes.
# Correction is limited to ±25% of the physics prediction.
MAX_CORRECTION_FRACTION = 0.25


FEATURES = [
    "hour",
    "day_of_week",
    "month",
    "temperature",
    "apparent_temperature",
    "cloud_cover",
    "precipitation",
    "wind_speed",
    "shortwave_radiation",
    "direct_radiation",
    "diffuse_radiation",
    "sunshine_duration",
    "physics_prediction",
]


def load_data():

    conn = sqlite3.connect(DB)

    df = pd.read_sql_query("""
        SELECT
            timestamp,
            hour,
            day_of_week,
            month,

            pv_kwh,

            temperature,
            apparent_temperature,
            cloud_cover,
            precipitation,
            wind_speed,
            shortwave_radiation,
            direct_radiation,
            diffuse_radiation,
            sunshine_duration

        FROM ml_training_hourly

        WHERE timestamp <= ?

        ORDER BY timestamp
    """, conn, params=(TEST_END,))

    conn.close()

    return df


def add_physics_prediction(df):

    df = df.copy()

    df["physics_prediction"] = (
        df["shortwave_radiation"]
        / 1000.0
        * SOLAR_CAPACITY_KW
        * PV_FACTOR
    )

    df["physics_prediction"] = np.maximum(
        0.0,
        df["physics_prediction"]
    )

    return df


def metrics(actual, predicted):

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

        "bias": np.mean(
            predicted - actual
        )
    }


def daily_results(df, prediction_col):

    temp = df.copy()

    temp["date"] = temp["timestamp"].str[:10]

    daily = temp.groupby("date").agg(
        actual=("pv_kwh", "sum"),
        predicted=(prediction_col, "sum")
    )

    daily["error"] = (
        daily["predicted"]
        - daily["actual"]
    )

    return {
        "table": daily,
        "mae": daily["error"].abs().mean(),
        "bias": daily["error"].mean(),
        "rmse": np.sqrt(
            np.mean(
                daily["error"] ** 2
            )
        )
    }


def main():

    df = load_data()

    df = add_physics_prediction(df)

    required = FEATURES + ["pv_kwh"]

    df = df.dropna(
        subset=required
    ).copy()

    # Residual = what the physics model got wrong.
    df["residual"] = (
        df["pv_kwh"]
        - df["physics_prediction"]
    )

    train = df[
        df["timestamp"] <= TRAIN_END
    ].copy()

    test = df[
        (df["timestamp"] >= TEST_START)
        &
        (df["timestamp"] <= TEST_END)
    ].copy()

    print()
    print("SOLAR ML CANDIDATE V2")
    print("=====================")
    print("Architecture: Physics + ML residual correction")
    print("Training rows:", len(train))
    print("Test rows:    ", len(test))

    X_train = train[FEATURES]
    y_train = train["residual"]

    X_test = test[FEATURES]

    model = ExtraTreesRegressor(
        n_estimators=500,
        min_samples_leaf=4,
        max_features=0.8,
        random_state=42,
        n_jobs=-1
    )

    print()
    print("Training residual model...")

    model.fit(
        X_train,
        y_train
    )

    raw_correction = model.predict(
        X_test
    )

    # Guardrail:
    # ML may only adjust physics by ±25%.
    max_adjustment = (
        test["physics_prediction"]
        * MAX_CORRECTION_FRACTION
    ).to_numpy()

    correction = np.clip(
        raw_correction,
        -max_adjustment,
        max_adjustment
    )

    # At night physics is zero; keep prediction zero.
    correction = np.where(
        test["physics_prediction"].to_numpy() > 0,
        correction,
        0.0
    )

    test["ml_correction"] = correction

    test["hybrid_prediction"] = np.maximum(
        0.0,
        test["physics_prediction"]
        + test["ml_correction"]
    )

    baseline = metrics(
        test["pv_kwh"],
        test["physics_prediction"]
    )

    hybrid = metrics(
        test["pv_kwh"],
        test["hybrid_prediction"]
    )

    baseline_daily = daily_results(
        test,
        "physics_prediction"
    )

    hybrid_daily = daily_results(
        test,
        "hybrid_prediction"
    )

    print()
    print("HOURLY TEST RESULTS")
    print("===================")

    print(
        f"{'Metric':<15}"
        f"{'Physics':>14}"
        f"{'Hybrid':>14}"
    )

    print(
        f"{'MAE kWh':<15}"
        f"{baseline['mae']:>14.3f}"
        f"{hybrid['mae']:>14.3f}"
    )

    print(
        f"{'RMSE kWh':<15}"
        f"{baseline['rmse']:>14.3f}"
        f"{hybrid['rmse']:>14.3f}"
    )

    print(
        f"{'Bias kWh':<15}"
        f"{baseline['bias']:>14.3f}"
        f"{hybrid['bias']:>14.3f}"
    )

    print()
    print("DAILY TEST RESULTS")
    print("==================")

    print(
        f"{'Metric':<15}"
        f"{'Physics':>14}"
        f"{'Hybrid':>14}"
    )

    print(
        f"{'Daily MAE':<15}"
        f"{baseline_daily['mae']:>14.2f}"
        f"{hybrid_daily['mae']:>14.2f}"
    )

    print(
        f"{'Daily RMSE':<15}"
        f"{baseline_daily['rmse']:>14.2f}"
        f"{hybrid_daily['rmse']:>14.2f}"
    )

    print(
        f"{'Daily Bias':<15}"
        f"{baseline_daily['bias']:>14.2f}"
        f"{hybrid_daily['bias']:>14.2f}"
    )

    b = baseline_daily["table"]
    h = hybrid_daily["table"]

    comparison = pd.DataFrame({
        "actual": b["actual"],
        "physics": b["predicted"],
        "hybrid": h["predicted"]
    })

    comparison["physics_error"] = (
        comparison["physics"]
        - comparison["actual"]
    )

    comparison["hybrid_error"] = (
        comparison["hybrid"]
        - comparison["actual"]
    )

    print()
    print("DAILY COMPARISON")
    print("================")

    print(
        comparison.round(2).to_string()
    )

    baseline_score = baseline_daily["mae"]
    hybrid_score = hybrid_daily["mae"]

    improvement = (
        (baseline_score - hybrid_score)
        / baseline_score
        * 100.0
    )

    # Promotion requires better daily MAE
    # AND no materially worse absolute bias.
    mae_wins = (
        hybrid_score < baseline_score
    )

    bias_safe = (
        abs(hybrid_daily["bias"])
        <= max(
            abs(baseline_daily["bias"]) + 0.50,
            1.50
        )
    )

    if mae_wins and bias_safe:
        status = "CANDIDATE_WINS"
    else:
        status = "PHYSICS_WINS"

    print()
    print("MODEL DECISION")
    print("==============")

    print(
        f"Physics daily MAE: {baseline_score:.2f} kWh"
    )

    print(
        f"Hybrid daily MAE:  {hybrid_score:.2f} kWh"
    )

    print(
        f"Improvement:        {improvement:.1f}%"
    )

    print(
        f"Physics bias:       "
        f"{baseline_daily['bias']:.2f} kWh/day"
    )

    print(
        f"Hybrid bias:        "
        f"{hybrid_daily['bias']:.2f} kWh/day"
    )

    print(
        f"Bias guardrail:     "
        f"{'PASS' if bias_safe else 'FAIL'}"
    )

    print("Result:             ", status)

    package = {
        "model": model,
        "features": FEATURES,
        "version": "solar_candidate_v2",
        "architecture": "physics_plus_residual_ml",
        "solar_capacity_kw": SOLAR_CAPACITY_KW,
        "pv_factor": PV_FACTOR,
        "max_correction_fraction":
            MAX_CORRECTION_FRACTION,
        "train_end": TRAIN_END,
        "test_start": TEST_START,
        "test_end": TEST_END,
        "physics_daily_mae":
            baseline_score,
        "hybrid_daily_mae":
            hybrid_score,
        "physics_daily_bias":
            baseline_daily["bias"],
        "hybrid_daily_bias":
            hybrid_daily["bias"],
        "improvement_percent":
            improvement,
        "status":
            status
    }

    joblib.dump(
        package,
        MODEL_FILE
    )

    print()
    print("Candidate saved:")
    print(MODEL_FILE)

    print()
    print(
        "SHADOW ONLY - export planner unchanged."
    )


if __name__ == "__main__":
    main()
