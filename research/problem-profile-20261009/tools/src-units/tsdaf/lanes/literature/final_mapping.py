"""One final-document submission and delivery, independent of scientific status."""
from copy import deepcopy
from functools import cache
from importlib.resources import files
from typing import Literal

from pydantic import Field, model_serializer, model_validator

from tsdaf.contracts._base import ContractModel
from tsdaf.contracts.source import ArtifactRef
from tsdaf.contracts.tool_declaration import (
    name_repeated_shapes, omit_stated_descriptions, provider_declaration_schema,
)
from tsdaf.contracts.tool_argument_codec import CODEC_DESCRIPTION
from tsdaf.contracts.tool_delivery import WriteDeliveryControl
from tsdaf.contracts.tool_read import (
    DirectorySelection, RecordSelection, TextPartSelection, TextSelection, WholeSelection,
    ReadEnvelope, ReadItem, ReadRequest, read_declaration, read_models,
)
from tsdaf.lanes.literature.structured_scientific_values import (
    REPEATED_SHAPE_NOTE, BlockTaskDocument, FinalTaskDocument, PaperTaskDocument,
    SkeletonScienceDocument, compilation_write_entry, repeated_object_shapes,
)


class SubmitFinalDocument(ContractModel):
    """Submit one consistent final scientific document against the supplied fixed skeleton.

    Nest materials, quantities and stages inside their scientific containers.
    """
    document: FinalTaskDocument


class SubmitBlockDocument(SubmitFinalDocument):
    __doc__ = SubmitFinalDocument.__doc__
    document: BlockTaskDocument


class SubmitPaperDocument(SubmitFinalDocument):
    __doc__ = SubmitFinalDocument.__doc__
    document: PaperTaskDocument


def submit_model(*, skeleton=False, task='WHOLE'):
    """The one submit contract of a compilation task: what it declares and what it accepts."""
    if skeleton:
        return SubmitSkeletonDocument
    return {'WHOLE': SubmitFinalDocument, 'BLOCK': SubmitBlockDocument, 'PAPER': SubmitPaperDocument}[task]


class FinishFinalMapping(WriteDeliveryControl):
    """Finish representation of the supplied notes; this is not a new scientific judgment."""
    explanation: str = Field(min_length=1, description='What was represented, or what representation remains undelivered.')


class SubmitSkeletonDocument(ContractModel):
    """Submit the complete paper skeleton, explicitly declaring its objects and relationships.

    There is no earlier skeleton. Host-owned identities and source transport are not model fields.
    """
    document: SkeletonScienceDocument


class FinalMappingAnswer(ContractModel):
    """Technical result of the final compilation task. Original science stays in records."""
    model_delivery: Literal['COMPLETE','PARTIAL'] | None = None
    explanation: str | None = None
    machine_ref: ArtifactRef | None = None
    compiled_ref: ArtifactRef | None = None
    delivery_ref: ArtifactRef | None = None

    @model_validator(mode='after')
    def _one_compilation(self):
        if self.machine_ref is not None and self.compiled_ref is not None:
            raise ValueError('a compilation has one representation, not two competing results')
        return self

    @model_serializer(mode='wrap')
    def _historical_shape(self, handler):
        value = handler(self)
        if self.compiled_ref is None:
            value.pop('compiled_ref', None)
        return value


class ScienceCompilationReceipt(ContractModel):
    """Actual compilation execution, before either publication's technical envelope."""
    kind: Literal['SKELETON', 'DETAIL']
    paper_id: str
    execution_id: str
    input_ref: ArtifactRef
    answer: FinalMappingAnswer
    failure: str | None = None
    closed_out: str | None = None
    request_refs: tuple[ArtifactRef, ...] = ()
    audit_refs: tuple[ArtifactRef, ...] = ()
    resources_sha256: str
    thought_tokens: int = 0
    model_seconds: float = 0.0


# The final compilation reads supplied accounts, the skeleton and its own submissions:
# records and their text. No source search or image handler is installed there, so
# those selectors are not declared. The first compilation reads sources and figures.
FINAL_READ_MODELS = read_models((
    WholeSelection, TextSelection, TextPartSelection, RecordSelection, DirectorySelection))


def compilation_read_models(*, skeleton=False):
    """``(item, envelope, request)`` for the read this compilation actually serves."""
    return (ReadItem, ReadEnvelope, ReadRequest) if skeleton else FINAL_READ_MODELS


