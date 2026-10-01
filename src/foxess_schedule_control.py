import json
from copy import deepcopy

from foxess import foxess_post, get_device


GET_PATH = "/op/v3/device/scheduler/get"
WRITE_PATH = "/op/v3/device/scheduler/enable"


def main():

    device = get_device()
    sn = device["deviceSN"]

    print("\n=== READING CURRENT FOXESS SCHEDULE ===")

    current = foxess_post(
        GET_PATH,
        {"deviceSN": sn}
    )

    if not current or current.get("errno") != 0:
        raise RuntimeError(
            f"Could not read scheduler: {current}"
        )

    result = current["result"]
    groups = deepcopy(result.get("groups", []))

    print(json.dumps(groups, indent=2))

    # Find exactly our existing 17:05 ForceDischarge schedule.
    matches = [
        g for g in groups
        if g.get("workMode") == "ForceDischarge"
        and g.get("startHour") == 17
        and g.get("startMinute") == 5
        and g.get("endHour") == 22
        and g.get("endMinute") == 55
    ]

    if len(matches) != 1:
        raise RuntimeError(
            "Expected exactly one 17:05-22:55 "
            f"ForceDischarge schedule; found {len(matches)}"
        )

    target = matches[0]

    old_soc = float(
        target.get("extraParam", {}).get("fdSoc")
    )

    new_soc = 45.0

    print("\n=== PROPOSED CHANGE ===")
    print(f"Device:        {device['deviceType']}")
    print("Schedule:      17:05 -> 22:55")
    print("Mode:          ForceDischarge")
    print(
        "Power:         "
        f"{target['extraParam'].get('fdPwr')} W"
    )
    print(f"Current fdSoc: {old_soc:.0f}%")
    print(f"New fdSoc:     {new_soc:.0f}%")
    print(
        "After cutoff:  "
        f"{target['extraParam'].get('secondWorkMode')}"
    )

    # Modify only fdSoc in memory.
    target["extraParam"]["fdSoc"] = new_soc

    payload = {
        "deviceSN": sn,
        "groups": groups
    }

    print("\n=== WRITE PAYLOAD PREVIEW ===")
    print(json.dumps(payload, indent=2))

    print("\nDRY RUN ONLY")
    print("Nothing has been written to FoxESS.")


if __name__ == "__main__":
    main()
