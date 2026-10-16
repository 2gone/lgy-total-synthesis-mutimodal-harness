"""What COMPLETE may not leave: prose unread (E76 ⑥), and a step with no account (E100);
and what a COMPLETE fusion needs: a target and a route. PARTIAL may stop without them.

A detail investigation's COMPLETE is a claim about the experimental part. The Host
already keeps the read ledger; this module says which unread blocks count against
the claim: paragraphs of at least ``MIN_SENTENCES`` sentences that are not closing
matter. Headings, one-line references, notes and the acknowledgments a split leaves
behind the experimental part are not the experiment, and never block a finish.

Thresholds were read off two real detail investigations (2026-09-12 runs, fixtures
under ``tests/fixtures/real_receipts``): decumbenine_B finished COMPLETE with five
such paragraphs unread (the general methods and four general procedures), and
alchivemycin_A's only unread prose was acknowledgments and author contributions.
"""

from __future__ import annotations

from collections.abc import Container, Iterable, Mapping
from typing import TYPE_CHECKING, Any, Protocol

from tsdaf.lanes.literature.fact_projection import is_route_step
from tsdaf.lanes.literature.selection_resolution import block_sentence_spans

if TYPE_CHECKING:
    from tsdaf.lanes.literature.gate_models import FusionResult

PRODUCT_ACCOUNT_GAP = "PRODUCT_WITHOUT_AN_ACCOUNT"
STEP_ACCOUNT_GAP = "STEP_WITHOUT_AN_ACCOUNT"

__all__ = (
    "CLOSING_MATTER_HEADINGS",
    "MIN_SENTENCES",
    "finish_left",
    "steps_without_an_account",
    "products_without_an_account",
    "unread_experimental_prose",
)

# A paragraph shorter than this is a line the reader can take in from the directory
# (a reference, a footnote, a caption); one at least this long is prose to be read.
MIN_SENTENCES = 3

# Headings that close a paper: everything under one of them until the next heading is
# front or back matter, not the experiment. The one place these are named.
CLOSING_MATTER_HEADINGS: tuple[str, ...] = (
    "acknowledgements",
    "acknowledgments",
    "acknowledgment",
    "author contributions",
    "author information",
    "competing interests",
    "conflict of interest",
    "conflicts of interest",
    "declaration of competing interest",
    "additional information",
    "supporting information",
    "supporting information available",
    "associated content",
    "data availability",
    "references",
    "references and notes",
    "notes and references",
    "funding",
    "notes",
)

# Prose that closes a paper without a heading of its own, by how it starts.
_CLOSING_MATTER_STARTS: tuple[str, ...] = (
    "acknowledgment",
    "acknowledgement",
    "supporting information available",
    "correspondence and requests",
    "reprints and permissions",
    "publisher's note",
    "peer review information",
    "©",
    "the authors declare",
)


class _Block(Protocol):
    @property
    def block_id(self) -> str: ...

    @property
    def block_type(self) -> object: ...

    @property
    def raw_text(self) -> str: ...


def _heading_text(block: _Block) -> str:
    return " ".join(block.raw_text.split()).casefold().rstrip(".:")


def _is_closing_heading(block: _Block) -> bool:
    text = _heading_text(block)
    return any(
        text == heading or text.startswith(heading + " ") for heading in CLOSING_MATTER_HEADINGS
    )


def unread_experimental_prose(
    blocks: Iterable[_Block],
    *,
    readable_ids: Iterable[str],
    delivered_scopes: Iterable[Mapping[str, Any]],
    min_sentences: int = MIN_SENTENCES,
) -> list[dict[str, Any]]:
    """Experimental paragraphs not fully delivered, for activity diagnostics.

    ``readable_ids`` are the experimental part, not every authorized cross-route
    source; ``delivered_scopes`` are the sentence ranges it did read. A paragraph counts
    when it is readable, has at least ``min_sentences`` sentences, is not wholly read,
    and is not closing matter (under a closing heading, or starting like one).
    """
    readable = set(readable_ids)
    covered: dict[str, set[int]] = {}
    for scope in delivered_scopes:
        source = scope.get("source_id")
        if isinstance(source, str):
            covered.setdefault(source, set()).update(
                range(int(scope.get("start_sentence", 0)), int(scope.get("end_sentence", 0)))
            )
    unread: list[dict[str, Any]] = []
    closing = False
    for block in blocks:
        kind = str(getattr(block.block_type, "value", block.block_type))
        if kind == "SECTION_HEADING":
            closing = _is_closing_heading(block)
            continue
        if block.block_id not in readable or kind != "PARAGRAPH" or closing:
            continue
        text = " ".join(block.raw_text.split())
        # The typographic apostrophe is the straight one for this comparison; the label
        # module is the only place that handles the character itself.
        if text.casefold().replace("\u2019", "'").startswith(_CLOSING_MATTER_STARTS):
            continue
        count = len(block_sentence_spans(block.raw_text))
        if count < min_sentences or covered.get(block.block_id, set()) >= set(range(count)):
            continue
        unread.append({"source_id": block.block_id, "sentence_count": count, "head": text[:80]})
    return unread


