class ReachDatasetUnavailable(Exception):
    """Raised when a reach database cannot be connected to."""


class ReachNotFoundError(Exception):
    """Raised when a reach ID is not available in a provided database."""


class InvalidAttributeError(Exception):
    """Raised when a required reach attribute is missing or invalid."""


class DuplicateReachError(Exception):
    """Raised when more than one reach is returned by querying the reach database."""


class DatasetUnavailableError(Exception):
    """Raised when a required source dataset cannot be opened or reached."""


class RasterProcessingError(Exception):
    """Raised when raster processing (clip, reproject, resample) fails."""


class WriteFailureError(Exception):
    """Raised when output artifacts cannot be written to storage."""


class InvalidWKTGeometryError(Exception):
    """Raised when one or more WKT geometries are malformed or unparsable."""
