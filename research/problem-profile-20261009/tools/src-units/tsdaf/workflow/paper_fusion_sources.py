"""Original group provenance and named reads for the paper-wide compilation.

Input construction and transcript replay use this same adapter. It does not infer
scientific ownership from the group that supplies reading authority.
"""
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.contracts.tool_read import WholeSelection, TextSelection, ImageSelection, item_name
from tsdaf.lanes.literature.evidence import ObservationOriginator
from tsdaf.lanes.literature.evidence_binding import (
    FUSION_SELECTION_STAGE, bind_fusion_evidence, source_location,
)
from tsdaf.lanes.literature.final_mapping import mapping_resources
from tsdaf.lanes.literature.lane_models import TextFragmentSelection, VisualRegionSelection
from tsdaf.lanes.literature.local_visual_read import materialize_local_visual_read
from tsdaf.lanes.literature.selection_resolution import block_sentence_spans, ResolvedPixelBox
from tsdaf.lanes.literature.source_inventory import LiteratureSourceInventory, resolve_fusion_read_authority
from tsdaf.lanes.literature.source_reads import SourceReader
from tsdaf.workflow.resource_reads import ReadPlan, ReadValue, ReadFailure, read_item_action_id


def paper_fusion_inputs(*, groups, package, store):
    """Pure assembly from verified group inputs; publication seals nothing."""
    sources, citations, locations, readings = {}, {}, {}, []
    observations = [entry.source_observation for group in groups for entry in group.evidence.entries]
    owners = {}
    for group in groups:
        inventory = LiteratureSourceInventory.model_validate_json(store.read_bytes(group.receipt.source_inventory_ref))
        authority = resolve_fusion_read_authority(work_unit=group.work_unit, package=package, source_inventory=inventory)
        for sid in (*authority.text_block_ids, *authority.visual_ids):
            owners.setdefault(sid, (group, inventory))
    by_source = {}
    for index, block in enumerate(package.blocks, 1):
        if block.block_id not in owners:
            continue
        name = f'MAIN passage {index}: {block.raw_text[:80]}'
        locations[name] = (block.block_id, 'TEXT', *owners[block.block_id])
        sources[name] = {'unit_block_id':block.block_id,
            'target_source_document_id':block.source.document_id,
            'target_source_revision_id':owners[block.block_id][0].work_unit.document_revision_id}
        by_source[block.block_id] = name
    for index, visual in enumerate(package.visual_assets, 1):
        if visual.visual_id in owners:
            name = f'MAIN original image {index}'
            locations[name] = (visual.visual_id, 'VISUAL', *owners[visual.visual_id])
            by_source[visual.visual_id] = name
    for gi, group in enumerate(groups, 1):
        for modality, sealed in (('TEXT',group.proposals.text),('VISUAL',group.proposals.visual)):
            selections = {}
            for ei, entry in enumerate(group.evidence.entries, 1):
                if entry.modality != modality:
                    continue
                name = f'Group {gi} {modality.lower()} source {ei}'
                citations[name] = [entry.evidence_ref.model_dump(mode='json')]
                selections[entry.selection_id] = name
            reading = sealed.proposal
            readings.append({'group':f'Group {gi}', 'modality':modality,
                'overview':reading.overview, 'limitations':reading.limitations,
                'observations':[{'scope':row.scope,'text':row.text,
                    'citations':[selections[sid] for sid in row.selection_ids if sid in selections]}
                    for row in reading.observations],
                'original_sources':[{'citation':selections[e.selection_id],
                    'location':by_source[e.canonical_source_id]}
                    for e in group.evidence.entries if e.modality==modality]})
    return dict(groups=groups, sources=sources, citations=citations, locations=locations,
                readings=readings, observations=observations)


def paper_fusion_initial(sources, *, failures):
    return {'group_readings':sources.readings, 'reading_failures':failures,
        'original_sources':list(sources.locations), 'procedure_sources':list(sources.sources),
        'bound_source_citations':list(sources.citations)}


def paper_source_reply(entry, location, selection, package):
    """One display of a sealed source selection for execution and verification."""
    selected=source_location(entry,location,package)
    whole=selection is None or isinstance(selection,WholeSelection)
    label=location if whole else location+' '+str(selection.model_dump(mode='json'))
    reply={'outcome':'READ','location':location,'citation':label,'modality':entry.modality}
    if entry.modality=='TEXT': reply['sentences']=selected['sentences']
    else: reply.update(source_dimensions=selected['source_dimensions'],region=selected['region'])
    return reply


