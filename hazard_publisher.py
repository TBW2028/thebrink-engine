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
                
                # Safely extract wind speed; if 0 or missing, pass None so UI handles it gracefully
                raw_wind = p.get("windspeed")
                wind_kts = float(raw_wind) if raw_wind else None
                if wind_kts == 0: wind_kts = None
                
                events.append({
                    "id": f"GDACS_TC_{p.get('eventid', name.replace(' ', ''))}",
                    "category": "cyclone",
                    "name": name,
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
                
                # STRICT 7-DAY RECENCY FILTER FOR GDACS VOLCANOES
                obs_date_str = p.get("fromdate")
                if obs_date_str:
                    try:
                        clean_date = obs_date_str.replace("Z", "+00:00")
                        obs_dt = datetime.fromisoformat(clean_date)
                        if datetime.now(timezone.utc) - obs_dt > timedelta(days=7):
                            continue # Discard ancient/background volcanoes
                    except Exception:
                        pass
                
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
    """Fetches physical planetary systems from NASA EONET (Open ocean storms & recent unrest)."""
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
                
                # Get the most recent observation position
                latest = geom[-1]
                coords = latest.get("coordinates")

                # Discard stale volcanic entries with no activity in the past 7 days
                if is_volcano:
                    event_date_str = latest.get("date")
                    if not event_date_str:
                        continue
                    try:
                        clean_date = event_date_str.replace("Z", "+00:00")
                        event_dt = datetime.fromisoformat(clean_date)
                        if datetime.now(timezone.utc) - event_dt > timedelta(days=7):
                            continue
                    except Exception:
                        continue

                geom_type = latest.get("type", "Point")
                
                # Extract lat/lon whether NASA sent a single Point or a Polygon track
                try:
                    if geom_type == "Polygon":
                        lon, lat = float(coords[0][0][0]), float(coords[0][0][1])
                    else:
                        lon, lat = float(coords[0]), float(coords[1])
                except (IndexError, TypeError):
                    continue

                # EXACT KNOT WIND SPEED EXTRACTION
                wind_kts = None
                if is_storm:
                    mag_val = latest.get("magnitudeValue")
                    mag_unit = latest.get("magnitudeUnit")
                    if mag_val is not None:
                        try:
                            val = float(mag_val)
                            # Convert NASA magnitudes to standard knots
                            if mag_unit == "kts": wind_kts = val
                            elif mag_unit == "mph": wind_kts = val * 0.868976
                            elif mag_unit == "km/h": wind_kts = val * 0.539957
                            else: wind_kts = val
                            
                            wind_kts = round(wind_kts)
                        except ValueError:
                            pass
                    
                category_str = "cyclone" if is_storm else "volcano"
                name = event.get("title", "Unknown Event")
                
                events.append({
                    "id": f"EONET_{event.get('id')}",
                    "category": category_str,
                    "name": name,
                    "basin": "GLOBAL",
                    "intensity": "Active Weather System" if is_storm else "Active Volcanic Unrest",
                    "wind_kts": wind_kts,
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
    cycle_start = datetime.now(timezone.utc)
    print(f"--- Starting Brink Ingestion Cycle at {cycle_start.isoformat()} ---")
    
    all_events = []
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

    # Immediate Pruning: Delete hazards that were not refreshed in this ingestion cycle
    try:
        supabase.table("live_hazards").delete().lt("updated_at", cycle_start.isoformat()).execute()
        print("✓ Pruned stale/dissipated hazards not present in current cycle.")
    except Exception as e:
        print(f"❌ Failed to prune old hazards: {e}")

if __name__ == "__main__":
    run_ingestion_cycle()