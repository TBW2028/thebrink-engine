import os
import requests
from datetime import datetime, timezone
from supabase import create_client, Client
from dotenv import load_dotenv

# Load secrets from the .env file
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_KEY:
    raise ValueError("Missing SUPABASE_SERVICE_ROLE_KEY. Check your .env file.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
headers = {"User-Agent": "TheBrinkEngine/4.0 (contact@thebrinkworld.com)"}

def fetch_gdacs_category(endpoint_code, category_name):
    """Generic fetcher for GDACS specific endpoints like Floods (FL), Droughts (DR)."""
    events = []
    print(f"Fetching {category_name} events from GDACS ({endpoint_code})...")
    try:
        r = requests.get(f"https://www.gdacs.org/datareport/resources/{endpoint_code}/events.geojson", headers=headers, timeout=15)
        print(f"GDACS {endpoint_code}: HTTP {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            for f in data.get("features", []):
                p = f.get("properties", {})
                coords = f.get("geometry", {}).get("coordinates", [])
                if not coords or len(coords) < 2: 
                    continue
                lon, lat = float(coords[0]), float(coords[1])
                name = p.get("eventname") or p.get("name") or f"Active {category_name}"
                
                # Do not invent alert levels; use what GDACS gives, or default to Unrated
                alert_level = p.get("alertlevel", "Unrated").capitalize()
                
                events.append({
                    "id": f"GDACS_{endpoint_code}_{p.get('eventid', name.replace(' ', ''))}",
                    "category": category_name.lower(),
                    "name": name[:100],
                    "basin": "GLOBAL",
                    "intensity": f"GDACS {alert_level} Alert",
                    "wind_kts": None,
                    "pressure_mb": None,
                    "latitude": lat,
                    "longitude": lon,
                    "alert_level": alert_level,
                    "source": "GDACS",
                    "observed_at": p.get("todate", p.get("fromdate", datetime.now(timezone.utc).isoformat())),
                    "updated_at": datetime.now(timezone.utc).isoformat()
                })
            print(f"-> Parsed {len(events)} events.")
    except Exception as e:
        print(f"[ERROR] GDACS {category_name}: {e}")
    return events

def fetch_and_publish_cyclones():
    events = []
    print("Fetching active tropical cyclones from GDACS...")
    try:
        r = requests.get("https://www.gdacs.org/datareport/resources/TC/events.geojson", headers=headers, timeout=15)
        print(f"GDACS TC: HTTP {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            for f in data.get("features", []):
                p = f.get("properties", {})
                coords = f.get("geometry", {}).get("coordinates", [])
                if not coords or len(coords) < 2: 
                    continue
                lon, lat = float(coords[0]), float(coords[1])
                name = p.get("eventname") or p.get("name") or "Tropical System"
                basin = (p.get("basin") or "GLOBAL").upper()

                alert_level = p.get("alertlevel", "Unrated").capitalize()
                
                # Extract windspeed safely from either top-level or nested severitydata
                severity_data = p.get("severitydata", {})
                raw_wind = p.get("windspeed") or severity_data.get("severity")
                wind_kts = None
                if raw_wind:
                    try:
                        val = float(raw_wind)
                        if val > 0:
                            # Convert km/h to knots if dynamically required by large threshold
                            wind_kts = round(val * 0.539957) if val > 150 else round(val)
                    except (ValueError, TypeError):
                        pass
                
                events.append({
                    "id": f"GDACS_TC_{p.get('eventid', name.replace(' ', ''))}",
                    "category": "cyclone",
                    "name": name[:100],
                    "basin": basin,
                    "intensity": f"GDACS {alert_level} Alert",
                    "wind_kts": wind_kts,
                    "pressure_mb": float(p.get("pressure", 0)) if p.get("pressure") else None,
                    "latitude": lat,
                    "longitude": lon,
                    "alert_level": alert_level,
                    "source": "GDACS / RSMC",
                    "observed_at": p.get("todate", datetime.now(timezone.utc).isoformat()),
                    "updated_at": datetime.now(timezone.utc).isoformat()
                })
            print(f"-> Parsed {len(events)} events.")
    except Exception as e:
        print(f"[ERROR] GDACS Cyclones: {e}")
    return events

def fetch_and_publish_volcanoes():
    events = []
    print("Fetching active volcanic alerts from GDACS...")
    try:
        r = requests.get("https://www.gdacs.org/datareport/resources/VO/events.geojson", headers=headers, timeout=15)
        print(f"GDACS VO: HTTP {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            for f in data.get("features", []):
                p = f.get("properties", {})
                coords = f.get("geometry", {}).get("coordinates", [])
                if not coords or len(coords) < 2: 
                    continue
                
                alert_level = p.get("alertlevel", "Unrated").capitalize()
                obs_date = p.get("todate") or p.get("fromdate") or datetime.now(timezone.utc).isoformat()

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
                    "source": "GDACS / GVP",
                    "observed_at": obs_date,
                    "updated_at": datetime.now(timezone.utc).isoformat()
                })
            print(f"-> Parsed {len(events)} events.")
    except Exception as e:
        print(f"[ERROR] GDACS Volcanoes: {e}")
    return events

def fetch_reliefweb_disasters():
    """Captures major regional floods and crises (e.g., Thailand) from ReliefWeb API v1."""
    events = []
    print("Fetching global humanitarian crises from ReliefWeb...")
    try:
        url = "https://api.reliefweb.int/v1/disasters?appname=thebrinkworld&filter[field]=status&filter[value]=current&limit=25&profile=full"
        r = requests.get(url, timeout=15)
        print(f"ReliefWeb: HTTP {r.status_code}")
        if r.status_code == 200:
            for d in r.json().get("data", []):
                f = d.get("fields", {})
                name = f.get("name", "Active Crisis")
                types = [t.get("name") for t in f.get("type", [])]
                
                cat = "extreme"
                if any("Flood" in t for t in types): cat = "flood"
                elif any("Wildfire" in t or "Fire" in t for t in types): cat = "wildfire"
                elif any("Earthquake" in t for t in types): cat = "quake"
                elif any("Cyclone" in t or "Storm" in t for t in types): cat = "storm"
                
                country = f.get("primary_country", {})
                lat = country.get("location", {}).get("lat")
                lon = country.get("location", {}).get("lon")
                
                if lat is not None and lon is not None:
                    events.append({
                        "id": f"RW_{d.get('id')}",
                        "category": cat,
                        "name": name[:100],
                        "basin": "GLOBAL",
                        "intensity": "Active Crisis Declaration",
                        "wind_kts": None,
                        "pressure_mb": None,
                        "latitude": float(lat),
                        "longitude": float(lon),
                        "alert_level": "Red",
                        "source": "ReliefWeb",
                        "observed_at": f.get("date", {}).get("created", datetime.now(timezone.utc).isoformat()),
                        "updated_at": datetime.now(timezone.utc).isoformat()
                    })
            print(f"-> Parsed {len(events)} events.")
    except Exception as e:
        print(f"[ERROR] ReliefWeb: {e}")
    return events

def fetch_eonet_hazards():
    raw_events = []
    print("Fetching global physical events from NASA EONET...")
    try:
        r = requests.get("https://eonet.gsfc.nasa.gov/api/v3/events?status=open&days=7", headers=headers, timeout=15)
        print(f"NASA EONET: HTTP {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            for event in data.get("events", []):
                categories = [c.get("id") for c in event.get("categories", [])]
                
                is_storm = "severeStorms" in categories
                is_volcano = "volcanoes" in categories
                is_wildfire = "wildfires" in categories
                is_flood = "floods" in categories
                is_extreme = "temperatureExtremes" in categories or "drought" in categories
                
                if not (is_storm or is_volcano or is_wildfire or is_flood or is_extreme):
                    continue
                    
                geom = event.get("geometry", [])
                if not geom:
                    continue
                
                latest = geom[-1]
                coords = latest.get("coordinates")
                geom_type = latest.get("type", "Point")
                
                try:
                    if geom_type == "Polygon":
                        lon, lat = float(coords[0][0][0]), float(coords[0][0][1])
                    else:
                        lon, lat = float(coords[0]), float(coords[1])
                except (IndexError, TypeError):
                    continue

                wind_kts = None
                if is_storm:
                    mag_val = latest.get("magnitudeValue")
                    mag_unit = latest.get("magnitudeUnit")
                    if mag_val is not None:
                        try:
                            val = float(mag_val)
                            if mag_unit == "kts": wind_kts = val
                            elif mag_unit == "mph": wind_kts = val * 0.868976
                            elif mag_unit == "km/h": wind_kts = val * 0.539957
                            else: wind_kts = val
                            wind_kts = round(wind_kts)
                        except (ValueError, TypeError):
                            pass
                    
                if is_storm: category_str = "cyclone"
                elif is_volcano: category_str = "volcano"
                elif is_wildfire: category_str = "wildfire"
                elif is_flood: category_str = "flood"
                else: category_str = "extreme"
                
                name = event.get("title", "Unknown Event")
                
                raw_events.append({
                    "id": f"EONET_{event.get('id')}",
                    "category": category_str,
                    "name": name[:100],
                    "basin": "GLOBAL",
                    "intensity": "Active Weather System" if is_storm else "NASA Active Telemetry",
                    "wind_kts": wind_kts,
                    "pressure_mb": None,
                    "latitude": lat,
                    "longitude": lon,
                    "alert_level": "Unrated",
                    "source": "NASA EONET",
                    "observed_at": latest.get("date", datetime.now(timezone.utc).isoformat()),
                    "updated_at": datetime.now(timezone.utc).isoformat()
                })
            
            # De-duplication: Drop EONET cyclones/volcanoes to prefer GDACS
            raw_events = [e for e in raw_events if e["category"] not in ["cyclone", "volcano"]]
            
            wildfires = [e for e in raw_events if e["category"] == "wildfire"]
            others = [e for e in raw_events if e["category"] != "wildfire"]
            
            wildfires.sort(key=lambda x: x["observed_at"], reverse=True)
            capped_wildfires = wildfires[:20] 
            
            final_events = others + capped_wildfires
            print(f"-> Parsed {len(final_events)} events (after de-dupe and capping).")
            return final_events
    except Exception as e:
        print(f"[ERROR] NASA EONET: {e}")
    return []

def fetch_and_publish_earthquakes():
    events = []
    print("Fetching recent M4.5+ earthquakes from USGS...")
    try:
        r = requests.get("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson", headers=headers, timeout=15)
        print(f"USGS Earthquakes: HTTP {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            for f in data.get("features", []):
                p = f.get("properties", {})
                coords = f.get("geometry", {}).get("coordinates", [])
                if not coords or len(coords) < 3: 
                    continue

                mag_val = p.get("mag")
                if mag_val is None:
                    continue
                try:
                    mag = float(mag_val)
                except ValueError:
                    continue

                alert = p.get("alert") or "Unrated"

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
                    "alert_level": alert.capitalize(),
                    "source": "USGS",
                    "observed_at": datetime.fromtimestamp(p.get("time", 0) / 1000.0, tz=timezone.utc).isoformat(),
                    "updated_at": datetime.now(timezone.utc).isoformat()
                })
            print(f"-> Parsed {len(events)} events.")
    except Exception as e:
        print(f"[ERROR] USGS Earthquakes: {e}")
    return events

def fetch_nws_alerts():
    events = []
    print("Fetching severe alerts from US NWS...")
    try:
        url = "https://api.weather.gov/alerts/active?severity=Severe,Extreme,Moderate"
        r = requests.get(url, headers=headers, timeout=15)
        print(f"NWS Alerts: HTTP {r.status_code}")
        if r.status_code == 200:
            data = r.json()
            for f in data.get("features", []):
                p = f.get("properties", {})
                event_type = p.get("event", "")
                
                # Strict filtering to prevent alert bloat
                if not any(x in event_type for x in ["Flood", "Storm", "Tornado", "Hurricane", "Blizzard", "Nor'easter", "Wildfire", "Fire"]):
                    continue
                
                if "Flood" in event_type: cat = "flood"
                elif "Fire" in event_type or "Wildfire" in event_type: cat = "wildfire"
                elif any(x in event_type for x in ["Storm", "Tornado", "Hurricane", "Blizzard", "Nor'easter"]): cat = "storm"
                else: continue
                
                geom = f.get("geometry")
                if not geom:
                    continue
                
                coords = geom.get("coordinates", [])
                
                def get_pts(c):
                    pts = []
                    if not isinstance(c, list): return pts
                    if len(c) == 2 and isinstance(c[0], (int, float)) and isinstance(c[1], (int, float)):
                        pts.append(c)
                    else:
                        for item in c:
                            pts.extend(get_pts(item))
                    return pts
                
                pts = get_pts(coords)
                if not pts:
                    continue
                
                # Derive reliable polygon centroids mathematically
                lon = sum(p[0] for p in pts) / len(pts)
                lat = sum(p[1] for p in pts) / len(pts)
                
                severity = p.get("severity", "Moderate")
                
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
                    "source": "US NWS",
                    "observed_at": p.get("effective", datetime.now(timezone.utc).isoformat()),
                    "updated_at": datetime.now(timezone.utc).isoformat()
                })
            print(f"-> Parsed {len(events)} events.")
    except Exception as e:
        print(f"[ERROR] NWS Alerts: {e}")
    return events

