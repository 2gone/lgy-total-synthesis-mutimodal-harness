"""Canonical source catalogue declarations shared by compilation and source reads."""

from __future__ import annotations

import hashlib
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from tsdaf.contracts._base import ContractModel, _sha256, _stable_string, _unique
from tsdaf.contracts.schema import canonical_json_bytes
from tsdaf.lanes.literature.layer0 import Layer0Shard

# Why a cleaned, referenceable block is not in a route group's initial selection: it
# is the experimental part. The detail lane is built to read it.
EXPERIMENTAL_PART_EXCLUSION = "EXPERIMENTAL_PART"


class SourceInventoryEntry(ContractModel):
    source_id: str
    source_kind: Literal["TEXT_BLOCK", "VISUAL_ASSET"]
    source_sha256: str
    shard_id: str
    router_role: str | None
    initial_route_group_ids: tuple[str, ...]
    initially_excluded: bool
    exclusion_reason: str | None
    readable: bool
    sentence_count: int | None = Field(default=None, ge=0)

    @field_validator("source_id", "shard_id")
    @classmethod
    def _identity(cls, value: str) -> str:
        return _stable_string(value, label="source inventory identity")

    @field_validator("source_sha256")
    @classmethod
    def _hash(cls, value: str) -> str:
        return _sha256(value, label="source inventory hash")

    @model_validator(mode="after")
    def _scope(self) -> Self:
        _unique(self.initial_route_group_ids, label="source initial route groups")
        if self.initially_excluded != (self.exclusion_reason is not None):
            raise ValueError("source exclusion must carry its reason")
        if self.initially_excluded and self.initial_route_group_ids:
            raise ValueError("an excluded source cannot be initially selected")
        return self


class LiteratureSourceInventory(ContractModel):
    schema_version: Literal["tsdaf.literature-source-inventory.v1"]
    paper_id: str
    package_sha256: str
    bootstrap_id: str
    bootstrap_sha256: str
    route_group_ids: tuple[str, ...]
    shards: tuple[Layer0Shard, ...]
    entries: tuple[SourceInventoryEntry, ...]
    inventory_sha256: str

    @field_validator("package_sha256", "bootstrap_sha256", "inventory_sha256")
    @classmethod
    def _hash(cls, value: str) -> str:
        return _sha256(value, label="source inventory identity hash")

    @model_validator(mode="after")
    def _identity(self) -> Self:
        _unique(self.route_group_ids, label="source inventory route groups")
        _unique(tuple(item.source_id for item in self.entries), label="source inventory sources")
        ownership = {
            source_id: shard.shard_id
            for shard in self.shards
            for source_id in (*shard.owned_block_ids, *shard.owned_visual_ids)
        }
        if len(ownership) != sum(
            len(shard.owned_block_ids) + len(shard.owned_visual_ids) for shard in self.shards
        ):
            raise ValueError("source inventory has duplicate Layer 0 ownership")
        if set(ownership) != {item.source_id for item in self.entries}:
            raise ValueError("source inventory does not cover every Layer 0 owner")
        if any(shard.package_sha256 != self.package_sha256 for shard in self.shards):
            raise ValueError("source inventory shards belong to another package")
        for entry in self.entries:
            if ownership[entry.source_id] != entry.shard_id or not set(
                entry.initial_route_group_ids
            ) <= set(self.route_group_ids):
                raise ValueError("source inventory ownership or route scope is invalid")
        if _digest(self, "inventory_sha256") != self.inventory_sha256:
            raise ValueError("source inventory hash differs from its projection")
        return self


def _digest(value: ContractModel, hash_field: str) -> str:
    return hashlib.sha256(
        canonical_json_bytes(value.model_dump(mode="json", exclude={hash_field}))
    ).hexdigest()
