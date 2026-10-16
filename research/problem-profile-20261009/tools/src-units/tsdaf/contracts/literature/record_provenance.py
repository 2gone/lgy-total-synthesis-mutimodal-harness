"""Public model records and mechanical delivery, separate from source observations.

These models are also the contract generator's definitions. Neither artifact grants
source authority; the publisher verifies its accepted execution and reference closure.
"""
from typing import Any, Literal

from pydantic import Field, model_validator, model_serializer, ConfigDict
from jsonschema import Draft202012Validator

from tsdaf.contracts._base import ContractModel

from .public_science_rules import (
    RECORDS_MEDIA, MAPPING_DELIVERY_MEDIA, PUBLIC_SCIENCE_MEDIA, validate_record_identity,
    validate_mapping_identity,
)


BINDING_STATE_RULES = {"allOf": [
    {"if": {"properties": {"status": {"const": "BOUND"}}}, "then": {
        "required": ["evidence_refs"], "properties": {"evidence_refs": {"minItems": 1}, "error_code": {"type": "null"}}}},
    {"if": {"properties": {"status": {"const": "UNBOUND"}}}, "then": {
        "required": ["error_code"], "properties": {"evidence_refs": {"maxItems": 0}, "error_code": {"type": "string", "minLength": 1}}}},
]}
MAPPING_STATE_RULES = {"allOf": [
    {"if": {"properties": {"status": {"const": "APPLIED"}}}, "then": {
        "properties": {"accepted_birth": {"not": {"type": "null"}}, "error_code": {"type": "null"}}}},
    {"if": {"properties": {"status": {"const": "NOT_MATERIALIZED"}}}, "then": {
        "required": ["error_code"], "properties": {"error_code": {"type": "string", "minLength": 1}}}},
]}
MAPPING_INPUT_RULES = {"allOf": [{
    "if": {"required":["kind"],"properties":{"kind":{"const":"SKELETON"}}},
    "then": {"required":["input_sha256"],"properties":{
        "input_sha256":{"type":"string","pattern":"^[0-9a-f]{64}$"},"baseline_sha256":{"type":"null"}}},
    "else": {"required":["baseline_sha256"],"properties":{
        "baseline_sha256":{"type":"string","pattern":"^[0-9a-f]{64}$"},"input_sha256":{"type":"null"}}},
}]}


def _validate_state(value, rules):
    error = next(Draft202012Validator(rules).iter_errors(value.model_dump(mode="json")), None)
    if error is not None:
        raise ValueError("inconsistent public delivery/binding state at " + ".".join(map(str, error.path)))
    return value


class PublicScientificRecordsArtifactRef(ContractModel):
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_size: int = Field(ge=0)
    logical_name: str
    media_type: Literal["application/vnd.tsdaf.public-scientific-records+json"]


class ModelRecordRef(ContractModel):
    artifact_ref: PublicScientificRecordsArtifactRef
    item_id: str = Field(min_length=1)


class ModelSubmissionRef(ContractModel):
    """Host-owned origin of an explicit model declaration, never source SUPPORT."""
    execution_id: str = Field(min_length=1)
    submission_action: str = Field(min_length=1)


def submission_origin(execution_id, action_id):
    """The same actual action coordinate in compilation and accepted replay."""
    return ModelSubmissionRef(execution_id=execution_id,submission_action=action_id).model_dump(mode='json')


class RecordBirth(ContractModel):
    execution_id: str = Field(min_length=1)
    action_id: str = Field(min_length=1)


class RecordEvidenceRef(ContractModel):
    observation_id: str = Field(min_length=1)
    source_observation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class RecordSourceBinding(ContractModel):
    model_config = ConfigDict(json_schema_extra=BINDING_STATE_RULES)
    submitted_name: str = Field(min_length=1)
    status: Literal["BOUND", "UNBOUND"]
    evidence_refs: tuple[RecordEvidenceRef, ...] = ()
    error_code: str | None = None

    @model_validator(mode="after")
    def _binding(self):
        return _validate_state(self, BINDING_STATE_RULES)


class PublicScientificRecord(ContractModel):
    item_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    birth: RecordBirth
    source_bindings: tuple[RecordSourceBinding, ...] = ()
    evidence_refs: tuple[RecordEvidenceRef, ...] = ()


class PublicScientificRecords(ContractModel):
    schema_version: Literal["tsdaf.public-scientific-records.v1"]
    paper_id: str = Field(min_length=1)
    recording_execution_id: str = Field(min_length=1)
    producer_receipt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scientific_status: Literal["COMPLETE", "PARTIAL"]
    scientific_explanation: str = Field(min_length=1)
    records: tuple[PublicScientificRecord, ...] = ()

    @model_validator(mode="after")
    def _identities(self):
        validate_record_identity(self.model_dump(mode="json"))
        return self


class PublicMappingItem(ContractModel):
    model_config = ConfigDict(json_schema_extra=MAPPING_STATE_RULES)
    submission_action: str = Field(min_length=1)
    accepted_birth: RecordBirth | None = Field(description=(
        "First accepted action of this mapping entry in mapping_execution_id. "
        "This is not the birth of a pre-existing scientific target object. "
        "Null means this submission was never accepted."
    ))
    submitted: dict[str, Any]
    status: Literal["APPLIED", "NOT_MATERIALIZED"]
    target_ids: tuple[str, ...] = ()
    model_record_refs: tuple[ModelRecordRef, ...] = ()
    error_code: str | None = None
    error_path: str | None = None
    explanation: str | None = None

    @model_validator(mode="after")
    def _delivery(self):
        return _validate_state(self, MAPPING_STATE_RULES)


class PublicMappingDelivery(ContractModel):
    model_config = ConfigDict(json_schema_extra=MAPPING_INPUT_RULES)
    schema_version: Literal["tsdaf.public-mapping-delivery.v1"]
    paper_id: str = Field(min_length=1)
    mapping_execution_id: str = Field(min_length=1)
    kind: Literal['SKELETON','DETAIL'] | None = None
    baseline_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    input_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    record_artifact_refs: tuple[PublicScientificRecordsArtifactRef, ...] = Field(default=(), description=(
        "Public provenance derivatives for this revision. Rebinding may create new artifact bytes; "
        "these references do not assert that the mapping model read those derived bytes. "
        "The sealed execution input and original submission retain the actual input history."
    ))
    items: tuple[PublicMappingItem, ...] = ()
    model_delivery: Literal["COMPLETE", "PARTIAL"] | None = None
    execution_stop: str | None = None

    @model_serializer(mode='wrap')
    def _legacy_shape(self, handler):
        result=handler(self)
        for key in ('kind','input_sha256'):
            if result[key] is None:
                result.pop(key)
        if self.kind=='SKELETON' and self.baseline_sha256 is None:
            result.pop('baseline_sha256',None)
        return result

    @model_validator(mode="after")
    def _identities(self):
        _validate_state(self,MAPPING_INPUT_RULES)
        validate_mapping_identity(self.model_dump(mode="json"))
        return self
