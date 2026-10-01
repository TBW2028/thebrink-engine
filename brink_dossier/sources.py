import os, math, json, time, requests, tempfile
from pathlib import Path
from datetime import datetime, timezone, timedelta

import cdsapi
import xarray as xr

CACHE_DIR = Path(".brink_cache")
CACHE_DIR.mkdir(exist_ok=True)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
CDS_API_KEY = os.environ.get("CDS_API_KEY")


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


def _percentile(values, q):
    vals = sorted(float(v) for v in values if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    pos = (len(vals) - 1) * q
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return vals[lo]
    return vals[lo] + (vals[hi] - vals[lo]) * (pos - lo)


def _cds_daily_temperature(lat, lon, statistic):
    """Download ERA5-Land daily 2 m temperature statistics for one grid-cell area."""
    if not CDS_API_KEY:
        raise RuntimeError("CDS_API_KEY is not configured.")

    cache_key = f"era5land_{statistic}_{round(lat, 2)}_{round(lon, 2)}_1991_2025.nc"
    cache_file = CACHE_DIR / cache_key

    if not cache_file.exists():
        client = cdsapi.Client(
            url="https://cds.climate.copernicus.eu/api",
            key=CDS_API_KEY,
            quiet=True,
            progress=False,
        )
        request = {
            "variable": ["2m_temperature"],
            "year": [str(y) for y in range(1991, 2026)],
            "month": [f"{m:02d}" for m in range(1, 13)],
            "day": [f"{d:02d}" for d in range(1, 32)],
            "daily_statistic": statistic,
            "time_zone": "utc+00:00",
            "frequency": "1_hourly",
            "area": [
                min(90.0, lat + 0.06),
                max(-180.0, lon - 0.06),
                max(-90.0, lat - 0.06),
                min(180.0, lon + 0.06),
            ],
        }
        client.retrieve(
            "derived-era5-land-daily-statistics",
            request,
            str(cache_file),
        )

    ds = xr.open_dataset(cache_file)
    try:
        if not ds.data_vars:
            raise RuntimeError("ERA5-Land response contained no data variables.")
        da = ds[next(iter(ds.data_vars))]

        time_dim = next(
            (d for d in da.dims if d in ("valid_time", "time", "date")),
            None,
        )
        if not time_dim:
            time_dim = next((d for d in da.dims if "time" in d.lower()), None)
        if not time_dim:
            raise RuntimeError("Could not identify the time dimension in ERA5-Land data.")

        for dim in list(da.dims):
            if dim != time_dim:
                da = da.isel({dim: 0})

        values = da.values.tolist()
        if not isinstance(values, list):
            values = [values]
        times = da[time_dim].values.tolist()
        if not isinstance(times, list):
            times = [times]

        rows = []
        for t, value in zip(times, values):
            try:
                temp = float(value)
                if math.isnan(temp):
                    continue
                # ERA5 temperatures are normally Kelvin.
                if temp > 150:
                    temp -= 273.15
                day = str(t)[:10]
                if len(day) < 10:
                    continue
                rows.append((day, temp))
            except (TypeError, ValueError):
                continue
        return rows
    finally:
        ds.close()


def fetch_historical_heat_context(lat, lon):
    """Institutional historical-heat baseline derived directly from Copernicus ERA5-Land."""
    if not CDS_API_KEY:
        return {
            "status": "not_configured",
            "reason": "CDS_API_KEY is not configured for Copernicus Climate Data Store access.",
            "dataset": "ERA5-Land",
            "baseline_period": "1991-2020",
        }

    try:
        maxima = _cds_daily_temperature(lat, lon, "daily_maximum")
        minima = _cds_daily_temperature(lat, lon, "daily_minimum")
    except Exception as exc:
        return {
            "status": "error",
            "reason": f"Copernicus ERA5-Land retrieval failed: {str(exc)[:260]}",
            "dataset": "ERA5-Land",
            "baseline_period": "1991-2020",
        }

    max_by_day = dict(maxima)
    min_by_day = dict(minima)
    shared_days = sorted(set(max_by_day) & set(min_by_day))
    if len(shared_days) < 365:
        return {
            "status": "error",
            "reason": "Copernicus ERA5-Land returned insufficient daily data.",
            "dataset": "ERA5-Land",
            "baseline_period": "1991-2020",
        }

    baseline_max = []
    baseline_min = []
    annual = {}
    hottest = None
    recent_year_counts = {}

    for day in shared_days:
        try:
            year = int(day[:4])
            tx = float(max_by_day[day])
            tn = float(min_by_day[day])
        except (TypeError, ValueError):
            continue

        if hottest is None or tx > hottest["temperature_c"]:
            hottest = {"date": day, "temperature_c": tx}

        if 1991 <= year <= 2020:
            baseline_max.append(tx)
            baseline_min.append(tn)
            yr = annual.setdefault(year, {"days_ge_35": 0, "days_ge_40": 0, "nights_ge_25": 0})
            if tx >= 35.0:
                yr["days_ge_35"] += 1
            if tx >= 40.0:
                yr["days_ge_40"] += 1
            if tn >= 25.0:
                yr["nights_ge_25"] += 1
        elif 2021 <= year <= 2025:
            recent_year_counts.setdefault(year, 0)
            if tx >= 35.0:
                recent_year_counts[year] += 1

    years = sorted(annual)
    if not baseline_max or not years:
        return {
            "status": "error",
            "reason": "Could not calculate the 1991-2020 ERA5-Land heat baseline.",
            "dataset": "ERA5-Land",
            "baseline_period": "1991-2020",
        }

    mean_days_35 = sum(annual[y]["days_ge_35"] for y in years) / len(years)
    mean_days_40 = sum(annual[y]["days_ge_40"] for y in years) / len(years)
    mean_nights_25 = sum(annual[y]["nights_ge_25"] for y in years) / len(years)
    recent_days_35 = (
        sum(recent_year_counts.values()) / len(recent_year_counts)
        if recent_year_counts else None
    )

    return {
        "status": "ok",
        "dataset": "ERA5-Land",
        "access": "Copernicus Climate Data Store — derived ERA5-Land daily statistics",
        "baseline_period": "1991-2020",
        "recent_period": "2021-2025",
        "daily_time_zone": "UTC+00:00",
        "spatial_resolution": "0.1° grid; ERA5-Land native resolution approximately 9 km",
        "p95_tmax_c": round(_percentile(baseline_max, 0.95), 1),
        "p99_tmax_c": round(_percentile(baseline_max, 0.99), 1),
        "mean_annual_days_ge_35c": round(mean_days_35, 1),
        "mean_annual_days_ge_40c": round(mean_days_40, 1),
        "mean_annual_nights_ge_25c": round(mean_nights_25, 1),
        "recent_mean_annual_days_ge_35c": round(recent_days_35, 1) if recent_days_35 is not None else None,
        "hottest_day": hottest,
        "baseline_years": len(years),
        "doi": "10.24381/cds.e9c9c792",
        "licence": "CC-BY",
        "limitations": (
            "Gridded reanalysis, not an on-site thermometer record. Daily statistics are aggregated in UTC. "
            "Building-scale urban heat, shade, ventilation and microclimate are not resolved."
        ),
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
    historical_heat = fetch_historical_heat_context(lat, lon)
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
        "historical_heat": historical_heat,
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
                "name": "Copernicus Climate Change Service (C3S) — ERA5-Land",
                "type": "Reanalysis / historical climate",
                "note": "Historical heat baseline derived from ERA5-Land daily statistics via the Copernicus Climate Data Store; CC-BY; DOI 10.24381/cds.e9c9c792."
            } if historical_heat.get("status") == "ok" else None,
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
