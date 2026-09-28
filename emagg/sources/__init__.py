"""Source adapters. Importing this package registers every built-in adapter type."""

from emagg.sources import (  # noqa: F401  (imported for registration side effects)
    coops,
    county_outages,
    dstring_events,
    faa_nas,
    fcc_dirs,
    fema_declarations,
    ibi511,
    iem_lsr,
    ioda,
    ipaws,
    kubra,
    mapped,
    nhc_gis,
    nhc_storms,
    nifc_wildfires,
    nisc_hosted,
    nws_alerts,
    nwps_gauges,
    osi_pop,
    outage_summary,
    outageentry,
    pacificorp,
    sienatech,
    teco,
    usgs_earthquakes,
    waze,
    wec_outages,
    wzdx,
)
from emagg.sources.base import REGISTRY, Source, SourceContext, SourceError

__all__ = ["REGISTRY", "Source", "SourceContext", "SourceError"]
