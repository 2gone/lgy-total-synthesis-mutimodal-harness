"""Final-document task on the common accepted tool protocol.

There is one proposal slot. The model maintains scientific notes upstream; this
consumer only represents them, seals real submissions and reports technical delivery.
"""
from copy import deepcopy
import json
import re

from pydantic import ValidationError

from tsdaf.contracts.tool_read import READ_DELIVERY_VERSION
from tsdaf.contracts.literature.record_provenance import (
    PublicMappingDelivery, PublicMappingItem, PublicScientificRecords, MAPPING_DELIVERY_MEDIA,
    submission_origin,
)
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.lanes.literature.container_enrichment import content_hash
from tsdaf.lanes.literature.enrichment_models import InvestigationReceipt
from tsdaf.lanes.literature.final_mapping import (
    FinalMappingAnswer, FinishFinalMapping, submit_model,
    ScienceCompilationReceipt, compilation_read_models, mapping_resources,
)
from tsdaf.lanes.literature.final_science_document import FinalDocumentCompiler, validate_mapping_scope
from tsdaf.lanes.literature.block_statements import StatementError
from tsdaf.lanes.literature.final_science_view import FinalScienceView
from tsdaf.workflow.naming_evidence import DomainNaming
from tsdaf.workflow.resource_reads import READ_BYTES, ReadValue, pointer_value
from tsdaf.workflow.read_dispatch import RecordTarget, project_record_value, resolve_read_location
from tsdaf.workflow.state_resources import STATE_PREFIX
from tsdaf.lanes.literature.selection_resolution import block_sentence_spans
from tsdaf.workflow.tool_execution import (
    FinishIntent, ToolConsumer, WriteOutcome, call_receipt, execute_tool_turns,
    lane_interaction_event, seal_artifact, diagnostic_text,
)
from tsdaf.workflow.content_window import DELIVERED_KEY, IN_FIRST_MESSAGE
from tsdaf.workflow.tool_turns import ToolTurnState
from tsdaf.workflow.write_delivery import WriteDelivery, write_action_id, write_result

INTERACTION_MEDIA = 'application/vnd.tsdaf.investigation-interaction+json'


def identity_domain(value):
    if value != {'kind':'identity'}:
        raise ValueError('unexpected final document naming view')
    return DomainNaming(lambda v:v, lambda v:v, value)


def mapping_assignment(machine, context):
    """Translate a sealed dispatch scope to the exact offered block name.

    Absence keeps legacy whole-document behavior; [] is an explicit paper task.
    """
    if 'mapping_assignment' not in context:
        return None
    ids = context['mapping_assignment']
    if not isinstance(ids, list) or len(ids) > 1 or any(not isinstance(v, str) for v in ids):
        raise ValueError('mapping_assignment must name one reaction block or the paper task')
    if not ids:
        return ()
    matches = [i for i, block in enumerate(machine['domain_payload']['reaction_blocks'])
               if block['reaction_block_id'] == ids[0]]
    if len(matches) != 1:
        raise ValueError('mapping_assignment does not name one fixed-baseline reaction block')
    view = FinalScienceView(machine)
    return (view.by_path[('domain_payload', 'reaction_blocks', matches[0])],)


