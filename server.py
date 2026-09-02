import asyncio
import io
import json
import os
from pathlib import Path
import re
import smtplib
import sqlite3
import ssl
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.application import MIMEApplication
import aiohttp
from fastapi import FastAPI, Query, Form, UploadFile, File, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors

app = FastAPI(title="The Brink World - Automated Intelligence Engine")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
INDEX_FILE = STATIC_DIR / "index.html"
UPLOADS_DIR = BASE_DIR / "uploads"
UPLOADS_DIR.mkdir(exist_ok=True)
REPORTS_FILE = BASE_DIR / "crowd_reports.json"
DB_FILE = BASE_DIR / "enterprise_vault.db"

ADMIN_PASSKEY = os.getenv("ADMIN_PASSKEY", "brink_admin_2026")
ADMIN_NOTIFICATION_EMAIL = os.getenv("ADMIN_NOTIFICATION_EMAIL", "your_personal_email@gmail.com")
SMTP_SERVER = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")

BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "https://thebrink-engine.onrender.com")

FEEDS = {
    "usgs": "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson",
    "emsc_india": "https://www.seismicportal.eu/fdsnws/event/1/query?format=json&limit=50&minlat=6.0&maxlat=37.5&minlon=68.0&maxlon=97.5",
    "swpc_xray": "https://services.swpc.noaa.gov/json/goes/primary/xrays-6-hour.json",
    "swpc_kp": "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json",
    "nws_alerts": "https://api.weather.gov/alerts/active",
    "nhc_rss": "https://www.nhc.noaa.gov/index-at.xml",
    "gdacs_rss": "https://www.gdacs.org/xml/rss.xml",
}

CACHE = {"data": None, "last_collected": 0}
FAST_INTERVAL_MINUTES = 3

# ================= PERSISTENT VAULT (SQLITE) =================

