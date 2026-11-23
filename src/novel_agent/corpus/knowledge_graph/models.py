from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class GraphError(ValueError):
    def __init__(self, code: str, message: str, status: int = 409):
        super().__init__(message)
        self.code, self.status = code, status


class GraphOutputLimitError(GraphError):
    def __init__(self):
        super().__init__("graph_model_output_limit", "模型输出达到上限，当前片段需要缩小或调整输出预算。")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Anchor(StrictModel):
    span_id: str
    quote: str = Field(min_length=1, max_length=600)
    occurrence: int = Field(default=1, ge=1)


class ExtractedEntity(StrictModel):
    local_id: str
    type: Literal["person", "place", "organization", "item", "skill", "concept"]
    name: str = Field(min_length=1, max_length=100)
    mentions: list[Anchor] = Field(min_length=1)
    description: str = Field(default="", max_length=300)
    plot_relevant: bool = False
    importance_evidence: list[Anchor] = Field(default_factory=list, max_length=3)


class ExtractedRelation(StrictModel):
    source: str
    target: str
    type: str
    polarity: Literal["affirmed", "denied"] = "affirmed"
    statement_mode: Literal["narration", "character_claim"] = "narration"
    evidence: list[Anchor] = Field(min_length=1, max_length=3)


class IdentityLink(StrictModel):
    source: str
    target: str
    evidence: list[Anchor] = Field(min_length=1, max_length=3)


class ExtractionBatch(StrictModel):
    entities: list[ExtractedEntity] = Field(default_factory=list)
    relations: list[ExtractedRelation] = Field(default_factory=list)
    identity_links: list[IdentityLink] = Field(default_factory=list)


class CompactEntity(StrictModel):
    local_id: str
    name: str = Field(min_length=1, max_length=100)
    type: Literal["person", "place", "organization", "item", "skill", "concept"]
    span_id: str
    occurrence: int = Field(default=1, ge=1)
    plot_relevant: bool = False
    importance_evidence_ids: list[str] = Field(default_factory=list, max_length=3)


class CompactRelation(StrictModel):
    source: str
    target: str
    type: str
    evidence_ids: list[str] = Field(min_length=1, max_length=3)
    polarity: Literal["affirmed", "denied"] = "affirmed"
    statement_mode: Literal["narration", "character_claim"] = "narration"


class CompactIdentityLink(StrictModel):
    source: str
    target: str
    evidence_ids: list[str] = Field(min_length=1, max_length=3)


class CompactExtractionBatch(StrictModel):
    entities: list[CompactEntity] = Field(default_factory=list)
    relations: list[CompactRelation] = Field(default_factory=list)
    identity_links: list[CompactIdentityLink] = Field(default_factory=list)


class ResolutionDecision(StrictModel):
    decision: Literal["merge", "new", "ambiguous"]
    candidate_id: str | None = None
    supporting_mentions: list[str] = Field(default_factory=list)


class EntityResolutionDecision(ResolutionDecision):
    entity_ref: str


class BatchResolutionDecision(StrictModel):
    decisions: list[EntityResolutionDecision] = Field(max_length=16)


class NameForm(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    kind: Literal["full_name", "given_name", "address", "alias", "proper_name"] = "proper_name"


class PersonName(StrictModel):
    family_name: str | None = None
    given_name: str | None = None


class CatalogProfileProposal(StrictModel):
    anchor_mention_id: str
    canonical_name: str = Field(min_length=1, max_length=100)
    name_forms: list[NameForm] = Field(min_length=1, max_length=24)
    supporting_mentions: list[str] = Field(min_length=1, max_length=24)
    person_name: PersonName | None = None
    description: str = Field(default="", max_length=300)


class CatalogDecision(StrictModel):
    profiles: list[CatalogProfileProposal] = Field(default_factory=list, max_length=16)


class MentionBindingDecision(StrictModel):
    mention_id: str
    entity_id: str | None = None
    supporting_mentions: list[str] = Field(default_factory=list, max_length=8)


class MentionBindingBatch(StrictModel):
    bindings: list[MentionBindingDecision] = Field(default_factory=list, max_length=16)


class BuildRequest(StrictModel):
    mode: Literal["start", "resume", "rebuild"] = "start"
    job_id: str | None = None

    @model_validator(mode="after")
    def validate_job(self):
        if (self.mode == "resume") != (self.job_id is not None):
            raise ValueError("继续构建必须传 job_id；其他模式不允许传 job_id。")
        return self
