import os
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
            evidence_summary = {
                "v2_evidence_items": evidence_count,
                "v2_risk_findings": finding_count,
                "methodology": METHODOLOGY_VERSION,
                "materiality_rules": MATERIALITY_RULES_VERSION,
                "confidence_rules": CONFIDENCE_RULES_VERSION,
                "evidence_schema": EVIDENCE_SCHEMA_VERSION,
            }
            if ledger_error:
                evidence_summary["ledger_error"] = ledger_error

            sb_patch("brink_report_runs", {"id": run["id"]}, {
                "report_status": "delivered",
                "report_ref": ref,
                "output_location": pdf_path,
                "completed_at": finished.isoformat(),
                "evidence_summary": evidence_summary,
            })

            following = next_due(finished, sub.get("cadence"))
            sub_update = {
                "last_report_at": finished.isoformat(),
                "next_report_at": following.isoformat() if following else None,
            }
            if str(sub.get("cadence") or "").lower() == "one_off":
                sub_update["status"] = "completed"
            sb_patch("brink_monitoring_subscriptions", {"id": sub["id"]}, sub_update)
            print(
                f"[OK] {sub['product_type']} delivered for {f['facility_name']} · {ref} "
                f"· evidence={evidence_count} findings={finding_count}"
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
