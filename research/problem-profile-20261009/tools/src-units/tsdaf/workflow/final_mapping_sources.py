"""Readable source choices from verified selections and explicit baseline bindings."""
from tsdaf.contracts.literature.objects import experiment_operations
from tsdaf.lanes.literature.source_manifest import general_method_targets


def mapping_source_choices(machine, handoff, sources):
    entries = {}
    def add(location, title):
        key = tuple(location.get(k) for k in ('unit_block_id','target_source_document_id','target_source_revision_id'))
        entries.setdefault(key,(location,title))
    # Already published locations may be intentionally incomplete in an older
    # contract. Preserve that declaration; do not infer a document from a block ID.
    for oi,operation in enumerate(experiment_operations(machine),1):
        for si,source in enumerate(operation.get('procedure_sources',()),1):
            if source.get('unit_block_id') is not None:
                add({k:source[k] for k in ('unit_block_id','target_source_document_id','target_source_revision_id') if k in source},
                    f'Baseline operation {oi} procedure source {si}')
    for index,binding in enumerate(handoff.context()['record_selections'].values(),1):
        if binding.get('exact_text') is None: continue
        identity=binding['source_identity'];document=sources.document(identity['document_revision_id'])
        block=next(b for b in document.package.blocks if b.block_id==identity['source_id'])
        add({'unit_block_id':block.block_id,'target_source_document_id':block.source.document_id,
             'target_source_revision_id':identity['document_revision_id']},
            f"{identity['source_phase']} passage {index}: {binding['exact_text'][:100]}")
    for index,target in enumerate(general_method_targets(sources.active()).values(),1):
        add({'unit_block_id':target['unit_block_id'],'target_source_document_id':target['document_id'],
             'target_source_revision_id':target['document_revision_id']},f'Upstream general methods {index}')
    return {f'Source {i}: {title}':location for i,(location,title) in enumerate(entries.values(),1)}
