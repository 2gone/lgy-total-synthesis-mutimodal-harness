"""The existing Fusion role applied once to the paper's real grouped readings."""
import json
from datetime import datetime, UTC
from types import SimpleNamespace
from dataclasses import asdict

from tsdaf.contracts.tool_read import READ_DELIVERY_VERSION
from tsdaf.contracts._base import ContractModel
from tsdaf.contracts.execution import ExecutionStatus, StageResult
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.source import ArtifactRef
from tsdaf.contracts.tool_read import (ReadEnvelope, ReadItem, WholeSelection, TextSelection, ImageSelection,
    canonical_read_item, item_name, read_batch)
from tsdaf.contracts.literature.record_provenance import PublicMappingDelivery, submission_origin
from tsdaf.lanes.literature.final_mapping import ScienceCompilationReceipt, SubmitSkeletonDocument
from tsdaf.lanes.literature.final_science_document import FinalDocumentCompiler
from tsdaf.lanes.literature.provider_port import AUTHORIZED_MODEL_PROFILE
from tsdaf.workflow.accepted_artifacts import read_artifact
from tsdaf.workflow.resource_reads import read_item_action_id
from tsdaf.lanes.literature.evidence_binding import (WorkUnitEvidenceRegistry, FUSION_SELECTION_STAGE,
    source_location, _scope_digest)
from tsdaf.lanes.literature.source_inventory import resolve_fusion_read_authority
from tsdaf.lanes.literature.source_reads import SourceReader
from tsdaf.lanes.literature.evidence import observation_texts
from tsdaf.lanes.literature.local_visual_read import read_cached_local_visual_read
from tsdaf.lanes.literature.selection_resolution import (block_sentence_spans,
    resolve_text_selection, resolve_visual_selection)
from tsdaf.workflow.final_science_mapping import run_science_document, skeleton_form
from tsdaf.workflow.compilation_transport import compilation_transport_descriptor
from tsdaf.workflow.final_mapping_handoff import verify_document_transcript
from tsdaf.workflow.paper_fusion_inputs import verify_group_readings
from tsdaf.workflow.paper_fusion_sources import PaperFusionSources, paper_fusion_inputs, paper_fusion_initial, paper_source_reply

PAPER_COMPILATION_MEDIA = 'application/vnd.tsdaf.paper-science-compilation+json'
STAGE = 'literature-paper-fusion'
STAGE_VERSION = '1.1.0'


class PaperCompilationReceipt(ContractModel):
    """Execution transport only; scientific declarations remain in the model proposal."""
    route_execution_id: str
    literature_execution_id: str
    paper_id: str
    document_revision_id: str
    package_sha256: str
    group_receipt_refs: tuple[ArtifactRef,...]
    group_failures: dict[str,str]
    compilation: ScienceCompilationReceipt
    source_reads: tuple[dict,...] = ()


def _groups(receipt_refs, *, package, router_execution_id, store, runner, provider_binding):
    groups=tuple(verify_group_readings(ref,package=package,router_execution_id=router_execution_id,
        store=store,runner=runner,provider_binding=provider_binding) for ref in receipt_refs)
    if not groups:
        raise ValueError('no verified group readings reached paper Fusion')
    return groups