def route_steps(view: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """Route activity excludes independent accounts; neither implies completion."""
    return [entry for entry in view["containers"] if "reaction_block_id" in entry]


def steps_without_an_account(
    view: Mapping[str, Any],
    *,
    written_ids: Iterable[str],
    named_scopes: Iterable[str],
) -> list[dict[str, str]]:
    """The steps a COMPLETE claim ends with neither a value nor a reason, in route order.

    The task sheet is the route step by step (E97), and a step ends one of two ways: the
    author wrote something and it is recorded against that step, or a limitation names
    that step and says why it has nothing to record. The account is asked of the step,
    not of each empty field: most steps state no pressure and no intermediate, and a
    reason demanded for every such field is the table-filling this lane is moving away
    from. A value counts in whatever form the author wrote it -- a patched field, a
    condition in his own words, a yield, a characterization of what the step makes.

    `written_ids` is every public id the findings name; `named_scopes` is the scope of
    each limitation, which names a step by its `reaction_block_id`.
    """
    written = set(written_ids)
    excused = set(named_scopes)
    unaccounted: list[dict[str, str]] = []
    for block in route_steps(view):
        block_id = str(block["reaction_block_id"])
        own = {block_id}
        own.update(str(member["target_id"]) for member in block.get("operations", ()))
        own.update(str(member["target_id"]) for member in block.get("measurements", ()))
        for key in ("input_compound_ids", "output_compound_ids"):
            value = block.get("known", {}).get(key)
            if isinstance(value, list):
                own.update(str(item) for item in value)
        if own & written or block_id in excused:
            continue
        unaccounted.append({"reaction_block_id": block_id, "step": str(block.get("step", ""))})
    return unaccounted


def products_without_an_account(
    view: Mapping[str, Any],
    *,
    characterized_ids: Iterable[str],
    named_scopes: Iterable[str],
) -> list[dict[str, str]]:
    """Each dispatched product needs a card or an unambiguously scoped reason.

    Existing cards count too: this is an account, not an obligation to write a second
    copy. A limitation naming a single-product step names that product unambiguously
    (the e106/e111 receipts do this for unisolated intermediates). A multi-product step
    cannot excuse all its products this way; each must be named separately. No names
    or chemical meaning are guessed from the limitation's prose.
    """
    scopes = set(named_scopes)
    accounted = set(characterized_ids) | scopes
    products: dict[str, str] = {}
    for block in route_steps(view):
        output = block.get("known", {}).get("output_compound_ids")
        if not isinstance(output, list):
            continue
        if len(output) == 1 and block["reaction_block_id"] in scopes:
            accounted.add(str(output[0]))
        for compound_id in output:
            products.setdefault(str(compound_id), str(block["reaction_block_id"]))
    return [
        {"compound_id": compound_id, "reaction_block_id": block_id}
        for compound_id, block_id in products.items()
        if compound_id not in accounted
    ]


def candidates_left_unanswered(
    candidates: Iterable[Mapping[str, Any]],
    *,
    cited_selection_ids: Container[str],
    named_scopes: Iterable[str],
) -> list[dict[str, Any]]:
    """Audit offered candidates without a saved citation, not scientific obligations.

    An uncited host guess may be irrelevant or already represented upstream.
    Its absence from a write cannot override the model's completion judgment.
    """
    excused = {str(scope) for scope in named_scopes}
    return [
        dict(candidate)
        for candidate in candidates
        if str(candidate.get("selection_id")) not in cited_selection_ids
        and str(candidate.get("reaction_block_id")) not in excused
    ]


def cited_sites(
    selection_ids: Iterable[str], selections: Mapping[str, Mapping[str, Any]]
) -> set[tuple[str, str]]:
    """The source blocks these citations point at, by revision and block.

    A citation names a selection and a selection names a range of one block; the range is
    dropped because the question asked of it is whether a block the host delivered was
    used at all, and a task that cited two sentences of the paragraph it was handed used
    the paragraph. One rule for the running task's progress view and for publication's
    reconciliation of the sealed receipt.
    """
    sites: set[tuple[str, str]] = set()
    for selection_id in selection_ids:
        identity = selections.get(selection_id, {}).get("source_identity")
        if isinstance(identity, Mapping):
            sites.add((str(identity["document_revision_id"]), str(identity["source_id"])))
    return sites


def finish_left(result: FusionResult) -> list[str]:
    """What a fusion's COMPLETE still lacks, one sentence each.

    A COMPLETE finish refuses with all of them at once, and progress names the same
    sentences every turn, so the list is written here and nowhere else.

    - A target: publication needs one TARGET fact owned by an accepted compound (the
      paper's "NO_SOURCE_BOUND_TARGET" readiness); a group that finished without one
      surfaced that three stages later, when nothing could be published (E56).
    - A route: at least one route step. Whether a step has operations is not asked: a
      step the main text draws is a block with or without one, and the experimental
      detail lane is sent to every block (2026-09-23 ruling), so "every reaction detailed
      or handed off" -- the check this replaced -- would press the model to invent
      operations or hand work to no one. What stays is the JOC r4 shape (ledger E51):
      nothing written, so there is no route to publish.
    """
    facts = {f.fact_id: f for f in result.facts}
    left: list[str] = []
    if not any(
        fact.kind == "TARGET"
        and fact.status == "ACCEPTED"
        and len(fact.owners) == 1
        and (owner := facts.get(fact.owners[0])) is not None
        and owner.kind == "COMPOUND"
        and owner.status == "ACCEPTED"
        for fact in result.facts
    ):
        left.append(
            "no target is on record: write one TARGET fact with the author's target "
            "statement, owned by the accepted COMPOUND it names, before COMPLETE delivery"
        )
    if not any(is_route_step(result, fact, facts) for fact in result.facts):
        left.append(
            "no route reaction is on record: write each step's REACTION with its inputs and "
            "outputs from the narrative and the schemes before COMPLETE delivery"
        )
    return left
