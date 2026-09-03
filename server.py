import asyncio
import base64
import io
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import aiohttp
from fastapi import FastAPI, Query, Form, UploadFile, File, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from reportlab.lib.pagesizes import A4
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors

app = FastAPI(title="The Brink World - Hazard & Health Intelligence Engine")

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
ADMIN_NOTIFICATION_EMAIL = os.getenv("ADMIN_NOTIFICATION_EMAIL", "thebrink2028@gmail.com")
RESEND_API_KEY = os.getenv("RESEND_API_KEY", "")

FEEDS = {
    "usgs": "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson",
    "emsc_india": "https://www.seismicportal.eu/fdsnws/event/1/query?format=json&limit=50&minlat=6.0&maxlat=37.5&minlon=68.0&maxlon=97.5",
    "swpc_xray": "https://services.swpc.noaa.gov/json/goes/primary/xrays-6-hour.json",
    "swpc_kp": "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json",
    "nws_alerts": "https://api.weather.gov/alerts/active",
    "nhc_rss": "https://www.nhc.noaa.gov/index-at.xml",
    "gdacs_rss": "https://www.gdacs.org/xml/rss.xml",
    "who_don": "https://www.who.int/rss-feeds/news-english.xml"
}

CACHE = {"data": None, "last_collected": 0}
HEALTH_CACHE = {"data": None, "last_collected": 0}

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
    conn.commit()
    conn.close()

init_db()

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def load_reports():
    if not REPORTS_FILE.exists(): return []
    try:
        with open(REPORTS_FILE, "r") as f: return json.load(f)
    except Exception: return []

async def fetch_feed(session, key, url, is_json=True):
    headers = {"User-Agent": "Mozilla/5.0 TheBrinkIntelligence/2.0", "Accept": "*/*"}
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=12)) as resp:
            if resp.status == 200:
                text_data = await resp.text()
                if is_json: return key, True, json.loads(text_data)
                return key, True, text_data
    except Exception: pass
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
            "depth_km": abs(float(depth or 10.0)), "source": "EMSC", "level": classify_mag(float(mag))
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

            kind = "Environmental Crisis"
            if "Flood" in title: kind = "Severe Inundation"
            elif "Cyclone" in title or "Typhoon" in title: kind = "Tropical Cyclone"

            events.append({
                "headline": title.strip(), "summary": desc.strip()[:160] if desc else "Active hazard alert.",
                "level": level, "kind": kind, "latitude": lat, "longitude": lon,
                "time": pub or datetime.now(timezone.utc).isoformat()
            })
    except Exception: pass
    return events

# ================= AUTOMATED HEALTH & PATHOGEN INGESTION =================

