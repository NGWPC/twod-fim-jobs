import os
from enum import Enum

### GENERAL ###
SDR_COMMIT = "826a602ddcaf58bf4081dc04b65ba15b82cc8c8a"
HASH_ALGORITHM = "sha256"
PRINT_SEPEX_STYLE_RESULTS: bool = os.environ.get(
    "PRINT_SEPEX_STYLE_RESULTS", "True"
).lower() in ("true", "1", "yes")


class SupportedSolver(str, Enum):
    SFINCS = "sfincs"
    LISFLOOD = "lisflood"


### BANKFULL REGRESSION ###


def bieger_bankfull_width(da_sqkm: float) -> float:
    """Estimate bankfull width (m) from drainage area (km2) using Bieger et al. (2015) regression for US."""
    return 2.7 * (da_sqkm**0.352)


### RUN_SCENARIOS ###

# Scenario directory name formatting
RUN_NAME_SLOPE_ROUNDING_PRECISION: int = 1
RUN_NAME_KWSE_ROUNDING_PRECISION: int = 1
RUN_NAME_Q_ROUNDING_PRECISION: int = 0

DEFAULT_SIM_SAVE_INTERVAL_SECONDS: float = 3600.0
DEFAULT_MASS_INTERVAL_SECONDS: float = 60.0
DEFAULT_INITIAL_TSTEP_SECONDS: float = 0.5
DEFAULT_SIM_TIME_SECONDS: float = 86400

### BUILD_MODEL ###

# Inputs
DEFAULT_DEM_SOURCE: str = os.environ.get(
    "DEFAULT_DEM_SOURCE",
    "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/USGS_Seamless_DEM_13.vrt",
)
DEFAULT_LULC_SOURCE: str = os.environ.get(
    "DEFAULT_LULC_SOURCE",
    "/vsis3/usgs-landcover/annual-nlcd/c1/v0/cu/mosaic/Annual_NLCD_LndCov_2023_CU_C1V0.tif",
)
DEFAULT_DOMAIN_BUFFER: float = 0.0
DEFAULT_GRID_RESOLUTION: int = 10
DEFAULT_WALK_US_DIST_PCT: float = 0.1
DEFAULT_EPSG_CODE: int = int(os.environ.get("DEFAULT_EPSG_CODE", 5070))
DEFAULT_BANKFULL_WIDTH_MULTIPLIER: float = 1.0

# Settings
REACH_TABLE: str = os.environ.get("REACH_TABLE", "reach_network")
REACH_ID_FIELD: str = os.environ.get("REACH_ID_FIELD", "reach_id")
REACH_TO_ID_FIELD: str = os.environ.get("REACH_TO_ID_FIELD", "reach_to_id")
DA_FIELD: str = os.environ.get("DA_FIELD", "total_da_sqkm")
STREAM_ORDER_FIELD: str = os.environ.get("STREAM_ORDER_FIELD", "stream_order")
SLOPE_FIELD: str = os.environ.get("SLOPE_FIELD", "slope")
REACH_FIELDS = [
    REACH_ID_FIELD,
    REACH_TO_ID_FIELD,
    DA_FIELD,
    STREAM_ORDER_FIELD,
    SLOPE_FIELD,
    "geom",
]
DEFAULT_LULC_LOOKUP = {
    11: 0.04,
    21: 0.04,
    22: 0.1,
    23: 0.08,
    24: 0.15,
    31: 0.025,
    41: 0.16,
    42: 0.16,
    43: 0.16,
    52: 0.1,
    71: 0.035,
    81: 0.03,
    82: 0.035,
    90: 0.12,
    95: 0.07,
}
USE_CUDA: bool = bool(os.environ.get("USE_CUDA", True))
STABILITY_WAIT: float = float(os.environ.get("STABILITY_WAIT", 0.1))
ADAPTIVE_STEP_ALGORITHM_SHRINK_FACTOR: float = float(
    os.environ.get("ADAPTIVE_STEP_ALGORITHM_SHRINK_FACTOR", 0.5)
)
ADAPTIVE_STEP_ALGORITHM_GROW_FACTOR: float = float(
    os.environ.get("ADAPTIVE_STEP_ALGORITHM_GROW_FACTOR", 1.5)
)
ADAPTIVE_STEP_ALGORITHM_MAX_STAGE_MIN_ACCEPTABLE: float = float(
    os.environ.get("ADAPTIVE_STEP_ALGORITHM_MAX_STAGE_MIN_ACCEPTABLE", 0.75)
)
ADAPTIVE_STEP_ALGORITHM_MAX_STAGE_MAX_ACCEPTABLE: float = float(
    os.environ.get("ADAPTIVE_STEP_ALGORITHM_MAX_STAGE_MAX_ACCEPTABLE", 1.25)
)
ADAPTIVE_STEP_ALGORITHM_MEDIAN_STAGE_MIN_ACCEPTABLE: float = float(
    os.environ.get("ADAPTIVE_STEP_ALGORITHM_MEDIAN_STAGE_MIN_ACCEPTABLE", 0.25)
)
ADAPTIVE_STEP_ALGORITHM_MEDIAN_STAGE_MAX_ACCEPTABLE: float = float(
    os.environ.get("ADAPTIVE_STEP_ALGORITHM_MEDIAN_STAGE_MAX_ACCEPTABLE", 0.75)
)
ADAPTIVE_STEP_ALGORITHM_EXTENT_MIN_ACCEPTABLE: float = float(
    os.environ.get("ADAPTIVE_STEP_ALGORITHM_EXTENT_MIN_ACCEPTABLE", 0.075)
)
ADAPTIVE_STEP_ALGORITHM_EXTENT_MAX_ACCEPTABLE: float = float(
    os.environ.get("ADAPTIVE_STEP_ALGORITHM_EXTENT_MAX_ACCEPTABLE", 0.125)
)

