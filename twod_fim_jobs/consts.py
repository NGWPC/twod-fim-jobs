### GENERAL ###
SDR_COMMIT = "826a602ddcaf58bf4081dc04b65ba15b82cc8c8a"
HASH_ALGORITHM = "sha256"

### BANKFULL REGRESSION ###


def bieger_bankfull_width(da_sqkm: float) -> float:
    """Estimate bankfull width (m) from drainage area (km2) using Bieger et al. (2015) regression for US."""
    return 2.7 * (da_sqkm**0.352)


### BUILD_MODEL ###

# Inputs
DEFAULT_DEM_SOURCE: str = "https://prd-tnm.s3.amazonaws.com/StagedProducts/Elevation/13/TIFF/USGS_Seamless_DEM_13.vrt"
DEFAULT_LULC_SOURCE: str = "/vsis3/usgs-landcover/annual-nlcd/c1/v0/cu/mosaic/Annual_NLCD_LndCov_2023_CU_C1V0.tif"
DEFAULT_DOMAIN_BUFFER: float = 0.0
DEFAULT_GRID_RESOLUTION: int = 10
DEFAULT_WALK_US_DIST_PCT: float = 0.1
DEFAULT_EPSG_CODE: int = 5070
DEFAULT_BANKFULL_WIDTH_MULTIPLIER: float = 1.0

# Settings
REACH_TABLE = "reach_network"
REACH_ID_FIELD = "reach_id"
REACH_TO_ID_FIELD = "reach_to_id"
DA_FIELD = "total_da_sqkm"
STREAM_ORDER_FIELD = "stream_order"
SLOPE_FIELD = "slope"
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
ANCHOR_FILENAME = "anchor.geojson"
DOMAIN_FILENAME = "domain.geojson"
MANIFEST_FILENAME = "model_manifest.json"

# Warning thresholds
LARGE_DOMAIN_AREA_THRESHOLD: float = 25_000_000.0  # TODO: tune (sq CRS units)
SIMILAR_ROUGHNESS_STD_THRESHOLD: float = 0.005  # TODO: tune (Manning's n)