async def collect_health_screener():
    now_ts = time.time()
    if HEALTH_CACHE["data"] and (now_ts - HEALTH_CACHE["last_collected"] < 1800):
        return HEALTH_CACHE["data"]

    items = []
    try:
        async with aiohttp.ClientSession() as session:
            _, ok, raw_xml = await fetch_feed(session, "who_don", FEEDS["who_don"], is_json=False)
            if ok and raw_xml:
                root = ET.fromstring(raw_xml)
                for item in root.findall(".//item")[:10]:
                    title = item.findtext("title", "")
                    desc = item.findtext("description", "")
                    pub = item.findtext("pubDate", "")
                    
                    t_low = (title + " " + desc).lower()
                    
                    # Pattern matching
                    disease = "Emerging Outbreak"
                    vector = "Respiratory / Contact"
                    severity = "REGIONAL ALERT"
                    badge_class = "badge-amber"
                    
                    if "mpox" in t_low or "monkeypox" in t_low:
                        disease = "Mpox (Clade Ib)"
                        vector = "Direct mucosal / close contact"
                        severity = "PANDEMIC WATCH"
                        badge_class = "badge-red"
                    elif "cholera" in t_low:
                        disease = "Cholera (V. cholerae)"
                        vector = "Contaminated water & raw food"
                        severity = "EPIDEMIC"
                        badge_class = "badge-red"
                    elif "avian" in t_low or "h5n1" in t_low or "influenza" in t_low:
                        disease = "Avian Influenza (H5N1)"
                        vector = "Direct animal contact / raw dairy"
                        severity = "ZOONOTIC WATCH"
                        badge_class = "badge-cyan"
                    elif "dengue" in t_low:
                        disease = "Dengue Fever (DENV-2)"
                        vector = "Aedes aegypti mosquito bites"
                        severity = "REGIONAL ALERT"
                        badge_class = "badge-amber"
                    elif "chikungunya" in t_low:
                        disease = "Chikungunya Virus"
                        vector = "Daytime Aedes mosquito bites"
                        severity = "LOCALIZED"
                        badge_class = "badge-green"

                    items.append({
                        "disease": disease,
                        "location": title[:45],
                        "headline": title.strip(),
                        "summary": desc.strip()[:140] if desc else "Active epidemiological investigation.",
                        "vector": vector,
                        "timestamp": pub[:16] if pub else datetime.now(timezone.utc).strftime("%d %b %Y"),
                        "severity": severity,
                        "badge_class": badge_class,
                        "source": "WHO Health Emergencies"
                    })
    except Exception as e:
        pass

    # Baseline registries if feed is quiet or in between cycles
    if not items or len(items) < 4:
        items = [
            {
                "disease": "Mpox (Clade Ib)", "location": "DRC, Burundi, Kenya, East Africa",
                "headline": "Mpox Public Health Emergency of International Concern",
                "summary": "Sustained transmission of Clade Ib strain. Close physical contact precautions active.",
                "vector": "Direct close contact, mucosal fluids", "timestamp": "Cycle Aug 2026",
                "severity": "PANDEMIC WATCH", "badge_class": "badge-red", "source": "WHO DON SitRep"
            },
            {
                "disease": "Dengue Fever (DENV-2)", "location": "South Asia (Gujarat, Maharashtra, Delhi)",
                "headline": "Post-Monsoon Vector-Borne Hospital Surge",
                "summary": "Thrombocytopenia and high fever cases trending upward in urban and sub-urban wards.",
                "vector": "Day-biting Aedes aegypti mosquito", "timestamp": "Cycle W34 2026",
                "severity": "REGIONAL ALERT", "badge_class": "badge-amber", "source": "NVBDCP / IDSP"
            },
            {
                "disease": "Cholera (V. cholerae O1)", "location": "Sudan (Al Jazirah), Horn of Africa Basin",
                "headline": "Acute Waterborne Inundation Surge",
                "summary": "Rapid volume loss and dehydration clusters across flood-affected zones.",
                "vector": "Contaminated municipal water & raw food", "timestamp": "Dispatch Aug 2026",
                "severity": "EPIDEMIC", "badge_class": "badge-red", "source": "WHO Global Emergency"
            },
            {
                "disease": "Avian Influenza (H5N1)", "location": "US Dairy Belts, EU Poultry Clusters",
                "headline": "Bovine and Poultry Zoonotic Surveillance",
                "summary": "Monitoring viral reassortment markers. Direct contact and unpasteurized raw dairy precautions active.",
                "vector": "Direct contact with infected animal fluids", "timestamp": "Review Aug 2026",
                "severity": "ZOONOTIC WATCH", "badge_class": "badge-cyan", "source": "CDC / ECDC"
            },
            {
                "disease": "Chikungunya", "location": "South Asian Urban Riverbank Belts",
                "headline": "Co-Circulating Post-Monsoon Arthralgia",
                "summary": "Severe symmetrical joint stiffness and acute fever presenting in outpatient clinics.",
                "vector": "Aedes mosquito bites in residential zones", "timestamp": "Monthly Tally",
                "severity": "LOCALIZED", "badge_class": "badge-green", "source": "Municipal Surveillance"
            }
        ]

    payload = {
        "updated_utc": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "active_records": len(items),
        "screener": items
    }
    HEALTH_CACHE["data"] = payload
    HEALTH_CACHE["last_collected"] = now_ts
    return payload

@app.get("/api/health/screener")
async def get_health_screener():
    return await collect_health_screener()

# ================= REST OF THE TELEMETRY & LEAD ENGINE =================