def final_mapping_input(machine, context):
    source_table = context.get('mapping_sources', {})
    view = FinalScienceView(machine, sources=source_table, compact=True)
    records = PublicScientificRecords.model_validate_json(canonical_json_bytes(context['mapping_records']))
    public_ref = context['mapping_record_ref']
    from tsdaf.contracts.literature.record_provenance import PublicScientificRecordsArtifactRef
    records_ref = PublicScientificRecordsArtifactRef.model_validate(public_ref)
    if records.paper_id != machine['paper_id']:
        raise ValueError('mapping accounts belong to a different paper')
    # One name for each immutable note, even when titles are equal. They are called
    # notes here because an account is a container of the result (ExperimentalAccount):
    # a model shown "account 1: ..." selected it as an existing experimental account.
    record_table = {f'note {i+1}: {r.title}':{'artifact_ref':public_ref,'item_id':r.item_id}
                    for i,r in enumerate(records.records)}
    accounts = [{'name':name,'explanation':r.explanation}
                for name,r in zip(record_table, records.records, strict=True)]
    citations={f'Note {i+1} ({record.title}), bound source {j+1}':
        [ref.model_dump(mode='json') for ref in binding.evidence_refs]
        for i,record in enumerate(records.records) for j,binding in enumerate(record.source_bindings)
        if binding.status=='BOUND'}
    skeleton = view.document()
    readable = {'scientific notes':accounts, 'fixed skeleton':skeleton,
                **{name:account for name,account in zip(record_table,accounts,strict=True)}}
    roots = view.root_declarations()
    readable['skeleton scientific declarations'] = roots
    return view, records, public_ref, records_ref, record_table, accounts, skeleton, readable, roots, citations


def map_final_science(*, sources, execution_id, input_ref, context, store, client,
                      provider_binding, bind, machine, baseline_machine_ref,
                      call_budget=None, emit_event=None, recovery_artifacts=()):
    if any(ref.media_type==INTERACTION_MEDIA for ref in recovery_artifacts):
        raise ValueError('restart final mapping with a new execution; do not silently resume an earlier proposal')
    view, records, _, records_ref, record_table, accounts, skeleton, readable, roots, citations = final_mapping_input(machine,context)
    assigned = mapping_assignment(machine, context)
    run = run_science_document(kind='DETAIL', paper_id=machine['paper_id'],
        execution_id=execution_id,input_ref=input_ref,store=store,client=client,
        provider_binding=provider_binding,bind=bind,machine=machine,
        baseline_machine_ref=baseline_machine_ref,existing=view.existing,record_table=record_table,
        source_table=context.get('mapping_sources',{}),citations=citations,
        public_record_refs=(records_ref,),readable=readable,
        assigned=assigned,statements=block_form(context) == 'STATEMENTS',
        initial={**({'assigned':assigned[0] if assigned else None} if assigned is not None else {}),
            'scientific_status':records.scientific_status,
            'scientific_explanation':records.scientific_explanation,
            'scientific_notes':accounts,'fixed_skeleton':skeleton,
            'readable':list(readable),'skeleton_scientific_declarations':roots,
            # Names the document may write to cite a source. They are not read locations:
            # this task reads the notes, the skeleton and its own submissions only.
            **({'names_to_cite':{'procedure_sources':list(context.get('mapping_sources',{})),
                                 'citations':list(citations)}}
               if context.get('mapping_sources') or citations else {})},
        call_budget=call_budget,emit_event=emit_event)
    return InvestigationReceipt(kind='EXPERIMENTAL_MAPPING',paper_id=sources.paper_id,
        execution_id=execution_id,input_ref=input_ref,baseline_machine_ref=baseline_machine_ref,
        raw_answer=run.answer.model_dump(mode='json'),failure=run.failure,closed_out=run.closed_out,
        request_refs=run.request_refs,audit_refs=run.audit_refs,resources_sha256=run.resources_sha256,
        source_identities=tuple(d.identity() for d in sources.active()),
        thought_tokens=run.thought_tokens,model_seconds=run.model_seconds)


# How many calls of one reply may each be refused on their own before the reply is refused whole.
REFUSED_CALLS_PER_REPLY = 12


# How a reaction-block task writes: 'DOCUMENT' (one whole document, submit_final) or
# 'STATEMENTS' (one small function per thing the note states). A run's context may name
# the form; otherwise this default holds. Publication replays with the same choice.
BLOCK_FORM = 'DOCUMENT'


