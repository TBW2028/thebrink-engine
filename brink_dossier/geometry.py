import re
import requests

def preview_location(input_text):
    raw = str(input_text).strip()
    
    # 1. Resolve short Google Maps URLs
    if "maps.app.goo.gl" in raw or "goo.gl" in raw:
        try:
            resp = requests.head(raw, allow_redirects=True, timeout=6)
            raw = resp.url
        except Exception:
            pass

    # 2. Extract coordinates from Google Maps full URL pins
    pin_match = re.search(r"!3d([-+]?\d{1,2}\.\d+)!4d([-+]?\d{1,3}\.\d+)", raw)
    if pin_match:
        return {"ok": True, "lat": float(pin_match.group(1)), "lon": float(pin_match.group(2)), "warning": None}

    # 3. Extract coordinates from Google Maps camera center (@lat,lon)
    center_match = re.search(r"@([-+]?\d{1,2}\.\d+),([-+]?\d{1,3}\.\d+)", raw)
    if center_match:
        return {"ok": True, "lat": float(center_match.group(1)), "lon": float(center_match.group(2)), "warning": "Pinned using camera center"}

    # 4. Extract raw latitude / longitude pairs (e.g. "18.5204, 73.8567")
    coord_match = re.search(r"([-+]?\d{1,2}\.\d+)[,\s]+([-+]?\d{1,3}\.\d+)", raw)
    if coord_match:
        return {"ok": True, "lat": float(coord_match.group(1)), "lon": float(coord_match.group(2)), "warning": None}

    # 5. Geocode city names or facility addresses using Nominatim
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"format": "json", "q": raw, "limit": 1},
            headers={"User-Agent": "TheBrinkEngine/1.0"},
            timeout=8
        )
        if resp.status_code == 200:
            results = resp.json()
            if results and len(results) > 0:
                return {
                    "ok": True,
                    "lat": float(results[0]["lat"]),
                    "lon": float(results[0]["lon"]),
                    "warning": f"Geocoded: {results[0].get('display_name', '')[:60]}..."
                }
    except Exception:
        pass

    return {"ok": False, "lat": None, "lon": None, "error": f"Invalid location: '{input_text}'"}