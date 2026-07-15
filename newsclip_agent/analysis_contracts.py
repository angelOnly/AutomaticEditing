"""Versioned contracts for semantic analysis artifacts.

These constants are intentionally kept in one small module.  A change to
attribution or semantic classification must invalidate downstream artifacts;
otherwise a task can silently combine new code with an old cached analysis.
"""

ASR_WINDOW_CONTRACT_VERSION = "asr_window_v2_core_owned"
ASR_SEGMENT_CONTRACT_VERSION = "asr_segments_v2_core_owned"
ASR_ATTRIBUTION_POLICY_VERSION = "owner_chunk_then_midpoint_legacy_v2"
ASR_DIGEST_CONTRACT_VERSION = "asr_digest_v2_owned_text"
VISION_ASR_INPUT_CONTRACT_VERSION = "vision_asr_input_v2_owned_text"
VISION_SCHEMA_VERSION = "vision_chunk_v4_program_ownership"
TIMELINE_CONTRACT_VERSION = "timeline_v2_owned_asr_visual_ownership"
TIMELINE_DIGEST_CONTRACT_VERSION = "llm_timeline_digest_v3_owned_asr"
AD_DETECTION_CONTRACT_VERSION = "ad_detection_v6_visual_ownership"
FULL_CONCAT_PLAN_CONTRACT_VERSION = "full_concat_plan_v2_drop_barrier"
FULL_CONCAT_PLAN_QC_VERSION = "full_concat_plan_qc_v1"
FULL_CONCAT_OUTPUT_QC_VERSION = "full_concat_output_qc_v1"
COMMON_ANALYSIS_CONTRACT_VERSION = "common_analysis_v2_asr_core_owned"

ASR_TIME_EPSILON_SECONDS = 0.001


def analysis_contract_profile() -> dict[str, str]:
    """Return the semantic versions used in cache/profile hashes."""

    return {
        "common": COMMON_ANALYSIS_CONTRACT_VERSION,
        "asr_window": ASR_WINDOW_CONTRACT_VERSION,
        "asr_segment": ASR_SEGMENT_CONTRACT_VERSION,
        "asr_attribution": ASR_ATTRIBUTION_POLICY_VERSION,
        "asr_digest": ASR_DIGEST_CONTRACT_VERSION,
        "vision_asr_input": VISION_ASR_INPUT_CONTRACT_VERSION,
        "vision_schema": VISION_SCHEMA_VERSION,
        "timeline": TIMELINE_CONTRACT_VERSION,
        "timeline_digest": TIMELINE_DIGEST_CONTRACT_VERSION,
        "ad_detection": AD_DETECTION_CONTRACT_VERSION,
        "full_concat_plan": FULL_CONCAT_PLAN_CONTRACT_VERSION,
        "plan_qc": FULL_CONCAT_PLAN_QC_VERSION,
        "output_qc": FULL_CONCAT_OUTPUT_QC_VERSION,
    }
