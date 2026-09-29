"""
The Brink World — Global Earth & Climate hazard publisher v5

Scope:
- Global hazard-event surveillance: USGS + GDACS + NASA EONET landslides.
- Official severe-weather warning overlay: WMO SWIC CAP aggregation.
- 50 km resident-population estimate: WorldPop API v2, cached per event.

Important semantics:
- 'signal_mode' distinguishes forecast/warning/monitoring/report-only records.
- WorldPop population_50km is resident population inside a radius, NOT predicted casualties/affected population.
- WMO SWIC warnings are authoritative where participating NMHS CAP feeds are available; absence of a CAP alert is not proof of no hazard.
"""

import json
import math
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv
from supabase import create_client, Client

try:
    import pycountry
except Exception:
    pycountry = None

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
WORLDPOP_API_KEY = os.getenv("WORLDPOP_API_KEY")
WMO_SWIC_JSON_URL = os.getenv("WMO_SWIC_JSON_URL", "https://severeweather.wmo.int/json/wmo_all.json")
WORLDPOP_YEAR = int(os.getenv("WORLDPOP_YEAR", str(datetime.now(timezone.utc).year)))
WORLDPOP_RESOLUTION = os.getenv("WORLDPOP_RESOLUTION", "1km")
WMO_DETAIL_LIMIT = int(os.getenv("WMO_DETAIL_LIMIT", "120"))
WMO_GEOCODE_LIMIT = int(os.getenv("WMO_GEOCODE_LIMIT", "100"))

if not SUPABASE_URL:
    raise ValueError("Missing SUPABASE_URL.")
