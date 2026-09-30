import os, math, json, time, requests
from pathlib import Path
from datetime import datetime, timezone, timedelta

CACHE_DIR = Path(".brink_cache")
CACHE_DIR.mkdir(exist_ok=True)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")


def _cached_get(url, params=None, ttl_seconds=1800, headers=None):
    key = str(url) + str(params)
    safe_key = "".join([c if c.isalnum() else "_" for c in key])[:80] + ".json"
    cache_file = CACHE_DIR / safe_key
    if cache_file.exists() and (time.time() - cache_file.stat().st_mtime < ttl_seconds):
        try:
            return json.loads(cache_file.read_text())
        except Exception:
            pass
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            cache_file.write_text(json.dumps(data))
            return data
    except Exception:
        pass
    return {}


def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0088
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def _iso_from_ms(ms):
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat()
    except Exception:
        return None


def fetch_live_hazards(lat, lon):
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []
    select = ",".join([
        "id","category","name","country","severity_tier","signal_mode","record_type",
        "source","latitude","longitude","magnitude","depth_km","population_50km",
        "observed_at","expires_at","alert_level","urgency","certainty"
    ])
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/live_hazards",
            params={"select": select, "limit": 1500},
            headers={
                "apikey": SUPABASE_KEY,
                "Authorization": f"Bearer {SUPABASE_KEY}",
            },
            timeout=18,
        )
        r.raise_for_status()
        now = datetime.now(timezone.utc)
        rows = []
        for h in r.json():
            try:
                hlat = float(h.get("latitude"))
                hlon = float(h.get("longitude"))
            except (TypeError, ValueError):
                continue
            expires = h.get("expires_at")
            if expires:
                try:
                    exp = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
                    if exp < now:
                        continue
                except Exception:
                    pass
            h["distance_km"] = round(haversine(lat, lon, hlat, hlon))
            rows.append(h)
        return sorted(rows, key=lambda x: x["distance_km"])
    except Exception:
        return []


def fetch_telemetry(lat, lon):
    now = datetime.now(timezone.utc)

    # USGS: observed earthquakes, 30-day regional context.
    usgs_url = "https://earthquake.usgs.gov/fdsnws/event/1/query"
    quakes_data = _cached_get(usgs_url, {
        "format": "geojson",
        "latitude": lat,
        "longitude": lon,
        "maxradiuskm": 350,
        "minmagnitude": 2.5,
        "starttime": (now - timedelta(days=30)).isoformat().replace("+00:00", "Z"),
        "endtime": now.isoformat().replace("+00:00", "Z"),
        "orderby": "time",
        "limit": 1000
    }, ttl_seconds=900)

    recent_events = []
    for f in quakes_data.get("features", [])[:12]:
        props = f.get("properties", {})
        coords = f.get("geometry", {}).get("coordinates", [])
        if len(coords) < 3:
            continue
        try:
            dist = haversine(lat, lon, float(coords[1]), float(coords[0]))
        except Exception:
            continue
        recent_events.append({
            "place": props.get("place", "Regional event"),
            "mag": round(float(props.get("mag") or 0), 1),
            "depth_km": round(float(coords[2]), 1),
            "distance_km": round(dist),
            "observed_at": _iso_from_ms(props.get("time") or 0),
            "source": "USGS"
        })

    # Open-Meteo: modelled/current atmospheric context and elevation.
    om_url = "https://api.open-meteo.com/v1/forecast"
    weather = _cached_get(om_url, {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,precipitation,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max",
        "forecast_days": 7,
        "timezone": "auto"
    }, ttl_seconds=900)

    current = weather.get("current") or {}
    daily = weather.get("daily") or {}
    days = []
    dates = daily.get("time") or []
    for i, day in enumerate(dates):
        try:
            days.append({
                "date": day,
                "tmax_c": daily.get("temperature_2m_max", [])[i],
                "tmin_c": daily.get("temperature_2m_min", [])[i],
                "precip_mm": daily.get("precipitation_sum", [])[i],
                "wind_max_kmh": daily.get("wind_speed_10m_max", [])[i],
            })
        except Exception:
            continue

    live = fetch_live_hazards(lat, lon)
    local_300 = [h for h in live if h["distance_km"] <= 300]
    nearby_1000 = [h for h in live if h["distance_km"] <= 1000]
    official_local = [h for h in local_300 if h.get("signal_mode") == "official_warning"]

    severity_rank = {"Critical": 4, "Severe": 3, "Significant": 2, "Monitor": 1}
    strongest = None
    if local_300:
        strongest = sorted(
            local_300,
            key=lambda h: (-(severity_rank.get(h.get("severity_tier"), 0)), h["distance_km"])
        )[0]

    # OSM operational-access context. This is observational map data, not hazard modelling.
    overpass_url = "https://overpass-api.de/api/interpreter"
    query = f"""[out:json][timeout:12];(
      way["highway"~"primary|secondary|tertiary"](around:1500,{lat},{lon});
      node["amenity"="fire_station"](around:25000,{lat},{lon});
      node["amenity"="hospital"](around:25000,{lat},{lon});
    );out center;"""
    road_names = set()
    nearest_fire = None
    nearest_hospital = None
    try:
        r = requests.post(overpass_url, data={"data": query}, timeout=15)
        if r.status_code == 200:
            for el in r.json().get("elements", []):
                tags = el.get("tags", {})
                if "highway" in tags:
                    road_names.add(tags.get("name") or "Unnamed mapped road")
                    continue
                el_lat = el.get("lat") or el.get("center", {}).get("lat")
                el_lon = el.get("lon") or el.get("center", {}).get("lon")
                if el_lat is None or el_lon is None:
                    continue
                d = round(haversine(lat, lon, float(el_lat), float(el_lon)), 1)
                if tags.get("amenity") == "fire_station":
                    nearest_fire = d if nearest_fire is None else min(nearest_fire, d)
                if tags.get("amenity") == "hospital":
                    nearest_hospital = d if nearest_hospital is None else min(nearest_hospital, d)
    except Exception:
        pass

    return {
        "retrieved_at": now.isoformat(),
        "elevation_m": weather.get("elevation"),
        "timezone": weather.get("timezone"),
        "weather_current": {
            "temperature_c": current.get("temperature_2m"),
            "precipitation_mm": current.get("precipitation"),
            "wind_kmh": current.get("wind_speed_10m"),
        },
        "forecast_days": days,
        "recent_quakes": recent_events,
        "quake_count_30d_350km": len(quakes_data.get("features", [])),
        "live_hazards_300km": local_300,
        "live_hazards_1000km": nearby_1000,
        "official_warnings_300km": official_local,
        "strongest_local_signal": strongest,
        "mapped_primary_roads_1_5km": sorted(road_names)[:12],
        "nearest_fire_station_km": nearest_fire,
        "nearest_hospital_km": nearest_hospital,
        "sources": [
            {
                "name": "The Brink World operational hazard layer",
                "type": "Observed / official-warning aggregation",
                "note": "USGS, GDACS, WMO SWIC/National Authorities and other configured operational feeds; coverage varies by source and country."
            },
            {
                "name": "USGS Earthquake Catalog",
                "type": "Observed",
                "note": "30-day earthquakes within 350 km, magnitude 2.5+ for regional context."
            },
            {
                "name": "Open-Meteo",
                "type": "Modelled / forecast",
                "note": "Current atmospheric conditions, seven-day forecast and elevation returned for the analysed coordinates."
            },
            {
                "name": "OpenStreetMap / Overpass",
                "type": "Mapped infrastructure",
                "note": "Nearby mapped roads, hospitals and fire stations; completeness varies by locality."
            }
        ]
    }
