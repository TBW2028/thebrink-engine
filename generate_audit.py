import os, requests
from brink_dossier.render import produce

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

def process_pending_orders():
    # Standalone Demo Fallback: North Pole (90.0° N, 0.0° E)
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("[*] Running standalone demo mode for testing at the North Pole...")
        produce(
            location="90.0, 0.0",
            answers={
                "occupancy": "research_station",
                "headcount": "1-5",
                "tolerance": "1-2d",
                "value_band": "skip",
                "customer_name": "Arctic Outpost Operations"
            },
            site_name="North Pole Station One",
            customer_email="thebrink2028@gmail.com"
        )
        return

    # Production Workflow: Read customer order directly from Supabase
    headers = {"apikey": SUPABASE_KEY, "Authorization": f"Bearer {SUPABASE_KEY}", "Content-Type": "application/json"}
    url = f"{SUPABASE_URL}/rest/v1/facilities?select=id,facility_name,latitude,longitude,account_id&order=updated_at.desc&limit=1"
    res = requests.get(url, headers=headers)
    rows = res.json()

    if not isinstance(rows, list) or len(rows) == 0:
        print("[*] No pending facilities to process.")
        return

    rec = rows[0]
    site_name = rec["facility_name"]
    loc_str = f"{rec['latitude']}, {rec['longitude']}"

    email = "thebrink2028@gmail.com"
    if rec.get("account_id"):
        acc_res = requests.get(f"{SUPABASE_URL}/rest/v1/accounts?id=eq.{rec['account_id']}", headers=headers).json()
        if isinstance(acc_res, list) and len(acc_res) > 0:
            email = acc_res[0].get("email", email)

    print(f"[*] Fulfilling order for: {site_name} -> {email}")
    pdf_path, ref = produce(
        location=loc_str,
        answers={"occupancy": "warehouse", "customer_name": site_name},
        site_name=site_name,
        customer_email=email
    )
    print(f"[✓] Completed and sent: {ref}")

if __name__ == "__main__":
    process_pending_orders()
