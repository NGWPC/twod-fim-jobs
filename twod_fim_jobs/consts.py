import os
from enum import Enum

# -------------------- GENERAL --------------------
SDR_COMMIT = "826a602ddcaf58bf4081dc04b65ba15b82cc8c8a"
HASH_ALGORITHM = "sha256"
PRINT_SEPEX_STYLE_RESULTS: bool = os.environ.get(
    "PRINT_SEPEX_STYLE_RESULTS", "True"
).lower() in ("true", "1", "yes")


# -------------------- SOLVERS --------------------
USE_CUDA: bool = bool(os.environ.get("USE_CUDA", True))
STABILITY_WAIT: float = float(os.environ.get("STABILITY_WAIT", 0.1))


class SupportedSolver(str, Enum):
    SFINCS = "sfincs"
    LISFLOOD = "lisflood"


SCENARIO_SOLVER: SupportedSolver = SupportedSolver(
    os.environ.get("SCENARIO_SOLVER", SupportedSolver.LISFLOOD)
)


# -------------------- ASSET CACHE --------------------
ASSET_CACHE_DIR = os.environ.get("ASSET_CACHE_DIR", "/.cache")
MAX_ASSET_CACHE_SIZE_GB = os.environ.get("MAX_ASSET_CACHE_SIZE_GB", 4)


# -------------------- BANKFULL REGRESSION --------------------
def bieger_bankfull_width(da_sqkm: float) -> float:
    """Estimate bankfull width (m) from drainage area (km2) using Bieger et al. (2015) regression for US."""
    return 2.7 * (da_sqkm**0.352)


# -------------------- BUILD_MODEL --------------------

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

# Artifact names (where they will be written)
DEM_FILENAME = "dem.tif"
ROUGHNESS_FILENAME = "roughness.tif"
REACH_FILENAME = "reach.geojson"
INFLOW_FILENAME = "inflow.geojson"
ANCHOR_FILENAME = "anchor.geojson"
DOMAIN_FILENAME = "domain.geojson"
MANIFEST_FILENAME = "model_manifest.json"

# Warning thresholds
LARGE_DOMAIN_AREA_THRESHOLD: float = 1e9  # TODO: tune (sq CRS units)
SIMILAR_ROUGHNESS_STD_THRESHOLD: float = 0.005  # TODO: tune (Manning's n)


# -------------------- RUN_SCENARIOS --------------------

# Scenario directory name formatting
RUN_NAME_SLOPE_ROUNDING_PRECISION: int = 1
RUN_NAME_KWSE_ROUNDING_PRECISION: int = 1
RUN_NAME_Q_ROUNDING_PRECISION: int = 0

# Solver config
DEFAULT_VOLUME_CONVERGENCE_THRESHOLD: float = 1e-3
DEFAULT_SIM_SAVE_INTERVAL_SECONDS: float = 3600.0
DEFAULT_MASS_INTERVAL_SECONDS: float = 60.0
DEFAULT_INITIAL_TSTEP_SECONDS: float = 0.5
DEFAULT_SIM_TIME_SECONDS: float = 86400
DEFAULT_ELEVOFF: bool = False
DEFAULT_MAX_WALL_TIME_SECONDS = 1e10

# Slope calculation
MINIMUM_REACH_SLOPE: float = float(os.environ.get("MINIMUM_REACH_SLOPE", 1e-4))

# Artifact names (where they will be written)
STL_FILENAME = "stl.geojson"
DEPTH_FILENAME = "depth.tif"
INUNDATED_AREA_FILENAME = "inundated_area.geojson"
SCENARIO_MANIFEST_FILENAME = "scenario_manifest.json"
DEPTH_ZARR_FILENAME = "depths.zarr"

# Adaptive step algorithm
ADAPTIVE_STEP_ALGORITHM_SHRINK_FACTOR: float = 0.5
ADAPTIVE_STEP_ALGORITHM_GROW_FACTOR: float = 1.5
ADAPTIVE_STEP_ALGORITHM_MAX_STAGE_MIN_ACCEPTABLE: float = 0.75
ADAPTIVE_STEP_ALGORITHM_MAX_STAGE_MAX_ACCEPTABLE: float = 1.25
ADAPTIVE_STEP_ALGORITHM_MEDIAN_STAGE_MIN_ACCEPTABLE: float = 0.25
ADAPTIVE_STEP_ALGORITHM_MEDIAN_STAGE_MAX_ACCEPTABLE: float = 0.75
ADAPTIVE_STEP_ALGORITHM_EXTENT_MIN_ACCEPTABLE: float = 0.075
ADAPTIVE_STEP_ALGORITHM_EXTENT_MAX_ACCEPTABLE: float = 0.125
