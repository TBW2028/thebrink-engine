import io
import os, math, json, time, requests, tempfile, zipfile, re
from pathlib import Path
from datetime import datetime, timezone, timedelta

import cdsapi
import xarray as xr
import numpy as np
import rasterio
from rasterio.windows import Window
from rasterio.warp import transform as rio_transform
from rasterio.transform import xy as raster_xy
import pandas as pd
import pyogrio
from shapely.geometry import Point
from pyproj import Transformer

CACHE_DIR = Path(".brink_cache")
CACHE_DIR.mkdir(exist_ok=True)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
CDS_API_KEY = os.environ.get("CDS_API_KEY")
NASA_FIRMS_MAP_KEY = os.environ.get("NASA_FIRMS_MAP_KEY")
JRC_FLOOD_BASE = "https://jeodpp.jrc.ec.europa.eu/ftp/jrc-opendata/CEMS-GLOFAS/flood_hazard"
WRI_AQUEDUCT_ZIP = "https://files.wri.org/aqueduct/aqueduct-4-0-water-risk-data.zip"
WRI_AQUEDUCT_FEATURESERVER = "https://services.arcgis.com/P3ePLMYs2RVChkJx/ArcGIS/rest/services/aqueduct_water_risk/FeatureServer"
COPERNICUS_DEM_30M_BASE = "https://copernicus-dem-30m.s3.amazonaws.com"
IBTRACS_SINCE1980_CSV = "https://www.ncei.noaa.gov/data/international-best-track-archive-for-climate-stewardship-ibtracs/v04r01/access/csv/ibtracs.since1980.list.v04r01.csv"


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


def fetch_met_no_weather(lat, lon):
    """Global current/short-range weather from MET Norway Locationforecast 2.0."""
    url = "https://api.met.no/weatherapi/locationforecast/2.0/compact"
    headers = {
        "User-Agent": "TheBrinkWorld/1.0 thebrink2028@gmail.com",
        "Accept": "application/json",
    }
    payload = _cached_get(
        url,
        {"lat": round(float(lat), 5), "lon": round(float(lon), 5)},
        ttl_seconds=1800,
        headers=headers,
    )
    timeseries = ((payload.get("properties") or {}).get("timeseries") or [])
    if not timeseries:
        return {
            "status": "error",
            "reason": "MET Norway Locationforecast did not return a usable time series.",
            "current": {},
            "days": [],
            "timezone": "UTC",
        }

    first = timeseries[0]
    first_data = first.get("data") or {}
    instant = ((first_data.get("instant") or {}).get("details") or {})

    precip_amount = None
    precip_period_hours = None
    for key, hours in (("next_1_hours", 1), ("next_6_hours", 6), ("next_12_hours", 12)):
        details = ((first_data.get(key) or {}).get("details") or {})
        if details.get("precipitation_amount") is not None:
            precip_amount = details.get("precipitation_amount")
            precip_period_hours = hours
            break

    daily = {}
    last_precip_end = None
    for entry in timeseries:
        ts_text = str(entry.get("time") or "")
        if len(ts_text) < 10:
            continue
        try:
            ts = datetime.fromisoformat(ts_text.replace("Z", "+00:00"))
        except Exception:
            continue
        day = ts.date().isoformat()
        rec = daily.setdefault(day, {"temps": [], "winds": [], "precip_mm": 0.0, "has_precip": False})
        data = entry.get("data") or {}
        details = ((data.get("instant") or {}).get("details") or {})
        temp = details.get("air_temperature")
        wind_ms = details.get("wind_speed")
        try:
            if temp is not None:
                rec["temps"].append(float(temp))
        except Exception:
            pass
        try:
            if wind_ms is not None:
                rec["winds"].append(float(wind_ms) * 3.6)
        except Exception:
            pass

        selected = None
        for key, hours in (("next_1_hours", 1), ("next_6_hours", 6), ("next_12_hours", 12)):
            p = ((data.get(key) or {}).get("details") or {}).get("precipitation_amount")
            if p is not None:
                selected = (hours, p)
                break
        if selected:
            hours, amount = selected
            interval_end = ts + timedelta(hours=hours)
            if last_precip_end is None or ts >= last_precip_end:
                try:
                    rec["precip_mm"] += max(0.0, float(amount))
                    rec["has_precip"] = True
                    last_precip_end = interval_end
                except Exception:
                    pass

    days = []
    for day in sorted(daily)[:7]:
        rec = daily[day]
        days.append({
            "date": day,
            "tmax_c": round(max(rec["temps"]), 1) if rec["temps"] else None,
            "tmin_c": round(min(rec["temps"]), 1) if rec["temps"] else None,
            "precip_mm": round(rec["precip_mm"], 1) if rec["has_precip"] else None,
            "wind_max_kmh": round(max(rec["winds"]), 1) if rec["winds"] else None,
        })

    current_temp = instant.get("air_temperature")
    current_wind_ms = instant.get("wind_speed")
    try:
        current_wind_kmh = round(float(current_wind_ms) * 3.6, 1) if current_wind_ms is not None else None
    except Exception:
        current_wind_kmh = None

    return {
        "status": "ok",
        "current": {
            "temperature_c": current_temp,
            "precipitation_mm": precip_amount,
            "precipitation_period_hours": precip_period_hours,
            "wind_kmh": current_wind_kmh,
        },
        "days": days,
        "timezone": "UTC",
        "source": "MET Norway Locationforecast 2.0",
        "licence": "CC BY 4.0",
        "limitations": (
            "Point forecast from MET Norway Locationforecast. Forecast skill and available parameters vary by location "
            "and lead time. Daily summaries are derived by The Brink World from the returned time series and should be "
            "used as operational weather context, not as climatology or engineering design data."
        ),
    }


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
    """Download ERA5-Land daily 2 m temperature statistics in bounded five-year chunks."""
    if not CDS_API_KEY:
        raise RuntimeError("CDS_API_KEY is not configured.")

    client = cdsapi.Client(
        url="https://cds.climate.copernicus.eu/api",
        key=CDS_API_KEY,
        quiet=True,
        progress=False,
    )
    rows = []

    for chunk_start in range(1991, 2026, 5):
        chunk_end = min(chunk_start + 4, 2025)
        cache_stem = (
            f"era5land_{statistic}_{round(lat, 2)}_{round(lon, 2)}_"
            f"{chunk_start}_{chunk_end}"
        )
        zip_path = CACHE_DIR / f"{cache_stem}.zip"
        extract_dir = CACHE_DIR / cache_stem

        if not zip_path.exists():
            request = {
                "variable": ["2m_temperature"],
                "year": [str(y) for y in range(chunk_start, chunk_end + 1)],
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
                str(zip_path),
            )

        extract_dir.mkdir(exist_ok=True)
        nc_files = sorted(extract_dir.glob("*.nc"))
        if not nc_files:
            if zipfile.is_zipfile(zip_path):
                with zipfile.ZipFile(zip_path, "r") as zf:
                    safe_members = [
                        name for name in zf.namelist()
                        if name.lower().endswith(".nc")
                        and ".." not in Path(name).parts
                        and not Path(name).is_absolute()
                    ]
                    if not safe_members:
                        raise RuntimeError("CDS ERA5-Land ZIP contained no NetCDF file.")
                    for name in safe_members:
                        target = extract_dir / Path(name).name
                        with zf.open(name) as source, open(target, "wb") as dest:
                            dest.write(source.read())
                nc_files = sorted(extract_dir.glob("*.nc"))
            else:
                direct_nc = extract_dir / f"{cache_stem}.nc"
                direct_nc.write_bytes(zip_path.read_bytes())
                nc_files = [direct_nc]

        for nc_file in nc_files:
            ds = xr.open_dataset(nc_file)
            try:
                if not ds.data_vars:
                    continue
                da = ds[next(iter(ds.data_vars))]
                time_dim = next((d for d in da.dims if d in ("valid_time", "time", "date")), None)
                if not time_dim:
                    time_dim = next((d for d in da.dims if "time" in d.lower()), None)
                if not time_dim:
                    raise RuntimeError("Could not identify the time dimension in ERA5-Land data.")
                for dim in list(da.dims):
                    if dim != time_dim:
                        da = da.isel({dim: 0})
                for t, value in zip(list(da[time_dim].values), list(da.values)):
                    try:
                        temp = float(value)
                        if math.isnan(temp):
                            continue
                        if temp > 150:
                            temp -= 273.15
                        day = str(t)[:10]
                        if len(day) == 10 and day[4] == "-" and day[7] == "-":
                            rows.append((day, temp))
                    except (TypeError, ValueError):
                        continue
            finally:
                ds.close()

    rows.sort(key=lambda x: x[0])
    if not rows:
        raise RuntimeError("ERA5-Land NetCDF contained no usable daily temperature values.")
    return rows


