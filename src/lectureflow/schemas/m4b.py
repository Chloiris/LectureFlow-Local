from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class M4BModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class TechnicalParagraph(M4BModel):
    schema_version: Literal["1.0"] = "1.0"
    paragraph_id: str = Field(pattern=r"^PAR[0-9]{4,}$")
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)
    title: str
    text_raw: str = Field(min_length=1)
    text_clean: str = Field(min_length=1)
    text_corrected: str = Field(min_length=1)
    source_segment_ids: list[str] = Field(min_length=1)
    correction_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    frame_ids: list[str] = Field(default_factory=list)
    packet_ids: list[str] = Field(min_length=1)
    confidence: Literal["high", "medium", "low"]
    uncertainties: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_duration(self) -> TechnicalParagraph:
        if self.end <= self.start:
            raise ValueError("paragraph end must exceed start")
        if self.end - self.start > 120.001:
            raise ValueError("technical paragraph cannot exceed 120 seconds")
        if len(set(self.source_segment_ids)) != len(self.source_segment_ids):
            raise ValueError("paragraph source segments must be unique")
        return self


class TechnicalPacket(M4BModel):
    schema_version: Literal["1.0"] = "1.0"
    packet_id: str = Field(pattern=r"^P[0-9]{4}$")
    time_range: list[float] = Field(min_length=2, max_length=2)
    primary_range: list[float] = Field(min_length=2, max_length=2)
    overlap_range: list[float] = Field(min_length=2, max_length=2)
    paragraph_ids: list[str] = Field(min_length=1)
    transcript_ids: list[str] = Field(min_length=1)
    primary_transcript_ids: list[str] = Field(min_length=1)
    frame_ids: list[str]
    formula_cue_count: int = Field(ge=0)
    technical_term_candidates: list[dict[str, object]]
    input_hashes: dict[str, str]
    packet_hash: str
    evidence_constraints: list[str]
    external_model_api_used: Literal[False] = False

    @model_validator(mode="after")
    def valid_ranges(self) -> TechnicalPacket:
        for value in (self.time_range, self.primary_range, self.overlap_range):
            if value[1] <= value[0]:
                raise ValueError("packet range must increase")
        if not (
            self.time_range[0] <= self.primary_range[0]
            and self.primary_range[1] <= self.time_range[1]
        ):
            raise ValueError("primary range must be inside packet range")
        return self


class FormulaRecord(M4BModel):
    schema_version: Literal["1.0"] = "1.0"
    formula_id: str = Field(pattern=r"^FORM[0-9]{4,}$")
    frame_id: str = Field(pattern=r"^F[0-9]{6,}$")
    timestamp: float = Field(ge=0, allow_inf_nan=False)
    crop_path: str | None = None
    visual_transcription: str
    latex: str
    spoken_context: str
    transcript_ids: list[str]
    paragraph_ids: list[str]
    variables: list[dict[str, object]]
    assumptions: list[str]
    role: Literal["definition", "objective", "derivation_step", "example", "result", "unknown"]
    confidence: Literal["high", "medium", "low"]
    uncertain_symbols: list[str]
    verification_status: Literal["verified", "needs_review", "unreadable"]

    @model_validator(mode="after")
    def uncertain_formula_can_omit_latex(self) -> FormulaRecord:
        if self.verification_status == "verified" and not self.latex:
            raise ValueError("verified formula requires LaTeX")
        if not self.visual_transcription:
            raise ValueError("formula requires a visual description")
        return self


class TechnicalSection(M4BModel):
    section_id: str = Field(pattern=r"^S[0-9]{4,}$")
    title: str = Field(min_length=1)
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(gt=0, allow_inf_nan=False)
    summary: str = Field(min_length=1)
    transcript_evidence: list[str] = Field(min_length=1)
    visual_evidence: list[str]
    confidence: Literal["high", "medium", "low"]
    uncertainties: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def valid_section_range(self) -> TechnicalSection:
        if self.end <= self.start:
            raise ValueError("section end must exceed start")
        return self