def run_paper_compilation(*,package,package_root,router_execution_id,execution_id,document_revision_id,
                          receipt_refs,failures,store,runner,client,provider_binding,call_budget=None):
    groups=_groups(receipt_refs, package=package, router_execution_id=router_execution_id,
        store=store,runner=runner,provider_binding=provider_binding)
    sources=PaperFusionSources(groups=groups,package=package,package_root=package_root,
        store=store,execution_id=execution_id,bind=lambda *refs:None)
    initial=paper_fusion_initial(sources,failures=failures)
    payload={'package_sha256':package.canonical_content_sha256,
        'router_execution_id':router_execution_id,'document_revision_id':document_revision_id,
        'group_receipt_refs':[ref.model_dump(mode='json') for ref in receipt_refs],
        'group_failures':failures,'input':initial}
    input_ref=store.put_bytes(canonical_json_bytes(payload),media_type='application/vnd.tsdaf.paper-fusion-input+json')

    def invoke(attempt_id,content_key):
        started=datetime.now(UTC)
        def bind(*refs):
            runner.bind_attempt_artifacts(execution_id,attempt_id,tuple(refs),
                stage_name=STAGE,content_key=content_key)
        def emit(name,data):
            runner.append_event(execution_id,name,data,attempt_id=attempt_id,
                stage_name=STAGE,content_key=content_key)
        sources.bind=bind
        bind(input_ref,sources.prompt_ref,sources.schema_ref)
        compilation=run_science_document(kind='SKELETON',paper_id=package.manifest.paper_id,
            execution_id=execution_id,input_ref=input_ref,store=store,client=client,
            provider_binding=provider_binding,bind=bind,readable={'group readings':sources.readings},
            initial=initial,existing={},record_table={},source_table=sources.sources,
            citations=sources.citations,source_read=sources.resolve,call_budget=call_budget,emit_event=emit,
            statements=skeleton_form()=='STATEMENTS')
        receipt=PaperCompilationReceipt(route_execution_id=router_execution_id,
            literature_execution_id=execution_id,paper_id=package.manifest.paper_id,
            document_revision_id=document_revision_id,package_sha256=package.canonical_content_sha256,
            group_receipt_refs=tuple(receipt_refs),group_failures=failures,
            compilation=compilation,source_reads=tuple(sources.read_receipts))
        ref=store.put_bytes(canonical_json_bytes(receipt.model_dump(mode='json')),media_type=PAPER_COMPILATION_MEDIA)
        bind(ref)
        return StageResult(schema_version='tsdaf.stage-result.v1',execution_id=execution_id,
            attempt_id=attempt_id,stage_name=STAGE,stage_version=STAGE_VERSION,input_content_key=content_key,
            status=ExecutionStatus.SUCCEEDED,output_artifact=ref,failure=None,cached_from_result=None,
            started_at=started,finished_at=datetime.now(UTC))
    result=runner.run_stage(execution_id=execution_id,stage_name=STAGE,stage_version=STAGE_VERSION,
        input_artifacts=(input_ref,*receipt_refs,sources.prompt_ref,sources.schema_ref),
        settings={'read_delivery':READ_DELIVERY_VERSION, 'argument_codec':compilation_transport_descriptor(), 'backend_namespace':provider_binding.namespace_for('GATE1'),
                  'compilation_execution_id':execution_id},
        prompt_sha256=sources.prompt_ref.sha256,schema_sha256=sources.schema_ref.sha256,
        model_profile=AUTHORIZED_MODEL_PROFILE,invoke=invoke)
    if not isinstance(result,StageResult) or result.output_artifact is None:
        raise ValueError('paper Fusion did not seal an execution receipt')
    runner.finish_execution(execution_id,ExecutionStatus.SUCCEEDED)
    return PaperCompilationReceipt.model_validate_json(read_artifact(store.read_bytes,result.output_artifact)),result.output_artifact


