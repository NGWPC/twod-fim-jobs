from datetime import datetime
from typing import Annotated, ClassVar, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializeAsAny,
    StringConstraints,
    model_serializer,
    model_validator,
)

import twod_fim_jobs
from twod_fim_jobs.consts import (
    DEFAULT_DRAINAGE_AREA_THRESHOLD_PERCENT,
    DEFAULT_LAKE_AREA_THRESHOLD_SQKM,
    DEFAULT_MAX_LENGTH_THRESHOLD_KM,
    DEFAULT_NEGATIVE_LAKE_BUFFER_METERS,
)
from twod_fim_jobs.models.common import Asset, JobWarning

Hash8 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{8}$")]
"""First 8 hex of SHA-256 (32-bit) — identity-hash role length."""

Count = Annotated[int, Field(ge=0)]
"""A reach counter value; None means the producing branch did not run."""


### WARNINGS ###


class _CodedWarning(JobWarning):
    """JobWarning that serializes its class-level ``code`` alongside ``message``.

    ``JobWarning.code`` is a ClassVar, which pydantic does not serialize, but
    network.schema.json requires every persisted warning to carry its code.
    """

    @model_serializer(mode="wrap")
    def _with_code(self, handler):
        data = handler(self)
        return {"code": type(self).code, **data}


class NetworkExistsWarning(_CodedWarning):
    """Response-only warning: a network already exists at the output path.

    Never persisted — the job short-circuits before writing anything, and the
    pre-existing network.json is left byte-for-byte untouched.
    """

    code: ClassVar[str] = "network_exists"

    def __init__(self, network_dir: str, identity_hash: str):
        message = (
            f"Network already exists at {network_dir} "
            f"(identity_hash {identity_hash}); returning without rebuilding. "
            "Delete the output directory to force a rebuild."
        )
        BaseModel.__init__(self, message=message)


class AmbiguousReachClassificationWarning(_CodedWarning):
    """Reaches flagged by both coastal and lake logic.

    Data-quality signal only: each such reach is removed once and counted once,
    against coastal (see modify_network_specs.md Metrics/Accounting).
    """

    code: ClassVar[str] = "ambiguous_reach_classification"

    # Excluded from serialization: persisted warnings are {code, message} only.
    n_reaches: int = Field(exclude=True)
    sample_reach_ids: list[int] = Field(exclude=True)

    def __init__(self, reach_ids: list[int], sample_size: int = 10):
        sample = sorted(reach_ids)[:sample_size]
        message = (
            f"{len(reach_ids)} reach(es) flagged by both coastal and lake "
            f"logic; counted against coastal per the accounting rule. "
            f"Sample reach_ids: {sample}"
        )
        BaseModel.__init__(
            self,
            message=message,
            n_reaches=len(reach_ids),
            sample_reach_ids=sample,
        )


### HELPER JOB MODELS ###


class Identity(BaseModel):
    """The inputs that define Network identity; hashed to identity_hash.

    Dataset members follow the _path -> _hash naming rule against inputs.
    Threshold members hold the resolved value after defaults are applied.
    Nullable members are required-but-nullable: 'no lakes' / 'no coastal' /
    'unfiltered' must be stated explicitly, never expressed by omission,
    which would otherwise hash differently.
    """

    model_config = ConfigDict(extra="forbid")

    sdr_commit: str = Field(description="Methodology version pin (output-determining).")
    reach_network_hash: Hash8 = Field(
        description="Hash of the raw hydrofabric network source + version."
    )
    lakes_layer_hash: Hash8 | None = Field(
        description="Hash of the lakes/waterbody source + version. Null when no "
        "lakes dataset was supplied and lake processing was skipped."
    )
    coastal_influence_layer_hash: Hash8 | None = Field(
        description="Hash of the coastal/tidal-influence vector source + version. "
        "Null when no coastal dataset was supplied and coastal processing was "
        "skipped."
    )
    drainage_area_threshold_percent: float = Field(
        gt=0,
        description="Max drainage-area difference (%) between reaches eligible "
        "for merge (DR-024).",
    )
    stream_order_filter_threshold: Annotated[int, Field(ge=1)] | None = Field(
        description="Minimum Strahler stream order kept in the network at all. "
        "Null when no threshold was given and the filter did not run."
    )
    max_length_threshold_km: float = Field(
        gt=0,
        description="Max combined length (km) of a merged reach chain (DR-024).",
    )
    lake_area_threshold_sqkm: float = Field(
        ge=0,
        description="Minimum lake area (km2) considered at all; smaller "
        "waterbodies are dropped before reach classification.",
    )
    negative_lake_buffer_meters: float = Field(
        ge=0,
        description="Inward buffer (m) approximating the dead-pool extent "
        "(DR-034 ALT-A).",
    )