@cache
def _mapping_resources(skeleton, task):
    name = 'paper_skeleton_compilation_v1.md' if skeleton else 'final_science_document_v1.md'
    prompt = files('tsdaf.lanes.literature').joinpath('prompts/'+name).read_text()
    if skeleton:
        # The first compilation is the Fusion task: it builds the route, so it carries
        # the same route conventions the Fusion prompt always has. The final compilation
        # only transfers accounts written with the route already settled.
        from tsdaf.lanes.literature.skeleton_meanings import skeleton_guidance
        prompt += '\n' + skeleton_guidance('FUSION') + '\n'
    assignment = {
        'WHOLE': '',
        'BLOCK': ('This task is assigned one reaction block, named in assigned: write that block. '
                  'What it delivers is that experiment as the notes report it: its phases as stages, '
                  'their order, and each condition on the stage it belongs to. Amounts alone are not the delivery. '
                  'A reagent or solution the notes prepare for this experiment is part of it: its preparation '
                  'is a stage of the operation that uses it. '
                  'So is what the notes report about how this transformation was tried under other conditions '
                  '(a screening, an optimization table, attempts that failed), even when the notes give it '
                  'an account of its own: write every entry of it with this block. '
                  'Other experiments have their own tasks. Do not create or remove reaction blocks. '
                  'In other root collections, add new members only; do not edit or remove existing ones. '
                  'COMPLETE concerns this assignment.'),
        'PAPER': ('This is the paper task after the reaction-block tasks, which have written the '
                  'reaction blocks. Represent what belongs to no reaction block in the declared collections. '
                  'What the notes report for one experiment, its reagent preparation and the screening of its '
                  'conditions included, belongs to that '
                  "block's task and is not repeated here. "
                  'COMPLETE concerns this assignment, not the delivery of other tasks.'),
    }[task]
    prompt = prompt.replace('{{mapping_assignment}}', assignment)
    prompt += '\n' + compilation_write_entry(skeleton=skeleton, task=task)
    models = {'submit_final':submit_model(skeleton=skeleton, task=task),
              'finish_mapping':FinishFinalMapping}
    # The representation entry above states each shared field explanation once, with
    # its scope, and names every object shape. The declaration keeps every field and
    # rule, omits only explanations already present word for word in this same prompt,
    # and gives a named object shape in full at its first position only. The Host
    # validates each submission against the complete contract either way.
    named = repeated_object_shapes(skeleton=skeleton, task=task)

    def shown(model):
        complete = omit_stated_descriptions(provider_declaration_schema(model), prompt)
        return name_repeated_shapes(complete, named, note=REPEATED_SHAPE_NOTE)

    return prompt, [read_declaration(compilation_read_models(skeleton=skeleton)[2]),
        *[{'type':'function','name':name,'description':model.__doc__ + (' '+CODEC_DESCRIPTION if name=='submit_final' else ''),
           'parameters':shown(model)}
          for name,model in models.items()]]


STATEMENT_MECHANICS = (
    'The notes and the skeleton are in the input; work from them directly. Write with the tools.\n'
    'Each call states one thing the note says: a stage, a material with its quantities, a condition,\n'
    'an outcome, something about a whole operation, or a statement with no other place. Make many\n'
    'calls in one reply. The Host takes the calls of one reply together, adds them to what the block\n'
    'already holds, and saves it; within a reply their order does not matter. If the Host cannot save\n'
    'a reply, it returns the reason and keeps nothing from that reply: make those calls again, corrected.\n'
    'Name operations, existing materials, outcomes and compounds by their offered names in the\n'
    'skeleton, and stages by the names you give them. What a note corrects outside this block is an\n'
    'unresolved call.\n'
    'Replies are not limited. A block usually takes one or two; use as many as the note needs. When\n'
    'everything the note states for this assignment is written, use finish_mapping: COMPLETE when it\n'
    'has all been transferred, PARTIAL when some of it could not be written. A saved reply confirms\n'
    "saving, not coverage. Finishing never changes the notes' own scientific status.\n")