def fetch_historical_heat_context(lat, lon):
    """Historical heat baseline derived from ERA5-Land hourly point time-series."""
    if not CDS_API_KEY:
        return {
            "status": "not_configured",
            "reason": "CDS API access is not configured.",
            "dataset": "ERA5-Land",
            "baseline_period": "1991-2020",
        }

    try:
        hourly, units = _cds_hourly_point_series(
            lat,
            lon,
            "2m_temperature",
            "1991-01-01",
            "2025-12-31",
            "temperature",
        )
    except Exception as exc:
        return {
            "status": "error",
            "reason": f"Copernicus ERA5-Land heat retrieval failed: {str(exc)[:260]}",
            "dataset": "ERA5-Land",
            "baseline_period": "1991-2020",
        }

    daily = {}
    for ts, value in hourly:
        try:
            temp = float(value)
            if math.isnan(temp):
                continue
            if temp > 150:
                temp -= 273.15
            day = str(ts)[:10]
            if len(day) != 10:
                continue
        except Exception:
            continue
        rec = daily.setdefault(day, {"min": temp, "max": temp})
        rec["min"] = min(rec["min"], temp)
        rec["max"] = max(rec["max"], temp)

    if len(daily) < 365:
        return {
            "status": "error",
            "reason": "ERA5-Land returned insufficient hourly temperature data to derive a heat baseline.",
            "dataset": "ERA5-Land",
            "baseline_period": "1991-2020",
        }

    baseline_max = []
    baseline_min = []
    annual = {}
    hottest = None
    recent_year_counts = {}

    for day, rec in sorted(daily.items()):
        try:
            year = int(day[:4])
            tx = float(rec["max"])
            tn = float(rec["min"])
        except Exception:
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

    recent_years = sorted(recent_year_counts)
    recent_mean_days_35 = (
        sum(recent_year_counts[y] for y in recent_years) / len(recent_years)
        if recent_years else None
    )

    return {
        "status": "ok",
        "dataset": "ERA5-Land hourly time-series",
        "publisher": "Copernicus Climate Change Service (C3S)",
        "baseline_period": "1991-2020",
        "recent_period": "2021-2025",
        "p95_tmax_c": round(_percentile(baseline_max, 0.95), 1),
        "p99_tmax_c": round(_percentile(baseline_max, 0.99), 1),
        "mean_annual_days_ge_35c": round(mean_days_35, 1),
        "mean_annual_days_ge_40c": round(mean_days_40, 1),
        "mean_annual_nights_ge_25c": round(mean_nights_25, 1),
        "recent_mean_annual_days_ge_35c": round(recent_mean_days_35, 1) if recent_mean_days_35 is not None else None,
        "hottest_day": hottest,
        "spatial_resolution": "ERA5-Land point time-series (~0.1° native grid)",
        "temporal_resolution": "hourly source aggregated by The Brink World to daily extrema",
        "units_source": units,
        "limitations": (
            "ERA5-Land is gridded reanalysis rather than an on-site thermometer. The Brink World derives daily maximum "
            "and minimum temperatures from hourly point time-series for screening. Local microclimate, urban heat, "
            "indoor conditions and future climate change require separate evidence."
        ),
    }


def _cds_hourly_point_series(lat, lon, variable, start_date, end_date, cache_tag):
    """Retrieve ERA5-Land hourly point data in bounded five-year chunks."""
    if not CDS_API_KEY:
        raise RuntimeError("CDS_API_KEY is not configured.")

    start_dt = datetime.fromisoformat(start_date)
    end_dt = datetime.fromisoformat(end_date)
    client = cdsapi.Client(
        url="https://cds.climate.copernicus.eu/api",
        key=CDS_API_KEY,
        quiet=True,
        progress=False,
    )

    rows = []
    units = None
    cursor_year = start_dt.year
    while cursor_year <= end_dt.year:
        chunk_end_year = min(cursor_year + 4, end_dt.year)
        chunk_start = start_date if cursor_year == start_dt.year else f"{cursor_year}-01-01"
        chunk_end = end_date if chunk_end_year == end_dt.year else f"{chunk_end_year}-12-31"

        cache_stem = (
            f"era5land_timeseries_{cache_tag}_{round(lat, 2)}_{round(lon, 2)}_"
            f"{chunk_start.replace('-', '')}_{chunk_end.replace('-', '')}"
        )
        zip_path = CACHE_DIR / f"{cache_stem}.zip"
        extract_dir = CACHE_DIR / cache_stem

        if not zip_path.exists():
            request = {
                "variable": [variable],
                "location": {"longitude": lon, "latitude": lat},
                "date": [f"{chunk_start}/{chunk_end}"],
                "data_format": "netcdf",
            }
            client.retrieve(
                "reanalysis-era5-land-timeseries",
                request,
                str(zip_path),
            )

        extract_dir.mkdir(exist_ok=True)
        nc_files = sorted(extract_dir.glob("*.nc"))
        if not nc_files:
            if zipfile.is_zipfile(zip_path):
                with zipfile.ZipFile(zip_path, "r") as zf:
                    safe_members = [
                        name for name in zf.namelist()
                        if name.lower().endswith(".nc")
                        and ".." not in Path(name).parts
                        and not Path(name).is_absolute()
                    ]
                    if not safe_members:
                        raise RuntimeError("CDS ERA5-Land time-series ZIP contained no NetCDF file.")
                    for name in safe_members:
                        target = extract_dir / Path(name).name
                        with zf.open(name) as source, open(target, "wb") as dest:
                            dest.write(source.read())
                nc_files = sorted(extract_dir.glob("*.nc"))
            else:
                direct_nc = extract_dir / f"{cache_stem}.nc"
                direct_nc.write_bytes(zip_path.read_bytes())
                nc_files = [direct_nc]

        for nc_file in nc_files:
            ds = xr.open_dataset(nc_file)
            try:
                if not ds.data_vars:
                    continue
                preferred = [variable, "tp" if variable == "total_precipitation" else None]
                var_name = next((name for name in preferred if name and name in ds.data_vars), None)
                if not var_name:
                    var_name = next(iter(ds.data_vars))
                da = ds[var_name]
                units = units or da.attrs.get("units")
                time_dim = next((d for d in da.dims if d in ("valid_time", "time", "date")), None)
                if not time_dim:
                    time_dim = next((d for d in da.dims if "time" in d.lower()), None)
                if not time_dim:
                    raise RuntimeError("Could not identify the ERA5-Land time dimension.")
                for dim in list(da.dims):
                    if dim != time_dim:
                        da = da.isel({dim: 0})
                for t, value in zip(list(da[time_dim].values), list(da.values)):
                    try:
                        val = float(value)
                        if math.isnan(val):
                            continue
                        ts = str(t)
                        if len(ts) >= 10:
                            rows.append((ts, val))
                    except (TypeError, ValueError):
                        continue
            finally:
                ds.close()

        cursor_year = chunk_end_year + 1

    rows.sort(key=lambda x: x[0])
    if not rows:
        raise RuntimeError(f"ERA5-Land returned no usable values for {variable}.")
    return rows, units


