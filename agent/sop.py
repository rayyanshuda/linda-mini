"""Routing SOP: the source for exact dropdown labels.

Values are copied verbatim from the README / assignment SOP table and are
selected in the portal by their visible label (not the option value).
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Category(str, Enum):
    REFERRAL = "referral"
    LAB = "lab_result"
    IMAGING = "imaging_report"
    PRIOR_AUTH = "prior_authorization"
    REFILL = "refill_request"
    RECORDS = "records_request"
    EOB = "eob"
    NEEDS_REVIEW = "needs_review"


@dataclass(frozen=True)
class Route:
    assigned_to: str
    document_type: str
    folder: str

    def as_dict(self) -> dict:
        return {"assigned_to": self.assigned_to, "document_type": self.document_type, "folder": self.folder}


SOP: dict[Category, Route] = {
    Category.REFERRAL: Route("Olivia Chen - Referrals", "Referral", "Referrals / New"),
    Category.LAB: Route("Noah Patel - Clinical Review", "Lab Result", "Clinical / Labs"),
    Category.IMAGING: Route("Noah Patel - Clinical Review", "Imaging Report", "Clinical / Imaging"),
    Category.PRIOR_AUTH: Route("Ethan Brooks - Authorizations", "Prior Authorization", "Authorizations / Pending"),
    Category.REFILL: Route("Ava Wilson - Nursing / Medication", "Refill Request", "Nursing / Medication"),
    Category.RECORDS: Route("Mia Gomez - Medical Records", "Records Request", "Records / Requests"),
    Category.EOB: Route("Liam Rivera - Billing", "EOB", "Billing / EOBs"),
    Category.NEEDS_REVIEW: Route("Jordan Ellis - Manual Review", "Needs Review", "Review / Unclassified"),
}
