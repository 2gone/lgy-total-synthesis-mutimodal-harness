"""Bind the two compilation boundaries to one explicit argument transport version."""
from copy import deepcopy
from functools import cache
import json

from tsdaf.contracts.tool_argument_codec import CODEC_DESCRIPTOR, decode_arguments, normalize_properties
from tsdaf.contracts.tool_declaration import provider_declaration_schema
from tsdaf.lanes.literature.final_mapping import SubmitFinalDocument, SubmitSkeletonDocument
from tsdaf.workflow.tool_turns import decode_tool_arguments

CODEC_MEDIA = 'application/vnd.tsdaf.compilation-argument-codec+json'


def compilation_transport_descriptor():
    return deepcopy(CODEC_DESCRIPTOR)


def decode_compilation_call(name, raw):
    return decode_arguments(raw) if name == 'submit_final' else decode_tool_arguments(raw)


@cache
def _schema(skeleton):
    return provider_declaration_schema(SubmitSkeletonDocument if skeleton else SubmitFinalDocument)


def compilation_arguments(raw, *, skeleton):
    return normalize_properties(raw, _schema(skeleton))


def transport_binding(*, execution_id, input_ref, kind):
    return {'descriptor':compilation_transport_descriptor(), 'execution_id':execution_id,
            'input_ref':input_ref.model_dump(mode='json'), 'kind':kind}


def verify_compilation_transport(receipt, read, *, skeleton):
    refs = [ref for ref in receipt.audit_refs if ref.media_type == CODEC_MEDIA]
    if not refs:
        return False  # Sealed historical executions retain their original decoder.
    if len(refs) != 1:
        raise ValueError('compilation must bind exactly one argument codec')
    value = json.loads(read(refs[0]))
    expected = transport_binding(execution_id=receipt.execution_id,input_ref=receipt.input_ref,
                                 kind='SKELETON' if skeleton else 'DETAIL')
    if value != expected:
        raise ValueError('compilation argument codec version or execution binding differs')
    return True