def fetch_historical_rainfall_context(lat, lon):
    """Historical extreme-rainfall baseline derived from ERA5-Land hourly precipitation."""
    if not CDS_API_KEY:
        return {
            "status": "not_configured",
            "reason": "CDS_API_KEY is not configured for Copernicus Climate Data Store access.",
            "dataset": "ERA5-Land",
            "baseline_period": "1991-2020",
        }

    try:
        hourly, units = _cds_hourly_point_series(
            lat, lon,
            "total_precipitation",
            "1991-01-01",
            "2025-12-31",
            "precipitation",
        )
    except Exception as exc:
        return {
            "status": "error",
            "reason": f"Copernicus ERA5-Land precipitation retrieval failed: {str(exc)[:260]}",
            "dataset": "ERA5-Land",
            "baseline_period": "1991-2020",
        }

    unit_text = str(units or "").strip().lower()
    multiplier = 1000.0 if unit_text in {"m", "metre", "meter", "m of water equivalent"} or "metre" in unit_text else 1.0

    daily = {}
    hourly_counts = {}
    for ts, raw_value in hourly:
        day = ts[:10]
        try:
            value_mm = max(0.0, float(raw_value) * multiplier)
        except (TypeError, ValueError):
            continue
        daily[day] = daily.get(day, 0.0) + value_mm
        hourly_counts[day] = hourly_counts.get(day, 0) + 1

    # Exclude clearly incomplete days so partial retrievals do not masquerade as low rainfall.
    daily = {
        day: total
        for day, total in daily.items()
        if hourly_counts.get(day, 0) >= 23
    }
    if len(daily) < 365:
        return {
            "status": "error",
            "reason": "Copernicus ERA5-Land returned insufficient complete daily precipitation data.",
            "dataset": "ERA5-Land",
            "baseline_period": "1991-2020",
        }

    annual = {}
    wet_day_values = []
    recent = {}
    wettest = None

    for day in sorted(daily):
        try:
            year = int(day[:4])
            rain = float(daily[day])
        except (TypeError, ValueError):
            continue

        if wettest is None or rain > wettest["precipitation_mm"]:
            wettest = {"date": day, "precipitation_mm": rain}

        if 1991 <= year <= 2020:
            yr = annual.setdefault(year, {
                "total_mm": 0.0,
                "wet_days": 0,
                "days_ge_20": 0,
                "days_ge_50": 0,
                "daily": [],
            })
            yr["total_mm"] += rain
            yr["daily"].append((day, rain))
            if rain >= 1.0:
                yr["wet_days"] += 1
                wet_day_values.append(rain)
            if rain >= 20.0:
                yr["days_ge_20"] += 1
            if rain >= 50.0:
                yr["days_ge_50"] += 1
        elif 2021 <= year <= 2025:
            yr = recent.setdefault(year, {"days_ge_20": 0, "daily": []})
            yr["daily"].append((day, rain))
            if rain >= 20.0:
                yr["days_ge_20"] += 1

    years = sorted(annual)
    if not years:
        return {
            "status": "error",
            "reason": "Could not calculate the 1991-2020 ERA5-Land rainfall baseline.",
            "dataset": "ERA5-Land",
            "baseline_period": "1991-2020",
        }

    annual_rx1 = []
    annual_rx5 = []
    for year in years:
        vals = [rain for _, rain in sorted(annual[year]["daily"])]
        if vals:
            annual_rx1.append(max(vals))
        if len(vals) >= 5:
            annual_rx5.append(max(sum(vals[i:i+5]) for i in range(len(vals) - 4)))

    recent_rx1 = []
    for year in sorted(recent):
        vals = [rain for _, rain in sorted(recent[year]["daily"])]
        if vals:
            recent_rx1.append(max(vals))

    return {
        "status": "ok",
        "dataset": "ERA5-Land",
        "access": "Copernicus Climate Data Store — ERA5-Land hourly time-series",
        "baseline_period": "1991-2020",
        "recent_period": "2021-2025",
        "daily_time_zone": "UTC+00:00",
        "spatial_resolution": "0.1° grid; ERA5-Land native resolution approximately 9 km",
        "mean_annual_precip_mm": round(sum(annual[y]["total_mm"] for y in years) / len(years), 1),
        "mean_annual_wet_days": round(sum(annual[y]["wet_days"] for y in years) / len(years), 1),
        "mean_annual_days_ge_20mm": round(sum(annual[y]["days_ge_20"] for y in years) / len(years), 1),
        "mean_annual_days_ge_50mm": round(sum(annual[y]["days_ge_50"] for y in years) / len(years), 1),
        "p95_wet_day_mm": round(_percentile(wet_day_values, 0.95), 1) if wet_day_values else None,
        "mean_annual_rx1day_mm": round(sum(annual_rx1) / len(annual_rx1), 1) if annual_rx1 else None,
        "mean_annual_rx5day_mm": round(sum(annual_rx5) / len(annual_rx5), 1) if annual_rx5 else None,
        "recent_mean_annual_days_ge_20mm": (
            round(sum(recent[y]["days_ge_20"] for y in recent) / len(recent), 1)
            if recent else None
        ),
        "recent_mean_annual_rx1day_mm": (
            round(sum(recent_rx1) / len(recent_rx1), 1)
            if recent_rx1 else None
        ),
        "wettest_day": (
            {"date": wettest["date"], "precipitation_mm": round(wettest["precipitation_mm"], 1)}
            if wettest else None
        ),
        "baseline_years": len(years),
        "doi": "10.24381/ee82e357",
        "licence": "CC-BY-4.0",
        "limitations": (
            "Gridded reanalysis at the nearest ERA5-Land grid point, not a site rain gauge. "
            "Daily totals are aggregated from hourly time-series in UTC. Historical rainfall intensity "
            "does not establish riverine/pluvial flood depth, return period, drainage capacity or building inundation."
        ),
    }


def _jrc_tile_label(lat, lon):
    """Return the JRC 10-degree tile label used in the flood-hazard filenames."""
    if lat > 0:
        lat_label = f"N{int(math.ceil(lat / 10.0) * 10)}"
    elif lat < 0:
        lat_label = f"S{int(math.ceil(abs(lat) / 10.0) * 10)}"
    else:
        lat_label = "N0"

    if lon > 0:
        lon_band = int(math.floor(lon / 10.0) * 10)
        lon_label = f"E{lon_band}" if lon_band > 0 else "W0"
    elif lon < 0:
        lon_label = f"W{int(math.ceil(abs(lon) / 10.0) * 10)}"
    else:
        lon_label = "W0"

    return f"{lat_label}_{lon_label}"


def _jrc_tile_prefix(lat, lon):
    """Resolve the tile ID from the public RP100 directory index."""
    tile_label = _jrc_tile_label(lat, lon)
    index_url = f"{JRC_FLOOD_BASE}/RP100/"
    r = requests.get(index_url, timeout=25)
    r.raise_for_status()
    pattern = re.compile(
        rf"(ID\d+_{re.escape(tile_label)})_RP100_depth\.tif",
        re.IGNORECASE,
    )
    match = pattern.search(r.text)
    return match.group(1) if match else None


def _jrc_sample_remote_depth(url, lat, lon):
    """Sample point depth and a small neighbourhood from a public JRC GeoTIFF."""
    env_opts = {
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
        "GDAL_HTTP_TIMEOUT": "25",
    }
    with rasterio.Env(**env_opts):
        with rasterio.open(url) as src:
            x, y = lon, lat
            if src.crs and str(src.crs).upper() not in ("EPSG:4326", "OGC:CRS84"):
                xs, ys = rio_transform("EPSG:4326", src.crs, [lon], [lat])
                x, y = xs[0], ys[0]

            point = next(src.sample([(x, y)], indexes=1, masked=True))
            point_value = None
            try:
                if not bool(point.mask[0]):
                    raw = float(point[0])
                    if not math.isnan(raw) and raw >= 0:
                        point_value = raw
            except Exception:
                point_value = None

            nearby_max = None
            nearest_inundated_km = None
            try:
                row, col = src.index(x, y)
                radius_px = 3
                window = Window(
                    col - radius_px,
                    row - radius_px,
                    radius_px * 2 + 1,
                    radius_px * 2 + 1,
                ).intersection(Window(0, 0, src.width, src.height))
                arr = src.read(1, window=window, masked=True)
                vals = arr.compressed()
                nearby_max = float(vals.max()) if vals.size else None
                if nearby_max is not None and (math.isnan(nearby_max) or nearby_max < 0):
                    nearby_max = None

                filled = arr.filled(np.nan)
                flooded = np.argwhere(np.isfinite(filled) & (filled >= 0.1))
                if flooded.size:
                    window_transform = src.window_transform(window)
                    distances = []
                    for rr, cc in flooded:
                        px, py = raster_xy(window_transform, int(rr), int(cc), offset="center")
                        if src.crs and str(src.crs).upper() not in ("EPSG:4326", "OGC:CRS84"):
                            lons, lats = rio_transform(src.crs, "EPSG:4326", [px], [py])
                            plon, plat = lons[0], lats[0]
                        else:
                            plon, plat = px, py
                        distances.append(haversine(lat, lon, float(plat), float(plon)))
                    if distances:
                        nearest_inundated_km = min(distances)
            except Exception:
                nearby_max = nearby_max

            return {
                "point_depth_m": round(point_value, 2) if point_value is not None else None,
                "nearby_max_depth_m": round(nearby_max, 2) if nearby_max is not None else None,
                "nearest_inundated_cell_distance_km": round(nearest_inundated_km, 3) if nearest_inundated_km is not None else None,
                "crs": str(src.crs) if src.crs else None,
                "pixel_size": [abs(src.transform.a), abs(src.transform.e)],
            }


