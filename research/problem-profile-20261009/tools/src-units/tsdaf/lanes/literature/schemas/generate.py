"""Generate or verify deterministic provider schemas from Pydantic authority."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[4]
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from pydantic import BaseModel  # noqa: E402

from tsdaf.contracts.schema import canonical_json_bytes  # noqa: E402
from tsdaf.contracts.tool_declaration import provider_declaration_schema  # noqa: E402
from tsdaf.contracts.tool_read import ReadRequest  # noqa: E402
from tsdaf.lanes.document.reading_units import ReadingUnitsAnswer  # noqa: E402
from tsdaf.lanes.literature.gate_models import (  # noqa: E402
    FUSION_TOOL_MODELS,
    FinishFusion,
)
from tsdaf.lanes.literature.profiles import generated_schema_bytes  # noqa: E402
from tsdaf.lanes.literature.source_investigation import investigation_tool_models  # noqa: E402

SCHEMA_DIR = Path(__file__).resolve().parent
DOCUMENT_SCHEMA_DIR = SCHEMA_DIR.parents[1] / "document" / "schemas"
TARGETS = {
    # The document lane's one answer schema is pinned by the same gate: a drift in
    # what the reading-units model is asked to return would otherwise go unnoticed.
    "UNITS": DOCUMENT_SCHEMA_DIR / "reading_units_v1.json",
    "T1": SCHEMA_DIR / "text_route_v1.json",
    "V1": SCHEMA_DIR / "visual_route_v1.json",
    "V3": SCHEMA_DIR / "visual_detail_v1.json",
    "FUSION": SCHEMA_DIR / "fusion_v1.json",
    "FUSION_FINISH": SCHEMA_DIR / "fusion_finish_v1.json",
    "FUSION_READ": SCHEMA_DIR / "fusion_read_v1.json",
    "INVESTIGATION_WRITE_ED": SCHEMA_DIR / "investigation_write_experimental_detail_v1.json",
    "INVESTIGATION_WRITE_ED_PAPER": SCHEMA_DIR
    / "investigation_write_experimental_detail_paper_v1.json",
    "INVESTIGATION_WRITE_ED_BLOCK": SCHEMA_DIR
    / "investigation_write_experimental_detail_block_v1.json",
    "INVESTIGATION_WRITE_SI": SCHEMA_DIR / "investigation_write_supplemental_information_v1.json",
    "INVESTIGATION_FINISH": SCHEMA_DIR / "investigation_finish_v1.json",
    "INVESTIGATION_WRITE_READING": SCHEMA_DIR / "investigation_write_experimental_reading_v1.json",
    "INVESTIGATION_WRITE_READING_OPEN": SCHEMA_DIR
    / "investigation_write_experimental_reading_v1_5.json",
    "INVESTIGATION_FINISH_READING": SCHEMA_DIR
    / "investigation_finish_experimental_reading_v1.json",
    "INVESTIGATION_FINISH_SI": SCHEMA_DIR / "investigation_finish_supplemental_information_v1.json",
    "INVESTIGATION_WRITE_RECORDING": SCHEMA_DIR / "investigation_write_experimental_recording_v1.json",
    "INVESTIGATION_FINISH_RECORDING": SCHEMA_DIR / "investigation_finish_experimental_recording_v1.json",
}
# Every tool declaration a model is shown is pinned here, as it is shown it: through the
# one renderer the lanes build their declarations with (`provider_declaration_schema`). A write
# not listed drifts silently; one pinned from the raw model schema would pin a
# declaration no task is ever given. Both scopes of the detail write are listed: the
# paper's task and the task given a few reaction blocks are shown different tools.
DECLARATION_MODELS: dict[str, type[BaseModel]] = {
    "FUSION": FUSION_TOOL_MODELS["write"],
    "FUSION_FINISH": FinishFusion,
    "FUSION_READ": ReadRequest,
    "INVESTIGATION_WRITE_ED": investigation_tool_models("EXPERIMENTAL_DETAIL")["write"],
    "INVESTIGATION_WRITE_ED_BLOCK": investigation_tool_models("EXPERIMENTAL_DETAIL", task="BLOCK")[
        "write"
    ],
    "INVESTIGATION_WRITE_ED_PAPER": investigation_tool_models("EXPERIMENTAL_DETAIL", task="PAPER")[
        "write"
    ],
    "INVESTIGATION_WRITE_SI": investigation_tool_models("SUPPLEMENTAL_INFORMATION")["write"],
    "INVESTIGATION_FINISH": investigation_tool_models("EXPERIMENTAL_DETAIL")[
        "finish_investigation"
    ],
    "INVESTIGATION_WRITE_READING": investigation_tool_models("EXPERIMENTAL_READING")["write"],
    "INVESTIGATION_WRITE_READING_OPEN": investigation_tool_models(
        "EXPERIMENTAL_READING", open_expression=True
    )["write"],
    "INVESTIGATION_FINISH_READING": investigation_tool_models("EXPERIMENTAL_READING")[
        "finish_investigation"
    ],
    "INVESTIGATION_FINISH_SI": investigation_tool_models("SUPPLEMENTAL_INFORMATION")[
        "finish_investigation"
    ],
}
DECLARATION_MODELS.update({
    "INVESTIGATION_WRITE_RECORDING": investigation_tool_models("EXPERIMENTAL_RECORDING")["write"],
    "INVESTIGATION_FINISH_RECORDING": investigation_tool_models("EXPERIMENTAL_RECORDING")["finish_investigation"],
})
# An answer schema is not a tool declaration: it is the response shape a lane pins and
# verifies against the resource on disk, and it is rendered raw.
ANSWER_MODELS: dict[str, type[BaseModel]] = {"UNITS": ReadingUnitsAnswer}


def expected(stage: str) -> bytes:
    declaration = DECLARATION_MODELS.get(stage)
    if declaration is not None:
        return canonical_json_bytes(provider_declaration_schema(declaration))
    answer = ANSWER_MODELS.get(stage)
    if answer is not None:
        return canonical_json_bytes(answer.model_json_schema(mode="validation"))
    return generated_schema_bytes(stage)  # type: ignore[arg-type]


def drift() -> list[str]:
    """Snapshot files that are missing or no longer match the models they came from."""
    drifted: list[str] = []
    for stage, path in TARGETS.items():
        try:
            actual = path.read_bytes()
        except OSError:
            drifted.append(path.name)
            continue
        if actual != expected(stage):
            drifted.append(path.name)
    return drifted


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.check:
        drifted = drift()
        if drifted:
            print("schema drift: " + ", ".join(drifted), file=sys.stderr)
            return 1
        return 0
    for stage, path in TARGETS.items():
        path.write_bytes(expected(stage))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
