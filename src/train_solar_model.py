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

MODEL_FILE = MODEL_DIR / "solar_candidate_v1.joblib"

TRAIN_END = "2026-08-31T23:00"
TEST_START = "2026-09-01T00:00"
TEST_END = "2026-09-26T23:00"

SOLAR_CAPACITY_KW = 9.7
PV_FACTOR = 0.78


FEATURES = [
    "hour",
    "day_of_week",
    "month",
    "is_weekend",
    "temperature",
    "apparent_temperature",
    "cloud_cover",
    "precipitation",
    "wind_speed",
    "shortwave_radiation",
    "direct_radiation",
    "diffuse_radiation",
    "sunshine_duration",
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


def baseline_prediction(df):
    """
    Existing physical approximation used by export planner:
    radiation / 1000 * 9.7 kW * 0.78
    """

    return (
        df["shortwave_radiation"]
        / 1000.0
        * SOLAR_CAPACITY_KW
        * PV_FACTOR
    )


def metrics(actual, predicted):
    mae = mean_absolute_error(actual, predicted)

    rmse = np.sqrt(
        mean_squared_error(actual, predicted)
    )

    bias = np.mean(predicted - actual)

    return {
        "mae": mae,
        "rmse": rmse,
        "bias": bias,
    }


def daily_metrics(df, prediction_column):
    temp = df.copy()

    temp["date"] = temp["timestamp"].str[:10]

    daily = temp.groupby("date").agg(
        actual=("pv_kwh", "sum"),
        predicted=(prediction_column, "sum"),
    )

    daily["error"] = (
        daily["predicted"] - daily["actual"]
    )

    daily_mae = daily["error"].abs().mean()
    daily_bias = daily["error"].mean()

    return daily, daily_mae, daily_bias


def main():

    df = load_data()

    required = FEATURES + ["pv_kwh"]

    before = len(df)

    df = df.dropna(subset=required).copy()

    print()
    print("SOLAR ML CANDIDATE V1")
    print("=====================")
    print("Rows loaded:", before)
    print("Rows usable:", len(df))

    train = df[
        df["timestamp"] <= TRAIN_END
    ].copy()

    test = df[
        (df["timestamp"] >= TEST_START)
        & (df["timestamp"] <= TEST_END)
    ].copy()

    print("Training rows:", len(train))
    print("Test rows:    ", len(test))

    if len(train) == 0 or len(test) == 0:
        raise RuntimeError(
            "Training or test dataset is empty."
        )

    X_train = train[FEATURES]
    y_train = train["pv_kwh"]

    X_test = test[FEATURES]
    y_test = test["pv_kwh"]

    model = ExtraTreesRegressor(
        n_estimators=400,
        min_samples_leaf=2,
        max_features=0.85,
        random_state=42,
        n_jobs=-1,
    )

    print()
    print("Training ExtraTrees model...")

    model.fit(X_train, y_train)

    test["baseline_prediction"] = np.maximum(
        0.0,
        baseline_prediction(test)
    )

    test["ml_prediction"] = np.maximum(
        0.0,
        model.predict(X_test)
    )

    baseline = metrics(
        y_test,
        test["baseline_prediction"]
    )

    ml = metrics(
        y_test,
        test["ml_prediction"]
    )

    baseline_daily, baseline_daily_mae, baseline_daily_bias = (
        daily_metrics(
            test,
            "baseline_prediction"
        )
    )

    ml_daily, ml_daily_mae, ml_daily_bias = (
        daily_metrics(
            test,
            "ml_prediction"
        )
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
        f"{baseline_daily_mae:>14.2f}"
        f"{ml_daily_mae:>14.2f}"
    )

    print(
        f"{'Daily Bias':<15}"
        f"{baseline_daily_bias:>14.2f}"
        f"{ml_daily_bias:>14.2f}"
    )

    comparison = pd.DataFrame({
        "actual": ml_daily["actual"],
        "baseline": baseline_daily["predicted"],
        "ml": ml_daily["predicted"],
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

    baseline_score = baseline_daily_mae
    ml_score = ml_daily_mae

    improvement = (
        (baseline_score - ml_score)
        / baseline_score
        * 100.0
    )

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

    if ml_score < baseline_score:
        status = "CANDIDATE_WINS"
    else:
        status = "BASELINE_WINS"

    print("Result:             ", status)

    package = {
        "model": model,
        "features": FEATURES,
        "version": "solar_candidate_v1",
        "train_end": TRAIN_END,
        "test_start": TEST_START,
        "test_end": TEST_END,
        "baseline_daily_mae": baseline_score,
        "ml_daily_mae": ml_score,
        "improvement_percent": improvement,
        "status": status,
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
        "NOTE: Candidate is SHADOW ONLY. "
        "Export planner has NOT been changed."
    )


if __name__ == "__main__":
    main()
