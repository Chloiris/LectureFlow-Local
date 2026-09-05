from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class AuditModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CropRegion(AuditModel):
    image_path: str = Field(min_length=1)
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class FormulaAudit(AuditModel):
    schema_version: Literal["1.0"] = "1.0"
    audit_id: str = Field(pattern=r"^FA[0-9]{4,}$")
    formula_id: str = Field(pattern=r"^FORM[0-9]{4,}$")
    frame_id: str = Field(pattern=r"^F[0-9]{6,}$")
    timestamp: float = Field(ge=0, le=1500, allow_inf_nan=False)
    original_image_path: str
    crop_paths: list[str]
    crop_regions: list[CropRegion] = Field(default_factory=list)
    existing_latex: str
    independent_visual_transcription: str = Field(min_length=1)
    audited_latex: str
    comparison: Literal[
        "exact_match", "equivalent", "minor_difference", "material_difference", "unverifiable"
    ]
    spoken_context: str
    transcript_ids: list[str]
    paragraph_ids: list[str]
    variables_verified: list[str]
    uncertain_symbols: list[str]
    existing_confidence: Literal["high", "medium", "low"]
    audited_confidence: Literal["high", "medium", "low"]
    existing_status: Literal["verified", "needs_review", "unreadable"]
    audited_status: Literal["verified", "needs_review", "unreadable"]
    rendering_complete: bool
    critical_error: bool
    fix_required: bool
    fix_reason: str | None
    blind_observed_at: str
    compared_at: str
    existing_latex_consulted_during_blind_pass: Literal[False] = False
    key_symbols_visible: dict[str, bool]

    @model_validator(mode="after")
    def confidence_is_evidence_based(self) -> FormulaAudit:
        if self.audited_status == "verified" and not self.audited_latex:
            raise ValueError("verified audit requires LaTeX")
        if self.audited_confidence == "high" and (
            self.audited_status != "verified"
            or self.uncertain_symbols
            or not self.key_symbols_visible
            or not all(self.key_symbols_visible.values())
        ):
            raise ValueError("high-confidence formula requires every key symbol to be visible")
        if self.crop_paths and any(not value for value in self.crop_paths):
            raise ValueError("formula crop path cannot be empty")
        if {item.image_path for item in self.crop_regions} != set(self.crop_paths):
            raise ValueError("formula crop paths and coordinate records must match")
        attention_keys = {
            "attention_arguments",
            "softmax",
            "qk_transpose",
            "sqrt_d_sub_k",
            "closing_parenthesis",
            "trailing_v",
        }
        if "attention_arguments" in self.key_symbols_visible and not attention_keys <= set(
            self.key_symbols_visible
        ):
            raise ValueError("Attention audit must explicitly check every structural symbol")
        return self


class CorrectionAudit(AuditModel):
    schema_version: Literal["1.0"] = "1.0"
    audit_id: str = Field(pattern=r"^CA[0-9]{4,}$")
    correction_id: str = Field(pattern=r"^C[0-9]{6,}$")
    paragraph_id: str = Field(pattern=r"^PAR[0-9]{4,}$")
    start: float = Field(ge=0, le=1500, allow_inf_nan=False)
    end: float = Field(ge=0, le=1500, allow_inf_nan=False)
    original_text: str = Field(min_length=1)
    corrected_text: str = Field(min_length=1)
    existing_decision: Literal["accepted", "pending", "rejected"]
    transcript_evidence: list[str]
    visual_evidence: list[str]
    independent_assessment: Literal[
        "supported", "partially_supported", "unsupported", "unverifiable"
    ]
    meaning_preserved: bool
    evidence_sufficient: bool
    audited_decision: Literal["accepted", "pending", "rejected"]
    audited_confidence: Literal["high", "medium", "low"]
    audited_suggested_text: str
    fix_required: bool
    fix_reason: str | None

    @model_validator(mode="after")
    def accepted_requires_support(self) -> CorrectionAudit:
        if self.end < self.start:
            raise ValueError("correction audit range is invalid")
        if self.audited_decision == "accepted" and (
            self.independent_assessment != "supported"
            or not self.evidence_sufficient
            or not self.meaning_preserved
        ):
            raise ValueError("audited accepted correction must be directly supported")
        if not self.meaning_preserved and self.audited_decision != "rejected":
            raise ValueError("meaning-changing correction must be rejected")
        return self


class ParagraphAudit(AuditModel):
    schema_version: Literal["1.0"] = "1.0"
    audit_id: str = Field(pattern=r"^PA[0-9]{4,}$")
    paragraph_id: str = Field(pattern=r"^PAR[0-9]{4,}$")
    packet_id: str = Field(pattern=r"^P[0-9]{4}$")
    start: float = Field(ge=0, le=1500, allow_inf_nan=False)
    end: float = Field(ge=0, le=1500, allow_inf_nan=False)
    sample_reason: list[str] = Field(min_length=1)
    source_segments: list[str] = Field(min_length=1)
    raw_preserved: bool
    cleaned_meaning_preserved: bool
    corrected_meaning_preserved: bool
    important_omission: bool
    unsupported_addition: bool
    uncertainty_preserved: bool
    time_range_correct: bool
    boundary_coherent: bool
    issues: list[str]
    fix_required: bool


class FixRecord(AuditModel):
    fix_id: str = Field(pattern=r"^FIX[0-9]{4,}$")
    target_type: Literal["formula", "correction", "paragraph", "packet", "frame", "web", "obsidian"]
    target_id: str
    before: dict[str, object]
    after: dict[str, object]
    reason: str
    evidence: list[str]
    downstream_rebuilt: list[str]
    upstream_reused: list[str]
    applied_at: str