def fetch_jrc_river_flood_context(lat, lon):
    """Screen modelled riverine inundation depth using JRC/CEMS GloFAS v2.1.2."""
    tile_prefix = None
    try:
        tile_prefix = _jrc_tile_prefix(lat, lon)
    except Exception as exc:
        return {
            "status": "error",
            "reason": f"JRC flood tile lookup failed: {str(exc)[:220]}",
            "dataset": "Global river flood hazard maps v2.1.2",
        }

    if not tile_prefix:
        return {
            "status": "not_covered",
            "reason": "No JRC/CEMS flood-hazard tile was resolved for the assessed coordinate.",
            "dataset": "Global river flood hazard maps v2.1.2",
        }

    depths = {}
    errors = {}
    for rp in (10, 20, 50, 75, 100, 200, 500):
        url = f"{JRC_FLOOD_BASE}/RP{rp}/{tile_prefix}_RP{rp}_depth.tif"
        try:
            depths[str(rp)] = {
                **_jrc_sample_remote_depth(url, lat, lon),
                "url": url,
            }
        except Exception as exc:
            errors[str(rp)] = str(exc)[:220]

    if not depths:
        return {
            "status": "error",
            "reason": "JRC/CEMS river-flood rasters could not be sampled for this coordinate.",
            "dataset": "Global river flood hazard maps v2.1.2",
            "errors": errors,
        }

    point_exposed_rps = [
        int(rp) for rp, rec in depths.items()
        if (rec.get("point_depth_m") or 0) >= 0.1
    ]
    nearby_exposed_rps = [
        int(rp) for rp, rec in depths.items()
        if (rec.get("nearby_max_depth_m") or 0) >= 0.1
    ]

    all_values = [
        value
        for rec in depths.values()
        for value in (rec.get("point_depth_m"), rec.get("nearby_max_depth_m"))
        if value is not None
    ]

    return {
        "status": "ok",
        "dataset": "Global river flood hazard maps v2.1.2",
        "publisher": "Copernicus Emergency Management Service / European Commission Joint Research Centre",
        "tile": tile_prefix,
        "resolution": "3 arc-seconds (~90 m)",
        "return_periods_years": sorted(int(rp) for rp in depths),
        "depths": depths,
        "lowest_point_exposure_rp_years": min(point_exposed_rps) if point_exposed_rps else None,
        "lowest_nearby_exposure_rp_years": min(nearby_exposed_rps) if nearby_exposed_rps else None,
        "artifact_caution": bool(all_values and max(all_values) > 10.0),
        "errors": errors,
        "licence": "Free and open Copernicus product / CC BY 4.0 catalogue terms",
        "limitations": (
            "Global modelled riverine flood screening, not an official local flood map. "
            "Point depth is the modelled grid-cell value at the assessed coordinate; the nearby value is a small "
            "approximately 250-300 m neighbourhood screen and must not be treated as on-site inundation. "
            "The dataset does not represent pluvial drainage flooding or parcel/building finished-floor conditions. "
            "Coverage/model artefacts can occur, especially very high depths and areas outside represented river basins."
        ),
    }


def _download_aqueduct4():
    """Download and cache the official WRI Aqueduct 4.0 global data package."""
    root = CACHE_DIR / "aqueduct4"
    root.mkdir(exist_ok=True)
    zip_path = root / "aqueduct-4-0-water-risk-data.zip"
    extract_dir = root / "extracted"
    marker = extract_dir / ".complete"

    if marker.exists():
        return extract_dir

    if not zip_path.exists():
        tmp_path = zip_path.with_suffix(".part")
        headers = {"User-Agent": "TheBrinkWorld/1.0 physical-risk-intelligence"}
        with requests.get(WRI_AQUEDUCT_ZIP, headers=headers, stream=True, timeout=180) as r:
            r.raise_for_status()
            with open(tmp_path, "wb") as out:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        out.write(chunk)
        tmp_path.replace(zip_path)

    if not zipfile.is_zipfile(zip_path):
        raise RuntimeError("WRI Aqueduct download is not a valid ZIP archive.")

    extract_dir.mkdir(exist_ok=True)
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            path = Path(member.filename)
            if path.is_absolute() or ".." in path.parts:
                continue
            zf.extract(member, extract_dir)

    marker.write_text("Aqueduct 4.0 extracted")
    return extract_dir


def _vector_point_row(path, layer, lon, lat):
    """Return the feature covering a WGS84 point without loading the full dataset."""
    info = pyogrio.read_info(path, layer=layer)
    crs = info.get("crs")
    x, y = lon, lat
    if crs and str(crs).upper() not in ("EPSG:4326", "OGC:CRS84"):
        transformer = Transformer.from_crs("EPSG:4326", crs, always_xy=True)
        x, y = transformer.transform(lon, lat)

    pad = 0.001 if not crs or "4326" in str(crs) else 250.0
    frame = pyogrio.read_dataframe(
        path,
        layer=layer,
        bbox=(x - pad, y - pad, x + pad, y + pad),
    )
    if frame.empty:
        return None

    point = Point(x, y)
    matches = frame[frame.geometry.intersects(point)]
    if matches.empty:
        return None
    return matches.iloc[0]


def _aqueduct_assets(root):
    """Find Aqueduct 4.0 baseline/future layers by schema, including the official FileGDB."""
    baseline_spatial = None
    future_spatial = None
    future_csv = None

    vector_files = (
        list(root.rglob("*.gpkg"))
        + list(root.rglob("*.shp"))
        + [p for p in root.rglob("*.gdb") if p.is_dir()]
    )
    # Prefer the canonical Aqueduct layer names when present.
    preferred = {"baseline_annual": 0, "future_annual": 1}

    for path in vector_files:
        try:
            layers = pyogrio.list_layers(path)
        except Exception:
            continue

        ordered_layers = sorted(
            [(str(name), geom) for name, geom in layers],
            key=lambda item: preferred.get(item[0].lower(), 10),
        )
        for layer_name, _geom_type in ordered_layers:
            try:
                info = pyogrio.read_info(path, layer=layer_name)
                fields = set(str(x) for x in info.get("fields", []))
            except Exception:
                continue

            is_baseline = (
                layer_name.lower() == "baseline_annual"
                or {"bws_raw", "bws_score", "bws_label"}.issubset(fields)
            )
            is_future = (
                layer_name.lower() == "future_annual"
                or any(key in fields for key in ("bau30_ws_x_r", "bau50_ws_x_r", "opt30_ws_x_r", "pes30_ws_x_r"))
            )
            if baseline_spatial is None and is_baseline:
                baseline_spatial = (path, layer_name)
            if future_spatial is None and is_future:
                future_spatial = (path, layer_name)

    for path in root.rglob("*.csv"):
        try:
            sample = pd.read_csv(path, nrows=2)
            fields = set(sample.columns)
        except Exception:
            continue
        if any(key in fields for key in ("bau30_ws_x_r", "bau50_ws_x_r", "opt30_ws_x_r", "pes30_ws_x_r")):
            future_csv = path
            break

    return baseline_spatial, future_spatial, future_csv


def _clean_aqueduct_value(value):
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if isinstance(value, (int, float)):
        if float(value) in (-9999.0,):
            return None
        return float(value)
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "no data", "-9999"}:
        return None
    return text


