import os
import sys
import argparse
import requests
from brink_dossier.render import produce

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

def process_audit(site_name, location, email, occupancy="warehouse"):
    print(f"[*] Compiling Threat Dossier for: {site_name} ({location}) -> {email}")
    pdf_path, ref = produce(
        location=location,
        answers={
            "occupancy": occupancy,
            "customer_name": site_name
        },
        site_name=site_name,
        customer_email=email
    )
    print(f"[✓] Dossier compiled and delivered successfully. Ref: {ref}")
    return pdf_path, ref

def process_pending_orders():
    # 1. Check for Direct CLI or GitHub Actions arguments
    parser = argparse.ArgumentParser(description="The Brink Engine Dossier Generator")
    parser.add_argument("--facility", type=str, help="Facility or asset name")
    parser.add_argument("--location", type=str, help="Coordinates or city name")
    parser.add_argument("--email", type=str, help="Recipient email address")
    args, _ = parser.parse_known_args()

    facility = args.facility or os.environ.get("FACILITY_NAME")
    location = args.location or os.environ.get("TARGET_LOCATION")
    email = args.email or os.environ.get("CUSTOMER_EMAIL")

    if facility and location and email:
        print("[*] Direct dispatch inputs detected:")
        process_audit(site_name=facility, location=location, email=email)
        return

    # 2. Standalone Demo Mode if no Supabase credentials exist
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("[*] No Supabase credentials or CLI inputs provided. Running standalone test...")
        process_audit(
            site_name="Sample Logistics Node",
            location="18.5204, 73.8567",
            email="thebrink2028@gmail.com"
        )
        return

    # 3. Production Queue Workflow: Read pending order from Supabase
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation"
    }

    # Query only records that are pending fulfillment
    url = f"{SUPABASE_URL}/rest/v1/facilities?select=id,facility_name,latitude,longitude,account_id&status=eq.pending&order=created_at.asc&limit=1"
    try:
        res = requests.get(url, headers=headers, timeout=12)
        rows = res.json()
    except Exception as e:
        print(f"[!] Database connection error: {e}")
        return

    if not isinstance(rows, list) or len(rows) == 0:
        print("[*] No pending orders in queue.")
        return

    rec = rows[0]
    site_name = rec.get("facility_name", "Monitored Facility")
    loc_str = f"{rec.get('latitude')}, {rec.get('longitude')}"
    target_email = "thebrink2028@gmail.com"

    if rec.get("account_id"):
        try:
            acc_res = requests.get(f"{SUPABASE_URL}/rest/v1/accounts?id=eq.{rec['account_id']}", headers=headers, timeout=10).json()
            if isinstance(acc_res, list) and len(acc_res) > 0:
                target_email = acc_res[0].get("email", target_email)
        except Exception:
            pass

    # Compile and send
    pdf_path, ref = process_audit(site_name=site_name, location=loc_str, email=target_email)

    # Mark facility as fulfilled in Supabase
    patch_url = f"{SUPABASE_URL}/rest/v1/facilities?id=eq.{rec['id']}"
    patch_data = {"status": "completed", "last_dossier_ref": ref}
    requests.patch(patch_url, headers=headers, json=patch_data, timeout=10)
    print(f"[✓] Facility record #{rec['id']} status updated to 'completed'.")

if __name__ == "__main__":
    process_pending_orders()