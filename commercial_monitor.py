import os
import calendar
from datetime import datetime, timezone, timedelta
import requests

from brink_dossier.render import produce

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")


def headers(prefer=None):
    h = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


def add_months(dt, months):
    month0 = dt.month - 1 + months
    year = dt.year + month0 // 12
    month = month0 % 12 + 1
    day = min(dt.day, calendar.monthrange(year, month)[1])
    return dt.replace(year=year, month=month, day=day)


def next_due(now, cadence):
    cadence = str(cadence or "monthly").lower()
    if cadence == "one_off":
        return None
    if cadence == "weekly":
        return now + timedelta(days=7)
    if cadence == "quarterly":
        return add_months(now, 3)
    if cadence == "annual":
        return add_months(now, 12)
    return add_months(now, 1)


def sb_get(path, params=None):
    r = requests.get(f"{SUPABASE_URL}/rest/v1/{path}", params=params, headers=headers(), timeout=20)
    r.raise_for_status()
    return r.json()


def sb_patch(path, filters, values):
    params = {k: f"eq.{v}" for k, v in filters.items()}
    r = requests.patch(
        f"{SUPABASE_URL}/rest/v1/{path}",
        params=params,
        headers=headers("return=minimal"),
        json=values,
        timeout=20,
    )
    r.raise_for_status()


def sb_insert(path, values):
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers=headers("return=representation"),
        json=values,
        timeout=20,
    )
    r.raise_for_status()
    rows = r.json()
    return rows[0] if rows else None


def main():
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Missing Supabase service credentials.")

    now = datetime.now(timezone.utc)
    due = sb_get(
        "brink_monitoring_subscriptions",
        {
            "select": "id,facility_id,product_type,cadence,next_report_at,status",
            "status": "eq.active",
            "next_report_at": f"lte.{now.isoformat()}",
            "order": "next_report_at.asc",
            "limit": "25",
        },
    )

    if not due:
        print("No active commercial subscriptions due.")
        return

    for sub in due:
        facility_rows = sb_get(
            "brink_facilities",
            {
                "select": "*",
                "id": f"eq.{sub['facility_id']}",
                "status": "eq.active",
                "limit": "1",
            },
        )
        if not facility_rows:
            print(f"[SKIP] Facility missing/inactive for subscription {sub['id']}")
            continue

        f = facility_rows[0]
        email = f.get("contact_email")
        if not email:
            print(f"[SKIP] No contact email for facility {f['id']}")
            continue

        run = sb_insert("brink_report_runs", {
            "facility_id": f["id"],
            "subscription_id": sub["id"],
            "product_type": sub["product_type"],
            "report_status": "generating",
            "evidence_as_of": now.isoformat(),
        })

        try:
            location = (
                f"{f['latitude']},{f['longitude']}"
                if f.get("latitude") is not None and f.get("longitude") is not None
                else f.get("location_label")
            )
            answers = {
                "customer_name": f.get("contact_name") or "Operations Lead",
                "occupancy": f.get("facility_type") or "Commercial property / facility",
                "country": f.get("country"),
                "country_code": f.get("country_code"),
                "critical_function": f.get("critical_function"),
                "dependencies": f.get("dependencies") or {},
            }

            pdf_path, ref = produce(
                location=location,
                answers=answers,
                site_name=f.get("facility_name") or f.get("location_label") or "Monitored Facility",
                customer_email=email,
                product_type=sub["product_type"],
            )

            finished = datetime.now(timezone.utc)
            sb_patch("brink_report_runs", {"id": run["id"]}, {
                "report_status": "delivered",
                "report_ref": ref,
                "output_location": pdf_path,
                "completed_at": finished.isoformat(),
            })
            following = next_due(finished, sub.get("cadence"))
            sub_update = {
                "last_report_at": finished.isoformat(),
                "next_report_at": following.isoformat() if following else None,
            }
            if str(sub.get("cadence") or "").lower() == "one_off":
                sub_update["status"] = "completed"
            sb_patch("brink_monitoring_subscriptions", {"id": sub["id"]}, sub_update)
            print(f"[OK] {sub['product_type']} delivered for {f['facility_name']} · {ref}")
        except Exception as exc:
            if run:
                sb_patch("brink_report_runs", {"id": run["id"]}, {
                    "report_status": "failed",
                    "completed_at": datetime.now(timezone.utc).isoformat(),
                    "evidence_summary": {"error": str(exc)[:800]},
                })
            print(f"[FAIL] subscription={sub['id']} facility={f.get('facility_name')}: {exc}")


if __name__ == "__main__":
    main()
