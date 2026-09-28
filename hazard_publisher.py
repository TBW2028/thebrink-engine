import os
import requests
from datetime import datetime, timezone
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_KEY:
    raise ValueError("Missing SUPABASE_SERVICE_ROLE_KEY.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
headers = {"User-Agent": "TheBrinkEngine/4.0 (contact@thebrinkworld.com)"}

def fetch_gdacs_category(endpoint_code, category_name):
    events = []
    try:
        r = requests.get(f"https://www.gdacs.org/datareport/resources/{endpoint_code}/events.geojson", headers=headers, timeout=15)
        if r.status_code == 200:
            for f in r.json().get("features", []):
                p = f.get("properties", {})
                coords = f.get("geometry", {}).get("coordinates", [])
                if not coords or len(coords) < 2: continue
                
                alert_level = p.get("alertlevel", "Unrated").capitalize()
                tier = "Alert" if alert_level in ["Red", "Orange"] else "Monitor"
                name = p.get("eventname") or p.get("name") or f"Active {category_name}"
                
                events.append({
                    "id": f"GDACS_{endpoint_code}_{p.get('eventid', name.replace(' ', ''))}",
                    "category": category_name.lower(),
                    "name": name[:100],
                    "basin": "GLOBAL",
                    "intensity": f"GDACS {alert_level} Alert",
                    "wind_kts": None,
                    "pressure_mb": None,
                    "latitude": float(coords[1]),
                    "longitude": float(coords[0]),
                    "alert_level": alert_level,
                    "magnitude": None,
                    "severity_tier": tier,
                    "source": "GDACS",
                    "observed_at": p.get("todate", p.get("fromdate", datetime.now(timezone.utc).isoformat())),
                    "updated_at": datetime.now(timezone.utc).isoformat()
                })
    except Exception as e:
        print(f"[ERROR] GDACS {category_name}: {e}")
    return events

def fetch_and_publish_cyclones():
    events = []
    try:
        r = requests.get("https://www.gdacs.org/datareport/resources/TC/events.geojson", headers=headers, timeout=15)
        if r.status_code == 200:
            for f in r.json().get("features", []):
                p = f.get("properties", {})
                coords = f.get("geometry", {}).get("coordinates", [])
                if not coords or len(coords) < 2: continue
                
                # Extract wind correctly from severitydata
                raw_wind = p.get("severitydata", {}).get("severity") or p.get("windspeed")
                wind_kts = None
                tier = "Monitor"
                if raw_wind:
                    try:
                        val = float(raw_wind)
                        wind_kts = round(val * 0.539957) if val > 150 else round(val)
                        tier = "Escalate" if wind_kts >= 96 else "Alert" if wind_kts >= 64 else "Monitor"
                    except: pass
                
                name = p.get("eventname") or p.get("name") or "Tropical System"
                events.append({
                    "id": f"GDACS_TC_{p.get('eventid', name.replace(' ', ''))}",
                    "category": "cyclone",
                    "name": name[:100],
                    "basin": (p.get("basin") or "GLOBAL").upper(),
                    "intensity": f"GDACS {p.get('alertlevel', 'Unrated').capitalize()} Alert",
                    "wind_kts": wind_kts,
                    "pressure_mb": float(p.get("pressure", 0)) if p.get("pressure") else None,
                    "latitude": float(coords[1]),
                    "longitude": float(coords[0]),
                    "alert_level": p.get("alertlevel", "Unrated").capitalize(),
                    "magnitude": None,
                    "severity_tier": tier,
                    "source": "GDACS / RSMC",
                    "observed_at": p.get("todate", datetime.now(timezone.utc).isoformat()),
                    "updated_at": datetime.now(timezone.utc).isoformat()
                })
    except Exception as e:
        print(f"[ERROR] GDACS Cyclones: {e}")
    return events

def fetch_and_publish_volcanoes():
    events = []
    try:
        r = requests.get("https://www.gdacs.org/datareport/resources/VO/events.geojson", headers=headers, timeout=15)
        if r.status_code == 200:
            for f in r.json().get("features", []):
                p = f.get("properties", {})
                coords = f.get("geometry", {}).get("coordinates", [])
                if not coords or len(coords) < 2: continue
                
                alert_level = p.get("alertlevel", "Unrated").capitalize()
                tier = "Alert" if alert_level in ["Red", "Orange"] else "Monitor"

                events.append({
                    "id": f"GDACS_VOLC_{p.get('eventid', p.get('name', 'Unknown'))}",
                    "category": "volcano",
                    "name": (p.get("eventname") or p.get("name") or "Volcano")[:100],
                    "basin": "TERRESTRIAL",
                    "intensity": f"Volcanic Alert: {alert_level}",
                    "wind_kts": None,
                    "pressure_mb": None,
                    "latitude": float(coords[1]),
                    "longitude": float(coords[0]),
                    "alert_level": alert_level,
                    "magnitude": None,
                    "severity_tier": tier,
                    "source": "GDACS / GVP",
                    "observed_at": p.get("todate", p.get("fromdate", datetime.now(timezone.utc).isoformat())),
                    "updated_at": datetime.now(timezone.utc).isoformat()
                })
    except Exception as e:
        print(f"[ERROR] GDACS Volcanoes: {e}")
    return events

def fetch_eonet_hazards():
    """Fetches NASA EONET, explicitly skipping storms and volcanoes to prevent double-counting GDACS."""
    raw_events = []
    try:
        r = requests.get("https://eonet.gsfc.nasa.gov/api/v3/events?status=open&days=7", headers=headers, timeout=15)
        if r.status_code == 200:
            for event in r.json().get("events", []):
                categories = [c.get("id") for c in event.get("categories", [])]
                
                # Deduplication: We only want EONET for wildfires and extremes
                if "wildfires" in categories: cat = "wildfire"
                elif "temperatureExtremes" in categories or "drought" in categories: cat = "extreme"
                else: continue
                    
                geom = event.get("geometry", [])
                if not geom: continue
                
                coords = geom[-1].get("coordinates")
                try:
                    if geom[-1].get("type", "Point") == "Polygon": lon, lat = float(coords[0][0][0]), float(coords[0][0][1])
                    else: lon, lat = float(coords[0]), float(coords[1])
                except: continue
                
                raw_events.append({
                    "id": f"EONET_{event.get('id')}",
                    "category": cat,
                    "name": event.get("title", "Unknown Event")[:100],
                    "basin": "GLOBAL",
                    "intensity": "NASA Active Telemetry",
                    "wind_kts": None,
                    "pressure_mb": None,
                    "latitude": lat,
                    "longitude": lon,
                    "alert_level": "Unrated",
                    "magnitude": None,
                    "severity_tier": "Monitor",
                    "source": "NASA EONET",
                    "observed_at": geom[-1].get("date", datetime.now(timezone.utc).isoformat()),
                    "updated_at": datetime.now(timezone.utc).isoformat()
                })
            
            wildfires = [e for e in raw_events if e["category"] == "wildfire"]
            others = [e for e in raw_events if e["category"] != "wildfire"]
            
            wildfires.sort(key=lambda x: x["observed_at"], reverse=True)
            return others + wildfires[:20] 
    except Exception as e:
        print(f"[ERROR] NASA EONET: {e}")
    return []

def fetch_and_publish_earthquakes():
    events = []
    try:
        r = requests.get("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson", headers=headers, timeout=15)
        if r.status_code == 200:
            for f in r.json().get("features", []):
                p = f.get("properties", {})
                coords = f.get("geometry", {}).get("coordinates", [])
                if not coords or len(coords) < 3: continue

                try:
                    mag = float(p.get("mag"))
                except (ValueError, TypeError):
                    continue

                tier = "Escalate" if mag >= 6.0 else "Alert" if mag >= 5.0 else "Monitor"

                events.append({
                    "id": f"EQ_{f.get('id')}",
                    "category": "earthquake",
                    "name": p.get("place", "Unknown Fault")[:100],
                    "basin": "LITHOSPHERIC",
                    "intensity": f"Magnitude {mag}",
                    "wind_kts": None,
                    "pressure_mb": None,
                    "latitude": float(coords[1]),
                    "longitude": float(coords[0]),
                    "alert_level": p.get("alert", "Unrated").capitalize() if p.get("alert") else "Unrated",
                    "magnitude": mag,
                    "severity_tier": tier,
                    "source": "USGS",
                    "observed_at": datetime.fromtimestamp(p.get("time", 0) / 1000.0, tz=timezone.utc).isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat()
                })
    except Exception as e:
        print(f"[ERROR] USGS Earthquakes: {e}")
    return events

def fetch_nws_alerts():
    events = []
    try:
        url = "https://api.weather.gov/alerts/active?severity=Severe,Extreme,Moderate"
        r = requests.get(url, headers=headers, timeout=15)
        if r.status_code == 200:
            for f in r.json().get("features", []):
                p = f.get("properties", {})
                event_type = p.get("event", "")
                
                # Strict Severity Gating by exact Event strings
                if not any(x in event_type for x in ["Coastal Flood", "Flash Flood", "Flood", "Storm", "Tornado", "Hurricane", "Typhoon", "Blizzard", "Winter Storm", "Nor'easter", "Wildfire", "Fire", "Heat", "Drought"]):
                    continue
                
                if "Flood" in event_type: cat = "flood"
                elif "Fire" in event_type or "Wildfire" in event_type: cat = "wildfire"
                elif any(x in event_type for x in ["Storm", "Tornado", "Hurricane", "Typhoon", "Blizzard", "Nor'easter", "Winter Storm"]): cat = "storm"
                elif "Heat" in event_type or "Drought" in event_type: cat = "extreme"
                else: continue
                
                geom = f.get("geometry")
                if not geom: continue
                coords = geom.get("coordinates", [])
                
                def get_pts(c):
                    pts = []
                    if not isinstance(c, list): return pts
                    if len(c) == 2 and isinstance(c[0], (int, float)) and isinstance(c[1], (int, float)):
                        pts.append(c)
                    else:
                        for item in c: pts.extend(get_pts(item))
                    return pts
                
                pts = get_pts(coords)
                if not pts: continue
                
                lon = sum(pt[0] for pt in pts) / len(pts)
                lat = sum(pt[1] for pt in pts) / len(pts)
                
                severity = p.get("severity", "Moderate")
                tier = "Escalate" if severity == "Extreme" else "Alert" if severity == "Severe" else "Monitor"
                
                events.append({
                    "id": f"NWS_{p.get('id', '')[-32:]}",
                    "category": cat,
                    "name": p.get("headline", event_type).split("\n")[0][:100],
                    "basin": "NORTH_AMERICA",
                    "intensity": event_type,
                    "wind_kts": None,
                    "pressure_mb": None,
                    "latitude": lat,
                    "longitude": lon,
                    "alert_level": severity,
                    "magnitude": None,
                    "severity_tier": tier,
                    "source": "US NWS",
                    "observed_at": p.get("effective", datetime.now(timezone.utc).isoformat()),
                    "updated_at": datetime.now(timezone.utc).isoformat()
                })
    except Exception as e:
        print(f"[ERROR] NWS Alerts: {e}")
    return events

def run_ingestion_cycle():
    cycle_start = datetime.now(timezone.utc)
    print(f"--- Starting Brink Ingestion Cycle at {cycle_start.isoformat()} ---")
    
    all_events = []
    sources_that_succeeded = set()

    def load_source(func, source_name):
        print(f"Executing {source_name}...")
        events = func()
        if events is not None and len(events) > 0:
            all_events.extend(events)
            sources_that_succeeded.add(source_name)

    load_source(fetch_and_publish_cyclones, "GDACS / RSMC")
    load_source(fetch_and_publish_volcanoes, "GDACS / GVP")
    load_source(lambda: fetch_gdacs_category("FL", "flood"), "GDACS")
    load_source(lambda: fetch_gdacs_category("DR", "extreme"), "GDACS")
    load_source(fetch_eonet_hazards, "NASA EONET")
    load_source(fetch_and_publish_earthquakes, "USGS")
    load_source(fetch_nws_alerts, "US NWS")

    if not all_events:
        print("No events captured. Exiting safely.")
        return

    # In-memory ID Deduplication
    unique_events = list({e["id"]: e for e in all_events}.values())

    upsert_ok = False
    try:
        supabase.table("live_hazards").upsert(unique_events).execute()
        upsert_ok = True
        print(f"✓ Upserted {len(unique_events)} records.")
    except Exception as e:
        print(f"❌ Upsert Failed: {e}")

    # Safe Source-Isolated Pruning
    if upsert_ok and sources_that_succeeded:
        try:
            supabase.table("live_hazards") \
                .delete() \
                .in_("source", list(sources_that_succeeded)) \
                .lt("updated_at", cycle_start.isoformat()) \
                .execute()
            print(f"✓ Pruned stale records strictly for: {', '.join(sources_that_succeeded)}")
        except Exception as e:
            print(f"❌ Failed to prune: {e}")

if __name__ == "__main__":
    run_ingestion_cycle()