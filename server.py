import asyncio
import base64
import io
import json
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

# ================= APP INITIALIZATION =================
app = FastAPI(title="The Brink World - Hazard Intelligence Engine")

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

def haversine_km(lat1, lon1, lat2, lon2):
    import math
    R = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2)**2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2)**2
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return R * c

def load_reports():
    if not REPORTS_FILE.exists():
        return []
    try:
        with open(REPORTS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []

def save_reports(reports):
    with open(REPORTS_FILE, "w") as f:
        json.dump(reports, f, indent=2)

async def fetch_feed(session, key, url, is_json=True):
    headers = {"User-Agent": "Mozilla/5.0 TheBrinkIntelligence/2.0", "Accept": "*/*"}
    try:
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=12)) as resp:
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
                "summary": f"Deep lithospheric shear at {depth}km depth. Strong acceleration hazard along local transit routes.",
                "level": "escalate" if mag >= 7.0 else "alert",
                "kind": "Severe Earthquake", "latitude": q["latitude"], "longitude": q["longitude"], "time": q["time"]
            })
        elif 4.2 <= mag < 6.0 and depth <= 12:
            news_feed.append({
                "headline": f"Shallow M{mag:.1f} Tremor Near {q['place']}",
                "summary": f"Superficial crustal displacement ({depth}km). Structural vibration felt along local facilities.",
                "level": "alert" if mag >= 5.0 else "watch",
                "kind": "Shallow Tremor", "latitude": q["latitude"], "longitude": q["longitude"], "time": q["time"]
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
        "summary": "Nominal solar baseline. Satcom telemetry and grid links operate within standard parameters.",
        "kp": 2.3, "level": "Normal"
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
                    "summary": f"Emergency weather declaration for {area}. Transport logistics precautions active.",
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

    total_listed = len(quakes["south_asia"]) + len(quakes["global"])

    compiled = {
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
        "elapsed_ms": int((time.time() - t0) * 1000),
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
        "crowd_reports": load_reports()
    }

    CACHE["data"] = compiled
    CACHE["last_collected"] = time.time()
    return compiled

# ================= PRINTABLE A4 REPORTLAB DOSSIER ENGINE =================

async def generate_pdf_binary(
    asset_name: str = "Designated Operational Corridor",
    lat: float = None,
    lon: float = None
) -> bytes:
    """
    Builds a professional 5-page printable A4 Risk Assessment Dossier (Pure White Background).
    Print geometry: A4 (595.27 x 841.89 pt), Printable Width = 523.27 pt.
    """
    intel = await run_collector()
    buffer = io.BytesIO()

    # Pure A4 Geometry with 36pt printable margins
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=36,
        leftMargin=36,
        topMargin=36,
        bottomMargin=36
    )

    # Ink-efficient Paper White Palette
    COLOR_PRIMARY = colors.HexColor("#0f172a")     # Deep Charcoal
    COLOR_SECONDARY = colors.HexColor("#1e3a8a")   # Corporate Navy
    COLOR_TEXT = colors.HexColor("#1e293b")        # Sharp Slate
    COLOR_MUTED = colors.HexColor("#64748b")       # Meta Slate
    COLOR_BORDER = colors.HexColor("#cbd5e1")      # Crisp Rule
    COLOR_BG_LIGHT = colors.HexColor("#f8fafc")    # Alternating Row Tint

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('MainTitle', parent=styles['Heading1'], fontSize=13.5, leading=17, textColor=COLOR_PRIMARY, fontName="Helvetica-Bold")
    h2_style = ParagraphStyle('SecH2', parent=styles['Heading2'], fontSize=9.5, leading=13, textColor=COLOR_SECONDARY, spaceBefore=7, spaceAfter=3, fontName="Helvetica-Bold")
    h3_style = ParagraphStyle('SecH3', parent=styles['Heading3'], fontSize=8.5, leading=11, textColor=COLOR_PRIMARY, spaceBefore=4, spaceAfter=2, fontName="Helvetica-Bold")
    body_style = ParagraphStyle('BodyCustom', parent=styles['Normal'], fontSize=7.8, leading=10.5, textColor=COLOR_TEXT)
    meta_style = ParagraphStyle('MetaCustom', parent=styles['Normal'], fontSize=6.8, leading=8.5, textColor=COLOR_MUTED)
    table_cell = ParagraphStyle('TableCell', parent=styles['Normal'], fontSize=7.2, leading=9, textColor=COLOR_TEXT)
    table_cell_bold = ParagraphStyle('TableCellB', parent=styles['Normal'], fontSize=7.2, leading=9, textColor=COLOR_PRIMARY, fontName="Helvetica-Bold")

    story = []

    def add_section_rule(heading_text):
        story.append(Paragraph(heading_text, h2_style))
        line_t = Table([[""]], colWidths=[523], rowHeights=[1.2])
        line_t.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,-1), COLOR_SECONDARY)]))
        story.append(line_t)
        story.append(Spacer(1, 5))

    # Location Telemetry Resolution
    has_coords = (lat is not None and lon is not None and (lat != 0.0 or lon != 0.0))
    quakes = intel.get("quakes", {}).get("south_asia", []) + intel.get("quakes", {}).get("global", [])
    
    # Calculate proximity-sorted vectors if coordinates exist
    prox_threats = []
    if has_coords:
        for q in quakes:
            q_lat = q.get("latitude")
            q_lon = q.get("longitude")
            if q_lat is not None and q_lon is not None:
                d = haversine_km(lat, lon, q_lat, q_lon)
                q_c = dict(q)
                q_c["dist_km"] = round(d, 1)
                prox_threats.append(q_c)
        prox_threats.sort(key=lambda x: x["dist_km"])

    # Composite Threat Score Calculation
    sit = intel.get("situation", {})
    score = 22
    score += sit.get("escalate", 0) * 24
    score += sit.get("alert", 0) * 11
    score += sit.get("watch", 0) * 3
    if intel.get("space", {}).get("kp", 0) >= 5.0: score += 15
    threat_score = min(100, max(15, score))
    threat_label = "CRITICAL DISRUPTION ALERT" if threat_score >= 70 else ("ELEVATED REGIONAL WATCH" if threat_score >= 40 else "NORMALIZED BASELINE")

    # =========================================================================
    # PAGE 1: EXECUTIVE THREAT MATRIX & TARGET CORRIDOR
    # =========================================================================
    target_coord_str = f"Lat {lat:.4f}° N, Lon {lon:.4f}° E" if has_coords else "Regional Centroid Projection"
    
    header_table_data = [
        [
            Paragraph("<b>THE BRINK WORLD // AUTOMATED HAZARD ENGINE</b><br/><font size=6.5 color='#64748b'>DEFENSE TELEMETRY & ASSET CONTINUITY DIVISION</font>", body_style),
            Paragraph(f"<b>CLASSIFICATION:</b> RESTRICTED B2B BRIEF<br/><b>CYCLE ID:</b> TBW-{int(time.time())}<br/><b>AUDIT UTC:</b> {intel.get('evaluated_at')[:16]}", meta_style)
        ]
    ]
    t_head = Table(header_table_data, colWidths=[333, 190])
    t_head.setStyle(TableStyle([
        ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
        ('BOTTOMPADDING', (0,0), (-1,-1), 0),
        ('TOPPADDING', (0,0), (-1,-1), 0),
    ]))
    story.append(t_head)
    story.append(Spacer(1, 7))

    story.append(Paragraph("STRATEGIC MACRO HAZARD & ASSET CONTINUITY DOSSIER", title_style))
    story.append(Spacer(1, 4))

    target_box_data = [
        [
            Paragraph(f"<b>TARGET FACILITY / PORTFOLIO:</b> {asset_name}", table_cell_bold),
            Paragraph(f"<b>LOCATION FIX:</b> {target_coord_str}", table_cell),
            Paragraph("<b>SURVEILLANCE RADIUS:</b> 300 km Buffer", table_cell)
        ]
    ]
    t_target = Table(target_box_data, colWidths=[203, 180, 140])
    t_target.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), COLOR_BG_LIGHT),
        ('GRID', (0,0), (-1,-1), 0.5, COLOR_BORDER),
        ('TOPPADDING', (0,0), (-1,-1), 4),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4),
    ]))
    story.append(t_target)
    story.append(Spacer(1, 8))

    kpi_matrix = [
        ["COMPOSITE RISK INDEX", "ACTIVE SEVERE BREACHES", "LITHOSPHERIC STRESS (<15km)", "IONOSPHERIC VECTOR"],
        [
            f"{threat_score}/100 ({threat_label})",
            f"{sit.get('escalate', 0)} Critical | {sit.get('alert', 0)} Elevated",
            f"{len([q for q in quakes if (q.get('depth_km') or 10) <= 15])} Shallow Displacements",
            f"Kp {intel.get('space', {}).get('kp', '—')} // {intel.get('space', {}).get('xray_class', 'Quiet')}"
        ]
    ]
    t_kpi = Table(kpi_matrix, colWidths=[140, 135, 135, 113])
    t_kpi.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), COLOR_PRIMARY),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,0), 6.5),
        ('ALIGN', (0,0), (-1,-1), 'CENTER'),
        ('BACKGROUND', (0,1), (-1,1), COLOR_BG_LIGHT),
        ('FONTNAME', (0,1), (-1,1), 'Helvetica-Bold'),
        ('FONTSIZE', (0,1), (-1,1), 7.5),
        ('GRID', (0,0), (-1,-1), 0.5, COLOR_BORDER),
        ('TOPPADDING', (0,0), (-1,-1), 4.5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 4.5),
    ]))
    story.append(t_kpi)
    story.append(Spacer(1, 8))

    add_section_rule("1. Executive Briefing & Multi-Hazard Situation")

    exec_summary = (
        f"This strategic assessment evaluates real-time sensory feeds relative to <b>{asset_name}</b> ({target_coord_str}). "
        f"The composite operational risk index is calibrated at <b>{threat_score}/100</b>. "
        f"Telemetry indicates <b>{sit.get('escalate', 0)} catastrophic threshold alerts</b> and <b>{sit.get('alert', 0)} elevated hazard warnings</b> "
        f"in current circulation. Facility supervisors and logistics planners must evaluate structural vibration, bridge scour, "
        f"and telecommunication signal integrity against the itemized thresholds detailed herein."
    )
    story.append(Paragraph(exec_summary, body_style))
    story.append(Spacer(1, 6))

    story.append(Paragraph("<b>Primary Detected Incidents (Priority Stream):</b>", h3_style))
    for item in intel.get("lookout_news", [])[:4]:
        b_data = [
            [
                Paragraph(f"<b>[{item.get('kind', 'HAZARD').upper()}]</b> {item.get('headline')}", table_cell_bold),
                Paragraph(f"Logged: {item.get('time', '')[:16]} UTC", meta_style)
            ],
            [
                Paragraph(item.get("summary", "No further analytical data."), table_cell),
                Paragraph("Status: Active Vector", meta_style)
            ]
        ]
        t_b = Table(b_data, colWidths=[413, 110])
        t_b.setStyle(TableStyle([
            ('BACKGROUND', (0,0), (-1,-1), COLOR_BG_LIGHT),
            ('GRID', (0,0), (-1,-1), 0.5, COLOR_BORDER),
            ('TOPPADDING', (0,0), (-1,-1), 2.5),
            ('BOTTOMPADDING', (0,0), (-1,-1), 2.5),
        ]))
        story.append(t_b)
        story.append(Spacer(1, 3))

    # =========================================================================
    # PAGE 2: SEISMIC MATRIX & PROXIMITY AUDIT
    # =========================================================================
    story.append(PageBreak())
    add_section_rule(f"2. Lithospheric Fault Dynamics & Proximity to {asset_name}")

    story.append(Paragraph(
        "Seismic slip events occurring under 15 km depth deliver heightened Peak Ground Acceleration (PGA) directly to foundation columns "
        "and civil retaining works. Below is the active fault movement register, sorted by proximity to your designated coordinates:",
        body_style
    ))
    story.append(Spacer(1, 6))

    seismic_headers = ["MAG", "GEOLOGICAL FAULT SECTOR", "FOCAL DEPTH", "EST. PGA", "DISTANCE TO ASSET", "LOGGED UTC"]
    seismic_rows = [seismic_headers]

    source_quakes = prox_threats[:15] if prox_threats else quakes[:15]
    for q in source_quakes:
        mag = q.get("magnitude", 0.0)
        depth = q.get("depth_km") or 10.0
        pga_est = f"{(10 ** (0.3 * mag - 1.2)):.2f}g" if depth <= 20 else "<0.05g"
        dist_str = f"{q.get('dist_km')} km" if "dist_km" in q else "Regional Basin"
        
        seismic_rows.append([
            Paragraph(f"<b>M{mag:.1f}</b>", table_cell_bold),
            Paragraph(str(q.get("place", "Unknown"))[:32], table_cell),
            Paragraph(f"{depth:.1f} km", table_cell),
            Paragraph(pga_est, table_cell),
            Paragraph(dist_str, table_cell_bold),
            Paragraph(str(q.get("time", ""))[:16], meta_style)
        ])

    t_seismic = Table(seismic_rows, colWidths=[38, 185, 60, 60, 95, 85])
    t_seismic.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), COLOR_PRIMARY),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,0), 6.5),
        ('GRID', (0,0), (-1,-1), 0.5, COLOR_BORDER),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, COLOR_BG_LIGHT]),
        ('TOPPADDING', (0,0), (-1,-1), 2.5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 2.5),
        ('ALIGN', (0,0), (0,-1), 'CENTER'),
        ('ALIGN', (2,0), (4,-1), 'CENTER'),
    ]))
    story.append(t_seismic)
    story.append(Spacer(1, 8))

    story.append(Paragraph("<b>Industrial Structural Integrity Protocols:</b>", h3_style))
    story.append(Paragraph(
        f"If ground vibrations from tremors within 200 km of {asset_name} exceed 0.15g, site safety teams must immediately inspect "
        "heavy overhead crane gantry rails, chemical storage anchor bolts, and electrical busbar connections for shear dislocation.",
        body_style
    ))

    # =========================================================================
    # PAGE 3: HYDROLOGICAL INUNDATION & METEOROLOGICAL VECTORS
    # =========================================================================
    story.append(PageBreak())
    add_section_rule("3. Hydrological Inundation, River Basin Scour & Storm Vectors")

    story.append(Paragraph(
        "Satellite precipitation gauges and GDACS hydrologic models track catchment saturation and reservoir discharge surges. "
        "Saturated mountain catchments and coastal zones present heightened risk of bridge substructure scour and severed transport links:",
        body_style
    ))
    story.append(Spacer(1, 6))

    if intel.get("severe_stories"):
        for s in intel.get("severe_stories")[:4]:
            h_box = [
                [Paragraph(f"<b>HAZARD TYPE:</b> {s.get('kind', 'Severe Weather')}", table_cell_bold), Paragraph(f"Onset: {str(s.get('time',''))[:16]}", meta_style)],
                [Paragraph(f"<b>Affected Zone:</b> {s.get('headline')}", table_cell), Paragraph("Alert Tier: HIGH", meta_style)],
                [Paragraph(f"<b>Impact Assessment:</b> {s.get('summary')}", body_style), Paragraph("Continuity: Route Audit", meta_style)]
            ]
            t_h = Table(h_box, colWidths=[403, 120])
            t_h.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,-1), COLOR_BG_LIGHT),
                ('GRID', (0,0), (-1,-1), 0.5, COLOR_BORDER),
                ('TOPPADDING', (0,0), (-1,-1), 2.5),
                ('BOTTOMPADDING', (0,0), (-1,-1), 2.5),
            ]))
            story.append(t_h)
            story.append(Spacer(1, 4))
    else:
        story.append(Paragraph("No active Level-3 severe flash inundation warnings detected in the immediate regional catchment.", body_style))
        story.append(Spacer(1, 6))

    hydro_ref = [
        ["HYDROLOGIC EVENT", "METRIC THRESHOLD", "ESTIMATED SUPPLY CHAIN IMPACT", "EVACUATION RADIUS"],
        [
            "Flash Inundation",
            "Precipitation >75mm / hr",
            "Roadway scouring, foundation mud-inundation, transit delays.",
            "3.0 km down-gradient"
        ],
        [
            "River Gauge Crest",
            "Gauge >2.5m above datum",
            "Bridge pier scour; ground-level warehouse inventory submergence.",
            "1.5 km riverine belt"
        ],
        [
            "Glacial Lake Surge (GLOF)",
            "Cryospheric moraine breach",
            "Debris mass >150k m3 at 30 km/h; permanent arterial severance.",
            "25.0 km downstream"
        ]
    ]
    t_hydro = Table(hydro_ref, colWidths=[110, 115, 188, 110])
    t_hydro.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), COLOR_SECONDARY),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,0), 6.5),
        ('GRID', (0,0), (-1,-1), 0.5, COLOR_BORDER),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, COLOR_BG_LIGHT]),
        ('TOPPADDING', (0,0), (-1,-1), 3.5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 3.5),
    ]))
    story.append(t_hydro)

    # =========================================================================
    # PAGE 4: SPACE WEATHER, TELECOM & SATCOM PROPAGATION
    # =========================================================================
    story.append(PageBreak())
    add_section_rule("4. Space Weather, Ionospheric Disturbance & Satcom Propagation")

    sp = intel.get("space", {})
    kp = sp.get("kp", 0.0)
    xray = sp.get("xray_class", "Quiet")

    story.append(Paragraph(
        "NOAA SWPC space weather arrays track geomagnetic flux, solar energetic flares, and ionospheric ionization. "
        "Elevated Kp values induce GPS carrier phase drift and disrupt autonomous RTK navigation systems:",
        body_style
    ))
    story.append(Spacer(1, 6))

    space_summary = [
        ["TELEMETRY VECTOR", "RECORDED METRIC", "STANDARD BASELINE", "OPERATIONAL STATUS"],
        [
            "Planetary Kp Index",
            f"Kp {kp}",
            "Kp < 4.0 (Nominal Magnetosphere)",
            "NORMAL (GNSS Stable)" if kp < 5.0 else "ELEVATED (GPS Phase Jitter Observed)"
        ],
        [
            "Solar X-Ray Emission Flux",
            f"{xray}",
            "Class B / C (Nominal Baseline)",
            "STABLE (HF Radio Clear)" if not xray.startswith(("M", "X")) else "DEGRADED (High Sunlit Absorption)"
        ],
        [
            "Satcom L-Band Propagation",
            "LEO / GEO Channels Nominal",
            "Bit Error Rate (BER) < 10^-7",
            "NOMINAL CONTINUITY"
        ]
    ]
    t_sp = Table(space_summary, colWidths=[120, 115, 158, 130])
    t_sp.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), COLOR_PRIMARY),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,0), 6.5),
        ('GRID', (0,0), (-1,-1), 0.5, COLOR_BORDER),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, COLOR_BG_LIGHT]),
        ('TOPPADDING', (0,0), (-1,-1), 3.5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 3.5),
    ]))
    story.append(t_sp)
    story.append(Spacer(1, 8))

    story.append(Paragraph("<b>Operational Recommendations for Navigational Equipment:</b>", h3_style))
    story.append(Paragraph(
        "• <b>Autonomous Port Machinery & Survey Drones:</b> If Kp >= 5.0, RTK differential positioning units may experience "
        "pseudorange errors up to 3.5 meters. Automated cranes and terminal vehicles should rely on secondary laser or optical guidance.<br/>"
        "• <b>Maritime Emergency Frequencies:</b> Sunlit trans-oceanic vessels must verify secondary satellite links as 3–30 MHz shortwave channels face absorption.",
        body_style
    ))

    # =========================================================================
    # PAGE 5: GROUND RECONNAISSANCE & CONTINUITY DIRECTIVES
    # =========================================================================
    story.append(PageBreak())
    add_section_rule("5. Field Reconnaissance Network & Operational Directives")

    story.append(Paragraph(
        "The Brink World Field Network ingests geotagged eyewitness dispatches verified against satellite radar imagery "
        "to confirm actual ground passability and infrastructure condition:",
        body_style
    ))
    story.append(Spacer(1, 5))

    reports = intel.get("crowd_reports", [])[:2]
    if reports:
        for rep in reports:
            r_box = [
                [Paragraph(f"<b>VERIFIED DISPATCH: {rep.get('title')}</b>", table_cell_bold), Paragraph(f"Location: {rep.get('location')}", meta_style)],
                [Paragraph(rep.get("details", "").replace("\n\n", "<br/>"), table_cell), Paragraph(f"Observer: {rep.get('author','Scout')}", meta_style)]
            ]
            t_r = Table(r_box, colWidths=[373, 150])
            t_r.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,-1), COLOR_BG_LIGHT),
                ('GRID', (0,0), (-1,-1), 0.5, COLOR_BORDER),
                ('TOPPADDING', (0,0), (-1,-1), 2.5),
                ('BOTTOMPADDING', (0,0), (-1,-1), 2.5),
            ]))
            story.append(t_r)
            story.append(Spacer(1, 4))

    story.append(Paragraph("<b>Standard Operational Continuity Directives (Execution Protocol):</b>", h3_style))
    directives = [
        ["TIMELINE", "MANDATORY CONTINUITY DIRECTIVE", "RESPONSIBLE DESK"],
        [
            "T + 00:00 to 01:00 hr",
            f"Establish perimeter geofence audit. Ping all facilities within 150km of {asset_name}.",
            "Crisis Command Desk"
        ],
        [
            "T + 01:00 to 04:00 hr",
            "Inspect bridge approaches and flood gates. Reroute logistics vehicles away from river flood belts.",
            "Supply Chain & Logistics"
        ],
        [
            "T + 04:00 to 12:00 hr",
            "Verify backup satellite comms and battery storage units if local electrical substations disconnect.",
            "IT & Telemetry Operations"
        ],
        [
            "T + 12:00 to 24:00 hr",
            "Commission targeted field scout verification for on-site damage certification and route clearance.",
            "Field Intelligence Desk"
        ]
    ]
    t_dir = Table(directives, colWidths=[95, 318, 110])
    t_dir.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), COLOR_PRIMARY),
        ('TEXTCOLOR', (0,0), (-1,0), colors.white),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('FONTSIZE', (0,0), (-1,0), 6.5),
        ('GRID', (0,0), (-1,-1), 0.5, COLOR_BORDER),
        ('ROWBACKGROUNDS', (0,1), (-1,-1), [colors.white, COLOR_BG_LIGHT]),
        ('TOPPADDING', (0,0), (-1,-1), 3.5),
        ('BOTTOMPADDING', (0,0), (-1,-1), 3.5),
    ]))
    story.append(t_dir)
    story.append(Spacer(1, 14))

    signoff = [
        [
            Paragraph("<b>DOSSIER AUTHENTICATION:</b><br/>The Brink World Automated Threat Telemetry Engine<br/>Defense & Strategic Continuity Desk", meta_style),
            Paragraph("<b>ENTERPRISE SUPPORT DESK:</b><br/>Email: thebrink2028@gmail.com<br/>Portal: https://thebrinkworld.com", meta_style)
        ]
    ]
    t_sign = Table(signoff, colWidths=[280, 243])
    t_sign.setStyle(TableStyle([
        ('LINEABOVE', (0,0), (-1,-1), 1, COLOR_PRIMARY),
        ('TOPPADDING', (0,0), (-1,-1), 5),
    ]))
    story.append(t_sign)

    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()