class PacketAnalysis(M4BModel):
    schema_version: Literal["1.0"] = "1.0"
    packet_id: str = Field(pattern=r"^P[0-9]{4}$")
    time_range: list[float] = Field(min_length=2, max_length=2)
    sections: list[TechnicalSection] = Field(min_length=1)
    concepts: list[dict[str, object]]
    definitions: list[dict[str, object]]
    technical_terms: list[dict[str, object]]
    formulas: list[FormulaRecord]
    derivations: list[dict[str, object]]
    examples: list[dict[str, object]]
    comparisons: list[dict[str, object]]
    visual_findings: list[dict[str, object]]
    asr_correction_suggestions: list[dict[str, object]]
    pitfalls: list[dict[str, object]]
    uncertainties: list[dict[str, object]]
    coverage: dict[str, object]
    images_opened: list[str] = Field(min_length=1)
    image_tool: Literal["view_image"]
    external_model_api_used: Literal[False] = False

    @model_validator(mode="after")
    def valid_packet_range(self) -> PacketAnalysis:
        if self.time_range[1] <= self.time_range[0]:
            raise ValueError("analysis time range must increase")
        return self


class TechnicalCorrection(M4BModel):
    schema_version: Literal["1.0"] = "1.0"
    correction_id: str = Field(pattern=r"^C[0-9]{6,}$")
    paragraph_id: str = Field(pattern=r"^PAR[0-9]{4,}$")
    source_segment_ids: list[str] = Field(min_length=1)
    start: float = Field(ge=0, le=1500, allow_inf_nan=False)
    end: float = Field(ge=0, le=1500, allow_inf_nan=False)
    original_text: str = Field(min_length=1)
    suggested_text: str = Field(min_length=1)
    change_type: Literal["term", "spelling", "symbol", "other"] = "term"
    reason: str = Field(min_length=1)
    transcript_evidence: list[str] = Field(min_length=1)
    visual_evidence: list[str]
    confidence: Literal["high", "medium", "low"]
    decision: Literal["accepted", "pending", "rejected"]
    applied: bool

    @model_validator(mode="after")
    def valid_correction(self) -> TechnicalCorrection:
        if self.end < self.start:
            raise ValueError("correction end precedes start")
        if self.applied != (self.decision == "accepted"):
            raise ValueError("only accepted corrections may be applied")
        if self.decision == "accepted" and (self.confidence != "high" or not self.visual_evidence):
            raise ValueError(
                "accepted technical correction requires high-confidence visual evidence"
            )
        return self


class TechnicalEvidence(M4BModel):
    schema_version: Literal["1.0"] = "1.0"
    evidence_id: str = Field(pattern=r"^E[0-9]{6,}$")
    start: float = Field(ge=0, le=1500, allow_inf_nan=False)
    end: float = Field(ge=0, le=1500, allow_inf_nan=False)
    topic: str = Field(min_length=1)
    claim: str = Field(min_length=1)
    transcript_ids: list[str]
    frame_ids: list[str]
    formula_ids: list[str] = Field(default_factory=list)
    evidence_type: Literal["speech", "slide", "speech+slide", "speech+slide+formula"]
    confidence: Literal["high", "medium", "low"]
    uncertainty: str | None = None

    @model_validator(mode="after")
    def valid_evidence(self) -> TechnicalEvidence:
        if self.end < self.start or not (self.transcript_ids or self.frame_ids):
            raise ValueError("evidence range/source invalid")
        if "speech" in self.evidence_type and not self.transcript_ids:
            raise ValueError("speech evidence requires transcript IDs")
        if "slide" in self.evidence_type and not self.frame_ids:
            raise ValueError("slide evidence requires frame IDs")
        if self.evidence_type.endswith("formula") and not self.formula_ids:
            raise ValueError("formula evidence requires formula IDs")
        return self
