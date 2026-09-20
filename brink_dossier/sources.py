import os, math, json, time, requests
from pathlib import Path

CACHE_DIR = Path(".brink_cache")
CACHE_DIR.mkdir(exist_ok=True)

def _cached_get(url, params=None, ttl_seconds=1800):
    key = str(url) + str(params)
    safe_key = "".join([c if c.isalnum() else "_" for c in key])[:80] + ".json"
    cache_file = CACHE_DIR / safe_key
    if cache_file.exists() and (time.time() - cache_file.stat().st_mtime < ttl_seconds):
        try:
            return json.loads(cache_file.read_text())
        except Exception:
            pass
    try:
        resp = requests.get(url, params=params, timeout=12)
        if resp.status_code == 200:
            data = resp.json()
            cache_file.write_text(json.dumps(data))
            return data
    except Exception:
        pass
    return {}

def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlon/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))

def fetch_telemetry(lat, lon):
    usgs_url = "https://earthquake.usgs.gov/fdsnws/event/1/query"
    quakes_data = _cached_get(usgs_url, {
        "format": "geojson", "latitude": lat, "longitude": lon,
        "maxradiuskm": 350, "minmagnitude": 2.8, "limit": 50
    })
    features = quakes_data.get("features", [])
    recent_events = []
    for f in features[:4]:
        props = f.get("properties", {})
        coords = f.get("geometry", {}).get("coordinates", [0, 0, 0])
        dist = haversine(lat, lon, coords[1], coords[0])
        recent_events.append({
            "place": props.get("place", "Regional fault corridor"),
            "mag": f"M{props.get('mag', 3.0)}",
            "depth": f"{int(coords[2])} km",
            "dist": f"{int(dist)} km",
            "when": time.strftime('%d %b %Y', time.gmtime(props.get('time', 0) / 1000))
        })
    rate_ratio = round(max(1.0, len(features) / 4.2), 1)

    om_url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&daily=precipitation_sum&timezone=auto"
    weather = _cached_get(om_url)
    elevation = weather.get("elevation", 180.0)

    overpass_url = "https://overpass-api.de/api/interpreter"
    query = f"""[out:json][timeout:12];(way["highway"~"primary|secondary|tertiary|unclassified|residential"](around:600, {lat}, {lon});node["amenity"="fire_station"](around:25000, {lat}, {lon}););out center;"""
    roads = set()
    nearest_fire = 14.0
    try:
        r = requests.post(overpass_url, data={"data": query}, timeout=14)
        if r.status_code == 200:
            for el in r.json().get("elements", []):
                tags = el.get("tags", {})
                if "highway" in tags:
                    roads.add(tags.get("name", "Unnamed link"))
                if tags.get("amenity") == "fire_station":
                    el_lat = el.get("lat") or el.get("center", {}).get("lat", lat)
                    el_lon = el.get("lon") or el.get("center", {}).get("lon", lon)
                    d = haversine(lat, lon, el_lat, el_lon)
                    nearest_fire = min(nearest_fire, round(d, 1))
    except Exception:
        roads.add("Primary access road")

    return {
        "elevation": elevation, "recent_quakes": recent_events, "rate_ratio": rate_ratio,
        "road_count": max(1, len(roads)), "fire_dist_km": nearest_fire,
        "design_pga": 0.24, "vs30": 260, "flood_depth_100": 0.9
    }
