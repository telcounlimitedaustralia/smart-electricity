import json

from ml_daily_performance import calculate as performance
from ml_risk_analysis import calculate as risk


MIN_DAYS = 7
PREFERRED_DAYS = 14

# Conservative promotion requirements.
MIN_IMPROVEMENT_PERCENT = 15.0
MAX_ABS_BIAS_KWH = 2.0
MAX_SAFE_UNDER_RATE = 15.0


def calculate():

    p = performance()
    r = risk()

    days = p.get("validated_days", 0)

    result = {
        "ready": False,
        "validated_days": days,
        "minimum_days": MIN_DAYS,
        "preferred_days": PREFERRED_DAYS,
        "mode": "SHADOW",
        "checks": {}
    }

    if not p.get("available") or not r.get("available"):
        result["status"] = "COLLECTING_DATA"
        result["reason"] = (
            f"Need at least {MIN_DAYS} genuine "
            "validated ML days."
        )
        return result

    improvement = p.get(
        "ml_improvement_percent"
    )

    bias = p.get("ml_bias_kwh")

    safe_under_rate = r.get(
        "safe_ml_underprediction_rate_percent"
    )

    enough_days = days >= MIN_DAYS

    improvement_ok = (
        improvement is not None
        and improvement >= MIN_IMPROVEMENT_PERCENT
    )

    bias_ok = (
        bias is not None
        and abs(bias) <= MAX_ABS_BIAS_KWH
    )

    safety_ok = (
        safe_under_rate is not None
        and safe_under_rate <= MAX_SAFE_UNDER_RATE
    )

    result["checks"] = {
        "enough_days": {
            "pass": enough_days,
            "value": days,
            "required": MIN_DAYS
        },

        "accuracy_improvement": {
            "pass": improvement_ok,
            "value_percent": improvement,
            "required_percent":
                MIN_IMPROVEMENT_PERCENT
        },

        "bias": {
            "pass": bias_ok,
            "value_kwh": bias,
            "max_absolute_kwh":
                MAX_ABS_BIAS_KWH
        },

        "safe_load_underprediction": {
            "pass": safety_ok,
            "value_percent": safe_under_rate,
            "max_percent":
                MAX_SAFE_UNDER_RATE
        }
    }

    all_pass = (
        enough_days
        and improvement_ok
        and bias_ok
        and safety_ok
    )

    if not enough_days:
        result["status"] = "COLLECTING_DATA"
        result["reason"] = (
            f"{days}/{MIN_DAYS} minimum "
            "validated days collected."
        )

    elif all_pass:
        result["ready"] = True
        result["status"] = "CANDIDATE_READY"
        result["reason"] = (
            "ML candidate passes current shadow "
            "validation guardrails."
        )

    else:
        result["status"] = "NOT_READY"
        result["reason"] = (
            "Enough data exists, but one or more "
            "ML guardrails failed."
        )

    if days >= PREFERRED_DAYS:
        result["evidence"] = "STRONGER"
    elif days >= MIN_DAYS:
        result["evidence"] = "PRELIMINARY"
    else:
        result["evidence"] = "INSUFFICIENT"

    return result


if __name__ == "__main__":
    print(
        json.dumps(
            calculate(),
            indent=2
        )
    )
