"""Construction record types: what fields each carries and which statuses it moves through.

Every record type validates its known fields but also keeps unknown ones, so an
app can attach its own data (``extra="allow"``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .store import PMError


class _Data(BaseModel):
    model_config = ConfigDict(extra="allow")


class PermitData(_Data):
    permit_type: Literal[
        "building", "grading", "demolition", "electrical", "plumbing", "mechanical", "fire",
        "right_of_way", "certificate_of_occupancy", "other",
    ] = "building"
    authority: str | None = Field(None, description="Issuing jurisdiction or agency")
    permit_number: str | None = None
    applied_date: date | None = None
    approved_date: date | None = None
    expires_date: date | None = None
    fee: float | None = None


class InspectionData(_Data):
    inspection_type: str = Field(description="e.g. footing, foundation, framing_rough_in, insulation, final")
    authority: str | None = None
    permit_id: int | None = Field(None, description="Record id of the permit this inspection belongs to")
    scheduled_date: date | None = None
    inspector: str | None = None
    result_notes: str | None = None
    attempts: list[dict] = Field(default_factory=list, description="History of results, newest last")


class BudgetItemData(_Data):
    cost_code: str = Field(description="Cost code, e.g. CSI division '03 - Concrete'")
    category: Literal["labor", "material", "equipment", "subcontract", "fee", "contingency", "other"] = "other"
    budgeted: float = 0.0
    committed: float = 0.0
    actual: float = 0.0
    vendor: str | None = None


class ChangeOrderData(_Data):
    description: str = ""
    reason: Literal[
        "owner_request", "design_change", "unforeseen_condition", "code_requirement",
        "value_engineering", "other",
    ] = "other"
    cost_impact: float = 0.0
    schedule_impact_days: int = 0
    requested_by: str | None = None
    approved_by: str | None = None
    approved_date: date | None = None


class RFIData(_Data):
    question: str
    asked_by: str | None = None
    assigned_to: str | None = None
    answer: str | None = None
    answered_by: str | None = None
    answered_date: date | None = None
    spec_section: str | None = None
    drawing_ref: str | None = None
    cost_impact: bool = False
    schedule_impact: bool = False


class SubmittalData(_Data):
    spec_section: str | None = None
    submittal_type: Literal[
        "shop_drawing", "product_data", "sample", "mockup", "certificate", "test_report", "other",
    ] = "product_data"
    subcontractor: str | None = None
    reviewer: str | None = None
    required_by: date | None = None
    returned_date: date | None = None
    notes: str | None = None


class PunchItemData(_Data):
    location: str | None = None
    description: str = ""
    trade: str | None = None
    assigned_to: str | None = None
    photo_url: str | None = None


class DailyLogData(_Data):
    log_date: date
    weather: str | None = None
    temperature: str | None = None
    crew_count: int | None = None
    work_performed: str = ""
    delays: str | None = None
    safety_incidents: str | None = None
    visitors: str | None = None
    deliveries: str | None = None


class ContactData(_Data):
    role: Literal[
        "owner", "architect", "engineer", "general_contractor", "subcontractor", "supplier",
        "inspector", "lender", "consultant", "agent", "other",
    ]
    name: str | None = None
    company: str | None = None
    trade: str | None = None
    email: str | None = None
    phone: str | None = None
    license_number: str | None = None
    insurance_expires: date | None = None


class DocumentData(_Data):
    doc_type: Literal[
        "drawing", "specification", "estimate", "schedule", "contract", "permit_packet", "report",
        "photo", "warranty", "manual", "lien_release", "other",
    ] = "other"
    url: str | None = Field(None, description="Link or path to the file in whatever system holds it")
    revision: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class RecordType:
    name: str
    model: type[_Data]
    statuses: tuple[str, ...]
    closed: tuple[str, ...]  # statuses that need no further action
    amount_field: str | None = None  # data field mirrored into the indexed amount column
    description: str = ""

    @property
    def default_status(self) -> str:
        return self.statuses[0]


RECORD_TYPES: dict[str, RecordType] = {
    rt.name: rt
    for rt in (
        RecordType("permit", PermitData,
                   ("not_applied", "applied", "in_review", "corrections_required", "approved", "rejected", "expired"),
                   ("approved", "rejected", "expired"), "fee",
                   "A permit from a jurisdiction. A permit-kind task linked to it completes when it is approved."),
        RecordType("inspection", InspectionData,
                   ("not_scheduled", "scheduled", "passed", "failed", "partial", "cancelled"),
                   ("passed", "cancelled"), None,
                   "A required inspection. Its inspection-kind gate task completes when it passes; a failure"
                   " creates a correction task the gate waits on."),
        RecordType("budget_item", BudgetItemData, ("planned", "committed", "invoiced", "paid"),
                   ("paid",), "budgeted", "A cost line with budgeted, committed and actual amounts."),
        RecordType("change_order", ChangeOrderData, ("draft", "submitted", "approved", "rejected", "void"),
                   ("approved", "rejected", "void"), "cost_impact",
                   "A change to scope, cost or schedule. Approved change orders adjust the contract value."),
        RecordType("rfi", RFIData, ("open", "answered", "closed", "void"), ("closed", "void"), None,
                   "Request for information: a question to the design team or owner."),
        RecordType("submittal", SubmittalData,
                   ("pending", "submitted", "approved", "approved_as_noted", "revise_resubmit", "rejected"),
                   ("approved", "approved_as_noted", "rejected"), None,
                   "Shop drawings, product data or samples sent for design-team review."),
        RecordType("punch_item", PunchItemData, ("open", "in_progress", "ready_for_review", "closed"),
                   ("closed",), None, "A deficiency to fix before handover."),
        RecordType("daily_log", DailyLogData, ("draft", "final"), ("final",), None,
                   "The superintendent's daily report: weather, crew, work done, delays."),
        RecordType("contact", ContactData, ("active", "inactive"), ("active", "inactive"), None,
                   "A person or company on the project."),
        RecordType("document", DocumentData, ("draft", "issued", "approved", "superseded"),
                   ("issued", "approved", "superseded"), None,
                   "A drawing, spec, estimate, contract or other file, referenced by URL or path."),
    )
}


def record_type(name: str) -> RecordType:
    try:
        return RECORD_TYPES[name]
    except KeyError:
        raise PMError(f"record_type must be one of {', '.join(RECORD_TYPES)}") from None


def validate_data(rtype: RecordType, data: dict) -> dict:
    """Validate and normalise a record's data, keeping app-specific extra fields."""
    try:
        return rtype.model.model_validate(data).model_dump(mode="json", exclude_none=True)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'data'}: {err['msg']}" for err in exc.errors()
        )
        raise PMError(f"invalid {rtype.name} data: {problems}") from None


def describe_record_types() -> list[dict]:
    return [
        {
            "type": rt.name,
            "description": rt.description,
            "statuses": list(rt.statuses),
            "default_status": rt.default_status,
            "closed_statuses": list(rt.closed),
            "fields": rt.model.model_json_schema().get("properties", {}),
            "required_fields": rt.model.model_json_schema().get("required", []),
        }
        for rt in RECORD_TYPES.values()
    ]
