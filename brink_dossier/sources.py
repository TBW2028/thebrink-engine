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


def _element_point(el):
    lat = el.get("lat") or el.get("center", {}).get("lat")
    lon = el.get("lon") or el.get("center", {}).get("lon")
    try:
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None


def _facility_record(el, lat, lon):
    tags = el.get("tags", {})
    point = _element_point(el)
    if not point:
        return None
    el_lat, el_lon = point
    phone = (
        tags.get("phone") or tags.get("contact:phone") or
        tags.get("emergency:phone") or tags.get("contact:mobile")
    )
    return {
        "name": tags.get("name") or tags.get("official_name") or "Unnamed mapped facility",
        "distance_km": round(haversine(lat, lon, el_lat, el_lon), 1),
        "phone": phone,
        "operator": tags.get("operator"),
        "source": "OpenStreetMap / Overpass"
    }


def fetch_osm_operational_context(lat, lon):
    """
    Mapped-service context, not an assertion that an unmapped service does not exist.
    We search a broad radius because rural and mountain locations may rely on a
    district-centre service well outside the immediate locality.
    """
    endpoints = [
        "https://overpass-api.de/api/interpreter",
        "https://overpass.kumi.systems/api/interpreter",
    ]

    service_radius_m = 80000
    transport_radius_m = 120000
    road_radius_m = 5000
    query = f"""[out:json][timeout:24];(
      nwr["amenity"~"hospital|clinic|fire_station|police"](around:{service_radius_m},{lat},{lon});
      nwr["railway"="station"](around:{transport_radius_m},{lat},{lon});
      nwr["aeroway"="aerodrome"](around:{transport_radius_m},{lat},{lon});
      way["highway"~"primary|secondary|tertiary"](around:{road_radius_m},{lat},{lon});
    );out center tags 650;"""

    elements = []
    for endpoint in endpoints:
        try:
            r = requests.post(endpoint, data={"data": query}, timeout=30)
            if r.status_code == 200:
                elements = r.json().get("elements", [])
                if elements:
                    break
        except Exception:
            continue

    road_names = set()
    buckets = {
        "hospital": [], "clinic": [], "fire_station": [], "police": [],
        "railway_station": [], "aerodrome": []
    }

    for el in elements:
        tags = el.get("tags", {})
        if "highway" in tags:
            road_names.add(tags.get("name") or tags.get("ref") or "Unnamed mapped road")
            continue

        rec = _facility_record(el, lat, lon)
        if not rec:
            continue
        amenity = tags.get("amenity")
        if amenity in {"hospital", "clinic", "fire_station", "police"}:
            buckets[amenity].append(rec)
        if tags.get("railway") == "station":
            buckets["railway_station"].append(rec)
        if tags.get("aeroway") == "aerodrome":
            buckets["aerodrome"].append(rec)

    for key in buckets:
        buckets[key] = sorted(buckets[key], key=lambda x: x["distance_km"])

    nearest_medical = None
    medical_type = None
    if buckets["hospital"]:
        nearest_medical = buckets["hospital"][0]
        medical_type = "Hospital"
    elif buckets["clinic"]:
        nearest_medical = buckets["clinic"][0]
        medical_type = "Clinic"

    return {
        "mapped_primary_roads_5km": sorted(road_names)[:20],
        "service_search_radius_km": service_radius_m // 1000,
        "transport_search_radius_km": transport_radius_m // 1000,
        "nearest_fire_station": buckets["fire_station"][0] if buckets["fire_station"] else None,
        "nearest_hospital_or_clinic": nearest_medical,
        "nearest_medical_type": medical_type,
        "nearest_police": buckets["police"][0] if buckets["police"] else None,
        "nearest_railway_station": buckets["railway_station"][0] if buckets["railway_station"] else None,
        "nearest_aerodrome": buckets["aerodrome"][0] if buckets["aerodrome"] else None,
    }


def emergency_contacts(country_code):
    cc = str(country_code or "").upper()
    if cc == "IN":
        return [{
            "service": "Unified emergency response",
            "number": "112",
            "scope": "India — police, fire, health and other emergency routing",
            "source": "Ministry of Home Affairs, Emergency Response Support System (ERSS)"
        }]
    return []


def forecast_summary(days):
    vals = [d for d in days if isinstance(d, dict)]
    rain = [float(d.get("precip_mm") or 0) for d in vals]
    winds = [float(d.get("wind_max_kmh") or 0) for d in vals]
    highs = [float(d["tmax_c"]) for d in vals if d.get("tmax_c") is not None]
    lows = [float(d["tmin_c"]) for d in vals if d.get("tmin_c") is not None]
    return {
        "rain_total_7d_mm": round(sum(rain), 1) if rain else None,
        "rain_days_7d": sum(1 for x in rain if x >= 1.0),
        "max_wind_7d_kmh": round(max(winds), 1) if winds else None,
        "max_temp_7d_c": round(max(highs), 1) if highs else None,
        "min_temp_7d_c": round(min(lows), 1) if lows else None,
    }


