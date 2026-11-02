"""Version-one annotation contract. Scientific judgements remain unreviewed."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from astro_pipeline.library.identity import source_identity

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
Name = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]
Role = Literal[
    "mentioned", "in_sample", "analysed", "reports_result",
    "cites_prior_result", "comparison", "calibrator",
]
Assessment = Literal["evidence_found", "uncertain", "not_found_in_inspected_blocks"]
MembershipStatus = Literal["member", "candidate", "non_member", "uncertain"]

ROLE_DEFINITIONS = {
    "mentioned": "Explicitly names the object in text, a table or a caption.",
    "in_sample": "Includes the object in this paper's working sample; not proof of association membership.",
    "analysed": "This work measures, fits, models or classifies the object.",
    "reports_result": "This work reports an object-specific result, including limits or non-detections.",
    "cites_prior_result": "Quotes or adopts a result attributed to earlier work; not a new result of this paper.",
    "comparison": "Uses the object as a comparison object.",
    "calibrator": "Uses the object as a calibrator or reference standard.",
}


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceAlias(Contract):
    name: Name
    provenance: Text = Field(description="Where the name-to-source mapping came from; never inferred from a name hit alone.")


class EvidenceTarget(Contract):
    release: Name
    source_id: Annotated[str, StringConstraints(pattern=r"^[0-9]+$", min_length=1)]
    aliases: list[SourceAlias] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def normalize_identity(self):
        self.release, self.source_id = source_identity(self.release, self.source_id)
        return self


class EvidenceSpan(Contract):
    """An exact slice of one parsed field, not a fuzzy quote or a PDF byte offset."""

    block_id: Name
    field: Literal["text", "table_cell", "table_note", "raw"] = "text"
    start: int = Field(ge=0, strict=True, description="Zero-based Unicode code-point offset in the complete field.")
    end: int = Field(gt=0, strict=True, description="Exclusive end offset.")
    quote: str = Field(min_length=1, max_length=4000)
    row: int | None = Field(default=None, ge=0, strict=True)
    column: int | None = Field(default=None, ge=0, strict=True)
    note_index: int | None = Field(default=None, ge=0, strict=True)

    @model_validator(mode="after")
    def validate_coordinates(self):
        if self.end <= self.start or not self.quote.strip():
            raise ValueError("evidence must be a non-empty forward slice")
        if self.field == "table_cell":
            if self.row is None or self.column is None or self.note_index is not None:
                raise ValueError("table_cell needs row and column only")
        elif self.field == "table_note":
            if self.note_index is None or self.row is not None or self.column is not None:
                raise ValueError("table_note needs note_index only")
        elif any(value is not None for value in (self.row, self.column, self.note_index)):
            raise ValueError("text/raw evidence cannot specify table coordinates")
        return self


class RelationAnnotation(Contract):
    role: Role
    statement: Text = Field(description="What the cited passages support, including current versus prior work attribution.")
    evidence: list[EvidenceSpan] = Field(min_length=1, max_length=12)


class MembershipResult(Contract):
    """An attributed membership judgement; it never edits a membership catalogue."""

    kind: Literal["membership"] = "membership"
    association: Name = Field(description="Association identifier, e.g. TWA; not a geometric search region.")
    population: Name | None = None
    status: MembershipStatus
    reported_label: Name = Field(description="Literal label or phrase present in the cited evidence, including candidate markers.")
    origin: Literal["current_analysis", "prior_work"]
    attribution: Text = Field(description="Who makes the claim; identify the earlier work when origin=prior_work.")
    method: Text
    qualification: Text = Field(description="Retain the paper's limits, assumptions or uncertainty; explicitly state if none are specified.")
    evidence: list[EvidenceSpan] = Field(min_length=1, max_length=12)


class ObjectPaperAnnotation(Contract):
    target: EvidenceTarget
    paper_name: Name
    subject_scope: Literal["source", "component", "system"]
    identity_status: Literal["resolved", "ambiguous"]
    identity_basis: Text = Field(description="Annotator's mapping rationale; resolved is an assertion, not server verification.")
    identity_evidence: list[EvidenceSpan] = Field(default_factory=list, max_length=12)
    assessment: Assessment
    inspected_block_ids: list[Name] = Field(min_length=1, max_length=2000)
    relations: list[RelationAnnotation] = Field(default_factory=list, max_length=7)
    results: list[MembershipResult] = Field(default_factory=list, max_length=12)
    note: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def validate_assessment(self):
        roles = [relation.role for relation in self.relations]
        if len(set(roles)) != len(roles):
            raise ValueError("each role may occur only once per object assessment")
        if len(set(self.inspected_block_ids)) != len(self.inspected_block_ids):
            raise ValueError("inspected_block_ids must be unique")
        if self.assessment == "evidence_found":
            if not self.relations or not self.identity_evidence or self.identity_status != "resolved":
                raise ValueError("evidence_found requires roles, identity evidence and a resolved identity assertion")
        elif self.relations:
            raise ValueError("uncertain/not-found assessments cannot assert roles")
        if self.identity_status == "ambiguous" and self.assessment != "uncertain":
            raise ValueError("ambiguous identities must remain uncertain")
        if self.assessment != "evidence_found" and not self.note.strip():
            raise ValueError("uncertain/not-found assessments need an explanatory note")
        if self.results and self.assessment != "evidence_found":
            raise ValueError("structured results require evidence_found and a resolved identity")
        for result in self.results:
            required_role = "reports_result" if result.origin == "current_analysis" else "cites_prior_result"
            if required_role not in roles:
                raise ValueError(f"{result.origin} results require the {required_role} role")
            if not any(result.reported_label in span.quote for span in result.evidence):
                raise ValueError("reported_label must occur literally in result evidence")
        return self


class ExtractorProvenance(Contract):
    name: Name = Field(description="Human/agent or extraction implementation name.")
    version: Name = Field(description="Model or implementation version; use an explicit unknown value if unavailable.")
    prompt_version: Name = Field(description="Version of the annotation instructions.")


class PaperAnnotationBatch(Contract):
    ontology_version: Literal["object-paper-1", "object-paper-2"] = "object-paper-1"
    batch_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
    paper_version_id: Name
    extractor: ExtractorProvenance
    annotations: list[ObjectPaperAnnotation] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_result_version(self):
        if self.ontology_version == "object-paper-1" and any(a.results for a in self.annotations):
            raise ValueError("structured results require ontology_version=object-paper-2")
        return self