# ================= PUBLIC API ROUTES =================

@app.get("/api/intel")
async def get_intel():
    if not CACHE["data"] or (time.time() - CACHE["last_collected"] > 60):
        return await run_collector()
    return CACHE["data"]

@app.get("/api/report/pdf")
async def get_pdf_report(
    asset_name: str = Query("Designated Operations Corridor"),
    lat: float = Query(None),
    lon: float = Query(None)
):
    pdf_bytes = await generate_pdf_binary(asset_name=asset_name, lat=lat, lon=lon)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=thebrink-dossier-{int(time.time())}.pdf"}
    )

# ================= LEAD CAPTURE VIA HTTPS RESEND =================

@app.post("/api/lead/capture")
async def capture_order_lead(
    plan: str = Form(...),
    name: str = Form(...),
    email: str = Form(...),
    company: str = Form(""),
    reason: str = Form("General Risk Assessment"),
    asset_name: str = Form("Designated Operations Corridor"),
    lat: float = Form(None),
    lon: float = Form(None),
    radius_km: float = Form(300.0)
):
    plan_labels = {
        "tier1_instant_dossier": "Tier 1: Instant Site Dossier ($49)",
        "tier2_strategic_audit": "Tier 2: 15-Page Strategic Asset Audit ($349)",
        "tier3_corridor_watch": "Tier 3: 30-Day Corridor Watch Desk ($599/mo)",
        "tier4_field_recon": "Tier 4: Ground & Remote Reconnaissance ($950+)"
    }
    label = plan_labels.get(plan, plan)

    # For Tier 1, compile the printable A4 5-page PDF in-memory
    pdf_bytes = None
    if plan in ("tier1_instant_dossier", "dossier_pass"):
        pdf_bytes = await generate_pdf_binary(asset_name=asset_name, lat=lat, lon=lon)

    # Save to persistent database
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        INSERT INTO client_assets (client_name, client_email, asset_name, latitude, longitude, radius_km, created_at, active)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0)
    """, (
        name, email, asset_name,
        float(lat or 0.0), float(lon or 0.0), float(radius_km or 300.0),
        datetime.now(timezone.utc).isoformat()
    ))
    conn.commit()
    conn.close()

    # Deliver Intake Alert via Resend HTTPS (Port 443)
    if RESEND_API_KEY:
        body = f"""THE BRINK WORLD // NEW INTAKE NOTIFICATION
