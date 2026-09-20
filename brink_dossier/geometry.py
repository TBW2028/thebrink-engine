import re
import requests

def preview_location(input_text):
    raw = str(input_text).strip()
    if "maps.app.goo.gl" in raw or "goo.gl" in raw:
        try:
            resp = requests.head(raw, allow_redirects=True, timeout=6)
            raw = resp.url
        except Exception:
            pass

    pin_match = re.search(r"!3d([-+]?\d{1,2}\.\d+)!4d([-+]?\d{1,3}\.\d+)", raw)
    if pin_match:
        return {"ok": True, "lat": float(pin_match.group(1)), "lon": float(pin_match.group(2)), "warning": None}

    center_match = re.search(r"@([-+]?\d{1,2}\.\d+),([-+]?\d{1,3}\.\d+)", raw)
    if center_match:
        return {"ok": True, "lat": float(center_match.group(1)), "lon": float(center_match.group(2)), "warning": "Pinned using camera center"}

    coord_match = re.search(r"([-+]?\d{1,2}\.\d+)[,\s]+([-+]?\d{1,3}\.\d+)", raw)
    if coord_match:
        return {"ok": True, "lat": float(coord_match.group(1)), "lon": float(coord_match.group(2)), "warning": None}

    return {"ok": False, "lat": None, "lon": None, "error": "Invalid location"}
