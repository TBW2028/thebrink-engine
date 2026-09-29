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

import math
import os
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
    """SWIC member IDs generally start with ISO-3166 alpha-2, e.g. au-bom-en."""
    if not mid:
        return None, None
    iso2 = mid.split("-")[0].upper()
    if len(iso2) != 2:
        return None, None
    if pycountry:
        try:
            c = pycountry.countries.get(alpha_2=iso2)
            if c:
                return c.name, c.alpha_3
        except Exception:
            pass
    return iso2, None


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


def cap_xml_summary(url: str) -> Dict[str, Any]:
    """Best-effort CAP XML parser. Returns severity/urgency/certainty plus point/area centroid where supplied."""
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
        lat = lon = None
        polygons = vals.get("polygon", [])
        circles = vals.get("circle", [])
        if polygons:
            pts = []
            for token in polygons[0].replace(";", " ").split():
                if "," not in token: continue
                a, b = token.split(",", 1)
                try: pts.append((float(a), float(b))) # CAP: lat,lon
                except Exception: pass
            if pts:
                lat = sum(x[0] for x in pts)/len(pts); lon = sum(x[1] for x in pts)/len(pts)
        elif circles:
            center = circles[0].split()[0]
            if "," in center:
                a,b = center.split(",",1)
                try: lat,lon=float(a),float(b)
                except Exception: pass
        return {
            "lat": lat, "lon": lon,
            "severity": (vals.get("severity") or [None])[0],
            "urgency": (vals.get("urgency") or [None])[0],
            "certainty": (vals.get("certainty") or [None])[0],
            "event": (vals.get("event") or [None])[0],
            "headline": (vals.get("headline") or [None])[0],
            "areaDesc": (vals.get("areaDesc") or [None])[0],
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

        for item in items:
            if not isinstance(item, dict): continue
            text = " ".join(str(item.get(k) or "") for k in ["event", "headline", "areaDesc"])
            cat = classify_weather_alert(text)
            if not cat: continue

            severity = str(item.get("severity") or item.get("s") or "Unknown")
            urgency = str(item.get("urgency") or item.get("u") or "Unknown")
            certainty = str(item.get("certainty") or item.get("c") or "Unknown")
            # Some SWIC summaries use coded values. Preserve them if not human-readable; CAP detail can improve them.
            mid = str(item.get("mid") or "")
            country, iso3 = country_from_mid(mid)
            lat = lon = None
            source_url = item.get("url")
            details: Dict[str, Any] = {}
            sev_lower = severity.lower()
            needs_detail = source_url and detail_budget > 0 and (sev_lower in {"extreme", "severe", "4", "3"} or cat in {"flash_flood", "cyclone", "landslide"})
            if needs_detail:
                details = cap_xml_summary(str(source_url)); detail_budget -= 1
                lat, lon = details.get("lat"), details.get("lon")
                severity = details.get("severity") or severity
                urgency = details.get("urgency") or urgency
                certainty = details.get("certainty") or certainty
                if details.get("event"): text = f"{details.get('event')} {details.get('headline') or ''} {details.get('areaDesc') or ''}"
                cat = classify_weather_alert(text) or cat

            # normalize coded SWIC severities if necessary
            coded = {"4":"Extreme", "3":"Severe", "2":"Moderate", "1":"Minor", "0":"Unknown"}
            severity = coded.get(str(severity), str(severity).capitalize())
            band = source_level_to_band(severity)

            effective = item.get("effective") or item.get("sent") or now_iso()
            expires = item.get("expires")
            name = item.get("headline") or item.get("event") or f"Official {cat.replace('_',' ')} warning"
            rows.append(base_event(
                id=f"WMO_CAP_{item.get('id') or (mid + '_' + str(effective))}", category=cat, name=str(name),
                basin="WMO MEMBER", intensity=str(item.get("event") or cat.replace("_"," ").title()),
                latitude=lat, longitude=lon, alert_level=severity, severity_tier=band,
                source="WMO SWIC / National Authority", signal_mode="official_warning", record_type="warning",
                country=country, iso3=iso3, observed_at=effective, source_url=(str(source_url) if source_url else None),
                expires_at=expires, urgency=urgency, certainty=certainty
            ))
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
