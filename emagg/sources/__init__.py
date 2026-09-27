"""Source adapters. Importing this package registers every built-in adapter type."""

from emagg.sources import (  # noqa: F401  (imported for registration side effects)
    ibi511,
    kubra,
    mapped,
    nhc_storms,
    nifc_wildfires,
    nws_alerts,
    nwps_gauges,
    usgs_earthquakes,
    waze,
    wzdx,
)
from emagg.sources.base import REGISTRY, Source, SourceContext, SourceError

__all__ = ["REGISTRY", "Source", "SourceContext", "SourceError"]