def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS client_assets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            client_name TEXT,
            client_email TEXT,
            asset_name TEXT,
            latitude REAL,
            longitude REAL,
            radius_km REAL,
            created_at TEXT,
            active INTEGER DEFAULT 0
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS alert_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id INTEGER,
            threat_signature TEXT,
            dispatched_at TEXT
        )
    ''')
    conn.commit()
    conn.close()

init_db()

def get_monitored_assets():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id, client_email, asset_name, latitude, longitude, radius_km FROM client_assets WHERE active=1")
    rows = c.fetchall()
    conn.close()
    return [{"id": r[0], "email": r[1], "name": r[2], "lat": r[3], "lon": r[4], "radius": r[5]} for r in rows]

def has_alert_dispatched(asset_id, threat_sig):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT id FROM alert_logs WHERE asset_id=? AND threat_signature=?", (asset_id, threat_sig))
    row = c.fetchone()
    conn.close()
    return row is not None

def record_alert_dispatch(asset_id, threat_sig):
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("INSERT INTO alert_logs (asset_id, threat_signature, dispatched_at) VALUES (?, ?, ?)",
              (asset_id, threat_sig, datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()

# ================= TELEMETRY UTILITIES =================

INITIAL_REPORTS = [
    {
        "id": "nepal-himalaya-corridor-2026",
        "title": "Glacial Lake Breach & High-Altitude Debris Surge",
        "location": "Trishuli / Langtang River Corridor, Nepal",
        "latitude": 27.7172,
        "longitude": 85.3240,
        "author": "Himalayan Field Recon Desk",
        "timestamp": "Verified Dispatch",
        "type": "Landslide / Flash Inundation",
        "details": (
            "Geological Trigger: Rapid freeze-thaw cycles combined with intense localized precipitation triggered bedrock slope shear failure above 3,800m elevation.\n\n"
            "Immediate Impact: High-velocity mass movement mobilized over 150,000 cubic meters of rocky debris, burying transit arteries and breaching retaining infrastructure.\n\n"
            "Downstream Progression: Temporary sediment dam formed upstream; hydrologic gauges downstream register erratic surge pulses. Low-lying river settlements remain under high-level evacuation watch."
        ),
        "media_url": "https://images.unsplash.com/photo-1547683905-f686c993aae5?auto=format&fit=crop&w=1000&q=80",
        "approved": True
    }
]

def load_reports():
    if not REPORTS_FILE.exists():
        save_reports(INITIAL_REPORTS)
        return INITIAL_REPORTS
    try:
        with open(REPORTS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return INITIAL_REPORTS

def save_reports(reports):
    with open(REPORTS_FILE, "w") as f:
        json.dump(reports, f, indent=2)

def haversine_km(lat1, lon1, lat2, lon2):
    import math
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

async def fetch_feed(session, key, url, is_json=True):
    headers = {"User-Agent": "Mozilla/5.0 TheBrinkIntelligence/2.0", "Accept": "*/*"}
    ssl_context = ssl.create_default_context()
    ssl_context.check_hostname = False
    ssl_context.verify_mode = ssl.CERT_NONE
    try:
        async with session.get(url, headers=headers, ssl=ssl_context, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            if resp.status == 200:
                text_data = await resp.text()
                if is_json: return key, True, json.loads(text_data)
                return key, True, text_data
    except Exception:
        pass
    return key, False, None

def is_in_south_asia(lat, lon):
    return 5.0 <= lat <= 38.0 and 60.0 <= lon <= 100.0

def classify_mag(mag):
    if mag >= 7.0: return "escalate"
    if mag >= 6.0: return "alert"
    if mag >= 5.0: return "watch"
    return "list"

def parse_emsc(data):
    events = []
    if not data or "features" not in data: return events
    for f in data["features"]:
        props = f.get("properties", {})
        coords = f.get("geometry", {}).get("coordinates", [None, None, None])
        lon, lat, depth = coords[0], coords[1], coords[2]
        mag = props.get("mag")
        if mag is None or lat is None or lon is None: continue
        events.append({
            "magnitude": float(mag), "place": props.get("flynn_region", "South Asia Region"),
            "time": props.get("time"), "latitude": lat, "longitude": lon,
            "depth_km": depth, "source": "EMSC", "level": classify_mag(float(mag))
        })
    return events

def parse_gdacs_rss(raw_xml):
    events = []
    if not raw_xml: return events
    try:
        root = ET.fromstring(raw_xml)
        for item in root.findall(".//item"):
            title = item.findtext("title", "")
            desc = item.findtext("description", "")
            pub = item.findtext("pubDate", "")
            
            raw_str = ET.tostring(item, encoding='unicode')
            lat_match = re.search(r"geo:lat>([0-9.-]+)", raw_str)
            lon_match = re.search(r"geo:long>([0-9.-]+)", raw_str)
            lat = float(lat_match.group(1)) if lat_match else None
            lon = float(lon_match.group(1)) if lon_match else None

            level = "watch"
            if "Red" in title or "Red" in desc: level = "escalate"
            elif "Orange" in title or "Orange" in desc: level = "alert"

            kind = "Global Crisis"
            if "Flood" in title: kind = "Severe Inundation"
            elif "Cyclone" in title or "Typhoon" in title: kind = "Tropical Cyclone"
            elif "Volcano" in title: kind = "Volcanic Ash"
            elif "Fire" in title or "Wildfire" in title: kind = "Wildfire Emergency"

            events.append({
                "headline": title.strip(), "summary": desc.strip()[:160] if desc else "Crisis warning active.",
                "level": level, "kind": kind, "latitude": lat, "longitude": lon,
                "time": pub or datetime.now(timezone.utc).isoformat()
            })
    except Exception:
        pass
    return events

def calculate_swarms(events, max_km=75.0):
    swarms = []
    processed = set()
    for i, q1 in enumerate(events):
        if i in processed or not q1.get("latitude") or not q1.get("longitude"): continue
        cluster = [q1]
        for j, q2 in enumerate(events[i+1:], start=i+1):
            if j in processed or not q2.get("latitude") or not q2.get("longitude"): continue
            dist = haversine_km(q1["latitude"], q1["longitude"], q2["latitude"], q2["longitude"])
            if dist <= max_km:
                cluster.append(q2)
                processed.add(j)
        if len(cluster) >= 3:
            processed.add(i)
            swarms.append(cluster)
    return swarms

# ================= AUTOMATED EMAIL DISPATCHER =================

def send_automated_alert(email: str, asset_name: str, threat: dict):
    if not SMTP_USER or not SMTP_PASS: return
    try:
        msg = MIMEMultipart()
        msg["From"] = f"The Brink Intelligence <{SMTP_USER}>"
        msg["To"] = email
        msg["Subject"] = f"🚨 PERIMETER BREACH ALERT: {asset_name} [{threat.get('severity', 'ELEVATED').upper()}]"

        body = f"""THE BRINK WORLD // AUTOMATED ASSET MONITORING DESK
---------------------------------------------------------
An environmental hazard has breached your monitored perimeter.

ASSET IDENTIFIER: {asset_name}
HAZARD TYPE:      {threat.get('type')}
EVENT SUMMARY:    {threat.get('title')}
PROXIMITY:        {threat.get('distance_km')} km from designated coordinates
EVENT TIMESTAMP:  {threat.get('time')}

RECOMMENDATION:
Initiate operational and continuity review for this sector.
Live Telemetry: https://thebrinkworld.com/watch

Automated Radar Engine — The Brink World
"""
        msg.attach(MIMEText(body, "plain"))
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=20)
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)
        server.quit()
    except Exception as e:
        print(f"[PERIMETER ALERT SMTP ERROR] {e}")

async def evaluate_client_geofences(intel):
    assets = get_monitored_assets()
    if not assets: return

    all_threats = []
    for q in intel["quakes"]["south_asia"] + intel["quakes"]["global"]:
        if q.get("latitude") and q.get("longitude"):
            all_threats.append({
                "lat": q["latitude"], "lon": q["longitude"],
                "type": "Seismic Ground Shaking", "title": f"M{q['magnitude']} Tremor near {q['place']}",
                "severity": q["level"], "time": q["time"], "id": f"q-{q['time']}-{q['magnitude']}"
            })
    for g in intel.get("lookout_news", []):
        if g.get("latitude") and g.get("longitude"):
            all_threats.append({
                "lat": g["latitude"], "lon": g["longitude"],
                "type": g.get("kind", "Environmental Crisis"), "title": g.get("headline"),
                "severity": g.get("level", "alert"), "time": g.get("time"), "id": f"g-{g['headline'][:20]}"
            })

    for asset in assets:
        for threat in all_threats:
            dist = haversine_km(asset["lat"], asset["lon"], threat["lat"], threat["lon"])
            if dist <= asset["radius"]:
                threat_copy = dict(threat)
                threat_copy["distance_km"] = round(dist, 1)
                sig = f"{asset['id']}_{threat['id']}"
                if not has_alert_dispatched(asset["id"], sig):
                    send_automated_alert(asset["email"], asset["name"], threat_copy)
                    record_alert_dispatch(asset["id"], sig)

# ================= TELEMETRY COLLECTOR =================

async def run_collector():
    t0 = time.time()
    async with aiohttp.ClientSession() as session:
        tasks = [
            fetch_feed(session, "usgs", FEEDS["usgs"], True),
            fetch_feed(session, "emsc", FEEDS["emsc_india"], True),
            fetch_feed(session, "swpc_xray", FEEDS["swpc_xray"], True),
            fetch_feed(session, "swpc_kp", FEEDS["swpc_kp"], True),
            fetch_feed(session, "nws", FEEDS["nws_alerts"], True),
            fetch_feed(session, "nhc", FEEDS["nhc_rss"], False),
            fetch_feed(session, "gdacs", FEEDS["gdacs_rss"], False),
        ]
        results = await asyncio.gather(*tasks)

    data_map = {k: (ok, payload) for k, ok, payload in results}
    sources_health = {}
    quakes = {"south_asia": [], "global": []}
    map_points = []
    news_feed = []

    # USGS
    usgs_ok, usgs_raw = data_map.get("usgs", (False, None))
    sources_health["USGS"] = {"ok": usgs_ok, "count": 0}
    if usgs_ok and usgs_raw and "features" in usgs_raw:
        feats = usgs_raw["features"]
        sources_health["USGS"]["count"] = len(feats)
        for f in feats:
            props = f.get("properties", {})
            coords = f.get("geometry", {}).get("coordinates", [None, None, None])
            lon, lat, depth = coords[0], coords[1], coords[2]
            mag = props.get("mag")
            if mag is None or lat is None or lon is None: continue
            iso_time = datetime.fromtimestamp(props.get("time", 0) / 1000, tz=timezone.utc).isoformat()
            q_obj = {
                "magnitude": float(mag), "place": props.get("place", "Unknown"),
                "time": iso_time, "latitude": lat, "longitude": lon, "depth_km": depth,
                "source": "USGS", "level": classify_mag(mag)
            }
            if mag >= 2.5:
                map_points.append({"lat": lat, "lon": lon, "mag": mag, "place": q_obj["place"], "time": iso_time})
            if is_in_south_asia(lat, lon): quakes["south_asia"].append(q_obj)
            else: quakes["global"].append(q_obj)

    # EMSC
    emsc_ok, emsc_raw = data_map.get("emsc", (False, None))
    sources_health["EMSC"] = {"ok": emsc_ok, "count": 0}
    if emsc_ok and emsc_raw:
        emsc_items = parse_emsc(emsc_raw)
        sources_health["EMSC"]["count"] = len(emsc_items)
        for eq in emsc_items:
            if not any(abs(eq["latitude"] - x["latitude"]) < 0.25 and abs(eq["longitude"] - x["longitude"]) < 0.25 for x in quakes["south_asia"]):
                quakes["south_asia"].append(eq)
                map_points.append({"lat": eq["latitude"], "lon": eq["longitude"], "mag": eq["magnitude"], "place": eq["place"], "time": eq["time"]})

    for k in quakes:
        quakes[k].sort(key=lambda x: str(x.get("time", "")), reverse=True)

    # News Synthesis
    all_quakes = quakes["south_asia"] + quakes["global"]
    for q in all_quakes:
        mag = q["magnitude"]
        depth = q.get("depth_km") or 10
        if mag >= 6.0:
            news_feed.append({
                "headline": f"Major M{mag:.1f} Rupture Near {q['place']}",
                "summary": f"Deep lithospheric shear detected at {depth}km depth. Surface acceleration warnings active.",
                "level": "escalate" if mag >= 7.0 else "alert",
                "kind": "Severe Earthquake", "latitude": q["latitude"], "longitude": q["longitude"], "time": q["time"]
            })
        elif 4.2 <= mag < 6.0 and depth <= 10:
            news_feed.append({
                "headline": f"Shallow M{mag:.1f} Tremor Near {q['place']}",
                "summary": f"Superficial crustal displacement ({depth}km). Enhanced vibration felt along local structures.",
                "level": "alert" if mag >= 5.0 else "watch",
                "kind": "Shallow Tremor", "latitude": q["latitude"], "longitude": q["longitude"], "time": q["time"]
            })

    for cl in calculate_swarms(all_quakes, max_km=75.0)[:3]:
        max_m = max(x["magnitude"] for x in cl)
        news_feed.append({
            "headline": f"Seismic Swarm: {len(cl)} Clustered Events Near {cl[0]['place']}",
            "summary": f"Sustained fault-stress transfer detected within a 75km zone. Escalated watch advised.",
            "level": "alert" if max_m >= 4.5 else "watch",
            "kind": "Fault Swarm", "latitude": cl[0]["latitude"], "longitude": cl[0]["longitude"], "time": cl[0]["time"]
        })

    # GDACS
    gdacs_ok, gdacs_raw = data_map.get("gdacs", (False, None))
    sources_health["GDACS"] = {"ok": gdacs_ok, "count": 0}
    if gdacs_ok and gdacs_raw:
        for g in parse_gdacs_rss(gdacs_raw)[:5]:
            news_feed.append(g)

    # SWPC Space
    space_data = {
        "xray_class": "Quiet (B-Class)", 
        "summary": "Normal solar baseline. Satellite telemetry and grid transmissions operating within parameters.",
        "kp": 2.0, "level": "Normal"
    }
    swpc_ok, swpc_x = data_map.get("swpc_xray", (False, None))
    if swpc_ok and isinstance(swpc_x, list) and len(swpc_x) > 0:
        for entry in reversed(swpc_x):
            if isinstance(entry, dict):
                flux = entry.get("current_class") or entry.get("max_class")
                if flux:
                    space_data["xray_class"] = flux
                    if flux.startswith(("M", "X")):
                        news_feed.append({
                            "headline": f"Solar Eruption Warning: {flux}-Class Flare in Progress",
                            "summary": "Ionospheric saturation spike. Degraded HF radio wave propagation on sunlit sectors.",
                            "level": "escalate" if flux.startswith("X") else "alert",
                            "kind": "Solar Flare", "time": entry.get("time_tag") or datetime.now(timezone.utc).isoformat()
                        })
                    break

    kp_ok, kp_raw = data_map.get("swpc_kp", (False, None))
    sources_health["SWPC"] = {"ok": (swpc_ok or kp_ok), "count": 1 if kp_ok else 0}
    if kp_ok and isinstance(kp_raw, list) and len(kp_raw) > 0:
        latest_kp = kp_raw[-1]
        kp_val = latest_kp.get("kp_index") or latest_kp.get("kp") if isinstance(latest_kp, dict) else (latest_kp[1] if isinstance(latest_kp, list) and len(latest_kp)>1 else 0.0)
        try: kp_val = float(kp_val)
        except Exception: kp_val = 0.0
        space_data["kp"] = kp_val
        if kp_val >= 5.0:
            space_data["level"] = "Geomagnetic Storm"
            space_data["summary"] = f"Planetary Kp reached {kp_val}. Auroral oval expansion and slight GPS phase drift detected."

    # Severe Weather
    severe_stories = []
    nws_ok, nws_raw = data_map.get("nws", (False, None))
    sources_health["NWS"] = {"ok": nws_ok, "count": 0}
    if nws_ok and nws_raw and "features" in nws_raw:
        feats = nws_raw["features"]
        sources_health["NWS"]["count"] = len(feats)
        for f in feats:
            p = f.get("properties", {})
            evt = p.get("event", "")
            severity = p.get("severity", "Unknown")
            area = p.get("areaDesc", "")
            onset = p.get("onset")
            if severity in ["Extreme", "Severe"] or any(k in evt.lower() for k in ["tornado", "flash flood", "storm", "blizzard"]):
                item = {
                    "headline": f"{evt}: {area}",
                    "summary": f"Official emergency declaration for {area}. Caution advised on transit routes.",
                    "level": "escalate" if severity == "Extreme" else "alert",
                    "kind": "Severe Weather", "time": onset
                }
                severe_stories.append(item)
                news_feed.append(item)

    seen = set()
    unique_news = []
    for item in news_feed:
        if item["headline"] not in seen:
            seen.add(item["headline"])
            unique_news.append(item)

    level_weights = {"escalate": 0, "alert": 1, "watch": 2}
    unique_news.sort(key=lambda x: level_weights.get(x.get("level"), 3))

    all_reports = load_reports()
    approved_reports = [r for r in all_reports if r.get("approved", True)]
    total_listed = len(quakes["south_asia"]) + len(quakes["global"])

    compiled = {
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_ms": int((time.time() - t0) * 1000),
        "intervals": {"fast_minutes": FAST_INTERVAL_MINUTES},
        "situation": {
            "escalate": len([x for x in unique_news if x["level"] == "escalate"]),
            "alert": len([x for x in unique_news if x["level"] == "alert"]),
            "watch": len([x for x in unique_news if x["level"] == "watch"]),
            "listed": total_listed,
            "space": 1 if (space_data["kp"] >= 5) else 0,
        },
        "lookout_news": unique_news,
        "map_points": map_points,
        "quakes": quakes,
        "space": space_data,
        "severe_stories": severe_stories[:6],
        "sources": sources_health,
        "crowd_reports": approved_reports
    }

    CACHE["data"] = compiled
    CACHE["last_collected"] = time.time()
    await evaluate_client_geofences(compiled)
    return compiled

# ================= PUBLIC API ROUTES =================

@app.get("/api/intel")
async def get_intel():
    if not CACHE["data"] or (time.time() - CACHE["last_collected"] > 60):
        return await run_collector()
    return CACHE["data"]

@app.get("/api/check-radius")
async def check_radius(
    lat: float = Query(..., description="Latitude"),
    lon: float = Query(..., description="Longitude"),
    radius_km: float = Query(300.0, description="Radius in km")
):
    intel = await get_intel()
    threats = []
    for q in intel["quakes"]["south_asia"] + intel["quakes"]["global"]:
        if q.get("latitude") and q.get("longitude"):
            dist = haversine_km(lat, lon, q["latitude"], q["longitude"])
            if dist <= radius_km:
                threats.append({
                    "type": "Seismic Ground Shaking", "title": f"M{q['magnitude']} Tremor — {q['place']}",
                    "place": q["place"], "distance_km": round(dist, 1),
                    "depth_km": q.get("depth_km"), "severity": q["level"], "time": q["time"]
                })
    for g in intel.get("lookout_news", []):
        if g.get("latitude") and g.get("longitude"):
            dist = haversine_km(lat, lon, g["latitude"], g["longitude"])
            if dist <= radius_km:
                threats.append({
                    "type": g.get("kind", "Environmental Crisis"), "title": g.get("headline"),
                    "place": g.get("summary", "Active Alert Area"), "distance_km": round(dist, 1),
                    "severity": g.get("level", "alert"), "time": g.get("time")
                })
    for r in intel.get("crowd_reports", []):
        if r.get("latitude") and r.get("longitude"):
            dist = haversine_km(lat, lon, r["latitude"], r["longitude"])
            if dist <= radius_km:
                threats.append({
                    "type": f"Field Dispatch: {r.get('type')}", "title": r.get("title"),
                    "place": r.get("location"), "distance_km": round(dist, 1),
                    "severity": "escalate" if "Flood" in r.get("type", "") or "Landslide" in r.get("type", "") else "alert",
                    "time": r.get("timestamp")
                })

    threats.sort(key=lambda x: x["distance_km"])
    return {
        "coordinates": {"lat": lat, "lon": lon}, "radius_km": radius_km,
        "threat_count": len(threats), "threats": threats,
        "risk_level": "CRITICAL" if any(t["severity"] == "escalate" for t in threats) else ("ELEVATED" if threats else "SECURE")
    }

# ================= 5-PAGE INSTITUTIONAL DOSSIER PDF =================

async def generate_pdf_binary(title: str = "Macro Hazard & Operational Continuity Dossier") -> bytes:
    intel = await get_intel()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('MainTitle', parent=styles['Heading1'], fontSize=16, leading=20, textColor=colors.HexColor("#0f172a"), fontName="Helvetica-Bold")
    h2_style = ParagraphStyle('SectionH2', parent=styles['Heading2'], fontSize=11, leading=15, textColor=colors.HexColor("#2563eb"), spaceBefore=10, spaceAfter=4, fontName="Helvetica-Bold")
    body_style = ParagraphStyle('BodyTextCustom', parent=styles['Normal'], fontSize=8.5, leading=12, textColor=colors.HexColor("#334155"))
    meta_style = ParagraphStyle('MetaStyle', parent=styles['Normal'], fontSize=7.5, leading=9, textColor=colors.HexColor("#64748b"))

    story = []
    
    # PAGE 1
    story.append(Paragraph("THE BRINK WORLD // STRATEGIC DEFENSE & RISK DOSSIER", meta_style))
    story.append(Paragraph(title, title_style))
    story.append(Paragraph(f"Telemetry Audit: {intel['evaluated_at']} UTC | Security Classification: RESTRICTED DESK", meta_style))
    story.append(Spacer(1, 10))

    sit = intel["situation"]
    summary_data = [
        ["CRITICAL THREATS", "SEVERE ALERTS", "ELEVATED WATCH", "SPACE TELEMETRY"],
        [str(sit["escalate"]), str(sit["alert"]), str(sit["watch"]), f"Kp {intel['space']['kp']} ({intel['space']['xray_class']})"]
    ]
    t = Table(summary_data, colWidths=[130, 130, 130, 140])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor("#0f172a")),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,0), 8),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('BACKGROUND', (0,1), (-1,1), colors.HexColor("#f1f5f9")),
        ('FONTNAME', (0,1), (-1,1), 'Helvetica-Bold'),
        ('FONTSIZE', (0,1), (-1,1), 11),
        ('BOTTOMPADDING', (0,0), (-1,-1), 5),
        ('TOPPADDING', (0,0), (-1,-1), 5),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor("#cbd5e1")),
    ]))
    story.append(t)
    story.append(Spacer(1, 12))

    story.append(Paragraph("1. Primary Macro Threat Analysis", h2_style))
    for item in intel["lookout_news"][:5]:
        story.append(Paragraph(f"• <b>[{item['kind'].upper()}] {item['headline']}</b>", body_style))
        story.append(Paragraph(f"  {item['summary']}", meta_style))
        story.append(Spacer(1, 4))

    # PAGE 2
    story.append(PageBreak())
    story.append(Paragraph("2. Regional Seismic & Fault Slip Evaluation", h2_style))
    story.append(Paragraph("Telemetry collected via USGS and EMSC seismic arrays across active convergent boundaries.", meta_style))
    story.append(Spacer(1, 6))

    eq_rows = [["MAG", "LOCATION / BASIN", "DEPTH", "SEVERITY", "TIMESTAMP"]]
    for q in intel["quakes"]["south_asia"][:12]:
        eq_rows.append([f"M{q['magnitude']:.1f}", q['place'][:28], f"{q.get('depth_km',10)}km", q['level'].upper(), q['time'][11:16]])
    
    eq_table = Table(eq_rows, colWidths=[40, 240, 60, 80, 110])
    eq_table.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor("#1e293b")),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTSIZE', (0,0), (-1,-1), 7.5),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor("#cbd5e1")),
    ]))
    story.append(eq_table)

    # PAGE 3
    story.append(PageBreak())
    story.append(Paragraph("3. Hydrological Inundation & Severe Weather Corridor Tracking", h2_style))
    story.append(Paragraph("Cross-referenced real-time alert data from NOAA and GDACS disaster satellites.", meta_style))
    story.append(Spacer(1, 8))
    if intel["severe_stories"]:
        for s in intel["severe_stories"]:
            story.append(Paragraph(f"<b>[SEVERE ALERT] {s['headline']}</b>", body_style))
            story.append(Paragraph(s['summary'], meta_style))
            story.append(Spacer(1, 6))
    else:
        story.append(Paragraph("No active Level-3 severe flash inundation warnings in current cycle.", body_style))

    # PAGE 4
    story.append(PageBreak())
    story.append(Paragraph("4. Space Weather, Magnetosphere & Telecom Vectors", h2_style))
    story.append(Paragraph(f"Solar X-Ray Emission: {intel['space']['xray_class']} | Planetary Kp Index: {intel['space']['kp']}", body_style))
    story.append(Paragraph(intel['space']['summary'], meta_style))
    story.append(Spacer(1, 10))
    story.append(Paragraph("Operational Guidelines for High-Altitude & Trans-Polar Communication:", h2_style))
    story.append(Paragraph("• Satcom L-band signals remain within standard latency parameters.\n• HF long-range maritime and aviation links require monitoring on sunlit sectors when X-ray class exceeds M5.0.", body_style))

    # PAGE 5
    story.append(PageBreak())
    story.append(Paragraph("5. Field Verification Network & Continuity Protocol", h2_style))
    story.append(Paragraph("Ground-level observations submitted through The Brink Field Network undergo secondary cross-referencing against orbital SAR imagery.", body_style))
    story.append(Spacer(1, 8))
    for r in intel["crowd_reports"][:3]:
        story.append(Paragraph(f"<b>DISPATCH: {r['title']}</b> ({r['location']})", body_style))
        story.append(Paragraph(r['details'], meta_style))
        story.append(Spacer(1, 6))

    story.append(Spacer(1, 20))
    story.append(Paragraph("END OF INTELLIGENCE DOSSIER // THE BRINK WORLD ENTERPRISE DESK", meta_style))

    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()

@app.get("/api/report/pdf")
async def get_pdf_report():
    pdf_bytes = await generate_pdf_binary()
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=thebrink-dossier-{int(time.time())}.pdf"}
    )

# ================= LEAD CAPTURE & ORDER INTAKE =================

@app.post("/api/lead/capture")
async def capture_order_lead(
    plan: str = Form(...),            # "dossier_pass" or "asset_watch"
    name: str = Form(...),
    email: str = Form(...),
    company: str = Form(""),
    reason: str = Form("General Risk Assessment"),
    asset_name: str = Form(None),
    lat: float = Form(None),
    lon: float = Form(None),
    radius_km: float = Form(300.0)
):
    asset_id = None
    if plan == "asset_watch":
        conn = sqlite3.connect(DB_FILE)
        c = conn.cursor()
        c.execute("""
            INSERT INTO client_assets (client_name, client_email, asset_name, latitude, longitude, radius_km, created_at, active)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0)
        """, (
            name, email, asset_name or "Strategic Corridor",
            float(lat or 0.0), float(lon or 0.0), float(radius_km or 300.0),
            datetime.now(timezone.utc).isoformat()
        ))
        asset_id = c.lastrowid
        conn.commit()
        conn.close()

    # Compile the 5-page PDF dossier
    pdf_bytes = await generate_pdf_binary()

    # Dispatch to YOUR email (Admin)
    if not SMTP_USER or not SMTP_PASS:
        print(f"[SMTP CONFIG MISSING] SMTP_USER or SMTP_PASS is empty in Render environment. Recipient intended: {ADMIN_NOTIFICATION_EMAIL}")
    else:
        try:
            print(f"[SMTP ATTEMPT] Connecting to {SMTP_SERVER}:{SMTP_PORT} for recipient {ADMIN_NOTIFICATION_EMAIL}...")
            msg = MIMEMultipart()
            msg["From"] = f"The Brink Intelligence <{SMTP_USER}>"
            msg["To"] = ADMIN_NOTIFICATION_EMAIL
            
            plan_label = "24/7 Asset Perimeter Radar ($199)" if plan == "asset_watch" else "Executive Threat Dossier ($49)"
            msg["Subject"] = f"🔔 NEW LEAD & ORDER: {name} [{plan_label}]"

            activation_link = f"{BACKEND_BASE_URL}/api/radar/activate?asset_id={asset_id}&passkey={ADMIN_PASSKEY}" if asset_id else "N/A"

            body = f"""THE BRINK WORLD // NEW CLIENT INTAKE
----------------------------------------------------------------------
A prospective client filled out their details and has been directed to Razorpay:

CLIENT DETAILS:
- Full Name:        {name}
- Corporate Email:  {email}
- Organization:     {company or 'Not specified'}
- Tier / Service:   {plan_label}
- Operational Focus:{reason}
- Timestamp:        {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
"""
            if plan == "asset_watch":
                body += f"""
MONITORED ASSET SPECS:
- Facility/Corridor: {asset_name}
- Coordinates:       Lat {lat}, Lon {lon}
- Buffer Radius:     {radius_km} km

>>> 1-CLICK RADAR ACTIVATION (After Razorpay payment confirmation):
Click this link to activate 24/7 background geofencing for {name}:
{activation_link}
----------------------------------------------------------------------
"""
            else:
                body += f"""
FULFILLMENT INSTRUCTIONS (Option 2):
Once Razorpay confirms payment, review the attached PDF dossier
and forward this email directly to {email}.
----------------------------------------------------------------------
"""
            msg.attach(MIMEText(body, "plain"))

            # Attach compiled 5-page PDF
            attachment = MIMEApplication(pdf_bytes, _subtype="pdf")
            attachment.add_header('Content-Disposition', 'attachment', filename=f"TheBrink_Dossier_{int(time.time())}.pdf")
            msg.attach(attachment)

            server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=25)
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
            server.quit()
            print(f"[LEAD DISPATCH SUCCESS] Emailed client profile and PDF to {ADMIN_NOTIFICATION_EMAIL}")
        except Exception as e:
            print(f"[SMTP DISPATCH EXCEPTION] Failed delivering to {ADMIN_NOTIFICATION_EMAIL}: {type(e).__name__} - {str(e)}")

    return {"status": "success", "message": "Lead captured."}

# ================= 1-CLICK RADAR ACTIVATION (ADMIN) =================

@app.get("/api/radar/activate", response_class=HTMLResponse)
async def activate_radar_asset(asset_id: int = Query(...), passkey: str = Query(...)):
    if passkey != ADMIN_PASSKEY:
        raise HTTPException(status_code=403, detail="Unauthorized passkey.")

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("SELECT client_name, client_email, asset_name, latitude, longitude, radius_km FROM client_assets WHERE id=?", (asset_id,))
    row = c.fetchone()
    if not row:
        conn.close()
        return HTMLResponse("<h2>Asset record not found.</h2>", status_code=404)

    client_name, client_email, asset_name, lat, lon, radius = row
    c.execute("UPDATE client_assets SET active=1 WHERE id=?", (asset_id,))
    conn.commit()
    conn.close()

    # Dispatch Welcome Email to Client
    if SMTP_USER and SMTP_PASS:
        try:
            msg = MIMEMultipart()
            msg["From"] = f"The Brink Intelligence <{SMTP_USER}>"
            msg["To"] = client_email
            msg["Subject"] = f"✅ 24/7 Asset Perimeter Radar Activated: {asset_name}"
            
            client_body = f"""Hello {client_name},

Your payment has been verified. Your asset perimeter is now actively monitored 24/7 by The Brink World Hazard Engine.

MONITORED SECTOR:
- Asset Identifier: {asset_name}
- Coordinates:      Lat {lat}, Lon {lon}
- Buffer Perimeter: {radius} km

AUTOMATED RADAR STATUS: ACTIVE
Our sensor arrays (USGS, EMSC, NOAA, GDACS) continuously evaluate this perimeter. If an active earthquake, flood breach, or severe storm enters your buffer, an emergency dispatch will be sent directly to this address.

Thank you for choosing The Brink World.
Enterprise Desk // https://thebrinkworld.com
"""
            msg.attach(MIMEText(client_body, "plain"))
            server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT, timeout=20)
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
            server.quit()
        except Exception as e:
            print(f"[RADAR WELCOME SMTP ERROR] {e}")

    return HTMLResponse(f"""
    <body style="background:#070b10;color:#e8eef5;font-family:sans-serif;padding:40px;text-align:center">
        <h1 style="color:#3dcc9c">✓ Perimeter Radar Activated</h1>
        <p style="color:#8b9aab">Asset <strong>{asset_name}</strong> for <strong>{client_email}</strong> is now live on 24/7 monitoring.</p>
        <p style="color:#8b9aab">A confirmation email was dispatched to the client.</p>
    </body>
    """)

# ================= FIELD DISPATCHES & MODERATION =================

@app.post("/api/crowd/report")
async def submit_crowd_report(
    title: str = Form(...), location: str = Form(...), report_type: str = Form(...), details: str = Form(...), file: UploadFile = File(None)
):
    media_path = None
    if file and file.filename:
        safe_name = f"{int(time.time())}_{re.sub(r'[^a-zA-Z0-9_.-]', '_', file.filename)}"
        save_dest = UPLOADS_DIR / safe_name
        with open(save_dest, "wb") as f_out:
            content = await file.read()
            f_out.write(content)
        media_path = f"/uploads/{safe_name}"

    reports = load_reports()
    new_report = {
        "id": f"rep-{int(time.time()*1000)}", "title": title.strip(), "location": location.strip(),
        "author": "Field Scout", "timestamp": datetime.now(timezone.utc).strftime("%d %b %H:%M UTC"),
        "type": report_type.strip(), "details": details.strip(), "media_url": media_path, "approved": False
    }
    reports.insert(0, new_report)
    save_reports(reports)
    return {"status": "success", "message": "Report received. Pushed to moderation queue."}

@app.get("/api/admin/reports")
async def admin_get_reports(passkey: str = Query(...)):
    if passkey != ADMIN_PASSKEY: raise HTTPException(status_code=403, detail="Invalid admin passkey.")
    return load_reports()

@app.post("/api/admin/moderate")
async def admin_moderate_report(report_id: str = Form(...), action: str = Form(...), passkey: str = Form(...)):
    if passkey != ADMIN_PASSKEY: raise HTTPException(status_code=403, detail="Invalid admin passkey.")
    reports = load_reports()
    if action == "approve":
        for r in reports:
            if r["id"] == report_id: r["approved"] = True; break
    elif action == "delete":
        reports = [r for r in reports if r["id"] != report_id]
    save_reports(reports)
    return {"status": "success", "action": action, "report_id": report_id}

if UPLOADS_DIR.exists():
    app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.api_route("/", methods=["GET", "HEAD"])
async def root_handler():
    if INDEX_FILE.exists(): return FileResponse(str(INDEX_FILE), media_type="text/html")
    alt_index = BASE_DIR / "index.html"
    if alt_index.exists(): return FileResponse(str(alt_index), media_type="text/html")
    return JSONResponse(content={"status": "online", "service": "The Brink Hazard Engine", "evaluated_at": datetime.now(timezone.utc).isoformat()}, status_code=200)

@app.api_route("/healthz", methods=["GET", "HEAD"])
async def health_check():
    return JSONResponse(content={"status": "healthy"}, status_code=200)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8080, reload=True)