PAPER_STATEMENT_MECHANICS = (
    'The notes and the skeleton are in the input; work from them directly. Write with the tools.\n'
    'Each call states one thing the notes say. An experiment that belongs to no reaction block is an\n'
    'account: declare it once, then give its stages, materials and conditions with their own calls,\n'
    'placed by the name you gave the account. A characterization, a result tied to no experiment, and\n'
    'what a note corrects in the skeleton (a compound, the route, a reaction block) each have their\n'
    'own call. Make many calls in one reply. The Host takes the calls of one reply together, adds them\n'
    'to what this task already holds, and saves it; within a reply their order does not matter. If the\n'
    'Host cannot save a reply, it returns the reason and keeps nothing from that reply: make those\n'
    'calls again, corrected. Name compounds, reaction blocks and routes by their offered names in the\n'
    'skeleton.\n'
    'State completeness once. Replies are not limited; this task usually takes one or two. When\n'
    'everything that belongs to this task is written, use finish_mapping: COMPLETE when it has all\n'
    'been transferred, PARTIAL when some of it could not be written. A saved reply confirms saving,\n'
    "not coverage. Finishing never changes the notes' own scientific status.\n")


@cache
def _statement_resources(task='BLOCK'):
    """A task written as statements: the same task and rule, no document entry."""
    from tsdaf.lanes.literature.block_statements import declarations
    prompt, tools = _mapping_resources(False, task)
    document_prompt = files('tsdaf.lanes.literature').joinpath('prompts/final_science_document_v1.md').read_text()
    head, _, _ = document_prompt.partition('{{mapping_assignment}}')
    assignment = prompt[len(head):].split('\n', 1)[0]
    finish = next(tool for tool in tools if tool['name'] == 'finish_mapping')
    paper = task == 'PAPER'
    return (head + assignment + '\n\n' + (PAPER_STATEMENT_MECHANICS if paper else STATEMENT_MECHANICS),
            [*declarations(paper), finish])


SKELETON_STATEMENT_MECHANICS = (
    'The group readings are in the input with their source context; work from them directly.\n'
    'Original authorized text and images are readable with read when a specific question needs them;\n'
    'read everything you expect to need in one reply. Citations select offered sources as support.\n'
    '\n'
    'Write with the tools. Each call states one thing: a compound, a step with what it consumes and\n'
    'forms, a material or condition or result of a step, a route, a statement of the paper. You give\n'
    'compounds, steps and routes short names of your own, and every other call refers to them by\n'
    'those names. Make many calls in one reply. The Host takes the calls of one reply together, adds\n'
    'them to what you have already written, and saves the skeleton; within a reply their order does\n'
    'not matter, and a call may name something a later call of the same reply declares. If the Host\n'
    'cannot place one call, it refuses that call alone, with the reason, and saves the rest: send\n'
    'only that call again, corrected, never the calls already saved. To change an earlier call,\n'
    'repeat it with replace and its id.\n'
    'Replies are not limited; a long paper may take several. Declare scientific completeness\n'
    'yourself with completeness in your first writing reply -- nothing is saved without it; it is\n'
    'separate from whether the skeleton was saved. Then use\n'
    'finish_mapping: COMPLETE when the skeleton is delivered, even if the science is PARTIAL;\n'
    'PARTIAL when some representation remains undelivered.\n')


@cache
def _skeleton_statement_resources():
    """The first compilation written as statements: the same task, rule and route conventions."""
    from tsdaf.lanes.literature.skeleton_meanings import skeleton_guidance
    from tsdaf.lanes.literature.skeleton_statements import declarations
    _, tools = _mapping_resources(True, 'WHOLE')
    prompt = files('tsdaf.lanes.literature').joinpath('prompts/paper_skeleton_compilation_v1.md').read_text()
    head, mark, _ = prompt.partition('The group readings are in the input with their source context')
    if not mark:
        raise ValueError('the skeleton prompt no longer has the paragraph the statement form replaces')
    keep = [tool for tool in tools if tool['name'] in ('read', 'finish_mapping')]
    return (head + SKELETON_STATEMENT_MECHANICS + '\n' + skeleton_guidance('FUSION') + '\n',
            [keep[0], *declarations(), keep[1]])


def mapping_resources(*, skeleton=False, task="WHOLE", statements=False):
    if statements and skeleton:
        prompt, tools = _skeleton_statement_resources()
    elif statements:
        if task not in ('BLOCK', 'PAPER'):
            raise ValueError('statements are the block and paper tasks of the final compilation')
        prompt, tools = _statement_resources(task)
    else:
        prompt, tools = _mapping_resources(skeleton, task)
    return prompt, deepcopy(tools)
