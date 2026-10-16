"""Verify whole-document acceptance against the existing immutable tool transcript."""
import json
from copy import deepcopy

from tsdaf.workflow.tool_turns import decode_tool_arguments
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.contracts.literature.record_provenance import PublicMappingDelivery, submission_origin
from tsdaf.lanes.literature.final_mapping import FinishFinalMapping, SubmitFinalDocument
from tsdaf.lanes.literature.final_science_document import FinalDocumentCompiler, validate_mapping_scope
from tsdaf.workflow.accepted_artifacts import read_artifact
from tsdaf.workflow.final_science_mapping import final_mapping_input, identity_domain, mapping_assignment
from tsdaf.workflow.naming_evidence import FrozenNaming, verify_accepted_turn_naming
from tsdaf.workflow.tool_execution import is_call_receipt
from tsdaf.workflow.tool_turns import response_calls
from tsdaf.workflow.write_delivery import split_write, write_action_id


def verify_final_mapping(receipt, *, baseline, baseline_ref, closure, store):
    """Only actual accepted proposals can become public; no scientific inference."""
    if receipt.kind != 'EXPERIMENTAL_MAPPING' or receipt.baseline_machine_ref != baseline_ref:
        raise ValueError('final mapping has a different kind or fixed baseline')
    def read(ref):
        if ref not in closure:
            raise ValueError('final mapping artifact is outside its verified closure')
        return read_artifact(store.read_bytes,ref)
    payload=json.loads(read(receipt.input_ref))
    if payload['baseline_machine_sha256']!=baseline_ref.sha256:
        raise ValueError('final mapping input has a different fixed baseline')
    context=payload['context']
    assigned = mapping_assignment(baseline, context)
    if assigned is not None and context['mapping_assignment'] != (payload.get('block_ids') or []):
        raise ValueError('sealed mapping assignment differs from dispatched block_ids')
    if assigned is None and payload.get('block_ids') is not None:
        raise ValueError('block mapping input has no assignment')
    view,records,public_ref,_,record_table,_,_,_,_,citations=final_mapping_input(baseline,context)
    answer=receipt.raw_model()
    if answer is None or answer.delivery_ref is None:
        raise ValueError('final mapping has no sealed delivery')
    delivery=PublicMappingDelivery.model_validate_json(read(answer.delivery_ref))
    if (delivery.mapping_execution_id!=receipt.execution_id or delivery.paper_id!=receipt.paper_id
            or delivery.baseline_sha256!=baseline_ref.sha256
            or delivery.model_delivery!=answer.model_delivery
            or [r.model_dump(mode='json') for r in delivery.record_artifact_refs]!=[public_ref]):
        raise ValueError('final delivery differs from its execution/input')
    def compile_proposal(document,action):
        validate_mapping_scope(document, assigned=assigned, existing=view.existing)
        compiler=FinalDocumentCompiler(baseline,submission=receipt.execution_id+'/'+action,
            existing=view.existing,records=record_table,sources=context.get('mapping_sources',{}),citations=citations,
            submission_refs=[submission_origin(receipt.execution_id,action)])
        return (compiler.compile_science(document) if answer.compiled_ref is not None else compiler.compile(document))
    statement_scope=None
    from tsdaf.workflow.final_science_mapping import block_form
    from tsdaf.lanes.literature.block_statements import compound_aliases
    if block_form(context)=='STATEMENTS' and assigned:
        block_path=tuple(view.existing[assigned[0]])
        statement_scope=(assigned[0],[name for name,path in view.existing.items()
            if tuple(path[:len(block_path)])==block_path and len(path)==len(block_path)+2 and path[-2]=='operations'],
            compound_aliases(view.document()))
    elif block_form(context)=='STATEMENTS' and assigned is not None:
        statement_scope=(None,None,compound_aliases(view.document()))  # the paper task: accounts are named by its own calls
    candidate=verify_document_transcript(receipt,answer=answer,delivery=delivery,read=read,
        closure=closure,compile_proposal=compile_proposal,statement_scope=statement_scope)
    if candidate is None:
        return None,delivery,payload
    if answer.compiled_ref is not None:
        machine=deepcopy(baseline)
        machine.update(candidate)
        return machine,delivery,payload
    return candidate,delivery,payload


