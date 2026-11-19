"""Source-led scientific records and open research tasks, independent of benchmarks."""
from __future__ import annotations

from typing import Annotated, Literal
from pydantic import Field, model_validator

from astro_pipeline.models.evidence import Contract, EvidenceSpan, EvidenceTarget, ExtractorProvenance, Name, Text

ItemKind = Literal["scientific_record", "research_task"]
Origin = Literal["paper_report", "prior_work", "catalog_record", "agent_calculation", "agent_synthesis", "synthetic_example"]


class ResearchTarget(EvidenceTarget):
    name: Name
    subject_scope: Literal["source", "component", "system"] = "source"
    identity_basis: Text


class PaperEvidence(Contract):
    id: Name
    kind: Literal["paper"] = "paper"
    paper_version_id: Name
    span: EvidenceSpan


class CatalogField(Contract):
    column: Name
    unit: Name


class CatalogEvidence(Contract):
    id: Name
    kind: Literal["catalog"] = "catalog"
    target: EvidenceTarget
    run_id: Name
    member_ordinal: int = Field(ge=0, strict=True)
    fields: list[CatalogField] = Field(min_length=1, max_length=100)


class Measurement(Contract):
    quantity: Name
    value: float | None = Field(default=None, strict=True, allow_inf_nan=False)
    unit: Name
    reported_text: Text
    uncertainty: str = Field(default="", max_length=2000)
    limit_type: Literal["measurement", "upper_limit", "lower_limit", "non_detection", "missing"] = "measurement"
    observed_at: str = Field(default="", max_length=1000)
    instrument: str = Field(default="", max_length=1000)
    band: str = Field(default="", max_length=1000)


class ResearchUnit(Contract):
    id: Name
    kind: Literal["observation", "method", "analysis", "result", "limitation"]
    statement: Text
    origin: Origin
    attribution: Text
    target_keys: list[Name] = Field(min_length=1, max_length=50)
    evidence_ids: list[Name] = Field(default_factory=list, max_length=100)
    depends_on: list[Name] = Field(default_factory=list, max_length=100)
    assumptions: list[Text] = Field(default_factory=list, max_length=30)
    measurement: Measurement | None = None
    calculation: str = Field(default="", max_length=20000, description="Formula or reproducible code as text; never executed by the server.")


class ResearchTask(Contract):
    objective: Text
    expected_outputs: list[Text] = Field(min_length=1, max_length=30)
    procedure: list[Text] = Field(default_factory=list, max_length=50)
    status: Literal["planned", "in_progress", "completed"] = "planned"


class ResearchItem(Contract):
    schema_version: Literal["research-item-1"] = "research-item-1"
    kind: ItemKind
    title: Name
    topic: Name
    question: Text
    summary: Text
    targets: list[ResearchTarget] = Field(min_length=1, max_length=50)
    evidence: list[Annotated[PaperEvidence | CatalogEvidence, Field(discriminator="kind")]] = Field(default_factory=list, max_length=100)
    units: list[ResearchUnit] = Field(default_factory=list, max_length=100)
    related_revision_ids: list[Name] = Field(default_factory=list, max_length=50, description="Immutable scientific-record/task revisions used as inputs; not mutable latest IDs.")
    task: ResearchTask | None = None
    author: ExtractorProvenance

    @model_validator(mode="after")
    def check_links(self):
        keys = [f"{t.release}:{t.source_id}" for t in self.targets]
        evidence = [e.id for e in self.evidence]
        evidence_kinds = {e.id: e.kind for e in self.evidence}
        if len(keys) != len(set(keys)) or len(evidence) != len(set(evidence)):
            raise ValueError("Target identities and evidence IDs must be unique")
        if len(self.related_revision_ids) != len(set(self.related_revision_ids)):
            raise ValueError("Repeated related revision")
        if not evidence and not self.related_revision_ids:
            raise ValueError("A research item needs pinned evidence or referenced record revisions")
        if (self.kind == "research_task") != (self.task is not None):
            raise ValueError("Research tasks require a task definition; scientific records must omit it")
        if self.kind == "scientific_record" and not self.units:
            raise ValueError("Scientific records require scientific content units")
        if self.task and self.task.status == "completed" and not self.units:
            raise ValueError("Completed tasks require recorded outcomes, not only a plan")
        seen = set()
        for unit in self.units:
            if unit.id in seen or not set(unit.depends_on) <= seen:
                raise ValueError("Unit IDs must be unique and dependencies must refer to preceding units")
            if not set(unit.target_keys) <= set(keys) or not set(unit.evidence_ids) <= set(evidence):
                raise ValueError("Unit refers to absent targets or evidence")
            if unit.origin in {"paper_report", "prior_work"} and not unit.evidence_ids:
                raise ValueError("Paper claims require direct evidence citations")
            if unit.origin in {"paper_report", "prior_work"} and not any(evidence_kinds[e] == "paper" for e in unit.evidence_ids):
                raise ValueError("Paper claims need paper evidence")
            if unit.origin == "catalog_record" and (not unit.evidence_ids or any(evidence_kinds[e] != "catalog" for e in unit.evidence_ids)):
                raise ValueError("Catalog records require direct catalog evidence")
            if unit.origin == "synthetic_example" and not unit.assumptions:
                raise ValueError("Synthetic examples require explicit assumptions")
            if unit.origin in {"agent_calculation", "agent_synthesis"} and not (unit.evidence_ids or unit.depends_on or self.related_revision_ids):
                raise ValueError("Agent analysis needs evidence or pinned input records")
            seen.add(unit.id)
        return self
