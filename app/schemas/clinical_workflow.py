from app.schemas.common import CamelModel


class ClinicalWorkflowCapabilities(CamelModel):
    functional_feedback_enabled: bool
    clinical_reviews_enabled: bool
    structured_discharge_enabled: bool
    patient_intake_enabled: bool
    can_write_review: bool
    can_finalize_review: bool
    can_discharge: bool
    can_manage_intake: bool