def verify_document_transcript(receipt, *, answer, delivery, read, closure, compile_proposal,
                               submit_model=SubmitFinalDocument,before_call=None,statement_scope=None):
    """Replay actual accepted calls for both compilation boundaries, never inferred writes."""
    from tsdaf.workflow.compilation_transport import (verify_compilation_transport,
        decode_compilation_call, compilation_arguments)
    from tsdaf.lanes.literature.final_mapping import SubmitSkeletonDocument
    skeleton = submit_model is SubmitSkeletonDocument
    codec = verify_compilation_transport(receipt,read,skeleton=skeleton)
    # A block written as statements: the accepted calls are replayed in order, each one
    # adding to what the earlier ones said, exactly as the task accumulated them.
    from tsdaf.lanes.literature.block_statements import PAPER_TOOLS, TOOLS, accept, assemble, statements_of
    from tsdaf.lanes.literature import skeleton_statements as skeleton_form_tools
    whole_skeleton = statement_scope is not None and statement_scope[0] == 'SKELETON'
    STATEMENT_TOOLS = (skeleton_form_tools.TOOLS if whole_skeleton else PAPER_TOOLS if statement_scope[0] is None else TOOLS) if statement_scope is not None else ()
    held=[]; saved_reply=None
    # Which calls were kept: a call the tool layer refused on its own is not part of what
    # its reply saved.
    kept_calls=set()
    for ref in receipt.audit_refs:
        if ref.media_type!='application/vnd.tsdaf.investigation-interaction+json': continue
        seen=json.loads(read(ref))
        if is_call_receipt(seen) and seen.get('outcome')!='REJECTED':
            kept_calls.add((ArtifactRef.model_validate(seen['interaction_ref']).sha256,seen['call_index']))
    by_action={item.submission_action:item for item in delivery.items}
    if len(by_action)!=len(delivery.items):
        raise ValueError('duplicate final submission action')
    checked=set(); accepted=[]; finished=False
    for ref in receipt.audit_refs:
        raw=read(ref)
        if ref.media_type!='application/vnd.tsdaf.investigation-interaction+json': continue
        event=json.loads(raw)
        if not is_call_receipt(event): continue
        turn_ref=ArtifactRef.model_validate(event['interaction_ref'])
        turn=json.loads(read(turn_ref))
        response=json.loads(read(ArtifactRef.model_validate(turn['response_ref'])))
        calls=response_calls(canonical_json_bytes(response))
        call=calls[event['call_index']]
        if call['call_id']!=event['call_id']:
            raise ValueError('final call identity differs from its accepted response')
        naming=verify_accepted_turn_naming(read,turn,closure,
            expected_binding={'execution_id':receipt.execution_id,'input_ref':receipt.input_ref.model_dump(mode='json')},
            expected_iteration=turn['iteration'])
        if naming is None: raise ValueError('final call has no accepted naming view')
        try:
            decoded=(decode_compilation_call(call['name'],call['arguments']) if codec
                     else decode_tool_arguments(call['arguments']))
        except ValueError:
            if event['outcome']!='REJECTED': raise
            decoded=call['arguments']
        # Refused submissions preserve the actual decoded/raw request, before names.
        arguments=(decoded if event['outcome']=='REJECTED' else
            FrozenNaming.from_snapshot(naming,identity_domain).resolve(decoded))
        if before_call is not None:
            before_call(event,turn,call,arguments)
        if statement_scope is not None and call['name'] in STATEMENT_TOOLS:
            action=write_action_id(turn_ref.sha256,event['call_index'],'document:0')
            if event['outcome']=='REJECTED':
                item=by_action.get(action)
                if item is None: raise ValueError('real final submission is absent from public delivery')
                checked.add(action)
                if item.accepted_birth is not None or item.status!='NOT_MATERIALIZED':
                    raise ValueError('rejected final submission cannot be accepted')
                if item.submitted!=(arguments if isinstance(arguments,dict) else {'raw':arguments}):
                    raise ValueError('rejected final submission was altered')
                continue
            # The write calls of one reply were saved as one write by the first of them.
            if saved_reply==turn_ref.sha256:
                if action in by_action: raise ValueError('a reply saved together carries more than one submission')
                continue
            saved_reply=turn_ref.sha256
            item=by_action.get(action)
            if item is None: raise ValueError('real final submission is absent from public delivery')
            checked.add(action)
            resolve=FrozenNaming.from_snapshot(naming,identity_domain).resolve
            together=[(other['name'],resolve(decode_tool_arguments(other['arguments'])))
                      for position,other in enumerate(calls)
                      if other.get('name') in STATEMENT_TOOLS and (turn_ref.sha256,position) in kept_calls]
            if whole_skeleton:
                held,_=skeleton_form_tools.accept(held,statements_of(together))
                document,_=skeleton_form_tools.assemble(held)
            else:
                held,_=accept(held,statements_of(together),operations=statement_scope[1],aliases=statement_scope[2])
                document,_,_=assemble(held,assigned=statement_scope[0],operations=statement_scope[1])
            body,_=compilation_arguments({'document':document},skeleton=skeleton)
            document=submit_model.model_validate_json(canonical_json_bytes(body)).document.root
            if item.submitted!=document:
                raise ValueError('accepted final submission was altered')
            if item.accepted_birth is None or item.accepted_birth.action_id!=action:
                raise ValueError('final submission lacks its actual accepted birth')
            accepted.append((action,compile_proposal(document,action)))
        elif call['name']=='submit_final':
            action=write_action_id(turn_ref.sha256,event['call_index'],'document:0')
            item=by_action.get(action)
            if item is None: raise ValueError('real final submission is absent from public delivery')
            checked.add(action)
            if event['outcome']=='REJECTED':
                if item.accepted_birth is not None or item.status!='NOT_MATERIALIZED':
                    raise ValueError('rejected final submission cannot be accepted')
                if item.submitted!=(arguments if isinstance(arguments,dict) else {'raw':arguments}):
                    raise ValueError('rejected final submission was altered')
                continue
            body, _, _ = split_write(arguments)
            spellings = []
            if codec:
                body, spellings = compilation_arguments(body,skeleton=skeleton)
            command=submit_model.model_validate_json(canonical_json_bytes(body))
            if item.submitted!=command.document.root:
                raise ValueError('accepted final submission was altered')
            if item.accepted_birth is None or item.accepted_birth.action_id!=action:
                raise ValueError('final submission lacks its actual accepted birth')
            candidate=compile_proposal(command.document.root,action)
            accepted.append((action,candidate))
        elif call['name']=='finish_mapping' and event['outcome']=='FINISHED':
            finish=FinishFinalMapping.model_validate_json(canonical_json_bytes(arguments))
            if (finish.disposition!=answer.model_delivery or finish.explanation!=answer.explanation
                    or event['delivery']['disposition']!=finish.disposition):
                raise ValueError('final delivery differs from model finish')
            if json.loads(read(ArtifactRef.model_validate(event['delivery']['result_ref'])))!=answer.model_dump(mode='json'):
                raise ValueError('final finish does not seal its delivered answer')
            finished=True
    if checked!=set(by_action): raise ValueError('public delivery invents a final submission')
    if answer.model_delivery is not None and not finished:
        raise ValueError('model delivery lacks a real finish')
    applied=[i for i in delivery.items if i.status=='APPLIED']
    if not accepted:
        if applied or answer.machine_ref is not None or answer.compiled_ref is not None:
            raise ValueError('final machine lacks any accepted submission')
        return None
    if len(applied)!=1 or applied[0].submission_action!=accepted[-1][0]:
        raise ValueError('public delivery must materialize only the last accepted whole proposal')
    compiled_ref=answer.compiled_ref or answer.machine_ref
    if compiled_ref is None or json.loads(read(compiled_ref))!=accepted[-1][1]:
        raise ValueError('final machine differs from its accepted explicit document')
    return accepted[-1][1]