# Artifact names (where they will be written)
DEM_FILENAME = "dem.tif"
ROUGHNESS_FILENAME = "roughness.tif"
REACH_FILENAME = "reach.geojson"
INFLOW_FILENAME = "inflow.geojson"
ANCHOR_FILENAME = "anchor.geojson"
DOMAIN_FILENAME = "domain.geojson"
MANIFEST_FILENAME = "model_manifest.json"
STL_FILENAME = "stl.geojson"
DEPTH_FILENAME = "depth.tif"
INUNDATED_AREA_FILENAME = "inundated_area.geojson"
SCENARIO_MANIFEST_FILENAME = "scenario_manifest.json"
DEPTH_ZARR_FILENAME = "depths.zarr"

# Warning thresholds
LARGE_DOMAIN_AREA_THRESHOLD: float = 1e9  # TODO: tune (sq CRS units)
SIMILAR_ROUGHNESS_STD_THRESHOLD: float = 0.005  # TODO: tune (Manning's n)


### MODIFY_NETWORK ###

# Inputs. stream_order_filter_threshold deliberately has no default:
# omitted means no stream-order filtering at all (see modify_network_specs.md).
DEFAULT_DRAINAGE_AREA_THRESHOLD_PERCENT: float = 5.0  # DR-024
DEFAULT_MIN_LENGTH_THRESHOLD_KM: float = 5.0  # DR-024, revised from 3
DEFAULT_LAKE_AREA_THRESHOLD_SQKM: float = 5.0
DEFAULT_NEGATIVE_LAKE_BUFFER_METERS: float = 50.0  # DR-034 ALT-A "shrink"

# Artifact names (written under base_output_path/<identity_hash>/)
NETWORK_FILENAME = "network.gpkg"
LAKES_FILENAME = "lakes.gpkg"
NETWORK_MANIFEST_FILENAME = "network.json"


# NHF v1.2.3 input layer/field names. STREAM_ORDER_FIELD (above) is shared:
# modify_network's output network is build_model's input reach db.
FLOWPATHS_LAYER = "flowpaths"  # NHF input layer; the OUTPUT layer is REACH_TABLE
LAKES_LAYER = "lakes_polygons"
COASTAL_LAYER = "coastal_influence"
FP_ID_FIELD = "fp_id"
FP_TO_ID_FIELD = "fp_to_id"
AREA_SQKM_FIELD = "area_sqkm"
LENGTH_KM_FIELD = "length_km"
# Identity columns on the lakes and coastal layers, carried onto the reaches
# they touch. A layer without its column yields a null reference, never a
# fabricated one.
LAKE_ID_FIELD = "lake_id"
COAST_ID_FIELD = "id"

# Output tag columns - the literal GPKG column names in network.gpkg
# (contract: specs-and-manifests/network.schema.json assets.network).
# REACH_ID_FIELD / REACH_TO_ID_FIELD (above) name the working topology columns.
IS_HEADWATER_FIELD = "is_headwater"
IS_TERMINAL_FIELD = "is_terminal"
TERMINAL_REASON_FIELD = "terminal_reason"
LAKE_INLET_FIELD = "lake_inlet"
LAKE_OUTLET_FIELD = "lake_outlet"
IS_TRIMMED_FIELD = "is_trimmed"
LAKE_TO_ID_FIELD = "lake_to_id"
COAST_TO_ID_FIELD = "coast_to_id"

# terminal_reason vocabulary (null when is_terminal is false)
TERMINAL_REASON_OUTLET = "outlet"
TERMINAL_REASON_COAST = "coast"
TERMINAL_REASON_LAKE = "lake"


# The output schema of network.gpkg. Every source column not listed here is
# dropped on write: fp_id/fp_to_id are superseded by reach_id/reach_to_id, and
# unlisted NHF attributes are not part of the contract. stream_order,
# total_da_sqkm and length_km are kept because build_model reads them off this
# network. area_sqkm (the LOCAL catchment) is deliberately not carried: nothing
# downstream reads it, and it would be wrong on a merged row unless summed.
OUTPUT_COLUMNS = [
    REACH_ID_FIELD,
    REACH_TO_ID_FIELD,
    LAKE_TO_ID_FIELD,
    COAST_TO_ID_FIELD,
    IS_HEADWATER_FIELD,
    IS_TERMINAL_FIELD,
    TERMINAL_REASON_FIELD,
    LAKE_INLET_FIELD,
    LAKE_OUTLET_FIELD,
    IS_TRIMMED_FIELD,
    STREAM_ORDER_FIELD,
    DA_FIELD,
    LENGTH_KM_FIELD,
]