class Properties(BaseModel):
    """Reach counters; informational, not part of identity.

    Null and 0 are distinct: 0 means the branch ran and matched nothing, null
    means the branch did not run. The removal counters are disjoint and must
    reconcile (enforced by a validator here — JSON Schema cannot express it):

        n_reaches_output = n_reaches_input
                         - n_reaches_below_stream_order_removed
                         - n_reaches_encompassed_removed_lake
                         - n_reaches_encompassed_removed_coastal
                         - n_reaches_dropped_coastal_cascade
                         - n_reaches_merged
                         + n_reaches_split_passthrough_lake

    Null terms drop out of the identity. Trim/stranded counters are absent by
    design (those reaches keep their rows).
    """

    model_config = ConfigDict(extra="forbid")

    n_reaches_input: Count | None = Field(
        description="Rows in reach_network_path as read, before the "
        "stream-order filter and any other processing."
    )
    n_reaches_below_stream_order_removed: Count | None = Field(
        description="Dropped by the stream_order_filter_threshold filter. Null "
        "when no threshold was given and the filter did not run."
    )
    n_reaches_encompassed_removed_lake: Count | None = Field(
        description="Fully inside a lake polygon — classified as encompassed "
        "and dropped. Intermediate classification; leaves no column."
    )
    n_reaches_encompassed_removed_coastal: Count | None = Field(
        description="Fully inside coastal coverage — classified as encompassed "
        "and dropped. Intermediate classification; leaves no column."
    )
    n_reaches_trimmed_inlet_lake: Count | None = Field(
        description="Downstream end inside a lake — trimmed to upstream "
        "portion, is_terminal set. Row kept; not in the reconciliation."
    )
    n_reaches_trimmed_outlet_lake: Count | None = Field(
        description="Upstream end inside a lake — trimmed to downstream "
        "portion, is_headwater set. Row kept; not in the reconciliation."
    )
    n_reaches_trimmed_inlet_coastal: Count | None = Field(
        description="Downstream end inside coastal coverage, upstream not — "
        "trimmed, terminal_reason='coast'. Row kept; not in the reconciliation."
    )
    n_reaches_dropped_coastal_cascade: Count | None = Field(
        description="Removed for being downstream of a coastal "
        "encompassed/trimmed reach, not for their own classification."
    )
    n_reaches_stranded_coastal: Count | None = Field(
        description="Tributaries left pointing at a cascade-deleted reach — "
        "made terminal (terminal_reason='coast'), geometry untouched. Row "
        "kept; not in the reconciliation."
    )
    n_reaches_split_passthrough_lake: Count | None = Field(
        description="Passed through a lake with both ends outside it — split "
        "into an inlet/outlet pair, minting a new reach_id (adds one row)."
    )
    n_reaches_merged: Count | None = Field(
        description="Consumed by a drainage-area-difference merge into a "
        "downstream neighbor."
    )
    n_reaches_output: Count | None = Field(
        description="Rows in the written network.gpkg, after every step "
        "including merge."
    )
    n_headwater_reaches: Count | None = Field(
        description="Rows in the written network.gpkg with is_headwater true — "
        "a state of the final artifact, not a count of any one step."
    )
    n_terminal_reaches: Count | None = Field(
        description="Rows in the written network.gpkg with is_terminal true — "
        "a state of the final artifact, not a count of any one step."
    )

    _LAKE_COUNTERS: ClassVar[tuple[str, ...]] = (
        "n_reaches_encompassed_removed_lake",
        "n_reaches_trimmed_inlet_lake",
        "n_reaches_trimmed_outlet_lake",
        "n_reaches_split_passthrough_lake",
    )
    _COASTAL_COUNTERS: ClassVar[tuple[str, ...]] = (
        "n_reaches_encompassed_removed_coastal",
        "n_reaches_trimmed_inlet_coastal",
        "n_reaches_dropped_coastal_cascade",
        "n_reaches_stranded_coastal",
    )

    @model_validator(mode="after")
    def _skipped_steps_are_all_or_nothing(self) -> "Properties":
        """A skipped step nulls every one of its counters, a run step none."""
        for step, names in (
            ("lake", self._LAKE_COUNTERS),
            ("coastal", self._COASTAL_COUNTERS),
        ):
            nulls = [getattr(self, name) is None for name in names]
            if any(nulls) and not all(nulls):
                raise ValueError(
                    f"{step} counters must be all-null (step skipped) or "
                    f"all-set (step ran); got a mixture"
                )
        return self

    @model_validator(mode="after")
    def _counters_reconcile(self) -> "Properties":
        """Enforce the reconciliation identity; null terms drop out."""
        if self.n_reaches_input is None or self.n_reaches_output is None:
            return self
        removed = (
            self.n_reaches_below_stream_order_removed,
            self.n_reaches_encompassed_removed_lake,
            self.n_reaches_encompassed_removed_coastal,
            self.n_reaches_dropped_coastal_cascade,
            self.n_reaches_merged,
        )
        added = (self.n_reaches_split_passthrough_lake,)
        expected = (
            self.n_reaches_input
            - sum(v for v in removed if v is not None)
            + sum(v for v in added if v is not None)
        )
        if self.n_reaches_output != expected:
            raise ValueError(
                f"counters do not reconcile: expected n_reaches_output="
                f"{expected}, got {self.n_reaches_output}"
            )
        return self


