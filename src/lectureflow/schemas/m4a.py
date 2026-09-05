from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class M4AModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Paragraph(M4AModel):
    schema_version: Literal["1.0"] = "1.0"
    paragraph_id: str = Field(pattern=r"^PAR[0-9]{4,}$")
    start: float = Field(ge=0, le=300, allow_inf_nan=False)
    end: float = Field(ge=0, le=300, allow_inf_nan=False)
    title: str
    text_raw: str
    text_clean: str
    text_corrected: str
    source_segment_ids: list[str]
    correction_ids: list[str]
    evidence_ids: list[str]
    frame_ids: list[str]
    confidence: Literal["high", "medium", "low"]
    uncertainties: list[str]
    overlap_context: bool = False

    @model_validator(mode="after")
    def validate_paragraph(self) -> Paragraph:
        if self.end <= self.start:
            raise ValueError("paragraph end must be greater than start")
        if self.end - self.start > 90:
            raise ValueError("paragraph cannot exceed 90 seconds")
        if not self.source_segment_ids:
            raise ValueError("paragraph requires source segments")
        if (
            not self.text_raw.strip()
            or not self.text_clean.strip()
            or not self.text_corrected.strip()
        ):
            raise ValueError("paragraph text layers cannot be empty")
        return self


class Correction(M4AModel):
    schema_version: Literal["1.0"] = "1.0"
    correction_id: str = Field(pattern=r"^C[0-9]{6,}$")
    paragraph_id: str = Field(pattern=r"^PAR[0-9]{4,}$")
    source_segment_ids: list[str]
    start: float = Field(ge=0, le=300, allow_inf_nan=False)
    end: float = Field(ge=0, le=300, allow_inf_nan=False)
    original_text: str
    suggested_text: str
    change_type: Literal["term", "name", "spelling", "number", "other"]
    reason: str
    transcript_evidence: list[str]
    visual_evidence: list[str]
    confidence: Literal["high", "medium", "low"]
    decision: Literal["accepted", "pending", "rejected"]
    applied: bool

    @model_validator(mode="after")
    def validate_decision(self) -> Correction:
        if self.end < self.start:
            raise ValueError("correction end precedes start")
        if self.applied != (self.decision == "accepted"):
            raise ValueError("only accepted corrections may be applied")
        if self.decision == "accepted" and (
            not self.transcript_evidence or not self.visual_evidence or self.confidence != "high"
        ):
            raise ValueError(
                "accepted correction requires high-confidence speech and visual evidence"
            )
        return self


class Section(M4AModel):
    schema_version: Literal["1.0"] = "1.0"
    section_id: str = Field(pattern=r"^SEC[0-9]{4,}$")
    title: str
    start: float = Field(ge=0, le=300, allow_inf_nan=False)
    end: float = Field(ge=0, le=300, allow_inf_nan=False)
    paragraph_ids: list[str]
    summary: str
    evidence_ids: list[str]
    frame_ids: list[str]
    confidence: Literal["high", "medium", "low"]
    uncertainties: list[str]

    @model_validator(mode="after")
    def validate_section(self) -> Section:
        if self.end <= self.start:
            raise ValueError("section end must be greater than start")
        if not self.paragraph_ids:
            raise ValueError("section requires paragraphs")
        if not self.evidence_ids:
            raise ValueError("section summary requires evidence")
        return self


class Highlight(M4AModel):
    highlight_id: str = Field(pattern=r"^H[0-9]{4,}$")
    category: str
    text: str
    paragraph_id: str
    evidence_id: str
    transcript_ids: list[str]
    frame_ids: list[str]
    timestamp: float = Field(ge=0, le=300, allow_inf_nan=False)
    status: Literal["confirmed", "pending"] = "confirmed"


class MindMapNode(M4AModel):
    title: str
    target_type: Literal["section", "paragraph"]
    target_id: str
    start: float = Field(ge=0, le=300, allow_inf_nan=False)
    children: list[MindMapNode] = Field(default_factory=list)


class MindMap(M4AModel):
    schema_version: Literal["1.0"] = "1.0"
    title: str
    children: list[MindMapNode]
