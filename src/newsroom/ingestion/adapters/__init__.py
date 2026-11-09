from .base import SourceAdapter, FetchResult
from .usgs import UsgsAdapter
from .nws import NwsAdapter
from .eonet import EonetAdapter
from .gdacs import GdacsAdapter
from .swpc import SwpcAdapter
from .acled import AcledAdapter
from .gdelt import GdeltAdapter
from .fred import FredAdapter
from .bls import BlsAdapter
from .eia import EiaAdapter
from .fema import FemaAdapter
from .cisa import CisaAdapter
from .treasury import TreasuryAdapter
from .nhc import NhcAdapter
from .coinbase import CoinbaseAdapter
from .news_rss import NewsRssAdapter
from .crime_rss import CrimeRssAdapter

_REGISTRY = {
    "usgs_geojson": UsgsAdapter,
    "nws_cap": NwsAdapter,
    "nasa_eonet": EonetAdapter,
    "gdacs": GdacsAdapter,
    "noaa_swpc": SwpcAdapter,
    "acled": AcledAdapter,
    "gdelt": GdeltAdapter,
    "fred": FredAdapter,
    "bls": BlsAdapter,
    "eia": EiaAdapter,
    "fema": FemaAdapter,
    "cisa_kev": CisaAdapter,
    "treasury": TreasuryAdapter,
    "noaa_nhc": NhcAdapter,
    "coinbase": CoinbaseAdapter,
    "news_rss": NewsRssAdapter,
    "crime_rss": CrimeRssAdapter,
}


def adapter_for(kind: str, name: str, config: dict) -> SourceAdapter:
    cls = _REGISTRY.get(kind)
    if not cls:
        raise ValueError(f"no adapter registered for source kind '{kind}'")
    return cls(name=name, config=config)


__all__ = ["SourceAdapter", "FetchResult", "UsgsAdapter", "adapter_for"]
