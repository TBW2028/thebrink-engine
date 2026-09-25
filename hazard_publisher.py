import os
import requests
from datetime import datetime, timezone, timedelta
from supabase import create_client, Client
from dotenv import load_dotenv

# Load secrets from the .env file
load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

if not SUPABASE_KEY:
    raise ValueError("Missing SUPABASE_SERVICE_ROLE_KEY. Check your .env file.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
headers = {"User-Agent": "TheBrinkEngine/2.0 (Planetary Ingestion Pipeline)"}

def fetch_and_publish_cyclones():
    """Fetches global tropical cyclones from GDACS (Humanitarian threats)."""
    events = []
    print("Fetching active tropical cyclones from GDACS...")
    try:
        r = requests.get("https://www.gdacs.org/datareport/resources/TC/events.geojson", headers=headers, timeout=15)
        if r.status_code == 200:
            data = r.json()
            for f in data.get("features", []):
                p = f.get("properties", {})
                coords = f.get("geometry", {}).get("coordinates", [])
                if len(coords) < 2: continue
                lon, lat = float(coords[0]), float(coords[1])
                name = p.get("eventname") or p.get("name") or "Tropical System"
                basin = (p.get("basin") or "GLOBAL").upper()

                if basin in ["EP", "NA", "AL", "CP"] and lon > 0:
                    lon = -lon

                alert_level = p.get("alertlevel", "Green").capitalize()
                
                events.append({
                    "id": f"GDACS_TC_{p.get('eventid', name.replace(' ', ''))}",
                    "category": "cyclone",
                    "name": name,
                    "basin": basin,
                    "intensity": f"GDACS {alert_level} Alert",
                    "wind_kts": float(p.get("windspeed", 0)),
                    "pressure_mb": float(p.get("pressure", 0)) if p.get("pressure") else None,
                    "latitude": lat,
                    "longitude": lon,
                    "alert_level": alert_level,
                    "source": "GDACS / RSMC",
                    "observed_at": p.get("todate", datetime.now(timezone.utc).isoformat()),
                    "updated_at": datetime.now(timezone.utc).isoformat()
                })
    except Exception as e:
        print(f"[ERROR] GDACS Cyclones: {e}")
    return events

def fetch_and_publish_volcanoes():
    """Fetches volcanic alerts from GDACS."""
    events = []
    print("Fetching active volcanic alerts from GDACS...")
    try:
        r = requests.get("https://www.gdacs.org/datareport/resources/VO/events.geojson", headers=headers, timeout=15)
        if r.status_code == 200:
            data = r.json()
            for f in data.get("features", []):
                p = f.get("properties", {})
                coords = f.get("geometry", {}).get("coordinates", [])
                if len(coords) < 2: continue
                alert_level = p.get("alertlevel", "Green").capitalize()

                events.append({
                    "id": f"GDACS_VOLC_{p.get('eventid', p.get('name', 'Unknown'))}",
                    "category": "volcano",
                    "name": p.get("eventname") or p.get("name") or "Volcano",
                    "basin": "TERRESTRIAL",
                    "intensity": f"Volcanic Alert: {alert_level}",
                    "wind_kts": None,
                    "pressure_mb": None,
                    "latitude": float(coords[1]),
                    "longitude": float(coords[0]),
                    "alert_level": alert_level,
                    "source": "GDACS / GVP",
                    "observed_at": p.get("fromdate", datetime.now(timezone.utc).isoformat()),
                    "updated_at": datetime.now(timezone.utc).isoformat()
                })
    except Exception as e:
        print(f"[ERROR] GDACS Volcanoes: {e}")
    return events

def fetch_eonet_hazards():
    """Fetches physical planetary systems from NASA EONET (Open ocean storms & unrest)."""
    events = []
    print("Fetching global physical events from NASA EONET...")
    try:
        r = requests.get("https://eonet.gsfc.nasa.gov/api/v3/events?status=open", headers=headers, timeout=15)
        if r.status_code == 200:
            data = r.json()
            for event in data.get("events", []):
                categories = [c.get("id") for c in event.get("categories", [])]
                
                is_storm = "severeStorms" in categories
                is_volcano = "volcanoes" in categories
                
                if not (is_storm or is_volcano):
                    continue
                    
                geom = event.get("geometry", [])
                if not geom:
                    continue
                
                # Get the most recent position
                latest = geom[-1]
                coords = latest.get("coordinates")
                geom_type = latest.get("type", "Point")
                
                # Extract lat/lon whether NASA sent a single Point or a Polygon track
                try:
                    if geom_type == "Polygon":
                        lon, lat = float(coords[0][0][0]), float(coords[0][0][1])
                    else:
                        lon, lat = float(coords[0]), float(coords[1])
                except (IndexError, TypeError):
                    continue
                    
                category_str = "cyclone" if is_storm else "volcano"
                name = event.get("title", "Unknown Event")
                
                # We prefix with EONET_ so it merges cleanly into the database alongside GDACS
                events.append({
                    "id": f"EONET_{event.get('id')}",
                    "category": category_str,
                    "name": name,
                    "basin": "GLOBAL",
                    "intensity": "Active Weather System" if is_storm else "Active Volcanic Unrest",
                    "wind_kts": 0,
                    "pressure_mb": None,
                    "latitude": lat,
                    "longitude": lon,
                    "alert_level": "NASA Monitor",
                    "source": "NASA EONET",
                    "observed_at": latest.get("date", datetime.now(timezone.utc).isoformat()),
                    "updated_at": datetime.now(timezone.utc).isoformat()
                })
    except Exception as e:
        print(f"[ERROR] NASA EONET: {e}")
    return events

def fetch_and_publish_earthquakes():
    """Fetches global significant earthquakes (M4.5+) from USGS."""
    events = []
    print("Fetching recent M4.5+ earthquakes from USGS...")
    try:
        r = requests.get("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson", headers=headers, timeout=15)
        if r.status_code == 200:
            data = r.json()
            for f in data.get("features", []):
                p = f.get("properties", {})
                coords = f.get("geometry", {}).get("coordinates", [])
                if len(coords) < 3: continue

                mag = float(p.get("mag", 0))
                alert = p.get("alert") or ("Red" if mag >= 7.0 else "Orange" if mag >= 6.0 else "Yellow")

                events.append({
                    "id": f"EQ_{f.get('id')}",
                    "category": "earthquake",
                    "name": p.get("place", "Unknown Fault"),
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
    except Exception as e:
        print(f"[ERROR] USGS Earthquakes: {e}")
    return events

def run_ingestion_cycle():
    print(f"--- Starting Brink Ingestion Cycle at {datetime.now(timezone.utc).isoformat()} ---")
    
    all_events = []
    # Combine GDACS, NASA EONET, and USGS into one master payload
    all_events.extend(fetch_and_publish_cyclones())
    all_events.extend(fetch_and_publish_volcanoes())
    all_events.extend(fetch_eonet_hazards())
    all_events.extend(fetch_and_publish_earthquakes())

    if not all_events:
        print("No events captured. Exiting.")
        return

    # Upsert new verified data
    try:
        res = supabase.table("live_hazards").upsert(all_events).execute()
        print(f"✓ Successfully upserted {len(all_events)} active hazards to Supabase.")
    except Exception as e:
        print(f"❌ Supabase Upsert Failed: {e}")

    # Self-Cleaning: Delete events not updated in the last 24 hours (hazards that have dissipated)
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        supabase.table("live_hazards").delete().lt("updated_at", cutoff.isoformat()).execute()
        print("✓ Pruned expired/dissipated hazards from database.")
    except Exception as e:
        print(f"❌ Failed to prune old hazards: {e}")

if __name__ == "__main__":
    run_ingestion_cycle()