import asyncio
import io
import json
import os
from pathlib import Path
import re
import ssl
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import aiohttp
from fastapi import FastAPI, Query, Form, UploadFile, File, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors

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

ADMIN_PASSKEY = "brink_admin_2026"

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
            "Geological Trigger: Rapid freeze-thaw cycles combined with intense localized monsoon precipitation triggered a bedrock slope shear failure above 3,800m elevation.\n\n"
            "Immediate Impact: High-velocity mass movement mobilized over 150,000 cubic meters of rocky debris, burying vital north-south transit roads and breaching local riverbank retaining walls.\n\n"
            "Downstream Progression: A temporary sediment dam formed at narrow gorges upstream; hydrologic gauges downstream show erratic surge pulses. Low-lying river settlements and transport bridges remain under active high-level evacuation watch."
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
                if is_json:
                    return key, True, json.loads(text_data)
                return key, True, text_data
    except Exception as e:
        print(f"[FETCH FAILED] {key}: {e}")
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
    if not data or "features" not in data:
        return events
    for f in data["features"]:
        props = f.get("properties", {})
        coords = f.get("geometry", {}).get("coordinates", [None, None, None])
        lon, lat, depth = coords[0], coords[1], coords[2]
        mag = props.get("mag")
        if mag is None or lat is None or lon is None: continue

        events.append({
            "magnitude": float(mag),
            "place": props.get("flynn_region", "South Asia Region"),
            "time": props.get("time"),
            "latitude": lat,
            "longitude": lon,
            "depth_km": depth,
            "source": "EMSC",
            "level": classify_mag(float(mag))
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
            if "Red" in title or "Red" in desc:
                level = "escalate"
            elif "Orange" in title or "Orange" in desc:
                level = "alert"

            kind = "Global Crisis"
            if "Flood" in title: kind = "Severe Inundation"
            elif "Cyclone" in title or "Typhoon" in title: kind = "Tropical Cyclone"
            elif "Volcano" in title: kind = "Volcanic Ash"
            elif "Fire" in title or "Wildfire" in title: kind = "Wildfire Emergency"

            events.append({
                "headline": title.strip(),
                "summary": desc.strip()[:160] if desc else "International crisis warning active.",
                "level": level,
                "kind": kind,
                "latitude": lat,
                "longitude": lon,
                "time": pub or datetime.now(timezone.utc).isoformat()
            })
    except Exception:
        pass
    return events

def calculate_swarms(events, max_km=75.0):
    swarms = []
    processed = set()
    for i, q1 in enumerate(events):
        if i in processed or not q1.get("latitude") or not q1.get("longitude"):
            continue
        cluster = [q1]
        for j, q2 in enumerate(events[i+1:], start=i+1):
            if j in processed or not q2.get("latitude") or not q2.get("longitude"):
                continue
            dist = haversine_km(q1["latitude"], q1["longitude"], q2["latitude"], q2["longitude"])
            if dist <= max_km:
                cluster.append(q2)
                processed.add(j)
        if len(cluster) >= 3:
            processed.add(i)
            swarms.append(cluster)
    return swarms

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

    # 1. USGS Processing
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
                "magnitude": float(mag),
                "place": props.get("place", "Unknown"),
                "time": iso_time,
                "latitude": lat,
                "longitude": lon,
                "depth_km": depth,
                "source": "USGS",
                "level": classify_mag(mag)
            }

            if mag >= 2.5:
                map_points.append({"lat": lat, "lon": lon, "mag": mag, "place": q_obj["place"], "time": iso_time})
            
            if is_in_south_asia(lat, lon):
                quakes["south_asia"].append(q_obj)
            else:
                quakes["global"].append(q_obj)

    # 2. Regional EMSC Processing
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

    # 3. News Transformation & Compound Analysis
    all_quakes = quakes["south_asia"] + quakes["global"]

    for q in all_quakes:
        mag = q["magnitude"]
        depth = q.get("depth_km") or 10
        if mag >= 6.0:
            news_feed.append({
                "headline": f"Major M{mag:.1f} Earthquake Strikes {q['place']}",
                "summary": f"Intense tectonic rupture at {depth}km depth. High surface acceleration likely felt across surrounding districts.",
                "level": "escalate" if mag >= 7.0 else "alert",
                "kind": "Severe Earthquake",
                "latitude": q["latitude"],
                "longitude": q["longitude"],
                "time": q["time"]
            })
        elif 4.2 <= mag < 6.0 and depth <= 10:
            news_feed.append({
                "headline": f"Shallow M{mag:.1f} Tremor Near {q['place']}",
                "summary": f"Extremely shallow ({depth}km) focal depth will cause noticeable shaking and minor building vibration despite moderate magnitude.",
                "level": "alert" if mag >= 5.0 else "watch",
                "kind": "Shallow Tremor",
                "latitude": q["latitude"],
                "longitude": q["longitude"],
                "time": q["time"]
            })

    # Swarms
    for cl in calculate_swarms(all_quakes, max_km=75.0)[:3]:
        max_m = max(x["magnitude"] for x in cl)
        news_feed.append({
            "headline": f"Seismic Swarm: {len(cl)} Clustered Tremors at {cl[0]['place']}",
            "summary": f"Multiple tremors detected within a 75km zone. Indicates ongoing fault stress transfer and active crustal adjustment.",
            "level": "alert" if max_m >= 4.5 else "watch",
            "kind": "Fault Swarm",
            "latitude": cl[0]["latitude"],
            "longitude": cl[0]["longitude"],
            "time": cl[0]["time"]
        })

    # GDACS Multi-Hazard
    gdacs_ok, gdacs_raw = data_map.get("gdacs", (False, None))
    sources_health["GDACS"] = {"ok": gdacs_ok, "count": 0}
    if gdacs_ok and gdacs_raw:
        for g in parse_gdacs_rss(gdacs_raw)[:5]:
            news_feed.append(g)

    # SWPC Space Weather
    space_data = {
        "xray_class": "Quiet (B-Class)", 
        "summary": "Normal solar baseline. Satellite communications operating undisturbed.",
        "kp": 2.0, 
        "level": "Normal"
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
                            "summary": "Spike in solar X-ray emissions. Minor radio blackouts and satellite GPS telemetry drift possible on sunlit regions.",
                            "level": "escalate" if flux.startswith("X") else "alert",
                            "kind": "Solar Flare",
                            "time": entry.get("time_tag") or datetime.now(timezone.utc).isoformat()
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
            space_data["summary"] = f"Planetary Kp reached {kp_val}. High-latitude power fluctuations and expanded aurora visibility."
            news_feed.append({
                "headline": f"Geomagnetic Disturbance: Planetary Kp Index at {kp_val}",
                "summary": "Heightened solar wind pressure compressing Earth's magnetic shield. Power grid and high-latitude aviation monitoring advised.",
                "level": "escalate" if kp_val >= 6.0 else "watch",
                "kind": "Geomagnetic Storm",
                "time": datetime.now(timezone.utc).isoformat()
            })

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
            headline = p.get("headline") or evt
            area = p.get("areaDesc", "")
            onset = p.get("onset")
            
            if severity in ["Extreme", "Severe"] or any(k in evt.lower() for k in ["tornado", "flash flood", "storm", "blizzard"]):
                item = {
                    "headline": f"{evt}: {area}",
                    "summary": f"Official severe warning issued for {area}. Take precautions against localized inundation and wind damage.",
                    "level": "escalate" if severity == "Extreme" else "alert",
                    "kind": "Severe Weather",
                    "time": onset
                }
                severe_stories.append(item)
                news_feed.append(item)

    # Deduplicate & Sort News
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
    return compiled

@app.get("/api/intel")
async def get_intel():
    if not CACHE["data"] or (time.time() - CACHE["last_collected"] > 60):
        return await run_collector()
    return CACHE["data"]

# Multi-hazard radius scanner (Earthquakes, Floods, Fires, Landslides)
@app.get("/api/check-radius")
async def check_radius(
    lat: float = Query(..., description="Latitude"),
    lon: float = Query(..., description="Longitude"),
    radius_km: float = Query(300.0, description="Radius in km")
):
    intel = await get_intel()
    threats = []
    
    # 1. Seismic
    all_quakes = intel["quakes"]["south_asia"] + intel["quakes"]["global"]
    for q in all_quakes:
        if q.get("latitude") and q.get("longitude"):
            dist = haversine_km(lat, lon, q["latitude"], q["longitude"])
            if dist <= radius_km:
                threats.append({
                    "type": "Seismic Ground Shaking",
                    "title": f"M{q['magnitude']} Tremor — {q['place']}",
                    "place": q["place"],
                    "distance_km": round(dist, 1),
                    "depth_km": q.get("depth_km"),
                    "severity": q["level"],
                    "time": q["time"]
                })

    # 2. GDACS Hazards
    for g in intel.get("lookout_news", []):
        if g.get("latitude") and g.get("longitude"):
            dist = haversine_km(lat, lon, g["latitude"], g["longitude"])
            if dist <= radius_km:
                threats.append({
                    "type": g.get("kind", "Environmental Crisis"),
                    "title": g.get("headline"),
                    "place": g.get("summary", "Active Alert Area"),
                    "distance_km": round(dist, 1),
                    "severity": g.get("level", "alert"),
                    "time": g.get("time")
                })

    # 3. Verified Field Reports
    for r in intel.get("crowd_reports", []):
        if r.get("latitude") and r.get("longitude"):
            dist = haversine_km(lat, lon, r["latitude"], r["longitude"])
            if dist <= radius_km:
                threats.append({
                    "type": f"Field Dispatch: {r.get('type')}",
                    "title": r.get("title"),
                    "place": r.get("location"),
                    "distance_km": round(dist, 1),
                    "severity": "escalate" if "Flood" in r.get("type", "") or "Landslide" in r.get("type", "") else "alert",
                    "time": r.get("timestamp")
                })

    threats.sort(key=lambda x: x["distance_km"])
    return {
        "coordinates": {"lat": lat, "lon": lon},
        "radius_km": radius_km,
        "threat_count": len(threats),
        "threats": threats,
        "risk_level": "CRITICAL" if any(t["severity"] == "escalate" for t in threats) else ("ELEVATED" if threats else "SECURE")
    }

@app.post("/api/crowd/report")
async def submit_crowd_report(
    title: str = Form(...),
    location: str = Form(...),
    report_type: str = Form(...),
    details: str = Form(...),
    file: UploadFile = File(None)
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
        "id": f"rep-{int(time.time()*1000)}",
        "title": title.strip(),
        "location": location.strip(),
        "author": "Citizen Field Scout",
        "timestamp": datetime.now(timezone.utc).strftime("%d %b %H:%M UTC"),
        "type": report_type.strip(),
        "details": details.strip(),
        "media_url": media_path,
        "approved": False
    }
    reports.insert(0, new_report)
    save_reports(reports)
    return {"status": "success", "message": "Report submitted. It will be broadcast publicly once verified by admin."}

@app.get("/api/admin/reports")
async def admin_get_reports(passkey: str = Query(...)):
    if passkey != ADMIN_PASSKEY:
        raise HTTPException(status_code=403, detail="Invalid admin passkey.")
    return load_reports()

@app.post("/api/admin/moderate")
async def admin_moderate_report(
    report_id: str = Form(...),
    action: str = Form(...),
    passkey: str = Form(...)
):
    if passkey != ADMIN_PASSKEY:
        raise HTTPException(status_code=403, detail="Invalid admin passkey.")
    
    reports = load_reports()
    if action == "approve":
        for r in reports:
            if r["id"] == report_id:
                r["approved"] = True
                break
    elif action == "delete":
        reports = [r for r in reports if r["id"] != report_id]

    save_reports(reports)
    return {"status": "success", "action": action, "report_id": report_id}

@app.get("/api/report/pdf")
async def generate_executive_pdf(title: str = "Daily Macro Disruption Brief"):
    intel = await get_intel()
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle('MainTitle', parent=styles['Heading1'], fontSize=18, leading=22, textColor=colors.HexColor("#0f172a"), fontName="Helvetica-Bold")
    h2_style = ParagraphStyle('SectionH2', parent=styles['Heading2'], fontSize=11, leading=15, textColor=colors.HexColor("#2563eb"), spaceBefore=10, spaceAfter=4, fontName="Helvetica-Bold")
    body_style = ParagraphStyle('BodyTextCustom', parent=styles['Normal'], fontSize=8.5, leading=12, textColor=colors.HexColor("#334155"))
    meta_style = ParagraphStyle('MetaStyle', parent=styles['Normal'], fontSize=7.5, leading=9, textColor=colors.HexColor("#64748b"))

    story = []
    story.append(Paragraph("THE BRINK WORLD // EXECUTIVE INTELLIGENCE DOSSIER", meta_style))
    story.append(Paragraph(title, title_style))
    story.append(Paragraph(f"Evaluated: {intel['evaluated_at']} UTC", meta_style))
    story.append(Spacer(1, 10))

    sit = intel["situation"]
    summary_data = [
        ["CRITICAL (ESCALATE)", "SEVERE ALERTS", "ELEVATED WATCH", "SPACE WX (KP)"],
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
    story.append(Spacer(1, 10))

    story.append(Paragraph("1. Priority News & Compound Risk Briefs", h2_style))
    if intel["lookout_news"]:
        for item in intel["lookout_news"][:6]:
            story.append(Paragraph(f"• <b>[{item['kind'].upper()}] {item['headline']}</b>", body_style))
            story.append(Paragraph(f"  {item['summary']}", meta_style))
            story.append(Spacer(1, 3))
    else:
        story.append(Paragraph("No critical emergency hazards active in this telemetry cycle.", body_style))

    doc.build(story)
    buffer.seek(0)
    
    return Response(
        content=buffer.getvalue(), 
        media_type="application/pdf", 
        headers={"Content-Disposition": f"attachment; filename=thebrink-brief-{int(time.time())}.pdf"}
    )

if UPLOADS_DIR.exists():
    app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Supports Render automated HEAD/GET health check without throwing 405/404
@app.api_route("/", methods=["GET", "HEAD"])
async def root_handler():
    if INDEX_FILE.exists():
        return FileResponse(str(INDEX_FILE), media_type="text/html")
    alt_index = BASE_DIR / "index.html"
    if alt_index.exists():
        return FileResponse(str(alt_index), media_type="text/html")
    return JSONResponse(
        content={
            "status": "online",
            "service": "The Brink Hazard Engine",
            "evaluated_at": datetime.now(timezone.utc).isoformat()
        },
        status_code=200
    )

@app.api_route("/healthz", methods=["GET", "HEAD"])
async def health_check():
    return JSONResponse(content={"status": "healthy"}, status_code=200)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8080, reload=True)