class PaperFusionSources:
    """Host-owned names and bindings, scoped to the actual sealed group inputs."""

    def __init__(self, *, groups, package, package_root, store, execution_id, bind):
        self.groups, self.package, self.store = groups, package, store
        self.execution_id, self.bind = execution_id, bind
        self.reader = SourceReader(package, package_root)
        self.__dict__.update(paper_fusion_inputs(groups=groups, package=package, store=store))
        self.read_receipts = []
        prompt, tools = mapping_resources(skeleton=True)
        self.prompt_ref = store.put_bytes(prompt.encode(), media_type='text/vnd.tsdaf.investigation-prompt+markdown')
        self.schema_ref = store.put_bytes(canonical_json_bytes(tools), media_type='application/vnd.tsdaf.investigation-tools+json')
        bind(self.prompt_ref, self.schema_ref)

    def resolve(self, location, selection, ctx, citations):
        if location not in self.locations:
            raise ReadFailure('UNAVAILABLE','Choose the named original passage or image.',
                available_reads=[{'location':name} for name in self.locations])
        sid, modality, group, inventory = self.locations[location]
        whole = selection is None or isinstance(selection, WholeSelection)
        texts, images = (), ()
        # Selection identities are internal; readable citation names are returned.
        identity = read_item_action_id(ctx.interaction_sha256, ctx.call_index, item_name(ctx.read_item))
        if modality=='TEXT':
            if not whole and not isinstance(selection,TextSelection):
                raise ReadFailure('UNSUPPORTED_SELECTION','This passage supports whole or text_range.')
            if not whole and selection.adjacent_blocks:
                raise ReadFailure('UNSUPPORTED_SELECTION','Read the separately named neighboring passage to retain its source identity.')
            block = next(b for b in self.package.blocks if b.block_id==sid)
            texts=(TextFragmentSelection(selection_id=identity,block_id=sid,
                start_sentence=0 if whole else selection.start_sentence,
                end_sentence=len(block_sentence_spans(block.raw_text)) if whole or selection.end_sentence is None else selection.end_sentence),)
        else:
            if not whole and not isinstance(selection,ImageSelection):
                raise ReadFailure('UNSUPPORTED_SELECTION','This image supports whole or image_region.')
            x0,y0,x1,y1=(0,0,1000,1000) if whole else selection.region.xyxy()
            images=(VisualRegionSelection(selection_id=identity,visual_id=sid,x0=x0,y0=y0,x1=x1,y1=y1),)

        def perform():
            direct,payloads=bind_fusion_evidence(work_unit=group.work_unit,source_reader=self.reader,
                source_inventory=inventory,execution_id=self.execution_id,
                text_selections=texts,visual_selections=images,
                originator=ObservationOriginator(originator_kind='MODEL',stage_name=FUSION_SELECTION_STAGE,
                    stage_version='2.0.0',execution_id=self.execution_id,model_profile=ctx.record['model_profile'],
                    prompt_sha256=self.prompt_ref.sha256,provider_schema_sha256=self.schema_ref.sha256,
                    requested_by_reviewer_id=None,base_artifact_sha256=group.work_unit.package_sha256),
                created_from=(self.prompt_ref,self.schema_ref,ctx.request_ref,ctx.response_ref),artifact_store=self.store)
            entry=direct.registry.entries[0]
            selected=source_location(entry,location,self.package)
            receipt=paper_source_reply(entry,location,selection,self.package)
            content=[]; pixels=0; mapping_ref=None; artifacts=[direct.registry_ref,entry.observation_ref]
            if modality=='VISUAL':
                view=materialize_local_visual_read(source=self.reader.visual_source(sid),
                    original_bytes=payloads[sid],sampled_box=ResolvedPixelBox(**selected['bbox_px']),
                    store=self.store,overview=whole)
                mapping_ref=view.mapping_ref
                artifacts.extend((view.mapping_ref,view.image_ref))
                content=[{'type':'image','mime_type':'image/png',
                    'artifact_ref':view.image_ref.model_dump(mode='json'),'resolution':'high'}]
                pixels=view.dimensions[0]*view.dimensions[1]
            def accept():
                self.bind(*artifacts)
                citations[receipt['citation']]=[entry.evidence_ref.model_dump(mode='json')]
                self.observations.append(entry.source_observation)
                self.read_receipts.append({'interaction_sha256':ctx.interaction_sha256,
                    'call_index':ctx.call_index,'location':location,
                    'selection':selection.model_dump(mode='json') if selection else None,
                    'registry_ref':direct.registry_ref.model_dump(mode='json'),
                    **({'display_mapping_ref':mapping_ref.model_dump(mode='json')} if mapping_ref else {})})
            return ReadValue(receipt,content,image_pixels=pixels,accept=accept)
        return ReadPlan(perform=perform)