if not SUPABASE_KEY:
    raise ValueError("Missing SUPABASE_SERVICE_ROLE_KEY.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
HEADERS = {
    "User-Agent": "TheBrinkEngine/5.1 (contact@thebrinkworld.com)",
    "Accept": "application/geo+json, application/json;q=0.9, */*;q=0.8",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

SEVERITY_ORDER = {"Monitor": 1, "Significant": 2, "Severe": 3, "Critical": 4}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean_timestamp(value: Any) -> Optional[Any]:
    """Convert blank timestamp-like values to None before sending them to Postgres."""
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    return value


def safe_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def source_level_to_band(level: str) -> str:
    x = (level or "").strip().lower()
    if x in {"red", "extreme"}: return "Critical"
    if x in {"orange", "severe"}: return "Severe"
    if x in {"yellow", "moderate"}: return "Significant"
    return "Monitor"


def earthquake_band(mag: float) -> str:
    if mag >= 7.0: return "Critical"
    if mag >= 6.0: return "Severe"
    if mag >= 5.0: return "Significant"
    return "Monitor"


def cyclone_band(wind_kts: Optional[float], alert_level: str) -> str:
    if wind_kts is not None:
        if wind_kts >= 113: return "Critical"     # Cat 4–5 equivalent
        if wind_kts >= 96: return "Severe"       # Cat 3 equivalent
        if wind_kts >= 64: return "Significant"  # Cat 1–2 equivalent
    return source_level_to_band(alert_level)


def country_from_mid(mid: str) -> Tuple[Optional[str], Optional[str]]:
    """Resolve SWIC member IDs only when they explicitly begin with ISO alpha-2.

    Numeric MID values are WMO/SWIC member identifiers, not ISO country codes,
    so they must not be interpreted as countries.
    """
    if not mid:
        return None, None
    first = str(mid).strip().split("-")[0].upper()
    if len(first) == 2 and first.isalpha() and pycountry:
        try:
            country = pycountry.countries.get(alpha_2=first)
            if country:
                return country.name, country.alpha_3
        except Exception:
            pass
    return None, None


def country_from_wmo_identifier(identifier: Any) -> Tuple[Optional[str], Optional[str]]:
    """Resolve ISO-3166 numeric country code embedded in WMO/CAP identifiers.

    Live SWIC records commonly use OID-shaped identifiers such as
    urn:oid:2.49.0.1.840.... where 840 is the ISO numeric code for the US.
    """
    if not identifier or not pycountry:
        return None, None
    text = str(identifier).strip()

    patterns = [
        r"2\.49\.0\.[01]\.(\d{3})(?:\.|-|$)",
        r"urn:oid:2\.49\.0\.[01]\.(\d{3})(?:\.|-|$)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.I)
        if not match:
            continue
        try:
            country = pycountry.countries.get(numeric=match.group(1))
            if country:
                return country.name, country.alpha_3
        except Exception:
            pass

    # Recognize well-known national authority identifiers when the country is
    # explicit in the issuer token but not encoded as an ISO number.
    lowered = text.lower()
    authority_prefixes = {
        "ausbom": ("Australia", "AUS"),
    }
    for prefix, pair in authority_prefixes.items():
        if lowered.startswith(prefix):
            return pair

    return None, None


def iso3_to_iso2(iso3: Optional[str]) -> Optional[str]:
    if not iso3 or not pycountry:
        return None
    try:
        c = pycountry.countries.get(alpha_3=str(iso3).upper())
        return c.alpha_2 if c else None
    except Exception:
        return None


def geocode_warning_area(
    area_desc: Any,
    country: Optional[str],
    iso3: Optional[str],
) -> Tuple[Optional[float], Optional[float], Optional[str], Optional[str], Optional[str]]:
    """Resolve a representative warning point and, when needed, infer country.

    This is intentionally a representative-point fallback, not a reconstruction
    of the full warning polygon.
    """
    if not area_desc:
        return None, None, None, None, "No area description"

    raw = re.sub(r"\s+", " ", str(area_desc)).strip()
    if not raw:
        return None, None, None, None, "Empty area description"

    # Build conservative candidate place names. Prefixes such as "Western Australia:"
    # and "Queensland:" are highly useful when the downstream coastal-zone text is not.
    candidates: List[str] = []
    prefix = raw.split(":", 1)[0].strip() if ":" in raw else ""
    if prefix and 2 <= len(prefix) <= 80:
        candidates.append(prefix)

    first = re.split(r";|\||\n| / ", raw, maxsplit=1)[0].strip()
    if first and first not in candidates:
        candidates.append(first)

    simplified = re.sub(
        r"\b(city jurisdiction|administrative district|autonomous county|autonomous prefecture|municipality|prefecture|county)\b",
        "",
        first,
        flags=re.I,
    ).strip(" ,-")
    if simplified and simplified not in candidates:
        candidates.append(simplified)

    # State abbreviations in US CAP area text (e.g. "Maricopa, AZ") benefit from
    # keeping the comma suffix intact, so 'first' remains ahead of simplified forms.
    params_base = {"count": 10, "language": "en", "format": "json"}
    requested_iso2 = iso3_to_iso2(iso3)
    if requested_iso2:
        params_base["countryCode"] = requested_iso2

    last_error = None
    for query in candidates[:3]:
        try:
            params = dict(params_base)
            params["name"] = query[:140]
            response = SESSION.get(
                "https://geocoding-api.open-meteo.com/v1/search",
                params=params,
                timeout=12,
            )
            response.raise_for_status()
            results = (response.json() or {}).get("results") or []
            if not results:
                continue

            chosen = None
            if requested_iso2:
                for result in results:
                    if str(result.get("country_code") or "").upper() == requested_iso2:
                        chosen = result
                        break
            else:
                # Without a known country, only accept a result if the geocoder gives
                # us an explicit country code. Prefer exact-ish name matches.
                qnorm = re.sub(r"[^a-z0-9]+", " ", query.lower()).strip()
                for result in results:
                    rname = re.sub(r"[^a-z0-9]+", " ", str(result.get("name") or "").lower()).strip()
                    if rname == qnorm and result.get("country_code"):
                        chosen = result
                        break
                if chosen is None:
                    with_country = [r for r in results if r.get("country_code")]
                    if len(with_country) == 1:
                        chosen = with_country[0]

            if chosen is None:
                continue

            lat = safe_float(chosen.get("latitude"))
            lon = safe_float(chosen.get("longitude"))
            if lat is None or lon is None:
                continue

            discovered_country, discovered_iso3 = normalize_country(chosen.get("country_code"))
            label = ", ".join(
                x for x in [
                    chosen.get("name"),
                    chosen.get("admin1"),
                    chosen.get("country"),
                ] if x
            )
            return lat, lon, discovered_country, discovered_iso3, label
        except Exception as exc:
            last_error = str(exc)

    return None, None, None, None, last_error or "No reliable geocoding match"


def feature_centroid(geometry: Optional[Dict[str, Any]]) -> Tuple[Optional[float], Optional[float]]:
    if not geometry:
        return None, None
    coords = geometry.get("coordinates")
    gtype = geometry.get("type")
    if not coords:
        return None, None
    try:
        if gtype == "Point":
            return float(coords[1]), float(coords[0])

        pts: List[Tuple[float, float]] = []
        def walk(x: Any):
            if isinstance(x, list) and len(x) >= 2 and isinstance(x[0], (int, float)) and isinstance(x[1], (int, float)):
                pts.append((float(x[1]), float(x[0])))
            elif isinstance(x, list):
                for y in x: walk(y)
        walk(coords)
        if not pts:
            return None, None
        return sum(p[0] for p in pts)/len(pts), sum(p[1] for p in pts)/len(pts)
    except Exception:
        return None, None


def gdacs_country_fields(p: Dict[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    country = p.get("country")
    iso3 = p.get("iso3")
    affected = p.get("affectedcountries") or []
    if not country and affected:
        country = ", ".join([a.get("countryname") for a in affected if a.get("countryname")]) or None
    if not iso3 and affected:
        iso3 = ",".join([a.get("iso3") for a in affected if a.get("iso3")]) or None
    return country, iso3


def base_event(**kwargs) -> Dict[str, Any]:
    out = {
        "id": kwargs.get("id"),
        "category": kwargs.get("category"),
        "name": kwargs.get("name", "Unknown event")[:180],
        "basin": kwargs.get("basin"),
        "intensity": kwargs.get("intensity"),
        "wind_kts": kwargs.get("wind_kts"),
        "pressure_mb": kwargs.get("pressure_mb"),
        "latitude": kwargs.get("latitude"),
        "longitude": kwargs.get("longitude"),
        "alert_level": kwargs.get("alert_level", "Unrated"),
        "magnitude": kwargs.get("magnitude"),
        "depth_km": kwargs.get("depth_km"),
        "severity_tier": kwargs.get("severity_tier", "Monitor"),
        "source": kwargs.get("source"),
        "observed_at": clean_timestamp(kwargs.get("observed_at")) or now_iso(),
        "updated_at": now_iso(),
        "country": kwargs.get("country"),
        "iso3": kwargs.get("iso3"),
        "signal_mode": kwargs.get("signal_mode", "reported"),
        "record_type": kwargs.get("record_type", "event"),
        "source_url": kwargs.get("source_url"),
        "expires_at": clean_timestamp(kwargs.get("expires_at")),
        "urgency": kwargs.get("urgency"),
        "certainty": kwargs.get("certainty"),
        "population_50km": kwargs.get("population_50km"),
        "population_year": kwargs.get("population_year"),
        "population_source": kwargs.get("population_source"),
    }
    return out


# -------------------- GLOBAL EVENT FEEDS --------------------

def fetch_usgs_earthquakes() -> Tuple[bool, List[Dict[str, Any]], Optional[str]]:
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=24)
    params = {
        "format": "geojson",
        "starttime": start.isoformat().replace("+00:00", "Z"),
        "endtime": end.isoformat().replace("+00:00", "Z"),
        "minmagnitude": 3.0,
        "orderby": "time",
        "limit": 20000,
    }
    try:
        r = SESSION.get("https://earthquake.usgs.gov/fdsnws/event/1/query", params=params, timeout=30)
        r.raise_for_status()
        events = []
        for f in r.json().get("features", []):
            p = f.get("properties", {})
            coords = (f.get("geometry") or {}).get("coordinates") or []
            if len(coords) < 3: continue
            mag = safe_float(p.get("mag"))
            if mag is None: continue
            events.append(base_event(
                id=f"USGS_EQ_{f.get('id')}", category="earthquake", name=p.get("place") or "Earthquake",
                basin="GLOBAL", intensity=f"Magnitude {mag:.1f}", latitude=float(coords[1]), longitude=float(coords[0]),
                magnitude=mag, depth_km=safe_float(coords[2]), alert_level=(p.get("alert") or "Unrated").capitalize(),
                severity_tier=earthquake_band(mag), source="USGS", signal_mode="reported", record_type="event",
                observed_at=datetime.fromtimestamp((p.get("time") or 0)/1000, tz=timezone.utc).isoformat(),
                source_url=p.get("url")
            ))
        return True, events, None
    except Exception as e:
        return False, [], str(e)


def fetch_gdacs_search(
    code: str,
    category: str,
    source_label: str,
    signal_mode: str,
    lookback_days: int,
) -> Tuple[bool, List[Dict[str, Any]], Optional[str]]:
    """Fetch GDACS through its supported GeoJSON SEARCH API, not the static resource folders."""
    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=lookback_days)
    params = {
        "eventlist": code,
        "fromDate": start.isoformat(),
        "toDate": end.isoformat(),
        "alertlevel": "Green;Orange;Red",
    }
    try:
        r = SESSION.get(
            "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH",
            params=params, timeout=45,
        )
        r.raise_for_status()
        payload = r.json()
        features = payload.get("features", []) if isinstance(payload, dict) else []
        events: List[Dict[str, Any]] = []

        for f in features:
            p = f.get("properties") or {}
            lat, lon = feature_centroid(f.get("geometry"))
            if lat is None or lon is None:
                continue

            level = str(p.get("alertlevel") or "Unrated").capitalize()
            severity = p.get("severitydata") or {}
            sev_text = severity.get("severitytext") or severity.get("severity")
            event_id = p.get("eventid")
            episode_id = p.get("episodeid")
            name = p.get("name") or p.get("eventname") or f"{category.title()} event"
            country = p.get("country") or None
            iso3 = p.get("iso3") or None
            source_url = None
            if event_id:
                source_url = f"https://www.gdacs.org/resources.aspx?eventid={event_id}&eventtype={code}"

            wind_kts = None
            if code == "TC":
                raw = safe_float(severity.get("severity"))
                unit = str(severity.get("severityunit") or "").strip().lower()
                if raw is not None:
                    if "km" in unit and ("/h" in unit or "h" in unit):
                        wind_kts = round(raw * 0.539957)
                    elif "kt" in unit or "knot" in unit:
                        wind_kts = round(raw)

            band = cyclone_band(wind_kts, level) if code == "TC" else source_level_to_band(level)

            events.append(base_event(
                id=f"GDACS_{code}_{event_id or (str(name).replace(' ', ''))}",
                category=category, name=str(name),
                basin="GLOBAL", intensity=str(sev_text or f"GDACS {level}"),
                wind_kts=wind_kts, pressure_mb=None,
                latitude=lat, longitude=lon, alert_level=level, severity_tier=band,
                source=source_label, signal_mode=signal_mode, record_type="event",
                country=country, iso3=iso3,
                observed_at=p.get("fromdate") or now_iso(),
                expires_at=p.get("todate"), source_url=source_url,
            ))

        return True, events, None
    except Exception as e:
        return False, [], str(e)


def fetch_gdacs_cyclones() -> Tuple[bool, List[Dict[str, Any]], Optional[str]]:
    return fetch_gdacs_search("TC", "cyclone", "GDACS / RSMC", "forecast", 21)


def fetch_gdacs_volcanoes() -> Tuple[bool, List[Dict[str, Any]], Optional[str]]:
    return fetch_gdacs_search("VO", "volcano", "GDACS / GVP", "monitoring", 90)


def fetch_gdacs_floods() -> Tuple[bool, List[Dict[str, Any]], Optional[str]]:
    return fetch_gdacs_search("FL", "flood", "GDACS", "reported", 30)


def fetch_gdacs_droughts() -> Tuple[bool, List[Dict[str, Any]], Optional[str]]:
    return fetch_gdacs_search("DR", "drought", "GDACS / GDO", "monitoring", 180)


def fetch_eonet_landslides() -> Tuple[bool, List[Dict[str, Any]], Optional[str]]:
    """NASA EONET provides curated open landslide events. Treat as notable reports, not a predictive warning feed."""
    try:
        r = SESSION.get("https://eonet.gsfc.nasa.gov/api/v3/events", params={"status":"open","days":14,"category":"landslides","limit":100}, timeout=30)
        r.raise_for_status()
        out = []
        for ev in r.json().get("events", []):
            geom = ev.get("geometry") or []
            if not geom: continue
            latest = geom[-1]
            lat, lon = feature_centroid({"type":latest.get("type","Point"), "coordinates":latest.get("coordinates")})
            if lat is None or lon is None: continue
            sources = ev.get("sources") or []
            source_url = sources[0].get("url") if sources else None
            out.append(base_event(
                id=f"EONET_LS_{ev.get('id')}", category="landslide", name=ev.get("title") or "Landslide",
                basin="GLOBAL", intensity="NASA EONET curated landslide report", latitude=lat, longitude=lon,
                alert_level="Curated", severity_tier="Monitor", source="NASA EONET", signal_mode="reported",
                record_type="event", observed_at=latest.get("date") or now_iso(), source_url=source_url
            ))
        return True, out, None
    except Exception as e:
        return False, [], str(e)


# -------------------- WMO OFFICIAL WARNING OVERLAY --------------------

WEATHER_KEYWORDS = {
    "flash_flood": ["flash flood", "flash-flood"],
    "flood": ["coastal flood", "river flood", "flooding", "flood"],
    "heavy_rain": ["heavy rain", "heavy rainfall", "torrential rain", "extreme rainfall", "precipitation"],
    "extreme_temperature": ["extreme heat", "heatwave", "heat wave", "high temperature", "extreme cold", "cold wave", "low temperature", "freeze", "frost"],
    "cyclone": ["hurricane", "typhoon", "tropical cyclone", "tropical storm"],
    "landslide": ["landslide", "mudslide", "debris flow"],
    "drought": ["drought"],
    "extreme_weather": ["severe thunderstorm", "thunderstorm", "tornado", "blizzard", "snow storm", "snowstorm", "gale", "strong wind", "severe weather", "hail"],
}


def classify_weather_alert(text: str) -> Optional[str]:
    t = (text or "").lower()
    # order matters: flash flood before generic flood
    for cat in ["flash_flood", "flood", "heavy_rain", "extreme_temperature", "cyclone", "landslide", "drought", "extreme_weather"]:
        if any(k in t for k in WEATHER_KEYWORDS[cat]):
            return cat
    return None



def normalize_country(value: Any) -> Tuple[Optional[str], Optional[str]]:
    """Normalize an explicit country name or ISO-2/ISO-3 code."""
    if value is None:
        return None, None
    raw = str(value).strip()
    if not raw:
        return None, None
    raw = raw.split(",")[0].strip()
    upper = raw.upper()
    aliases = {
        "UK": "GB", "UAE": "AE", "USA": "US",
        "UNITED STATES OF AMERICA": "US",
        "RUSSIA": "RU", "SOUTH KOREA": "KR", "NORTH KOREA": "KP",
        "VIETNAM": "VN", "LAOS": "LA", "BOLIVIA": "BO",
        "VENEZUELA": "VE", "IRAN": "IR", "SYRIA": "SY",
        "TANZANIA": "TZ", "MOLDOVA": "MD", "BRUNEI": "BN",
    }
    upper = aliases.get(upper, upper)
    if pycountry:
        try:
            if len(upper) == 2:
                country = pycountry.countries.get(alpha_2=upper)
            elif len(upper) == 3:
                country = pycountry.countries.get(alpha_3=upper)
            else:
                country = pycountry.countries.lookup(raw)
            if country:
                return country.name, country.alpha_3
        except Exception:
            pass
    return (raw if len(raw) > 3 else None), (upper if len(upper) == 3 else None)


def country_from_sender(sender: Any) -> Tuple[Optional[str], Optional[str]]:
    """Best-effort country extraction from authoritative sender domains such as *.gov.cn."""
    if not sender:
        return None, None
    text = str(sender).strip().lower()
    # First allow a senderName that explicitly contains a country name.
    if pycountry:
        for country in pycountry.countries:
            names = [country.name]
            official = getattr(country, "official_name", None)
            common = getattr(country, "common_name", None)
            if official: names.append(official)
            if common: names.append(common)
            for name in names:
                if len(name) >= 5 and re.search(r"\b" + re.escape(name.lower()) + r"\b", text):
                    return country.name, country.alpha_3
    # Then use ccTLD from an email/domain when it is an ISO country code.
    match = re.search(r"(?:@|\b)([a-z0-9.-]+\.([a-z]{2}))\b", text)
    if match:
        cc = match.group(2).upper()
        cc = {"UK": "GB"}.get(cc, cc)
        return normalize_country(cc)
    return None, None


def country_from_wmo_fields(item: Dict[str, Any], details: Optional[Dict[str, Any]] = None) -> Tuple[Optional[str], Optional[str]]:
    """Resolve country using explicit SWIC/CAP metadata before any inference."""
    details = details or {}

    # 1. Explicit country/code fields from SWIC summary.
    for key in [
        "country", "countryName", "countryname", "country_code", "countryCode",
        "iso2", "iso3", "cc", "memberCountry", "member_country"
    ]:
        country, iso3 = normalize_country(item.get(key))
        if country or iso3:
            return country, iso3

    # 2. CAP/WMO identifiers are the strongest fallback when they embed an ISO
    #    numeric country code, e.g. urn:oid:2.49.0.1.840....
    for key in ["id", "identifier", "uid"]:
        country, iso3 = country_from_wmo_identifier(item.get(key))
        if country or iso3:
            return country, iso3

    # 3. WMO member id only when it explicitly begins with ISO alpha-2.
    for key in ["mid", "memberId", "member_id"]:
        country, iso3 = country_from_mid(str(item.get(key) or ""))
        if country or iso3:
            return country, iso3

    # 4. CAP geocodes when an authority supplies ISO/country values.
    for pair in details.get("geocodes", []) or []:
        name = str(pair.get("name") or "").lower()
        value = pair.get("value")
        if any(token in name for token in ["iso", "country", "nation"]):
            country, iso3 = normalize_country(value)
            if country or iso3:
                return country, iso3

    # 5. National authority tokens may be present directly in the alert identifier.
    for key in ["id", "identifier", "uid"]:
        country, iso3 = country_from_wmo_identifier(item.get(key))
        if country or iso3:
            return country, iso3

    # 6. Authoritative sender / senderName.
    sender_candidates = [
        details.get("sender"), details.get("senderName"),
        item.get("sender"), item.get("senderName"), item.get("sender_name"),
        item.get("source"), item.get("publisher"), item.get("author"),
    ]
    for sender in sender_candidates:
        country, iso3 = country_from_sender(sender)
        if country or iso3:
            return country, iso3

    return None, None


def cap_shape_centroid(value: Any, shape: str) -> Tuple[Optional[float], Optional[float]]:
    if not value:
        return None, None
    try:
        if shape == "polygon":
            pts = []
            for token in str(value).replace(";", " ").split():
                if "," not in token:
                    continue
                a, b = token.split(",", 1)
                pts.append((float(a), float(b)))  # CAP uses lat,lon
            if pts:
                return sum(x[0] for x in pts) / len(pts), sum(x[1] for x in pts) / len(pts)
        if shape == "circle":
            center = str(value).split()[0]
            if "," in center:
                a, b = center.split(",", 1)
                return float(a), float(b)
    except Exception:
        pass
    return None, None


def wmo_item_centroid(item: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    """Use geometry already present in the SWIC summary before spending a CAP detail request."""
    for key in ["geometry", "geojson"]:
        geom = item.get(key)
        if isinstance(geom, str):
            try:
                geom = json.loads(geom)
            except Exception:
                geom = None
        if isinstance(geom, dict):
            lat, lon = feature_centroid(geom)
            if lat is not None and lon is not None:
                return lat, lon

    lat = safe_float(item.get("latitude") if item.get("latitude") is not None else item.get("lat"))
    lon = safe_float(item.get("longitude") if item.get("longitude") is not None else item.get("lon"))
    if lat is not None and lon is not None:
        return lat, lon

    for key in ["polygon", "areaPolygon"]:
        lat, lon = cap_shape_centroid(item.get(key), "polygon")
        if lat is not None and lon is not None:
            return lat, lon
    for key in ["circle", "areaCircle"]:
        lat, lon = cap_shape_centroid(item.get(key), "circle")
        if lat is not None and lon is not None:
            return lat, lon

    return None, None


def get_wmo_enrichment_cache() -> Dict[str, Dict[str, Any]]:
    """Reuse country/geometry already resolved in earlier cycles so detail requests progress through the feed."""
    cache: Dict[str, Dict[str, Any]] = {}
    try:
        res = (
            supabase.table("live_hazards")
            .select("id,country,iso3,latitude,longitude,urgency,certainty,expires_at")
            .eq("source", "WMO SWIC / National Authority")
            .execute()
        )
        for row in (res.data or []):
            if row.get("id"):
                cache[row["id"]] = row
    except Exception as exc:
        print(f"[WARN] WMO enrichment cache unavailable: {exc}")
    return cache


def cap_xml_summary(url: str) -> Dict[str, Any]:
    """Best-effort CAP parser with authoritative sender, geocodes and area centroid."""
    try:
        full_url = url if url.startswith("http") else "https://severeweather.wmo.int" + (url if url.startswith("/") else "/" + url)
        r = SESSION.get(full_url, timeout=12)
        r.raise_for_status()
        root = ET.fromstring(r.content)

        vals: Dict[str, List[str]] = {}
        for el in root.iter():
            tag = el.tag.split("}")[-1]
            if el.text and el.text.strip():
                vals.setdefault(tag, []).append(el.text.strip())

        geocodes: List[Dict[str, str]] = []
        for geocode in root.iter():
            if geocode.tag.split("}")[-1] != "geocode":
                continue
            value_name = None
            value = None
            for child in list(geocode):
                tag = child.tag.split("}")[-1]
                txt = (child.text or "").strip()
                if tag == "valueName":
                    value_name = txt
                elif tag == "value":
                    value = txt
            if value_name or value:
                geocodes.append({"name": value_name or "", "value": value or ""})

        lat = lon = None
        polygons = vals.get("polygon", [])
        circles = vals.get("circle", [])
        if polygons:
            lat, lon = cap_shape_centroid(polygons[0], "polygon")
        elif circles:
            lat, lon = cap_shape_centroid(circles[0], "circle")

        return {
            "lat": lat,
            "lon": lon,
            "severity": (vals.get("severity") or [None])[0],
            "urgency": (vals.get("urgency") or [None])[0],
            "certainty": (vals.get("certainty") or [None])[0],
            "event": (vals.get("event") or [None])[0],
            "headline": (vals.get("headline") or [None])[0],
            "areaDesc": (vals.get("areaDesc") or [None])[0],
            "sender": (vals.get("sender") or [None])[0],
            "senderName": (vals.get("senderName") or [None])[0],
            "effective": (vals.get("effective") or [None])[0],
            "expires": (vals.get("expires") or [None])[0],
            "geocodes": geocodes,
        }
    except Exception:
        return {}


def fetch_wmo_cap_warnings() -> Tuple[bool, List[Dict[str, Any]], Optional[str]]:
    try:
        r = SESSION.get(WMO_SWIC_JSON_URL, timeout=30)
        r.raise_for_status()
        payload = r.json()
        items = payload.get("items") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            items = payload.get("data", []) if isinstance(payload, dict) else []

        rows: List[Dict[str, Any]] = []
        detail_budget = WMO_DETAIL_LIMIT
        geocode_budget = WMO_GEOCODE_LIMIT
        enrichment_cache = get_wmo_enrichment_cache()
        detail_fetches = 0
        geocode_fetches = 0
        resolved_country = 0
        resolved_geometry = 0
        unresolved_samples: List[str] = []

        for item in items:
            if not isinstance(item, dict):
                continue

            text = " ".join(str(item.get(k) or "") for k in ["event", "headline", "areaDesc"])
            cat = classify_weather_alert(text)
            if not cat:
                continue

            severity = str(item.get("severity") or item.get("s") or "Unknown")
            urgency = str(item.get("urgency") or item.get("u") or "Unknown")
            certainty = str(item.get("certainty") or item.get("c") or "Unknown")
            mid = str(item.get("mid") or item.get("memberId") or item.get("member_id") or "")

            effective = clean_timestamp(item.get("effective") or item.get("sent")) or now_iso()
            record_id = f"WMO_CAP_{item.get('id') or (mid + '_' + str(effective))}"
            source_url = item.get("url") or item.get("cap") or item.get("capUrl") or item.get("link")

            country, iso3 = country_from_wmo_fields(item)
            lat, lon = wmo_item_centroid(item)
            expires = clean_timestamp(item.get("expires"))

            # Reuse previously resolved values so each 15-minute cycle can spend its
            # CAP-detail budget on warnings that are still missing metadata.
            cached = enrichment_cache.get(record_id) or {}
            country = country or cached.get("country")
            iso3 = iso3 or cached.get("iso3")
            lat = lat if lat is not None else cached.get("latitude")
            lon = lon if lon is not None else cached.get("longitude")
            if urgency in {"", "Unknown", "None"} and cached.get("urgency"):
                urgency = cached.get("urgency")
            if certainty in {"", "Unknown", "None"} and cached.get("certainty"):
                certainty = cached.get("certainty")
            expires = expires or cached.get("expires_at")

            details: Dict[str, Any] = {}
            sev_lower = severity.lower()
            priority_detail = (
                country is None or iso3 is None or lat is None or lon is None
                or sev_lower in {"extreme", "severe", "4", "3"}
                or cat in {"flash_flood", "cyclone", "landslide"}
            )

            if source_url and detail_budget > 0 and priority_detail:
                details = cap_xml_summary(str(source_url))
                detail_budget -= 1
                detail_fetches += 1

                detail_lat, detail_lon = details.get("lat"), details.get("lon")
                if detail_lat is not None and detail_lon is not None:
                    lat, lon = detail_lat, detail_lon

                detail_country, detail_iso3 = country_from_wmo_fields(item, details)
                country = country or detail_country
                iso3 = iso3 or detail_iso3

                severity = details.get("severity") or severity
                urgency = details.get("urgency") or urgency
                certainty = details.get("certainty") or certainty
                effective = clean_timestamp(details.get("effective")) or effective
                expires = clean_timestamp(details.get("expires")) or expires

                if details.get("event"):
                    text = f"{details.get('event')} {details.get('headline') or ''} {details.get('areaDesc') or ''}"
                    cat = classify_weather_alert(text) or cat

            # Retry country after CAP detail, including the OID/numeric identifier path.
            if not country or not iso3:
                detail_country, detail_iso3 = country_from_wmo_fields(item, details)
                country = country or detail_country
                iso3 = iso3 or detail_iso3

            coded = {"4":"Extreme", "3":"Severe", "2":"Moderate", "1":"Minor", "0":"Unknown"}
            severity = coded.get(str(severity), str(severity).capitalize())
            band = source_level_to_band(severity)

            # Many CAP feeds publish geocodes/area names rather than polygons.
            # For high-priority warnings only, resolve a representative point
            # so the dashboard can offer a clearly-labelled 50 km reference exposure.
            area_desc = details.get("areaDesc") or item.get("areaDesc") or item.get("area") or ""
            needs_point = lat is None or lon is None
            point_priority = (
                band in {"Critical", "Severe"}
                or cat in {"flash_flood", "cyclone", "landslide"}
                or country is None
            )
            if needs_point and point_priority and geocode_budget > 0 and area_desc:
                geo_lat, geo_lon, geo_country, geo_iso3, geo_label = geocode_warning_area(
                    area_desc, country, iso3
                )
                geocode_budget -= 1
                geocode_fetches += 1
                if geo_lat is not None and geo_lon is not None:
                    lat, lon = geo_lat, geo_lon
                if not country and geo_country:
                    country = geo_country
                if not iso3 and geo_iso3:
                    iso3 = geo_iso3

            name = (
                details.get("headline")
                or item.get("headline")
                or details.get("event")
                or item.get("event")
                or f"Official {cat.replace('_',' ')} warning"
            )
            intensity = details.get("event") or item.get("event") or cat.replace("_", " ").title()

            if country:
                resolved_country += 1
            else:
                if len(unresolved_samples) < 8:
                    unresolved_samples.append(
                        f"id={item.get('id')!r} mid={item.get('mid')!r} area={str(item.get('areaDesc') or '')[:80]!r}"
                    )
            if lat is not None and lon is not None:
                resolved_geometry += 1

            rows.append(base_event(
                id=record_id,
                category=cat,
                name=str(name),
                basin="WMO MEMBER",
                intensity=str(intensity),
                latitude=lat,
                longitude=lon,
                alert_level=severity,
                severity_tier=band,
                source="WMO SWIC / National Authority",
                signal_mode="official_warning",
                record_type="warning",
                country=country,
                iso3=iso3,
                observed_at=effective,
                source_url=(str(source_url) if source_url else None),
                expires_at=expires,
                urgency=urgency,
                certainty=certainty,
            ))

        print(
            f"[WMO] detail fetches={detail_fetches}; geocode fetches={geocode_fetches}; "
            f"country resolved={resolved_country}/{len(rows)}; "
            f"geometry resolved={resolved_geometry}/{len(rows)}"
        )
        if unresolved_samples:
            print("[WMO] unresolved country samples:")
            for sample in unresolved_samples:
                print(f"  - {sample}")
        return True, rows, None
    except Exception as e:
        return False, [], str(e)


# -------------------- WORLDPOP 50 KM EXPOSURE --------------------

def geodesic_circle_geojson(lat: float, lon: float, radius_km: float = 50.0, n: int = 72) -> Dict[str, Any]:
    R = 6371.0088
    phi1 = math.radians(lat)
    lam1 = math.radians(lon)
    d = radius_km / R
    coords = []
    for i in range(n + 1):
        brng = math.radians((360.0 * i) / n)
        phi2 = math.asin(math.sin(phi1)*math.cos(d) + math.cos(phi1)*math.sin(d)*math.cos(brng))
        lam2 = lam1 + math.atan2(math.sin(brng)*math.sin(d)*math.cos(phi1), math.cos(d)-math.sin(phi1)*math.sin(phi2))
        lon2 = (math.degrees(lam2) + 540) % 360 - 180
        coords.append([lon2, math.degrees(phi2)])
    return {"type":"Polygon", "coordinates":[coords]}


def worldpop_50km(lat: float, lon: float) -> Tuple[Optional[int], Optional[str]]:
    payload = {"geojson": geodesic_circle_geojson(lat, lon, 50.0), "year": WORLDPOP_YEAR, "resolution": WORLDPOP_RESOLUTION}
    h = dict(HEADERS)
    if WORLDPOP_API_KEY:
        h["X-API-Key"] = WORLDPOP_API_KEY
    try:
        r = requests.post("https://api.worldpop.org/v2/population", headers=h, json=payload, timeout=30)
        r.raise_for_status()
        task_id = r.json().get("task_id")
        if not task_id: return None, "WorldPop returned no task_id"
        for _ in range(25):
            t = requests.get(f"https://api.worldpop.org/v2/tasks/{task_id}", headers=h, timeout=20)
            t.raise_for_status()
            body = t.json()
            if body.get("status") == "success":
                value = ((body.get("result") or {}).get("total_population"))
                return (round(float(value)) if value is not None else None), None
            if body.get("status") == "failure":
                return None, str(body.get("error") or "WorldPop task failed")
            time.sleep(0.8)
        return None, "WorldPop task timeout"
    except Exception as e:
        return None, str(e)


def get_population_cache() -> Dict[str, Tuple[Optional[int], Optional[int], Optional[str]]]:
    cache = {}
    try:
        res = supabase.table("live_hazards").select("id,population_50km,population_year,population_source").not_.is_("population_50km", "null").execute()
        for row in (res.data or []):
            cache[row["id"]] = (row.get("population_50km"), row.get("population_year"), row.get("population_source"))
    except Exception as e:
        print(f"[WARN] Population cache unavailable: {e}")
    return cache


def add_population_estimates(events: List[Dict[str, Any]]) -> None:
    """Cache by stable event ID. Population is computed for records with coordinates; no repeated API call on updates."""
    cache = get_population_cache()
    failures = 0
    for e in events:
        if e.get("id") in cache:
            pop, yr, src = cache[e["id"]]
            e["population_50km"], e["population_year"], e["population_source"] = pop, yr, src
            continue
        lat, lon = e.get("latitude"), e.get("longitude")
        if lat is None or lon is None:
            continue
        # Every point-based event gets a 50 km population estimate once, cached by stable event ID.
        # Polygon/country warnings can generate very large volumes, so only Severe/Critical warnings are enriched.
        band = e.get("severity_tier") or "Monitor"
        if e.get("record_type") == "warning" and band not in {"Critical", "Severe"}:
            continue
        pop, err = worldpop_50km(float(lat), float(lon))
        if pop is not None:
            e["population_50km"] = pop
            e["population_year"] = WORLDPOP_YEAR
            e["population_source"] = "WorldPop Global2"
        else:
            failures += 1
            if failures <= 5:
                print(f"[WARN] WorldPop {e.get('id')}: {err}")


# -------------------- HEALTH, UPSERT, PRUNING --------------------

def update_feed_status(stream_key: str, source: str, category: str, success: bool, count: int, attempt_at: str, error: Optional[str]) -> None:
    row = {
        "stream_key": stream_key, "source": source, "category": category,
        "status": "healthy" if success else "error", "event_count": count,
        "last_attempt_at": attempt_at, "error_message": None if success else (error or "Unknown error"),
        "updated_at": now_iso(),
    }
    if success:
        row["last_success_at"] = attempt_at
    else:
        # preserve previous last_success_at by reading current row
        try:
            old = supabase.table("feed_status").select("last_success_at").eq("stream_key", stream_key).limit(1).execute()
            if old.data and old.data[0].get("last_success_at"):
                row["last_success_at"] = old.data[0]["last_success_at"]
        except Exception:
            pass
    supabase.table("feed_status").upsert(row).execute()


def prune_stream(source: str, categories: List[str], cycle_start: str) -> None:
    q = supabase.table("live_hazards").delete().eq("source", source).lt("updated_at", cycle_start)
    if categories:
        q = q.in_("category", categories)
    q.execute()


def run_ingestion_cycle() -> None:
    cycle_start = now_iso()
    print(f"--- Brink Global Hazard Cycle {cycle_start} ---")

    streams = [
        ("usgs_eq", "USGS", ["earthquake"], fetch_usgs_earthquakes),
        ("gdacs_tc", "GDACS / RSMC", ["cyclone"], fetch_gdacs_cyclones),
        ("gdacs_vo", "GDACS / GVP", ["volcano"], fetch_gdacs_volcanoes),
        ("gdacs_fl", "GDACS", ["flood"], fetch_gdacs_floods),
        ("gdacs_dr", "GDACS / GDO", ["drought"], fetch_gdacs_droughts),
        ("eonet_ls", "NASA EONET", ["landslide"], fetch_eonet_landslides),
        ("wmo_cap", "WMO SWIC / National Authority", ["flash_flood","flood","heavy_rain","extreme_temperature","cyclone","landslide","drought","extreme_weather"], fetch_wmo_cap_warnings),
    ]

    collected: List[Dict[str, Any]] = []
    successful: List[Tuple[str, List[str]]] = []

    for stream_key, source, categories, fn in streams:
        attempt = now_iso()
        print(f"Fetching {stream_key}...")
        ok, rows, err = fn()
        try:
            update_feed_status(stream_key, source, ",".join(categories), ok, len(rows), attempt, err)
        except Exception as e:
            print(f"[WARN] feed_status update failed for {stream_key}: {e}")
        if ok:
            successful.append((source, categories))
            collected.extend(rows)
            print(f"  ✓ {len(rows)} records")
        else:
            print(f"  ✗ {err}")

    # Deduplicate only exact stable IDs; different authoritative warnings remain separate signals by design.
    unique = list({e["id"]: e for e in collected if e.get("id")}.values())
    add_population_estimates(unique)

    if unique:
        try:
            supabase.table("live_hazards").upsert(unique).execute()
            print(f"✓ Upserted {len(unique)} signals")
        except Exception as e:
            print(f"❌ live_hazards upsert failed: {e}")
            return

    # Critical bug fix: prune a stream after ANY successful fetch, including a healthy zero-event response.
    for source, categories in successful:
        try:
            prune_stream(source, categories, cycle_start)
        except Exception as e:
            print(f"[WARN] stale prune failed for {source}: {e}")

    print("--- Cycle complete ---")


if __name__ == "__main__":
    run_ingestion_cycle()