class Assets(BaseModel):
    """This job's own outputs, written as separate GeoPackage files.

    network.gpkg is always present; lakes.gpkg only when lake processing ran.
    An absent lakes asset is omitted from serialization entirely (the schema
    forbids a null asset entry).
    """

    model_config = ConfigDict(extra="forbid")

    network: Asset = Field(
        description="The modified reach network (flowpaths layer) — what "
        "build_model's db_uri points at downstream."
    )
    lakes: Asset | None = Field(
        default=None,
        description="Filtered + buffered lake polygons actually used for "
        "classification (QC/reference only, DR-037 ALT-B). Present only when "
        "lake processing ran.",
    )

    @model_serializer(mode="wrap")
    def _omit_absent(self, handler):
        data = handler(self)
        return {k: v for k, v in data.items() if v is not None}


### CORE JOB MODELS ###


class ModifyNetworkInputs(BaseModel):
    """Inputs for the modify_network workflow.

    Defaults resolve at validation, so a manifest records resolved values for
    the four defaulted thresholds. stream_order_filter_threshold deliberately
    has no default: None means no stream-order filtering at all.
    """

    model_config = ConfigDict(extra="forbid")

    # Required
    reach_network_path: str = Field(
        description="Raw hydrofabric reach network. Must be a GPKG file; "
        "column/layer names follow the NHF v1.2.3 flowpaths layer schema."
    )
    base_output_path: str = Field(
        description="Output location for the modified network and manifest; "
        "artifacts are written under <base_output_path>/<identity_hash>/."
    )

    # Optional
    lakes_layer_path: str | None = Field(
        default=None,
        description="Lakes/waterbody dataset (NHF v1.2.3 lakes_polygons layer "
        "schema). Must be a GPKG file. Omit to skip lake processing entirely.",
    )
    coastal_influence_layer_path: str | None = Field(
        default=None,
        description="Coastal/tidal-influence surface boundary vector dataset. "
        "Must be a GPKG file; default layer name is coastal_influence. Omit to "
        "skip coastal processing entirely.",
    )
    drainage_area_threshold_percent: float = Field(
        default=DEFAULT_DRAINAGE_AREA_THRESHOLD_PERCENT,
        gt=0,
        description="Max drainage-area difference (%) between reaches eligible "
        "for merge (DR-024).",
    )
    stream_order_filter_threshold: Annotated[int, Field(ge=1)] | None = Field(
        default=None,
        description="Minimum Strahler stream order kept in the network at all. "
        "Omit to skip stream-order filtering entirely — every reach enters "
        "processing.",
    )
    max_length_threshold_km: float = Field(
        default=DEFAULT_MAX_LENGTH_THRESHOLD_KM,
        gt=0,
        description="Max combined length (km) of a merged reach chain (DR-024).",
    )
    lake_area_threshold_sqkm: float = Field(
        default=DEFAULT_LAKE_AREA_THRESHOLD_SQKM,
        ge=0,
        description="Minimum lake area (km2) considered at all; smaller "
        "waterbodies are dropped before any reach classification.",
    )
    negative_lake_buffer_meters: float = Field(
        default=DEFAULT_NEGATIVE_LAKE_BUFFER_METERS,
        ge=0,
        description="Inward buffer (m) applied to raw waterbody polygons to "
        "approximate the dead-pool extent (DR-034 ALT-A).",
    )


class ModifyNetworkResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identity_hash: str
    network_dir: str
    warnings: list[SerializeAsAny[JobWarning]]


class NetworkManifest(BaseModel):
    """modify_network output record: network definition + artifact inventory.

    Identity-only variant of the manifest pattern — the job operates on the
    whole network in one shot, with no realization axis, so id ==
    identity_hash (enforced below).
    """

    model_config = ConfigDict(extra="forbid")

    type: Literal["network"] = Field(
        default="network", description="Discriminator vs. model/run records."
    )
    hash_algo: Literal["sha256"] = Field(
        default="sha256",
        description="Hash function for every hash/checksum in this document.",
    )
    twod_fim_version: str = Field(
        default=twod_fim_jobs.__version__,
        description="Producer software version (provenance).",
    )
    created_at: datetime = Field(
        description="Write completion time (UTC). network.json is written "
        "last, after network.gpkg/lakes.gpkg."
    )
    identity_hash: Hash8 = Field(description="Hash of the identity object.")
    id: Hash8 = Field(
        description="Equal to identity_hash — no realization code to append."
    )
    identity: Identity
    inputs: ModifyNetworkInputs = Field(
        description="The modify_network call arguments after default "
        "resolution. This is Network's provenance — not part of identity_hash."
    )
    properties: Properties
    assets: Assets
    warnings: list[SerializeAsAny[JobWarning]] = Field(
        default=[],
        description="Only ambiguous_reach_classification is ever persisted "
        "here; network_exists is response-only and never lands in a manifest.",
    )

    @model_validator(mode="after")
    def _id_equals_identity_hash(self) -> "NetworkManifest":
        if self.id != self.identity_hash:
            raise ValueError(
                f"id must equal identity_hash (identity-only manifest, no "
                f"realization axis): id={self.id!r}, "
                f"identity_hash={self.identity_hash!r}"
            )
        return self

    @model_validator(mode="after")
    def _skip_coherence(self) -> "NetworkManifest":
        """A skipped step must be skipped consistently across all blocks."""
        lakes_given = self.inputs.lakes_layer_path is not None
        coastal_given = self.inputs.coastal_influence_layer_path is not None
        checks = (
            ("identity.lakes_layer_hash", self.identity.lakes_layer_hash, lakes_given),
            ("assets.lakes", self.assets.lakes, lakes_given),
            (
                "lake counters",
                self.properties.n_reaches_encompassed_removed_lake,
                lakes_given,
            ),
            (
                "identity.coastal_influence_layer_hash",
                self.identity.coastal_influence_layer_hash,
                coastal_given,
            ),
            (
                "coastal counters",
                self.properties.n_reaches_encompassed_removed_coastal,
                coastal_given,
            ),
        )
        for name, value, given in checks:
            if (value is not None) != given:
                state = "supplied" if given else "omitted"
                raise ValueError(
                    f"{name} must be {'set' if given else 'null'} when the "
                    f"corresponding input path is {state}"
                )
        return self
