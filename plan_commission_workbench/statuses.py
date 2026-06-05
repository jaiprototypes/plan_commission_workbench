"""Status constants for the standalone workbench."""

AGENDA_PENDING = "agenda_pending"
AGENDA_CLASSIFYING = "agenda_classifying"
AGENDA_HIT = "agenda_hit"
NOT_TARGET_PROJECT = "not_target_project"
NEEDS_AGENDA_REVIEW = "needs_agenda_review"

APPLICATION_QUEUED = "application_queued"
APPLICATION_DOWNLOADING = "application_downloading"
APPLICATION_DOCLING = "application_docling"
APPLICATION_LLM_EXTRACTING = "application_llm_extracting"
APPLICATION_EXTRACTED = "application_extracted"
NEEDS_OPERATOR_REVIEW = "needs_operator_review"
ACCEPTED = "accepted"
REJECTED = "rejected"

FAILED_AGENDA_DOCLING = "failed_agenda_docling"
FAILED_AGENDA_LLM = "failed_agenda_llm"
FAILED_APPLICATION_DOWNLOAD = "failed_application_download"
FAILED_APPLICATION_DOCLING = "failed_application_docling"
FAILED_APPLICATION_LLM = "failed_application_llm"

RUNNING = "running"
COMPLETED = "completed"

AGENDA_FINAL_STATUSES = {AGENDA_HIT, NOT_TARGET_PROJECT, NEEDS_AGENDA_REVIEW}
APPLICATION_FINAL_STATUSES = {APPLICATION_EXTRACTED, NEEDS_OPERATOR_REVIEW, ACCEPTED, REJECTED}
RUN_FAILURE_STATUSES = {
    FAILED_AGENDA_DOCLING,
    FAILED_AGENDA_LLM,
    FAILED_APPLICATION_DOWNLOAD,
    FAILED_APPLICATION_DOCLING,
    FAILED_APPLICATION_LLM,
}