def agriculture_context(now, country_code, purpose_details, wx_summary):
    details = purpose_details or {}
    crop = str(details.get("crop") or "").strip() or None
    crop_stage = str(details.get("crop_stage") or "").strip() or None
    result = {
        "crop": crop,
        "crop_stage": crop_stage,
        "weather_window": wx_summary,
        "season_label": None,
        "season_note": None,
        "advisory_note": (
            "Crop-specific sowing, irrigation, spraying and harvest decisions should be checked "
            "against the local agricultural authority or agrometeorological advisory for the crop and district."
        ),
    }

    if str(country_code or "").upper() == "IN":
        month = now.month
        if month in (7, 8, 9):
            label = "Kharif season"
            note = "Broad national seasonal context: Kharif runs through the southwest-monsoon period, approximately July–October."
        elif month == 10:
            label = "Kharif harvest / Rabi transition"
            note = "Broad national seasonal context: October is commonly a Kharif harvest and Rabi establishment transition, varying by crop, altitude and district."
        elif month in (11, 12, 1, 2):
            label = "Rabi season"
            note = "Broad national seasonal context: Rabi occupies the winter cropping period, approximately October–March."
        elif month == 3:
            label = "Rabi harvest / summer transition"
            note = "Broad national seasonal context: March is commonly a Rabi harvest and summer-crop transition, depending on crop and locality."
        else:
            label = "Summer / pre-Kharif period"
            note = "Broad national seasonal context: crops grown between March and June are commonly treated as summer crops before the main Kharif season."
        result["season_label"] = label
        result["season_note"] = note
        result["official_agromet_source"] = "India Meteorological Department — Agromet Advisory Services / GKMS"

    return result


def fetch_telemetry(lat, lon, context=None):
    context = context or {}
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

    osm = fetch_osm_operational_context(lat, lon)
    wx_summary = forecast_summary(days)
    purpose_details = context.get("purpose_details") or {}
    purpose = str(context.get("occupancy") or context.get("purpose") or "").lower()

    agri = None
    if "agric" in purpose or "farm" in purpose or "land" in purpose:
        agri = agriculture_context(
            now,
            context.get("country_code"),
            purpose_details,
            wx_summary
        )

    return {
        "retrieved_at": now.isoformat(),
        "elevation_m": weather.get("elevation"),
        "timezone": weather.get("timezone"),
        "country": context.get("country"),
        "country_code": context.get("country_code"),
        "weather_current": {
            "temperature_c": current.get("temperature_2m"),
            "precipitation_mm": current.get("precipitation"),
            "wind_kmh": current.get("wind_speed_10m"),
        },
        "forecast_days": days,
        "forecast_summary": wx_summary,
        "recent_quakes": recent_events,
        "quake_count_30d_350km": len(quakes_data.get("features", [])),
        "live_hazards_300km": local_300,
        "live_hazards_1000km": nearby_1000,
        "official_warnings_300km": official_local,
        "strongest_local_signal": strongest,
        "mapped_primary_roads_5km": osm.get("mapped_primary_roads_5km") or [],
        "service_search_radius_km": osm.get("service_search_radius_km"),
        "transport_search_radius_km": osm.get("transport_search_radius_km"),
        "nearest_fire_station": osm.get("nearest_fire_station"),
        "nearest_hospital_or_clinic": osm.get("nearest_hospital_or_clinic"),
        "nearest_medical_type": osm.get("nearest_medical_type"),
        "nearest_police": osm.get("nearest_police"),
        "nearest_railway_station": osm.get("nearest_railway_station"),
        "nearest_aerodrome": osm.get("nearest_aerodrome"),
        "emergency_contacts": emergency_contacts(context.get("country_code")),
        "agriculture_context": agri,
        "sources": [s for s in [
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
                "note": "Mapped emergency services, roads and transport nodes. Absence from the query is not proof that a real-world service does not exist."
            },
            {
                "name": "Ministry of Home Affairs, India — ERSS 112",
                "type": "Official emergency contact",
                "note": "Included only for Indian locations; 112 is the nationwide unified emergency-response number."
            } if str(context.get("country_code") or "").upper() == "IN" else None,
            {
                "name": "India Meteorological Department — Agromet Advisory Services / GKMS",
                "type": "Official agricultural weather service",
                "note": "For Indian agricultural reports, IMD provides district/state agromet advisories and dynamic crop-weather information; crop-specific local advice should defer to the relevant bulletin."
            } if agri and str(context.get("country_code") or "").upper() == "IN" else None,
        ] if s is not None]
    }