def verify_paper_compilation(receipt_ref, *,package,package_root,store,runner,provider_binding):
    receipt=PaperCompilationReceipt.model_validate_json(read_artifact(store.read_bytes,receipt_ref))
    terminal=runner.verified_terminal_manifest(receipt.literature_execution_id)
    closure=runner.verified_execution_artifact_closure(receipt.literature_execution_id,terminal)
    def read(ref):
        if ref not in closure:
            raise ValueError('paper Fusion artifact is outside its execution closure')
        return read_artifact(store.read_bytes,ref)
    read(receipt_ref)
    if receipt.package_sha256!=package.canonical_content_sha256 or receipt.paper_id!=package.manifest.paper_id:
        raise ValueError('paper Fusion changed its package identity')
    compilation=receipt.compilation
    if (compilation.kind,compilation.execution_id,compilation.paper_id)!=('SKELETON',receipt.literature_execution_id,receipt.paper_id):
        raise ValueError('paper Fusion compilation changed its producer')
    groups=_groups(receipt.group_receipt_refs,package=package,
        router_execution_id=receipt.route_execution_id,store=store,runner=runner,provider_binding=provider_binding)
    sources=SimpleNamespace(**paper_fusion_inputs(groups=groups,package=package,store=store))
    payload=json.loads(read(compilation.input_ref))
    expected={'package_sha256':receipt.package_sha256,'router_execution_id':receipt.route_execution_id,
        'document_revision_id':receipt.document_revision_id,
        'group_receipt_refs':[ref.model_dump(mode='json') for ref in receipt.group_receipt_refs],
        'group_failures':receipt.group_failures,'input':paper_fusion_initial(sources,failures=receipt.group_failures)}
    if payload!=expected:
        raise ValueError('paper Fusion input differs from its verified group readings')
    answer=compilation.answer
    if answer.delivery_ref is None:
        raise ValueError('paper Fusion has no sealed delivery')
    delivery=PublicMappingDelivery.model_validate_json(read(answer.delivery_ref))
    if (delivery.kind,delivery.mapping_execution_id,delivery.paper_id,delivery.input_sha256,delivery.model_delivery)!= (
        'SKELETON',compilation.execution_id,compilation.paper_id,compilation.input_ref.sha256,answer.model_delivery):
        raise ValueError('paper delivery differs from its original compilation')
    consumed=[]
    sources.read_receipts=consumed
    def before_call(event,turn,call,arguments):
        if call['name']!='read':
            return
        matching=[row for row in receipt.source_reads if row['interaction_sha256']==event['interaction_ref']['sha256']
                  and row['call_index']==event['call_index']]
        if event['outcome']=='REJECTED':
            if matching: raise ValueError('rejected read cannot carry accepted source bindings')
            return
        command=ReadEnvelope.model_validate_json(canonical_json_bytes(arguments))
        requested=read_batch(command, item_model=ReadItem)
        rows=event.get('items', [])
        if len(rows)!=len(requested):
            raise ValueError('paper source read rows differ from the actual batch')
        # Use actual accepted row order; a rejected sibling never authorizes a source.
        actual=[]
        for raw,row in zip(requested,rows,strict=True):
            if row.get('outcome')!='READ':
                continue
            item,derived=canonical_read_item(raw,item_model=ReadItem)
            if item.location in sources.locations:
                actual.append((item,derived,row))
        if len(actual)!=len(matching):
            raise ValueError('paper source receipts differ from delivered source items')
        for logged,(item,derived,row) in zip(matching,actual,strict=True):
            shape=item.select.model_dump(mode='json') if item.select else None
            if (item.location,shape)!=(logged['location'],logged['selection']):
                raise ValueError('paper source receipt invents a model read')
            sid,modality,group,inventory=sources.locations[item.location]
            work=group.work_unit
            registry=WorkUnitEvidenceRegistry.model_validate_json(read(ArtifactRef.model_validate(logged['registry_ref'])))
            if (registry.execution_id,registry.package_sha256,registry.work_unit_id,
                registry.work_unit_sha256,registry.source_view_sha256,registry.document_revision_id)!= (
                compilation.execution_id,work.package_sha256,work.work_unit_id,
                work.work_unit_sha256,inventory.inventory_sha256,work.document_revision_id):
                raise ValueError('paper source binding changed its execution or reading authority')
            if len(registry.entries)!=1:
                raise ValueError('a delivered source item must have one sealed selection')
            entry=registry.entries[0]
            authority=resolve_fusion_read_authority(work_unit=work,package=package,source_inventory=inventory)
            if (entry.canonical_source_id!=sid or entry.modality!=modality or
                entry.authorized_scope_sha256!=_scope_digest(work,modality=modality,source_id=sid,read_authority=authority)):
                raise ValueError('paper source entry changed its authorized source')
            identity=(read_item_action_id(event['interaction_ref']['sha256'],event['call_index'],item_name(item))
                if entry.selection_id.startswith('read:') else
                f"{event['interaction_ref']['sha256']}-{event['call_index']}-{sid}")
            if entry.selection_id!=identity:
                raise ValueError('paper source entry changed its read action identity')
            observation=entry.source_observation
            if read(entry.observation_ref)!=canonical_json_bytes(observation):
                raise ValueError('paper source entry differs from the sealed observation')
            origin=observation['originator']
            if (origin['execution_id'],origin['stage_name'],origin['model_profile'],origin['base_artifact_sha256'])!=(
                compilation.execution_id,FUSION_SELECTION_STAGE,turn['model_profile'],work.package_sha256):
                raise ValueError('paper source observation changed its producer')
            created=observation['created_from_artifact_refs']
            refs=[]
            for public in created:
                candidates=[ref for ref in closure if (ref.sha256,ref.media_type,ref.byte_size)==(
                    public['sha256'],public['media_type'],public['byte_size'])]
                if len(candidates)!=1:
                    raise ValueError('paper source origin lacks its precise sealed artifact')
                read(candidates[0]); refs.append(candidates[0])
            if not {ArtifactRef.model_validate(turn['request_ref']),ArtifactRef.model_validate(turn['response_ref'])}<=set(refs):
                raise ValueError('paper source origin belongs to a different model turn')
            for key,media in [('prompt_sha256','text/vnd.tsdaf.investigation-prompt+markdown'),
                              ('provider_schema_sha256','application/vnd.tsdaf.investigation-tools+json')]:
                if not any(ref.sha256==origin[key] and ref.media_type==media for ref in refs):
                    raise ValueError('paper source origin lacks its original prompt or declaration')
            selection=item.select
            whole=selection is None or isinstance(selection,WholeSelection)
            reader=SourceReader(package,package_root)
            anchor=observation['anchor']
            if modality=='TEXT':
                if not whole and (not isinstance(selection,TextSelection) or selection.adjacent_blocks):
                    raise ValueError('paper source text selection is unsupported')
                reader.verify_text()
                block=next(b for b in package.blocks if b.block_id==sid)
                span=resolve_text_selection(raw_text=block.raw_text,
                    start_sentence=0 if whole else selection.start_sentence,
                    end_sentence=len(block_sentence_spans(block.raw_text)) if whole or selection.end_sentence is None else selection.end_sentence)
                if observation_texts([observation],[package]).get(observation['observation_id']) != block.raw_text[span.char_start:span.char_end]:
                    raise ValueError('paper source text hashes differ from its original bytes')
                fragments=anchor['fragments']
                if len(fragments)!=1 or (fragments[0]['block_id'],fragments[0]['char_start'],fragments[0]['char_end'])!=(sid,span.char_start,span.char_end):
                    raise ValueError('paper source text anchor differs from the delivered selection')
            else:
                if not whole and not isinstance(selection,ImageSelection):
                    raise ValueError('paper source image selection is unsupported')
                source=reader.visual_source(sid)
                asset=anchor['source_asset']
                if ((asset['sha256'],asset['media_type'],asset['byte_size']) !=
                        (source.source_asset.sha256,source.source_asset.media_type,source.source_asset.byte_size)
                        or anchor['source_dimensions'] != dict(zip(('width_px','height_px'),source.dimensions,strict=True))
                        or anchor['page_index'] != source.page_index):
                    raise ValueError('paper source image anchor differs from its original asset')
                x0,y0,x1,y1=(0,0,1000,1000) if whole else selection.region.xyxy()
                box=resolve_visual_selection(x0=x0,y0=y0,x1=x1,y1=y1,dimensions=source.dimensions)
                if anchor['bbox_px']!=asdict(box):
                    raise ValueError('paper source image anchor differs from the delivered selection')
                if 'display_mapping_ref' not in logged:
                    raise ValueError('historical visual read lacks its sealed display mapping; it cannot be reconstructed')
                payload=reader.read_visuals(work_unit=work,visual_ids=(sid,),source_inventory=inventory)[sid]
                view=read_cached_local_visual_read(source=source,original_bytes=payload,
                    mapping_ref=ArtifactRef.model_validate(logged['display_mapping_ref']),
                    store=SimpleNamespace(verify=lambda ref:read(ref),read_bytes=read))
                mapping=json.loads(read(view.mapping_ref))
                if view.sampled_box!=box or mapping['sampling']!=('overview-lanczos-1024' if whole else 'original-pixels'):
                    raise ValueError('paper source display mapping differs from the delivered selection')
            expected=paper_source_reply(entry,item.location,selection,package)
            if derived: expected['derived']=derived
            if row!=expected:
                raise ValueError('paper source was not delivered by the accepted read')
            sources.citations[expected['citation']]=[entry.evidence_ref.model_dump(mode='json')]
            sources.observations.append(observation)
            consumed.append(logged)
    def compile_proposal(document,action):
        return FinalDocumentCompiler(paper_id=receipt.paper_id,submission=compilation.execution_id+'/'+action,
            sources=sources.sources,citations=sources.citations,
            submission_refs=[submission_origin(compilation.execution_id,action)]).compile_science(document)
    science=verify_document_transcript(compilation,answer=answer,delivery=delivery,read=read,
        closure=closure,compile_proposal=compile_proposal,submit_model=SubmitSkeletonDocument,before_call=before_call,
        statement_scope=('SKELETON',None,None) if skeleton_form()=='STATEMENTS' else None)
    if consumed!=list(receipt.source_reads):
        raise ValueError('paper source receipts have no corresponding delivered reads')
    return receipt,science,sources,terminal
