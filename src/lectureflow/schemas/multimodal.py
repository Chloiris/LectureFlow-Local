from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class MultimodalModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PacketSection(MultimodalModel):
    section_id: str = Field(pattern=r"^S[0-9]{4,}$")
    title: str
    start: float = Field(ge=0, le=300, allow_inf_nan=False)
    end: float = Field(ge=0, le=300, allow_inf_nan=False)
    summary: str
    transcript_evidence: list[str]
    visual_evidence: list[str]
    confidence: Literal["high", "medium", "low"]
    uncertainties: list[str]

    @model_validator(mode="after")
    def validate_section(self) -> PacketSection:
        if self.end < self.start:
            raise ValueError("section end precedes start")
        if not self.transcript_evidence and not self.visual_evidence:
            raise ValueError("section requires transcript or visual evidence")
        return self


class EvidenceBoundFinding(MultimodalModel):
    finding_id: str
    claim: str
    transcript_evidence: list[str]
    visual_evidence: list[str]
    confidence: Literal["high", "medium", "low"]
    uncertainty: str | None = None

    @model_validator(mode="after")
    def require_evidence(self) -> EvidenceBoundFinding:
        if not self.transcript_evidence and not self.visual_evidence:
            raise ValueError("finding requires transcript or visual evidence")
        return self


class ASRCorrectionSuggestion(MultimodalModel):
    segment_id: str
    original_text: str
    suggested_text: str
    visual_evidence: list[str]
    reason: str
    confidence: Literal["high", "medium", "low"]


class PacketAnalysis(MultimodalModel):
    schema_version: Literal["1.0"] = "1.0"
    packet_id: Literal["P0001"]
    time_range: tuple[float, float]
    images_opened: list[str]
    image_tool: str
    sections: list[PacketSection]
    concepts: list[EvidenceBoundFinding]
    definitions: list[EvidenceBoundFinding]
    course_information: list[EvidenceBoundFinding]
    formulas: list[EvidenceBoundFinding]
    code_blocks: list[EvidenceBoundFinding]
    visual_findings: list[EvidenceBoundFinding]
    asr_correction_suggestions: list[ASRCorrectionSuggestion]
    examples: list[EvidenceBoundFinding]
    pitfalls: list[EvidenceBoundFinding]
    uncertainties: list[EvidenceBoundFinding]
    coverage: dict[str, Any]
    external_model_api_used: bool = False
    ocr_used: bool = False

    @model_validator(mode="after")
    def validate_range_and_safety(self) -> PacketAnalysis:
        if self.time_range != (0.0, 300.0):
            raise ValueError("M3B analysis must cover exactly 0–300 seconds")
        if self.external_model_api_used or self.ocr_used:
            raise ValueError("external model API or OCR invalidates M3B")
        return self


class EvidenceRecord(MultimodalModel):
    schema_version: Literal["1.0"] = "1.0"
    evidence_id: str = Field(pattern=r"^E[0-9]{6,}$")
    start: float = Field(ge=0, le=300, allow_inf_nan=False)
    end: float = Field(ge=0, le=300, allow_inf_nan=False)
    topic: str
    claim: str
    transcript_ids: list[str]
    frame_ids: list[str]
    evidence_type: Literal["speech", "slide", "speech+slide"]
    confidence: Literal["high", "medium", "low"]
    uncertainty: str | None = None

    @model_validator(mode="after")
    def validate_evidence_shape(self) -> EvidenceRecord:
        if self.end < self.start:
            raise ValueError("evidence end precedes start")
        if not self.transcript_ids and not self.frame_ids:
            raise ValueError("evidence must cite transcript or frame IDs")
        if self.evidence_type == "speech" and not self.transcript_ids:
            raise ValueError("speech evidence requires transcript IDs")
        if self.evidence_type == "slide" and not self.frame_ids:
            raise ValueError("slide evidence requires frame IDs")
        if self.evidence_type == "speech+slide" and (not self.transcript_ids or not self.frame_ids):
            raise ValueError("speech+slide requires both transcript and frame IDs")
        return self
