import os
import base64
import hashlib
import secrets
import calendar
from datetime import datetime, timezone, timedelta
import requests

from brink_dossier.render import produce
from brink_dossier.evidence import (
    build_evidence_items,
    build_risk_findings,
    METHODOLOGY_VERSION,
    MATERIALITY_RULES_VERSION,
    CONFIDENCE_RULES_VERSION,
    EVIDENCE_SCHEMA_VERSION,
)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY")
TARGET_SUBSCRIPTION_ID = os.environ.get("TARGET_SUBSCRIPTION_ID", "").strip()
ADMIN_EMAIL = os.environ.get("COMMERCIAL_REVIEW_EMAIL", "thebrink2028@gmail.com")
DRAFT_BUCKET = os.environ.get("COMMERCIAL_DRAFT_BUCKET", "commercial-report-drafts")
REVIEW_BASE_URL = os.environ.get("COMMERCIAL_REVIEW_BASE_URL", "https://thebrink-engine.thebrink2028.workers.dev").rstrip("/")


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


def sb_insert_rows(path, values):
    if not values:
        return []
    r = requests.post(
        f"{SUPABASE_URL}/rest/v1/{path}",
        headers=headers("return=representation"),
        json=values,
        timeout=30,
    )
    r.raise_for_status()
    rows = r.json()
    return rows if isinstance(rows, list) else []


def ensure_draft_bucket():
    """Create the private Supabase Storage bucket once; existing-bucket responses are harmless."""
    url = f"{SUPABASE_URL}/storage/v1/bucket"
    payload = {
        "id": DRAFT_BUCKET,
        "name": DRAFT_BUCKET,
        "public": False,
        "file_size_limit": 25000000,
        "allowed_mime_types": ["application/pdf"],
    }
    response = requests.post(url, headers=headers(), json=payload, timeout=20)
    if response.status_code in (200, 201):
        return
    # Supabase can return 400/409 when the bucket already exists.
    if response.status_code in (400, 409) and "exist" in response.text.lower():
        return
    # Verify the bucket before treating another create response as fatal.
    check = requests.get(
        f"{SUPABASE_URL}/storage/v1/bucket/{DRAFT_BUCKET}",
        headers=headers(),
        timeout=20,
    )
    if check.status_code == 200:
        return
    raise RuntimeError(
        f"Commercial draft storage bucket could not be prepared: "
        f"{response.status_code} {response.text[:300]}"
    )


def upload_draft_pdf(pdf_path, facility_id, run_id, ref):
    ensure_draft_bucket()
    object_path = f"{facility_id}/{run_id}/{ref}.pdf"
    with open(pdf_path, "rb") as handle:
        content = handle.read()

    upload_headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/pdf",
        "x-upsert": "true",
    }
    response = requests.post(
        f"{SUPABASE_URL}/storage/v1/object/{DRAFT_BUCKET}/{object_path}",
        headers=upload_headers,
        data=content,
        timeout=60,
    )
    if response.status_code not in (200, 201):
        raise RuntimeError(
            f"Commercial draft PDF upload failed ({response.status_code}): "
            f"{response.text[:400]}"
        )
    return object_path


def send_admin_draft(pdf_path, ref, facility, subscription, run_id, approval_token):
    if not RESEND_API_KEY:
        raise RuntimeError("RESEND_API_KEY is required for internal draft review delivery.")

    with open(pdf_path, "rb") as handle:
        pdf_b64 = base64.b64encode(handle.read()).decode("utf-8")

    product_label = str(subscription.get("product_type") or "commercial report").replace("_", " ").title()
    site_name = facility.get("facility_name") or facility.get("location_label") or "Monitored Facility"
    client_email = facility.get("contact_email") or "Not supplied"
    review_url = (
        f"{REVIEW_BASE_URL}/api/commercial/report-review"
        f"?run={run_id}&token={approval_token}"
    )
    payload = {
        "from": os.environ.get("DOSSIER_FROM_EMAIL", "The Brink World <intel@thebrinkworld.com>"),
        "to": [ADMIN_EMAIL],
        "reply_to": client_email if "@" in client_email else ADMIN_EMAIL,
        "subject": f"[DRAFT REVIEW REQUIRED] {product_label} · {site_name} · {ref}",
        "html": (
            "<h3>The Brink World — Commercial Report Draft</h3>"
            f"<p><strong>Reference:</strong> {ref}</p>"
            f"<p><strong>Facility:</strong> {site_name}</p>"
            f"<p><strong>Client recipient after approval:</strong> {client_email}</p>"
            f"<p><strong>Location:</strong> {facility.get('location_label') or 'Not supplied'}</p>"
            "<p><strong>The client has NOT been emailed this report.</strong></p>"
            "<p>Review the attached PDF for facility details, coordinates, evidence and wording.</p>"
            f'<p style="margin:24px 0"><a href="{review_url}" '
            'style="background:#0b7285;color:white;text-decoration:none;padding:12px 18px;'
            'border-radius:5px;font-weight:700">REVIEW & APPROVE CLIENT DELIVERY</a></p>'
            "<p>Opening the review page does not send the report. A second confirmation is required.</p>"
        ),
        "attachments": [{"filename": f"DRAFT_{ref}.pdf", "content": pdf_b64}],
    }
    response = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    if response.status_code not in (200, 201):
        raise RuntimeError(f"Internal draft email failed ({response.status_code}): {response.text[:500]}")