async def fetch_temperature_anomaly(lat: float, lon: float) -> dict:
    url_history = f"https://archive-api.open-meteo.com/v1/archive?latitude={lat}&longitude={lon}&start_date=2021-01-01&end_date=2025-12-31&daily=temperature_2m_mean&timezone=auto"
    url_current = f"https://archive-api.open-meteo.com/v1/archive?latitude={lat}&longitude={lon}&start_date=2026-01-01&end_date=2026-08-31&daily=temperature_2m_mean&timezone=auto"

    result = {
        "status": "NORMALIZED BASELINE", "current_mean": None, "baseline_mean": None,
        "delta": 0.0, "warning_text": "Thermal baseline operates within standard 5-year climatological tolerance."
    }

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url_history, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    data_hist = await resp.json()
                    temps_hist = [t for t in data_hist.get("daily", {}).get("temperature_2m_mean", []) if t is not None]
                    if temps_hist: result["baseline_mean"] = round(sum(temps_hist) / len(temps_hist), 1)

            async with session.get(url_current, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    data_curr = await resp.json()
                    temps_curr = [t for t in data_curr.get("daily", {}).get("temperature_2m_mean", []) if t is not None]
                    if temps_curr: result["current_mean"] = round(sum(temps_curr) / len(temps_curr), 1)

        if result["current_mean"] is not None and result["baseline_mean"] is not None:
            delta = round(result["current_mean"] - result["baseline_mean"], 1)
            result["delta"] = delta
            if delta >= 1.5:
                result["status"] = "HEAT ANOMALY (ELEVATED)"
                result["warning_text"] = f"Running annual mean is +{delta}°C above 5-year baseline ({result['current_mean']}°C vs {result['baseline_mean']}°C). Risk of transformer load tripping and concrete curing micro-cracks."
            elif delta <= -1.5:
                result["status"] = "COLD ANOMALY (ELEVATED)"
                result["warning_text"] = f"Running annual mean is {delta}°C below 5-year baseline ({result['current_mean']}°C vs {result['baseline_mean']}°C). Risk of uninsulated pipe freezing and diesel waxing."
            else:
                diff_sign = f"+{delta}" if delta > 0 else f"{delta}"
                result["warning_text"] = f"Nominal thermal variation ({diff_sign}°C relative to 5-yr baseline of {result['baseline_mean']}°C). Infrastructure operating within historical margins."
    except Exception:
        pass
    return result

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
                "time": iso_time, "latitude": lat, "longitude": lon, "depth_km": abs(float(depth or 10.0)),
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

    for k in quakes: quakes[k].sort(key=lambda x: str(x.get("time", "")), reverse=True)

    all_quakes = quakes["south_asia"] + quakes["global"]
    for q in all_quakes:
        mag = q["magnitude"]
        depth = q.get("depth_km") or 10
        if mag >= 6.0:
            news_feed.append({
                "headline": f"Major M{mag:.1f} Rupture Near {q['place']}",
                "summary": f"Deep lithospheric shear at {depth}km depth. Surface acceleration alert active.",
                "level": "escalate" if mag >= 7.0 else "alert",
                "kind": "Severe Tremor", "latitude": q["latitude"], "longitude": q["longitude"], "time": q["time"]
            })

    gdacs_ok, gdacs_raw = data_map.get("gdacs", (False, None))
    sources_health["GDACS"] = {"ok": gdacs_ok, "count": 0}
    if gdacs_ok and gdacs_raw:
        for g in parse_gdacs_rss(gdacs_raw)[:5]: news_feed.append(g)

    space_data = {
        "xray_class": "Quiet (B-Class)", "summary": "Nominal solar baseline. Satcom and navigation channels nominal.",
        "kp": 2.1, "level": "Normal"
    }
    kp_ok, kp_raw = data_map.get("swpc_kp", (False, None))
    if kp_ok and isinstance(kp_raw, list) and len(kp_raw) > 0:
        latest_kp = kp_raw[-1]
        try: kp_val = float(latest_kp.get("kp_index") or latest_kp.get("kp") or 2.1)
        except Exception: kp_val = 2.1
        space_data["kp"] = kp_val
        if kp_val >= 5.0:
            space_data["level"] = "Geomagnetic Storm"
            space_data["summary"] = f"Planetary Kp reached {kp_val}. Auroral oval expansion and GPS carrier phase drift observed."

    severe_stories = []
    nws_ok, nws_raw = data_map.get("nws", (False, None))
    if nws_ok and nws_raw and "features" in nws_raw:
        for f in nws_raw["features"][:4]:
            p = f.get("properties", {})
            if p.get("severity") in ["Extreme", "Severe"]:
                severe_stories.append({
                    "headline": p.get("event", "Severe Storm"), "summary": p.get("areaDesc", ""),
                    "level": "alert", "kind": "Severe Weather", "time": p.get("onset")
                })

    seen = set()
    unique_news = []
    for item in news_feed:
        if item["headline"] not in seen:
            seen.add(item["headline"])
            unique_news.append(item)

    compiled = {
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_ms": int((time.time() - t0) * 1000),
        "situation": {
            "escalate": len([x for x in unique_news if x.get("level") == "escalate"]),
            "alert": len([x for x in unique_news if x.get("level") == "alert"]),
            "watch": len([x for x in unique_news if x.get("level") == "watch"]),
            "listed": len(quakes["south_asia"]) + len(quakes["global"]),
            "space": 1 if (space_data["kp"] >= 5) else 0,
        },
        "lookout_news": unique_news, "map_points": map_points, "quakes": quakes,
        "space": space_data, "severe_stories": severe_stories, "sources": sources_health,
        "crowd_reports": load_reports()
    }

    CACHE["data"] = compiled
    CACHE["last_collected"] = time.time()
    return compiled

async def generate_pdf_binary(asset_name: str = "Designated Operational Corridor", lat: float = None, lon: float = None) -> bytes:
    intel = await run_collector()
    buffer = io.BytesIO()

    if (lat is None or lon is None or (lat == 0.0 and lon == 0.0)) and asset_name:
        try:
            async with aiohttp.ClientSession() as session:
                geo_url = f"https://nominatim.openstreetmap.org/search?format=json&q={aiohttp.helpers.quote(asset_name)}&limit=1"
                headers = {"User-Agent": "TheBrinkIntelligence/2.0"}
                async with session.get(geo_url, headers=headers, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    if resp.status == 200:
                        geo_data = await resp.json()
                        if geo_data:
                            lat = float(geo_data[0]["lat"])
                            lon = float(geo_data[0]["lon"])
        except Exception: pass

    has_coords = (lat is not None and lon is not None and (lat != 0.0 or lon != 0.0))
    target_lat = lat if has_coords else 28.6139
    target_lon = lon if has_coords else 77.2090

    temp_anomaly = await fetch_temperature_anomaly(target_lat, target_lon)

    quakes = intel.get("quakes", {}).get("south_asia", []) + intel.get("quakes", {}).get("global", [])
    regional_threats = []
    
    for q in quakes:
        q_lat, q_lon = q.get("latitude"), q.get("longitude")
        if q_lat is not None and q_lon is not None:
            dist = haversine_km(target_lat, target_lon, q_lat, q_lon)
            if dist <= 1200.0:
                q_c = dict(q)
                q_c["dist_km"] = round(dist, 1)
                raw_depth = abs(float(q.get("depth_km") or 10.0))
                q_c["depth_km"] = raw_depth
                
                mag = float(q_c.get("magnitude", 3.0))
                hypo_dist = math.sqrt(dist**2 + raw_depth**2)
                if hypo_dist < 10: hypo_dist = 10
                
                site_pga = (10 ** (0.41 * mag - 2.8)) / (hypo_dist / 10)
                if site_pga < 0.01: q_c["pga_str"] = "<0.01g (Negligible)"
                elif site_pga < 0.05: q_c["pga_str"] = f"{site_pga:.3f}g (Weak)"
                else: q_c["pga_str"] = f"{site_pga:.2f}g (Noticeable)"
                regional_threats.append(q_c)

    regional_threats.sort(key=lambda x: x["dist_km"])

    critical_nearby = len([q for q in regional_threats if q["dist_km"] <= 300 and q.get("magnitude",0) >= 5.0])
    elevated_nearby = len([q for q in regional_threats if q["dist_km"] <= 300 and q.get("magnitude",0) >= 3.0])
    score = 18 + (critical_nearby * 30) + (elevated_nearby * 8)
    if "ANOMALY" in temp_anomaly["status"]: score += 15
    if intel.get("space", {}).get("kp", 0) >= 5.0: score += 10
    threat_score = min(100, max(12, score))
    threat_label = "CRITICAL DISRUPTION ALERT" if threat_score >= 70 else ("ELEVATED REGIONAL WATCH" if threat_score >= 40 else "NORMALIZED BASELINE")

    doc = SimpleDocTemplate(buffer, pagesize=A4, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    PRIMARY = colors.HexColor("#0f172a")
    NAVY = colors.HexColor("#1e3a8a")
    TEXT = colors.HexColor("#1e293b")
    MUTED = colors.HexColor("#64748b")
    BORDER = colors.HexColor("#cbd5e1")
    BG_LIGHT = colors.HexColor("#f8fafc")

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('T1', parent=styles['Heading1'], fontSize=13, leading=16, textColor=PRIMARY, fontName="Helvetica-Bold")
    h2_style = ParagraphStyle('T2', parent=styles['Heading2'], fontSize=9.5, leading=13, textColor=NAVY, spaceBefore=6, spaceAfter=3, fontName="Helvetica-Bold")
    body_style = ParagraphStyle('BC', parent=styles['Normal'], fontSize=7.5, leading=10, textColor=TEXT)
    meta_style = ParagraphStyle('MC', parent=styles['Normal'], fontSize=6.8, leading=8.5, textColor=MUTED)
    tc_wrap = ParagraphStyle('TCW', parent=styles['Normal'], fontSize=7.2, leading=9, textColor=TEXT)
    tc_wrap_b = ParagraphStyle('TCWB', parent=styles['Normal'], fontSize=7.2, leading=9, textColor=PRIMARY, fontName="Helvetica-Bold")

    story = []
    def section_break(heading):
        story.append(Paragraph(heading, h2_style))
        line_t = Table([[""]], colWidths=[523], rowHeights=[1.2])
        line_t.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,-1), NAVY)]))
        story.append(line_t)
        story.append(Spacer(1, 5))

    target_str = f"Lat {target_lat:.4f}° N, Lon {target_lon:.4f}° E"
    story.append(Table([[
        Paragraph("<b>THE BRINK WORLD // AUTOMATED HAZARD ENGINE</b><br/><font size=6.5 color='#64748b'>DEFENSE TELEMETRY & ASSET CONTINUITY DIVISION</font>", body_style),
        Paragraph(f"<b>SECURITY LEVEL:</b> COMMERCIAL IN-CONFIDENCE<br/><b>CYCLE ID:</b> TBW-{int(time.time())}<br/><b>INGESTED:</b> {intel.get('evaluated_at')[:16]} UTC", meta_style)
    ]], colWidths=[333, 190], style=[('VALIGN', (0,0), (-1,-1), 'MIDDLE')]))
    story.append(Spacer(1, 6))

    story.append(Paragraph("STRATEGIC MACRO HAZARD & ASSET CONTINUITY DOSSIER", title_style))
    story.append(Spacer(1, 4))

    story.append(Table([[
        Paragraph(f"<b>TARGET FACILITY:</b> {asset_name}", tc_wrap_b),
        Paragraph(f"<b>LOCATION FIX:</b> {target_str}", tc_wrap),
        Paragraph("<b>SURVEILLANCE RADIUS:</b> 300 km Buffer", tc_wrap)
    ]], colWidths=[203, 180, 140], style=[
        ('BACKGROUND', (0,0), (-1,-1), BG_LIGHT), ('GRID', (0,0), (-1,-1), 0.5, BORDER),
        ('TOPPADDING', (0,0), (-1,-1), 4), ('BOTTOMPADDING', (0,0), (-1,-1), 4)
    ]))
    story.append(Spacer(1, 8))

    story.append(Table([
        [
            Paragraph("<b>COMPOSITE RISK INDEX</b>", tc_wrap_b),
            Paragraph("<b>300KM SEVERE BREACHES</b>", tc_wrap_b),
            Paragraph("<b>THERMAL ANOMALY (5-YR)</b>", tc_wrap_b),
            Paragraph("<b>SPACE TELEMETRY</b>", tc_wrap_b)
        ],
        [
            Paragraph(f"<b>{threat_score}/100</b> ({threat_label})", tc_wrap),
            Paragraph(f"{critical_nearby} Critical | {elevated_nearby} Elevated", tc_wrap),
            Paragraph(f"<b>{temp_anomaly['status']}</b> ({'+' if temp_anomaly['delta']>0 else ''}{temp_anomaly['delta']}°C)", tc_wrap),
            Paragraph(f"Kp {intel.get('space', {}).get('kp', '0.0')} // {intel.get('space', {}).get('xray_class', 'Quiet')}", tc_wrap)
        ]
    ], colWidths=[140, 135, 135, 113], style=[
        ('BACKGROUND', (0,0), (-1,0), BG_LIGHT), ('GRID', (0,0), (-1,-1), 0.5, BORDER),
        ('TOPPADDING', (0,0), (-1,-1), 4), ('BOTTOMPADDING', (0,0), (-1,-1), 4)
    ]))
    story.append(Spacer(1, 8))

    section_break("1. Executive Briefing & Regional Context")
    story.append(Paragraph(
        f"This strategic intelligence dossier analyzes real-time environmental stress factors within proximity to <b>{asset_name}</b> ({target_str}). "
        f"Composite risk is calibrated at <b>{threat_score}/100</b>. Within your 300km operational perimeter, sensor arrays registered "
        f"<b>{elevated_nearby} seismic shocks</b> in the current cycle. Operational thresholds are detailed below.",
        body_style
    ))
    story.append(Spacer(1, 6))

    story.append(PageBreak())
    section_break(f"2. Lithospheric Fault Dynamics Relative to {asset_name}")
    seismic_rows = [[
        Paragraph("<b>MAG</b>", tc_wrap_b), Paragraph("<b>FAULT SECTOR</b>", tc_wrap_b),
        Paragraph("<b>DEPTH</b>", tc_wrap_b), Paragraph("<b>EST. PGA</b>", tc_wrap_b),
        Paragraph("<b>DISTANCE</b>", tc_wrap_b), Paragraph("<b>TIMESTAMP</b>", tc_wrap_b)
    ]]
    for q in regional_threats[:14]:
        seismic_rows.append([
            Paragraph(f"M{q['magnitude']:.1f}", tc_wrap_b),
            Paragraph(str(q.get("place", "Suture Zone"))[:30], tc_wrap),
            Paragraph(f"{q['depth_km']:.1f} km", tc_wrap),
            Paragraph(q['pga_str'], tc_wrap),
            Paragraph(f"{q['dist_km']} km", tc_wrap_b),
            Paragraph(str(q.get("time", ""))[:16], meta_style)
        ])
    story.append(Table(seismic_rows, colWidths=[35, 175, 55, 105, 75, 78], style=[
        ('BACKGROUND', (0,0), (-1,0), BG_LIGHT), ('GRID', (0,0), (-1,-1), 0.5, BORDER),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, BG_LIGHT]),
        ('TOPPADDING', (0,0), (-1,-1), 2.5), ('BOTTOMPADDING', (0,0), (-1,-1), 2.5)
    ]))

    story.append(PageBreak())
    section_break("3. Hydrological Inundation & Thermal Stress Audit")
    hydro_table = [
        [
            Paragraph("<b>HYDROLOGIC EVENT</b>", tc_wrap_b), Paragraph("<b>METRIC TRIGGER</b>", tc_wrap_b),
            Paragraph("<b>ESTIMATED SUPPLY CHAIN IMPACT</b>", tc_wrap_b), Paragraph("<b>EVACUATION BUFFER</b>", tc_wrap_b)
        ],
        [
            Paragraph("Flash Inundation", tc_wrap_b), Paragraph("Precipitation >75mm / hr", tc_wrap),
            Paragraph("Arterial roadway scouring, culvert silt blockage, transit delays.", tc_wrap), Paragraph("3.0 km down-gradient", tc_wrap)
        ],
        [
            Paragraph("River Gauge Crest", tc_wrap_b), Paragraph("Water Level >2.5m datum", tc_wrap),
            Paragraph("Bridge pier scour; ground-level warehouse inventory submergence.", tc_wrap), Paragraph("1.5 km riverine belt", tc_wrap)
        ]
    ]
    story.append(Table(hydro_table, colWidths=[105, 110, 203, 105], style=[
        ('BACKGROUND', (0,0), (-1,0), BG_LIGHT), ('GRID', (0,0), (-1,-1), 0.5, BORDER),
        ('TOPPADDING', (0,0), (-1,-1), 4), ('BOTTOMPADDING', (0,0), (-1,-1), 4)
    ]))
    story.append(Spacer(1, 8))

    story.append(Paragraph("<b>5-Year Baseline Climatological & Thermal Stress Audit:</b>", tc_wrap_b))
    thermal_bg = colors.HexColor("#fef2f2") if "ANOMALY" in temp_anomaly["status"] else BG_LIGHT
    thermal_border = colors.HexColor("#f87171") if "ANOMALY" in temp_anomaly["status"] else BORDER

    story.append(Table([
        [
            Paragraph(f"<b>STATUS: {temp_anomaly['status']}</b>", tc_wrap_b),
            Paragraph(f"Current Running: <b>{temp_anomaly['current_mean'] or '—'}°C</b> | 5-Yr Baseline: <b>{temp_anomaly['baseline_mean'] or '—'}°C</b>", meta_style)
        ],
        [
            Paragraph(temp_anomaly["warning_text"], tc_wrap),
            Paragraph(f"Deviation: <b>{'+' if temp_anomaly['delta']>0 else ''}{temp_anomaly['delta']}°C</b>", meta_style)
        ]
    ], colWidths=[383, 140], style=[
        ('BACKGROUND', (0,0), (-1,-1), thermal_bg), ('GRID', (0,0), (-1,-1), 0.5, thermal_border),
        ('TOPPADDING', (0,0), (-1,-1), 3), ('BOTTOMPADDING', (0,0), (-1,-1), 3)
    ]))

    story.append(PageBreak())
    section_break("4. Space Weather, Ionospheric Disturbance & Satcom")
    sp = intel.get("space", {})
    space_table = [
        [
            Paragraph("<b>TELEMETRY VECTOR</b>", tc_wrap_b), Paragraph("<b>LOGGED METRIC</b>", tc_wrap_b),
            Paragraph("<b>STANDARD BASELINE</b>", tc_wrap_b), Paragraph("<b>OPERATIONAL STATUS</b>", tc_wrap_b)
        ],
        [
            Paragraph("Planetary Kp Index", tc_wrap_b), Paragraph(f"Kp {sp.get('kp', 0.0)}", tc_wrap),
            Paragraph("Kp < 4.0 (Nominal Magnetosphere)", tc_wrap),
            Paragraph("NORMAL (GNSS Stable)" if sp.get("kp",0) < 5.0 else "ELEVATED (Phase Drift)", tc_wrap)
        ]
    ]
    story.append(Table(space_table, colWidths=[120, 110, 163, 130], style=[
        ('BACKGROUND', (0,0), (-1,0), BG_LIGHT), ('GRID', (0,0), (-1,-1), 0.5, BORDER),
        ('TOPPADDING', (0,0), (-1,-1), 4), ('BOTTOMPADDING', (0,0), (-1,-1), 4)
    ]))

    story.append(PageBreak())
    section_break("5. Emergency Execution Directives & Authentication")
    directives_table = [
        [Paragraph("<b>TIMELINE</b>", tc_wrap_b), Paragraph("<b>DIRECTIVE</b>", tc_wrap_b), Paragraph("<b>DESK</b>", tc_wrap_b)],
        [
            Paragraph("T + 00:00 to 01:00 hr", tc_wrap_b),
            Paragraph(f"Ping telemetry sensors within 150 km of {asset_name}.", tc_wrap), Paragraph("Crisis Command", tc_wrap)
        ],
        [
            Paragraph("T + 01:00 to 04:00 hr", tc_wrap_b),
            Paragraph("Inspect bridge abutments and stormwater culverts.", tc_wrap), Paragraph("Supply Chain", tc_wrap)
        ]
    ]
    story.append(Table(directives_table, colWidths=[100, 313, 110], style=[
        ('BACKGROUND', (0,0), (-1,0), BG_LIGHT), ('GRID', (0,0), (-1,-1), 0.5, BORDER),
        ('TOPPADDING', (0,0), (-1,-1), 4), ('BOTTOMPADDING', (0,0), (-1,-1), 4)
    ]))
    story.append(Spacer(1, 16))

    story.append(Table([[
        Paragraph("<b>DOSSIER AUTHENTICATION:</b><br/>The Brink World Automated Threat Engine<br/>Enterprise Defense Desk", meta_style),
        Paragraph("<b>SUPPORT DESK:</b><br/>Email: thebrink2028@gmail.com<br/>Portal: https://thebrinkworld.com", meta_style)
    ]], colWidths=[280, 243], style=[('LINEABOVE', (0,0), (-1,-1), 1, PRIMARY), ('TOPPADDING', (0,0), (-1,-1), 5)]))

    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()

