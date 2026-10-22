"""Delivery evidence is separate from the lifecycle state of a finite batch."""


def summarize(items, mode):
    counts = dict.fromkeys(
        (
            "confirmed",
            "simulated",
            "unknown",
            "missed",
            "failed_known",
            "scheduled",
            "manual_attention",
            "not_attempted",
            "unverified",
        ),
        0,
    )
    for item in items:
        status = item["status"]
        if status == "confirmed":
            reference = item.get("external_reference") or ""
            if mode == "simulation" or reference.startswith("simulation://"):
                counts["simulated"] += 1
            elif reference and item.get("result", {}).get("verified") is True:
                counts["confirmed"] += 1
            else:
                counts["unverified"] += 1
        elif status in counts:
            counts[status] += 1
    return counts