def main():
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Missing Supabase service credentials.")

    now = datetime.now(timezone.utc)
    due_params = {
        "select": "id,facility_id,product_type,cadence,next_report_at,status",
        "status": "eq.active",
        "order": "next_report_at.asc",
        "limit": "25",
    }
    if TARGET_SUBSCRIPTION_ID:
        due_params["id"] = f"eq.{TARGET_SUBSCRIPTION_ID}"
        due_params["limit"] = "1"
    else:
        due_params["next_report_at"] = f"lte.{now.isoformat()}"

    due = sb_get("brink_monitoring_subscriptions", due_params)

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

        profile_rows = sb_get(
            "brink_facility_profiles",
            {
                "select": "*",
                "facility_id": f"eq.{f['id']}",
                "is_current": "eq.true",
                "order": "profile_version.desc",
                "limit": "1",
            },
        )
        profile = profile_rows[0] if profile_rows else None

        acceptance_rows = sb_get(
            "brink_contract_acceptances",
            {
                "select": "terms_version,privacy_version,accepted_at",
                "subscription_id": f"eq.{sub['id']}",
                "order": "accepted_at.desc",
                "limit": "1",
            },
        )
        acceptance = acceptance_rows[0] if acceptance_rows else {}

        run = sb_insert("brink_report_runs", {
            "facility_id": f["id"],
            "subscription_id": sub["id"],
            "product_type": sub["product_type"],
            "report_status": "generating",
            "evidence_as_of": now.isoformat(),
            "facility_profile_id": profile.get("id") if profile else None,
            "report_methodology_version": METHODOLOGY_VERSION,
            "materiality_rules_version": MATERIALITY_RULES_VERSION,
            "confidence_rules_version": CONFIDENCE_RULES_VERSION,
            "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
            "engine_commit_sha": os.environ.get("GITHUB_SHA"),
            "terms_version": acceptance.get("terms_version"),
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
                "facility_profile": profile or {},
            }

            pdf_path, ref, report_context = produce(
                location=location,
                answers=answers,
                site_name=f.get("facility_name") or f.get("location_label") or "Monitored Facility",
                customer_email=email,
                product_type=sub["product_type"],
                return_context=True,
            )

            evidence_count = 0
            finding_count = 0
            ledger_error = None
            try:
                evidence_payloads = build_evidence_items(
                    report_run_id=run["id"],
                    facility=f,
                    profile=profile,
                    telemetry=report_context["telemetry"],
                )
                evidence_rows = sb_insert_rows("brink_evidence_items", evidence_payloads)
                evidence_count = len(evidence_rows)
                evidence_by_code = {
                    row.get("metric_code"): row.get("id")
                    for row in evidence_rows
                    if row.get("metric_code") and row.get("id")
                }

                finding_specs = build_risk_findings(
                    report_run_id=run["id"],
                    facility=f,
                    profile=profile,
                    telemetry=report_context["telemetry"],
                )

                bridges = []
                for spec in finding_specs:
                    finding = sb_insert("brink_risk_findings", spec["record"])
                    if not finding:
                        continue
                    finding_count += 1
                    seen = set()
                    for code in spec.get("supporting_metric_codes") or []:
                        evidence_id = evidence_by_code.get(code)
                        if evidence_id and evidence_id not in seen:
                            bridges.append({
                                "finding_id": finding["id"],
                                "evidence_id": evidence_id,
                            })
                            seen.add(evidence_id)

                if bridges:
                    sb_insert_rows("brink_risk_finding_evidence", bridges)
            except Exception as ledger_exc:
                ledger_error = str(ledger_exc)[:800]
                print(f"[WARN] V2 evidence ledger write failed for {f['facility_name']}: {ledger_exc}")

            finished = datetime.now(timezone.utc)
            approval_token = secrets.token_urlsafe(32)
            approval_hash = hashlib.sha256(approval_token.encode("utf-8")).hexdigest()
            approval_expires = finished + timedelta(days=14)
            object_path = upload_draft_pdf(
                pdf_path,
                facility_id=f["id"],
                run_id=run["id"],
                ref=ref,
            )

            evidence_summary = {
                "v2_evidence_items": evidence_count,
                "v2_risk_findings": finding_count,
                "methodology": METHODOLOGY_VERSION,
                "materiality_rules": MATERIALITY_RULES_VERSION,
                "confidence_rules": CONFIDENCE_RULES_VERSION,
                "evidence_schema": EVIDENCE_SCHEMA_VERSION,
                "draft_bucket": DRAFT_BUCKET,
                "draft_object_path": object_path,
                "delivery_approval_hash": approval_hash,
                "delivery_approval_expires_at": approval_expires.isoformat(),
                "client_delivery_status": "awaiting_approval",
                "client_email": f.get("contact_email"),
                "client_name": f.get("contact_name"),
                "facility_name": f.get("facility_name"),
                "location_label": f.get("location_label"),
            }
            if ledger_error:
                evidence_summary["ledger_error"] = ledger_error

            # Persist the approval gate before sending the review email.
            sb_patch("brink_report_runs", {"id": run["id"]}, {
                "report_status": "awaiting_approval",
                "report_ref": ref,
                "output_location": f"supabase://{DRAFT_BUCKET}/{object_path}",
                "completed_at": finished.isoformat(),
                "evidence_summary": evidence_summary,
            })
            sb_patch("brink_monitoring_subscriptions", {"id": sub["id"]}, {
                "status": "awaiting_report_approval",
                "next_report_at": None,
            })

            send_admin_draft(
                pdf_path,
                ref,
                f,
                sub,
                run_id=run["id"],
                approval_token=approval_token,
            )

            print(
                f"[DRAFT] {sub['product_type']} generated for {f['facility_name']} · {ref} "
                f"· admin review required · evidence={evidence_count} findings={finding_count}"
            )
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
