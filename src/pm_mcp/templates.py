"""Construction plan templates: phases and a task graph that become a project.

A template lists phases (created as milestones) and tasks keyed by short ids so
dependencies can refer to each other. Durations are working days and are
typical values for planning, meant to be adjusted per project. Tasks are
tagged with the roles that do them so specialist agents can claim only their
own work with claim_next_task(tags=[...]).

Built-in templates live here; apps can store their own with save_template.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

ROLES = (
    "owner", "project_manager", "architect", "engineer", "estimator", "permit", "procurement",
    "scheduler", "superintendent", "safety", "site_work", "concrete", "steel", "framing", "roofing",
    "plumbing", "electrical", "hvac", "fire_protection", "insulation", "drywall", "finishes",
    "landscaping", "inspection", "commissioning", "closeout",
)


class TemplatePhase(BaseModel):
    key: str
    name: str
    description: str = ""


class TemplateTask(BaseModel):
    key: str
    title: str
    phase: str
    roles: list[str] = Field(default_factory=list, description="Tags; agents claim by role")
    duration_days: float = 1
    depends_on: list[str] = Field(default_factory=list, description="Keys of tasks that must finish first")
    description: str = ""
    priority: Literal["low", "medium", "high", "urgent"] = "medium"
    kind: Literal["task", "approval", "milestone", "inspection", "permit"] = "task"
    inspection_type: str | None = None
    permit_type: str | None = None


class Template(BaseModel):
    key: str
    name: str
    description: str = ""
    project_type: str | None = None
    phases: list[TemplatePhase]
    tasks: list[TemplateTask]

    @model_validator(mode="after")
    def _check(self) -> "Template":
        phase_keys = [p.key for p in self.phases]
        task_keys = [t.key for t in self.tasks]
        for label, keys in (("phase", phase_keys), ("task", task_keys)):
            dupes = {k for k in keys if keys.count(k) > 1}
            if dupes:
                raise ValueError(f"duplicate {label} keys: {sorted(dupes)}")
        known = set(task_keys)
        for t in self.tasks:
            if t.phase not in phase_keys:
                raise ValueError(f"task {t.key!r} uses unknown phase {t.phase!r}")
            missing = [d for d in t.depends_on if d not in known]
            if missing:
                raise ValueError(f"task {t.key!r} depends on unknown tasks {missing}")
            if t.kind == "inspection" and not t.inspection_type:
                raise ValueError(f"inspection task {t.key!r} needs an inspection_type")
            if t.kind == "permit" and not t.permit_type:
                raise ValueError(f"permit task {t.key!r} needs a permit_type")
        # Reject cycles (depth-first search with colouring).
        deps = {t.key: t.depends_on for t in self.tasks}
        state: dict[str, int] = {}

        def visit(k: str, path: list[str]) -> None:
            if state.get(k) == 1:
                raise ValueError(f"dependency cycle: {' -> '.join(path + [k])}")
            if state.get(k) == 2:
                return
            state[k] = 1
            for d in deps[k]:
                visit(d, path + [k])
            state[k] = 2

        for k in deps:
            visit(k, [])
        return self


def _t(key, title, phase, roles, days, deps=(), kind="task", **kw) -> dict:
    return {"key": key, "title": title, "phase": phase, "roles": list(roles), "duration_days": days,
            "depends_on": list(deps), "kind": kind, **kw}


def _insp(key, title, phase, itype, deps, **kw) -> dict:
    return _t(key, title, phase, ["inspection"], 1, deps, "inspection", inspection_type=itype,
              priority="high", **kw)


def _permit(key, title, phase, ptype, deps, roles=("permit",), **kw) -> dict:
    return _t(key, title, phase, roles, 1, deps, "permit", permit_type=ptype, priority="high", **kw)


PHASES = [
    {"key": "predesign", "name": "1. Pre-design & feasibility",
     "description": "Owner needs, site conditions, zoning, feasibility and preliminary budget."},
    {"key": "design", "name": "2. Design",
     "description": "Schematic design, design development and construction documents."},
    {"key": "permitting", "name": "3. Permitting",
     "description": "Permit applications, plan review and approvals."},
    {"key": "procurement", "name": "4. Bidding & procurement",
     "description": "Bid packages, subcontractor selection, contract and long-lead orders."},
    {"key": "preconstruction", "name": "5. Pre-construction",
     "description": "Baseline schedule, site logistics, safety plan and mobilization."},
    {"key": "construction", "name": "6. Construction",
     "description": "Site work through finishes, with required inspections as gates."},
    {"key": "closeout", "name": "7. Closeout",
     "description": "Punch list, final inspections, occupancy, closeout documents and handover."},
    {"key": "warranty", "name": "8. Warranty & post-occupancy",
     "description": "Warranty period follow-up and lessons learned."},
]


def _residential_new() -> dict:
    tasks = [
        # 1. Pre-design
        _t("brief", "Owner needs & program interview", "predesign", ["project_manager", "architect"], 2,
           description="Rooms, sizes, style, must-haves, budget range and target move-in date."),
        _t("site_survey", "Boundary & topographic survey", "predesign", ["engineer"], 5, ["brief"]),
        _t("soils", "Geotechnical / soils report", "predesign", ["engineer"], 10, ["brief"]),
        _t("zoning", "Zoning, setbacks, easements & HOA review", "predesign", ["architect", "permit"], 3,
           ["site_survey"]),
        _t("feasibility", "Feasibility study & preliminary budget", "predesign", ["estimator"], 3,
           ["zoning", "soils"]),
        _t("go_decision", "Owner go/no-go decision", "predesign", ["owner"], 0, ["feasibility"], "approval",
           priority="high"),
        _t("design_contract", "Execute design agreement", "predesign", ["project_manager", "owner"], 2,
           ["go_decision"]),
        # 2. Design
        _t("schematic", "Schematic design (floor plans, massing, elevations)", "design", ["architect"], 15,
           ["design_contract"]),
        _t("sd_approval", "Owner approval of schematic design", "design", ["owner"], 0, ["schematic"], "approval"),
        _t("design_dev", "Design development (materials, systems, selections)", "design", ["architect"], 15,
           ["sd_approval"]),
        _t("structural", "Structural engineering & calculations", "design", ["engineer"], 10, ["design_dev"]),
        _t("mep_design", "MEP design & energy compliance", "design", ["engineer"], 10, ["design_dev"]),
        _t("dd_estimate", "Design development cost estimate", "design", ["estimator"], 5, ["design_dev"]),
        _t("cds", "Construction documents & specifications", "design", ["architect"], 20,
           ["structural", "mep_design", "dd_estimate"]),
        _t("cd_approval", "Owner approval of construction documents", "design", ["owner"], 0, ["cds"],
           "approval", priority="high"),
        # 3. Permitting
        _t("permit_app", "Prepare & submit building permit application", "permitting", ["permit"], 3,
           ["cd_approval"]),
        _t("plan_review", "Plan review & respond to corrections", "permitting", ["permit", "architect"], 20,
           ["permit_app"]),
        _permit("building_permit", "Building permit issued", "permitting", "building", ["plan_review"]),
        _t("utility_apps", "Utility service applications (power, water, sewer, gas)", "permitting",
           ["project_manager"], 5, ["cd_approval"]),
        # 4. Bidding & procurement
        _t("bid_packages", "Prepare bid packages & scopes of work", "procurement", ["estimator"], 5,
           ["cd_approval"]),
        _t("bidding", "Solicit, level & recommend subcontractor bids", "procurement",
           ["estimator", "procurement"], 15, ["bid_packages"]),
        _t("construction_contract", "Final budget & construction contract executed", "procurement",
           ["owner", "project_manager"], 3, ["bidding"], "approval", priority="high"),
        _t("subcontracts", "Award subcontracts; verify licenses & insurance", "procurement", ["procurement"], 5,
           ["construction_contract"]),
        _t("long_lead", "Order long-lead items (windows, trusses, HVAC equipment)", "procurement",
           ["procurement"], 2, ["subcontracts"]),
        # 5. Pre-construction
        _t("baseline_schedule", "Baseline construction schedule", "preconstruction",
           ["scheduler", "project_manager"], 3, ["subcontracts"]),
        _t("site_plan", "Site logistics & safety plan", "preconstruction", ["superintendent", "safety"], 2,
           ["subcontracts"]),
        _t("precon_meeting", "Pre-construction meeting", "preconstruction",
           ["project_manager", "superintendent"], 1, ["baseline_schedule", "site_plan", "building_permit"]),
        _t("mobilize", "Mobilize: temp power, fencing, toilets, erosion control", "preconstruction",
           ["superintendent", "site_work"], 3, ["precon_meeting"]),
        # 6. Construction
        _t("layout", "Survey staking & building layout", "construction", ["site_work"], 2, ["mobilize"]),
        _t("excavation", "Clearing, grading & excavation", "construction", ["site_work"], 5, ["layout"]),
        _t("underground", "Underground utilities & under-slab plumbing", "construction",
           ["plumbing", "site_work"], 5, ["excavation"]),
        _t("foundation_prep", "Footings & foundation: forms, rebar, vapor barrier", "construction",
           ["concrete"], 5, ["underground"]),
        _insp("foundation_insp", "Foundation & under-slab inspection", "construction", "foundation",
              ["foundation_prep"]),
        _t("pour", "Pour foundation / slab & cure", "construction", ["concrete"], 7, ["foundation_insp"]),
        _t("framing", "Framing: floors, walls, roof structure", "construction", ["framing"], 15,
           ["pour", "long_lead"]),
        _t("dry_in", "Sheathing, windows & exterior doors (dry-in)", "construction", ["framing"], 5,
           ["framing"]),
        _t("roofing", "Roofing", "construction", ["roofing"], 5, ["dry_in"]),
        _t("rough_plumbing", "Rough plumbing", "construction", ["plumbing"], 5, ["dry_in"]),
        _t("rough_electrical", "Rough electrical & low voltage", "construction", ["electrical"], 5, ["dry_in"]),
        _t("rough_hvac", "Rough HVAC & ductwork", "construction", ["hvac"], 5, ["dry_in"]),
        _insp("rough_insp", "Framing & MEP rough-in inspection", "construction", "framing_rough_in",
              ["roofing", "rough_plumbing", "rough_electrical", "rough_hvac"]),
        _t("insulation", "Insulation", "construction", ["insulation"], 3, ["rough_insp"]),
        _insp("insulation_insp", "Insulation inspection", "construction", "insulation", ["insulation"]),
        _t("drywall", "Drywall: hang, tape & texture", "construction", ["drywall"], 10, ["insulation_insp"]),
        _t("exterior", "Exterior siding / stucco, trim & paint", "construction", ["finishes"], 10,
           ["rough_insp"]),
        _t("cabinets_trim", "Interior doors, trim & cabinets", "construction", ["finishes"], 8, ["drywall"]),
        _t("paint", "Interior paint", "construction", ["finishes"], 5, ["cabinets_trim"]),
        _t("countertops", "Countertops (template & install)", "construction", ["finishes"], 5,
           ["cabinets_trim"]),
        _t("flooring", "Flooring & tile", "construction", ["finishes"], 7, ["paint"]),
        _t("mep_trim", "Plumbing, electrical & HVAC trim-out", "construction",
           ["plumbing", "electrical", "hvac"], 5, ["flooring", "countertops", "utility_apps"],
           description="Needs permanent utility service for fixture testing and equipment startup."),
        _t("appliances", "Appliances & fixtures", "construction", ["finishes"], 2, ["mep_trim"]),
        _t("flatwork", "Driveway, flatwork & landscaping", "construction", ["site_work", "landscaping"], 7,
           ["exterior"]),
        _t("final_clean", "Final construction clean", "construction", ["superintendent"], 2,
           ["appliances", "flatwork"]),
        # 7. Closeout
        _insp("final_insp", "Final building inspection", "closeout", "final", ["final_clean"]),
        _permit("co", "Certificate of occupancy issued", "closeout", "certificate_of_occupancy", ["final_insp"]),
        _t("punch_walk", "Owner punch walk & punch list", "closeout", ["project_manager", "owner"], 1,
           ["final_clean"]),
        _t("punch_complete", "Complete punch list", "closeout", ["superintendent"], 5, ["punch_walk"]),
        _t("closeout_docs", "Closeout documents: warranties, manuals, as-builts, lien releases", "closeout",
           ["project_manager", "closeout"], 3, ["punch_complete"]),
        _t("handover", "Owner handover, keys & final payment", "closeout", ["project_manager", "owner"], 1,
           ["co", "punch_complete", "closeout_docs"], "milestone", priority="high"),
        # 8. Warranty
        _t("warranty_walk", "11-month warranty walkthrough", "warranty", ["project_manager"], 1, ["handover"],
           description="Schedule about 11 months after handover; log any items as punch items."),
        _t("lessons_learned", "Lessons learned & project closeout review", "warranty", ["project_manager"], 1,
           ["handover"]),
    ]
    return {"key": "residential_new", "name": "Residential new construction",
            "description": "Single-family home from owner brief to warranty, wood-frame on slab or foundation.",
            "project_type": "residential_new", "phases": PHASES, "tasks": tasks}


def _residential_remodel() -> dict:
    tasks = [
        _t("brief", "Owner goals, scope & budget interview", "predesign", ["project_manager"], 1),
        _t("existing", "Measure & document existing conditions", "predesign", ["architect"], 3, ["brief"]),
        _t("hazmat", "Asbestos / lead testing (pre-1980 homes)", "predesign", ["engineer"], 5, ["existing"]),
        _t("feasibility", "Feasibility & preliminary budget", "predesign", ["estimator"], 2, ["existing"]),
        _t("go_decision", "Owner go/no-go decision", "predesign", ["owner"], 0, ["feasibility", "hazmat"],
           "approval", priority="high"),
        _t("design", "Remodel design & layout", "design", ["architect"], 10, ["go_decision"]),
        _t("selections", "Finish & fixture selections", "design", ["owner", "architect"], 5, ["design"]),
        _t("engineering", "Structural engineering (if walls move)", "design", ["engineer"], 5, ["design"]),
        _t("cds", "Construction drawings & scope of work", "design", ["architect"], 7,
           ["selections", "engineering"]),
        _t("cd_approval", "Owner approval of drawings & scope", "design", ["owner"], 0, ["cds"], "approval"),
        _t("permit_app", "Submit permit application", "permitting", ["permit"], 2, ["cd_approval"]),
        _permit("building_permit", "Building permit issued", "permitting", "building", ["permit_app"]),
        _t("bids", "Trade bids & final pricing", "procurement", ["estimator", "procurement"], 7, ["cd_approval"]),
        _t("contract", "Construction contract executed", "procurement", ["owner", "project_manager"], 2,
           ["bids"], "approval", priority="high"),
        _t("order_materials", "Order materials, cabinets & fixtures", "procurement", ["procurement"], 2,
           ["contract"]),
        _t("schedule", "Construction schedule & occupant plan", "preconstruction",
           ["scheduler", "project_manager"], 1, ["contract"]),
        _t("protection", "Dust control, floor protection & site setup", "preconstruction",
           ["superintendent"], 1, ["schedule", "building_permit"]),
        _t("demo", "Selective demolition & haul-off", "construction", ["site_work"], 3, ["protection"]),
        _t("structural_work", "Structural changes & framing", "construction", ["framing"], 5, ["demo"]),
        _t("rough_mep", "Rough plumbing, electrical & HVAC", "construction",
           ["plumbing", "electrical", "hvac"], 5, ["structural_work"]),
        _insp("rough_insp", "Framing & rough-in inspection", "construction", "framing_rough_in", ["rough_mep"]),
        _t("insulation", "Insulation", "construction", ["insulation"], 1, ["rough_insp"]),
        _insp("insulation_insp", "Insulation inspection", "construction", "insulation", ["insulation"]),
        _t("drywall", "Drywall & texture", "construction", ["drywall"], 5, ["insulation_insp"]),
        _t("cabinets", "Cabinets, trim & doors", "construction", ["finishes"], 4, ["drywall", "order_materials"]),
        _t("tile_floor", "Tile & flooring", "construction", ["finishes"], 4, ["cabinets"]),
        _t("paint", "Paint", "construction", ["finishes"], 3, ["tile_floor"]),
        _t("trim_out", "Fixtures & MEP trim-out", "construction", ["plumbing", "electrical", "hvac"], 2,
           ["paint"]),
        _t("clean", "Final clean & remove protection", "construction", ["superintendent"], 1, ["trim_out"]),
        _insp("final_insp", "Final inspection", "closeout", "final", ["clean"]),
        _t("punch", "Punch walk & complete punch list", "closeout", ["project_manager", "superintendent"], 3,
           ["clean"]),
        _t("closeout_docs", "Warranties, manuals & lien releases", "closeout", ["project_manager", "closeout"], 1,
           ["punch"]),
        _t("handover", "Owner handover & final payment", "closeout", ["project_manager", "owner"], 1,
           ["final_insp", "closeout_docs"], "milestone", priority="high"),
        _t("warranty_walk", "Warranty follow-up visit", "warranty", ["project_manager"], 1, ["handover"],
           description="Schedule a few months after handover."),
    ]
    return {"key": "residential_remodel", "name": "Residential remodel / addition",
            "description": "Kitchen, bath, whole-house remodel or addition in an existing home.",
            "project_type": "residential_remodel", "phases": PHASES, "tasks": tasks}


def _commercial_new() -> dict:
    tasks = [
        # 1. Pre-design
        _t("program", "Owner program & project charter", "predesign", ["project_manager", "architect"], 5),
        _t("due_diligence", "Site due diligence: title, ALTA survey, utilities", "predesign",
           ["engineer", "project_manager"], 15, ["program"]),
        _t("phase1_esa", "Phase I environmental site assessment", "predesign", ["engineer"], 15, ["program"]),
        _t("geotech", "Geotechnical investigation", "predesign", ["engineer"], 15, ["program"]),
        _t("entitlements", "Zoning & entitlement review (use, parking, traffic)", "predesign",
           ["architect", "permit"], 10, ["due_diligence"]),
        _t("feasibility", "Feasibility study, pro forma & budget", "predesign", ["estimator", "project_manager"], 10,
           ["entitlements", "phase1_esa", "geotech"]),
        _t("go_decision", "Owner investment decision", "predesign", ["owner"], 0, ["feasibility"], "approval",
           priority="high"),
        _t("design_contracts", "Execute A/E and CM/GC preconstruction agreements", "predesign",
           ["project_manager", "owner"], 10, ["go_decision"]),
        # 2. Design
        _t("schematic", "Schematic design", "design", ["architect"], 25, ["design_contracts"]),
        _t("sd_estimate", "Schematic design estimate", "design", ["estimator"], 5, ["schematic"]),
        _t("sd_approval", "Owner approval of schematic design", "design", ["owner"], 0, ["sd_estimate"],
           "approval"),
        _t("design_dev", "Design development (all disciplines)", "design", ["architect", "engineer"], 30,
           ["sd_approval"]),
        _t("dd_estimate", "Design development estimate & value engineering", "design", ["estimator"], 10,
           ["design_dev"]),
        _t("code_review", "Code, fire & accessibility (ADA) review", "design", ["architect"], 5, ["design_dev"]),
        _t("cds", "Construction documents: arch, structural, civil, MEP, fire protection", "design",
           ["architect", "engineer"], 40, ["dd_estimate", "code_review"]),
        _t("cd_approval", "Owner approval of construction documents", "design", ["owner"], 0, ["cds"],
           "approval", priority="high"),
        # 3. Permitting
        _t("permit_app", "Submit building permit application", "permitting", ["permit"], 5, ["cd_approval"]),
        _t("plan_review", "Plan review & correction cycles", "permitting", ["permit", "architect", "engineer"], 40,
           ["permit_app"]),
        _permit("grading_permit", "Grading / site development permit issued", "permitting", "grading",
                ["plan_review"]),
        _permit("building_permit", "Building permit issued", "permitting", "building", ["plan_review"]),
        _permit("fire_permit", "Fire sprinkler & alarm permits issued", "permitting", "fire", ["plan_review"],
                roles=("permit", "fire_protection")),
        _t("swppp", "Stormwater pollution prevention plan (SWPPP) & NOI", "permitting", ["engineer", "permit"], 10,
           ["cd_approval"]),
        _t("utility_agreements", "Utility service agreements & will-serve letters", "permitting",
           ["project_manager", "engineer"], 20, ["cd_approval"]),
        # 4. Bidding & procurement
        _t("bid_packages", "Trade bid packages", "procurement", ["estimator"], 10, ["cd_approval"]),
        _t("bidding", "Bid, level & interview trade partners", "procurement", ["estimator", "procurement"], 20,
           ["bid_packages"]),
        _t("gmp", "GMP / final construction contract executed", "procurement", ["owner", "project_manager"], 5,
           ["bidding"], "approval", priority="high"),
        _t("buyout", "Subcontract buyout; verify bonds, licenses & insurance", "procurement", ["procurement"], 10,
           ["gmp"]),
        _t("submittal_register", "Build submittal register & log", "procurement", ["project_manager"], 3,
           ["buyout"]),
        _t("long_lead", "Release long-lead items (steel, switchgear, RTUs, elevators)", "procurement",
           ["procurement"], 5, ["buyout"], priority="high"),
        # 5. Pre-construction
        _t("baseline_schedule", "CPM baseline schedule", "preconstruction", ["scheduler"], 5, ["buyout"]),
        _t("safety_plan", "Site-specific safety plan & logistics", "preconstruction", ["safety", "superintendent"],
           3, ["buyout"]),
        _t("qa_plan", "Quality control & special inspection plan", "preconstruction",
           ["project_manager", "engineer"], 3, ["buyout"]),
        _t("precon_meeting", "Pre-construction meeting", "preconstruction", ["project_manager", "superintendent"],
           1, ["baseline_schedule", "safety_plan", "qa_plan", "submittal_register", "building_permit",
               "grading_permit", "swppp"]),
        _t("mobilize", "Mobilize: trailers, temp utilities, fencing, BMPs", "preconstruction",
           ["superintendent", "site_work"], 5, ["precon_meeting"]),
        # 6. Construction
        _t("demo_clear", "Demolition, clearing & rough grading", "construction", ["site_work"], 10, ["mobilize"]),
        _t("site_utilities", "Site utilities: storm, sewer, water, dry utilities", "construction",
           ["site_work", "plumbing", "electrical"], 20, ["demo_clear", "utility_agreements"]),
        _t("foundations", "Foundations: footings, grade beams, piers", "construction", ["concrete"], 15,
           ["demo_clear"]),
        _insp("foundation_insp", "Foundation special inspection", "construction", "foundation", ["foundations"]),
        _t("underslab", "Under-slab MEP & slab on grade", "construction", ["plumbing", "electrical", "concrete"],
           10, ["foundation_insp"]),
        _t("structure", "Structural frame (steel / concrete / wood)", "construction", ["steel", "framing"], 30,
           ["underslab", "long_lead"]),
        _insp("structural_insp", "Structural special inspections (welds, bolts, connections)", "construction",
              "structural", ["structure"]),
        _t("envelope", "Building envelope: exterior walls, glazing, air barrier", "construction",
           ["framing", "finishes"], 30, ["structural_insp"]),
        _t("roofing", "Roofing system", "construction", ["roofing"], 10, ["structural_insp"]),
        _t("rough_mep", "MEP rough-in", "construction", ["plumbing", "electrical", "hvac"], 30, ["roofing"]),
        _t("fire_sprinkler", "Fire sprinkler & fire alarm rough-in", "construction", ["fire_protection"], 15,
           ["roofing", "fire_permit"]),
        _insp("rough_insp", "MEP & fire rough-in / above-ceiling inspections", "construction",
              "mep_fire_rough_in", ["rough_mep", "fire_sprinkler"]),
        _t("interiors", "Interior framing, insulation & drywall", "construction",
           ["framing", "insulation", "drywall"], 25, ["rough_insp", "envelope"]),
        _t("finishes", "Ceilings, flooring, paint, doors & millwork", "construction", ["finishes"], 25,
           ["interiors"]),
        _t("mep_trim", "MEP trim-out & equipment startup", "construction", ["plumbing", "electrical", "hvac"], 10,
           ["finishes"]),
        _t("elevator", "Elevator installation", "construction", ["superintendent"], 20, ["structural_insp"]),
        _insp("elevator_insp", "Elevator state inspection", "construction", "elevator", ["elevator"]),
        _t("sitework_final", "Paving, curbs, striping & landscaping", "construction",
           ["site_work", "landscaping"], 15, ["site_utilities", "envelope"]),
        _t("commissioning", "Commissioning: functional testing & TAB", "construction", ["commissioning", "hvac"],
           10, ["mep_trim"]),
        _insp("fire_final", "Fire marshal final (sprinkler & alarm acceptance)", "construction", "fire_final",
              ["commissioning", "fire_sprinkler"]),
        _t("final_clean", "Final cleaning", "construction", ["superintendent"], 3,
           ["commissioning", "sitework_final", "elevator_insp"]),
        # 7. Closeout
        _insp("final_insp", "Final building inspection", "closeout", "final", ["final_clean", "fire_final"]),
        _permit("co", "Certificate of occupancy issued", "closeout", "certificate_of_occupancy", ["final_insp"]),
        _t("punch", "Punch list walk & completion", "closeout", ["project_manager", "superintendent"], 10,
           ["final_clean"]),
        _t("training", "Owner O&M training", "closeout", ["commissioning", "project_manager"], 3, ["commissioning"]),
        _t("closeout_docs", "Closeout: as-builts, O&M manuals, warranties, attic stock, lien releases", "closeout",
           ["project_manager", "closeout"], 10, ["punch"]),
        _t("substantial_completion", "Substantial completion & owner occupancy", "closeout",
           ["owner", "project_manager"], 0, ["co", "punch"], "milestone", priority="high"),
        _t("final_completion", "Final completion & final payment / retention release", "closeout",
           ["owner", "project_manager"], 1, ["substantial_completion", "closeout_docs", "training"], "milestone"),
        # 8. Warranty
        _t("warranty_review", "11-month warranty review", "warranty", ["project_manager"], 1, ["final_completion"],
           description="Schedule about 11 months after substantial completion."),
        _t("post_occupancy", "Post-occupancy evaluation & lessons learned", "warranty", ["project_manager"], 2,
           ["final_completion"]),
    ]
    return {"key": "commercial_new", "name": "Commercial new construction",
            "description": "Ground-up commercial building delivered CM/GC style, from feasibility to warranty.",
            "project_type": "commercial_new", "phases": PHASES, "tasks": tasks}


def _commercial_ti() -> dict:
    tasks = [
        _t("lease_review", "Lease work letter & landlord requirements review", "predesign",
           ["project_manager"], 3),
        _t("existing", "Existing conditions survey (MEP, fire, accessibility)", "predesign",
           ["architect", "engineer"], 5, ["lease_review"]),
        _t("budget", "Preliminary budget & schedule", "predesign", ["estimator"], 3, ["existing"]),
        _t("go_decision", "Tenant approval to proceed", "predesign", ["owner"], 0, ["budget"], "approval"),
        _t("space_plan", "Space plan & test fit", "design", ["architect"], 10, ["go_decision"]),
        _t("cds", "Construction documents incl. MEP & fire protection", "design", ["architect", "engineer"], 20,
           ["space_plan"]),
        _t("landlord_approval", "Landlord approval of drawings", "design", ["owner"], 5, ["cds"], "approval"),
        _t("permit_app", "Submit TI permit application", "permitting", ["permit"], 2, ["landlord_approval"]),
        _permit("building_permit", "TI building permit issued", "permitting", "building", ["permit_app"]),
        _permit("fire_permit", "Fire sprinkler / alarm modification permits", "permitting", "fire", ["permit_app"],
                roles=("permit", "fire_protection")),
        _t("bids", "Bid & award subcontracts", "procurement", ["estimator", "procurement"], 10,
           ["landlord_approval"]),
        _t("contract", "Construction contract executed", "procurement", ["owner", "project_manager"], 2, ["bids"],
           "approval", priority="high"),
        _t("long_lead", "Order long-lead items (doors/hardware, light fixtures, RTUs)", "procurement",
           ["procurement"], 2, ["contract"]),
        _t("building_rules", "Coordinate building rules, after-hours work & COIs", "preconstruction",
           ["project_manager", "superintendent"], 2, ["contract"]),
        _t("mobilize", "Mobilize & protect common areas", "preconstruction", ["superintendent"], 1,
           ["building_rules", "building_permit"]),
        _t("demo", "Interior demolition", "construction", ["site_work"], 5, ["mobilize"]),
        _t("framing", "Metal stud framing & blocking", "construction", ["framing"], 7, ["demo"]),
        _t("rough_mep", "MEP rough-in", "construction", ["plumbing", "electrical", "hvac"], 10, ["framing"]),
        _t("sprinkler_mods", "Fire sprinkler & alarm modifications", "construction", ["fire_protection"], 5,
           ["framing", "fire_permit"]),
        _insp("above_ceiling", "Framing & above-ceiling inspections", "construction", "above_ceiling",
              ["rough_mep", "sprinkler_mods"]),
        _t("drywall", "Insulation & drywall", "construction", ["insulation", "drywall"], 7, ["above_ceiling"]),
        _t("ceilings", "Ceiling grid & tile", "construction", ["finishes"], 4, ["drywall"]),
        _t("finishes", "Paint, flooring, doors, millwork", "construction", ["finishes"], 10,
           ["ceilings", "long_lead"]),
        _t("mep_trim", "MEP trim & HVAC balancing", "construction", ["plumbing", "electrical", "hvac"], 4,
           ["finishes"]),
        _insp("fire_test", "Fire alarm & sprinkler acceptance test", "construction", "fire_final", ["mep_trim"]),
        _t("clean", "Final clean", "construction", ["superintendent"], 1, ["fire_test"]),
        _insp("final_insp", "Final inspection", "closeout", "final", ["clean"]),
        _permit("co", "Certificate of occupancy / completion issued", "closeout", "certificate_of_occupancy",
                ["final_insp"]),
        _t("punch", "Punch list", "closeout", ["project_manager", "superintendent"], 3, ["clean"]),
        _t("closeout_docs", "As-builts, warranties, lien releases to landlord & tenant", "closeout",
           ["project_manager", "closeout"], 3, ["punch"]),
        _t("move_in", "Tenant move-in", "closeout", ["owner"], 0, ["co", "punch"], "milestone", priority="high"),
        _t("final_completion", "Final completion & retention release", "closeout", ["owner", "project_manager"], 1,
           ["move_in", "closeout_docs"], "milestone"),
        _t("warranty_review", "Warranty follow-up", "warranty", ["project_manager"], 1, ["final_completion"]),
    ]
    return {"key": "commercial_tenant_improvement", "name": "Commercial tenant improvement",
            "description": "Interior build-out of leased commercial space.",
            "project_type": "commercial_tenant_improvement", "phases": PHASES, "tasks": tasks}


BUILTIN_TEMPLATES: dict[str, Template] = {
    t.key: t
    for t in (Template.model_validate(f()) for f in (
        _residential_new, _residential_remodel, _commercial_new, _commercial_ti))
}
