"""Model-visible citation timing, shared by prompts and rejection diagnostics."""

SOURCE_SELECTION_MEANING = (
    "Use a supplied source selection permitted by this task's citation policy: "
    "delivered source content or a verified inherited source accompanying its meaning. "
    "A search hit or a source requested in this response is not yet citable."
)

SOURCE_CITATION_POLICY = (
    "New source findings require source content delivered before this response, including "
    "SOURCE in the initial input and completed prior read results. Cite its delivered IDs "
    "without rereading. A read requested in this response becomes evidence only after its "
    "content reaches you; search hits, directories, previews and attachment cards are not "
    "delivered source content."
)

INHERITED_CITATION_POLICY = (
    "Sources accompanying a delivered inherited finding may also be retained with that finding."
)

FINAL_CITATION_POLICY = (
    "Sources accompanying the verified scientific_records may be retained with their meaning "
    "without rereading. This inherited citation authority does not claim you read their originals. "
    + SOURCE_CITATION_POLICY
)