# What stays visible of earlier reads. The compilation loop declared none and inherited the
# default 24,000 characters, which one figure alone exceeds: from the first figure it read,
# every earlier passage became a 200-character pointer and the figure itself lasted one
# reply (hyperfirin, 2026-10-10: 40 replies, one passage read three times, one scheme asked
# for fourteen times). These are the numbers the fusion lane it replaced had measured for
# the same sources (`literature_fusion_runtime.FUSION_WINDOW_CHARS`): prose by characters,
# pictures by count.
COMPILATION_WINDOW_CHARS = 96_000
# A paper's main text carries six to thirteen schemes and the compile reads crops of them
# as well; at four, each new crop turned an earlier one into a pointer and the same region
# was asked for again (hyperfirin: one region three times). A picture costs about a
# thousand tokens, so the working set of a whole paper's schemes fits.
COMPILATION_WINDOW_IMAGES = 16


def block_form(context):
    return context.get('mapping_form') or BLOCK_FORM


# The same choice for the first compilation, the paper skeleton.
SKELETON_FORM = 'DOCUMENT'


def skeleton_form():
    return SKELETON_FORM


def run_science_document(*, kind, paper_id, execution_id, input_ref, store, client,
                        provider_binding, bind, readable, initial, existing, record_table,
                        source_table, citations, public_record_refs=(), machine=None,
                        baseline_machine_ref=None, call_budget=None, emit_event=None,
                        source_read=None, assigned=None, statements=False):
    """Shared accepted tool loop for the two real scientific compilation boundaries."""
    first = kind == 'SKELETON'
    if first != (machine is None and baseline_machine_ref is None):
        raise ValueError('compilation mode and actual baseline disagree')
    # Both callers supply these complete objects in the initial message. Original
    # source directories and later submissions are deliberately not in this set.
    supplied = list(readable)
    readable = dict(readable)
    if machine is not None:
        readable['complete fixed skeleton'] = FinalScienceView(machine, sources=source_table).document()
        initial = {**initial, 'readable': [*initial.get('readable', ()), 'complete fixed skeleton']}
    readable.update({'latest submission':{'status':'NOT_SUBMITTED'},
                     'latest accepted document':{'status':'NOT_SUBMITTED'}})
    from tsdaf.workflow.compilation_transport import (CODEC_MEDIA, compilation_transport_descriptor,
        transport_binding, decode_compilation_call, compilation_arguments)
    task = 'WHOLE' if assigned is None else 'BLOCK' if assigned else 'PAPER'
    skeleton_statements = bool(statements and first)
    statements = bool(statements and (first or task in ('BLOCK', 'PAPER')))
    paper_statements = statements and task == 'PAPER'
    prompt, tools = mapping_resources(skeleton=first, task=task, statements=statements)
    # A block written as statements: what has been said so far, and where each statement
    # landed in the last assembled document (to name it in a refusal).
    entries, placed = [], {'where': {}, 'order': []}
    # The write calls of one reply are one write: the first of them saves them all, and the
    # others report that same save (or that same refusal).
    reply_write = {'response': None, 'calls': {}, 'error': None, 'refused': {}}
    compound_names = {}
    if statements:
        from tsdaf.lanes.literature.block_statements import compound_aliases
        compound_names = compound_aliases(readable.get('complete fixed skeleton'))
    statement_tools = ()
    if statements:
        from tsdaf.lanes.literature.block_statements import PAPER_TOOLS, TOOLS
        from tsdaf.lanes.literature.skeleton_statements import TOOLS as SKELETON_TOOLS
        statement_tools = SKELETON_TOOLS if skeleton_statements else PAPER_TOOLS if paper_statements else TOOLS
    assigned_name = assigned[0] if statements and not paper_statements and not skeleton_statements else None
    block_path = tuple(existing[assigned_name]) if assigned_name else ()
    block_operations = [name for name, path in existing.items()
                        if tuple(path[:len(block_path)]) == block_path
                        and len(path) == len(block_path) + 2 and path[-2] == 'operations'] if assigned_name else []
    if paper_statements:
        block_operations = None  # the paper task names its own accounts

    def statement_diagnostic(error):
        from tsdaf.lanes.literature.block_statements import locate
        text = diagnostic_text(error)
        return locate(text, placed['where'], placed['order']) if isinstance(text, str) and not skeleton_statements else text
    read_item_model, read_envelope_model, _ = compilation_read_models(skeleton=first)
    requests, audits, items = [], [], []
    compiled_ref = None
    result_ref = None
    delivery_ref = None
    model_delivery = None
    explanation = None
    failure = None
    stop = None
    thought_tokens = 0
    model_seconds = 0.0
    loop = ToolTurnState(history=[{'type':'user_input','content':[{'type':'text',
        'text':canonical_json_bytes(initial).decode()}], DELIVERED_KEY:{
            'items':[{'location':name} for name in supplied],
            'reply':{'outcome':'READ','items':[{'outcome':'READ','location':name} for name in supplied]},
            IN_FIRST_MESSAGE:True}}])
    delivery = WriteDelivery()
    resource_locations = {}

    def seal(value, media):
        return seal_artifact(store,bind,value,media)

    seal(prompt.encode(),'text/vnd.tsdaf.investigation-prompt+markdown')
    seal(tools,'application/vnd.tsdaf.investigation-tools+json')
    audits.append(seal(transport_binding(execution_id=execution_id,input_ref=input_ref,kind=kind),CODEC_MEDIA))

    def save():
        nonlocal result_ref, delivery_ref
        public_delivery = PublicMappingDelivery(
            schema_version='tsdaf.public-mapping-delivery.v1', paper_id=paper_id,
            mapping_execution_id=execution_id,kind=kind,
            baseline_sha256=baseline_machine_ref.sha256 if baseline_machine_ref else None,
            input_sha256=input_ref.sha256 if first else None,
            record_artifact_refs=public_record_refs, items=tuple(PublicMappingItem.model_validate_json(canonical_json_bytes(i)) for i in items),
            model_delivery=model_delivery, execution_stop=stop or failure,
        )
        delivery_ref = seal(public_delivery.model_dump(mode='json'),MAPPING_DELIVERY_MEDIA)
        answer = FinalMappingAnswer(model_delivery=model_delivery, explanation=explanation,
                                    compiled_ref=compiled_ref, delivery_ref=delivery_ref)
        result_ref = seal(answer.model_dump(mode='json'),'application/vnd.tsdaf.investigation-answer+json')
        return answer

    def apply_write(name, raw, ctx, targets, repairs):
        nonlocal compiled_ref
        accepted_entries = statement_ids = None
        if statements:
            from tsdaf.lanes.literature.block_statements import accept, assemble, statements_of
            from tsdaf.workflow.tool_turns import decode_tool_arguments, response_calls
            if reply_write['response'] == ctx.response_ref.sha256:
                if reply_write['error'] is not None:
                    raise ValueError('Not saved: the calls of one reply are saved together, and this reply was refused. '
                                     'The reason is in the result of its first call.')
                if ctx.call_index in reply_write['refused']:
                    raise ValueError(reply_write['refused'][ctx.call_index])
                row = write_result(interaction_sha256=ctx.interaction_sha256,call_index=ctx.call_index,
                    item='document:0',tool=name,target='fixed-final-document',outcome='ACCEPTED',
                    repairs=repairs.get('document:0',()))
                return WriteOutcome(reply={'outcome':'ACCEPTED','items':[{'item':'document:0','outcome':'ACCEPTED'}],
                    'message':'Saved with the other calls of this reply.',
                    'id':reply_write['calls'].get(ctx.call_index)},rows=[row],audit={'statements':len(entries),'saved_with':reply_write['first']})
            siblings = [(index, call) for index, call in enumerate(response_calls(store.read_bytes(ctx.response_ref)))
                        if call.get('name') in statement_tools]
            reply_write.update(response=ctx.response_ref.sha256, calls={}, error=True, first=ctx.call_index, refused={})
            try:
                together = statements_of((call['name'], decode_tool_arguments(call['arguments'])) for _, call in siblings)
                # A call the tool layer itself cannot place is refused on its own, with its
                # reason; the rest of the reply is still put together and saved. Resending a
                # hundred and forty calls because one named an undeclared compound cost a
                # skeleton compile four extra replies (ineleganolide, 2026-10-10).
                kept, refused = list(range(len(siblings))), {}
                while True:
                    try:
                        chosen = [together[position] for position in kept]
                        if skeleton_statements:
                            from tsdaf.lanes.literature import skeleton_statements as skeleton_form_tools
                            accepted_entries, statement_ids = skeleton_form_tools.accept(entries, chosen, sources=citations)
                            (document, where), order = skeleton_form_tools.assemble(accepted_entries), []
                        else:
                            accepted_entries, statement_ids = accept(entries, chosen, operations=block_operations, aliases=compound_names)
                            document, where, order = assemble(accepted_entries, assigned=assigned_name, operations=block_operations)
                        break
                    except StatementError as error:
                        found = re.match(r'call (\d+) of this reply', str(error))
                        if found is None or len(kept) < 2 or len(refused) >= REFUSED_CALLS_PER_REPLY:
                            raise
                        position = kept.pop(int(found.group(1)) - 1)
                        refused[siblings[position][0]] = (
                            f'call {position + 1} of this reply' + str(error)[found.end():]
                            + ' ONLY THIS CALL was refused. Every other call of this reply is saved: do not send those again. '
                              'Send this one call again, corrected.')
                siblings = [siblings[position] for position in kept]
                reply_write['refused'] = refused
            except (TypeError, KeyError, AttributeError, IndexError) as error:
                # The shape check should have named this; never return a bare exception name.
                raise ValueError('The calls of this reply do not have the shape their functions declare '
                                 f'({type(error).__name__}: {error}). Check each call against its parameters.') from error
            placed.update(where=where, order=order)
            raw = {'document': document}
        canonical, spellings = compilation_arguments(raw, skeleton=first)
        command = submit_model(skeleton=first, task=task).model_validate(canonical)
        validate_mapping_scope(command.document.root, assigned=assigned, existing=existing)
        # A reply saved together is one submission, born at the first of its calls that was kept.
        action = write_action_id(ctx.interaction_sha256,siblings[0][0] if statements else ctx.call_index,'document:0')
        compiler = FinalDocumentCompiler(machine, submission=execution_id+'/'+action,
            existing=existing, records=record_table, sources=source_table,citations=citations,
            submission_refs=[submission_origin(execution_id,action)],paper_id=paper_id)
        candidate = compiler.compile_science(command.document.root)
        accepted = seal(candidate,'application/vnd.tsdaf.final-science-candidate+json')
        # Earlier accepted proposals remain in the audit, but are not presented as
        # materialized in the latest whole-document result.
        for item in items:
            if item['status']=='APPLIED':
                item.update(status='NOT_MATERIALIZED',error_code='REPLACED_FINAL_DOCUMENT',
                            explanation='Replaced by a later complete final document.')
        refs = []
        for node in compiler.nodes.values():
            if hasattr(node,'records'):
                for ref in node.records:
                    if ref not in refs: refs.append(ref)
        items.append({'submission_action':action,
            'accepted_birth':{'execution_id':execution_id,'action_id':action},
            'submitted':deepcopy(command.document.root),'status':'APPLIED',
            'target_ids':sorted({value.identity for value in compiler.nodes.values() if hasattr(value,'identity')}),
            'model_record_refs':refs})
        compiled_ref = accepted
        if accepted_entries is not None:
            entries[:] = accepted_entries
            reply_write.update(error=None, calls={index: found for (index, _), found in zip(siblings, statement_ids)})
        save()
        row = write_result(interaction_sha256=ctx.interaction_sha256,call_index=ctx.call_index,
            item='document:0',tool=name,target='fixed-final-document',outcome='ACCEPTED',
            repairs=repairs.get('document:0',()))
        if statement_ids is not None and ctx.call_index in reply_write['refused']:
            raise ValueError(reply_write['refused'][ctx.call_index])
        if statement_ids is not None:
            return WriteOutcome(reply={'outcome':'ACCEPTED','items':[{'item':'document:0','outcome':'ACCEPTED'}],
                'message':'Saved, with the other calls of this reply. Every call you have made is held.',
                'id':reply_write['calls'].get(ctx.call_index),'calls_held':len(entries)},rows=[row],
                audit={'statements':len(entries),'reply_calls':[index for index, _ in siblings]})
        return WriteOutcome(reply={'outcome':'ACCEPTED','items':[{'item':'document:0','outcome':'ACCEPTED'}],
            'message':'The complete scientific document is saved.',
            **({'argument_normalizations':spellings} if spellings else {})},rows=[row],
            audit={'argument_normalizations':spellings})

    def finish(name, raw, ctx):
        command = FinishFinalMapping.model_validate(raw)
        if statements and reply_write['response'] == ctx.response_ref.sha256 and (reply_write['error'] is not None or reply_write['refused']):
            raise ValueError('Some calls of this reply were not saved. Make them again, corrected, before finishing.')
        if command.disposition=='COMPLETE' and compiled_ref is None:
            raise ValueError('No complete document has been accepted. Submit the complete document or finish PARTIAL.' if first else 'No final document has been accepted. Submit {} for a no-change result, or finish PARTIAL.')
        def accept():
            nonlocal model_delivery, explanation
            model_delivery, explanation = command.disposition, command.explanation
            save()
        return FinishIntent(disposition=command.disposition,note=command.explanation,accept=accept)

    def submission_view(location):
        item=None
        if location=='latest submission' and items:
            item=items[-1]
        elif location=='latest accepted document':
            item=next((value for value in reversed(items) if value.get('accepted_birth') is not None),None)
        return readable[location] if item is None else {
            'acceptance':'ACCEPTED' if item.get('accepted_birth') is not None else 'REJECTED',
            'materialization':item['status'], 'submitted':deepcopy(item['submitted']),
            **{key:item[key] for key in ('error_code','explanation') if key in item}}

    def read(location, selection, ctx):
        root = location.partition('#')[0]
        if root not in readable and not location.startswith(STATE_PREFIX) and source_read is not None:
            return source_read(location,selection,ctx,citations)

        def resolve_root(name):
            if name not in readable:
                return None
            # Pin this actual proposal before applying a sub-selection. The same
            # dispatcher handles both directory links and versioned state links.
            content = deepcopy(submission_view(name))
            def perform(pointer, selected, max_bytes=READ_BYTES):
                address = name + ('#' + pointer if pointer else '')
                value = project_record_value(pointer_value(content,pointer),selected,
                    location=address,item_key=None,max_bytes=max_bytes,
                    sentence_spans=lambda text:[(s.char_start,s.char_end) for s in block_sentence_spans(text)])
                if name in {'latest submission','latest accepted document'}:
                    value['version']=content_hash(content)
                return ReadValue(receipt=value)
            return RecordTarget(perform, with_budget=perform)

        return resolve_read_location(location,selection,task_id=execution_id,store=store,
            state_resources=ctx.state_resources,persisted_locations=resource_locations,resolve_root=resolve_root,
            sentence_spans=lambda text:[(s.char_start,s.char_end) for s in block_sentence_spans(text)])

    def current_proposal_calls():
        for step in reversed(loop.history):
            if step.get('type')=='function_call' and step.get('name')=='submit_final':
                return (step['call_id'],)
        if statements:
            # Statements accumulate: every write call is part of what the block holds.
            return tuple(step['call_id'] for step in loop.history
                         if step.get('type')=='function_call' and step.get('name') in statement_tools)
        return ()

    def audit(ctx, event):
        audits.append(seal(call_receipt(ctx,event),INTERACTION_MEDIA))
        if event['kind']=='REJECTED' and ctx.call['name'] in ('submit_final',*statement_tools):
            action = write_action_id(ctx.interaction_sha256,ctx.call_index,'document:0')
            submitted = ctx.call['arguments']
            items.append({'submission_action':action,'accepted_birth':None,
                'submitted':deepcopy(submitted) if isinstance(submitted,dict) else {'raw':submitted},
                'status':'NOT_MATERIALIZED','error_code':'FINAL_DOCUMENT_REJECTED',
                'explanation':str(event['diagnostic'])})
            save()

    def on_provider_error(error, ref):
        nonlocal failure
        failure = error.failure.code.value
        if ref is not None: audits.append(ref)

    def on_turn_error(error):
        nonlocal failure
        failure = type(error).__name__+':'+str(error)

    def on_close_out(completion):
        nonlocal stop
        stop = completion['closed_out']
        save()

    def on_interaction(record):
        nonlocal thought_tokens, model_seconds
        thought_tokens += int(record.get('total_thought_tokens') or 0)
        model_seconds += float(record.get('elapsed_ms') or 0)/1000
        if emit_event is not None:
            emit_event('literature_lane_interaction',lane_interaction_event('PAPER_FUSION' if first else 'EXPERIMENTAL_MAPPING',record))

    from tsdaf.workflow.source_investigation import check_context_capacity
    execute_tool_turns(loop, ToolConsumer(
        label='PAPER_FUSION' if first else 'EXPERIMENTAL_MAPPING', name='science compilation', task_id=execution_id,
        stall_limit=0,
        turn_binding={'execution_id':execution_id,'input_ref':input_ref.model_dump(mode='json')},
        request_media='application/vnd.tsdaf.investigation-request+octets',
        response_media='application/vnd.tsdaf.investigation-response+octets',interaction_media=INTERACTION_MEDIA,
        delivery=delivery,catalog_data=[{'location':key} for key in readable],locations=resource_locations,wrap_state_content=False,
        result_ref=lambda:result_ref,system_instruction=prompt,tools=tools,
        window_chars=COMPILATION_WINDOW_CHARS,window_images=COMPILATION_WINDOW_IMAGES,
        tool_kinds=({**({'read':'read'} if skeleton_statements else {}),**{tool:'write' for tool in statement_tools},'finish_mapping':'finish'} if statements
                    else {'read':'read','submit_final':'write','finish_mapping':'finish'}),
        read_item_model=read_item_model,read_envelope_model=read_envelope_model,
        versions=lambda location:content_hash(submission_view(location.partition('#')[0]))
            if location.partition('#')[0] in {'latest submission','latest accepted document'} else None,
        retained_call_ids=current_proposal_calls,
        check_prepared=check_context_capacity,
        **({} if statements else {'decode_arguments':decode_compilation_call}),
        progress=lambda states:{'document_saved':compiled_ref is not None,'unresolved_writes':delivery.pending()},
        interaction_extras=lambda *args:{},make_domain_names=lambda _:identity_domain({"kind":"identity"}),
        restore_domain_names=identity_domain,
        on_provider_error=on_provider_error,on_request_sealed=requests.append,on_response_sealed=audits.append,
        on_turn_recorded=lambda ref,extra:audits.extend([ref,*([extra] if extra is not None else [])]),
        on_interaction=on_interaction,on_turn_error=on_turn_error,turn_errors=(ValueError,TypeError,OSError),
        on_close_out=on_close_out,
        targets=lambda name,raw:{'document:0':'fixed-final-document'},
        fixed_write_targets={'document:0':'fixed-final-document'},resolve_read=read,
        apply_write=apply_write,finish=finish,audit=audit,
        diagnose=statement_diagnostic if statements else diagnostic_text,
        rejected_errors=(ValueError,TypeError,KeyError),finished_message='Mapping already finished.',
        unknown_tool_message='Use one of the declared final-mapping tools.',
        reject_message='The final proposal was not replaced; the submitted content remains recorded.',
    ),store=store,bind=bind,client=client,provider_binding=provider_binding,
        recovery_artifacts=(),checkpoint=lambda:None,call_budget=call_budget)
    answer = save()
    return ScienceCompilationReceipt(kind=kind,paper_id=paper_id,
        execution_id=execution_id,input_ref=input_ref,answer=answer,
        failure=failure,closed_out=stop,request_refs=tuple(requests),audit_refs=tuple(audits),
        resources_sha256=content_hash([prompt,tools,client.context_policy,compilation_transport_descriptor(),READ_DELIVERY_VERSION]),
        thought_tokens=thought_tokens,model_seconds=round(model_seconds,1))
