from .base import SourceAdapter, FetchResult
from .usgs import UsgsAdapter
from .nws import NwsAdapter
from .eonet import EonetAdapter

_REGISTRY = {
    "usgs_geojson": UsgsAdapter,
    "nws_cap": NwsAdapter,
    "nasa_eonet": EonetAdapter,
}


def adapter_for(kind: str, name: str, config: dict) -> SourceAdapter:
    cls = _REGISTRY.get(kind)
    if not cls:
        raise ValueError(f"no adapter registered for source kind '{kind}'")
    return cls(name=name, config=config)


__all__ = ["SourceAdapter", "FetchResult", "UsgsAdapter", "adapter_for"]