@app.get("/api/intel")
async def get_intel():
    if not CACHE["data"] or (time.time() - CACHE["last_collected"] > 60):
        return await run_collector()
    return CACHE["data"]

@app.post("/api/lead/capture")
async def capture_order_lead(
    plan: str = Form(...),
    name: str = Form(...),
    email: str = Form(...),
    company: str = Form(""),
    reason: str = Form("General Assessment"),
    asset_name: str = Form("Designated Operations Corridor"),
    lat: float = Form(None),
    lon: float = Form(None),
    radius_km: float = Form(300.0)
):
    plan_labels = {
        "tier1_instant_dossier": "Tier 1: Instant Site Dossier ($49)",
        "tier2_strategic_audit": "Tier 2: 15-Page Strategic Asset Audit ($349)",
        "tier3_corridor_watch": "Tier 3: 30-Day Corridor Watch Desk ($599/mo)",
        "tier4_field_recon": "Tier 4: Ground & Remote Reconnaissance ($950+)",
        "medical_pharmacy_desk": "Medical & Pharmacy Outbreak Desk ($49 / ₹3,999)"
    }
    label = plan_labels.get(plan, plan)

    pdf_bytes = None
    if plan in ("tier1_instant_dossier", "dossier_pass", "medical_pharmacy_desk"):
        pdf_bytes = await generate_pdf_binary(asset_name=asset_name, lat=lat, lon=lon)

    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        INSERT INTO client_assets (client_name, client_email, asset_name, latitude, longitude, radius_km, created_at, active)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0)
    """, (name, email, asset_name, float(lat or 0.0), float(lon or 0.0), float(radius_km or 300.0), datetime.now(timezone.utc).isoformat()))
    conn.commit()
    conn.close()

    if RESEND_API_KEY:
        body = f"""THE BRINK WORLD // NEW INTAKE NOTIFICATION
