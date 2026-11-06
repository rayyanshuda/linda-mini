"""Expected per-fax outcome for the supplied 12-fax queue (derived by reading the scans)."""
R = {
    "referral": ("Olivia Chen - Referrals", "Referral", "Referrals / New"),
    "lab": ("Noah Patel - Clinical Review", "Lab Result", "Clinical / Labs"),
    "imaging": ("Noah Patel - Clinical Review", "Imaging Report", "Clinical / Imaging"),
    "pa": ("Ethan Brooks - Authorizations", "Prior Authorization", "Authorizations / Pending"),
    "refill": ("Ava Wilson - Nursing / Medication", "Refill Request", "Nursing / Medication"),
    "records": ("Mia Gomez - Medical Records", "Records Request", "Records / Requests"),
    "eob": ("Liam Rivera - Billing", "EOB", "Billing / EOBs"),
    "review": ("Jordan Ellis - Manual Review", "Needs Review", "Review / Unclassified"),
}

# fax_id: (action, route-key or None, classification, source_document_id)
EXPECTED = {
    "FX-201": ("routed", "referral", "referral", "RF-3001"),
    "FX-202": ("needs_review", "review", "lab_result", "LAB-8302"),
    "FX-203": ("routed", "imaging", "imaging_report", "IMG-4003"),
    "FX-204": ("routed", "pa", "prior_authorization", "PA-4004"),
    "FX-205": ("routed", "refill", "refill_request", "RX-4005"),
    "FX-206": ("routed", "records", "records_request", "MR-4006"),
    "FX-207": ("routed", "eob", "eob", "EOB-4007"),
    "FX-208": ("routed", "lab", "lab_result", "LAB-4008"),
    "FX-209": ("needs_review", "review", "unclassified", None),
    "FX-210": ("skipped_duplicate", None, "referral", None),
    "FX-211": ("already_routed", None, "imaging_report", "IMG-4111"),
    "FX-212": ("needs_review", "review", "mixed:[imaging_report,lab_result]", "LAB-4012A"),
}

RECEIVED = {
    "FX-201": "2026-11-03 08:02", "FX-202": "2026-11-03 08:06", "FX-203": "2026-11-03 08:12",
    "FX-204": "2026-11-03 08:12", "FX-205": "2026-11-03 08:19", "FX-206": "2026-11-03 08:25",
    "FX-207": "2026-11-03 08:31", "FX-208": "2026-11-03 08:37", "FX-209": "2026-11-03 08:42",
    "FX-210": "2026-11-03 08:49", "FX-211": "2026-11-03 08:54", "FX-212": "2026-11-03 09:03",
}
PAGES = {"FX-201": 2, "FX-206": 2, "FX-212": 3}


def route_dict(key):
    a, t, f = R[key]
    return {"assigned_to": a, "document_type": t, "folder": f}