def _aqueduct_arcgis_point(layer_id, lat, lon, out_fields):
    """Resolve the Aqueduct polygon intersecting a point from the public ArcGIS Feature Service."""
    url = f"{WRI_AQUEDUCT_FEATURESERVER}/{layer_id}/query"
    params = {
        "f": "json",
        "where": "1=1",
        "geometry": f"{lon},{lat}",
        "geometryType": "esriGeometryPoint",
        "inSR": "4326",
        "spatialRel": "esriSpatialRelIntersects",
        "outFields": ",".join(out_fields),
        "returnGeometry": "false",
        "resultRecordCount": "1",
    }
    response = requests.get(
        url,
        params=params,
        headers={"User-Agent": "TheBrinkWorld/1.0 physical-risk-intelligence"},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("error"):
        raise RuntimeError(str(payload["error"])[:300])
    features = payload.get("features") or []
    if not features:
        return None
    return (features[0] or {}).get("attributes") or {}


def _aqueduct_from_arcgis(lat, lon):
    baseline_fields = [
        "pfaf_id", "name_0", "name_1",
        "bws_raw", "bws_score", "bws_cat", "bws_label",
        "bwd_raw", "bwd_score", "bwd_cat", "bwd_label",
        "iav_raw", "iav_score", "iav_cat", "iav_label",
        "sev_raw", "sev_score", "sev_cat", "sev_label",
        "drr_raw", "drr_score", "drr_cat", "drr_label",
    ]
    future_fields = ["pfaf_id"]
    for scenario in ("opt", "bau", "pes"):
        for year_code in ("30", "50", "80"):
            for suffix in ("r", "s", "c", "l"):
                future_fields.append(f"{scenario}{year_code}_ws_x_{suffix}")

    baseline_row = _aqueduct_arcgis_point(1, lat, lon, baseline_fields)
    if not baseline_row:
        return None

    future_row = _aqueduct_arcgis_point(0, lat, lon, future_fields)

    def val(row, key):
        if not row:
            return None
        return _clean_aqueduct_value(row.get(key))

    baseline = {
        "pfaf_id": val(baseline_row, "pfaf_id"),
        "name_0": val(baseline_row, "name_0"),
        "name_1": val(baseline_row, "name_1"),
        "water_stress_raw": val(baseline_row, "bws_raw"),
        "water_stress_score": val(baseline_row, "bws_score"),
        "water_stress_label": val(baseline_row, "bws_label"),
        "water_stress_category": val(baseline_row, "bws_cat"),
        "water_depletion_raw": val(baseline_row, "bwd_raw"),
        "water_depletion_score": val(baseline_row, "bwd_score"),
        "water_depletion_label": val(baseline_row, "bwd_label"),
        "interannual_variability_raw": val(baseline_row, "iav_raw"),
        "interannual_variability_label": val(baseline_row, "iav_label"),
        "seasonal_variability_raw": val(baseline_row, "sev_raw"),
        "seasonal_variability_label": val(baseline_row, "sev_label"),
        "drought_risk_raw": val(baseline_row, "drr_raw"),
        "drought_risk_score": val(baseline_row, "drr_score"),
        "drought_risk_label": val(baseline_row, "drr_label"),
    }

    future = {}
    if future_row:
        for scenario in ("opt", "bau", "pes"):
            scenario_name = {
                "opt": "optimistic",
                "bau": "business_as_usual",
                "pes": "pessimistic",
            }[scenario]
            future[scenario_name] = {}
            for year_code, year in (("30", 2030), ("50", 2050), ("80", 2080)):
                prefix = f"{scenario}{year_code}_ws_x_"
                future[scenario_name][str(year)] = {
                    "raw": val(future_row, prefix + "r"),
                    "score": val(future_row, prefix + "s"),
                    "label": val(future_row, prefix + "l"),
                    "category": val(future_row, prefix + "c"),
                }

    return baseline, future


def fetch_aqueduct_water_risk_context(lat, lon):
    """Site-level basin screening using WRI Aqueduct 4.0 baseline and future data."""
    baseline = None
    future = {}
    access_note = None

    # Primary path: WRI/ArcGIS-hosted public feature service. This avoids
    # downloading the full global geodatabase for each ephemeral report runner.
    try:
        resolved = _aqueduct_from_arcgis(lat, lon)
        if resolved:
            baseline, future = resolved
            access_note = "WRI Aqueduct 4.0 public ArcGIS Feature Service"
    except Exception as arc_exc:
        access_note = f"ArcGIS lookup unavailable: {str(arc_exc)[:180]}"

    # Fallback: official WRI global download package.
    if baseline is None:
        try:
            root = _download_aqueduct4()
            baseline_asset, future_asset, future_csv = _aqueduct_assets(root)
            if not baseline_asset:
                raise RuntimeError("baseline annual spatial layer was not identified")

            baseline_row = _vector_point_row(
                str(baseline_asset[0]),
                baseline_asset[1],
                lon,
                lat,
            )
            if baseline_row is None:
                return {
                    "status": "not_covered",
                    "reason": "Aqueduct 4.0 did not resolve a baseline annual feature at the assessed coordinate.",
                    "dataset": "Aqueduct 4.0 Current and Future Global Maps Data",
                }

            def field(row, name):
                return _clean_aqueduct_value(row.get(name)) if row is not None and name in row.index else None

            baseline = {
                "pfaf_id": field(baseline_row, "pfaf_id"),
                "name_0": field(baseline_row, "name_0"),
                "name_1": field(baseline_row, "name_1"),
                "water_stress_raw": field(baseline_row, "bws_raw"),
                "water_stress_score": field(baseline_row, "bws_score"),
                "water_stress_label": field(baseline_row, "bws_label"),
                "water_stress_category": field(baseline_row, "bws_cat"),
                "water_depletion_raw": field(baseline_row, "bwd_raw"),
                "water_depletion_score": field(baseline_row, "bwd_score"),
                "water_depletion_label": field(baseline_row, "bwd_label"),
                "interannual_variability_raw": field(baseline_row, "iav_raw"),
                "interannual_variability_label": field(baseline_row, "iav_label"),
                "seasonal_variability_raw": field(baseline_row, "sev_raw"),
                "seasonal_variability_label": field(baseline_row, "sev_label"),
                "drought_risk_raw": field(baseline_row, "drr_raw"),
                "drought_risk_score": field(baseline_row, "drr_score"),
                "drought_risk_label": field(baseline_row, "drr_label"),
            }

            future_row = None
            if future_asset:
                try:
                    future_row = _vector_point_row(
                        str(future_asset[0]),
                        future_asset[1],
                        lon,
                        lat,
                    )
                except Exception:
                    future_row = None

            if future_row is None and future_csv and baseline.get("pfaf_id") is not None:
                try:
                    table = pd.read_csv(future_csv)
                    pfaf_numeric = pd.to_numeric(table.get("pfaf_id"), errors="coerce")
                    target = float(baseline["pfaf_id"])
                    rows = table[pfaf_numeric == target]
                    if not rows.empty:
                        future_row = rows.iloc[0]
                except Exception:
                    future_row = None

            if future_row is not None:
                for scenario in ("opt", "bau", "pes"):
                    scenario_name = {
                        "opt": "optimistic",
                        "bau": "business_as_usual",
                        "pes": "pessimistic",
                    }[scenario]
                    future[scenario_name] = {}
                    for year_code, year in (("30", 2030), ("50", 2050), ("80", 2080)):
                        prefix = f"{scenario}{year_code}_ws_x_"
                        future[scenario_name][str(year)] = {
                            "raw": field(future_row, prefix + "r"),
                            "score": field(future_row, prefix + "s"),
                            "label": field(future_row, prefix + "l"),
                            "category": field(future_row, prefix + "c"),
                        }
            access_note = "WRI Aqueduct 4.0 official download package"
        except Exception as exc:
            return {
                "status": "error",
                "reason": (
                    "WRI Aqueduct 4.0 could not be resolved through either the public feature service "
                    f"or official download package: {str(exc)[:220]}"
                ),
                "dataset": "Aqueduct 4.0 Current and Future Global Maps Data",
            }

    return {
        "status": "ok",
        "dataset": "Aqueduct 4.0 Current and Future Global Maps Data",
        "publisher": "World Resources Institute",
        "access_path": access_note,
        "baseline": baseline,
        "future_water_stress": future,
        "future_scenarios": {
            "optimistic": "SSP1 / RCP2.6",
            "business_as_usual": "SSP3 / RCP7.0",
            "pessimistic": "SSP5 / RCP8.5",
        },
        "future_periods": {
            "2030": "2015-2045",
            "2050": "2035-2065",
            "2080": "2065-2095",
        },
        "licence": "CC BY 4.0",
        "citation": (
            "Kuzma, S. et al. (2023), Aqueduct 4.0: Updated decision-relevant global water risk indicators, "
            "World Resources Institute."
        ),
        "limitations": (
            "Aqueduct is a global basin-level prioritization and screening framework. "
            "It does not replace local water-supply, utility, hydrogeological or drought-resilience studies. "
            "WRI notes that important water-management and governance elements are only partially represented "
            "and recommends local/regional deep dives for decisions requiring greater precision."
        ),
    }


def _copernicus_dem_tile_name(lat, lon):
    """Resolve the public Copernicus DEM GLO-30 1° tile containing a coordinate."""
    south = math.floor(lat)
    west = math.floor(lon)

    ns = "N" if south >= 0 else "S"
    ew = "E" if west >= 0 else "W"
    lat_label = f"{ns}{abs(int(south)):02d}_00"
    lon_label = f"{ew}{abs(int(west)):03d}_00"
    return f"Copernicus_DSM_COG_10_{lat_label}_{lon_label}_DEM"


def _terrain_window_metrics(src, x, y, radius_px, lat):
    row, col = src.index(x, y)
    full = Window(0, 0, src.width, src.height)
    requested = Window(
        col - radius_px,
        row - radius_px,
        radius_px * 2 + 1,
        radius_px * 2 + 1,
    )
    try:
        window = requested.intersection(full)
    except Exception:
        window = full.intersection(requested)

    arr = src.read(1, window=window, masked=True).astype("float64")
    if arr.count() < 9:
        return None

    data = arr.filled(np.nan)
    finite = np.isfinite(data)
    if finite.sum() < 9:
        return None

    # Copernicus GLO-30 is geographic. Derive local metre spacing from transform.
    transform = src.window_transform(window)
    dx_deg = abs(transform.a)
    dy_deg = abs(transform.e)
    dy_m = max(1.0, dy_deg * 111320.0)
    dx_m = max(1.0, dx_deg * 111320.0 * math.cos(math.radians(lat)))

    # Fill isolated nodata with local median only for stable gradient calculation.
    median = float(np.nanmedian(data))
    work = np.where(finite, data, median)
    dz_dy, dz_dx = np.gradient(work, dy_m, dx_m)
    slope = np.degrees(np.arctan(np.sqrt(dz_dx ** 2 + dz_dy ** 2)))

    return {
        "elevation_mean_m": round(float(np.nanmean(data)), 1),
        "elevation_min_m": round(float(np.nanmin(data)), 1),
        "elevation_max_m": round(float(np.nanmax(data)), 1),
        "elevation_std_m": round(float(np.nanstd(data)), 1),
        "relief_m": round(float(np.nanmax(data) - np.nanmin(data)), 1),
        "slope_mean_deg": round(float(np.nanmean(slope)), 1),
        "slope_p95_deg": round(float(np.nanpercentile(slope, 95)), 1),
        "slope_max_deg": round(float(np.nanmax(slope)), 1),
        "valid_pixels": int(finite.sum()),
    }


def fetch_terrain_context(lat, lon):
    """Terrain/elevation screening from Copernicus DEM GLO-30 public COG tiles."""
    tile = _copernicus_dem_tile_name(lat, lon)
    url = f"{COPERNICUS_DEM_30M_BASE}/{tile}/{tile}.tif"

    env_opts = {
        "GDAL_DISABLE_READDIR_ON_OPEN": "EMPTY_DIR",
        "CPL_VSIL_CURL_ALLOWED_EXTENSIONS": ".tif",
        "GDAL_HTTP_TIMEOUT": "30",
    }

    try:
        with rasterio.Env(**env_opts):
            with rasterio.open(url) as src:
                x, y = lon, lat
                if src.crs and str(src.crs).upper() not in ("EPSG:4326", "OGC:CRS84"):
                    xs, ys = rio_transform("EPSG:4326", src.crs, [lon], [lat])
                    x, y = xs[0], ys[0]

                point = next(src.sample([(x, y)], indexes=1, masked=True))
                point_elevation = None
                try:
                    if not bool(point.mask[0]):
                        raw = float(point[0])
                        if math.isfinite(raw):
                            point_elevation = raw
                except Exception:
                    point_elevation = None

                # ~250 m and ~1 km radius neighbourhoods at ~30 m pixels.
                metrics_250m = _terrain_window_metrics(src, x, y, 9, lat)
                metrics_1km = _terrain_window_metrics(src, x, y, 34, lat)

                return {
                    "status": "ok",
                    "dataset": "Copernicus DEM GLO-30 Public",
                    "publisher": "Copernicus Programme / AWS Open Data",
                    "release": "2021",
                    "tile": tile,
                    "url": url,
                    "point_elevation_m": round(point_elevation, 1) if point_elevation is not None else ((metrics_250m or {}).get("elevation_mean_m")),
                    "metrics_250m": metrics_250m,
                    "metrics_1km": metrics_1km,
                    "resolution": "1 arc-second (~30 m)",
                    "surface_model_note": "Digital Surface Model including terrain plus above-ground features such as vegetation and buildings.",
                    "limitations": (
                        "Copernicus DEM GLO-30 is a digital surface model, not a survey-grade bare-earth terrain model. "
                        "Slope and relief are screening metrics derived by The Brink World from the DEM neighbourhood. "
                        "Buildings, vegetation, voids and local artefacts can influence values. These metrics do not by themselves "
                        "establish landslide susceptibility, geotechnical stability or access-route failure."
                    ),
                }
    except Exception as exc:
        return {
            "status": "error",
            "reason": f"Copernicus DEM terrain retrieval failed: {str(exc)[:260]}",
            "dataset": "Copernicus DEM GLO-30 Public",
            "tile": tile,
            "url": url,
        }


def _download_ibtracs_since1980():
    """Download/cache NOAA IBTrACS v04r01 modern-era CSV."""
    root = CACHE_DIR / "ibtracs"
    root.mkdir(exist_ok=True)
    csv_path = root / "ibtracs.since1980.list.v04r01.csv"

    if csv_path.exists() and csv_path.stat().st_size > 100000:
        return csv_path

    tmp_path = csv_path.with_suffix(".part")
    headers = {"User-Agent": "TheBrinkWorld/1.0 physical-risk-intelligence"}
    with requests.get(IBTRACS_SINCE1980_CSV, headers=headers, stream=True, timeout=180) as r:
        r.raise_for_status()
        with open(tmp_path, "wb") as out:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    out.write(chunk)
    tmp_path.replace(csv_path)
    return csv_path


def _safe_float(value):
    try:
        if value is None or pd.isna(value):
            return None
        text = str(value).strip()
        if not text:
            return None
        val = float(text)
        return val if math.isfinite(val) else None
    except Exception:
        return None


def fetch_cyclone_history_context(lat, lon):
    """Historical tropical-cyclone proximity/intensity context from NOAA IBTrACS since 1980."""
    try:
        csv_path = _download_ibtracs_since1980()
        usecols = [
            "SID", "SEASON", "BASIN", "SUBBASIN", "NAME", "ISO_TIME",
            "NATURE", "LAT", "LON", "WMO_WIND", "WMO_PRES",
        ]
        frame = pd.read_csv(
            csv_path,
            skiprows=[1],
            usecols=lambda c: c in usecols,
            low_memory=False,
        )
    except Exception as exc:
        return {
            "status": "error",
            "reason": f"NOAA IBTrACS retrieval/read failed: {str(exc)[:280]}",
            "dataset": "IBTrACS v04r01 since1980",
        }

    required = {"SID", "LAT", "LON"}
    if not required.issubset(frame.columns):
        return {
            "status": "error",
            "reason": "IBTrACS CSV did not contain the required storm identifier and coordinate columns.",
            "dataset": "IBTrACS v04r01 since1980",
        }

    # Coarse bounding-box filter first, then great-circle distance.
    lat_span = 6.0
    lon_span = 8.0 / max(0.25, math.cos(math.radians(lat)))
    lats = pd.to_numeric(frame["LAT"], errors="coerce")
    lons = pd.to_numeric(frame["LON"], errors="coerce")

    # Handle dateline by calculating a wrapped longitude delta.
    lon_delta = ((lons - lon + 180.0) % 360.0) - 180.0
    mask = (
        lats.between(lat - lat_span, lat + lat_span)
        & lon_delta.between(-lon_span, lon_span)
    )
    subset = frame.loc[mask].copy()

    if subset.empty:
        return {
            "status": "ok",
            "dataset": "IBTrACS v04r01 since1980",
            "publisher": "NOAA National Centers for Environmental Information",
            "period": "1980-present",
            "storm_count_within_100km": 0,
            "storm_count_within_250km": 0,
            "storm_count_within_500km": 0,
            "nearest_storm": None,
            "recent_storms": [],
            "max_reported_wmo_wind_within_250km_kt": None,
            "max_reported_wmo_wind_storm": None,
            "doi": "10.25921/82ty-9e16",
            "limitations": (
                "IBTrACS is a historical tropical-cyclone best-track archive. Absence of a track near a site since 1980 "
                "does not prove future cyclone absence and does not characterize non-tropical severe wind. Track-point "
                "wind values are storm intensity observations, not site wind speeds or structural design loads."
            ),
        }

    distances = []
    for idx, row in subset.iterrows():
        rlat = _safe_float(row.get("LAT"))
        rlon = _safe_float(row.get("LON"))
        if rlat is None or rlon is None:
            distances.append(None)
            continue
        distances.append(haversine(lat, lon, rlat, rlon))
    subset["_distance_km"] = distances
    subset = subset[pd.notna(subset["_distance_km"])].copy()

    if subset.empty:
        return {
            "status": "error",
            "reason": "IBTrACS candidate tracks could not be distance-resolved.",
            "dataset": "IBTrACS v04r01 since1980",
        }

    storm_summaries = []
    for sid, group in subset.groupby("SID", dropna=True):
        group = group.sort_values("_distance_km")
        nearest = group.iloc[0]
        min_dist = float(nearest["_distance_km"])

        nearby = group[group["_distance_km"] <= 500.0].copy()
        if nearby.empty:
            continue

        winds = pd.to_numeric(nearby.get("WMO_WIND"), errors="coerce") if "WMO_WIND" in nearby else pd.Series(dtype=float)
        max_wind = float(winds.max()) if not winds.empty and pd.notna(winds.max()) else None

        times = pd.to_datetime(nearby.get("ISO_TIME"), errors="coerce", utc=True) if "ISO_TIME" in nearby else pd.Series(dtype="datetime64[ns, UTC]")
        last_time = times.max() if not times.empty else None

        storm_summaries.append({
            "sid": str(sid),
            "season": int(nearest["SEASON"]) if "SEASON" in nearest and pd.notna(nearest["SEASON"]) else None,
            "name": str(nearest.get("NAME") or "").strip() or "Unnamed tropical cyclone",
            "basin": str(nearest.get("BASIN") or "").strip() or None,
            "subbasin": str(nearest.get("SUBBASIN") or "").strip() or None,
            "nearest_distance_km": round(min_dist, 1),
            "nearest_time": str(nearest.get("ISO_TIME") or "").strip() or None,
            "max_wmo_wind_within_500km_kt": round(max_wind, 1) if max_wind is not None else None,
            "last_time_within_500km": last_time.isoformat() if last_time is not None and pd.notna(last_time) else None,
        })

    storm_summaries.sort(key=lambda x: x["nearest_distance_km"])
    nearest_storm = storm_summaries[0] if storm_summaries else None

    counts = {}
    for radius in (100, 250, 500):
        counts[radius] = sum(1 for storm in storm_summaries if storm["nearest_distance_km"] <= radius)

    within250 = subset[subset["_distance_km"] <= 250.0].copy()
    max_wind = None
    max_wind_storm = None
    if not within250.empty and "WMO_WIND" in within250.columns:
        within250["_wmo_wind"] = pd.to_numeric(within250["WMO_WIND"], errors="coerce")
        usable = within250[pd.notna(within250["_wmo_wind"])]
        if not usable.empty:
            peak = usable.sort_values("_wmo_wind", ascending=False).iloc[0]
            max_wind = float(peak["_wmo_wind"])
            max_wind_storm = {
                "sid": str(peak.get("SID") or ""),
                "name": str(peak.get("NAME") or "").strip() or "Unnamed tropical cyclone",
                "time": str(peak.get("ISO_TIME") or "").strip() or None,
                "distance_km": round(float(peak["_distance_km"]), 1),
                "wmo_wind_kt": round(max_wind, 1),
                "basin": str(peak.get("BASIN") or "").strip() or None,
            }

    recent = sorted(
        storm_summaries,
        key=lambda x: x.get("last_time_within_500km") or "",
        reverse=True,
    )[:10]

    return {
        "status": "ok",
        "dataset": "IBTrACS v04r01 since1980",
        "publisher": "NOAA National Centers for Environmental Information",
        "period": "1980-present",
        "storm_count_within_100km": counts[100],
        "storm_count_within_250km": counts[250],
        "storm_count_within_500km": counts[500],
        "nearest_storm": nearest_storm,
        "recent_storms": recent,
        "max_reported_wmo_wind_within_250km_kt": round(max_wind, 1) if max_wind is not None else None,
        "max_reported_wmo_wind_storm": max_wind_storm,
        "doi": "10.25921/82ty-9e16",
        "limitations": (
            "IBTrACS is a historical tropical-cyclone best-track archive. The since-1980 subset is used as the modern "
            "satellite-era screening baseline. Historical track proximity does not predict future occurrence. WMO wind "
            "values can use different agency averaging periods and are not adjusted by IBTrACS; they describe storm "
            "intensity at a reported track point, not the wind speed experienced at the facility. This module does not "
            "characterize non-tropical severe wind, convective gusts or structural design wind loads."
        ),
    }


def fetch_firms_fire_context(lat, lon):
    """Operational thermal-anomaly/fire context from NASA FIRMS VIIRS NOAA-20/21 NRT."""
    if not NASA_FIRMS_MAP_KEY:
        return {
            "status": "not_configured",
            "reason": "NASA_FIRMS_MAP_KEY is not configured.",
            "dataset": "NASA FIRMS VIIRS NOAA-20/21 NRT",
            "window_days": 5,
        }

    # Small regional box around the facility; distance thresholds are calculated after retrieval.
    lat_pad = 1.6
    lon_pad = 1.6 / max(0.25, math.cos(math.radians(lat)))
    west = max(-180.0, lon - lon_pad)
    east = min(180.0, lon + lon_pad)
    south = max(-90.0, lat - lat_pad)
    north = min(90.0, lat + lat_pad)
    area = f"{west},{south},{east},{north}"

    detections = []
    errors = {}
    for source in ("VIIRS_NOAA20_NRT", "VIIRS_NOAA21_NRT"):
        url = (
            "https://firms.modaps.eosdis.nasa.gov/api/area/csv/"
            f"{NASA_FIRMS_MAP_KEY}/{source}/{area}/5"
        )
        try:
            r = requests.get(url, timeout=45, headers={"User-Agent": "TheBrinkWorld/1.0 physical-risk-intelligence"})
            if r.status_code != 200:
                errors[source] = f"HTTP {r.status_code}"
                continue
            lines = r.text.strip().splitlines()
            if len(lines) < 2:
                continue
            reader = pd.read_csv(io.StringIO(r.text))
            for _, row in reader.iterrows():
                dlat = _safe_float(row.get("latitude"))
                dlon = _safe_float(row.get("longitude"))
                if dlat is None or dlon is None:
                    continue
                distance = haversine(lat, lon, dlat, dlon)
                if distance > 150.0:
                    continue
                frp = _safe_float(row.get("frp"))
                confidence = str(row.get("confidence") or "").strip() or None
                date = str(row.get("acq_date") or "").strip() or None
                time_txt = str(row.get("acq_time") or "").strip()
                detections.append({
                    "source": source,
                    "latitude": dlat,
                    "longitude": dlon,
                    "distance_km": round(distance, 1),
                    "acq_date": date,
                    "acq_time": time_txt or None,
                    "confidence": confidence,
                    "frp_mw": round(frp, 1) if frp is not None else None,
                    "daynight": str(row.get("daynight") or "").strip() or None,
                })
        except Exception as exc:
            errors[source] = str(exc)[:220]

    # Deduplicate the same approximate thermal anomaly across two VIIRS feeds.
    seen = set()
    unique = []
    for rec in sorted(detections, key=lambda x: (x.get("acq_date") or "", x.get("acq_time") or "", x["distance_km"])):
        key = (
            rec.get("acq_date"),
            rec.get("acq_time"),
            round(rec["latitude"], 3),
            round(rec["longitude"], 3),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(rec)

    nearest = min(unique, key=lambda x: x["distance_km"]) if unique else None
    peak_frp = None
    if unique:
        with_frp = [x for x in unique if x.get("frp_mw") is not None]
        if with_frp:
            peak_frp = max(with_frp, key=lambda x: x["frp_mw"])

    counts = {
        radius: sum(1 for x in unique if x["distance_km"] <= radius)
        for radius in (5, 10, 25, 50, 100)
    }
    days = sorted({x.get("acq_date") for x in unique if x.get("acq_date")})

    status = "ok" if unique or len(errors) < 2 else "error"
    return {
        "status": status,
        "dataset": "NASA FIRMS VIIRS NOAA-20/21 NRT",
        "publisher": "NASA LANCE FIRMS",
        "window_days": 5,
        "detection_count_within_5km": counts[5],
        "detection_count_within_10km": counts[10],
        "detection_count_within_25km": counts[25],
        "detection_count_within_50km": counts[50],
        "detection_count_within_100km": counts[100],
        "detection_days": days,
        "nearest_detection": nearest,
        "peak_frp_detection": peak_frp,
        "detections": unique[:100],
        "errors": errors,
        "source_reference": "https://firms.modaps.eosdis.nasa.gov/api/area/",
        "limitations": (
            "FIRMS reports satellite-detected thermal anomalies/hotspots, not verified wildfire perimeters. "
            "A detection may reflect vegetation fire or another heat source, and cloud/smoke/satellite overpass timing can "
            "cause missed detections. Distance is straight-line proximity. This five-day operational screen does not "
            "characterize long-term wildfire susceptibility, fuel, burn probability, flame length or structure vulnerability."
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

    # USGS: primary operational seismic window = last 24h, M1.0+, within 350 km.
    usgs_url = "https://earthquake.usgs.gov/fdsnws/event/1/query"
    quakes_24h_data = _cached_get(usgs_url, {
        "format": "geojson",
        "latitude": lat,
        "longitude": lon,
        "maxradiuskm": 350,
        "minmagnitude": 1.0,
        "starttime": (now - timedelta(hours=24)).isoformat().replace("+00:00", "Z"),
        "endtime": now.isoformat().replace("+00:00", "Z"),
        "orderby": "time",
        "limit": 1000
    }, ttl_seconds=600)

    quakes_30d_data = _cached_get(usgs_url, {
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

    recent_events_24h = []
    for feature in quakes_24h_data.get("features", []):
        props = feature.get("properties", {})
        coords = feature.get("geometry", {}).get("coordinates", [])
        if len(coords) < 3:
            continue
        try:
            dist = haversine(lat, lon, float(coords[1]), float(coords[0]))
            mag = float(props.get("mag"))
        except Exception:
            continue
        recent_events_24h.append({
            "place": props.get("place", "Regional event"),
            "mag": round(mag, 1),
            "depth_km": round(float(coords[2]), 1),
            "distance_km": round(dist),
            "observed_at": _iso_from_ms(props.get("time") or 0),
            "source": "USGS"
        })

    recent_events_24h.sort(key=lambda q: q.get("observed_at") or "", reverse=True)

    # MET Norway Locationforecast: global modelled current/short-range weather.
    weather = fetch_met_no_weather(lat, lon)
    current = weather.get("current") or {}
    days = weather.get("days") or []

    live = fetch_live_hazards(lat, lon)

    # For India-facing reports, augment the USGS 24-hour view with NCS India
    # events already present in the operational hazard layer. Prefer NCS when
    # the same earthquake is represented by both sources.
    ncs_events_24h = []
    cutoff_24h = now - timedelta(hours=24)
    for hazard in live:
        if "earth" not in str(hazard.get("category") or "").lower():
            continue
        if "ncs" not in str(hazard.get("source") or "").lower():
            continue
        try:
            observed = datetime.fromisoformat(str(hazard.get("observed_at") or "").replace("Z", "+00:00"))
            magnitude = float(hazard.get("magnitude"))
            distance = float(hazard.get("distance_km"))
        except Exception:
            continue
        if observed < cutoff_24h or distance > 350 or magnitude < 1.0:
            continue
        ncs_events_24h.append({
            "place": hazard.get("name") or "India region earthquake",
            "mag": round(magnitude, 1),
            "depth_km": round(float(hazard.get("depth_km") or 0), 1),
            "distance_km": round(distance),
            "observed_at": observed.isoformat(),
            "source": "NCS India",
        })

    combined_quakes_24h = list(recent_events_24h)
    for ncs in ncs_events_24h:
        try:
            ncs_time = datetime.fromisoformat(str(ncs.get("observed_at")).replace("Z", "+00:00"))
        except Exception:
            ncs_time = None
        match_index = None
        for idx, usgs in enumerate(combined_quakes_24h):
            if usgs.get("source") != "USGS":
                continue
            try:
                usgs_time = datetime.fromisoformat(str(usgs.get("observed_at")).replace("Z", "+00:00"))
                close_time = ncs_time is not None and abs((usgs_time - ncs_time).total_seconds()) <= 600
                close_mag = abs(float(usgs.get("mag") or 0) - float(ncs.get("mag") or 0)) <= 0.3
                close_distance = abs(float(usgs.get("distance_km") or 0) - float(ncs.get("distance_km") or 0)) <= 35
            except Exception:
                continue
            if close_time and close_mag and close_distance:
                match_index = idx
                break
        if match_index is not None:
            combined_quakes_24h[match_index] = ncs
        else:
            combined_quakes_24h.append(ncs)

    combined_quakes_24h.sort(key=lambda q: q.get("observed_at") or "", reverse=True)

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
    historical_rainfall = fetch_historical_rainfall_context(lat, lon)
    river_flood = fetch_jrc_river_flood_context(lat, lon)
    water_risk = fetch_aqueduct_water_risk_context(lat, lon)
    terrain = fetch_terrain_context(lat, lon)
    cyclone_history = fetch_cyclone_history_context(lat, lon)
    fire_context = fetch_firms_fire_context(lat, lon)
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
        "elevation_m": terrain.get("point_elevation_m"),
        "timezone": weather.get("timezone") or "UTC",
        "country": context.get("country"),
        "country_code": context.get("country_code"),
        "weather_current": {
            "temperature_c": current.get("temperature_c"),
            "precipitation_mm": current.get("precipitation_mm"),
            "precipitation_period_hours": current.get("precipitation_period_hours"),
            "wind_kmh": current.get("wind_kmh"),
        },
        "forecast_days": days,
        "forecast_summary": wx_summary,
        "historical_heat": historical_heat,
        "historical_rainfall": historical_rainfall,
        "river_flood": river_flood,
        "water_risk": water_risk,
        "terrain": terrain,
        "cyclone_history": cyclone_history,
        "fire_context": fire_context,
        "recent_quakes": combined_quakes_24h,
        "recent_quakes_24h": combined_quakes_24h,
        "quake_count_24h_350km_m1": len(combined_quakes_24h),
        "quake_count_30d_350km": len(quakes_30d_data.get("features", [])),
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
                "note": "Primary operational seismic table: catalogued M1.0+ events within 350 km in the latest 24 hours, using NCS India where available for India-facing events and USGS as a global source. A separate USGS 30-day M2.5+ count is retained for broader context."
            },
            {
                "name": "MET Norway Locationforecast 2.0",
                "type": "Modelled / forecast",
                "note": "Global short-range point forecast used for current/near-term temperature, precipitation and wind context. Data are provided under CC BY 4.0; daily summaries are derived by The Brink World."
            } if weather.get("status") == "ok" else None,
            {
                "name": "Copernicus Climate Change Service (C3S) — ERA5-Land",
                "type": "Reanalysis / historical climate",
                "note": "Historical heat baseline derived by The Brink World from ERA5-Land hourly point time-series, aggregated to daily maxima/minima; CC-BY-4.0; DOI 10.24381/ee82e357."
            } if historical_heat.get("status") == "ok" else None,
            {
                "name": "Copernicus Climate Change Service (C3S) — ERA5-Land precipitation",
                "type": "Reanalysis / historical precipitation",
                "note": "Historical rainfall baseline derived from ERA5-Land hourly point time-series; CC-BY-4.0; DOI 10.24381/ee82e357. This is rainfall evidence, not a flood-depth map."
            } if historical_rainfall.get("status") == "ok" else None,
            {
                "name": "JRC/CEMS GloFAS Global river flood hazard maps v2.1.2",
                "type": "Modelled riverine flood inundation",
                "note": "Global riverine flood water-depth screening at approximately 90 m for multiple return periods. Not an official local flood map; does not represent pluvial drainage flooding."
            } if river_flood.get("status") == "ok" else None,
            {
                "name": "WRI Aqueduct 4.0",
                "type": "Basin-level baseline and future water-risk screening",
                "note": "Baseline water stress, depletion, variability and drought-risk indicators plus CMIP6-based future water-stress projections for 2030, 2050 and 2080. CC BY 4.0; use as a prioritization tool with local verification."
            } if water_risk.get("status") == "ok" else None,
            {
                "name": "Copernicus DEM GLO-30 Public",
                "type": "Digital surface model / terrain screening",
                "note": "Public ~30 m Copernicus DEM 2021 COG used to derive point elevation, local slope and relief metrics. DSM values may include buildings and vegetation; not a geotechnical assessment."
            } if terrain.get("status") == "ok" else None,
            {
                "name": "NOAA NCEI — IBTrACS v04r01",
                "type": "Historical tropical-cyclone best-track archive",
                "note": "Modern satellite-era (since 1980) storm-track proximity and reported WMO storm intensity context. Track-point winds are not site wind speeds or structural design loads; DOI 10.25921/82ty-9e16."
            } if cyclone_history.get("status") == "ok" else None,
            {
                "name": "NASA LANCE FIRMS — VIIRS NOAA-20/21 NRT",
                "type": "Operational satellite thermal-anomaly detections",
                "note": "Five-day nearby thermal-anomaly screen from VIIRS NOAA-20 and NOAA-21. Detections are not verified wildfire perimeters and do not establish long-term wildfire susceptibility."
            } if fire_context.get("status") == "ok" else None,
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
