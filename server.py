import asyncio
import os
from pathlib import Path
import re
import ssl
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import aiohttp
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

app = FastAPI(title="The Brink World - Global Watch Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
INDEX_FILE = STATIC_DIR / "index.html"

FEEDS = {
    "usgs": "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson",
    "emsc_india": "https://www.seismicportal.eu/fdsnws/event/1/query?format=json&limit=50&minlat=6.0&maxlat=37.5&minlon=68.0&maxlon=97.5",
    "ncs_india": "https://riseq.seismo.gov.in/event/feed/rss.xml",
    "swpc_xray": "https://services.swpc.noaa.gov/json/goes/primary/xrays-6-hour.json",
    "swpc_kp": "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json",
    "nws_alerts": "https://api.weather.gov/alerts/active",
    "nhc_rss": "https://www.nhc.noaa.gov/index-at.xml",
    "tsunami_rss": "https://www.tsunami.gov/events/xml/PAAQ_active.xml",
}

CACHE = {"data": None, "last_collected": 0}
FAST_INTERVAL_MINUTES = 15

async def fetch_feed(session, key, url, is_json=True):
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, application/xml, text/xml, */*",
    }
    # Bypass strict SSL verification for regional gov servers that have expired intermediate certs
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE

    try:
        async with session.get(url, headers=headers, ssl=ssl_context, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            if resp.status == 200:
                payload = await resp.json() if is_json else await resp.text()
                return key, True, payload
    except Exception as e:
        print(f"[FETCH FAILED] {key}: {e}")
    return key, False, None

def is_in_india(lat, lon):
    return 6.0 <= lat <= 37.5 and 68.0 <= lon <= 97.5

def is_in_us(lat, lon):
    return (24.0 <= lat <= 50.0 and -125.0 <= lon <= -66.0) or (51.0 <= lat <= 72.0 and -180.0 <= lon <= -129.0)

def classify_mag(mag):
    if mag >= 7.0: return "escalate"
    if mag >= 6.0: return "alert"
    if mag >= 5.0: return "watch"
    return "list"

def parse_emsc(data):
    events = []
    if not data or "features" not in data:
        return events
    for f in data["features"]:
        props = f.get("properties", {})
        geom = f.get("geometry", {})
        coords = geom.get("coordinates", [None, None, None])
        lon, lat, depth = coords[0], coords[1], coords[2]
        mag = props.get("mag")
        if mag is None: continue

        events.append({
            "magnitude": float(mag),
            "place": props.get("flynn_region", "India Region"),
            "time": props.get("time"),
            "latitude": lat,
            "longitude": lon,
            "depth_km": depth,
            "status": "reviewed",
            "source": "EMSC/NCS",
            "level": classify_mag(float(mag)),
            "url": f"https://www.emsc-csem.org/Earthquake/earthquake.php?id={props.get('source_id', '')}"
        })
    return events

def parse_ncs_rss(raw_xml):
    events = []
    if not raw_xml:
        return events
    try:
        root = ET.fromstring(raw_xml)
        for item in root.findall(".//item"):
            title = item.findtext("title", "")
            desc = item.findtext("description", "")
            pub = item.findtext("pubDate", "")
            link = item.findtext("link", "https://riseq.seismo.gov.in")
            
            mag_match = re.search(r"M(?:agnitude)?[:\s]+([0-9.]+)", title + " " + desc, re.I)
            mag = float(mag_match.group(1)) if mag_match else 2.5

            lat_match = re.search(r"Lat(?:itude)?[:\s]+([0-9.]+[NS]?)", desc, re.I)
            lon_match = re.search(r"Lon(?:gitude)?[:\s]+([0-9.]+[EW]?)", desc, re.I)
            depth_match = re.search(r"Depth[:\s]+([0-9]+)\s*km", desc, re.I)

            lat = float(re.sub(r"[^\d.]", "", lat_match.group(1))) if lat_match else 20.59
            lon = float(re.sub(r"[^\d.]", "", lon_match.group(1))) if lon_match else 78.96
            depth = int(depth_match.group(1)) if depth_match else 10

            clean_place = title.replace(f"M: {mag}", "").replace(f"M:{mag}", "").strip(" -:,")
            if not clean_place: clean_place = "India Region"

            events.append({
                "magnitude": mag,
                "place": clean_place,
                "time": pub or datetime.now(timezone.utc).isoformat(),
                "latitude": lat,
                "longitude": lon,
                "depth_km": depth,
                "status": "reviewed",
                "source": "NCS",
                "level": classify_mag(mag),
                "url": link,
            })
    except Exception as e:
        print(f"[NCS PARSE ERROR] {e}")
    return events

async def run_collector():
    t0 = time.time()
    async with aiohttp.ClientSession() as session:
        tasks = [
            fetch_feed(session, "usgs", FEEDS["usgs"], True),
            fetch_feed(session, "emsc", FEEDS["emsc_india"], True),
            fetch_feed(session, "ncs", FEEDS["ncs_india"], False),
            fetch_feed(session, "swpc_xray", FEEDS["swpc_xray"], True),
            fetch_feed(session, "swpc_kp", FEEDS["swpc_kp"], True),
            fetch_feed(session, "nws", FEEDS["nws_alerts"], True),
            fetch_feed(session, "nhc", FEEDS["nhc_rss"], False),
            fetch_feed(session, "tsunami", FEEDS["tsunami_rss"], False),
        ]
        results = await asyncio.gather(*tasks)

    data_map = {k: (ok, payload) for k, ok, payload in results}
    sources_health = {}

    quakes = {"india": [], "us": [], "global": []}
    map_points = []
    lookout = []

    # 1. USGS Ingestion
    usgs_ok, usgs_raw = data_map.get("usgs", (False, None))
    sources_health["USGS"] = {"ok": usgs_ok, "count": 0}

    if usgs_ok and usgs_raw and "features" in usgs_raw:
        feats = usgs_raw["features"]
        sources_health["USGS"]["count"] = len(feats)
        for f in feats:
            props = f.get("properties", {})
            geom = f.get("geometry", {})
            coords = geom.get("coordinates", [None, None, None])
            lon, lat, depth = coords[0], coords[1], coords[2]
            mag = props.get("mag")
            if mag is None: continue

            iso_time = datetime.fromtimestamp(props.get("time", 0) / 1000, tz=timezone.utc).isoformat()
            level = classify_mag(mag)

            q_obj = {
                "magnitude": mag,
                "place": props.get("place", "Unknown"),
                "time": iso_time,
                "latitude": lat,
                "longitude": lon,
                "depth_km": depth,
                "status": props.get("status"),
                "source": "USGS",
                "level": level,
                "url": props.get("url"),
            }

            if lat is not None and lon is not None:
                if mag >= 2.0:
                    map_points.append({"lat": lat, "lon": lon, "mag": mag, "place": q_obj["place"], "time": iso_time})
                
                if is_in_india(lat, lon):
                    quakes["india"].append(q_obj)
                elif is_in_us(lat, lon):
                    quakes["us"].append(q_obj)
                else:
                    if mag >= 4.0:
                        quakes["global"].append(q_obj)

            if mag >= 6.0:
                lookout.append({
                    "title": f"M{mag:.1f} Earthquake — {q_obj['place']}",
                    "kind": "Seismic",
                    "level": "escalate" if mag >= 7.0 else "alert",
                    "meta": f"Depth: {depth}km",
                    "time": iso_time,
                    "url": q_obj["url"],
                })

    # 2. EMSC India & Regional Ingestion (Guaranteed feed for Indian sub-continent)
    emsc_ok, emsc_raw = data_map.get("emsc", (False, None))
    if emsc_ok and emsc_raw:
        emsc_events = parse_emsc(emsc_raw)
        for eq in emsc_events:
            # deduplicate by proximity and magnitude
            if not any(abs(eq["latitude"] - x["latitude"]) < 0.2 and abs(eq["longitude"] - x["longitude"]) < 0.2 for x in quakes["india"]):
                quakes["india"].append(eq)
                map_points.append({"lat": eq["latitude"], "lon": eq["longitude"], "mag": eq["magnitude"], "place": eq["place"], "time": eq["time"]})

    # 3. Direct NCS Ingestion
    ncs_ok, ncs_raw = data_map.get("ncs", (False, None))
    sources_health["NCS/EMSC"] = {"ok": (emsc_ok or ncs_ok), "count": len(quakes["india"])}
    if ncs_ok and ncs_raw:
        ncs_events = parse_ncs_rss(ncs_raw)
        for nq in ncs_events:
            if not any(abs(nq["latitude"] - x["latitude"]) < 0.2 and abs(nq["longitude"] - x["longitude"]) < 0.2 for x in quakes["india"]):
                quakes["india"].append(nq)
                map_points.append({"lat": nq["latitude"], "lon": nq["longitude"], "mag": nq["magnitude"], "place": nq["place"], "time": nq["time"]})

    # Sort quakes by newest first
    for k in quakes:
        quakes[k].sort(key=lambda x: str(x.get("time", "")), reverse=True)

    # 4. SWPC Space Weather
    space_data = {
        "xray_class": "Background (A/B)", 
        "xray_time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), 
        "kp": 0.0, 
        "kp_time": None,
        "kp_forecast_note": "Next 3-hour Kp planetary reading in ~15–45 mins."
    }
    
    swpc_ok, swpc_x = data_map.get("swpc_xray", (False, None))
    if swpc_ok and isinstance(swpc_x, list) and len(swpc_x) > 0:
        for entry in reversed(swpc_x):
            if isinstance(entry, dict):
                flux_class = entry.get("current_class") or entry.get("max_class")
                if flux_class:
                    space_data["xray_class"] = flux_class
                    space_data["xray_time"] = entry.get("time_tag")
                    break

    kp_ok, kp_raw = data_map.get("swpc_kp", (False, None))
    sources_health["SWPC"] = {"ok": (swpc_ok or kp_ok), "count": 1 if kp_ok else 0}
    if kp_ok and isinstance(kp_raw, list) and len(kp_raw) > 0:
        latest_kp = kp_raw[-1]
        kp_val, kp_time = 0.0, None
        if isinstance(latest_kp, dict):
            kp_val = latest_kp.get("kp_index") or latest_kp.get("kp")
            kp_time = latest_kp.get("time_tag")
        elif isinstance(latest_kp, list) and len(latest_kp) > 1:
            kp_val = latest_kp[1]
            kp_time = latest_kp[0]
            
        try:
            kp_val = float(kp_val) if kp_val is not None else 0.0
        except (ValueError, TypeError):
            kp_val = 0.0

        space_data["kp"] = kp_val
        space_data["kp_time"] = kp_time
        if kp_val >= 6.0:
            lookout.append({
                "title": f"Geomagnetic Disturbance (Kp {kp_val})",
                "kind": "Space Weather",
                "level": "escalate" if kp_val >= 7.0 else "alert",
                "meta": f"Storm G{max(1, int(kp_val - 4))}",
                "time": kp_time or datetime.now(timezone.utc).isoformat(),
                "url": "https://www.spaceweather.gov",
            })

    # 5. NOAA NWS Alerts
    nws_ok, nws_raw = data_map.get("nws", (False, None))
    sources_health["NWS"] = {"ok": nws_ok, "count": 0}
    tornado_notices, severe_notices = [], []

    if nws_ok and nws_raw and "features" in nws_raw:
        feats = nws_raw["features"]
        sources_health["NWS"]["count"] = len(feats)
        for f in feats:
            p = f.get("properties", {})
            evt = p.get("event", "")
            severity = p.get("severity")
            headline = p.get("headline") or evt
            item = {
                "headline": headline,
                "area": p.get("areaDesc"),
                "classification": severity,
                "source": "NOAA-NWS",
                "onset": p.get("onset"),
                "url": p.get("@id"),
                "level": "escalate" if severity == "Extreme" else ("alert" if severity == "Severe" else "watch"),
            }
            if "Tornado" in evt:
                tornado_notices.append(item)
                if severity in ["Extreme", "Severe"]:
                    lookout.append({"title": headline, "kind": "Tornado Alert", "level": item["level"], "meta": item["area"], "time": item["onset"], "url": item["url"]})
            elif severity in ["Extreme", "Severe"]:
                severe_notices.append(item)

    # 6. NOAA NHC Tropical
    nhc_ok, nhc_raw = data_map.get("nhc", (False, None))
    sources_health["NHC"] = {"ok": nhc_ok, "count": 0}
    tropical_notices = []
    if nhc_ok and nhc_raw:
        try:
            root = ET.fromstring(nhc_raw)
            for item in root.findall(".//item"):
                title = item.findtext("title", "")
                link = item.findtext("link", "")
                pub = item.findtext("pubDate", "")
                if any(k in title for k in ["Disturbance", "Tropical", "Hurricane", "Cyclone"]):
                    tropical_notices.append({
                        "title": title,
                        "area": "Atlantic / E-Pacific",
                        "source": "NOAA-NHC",
                        "time": pub,
                        "url": link,
                        "level": "escalate" if "Hurricane" in title else "watch",
                    })
            sources_health["NHC"]["count"] = len(tropical_notices)
        except Exception:
            pass

    # 7. PTWC / NTWC Tsunami
    tsunami_ok, tsu_raw = data_map.get("tsunami", (False, None))
    sources_health["PTWC"] = {"ok": tsunami_ok, "count": 0}
    tsunami_notices = []
    if tsunami_ok and tsu_raw:
        try:
            root = ET.fromstring(tsu_raw)
            for item in root.findall(".//item"):
                title = item.findtext("title", "")
                link = item.findtext("link", "")
                pub = item.findtext("pubDate", "")
                is_threat = any(w in title.lower() for w in ["warning", "watch", "threat", "advisory"])
                tsunami_notices.append({
                    "title": title,
                    "area": "Oceanic Basin",
                    "source": "PTWC/NTWC",
                    "time": pub,
                    "url": link,
                    "level": "alert" if is_threat else "info",
                })
                if is_threat:
                    lookout.append({"title": title, "kind": "Tsunami Event", "level": "alert", "meta": "Basin Wide", "time": pub, "url": link})
            sources_health["PTWC"]["count"] = len(tsunami_notices)
        except Exception:
            pass

    total_listed = len(quakes["india"]) + len(quakes["us"]) + len(quakes["global"])
    compiled = {
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_ms": int((time.time() - t0) * 1000),
        "intervals": {"fast_minutes": FAST_INTERVAL_MINUTES},
        "situation": {
            "escalate": len([x for x in lookout if x["level"] == "escalate"]),
            "alert": len([x for x in lookout if x["level"] == "alert"]),
            "watch": len([x for x in lookout if x["level"] == "watch"]),
            "listed": total_listed,
            "space": 1 if (space_data["kp"] and space_data["kp"] >= 5) else 0,
        },
        "lookout": lookout,
        "map_points": map_points,
        "quakes": quakes,
        "space": space_data,
        "tornado": tornado_notices,
        "tropical": tropical_notices,
        "tsunami": tsunami_notices,
        "severe": severe_notices,
        "sources": sources_health,
        "ncs": {
            "note": "NCS MoES & EMSC South Asia seismic sensors active (all magnitudes).",
            "url": "https://riseq.seismo.gov.in",
        },
    }

    CACHE["data"] = compiled
    CACHE["last_collected"] = time.time()
    return compiled

async def background_poller():
    while True:
        try:
            await run_collector()
        except Exception as e:
            print(f"[POLLER ERROR] {e}")
        await asyncio.sleep(FAST_INTERVAL_MINUTES * 60)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(background_poller())

@app.get("/api/intel")
async def get_intel():
    if not CACHE["data"] or (time.time() - CACHE["last_collected"] > 60):
        return await run_collector()
    return CACHE["data"]

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.get("/")
async def serve_index():
    if INDEX_FILE.exists():
        return FileResponse(str(INDEX_FILE), media_type="text/html")
    return JSONResponse(status_code=404, content={"error": "static/index.html not found"})

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8080, reload=True)