import os
import time
from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML
from .geometry import preview_location
from .sources import fetch_telemetry
from .blocks import build_report_blocks
from .products import get_product_profile
from .evidence import (
    build_risk_findings,
    METHODOLOGY_VERSION,
    MATERIALITY_RULES_VERSION,
    CONFIDENCE_RULES_VERSION,
    EVIDENCE_SCHEMA_VERSION,
)


def produce(location, answers, site_name, customer_email=None, out_dir="reports", product_type="location_dossier", return_context=False, ref_code_override=None):
    pin = preview_location(location)
    if not pin["ok"]:
        raise ValueError(pin["error"])

    lat, lon = pin["lat"], pin["lon"]
    product_key, product = get_product_profile(product_type)
    ref_code = ref_code_override or f"BRK-{int(time.time()) % 100000:05d}"
    data = fetch_telemetry(lat, lon, context=answers)

    # Correct cardinal signs for global assets
    lat_card = "N" if lat >= 0 else "S"
    lon_card = "E" if lon >= 0 else "W"
    coords_formatted = f"{abs(round(lat, 4))}°{lat_card}, {abs(round(lon, 4))}°{lon_card}"

    meta = {
        "report_title": product["title"],
        "product_name": "The Brink World",
        "tier_label": product["tier"],
        "product_type": product_key,
        "product_intended_use": product["intended_use"],
        "product_limitation": product["limitation"],
        "ref": ref_code,
        "site_name": site_name,
        "customer_name": answers.get("customer_name", "Operations Lead"),
        "occupancy_label": answers.get("occupancy", "General location intelligence").title(),
        "coords_str": coords_formatted,
        "methodology_version": METHODOLOGY_VERSION,
        "materiality_rules_version": MATERIALITY_RULES_VERSION,
        "confidence_rules_version": CONFIDENCE_RULES_VERSION,
        "evidence_schema_version": EVIDENCE_SCHEMA_VERSION,
    }

    profile = answers.get("facility_profile") or {}
    risk_findings = build_risk_findings(
        report_run_id="render-preview",
        facility={"id": "render-preview"},
        profile=profile,
        telemetry=data,
    )

    cover, sections = build_report_blocks(meta, data, answers, risk_findings=risk_findings)

    tpl_dir = os.path.join(os.path.dirname(__file__), "templates")
    env = Environment(loader=FileSystemLoader(tpl_dir))
    tpl = env.get_template("report.html")
    rendered_html = tpl.render(meta=meta, cover=cover, sections=sections)

    os.makedirs(out_dir, exist_ok=True)
    pdf_path = os.path.join(out_dir, f"{ref_code}_{site_name.replace(' ', '_')}.pdf")
    HTML(string=rendered_html).write_pdf(pdf_path)
    print(f"[✓] PDF created: {pdf_path}")

    if return_context:
        return pdf_path, ref_code, {
            "telemetry": data,
            "meta": meta,
            "cover": cover,
            "sections": sections,
            "risk_findings": risk_findings,
        }
    return pdf_path, ref_code
