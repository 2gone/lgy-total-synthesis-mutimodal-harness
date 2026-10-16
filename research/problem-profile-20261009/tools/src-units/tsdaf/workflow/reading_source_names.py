"""Bind task-local source names without rebinding a prior accepted observation."""

from tsdaf.lanes.literature.container_enrichment import content_hash
from tsdaf.workflow.literature_fusion_actions import name_binder


def reading_source_binder(base, selections):
    """The model keeps its delivered name; only its private evidence key changes.

    A fresh read of the same passage can have a different observation producer.
    Existing facts retain their accepted source. Both exact readings may coexist,
    and replay derives the same keys from the sealed selections without a journal.
    """
    bind = name_binder(base)
    aliases = {}
    for sid, selection in selections.items():
        original = bind(sid)
        refs = (dict(selection["evidence_ref"]),)
        key = original
        if original in base.sources and base.sources[original] != refs:
            key = bind("read-source:" + content_hash([sid, refs]))
        if key in base.sources and base.sources[key] != refs:
            raise ValueError("reading source key has conflicting immutable evidence")
        aliases[sid] = key
    return lambda token: aliases.get(token, bind(token))