def run_ingestion_cycle():
    cycle_start = datetime.now(timezone.utc)
    print(f"--- Starting Brink Ingestion Cycle at {cycle_start.isoformat()} ---")
    
    all_events = []
    sources_that_succeeded = set()

    def load_source(func, source_name):
        events = func()
        if events is not None:
            all_events.extend(events)
            sources_that_succeeded.add(source_name)

    # Core Planetary Vectors
    load_source(fetch_and_publish_cyclones, "GDACS / RSMC")
    load_source(fetch_and_publish_volcanoes, "GDACS / GVP")
    load_source(lambda: fetch_gdacs_category("FL", "flood"), "GDACS")
    load_source(lambda: fetch_gdacs_category("DR", "extreme"), "GDACS")
    
    load_source(fetch_eonet_hazards, "NASA EONET")
    load_source(fetch_and_publish_earthquakes, "USGS")
    
    # Regional Vectors
    load_source(fetch_reliefweb_disasters, "ReliefWeb")
    load_source(fetch_nws_alerts, "US NWS")

    if not all_events:
        print("No events captured. Exiting safely.")
        return

    # De-duplicate strictly by ID mapping before pushing to prevent Batch Collision crashes
    unique_events_dict = {e["id"]: e for e in all_events}
    unique_events = list(unique_events_dict.values())

    upsert_ok = False
    try:
        supabase.table("live_hazards").upsert(unique_events).execute()
        upsert_ok = True
        print(f"✓ Successfully upserted {len(unique_events)} clean records to Supabase.")
    except Exception as e:
        print(f"❌ Supabase Upsert Failed: {e}")

    # Immediate Pruning: Run safety bounds only on valid runs to protect intact historical feeds
    if upsert_ok and sources_that_succeeded:
        try:
            supabase.table("live_hazards") \
                .delete() \
                .in_("source", list(sources_that_succeeded)) \
                .lt("updated_at", cycle_start.isoformat()) \
                .execute()
            print(f"✓ Pruned stale/dissipated hazards for sources: {', '.join(sources_that_succeeded)}")
        except Exception as e:
            print(f"❌ Failed to prune old hazards: {e}")

if __name__ == "__main__":
    run_ingestion_cycle()