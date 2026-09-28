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
headers = {"User-Agent": "TheBrinkEngine/3.2 (Planetary Macro-Intelligence Pipeline)"}

def fetch_gdacs_category(endpoint_code, category_name):
    """Generic fetcher for GDACS specific endpoints like Floods (FL), Droughts (DR), Heat (HW)."""
    events = []
    print(f"Fetching {category_name} events from GDACS ({endpoint_code})...")
    try:
        r = requests.get(f"https://www.gdacs.org/datareport/resources/{endpoint_code}/events.geojson", headers=headers, timeout=15)
        if r.status_code == 200:
            data = r.json()
            for f in data.get("features", []):
                p = f.get("properties", {})
                coords = f.get("geometry", {}).get("coordinates", [])
                if len(coords) < 2: 
                    continue
                lon, lat = float(coords[0]), float(coords[1])
                name = p.get("eventname") or p.get("name") or f"Active {category_name}"
                
                alert_level = p.get("alertlevel", "Orange").capitalize()
                
                events.append({
                    "id": f"GDACS_{endpoint_code}_{p.get('eventid', name.replace(' ', ''))}",
                    "category": category_name.lower(),
                    "name": name,
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
    except Exception as e:
        print(f"[ERROR] GDACS {category_name}: {e}")
    return events

def fetch_and_publish_cyclones():
    """Fetches global tropical cyclones from GDACS."""
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
                lon, lat = float(coords[0]), float(coords[1])
                name = p.get("eventname") or p.get("name") or "Tropical System"
                basin = (p.get("basin") or "GLOBAL").upper()

                if basin in ["EP", "NA", "AL", "CP"] and lon > 0: 
                    lon = -lon

                alert_level = p.get("alertlevel", "Green").capitalize()
                raw_wind = p.get("windspeed")
                wind_kts = float(raw_wind) if raw_wind else None
                if wind_kts == 0: 
                    wind_kts = None
                
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
    """Fetches active volcanic alerts from GDACS."""
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
                obs_date = p.get("todate") or p.get("fromdate") or datetime.now(timezone.utc).isoformat()

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
                    "observed_at": obs_date,
                    "updated_at": datetime.now(timezone.utc).isoformat()
                })
    except Exception as e:
        print(f"[ERROR] GDACS Volcanoes: {e}")
    return events

def fetch_eonet_hazards():
    """Fetches storms, volcanoes, wildfires, floods, and temp extremes from NASA EONET."""
    raw_events = []
    print("Fetching global physical events from NASA EONET...")
    try:
        r = requests.get("https://eonet.gsfc.nasa.gov/api/v3/events?status=open&days=7", headers=headers, timeout=15)
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
                        except ValueError:
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
                    "name": name,
                    "basin": "GLOBAL",
                    "intensity": "Active Weather System" if is_storm else "NASA Active Telemetry",
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
        
    wildfires = [e for e in raw_events if e["category"] == "wildfire"]
    others = [e for e in raw_events if e["category"] != "wildfire"]
    
    # Cap wildfires to the most recent 40 events to prevent table exhaustion
    wildfires.sort(key=lambda x: x["observed_at"], reverse=True)
    capped_wildfires = wildfires[:40]
    
    return others + capped_wildfires

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
        print(f"[ERROR] USGS Earthquakes: {e}")
    return events

def fetch_nws_alerts():
    """Fetches severe and extreme land-based weather alerts from the US National Weather Service."""
    events = []
    print("Fetching severe alerts from US NWS...")
    try:
        nws_headers = {"User-Agent": "(TheBrinkEngine, contact@thebrink.world)"}
        url = "https://api.weather.gov/alerts/active?severity=Severe,Extreme"
        r = requests.get(url, headers=nws_headers, timeout=15)
        
        if r.status_code == 200:
            data = r.json()
            for f in data.get("features", []):
                p = f.get("properties", {})
                event_type = p.get("event", "Severe Weather")
                
                if "Flood" in event_type: 
                    cat = "flood"
                elif "Fire" in event_type: 
                    cat = "wildfire"
                elif any(s in event_type for s in ["Winter", "Blizzard", "Wind", "Storm", "Tornado"]): 
                    cat = "storm"
                else: 
                    cat = "extreme"

                geom = f.get("geometry")
                if not geom: 
                    continue
                coords = geom.get("coordinates", [])
                
                try:
                    if geom["type"] == "Polygon":
                        lon, lat = float(coords[0][0][0]), float(coords[0][0][1])
                    elif geom["type"] == "MultiPolygon":
                        lon, lat = float(coords[0][0][0][0]), float(coords[0][0][0][1])
                    elif geom["type"] == "Point":
                        lon, lat = float(coords[0]), float(coords[1])
                    else:
                        continue
                except (IndexError, TypeError):
                    continue
                
                headline = p.get("headline") or event_type
                headline = headline.split("\n")[0][:100]
                
                events.append({
                    "id": f"NWS_{p.get('id', '')[-32:]}",
                    "category": cat,
                    "name": headline,
                    "basin": "NORTH_AMERICA",
                    "intensity": event_type,
                    "wind_kts": None,
                    "pressure_mb": None,
                    "latitude": lat,
                    "longitude": lon,
                    "alert_level": "Red" if p.get("severity") == "Extreme" else "Orange",
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
    
    # Core Global Planetary Vectors
    all_events.extend(fetch_and_publish_cyclones())
    all_events.extend(fetch_and_publish_volcanoes())
    all_events.extend(fetch_eonet_hazards())
    all_events.extend(fetch_and_publish_earthquakes())
    
    # Extended Hydrology & Thermal Vectors from GDACS
    all_events.extend(fetch_gdacs_category("FL", "Flood"))
    all_events.extend(fetch_gdacs_category("DR", "Drought"))
    all_events.extend(fetch_gdacs_category("HW", "Heatwave"))
    
    # Regional Sub-Continental Vector (US NWS)
    all_events.extend(fetch_nws_alerts())

    if not all_events:
        print("No events captured. Exiting.")
        return

    # Upsert new verified data
    try:
        supabase.table("live_hazards").upsert(all_events).execute()
        print(f"✓ Successfully upserted {len(all_events)} active hazards to Supabase.")
    except Exception as e:
        print(f"❌ Supabase Upsert Failed: {e}")

    # Immediate Pruning: Drop hazards that were not refreshed in this cycle
    try:
        supabase.table("live_hazards").delete().lt("updated_at", cycle_start.isoformat()).execute()
        print("✓ Pruned stale/dissipated hazards not present in current cycle.")
    except Exception as e:
        print(f"❌ Failed to prune old hazards: {e}")

if __name__ == "__main__":
    run_ingestion_cycle()