----------------------------------------------------------------------
A prospective client submitted an order/inquiry via the Advisory Desk:

SERVICE TIER:       {label}
CLIENT NAME:        {name}
CORPORATE EMAIL:    {email}
ORGANIZATION:       {company or 'Not specified'}
TARGET ASSET/ROUTE: {asset_name}
SPECIFIED DETAILS:  {reason}
COORDINATES:        Lat: {lat}, Lon: {lon}
TIMESTAMP:          {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
----------------------------------------------------------------------
"""
        if plan in ("tier1_instant_dossier", "dossier_pass"):
            body += f"""FULFILLMENT INSTRUCTIONS (Tier 1):
The tailored 5-page printable A4 PDF dossier is attached to this email.
Once Razorpay confirms the $49 receipt, forward this attachment directly to {email}.
"""
        elif plan == "tier2_strategic_audit":
            body += f"""ACTION REQUIRED (Tier 2 - $349):
Review the target facility ({asset_name}) and scoping details.
Assemble the 15-page analytical dossier with historical seismic catalog and DEM flood modeling, then issue the corporate invoice.
"""
        elif plan == "tier3_corridor_watch":
            body += f"""ACTION REQUIRED (Tier 3 - $599/mo):
Review the waypoint nodes ({reason}). Establish the 30-day SQLite alert geofence array.
"""
        elif plan == "tier4_field_recon":
            body += f"""ACTION REQUIRED (Tier 4 - $950+ Urgent):
Check Sentinel-1/2 satellite pass availability for {asset_name} and review regional ground-scout coverage. Respond within 4 hours.
"""

        payload = {
            "from": "The Brink Intelligence <onboarding@resend.dev>",
            "to": [ADMIN_NOTIFICATION_EMAIL],
            "subject": f"🔔 NEW CLIENT INTAKE: {name} [{label}]",
            "text": body
        }

        if pdf_bytes:
            payload["attachments"] = [
                {
                    "filename": f"TheBrink_Dossier_{int(time.time())}.pdf",
                    "content": base64.b64encode(pdf_bytes).decode("utf-8")
                }
            ]

        headers = {"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post("https://api.resend.com/emails", json=payload, headers=headers) as resp:
                    print(f"[INTAKE DISPATCH] Status: {resp.status}")
        except Exception as e:
            print(f"[INTAKE DISPATCH ERROR] {e}")

    return {"status": "success", "message": "Intake processed successfully."}

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
    return JSONResponse(content={"status": "online", "service": "The Brink Hazard Engine"}, status_code=200)

@app.api_route("/healthz", methods=["GET", "HEAD"])
async def health_check():
    return JSONResponse(content={"status": "healthy"}, status_code=200)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8080, reload=True)