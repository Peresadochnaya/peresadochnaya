"""Yandex address APIs. No retries, redirects, startup probes or raw error URLs."""
import os
import logging
import re

import httpx
from pydantic import ValidationError

from .schemas import GeocodeResponse, GeocodedPlace, Suggestion, SuggestResponse


# A search bias covering Saint Petersburg and Leningrad Oblast, not an
# administrative boundary. Do not exclude results using a rectangular border.
SEARCH_BBOX = "26.5,58.3~35.8,61.4"
SUGGEST_URL = "https://suggest-maps.yandex.ru/v1/suggest"
GEOCODER_URL = "https://geocode-maps.yandex.ru/v1/"


class _HideAPIKeys(logging.Filter):
    def filter(self, record):
        # HTTPX logs full query URLs at INFO, including apikey. Apply to
        # records before they reach any configured handlers or parent loggers.
        message = record.getMessage()
        record.msg = re.sub(r"([?&](?:apikey|key|api_key|token)=)[^&\s\"']+", r"\1[REDACTED]", message, flags=re.IGNORECASE)
        record.args = ()
        return True


logging.getLogger("httpx").addFilter(_HideAPIKeys())


class ProviderError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message
        super().__init__(message)


class YandexClient:
    error_prefix = "address_provider"

    def __init__(self, client: httpx.Client | None = None):
        self.client = client

    def _get(self, url: str, key_name: str, params: dict, *, timeout_seconds: float = 8) -> dict:
        key = os.getenv(key_name, "").strip()
        if not key:
            raise ProviderError(503, f"{self.error_prefix}_unavailable", "Yandex provider key is not configured")
        try:
            kwargs = {"params": {**params, "apikey": key}}
            if self.client is not None:
                response = self.client.get(url, follow_redirects=False, timeout=timeout_seconds, **kwargs)
            else:
                with httpx.Client(timeout=timeout_seconds, follow_redirects=False, trust_env=False) as client:
                    response = client.get(url, **kwargs)
            if response.status_code in (408, 504):
                raise ProviderError(504, f"{self.error_prefix}_timeout", "Yandex provider timed out")
            if response.status_code == 429:
                raise ProviderError(503, f"{self.error_prefix}_quota", "Yandex provider quota or rate limit reached")
            if response.status_code != 200:
                raise ProviderError(502, f"{self.error_prefix}_error", "Yandex provider rejected the request")
            result = response.json()
            if not isinstance(result, dict):
                raise ValueError("Expected object")
            return result
        except httpx.TimeoutException:
            raise ProviderError(504, f"{self.error_prefix}_timeout", "Yandex provider timed out") from None
        except (httpx.HTTPError, ValueError):
            raise ProviderError(502, f"{self.error_prefix}_error", "Invalid response from Yandex provider") from None


class YandexAddresses(YandexClient):
    def suggest(self, q: str, limit: int) -> SuggestResponse:
        data = self._get(SUGGEST_URL, "YANDEX_SUGGEST_API_KEY", {
            "text": q, "results": limit, "lang": "ru", "attrs": "uri",
            "highlight": 0, "bbox": SEARCH_BBOX, "countries": "ru",
            "types": "geo", "print_address": 1,
        })
        try:
            # The documented empty response may omit results entirely.
            rows = data.get("results", [])
            if not isinstance(rows, list):
                raise ValueError("Expected results list")
            items = [Suggestion(title=row["title"]["text"],
                                subtitle=(row.get("subtitle") or {}).get("text"),
                                uri=row["uri"]) for row in rows[:limit]]
            return SuggestResponse(mode="yandex", items=items)
        except (KeyError, TypeError, AttributeError, ValueError, ValidationError):
            raise ProviderError(502, "address_provider_error", "Invalid suggestions from address provider") from None

    def geocode(self, *, q: str | None = None, uri: str | None = None,
                lat: float | None = None, lon: float | None = None) -> GeocodeResponse:
        params = {"format": "json", "lang": "ru_RU", "results": 1}
        if uri is not None:
            params["uri"] = uri
        elif lat is not None and lon is not None:
            params.update(geocode=f"{lon},{lat}", sco="longlat")
        else:
            params.update(geocode=q, bbox=SEARCH_BBOX, rspn=0)
        data = self._get(GEOCODER_URL, "YANDEX_GEOCODER_API_KEY", params)
        try:
            rows = data["response"]["GeoObjectCollection"]["featureMember"]
            if not isinstance(rows, list):
                raise ValueError("Expected feature list")
            items = []
            for row in rows[:1]:
                obj = row["GeoObject"]
                lon_value, lat_value = obj["Point"]["pos"].split()
                meta = obj["metaDataProperty"]["GeocoderMetaData"]
                items.append(GeocodedPlace(lon=float(lon_value), lat=float(lat_value),
                                          label=meta["text"], precision=meta.get("precision")))
            return GeocodeResponse(mode="yandex", items=items)
        except (KeyError, TypeError, AttributeError, ValueError, ValidationError):
            raise ProviderError(502, "address_provider_error", "Invalid coordinates from address provider") from None
