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

MODEL_FILE = MODEL_DIR / "load_candidate_v1.joblib"

TRAIN_END = "2026-08-31T23:00"
TEST_START = "2026-09-01T00:00"
TEST_END = "2026-09-26T23:00"

SAFETY_FACTOR = 1.10

FEATURES = [
    "hour",
    "day_of_week",
    "month",
    "is_weekend",
    "temperature",
    "apparent_temperature",
    "cloud_cover",
    "precipitation",
]


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
        WHERE timestamp <= ?
        ORDER BY timestamp
    """, conn, params=(TEST_END,))

    conn.close()

    return df


def metrics(actual, predicted):

    return {
        "mae": mean_absolute_error(actual, predicted),

        "rmse": np.sqrt(
            mean_squared_error(actual, predicted)
        ),

        "bias": np.mean(predicted - actual)
    }


def daily_results(df, prediction_col):

    temp = df.copy()
    temp["date"] = temp["timestamp"].str[:10]

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

    df = df.dropna(
        subset=FEATURES + ["load_kwh"]
    ).copy()

    train = df[
        df["timestamp"] <= TRAIN_END
    ].copy()

    test = df[
        (df["timestamp"] >= TEST_START)
        &
        (df["timestamp"] <= TEST_END)
    ].copy()

    print()
    print("LOAD ML CANDIDATE V1")
    print("====================")
    print("Training rows:", len(train))
    print("Test rows:    ", len(test))

    # -----------------------------------------
    # Current baseline
    # -----------------------------------------

    profile = (
        train.groupby("hour")["load_kwh"]
        .mean()
        * SAFETY_FACTOR
    )

    test["baseline_prediction"] = (
        test["hour"].map(profile)
    )

    # -----------------------------------------
    # ML model
    # -----------------------------------------

    X_train = train[FEATURES]
    y_train = train["load_kwh"]

    X_test = test[FEATURES]

    model = ExtraTreesRegressor(
        n_estimators=500,
        min_samples_leaf=5,
        max_features=0.8,
        random_state=42,
        n_jobs=-1
    )

    print()
    print("Training ExtraTrees load model...")

    model.fit(X_train, y_train)

    test["ml_prediction"] = np.maximum(
        0.0,
        model.predict(X_test)
    )

    # -----------------------------------------
    # Hourly metrics
    # -----------------------------------------

    baseline = metrics(
        test["load_kwh"],
        test["baseline_prediction"]
    )

    ml = metrics(
        test["load_kwh"],
        test["ml_prediction"]
    )

    # -----------------------------------------
    # Daily metrics
    # -----------------------------------------

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
        f"{'ML':>14}"
    )

    print(
        f"{'MAE kWh':<15}"
        f"{baseline['mae']:>14.3f}"
        f"{ml['mae']:>14.3f}"
    )

    print(
        f"{'RMSE kWh':<15}"
        f"{baseline['rmse']:>14.3f}"
        f"{ml['rmse']:>14.3f}"
    )

    print(
        f"{'Bias kWh':<15}"
        f"{baseline['bias']:>14.3f}"
        f"{ml['bias']:>14.3f}"
    )

    print()
    print("DAILY TEST RESULTS")
    print("==================")

    print(
        f"{'Metric':<15}"
        f"{'Baseline':>14}"
        f"{'ML':>14}"
    )

    print(
        f"{'Daily MAE':<15}"
        f"{baseline_daily['mae']:>14.2f}"
        f"{ml_daily['mae']:>14.2f}"
    )

    print(
        f"{'Daily RMSE':<15}"
        f"{baseline_daily['rmse']:>14.2f}"
        f"{ml_daily['rmse']:>14.2f}"
    )

    print(
        f"{'Daily Bias':<15}"
        f"{baseline_daily['bias']:>14.2f}"
        f"{ml_daily['bias']:>14.2f}"
    )

    # -----------------------------------------
    # Daily comparison
    # -----------------------------------------

    b = baseline_daily["table"]
    m = ml_daily["table"]

    comparison = pd.DataFrame({
        "actual": b["actual"],
        "baseline": b["predicted"],
        "ml": m["predicted"]
    })

    comparison["baseline_error"] = (
        comparison["baseline"]
        - comparison["actual"]
    )

    comparison["ml_error"] = (
        comparison["ml"]
        - comparison["actual"]
    )

    print()
    print("DAILY COMPARISON")
    print("================")

    print(
        comparison.round(2).to_string()
    )

    # -----------------------------------------
    # Decision
    # -----------------------------------------

    baseline_score = baseline_daily["mae"]
    ml_score = ml_daily["mae"]

    improvement = (
        (baseline_score - ml_score)
        / baseline_score
        * 100
    )

    mae_wins = ml_score < baseline_score

    # Allow <= 2 kWh/day absolute systematic bias.
    bias_safe = (
        abs(ml_daily["bias"]) <= 2.0
    )

    if mae_wins and bias_safe:
        status = "CANDIDATE_WINS"
    else:
        status = "BASELINE_WINS"

    print()
    print("MODEL DECISION")
    print("==============")

    print(
        f"Baseline daily MAE: {baseline_score:.2f} kWh"
    )

    print(
        f"ML daily MAE:       {ml_score:.2f} kWh"
    )

    print(
        f"Improvement:         {improvement:.1f}%"
    )

    print(
        f"Baseline bias:       "
        f"{baseline_daily['bias']:.2f} kWh/day"
    )

    print(
        f"ML bias:             "
        f"{ml_daily['bias']:.2f} kWh/day"
    )

    print(
        "Bias guardrail:      "
        + ("PASS" if bias_safe else "FAIL")
    )

    print("Result:              ", status)

    package = {
        "model": model,
        "features": FEATURES,
        "version": "load_candidate_v1",
        "train_end": TRAIN_END,
        "test_start": TEST_START,
        "test_end": TEST_END,
        "baseline_daily_mae":
            baseline_score,
        "ml_daily_mae":
            ml_score,
        "baseline_daily_bias":
            baseline_daily["bias"],
        "ml_daily_bias":
            ml_daily["bias"],
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
