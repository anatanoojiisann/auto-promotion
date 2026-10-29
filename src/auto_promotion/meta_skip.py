"""Human-requested workflow cancellation; publication outcome stays unresolved."""


def skip_unknown(browser, item_id, owner, authorization):
    return browser.result(
        item_id,
        {
            "status": "cancelled",
            "acknowledged_unknown": True,
            "authorization": authorization,
            "evidence": "Explicit human instruction to skip this uncertain item. "
            "No assertion of delivery/non-delivery, no resend, and the target reservation remains.",
        },
        owner,
    )
