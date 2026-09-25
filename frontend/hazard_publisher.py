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
headers = {"User-Agent": "TheBrinkEngine/1.0 (Data Ingestion Pipeline)"}

def fetch_and_publish_cyclones():
    """Fetches global tropical cyclones from GDACS. Fixes coordinate hemisphere bugs."""
    events = []
    print("Fetching active tropical cyclones from GDACS...")
    
    try:
        r = requests.get("https://www.gdacs.org/datareport/resources/TC/events.geojson", headers=headers, timeout=15)
        if r.status_code == 200:
            data = r.json()
            for f in data.get("features", []):
                p = f.get("properties", {})
                coords = f.get("geometry", {}).get("coordinates", [])
                if len(coords) < 2:
                    continue
                
                lon = float(coords[0])
                lat = float(coords[1])
                name = p.get("eventname") or p.get("name") or "Tropical System"
                basin = (p.get("basin") or "GLOBAL").upper()

                # EXACT FIX FOR THE THAILAND/MEXICO BUG:
                # GDACS sometimes drops the negative sign on Eastern Pacific/Atlantic storms.
                if basin in ["EP", "NA", "AL", "CP"] and lon > 0:
                    lon = -lon

                # Extract verified government data, no fake numbers
                alert_level = p.get("alertlevel", "Green").capitalize()
                
                events.append({
                    "id": f"TC_{p.get('eventid', name.replace(' ', ''))}",
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
        print(f"[ERROR] Failed to fetch GDACS Cyclones: {e}")

    return events

def fetch_and_publish_volcanoes():
    """Fetches verified volcanic alerts from GDACS (replaces EONET false alarms)."""
    events = []
    print("Fetching active volcanic alerts from GDACS...")

    try:
        r = requests.get("https://www.gdacs.org/datareport/resources/VO/events.geojson", headers=headers, timeout=15)
        if r.status_code == 200:
            data = r.json()
            for f in data.get("features", []):
                p = f.get("properties", {})
                coords = f.get("geometry", {}).get("coordinates", [])
                if len(coords) < 2:
                    continue

                alert_level = p.get("alertlevel", "Green").capitalize()
                # Ignore "Green" background degassing to prevent false alarms
                if alert_level == "Green":
                    continue

                events.append({
                    "id": f"VOLC_{p.get('eventid', p.get('name', 'Unknown'))}",
                    "category": "volcano",
                    "name": p.get("eventname") or p.get("name") or "Volcano",
                    "basin": "TERRESTRIAL",
                    "intensity": f"Volcanic Alert: {alert_level}",
                    "wind_kts": None,
                    "pressure_mb": None,
                    "latitude": float(coords[1]),
                    "longitude": float(coords[0]),
                    "alert_level": alert_level,
                    "source": "GDACS / Global Volcanism Program",
                    "observed_at": p.get("fromdate", datetime.now(timezone.utc).isoformat()),
                    "updated_at": datetime.now(timezone.utc).isoformat()
                })
    except Exception as e:
        print(f"[ERROR] Failed to fetch GDACS Volcanoes: {e}")

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
                if len(coords) < 3:
                    continue

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
        print(f"[ERROR] Failed to fetch USGS Earthquakes: {e}")

    return events

def run_ingestion_cycle():
    print(f"--- Starting Brink Ingestion Cycle at {datetime.now(timezone.utc).isoformat()} ---")
    
    all_events = []
    all_events.extend(fetch_and_publish_cyclones())
    all_events.extend(fetch_and_publish_volcanoes())
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