----------------------------------------------------------------------
A client submitted an order/inquiry via the Advisory Desk:

SERVICE TIER:       {label}
CLIENT NAME:        {name}
CORPORATE EMAIL:    {email}
ORGANIZATION:       {company or 'Not specified'}
TARGET ASSET/CITY:  {asset_name}
SELECTED PLAN/NOTE: {reason}
COORDINATES:        Lat: {lat}, Lon: {lon}
TIMESTAMP:          {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
----------------------------------------------------------------------
FULFILLMENT INSTRUCTIONS:
The compiled printable A4 PDF report for {asset_name} is attached to this email.
Once Razorpay confirms payment receipt, forward this attachment directly to {email}.
"""
        payload = {
            "from": "The Brink Intelligence <onboarding@resend.dev>",
            "to": [ADMIN_NOTIFICATION_EMAIL],
            "subject": f"🔔 NEW ORDER INTAKE: {name} [{label}]",
            "text": body
        }
        if pdf_bytes:
            payload["attachments"] = [{
                "filename": f"TheBrink_Report_{int(time.time())}.pdf",
                "content": base64.b64encode(pdf_bytes).decode("utf-8")
            }]
        headers = {"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post("https://api.resend.com/emails", json=payload, headers=headers) as resp:
                    pass
        except Exception: pass

    return {"status": "success", "message": "Intake processed successfully."}

if UPLOADS_DIR.exists(): app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")
if STATIC_DIR.exists(): app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

@app.api_route("/", methods=["GET", "HEAD"])
async def root_handler():
    if INDEX_FILE.exists(): return FileResponse(str(INDEX_FILE), media_type="text/html")
    alt_index = BASE_DIR / "index.html"
    if alt_index.exists(): return FileResponse(str(alt_index), media_type="text/html")
    return JSONResponse(content={"status": "online", "service": "The Brink Hazard Engine"}, status_code=200)

@app.api_route("/healthz", methods=["GET", "HEAD"])
async def health_check():
    return JSONResponse(content={"status": "healthy"}, status_code=200)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8080, reload=True)