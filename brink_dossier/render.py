import os
import time
import base64
import requests
from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML
from .geometry import preview_location
from .sources import fetch_telemetry
from .blocks import build_report_blocks
from .products import get_product_profile

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")

def produce(location, answers, site_name, customer_email, out_dir="reports", product_type="location_dossier", return_context=False):
    pin = preview_location(location)
    if not pin["ok"]:
        raise ValueError(pin["error"])

    lat, lon = pin["lat"], pin["lon"]
    product_key, product = get_product_profile(product_type)
    ref_code = f"BRK-{int(time.time()) % 100000:05d}"
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
        "coords_str": coords_formatted
    }

    cover, sections = build_report_blocks(meta, data, answers)

    tpl_dir = os.path.join(os.path.dirname(__file__), "templates")
    env = Environment(loader=FileSystemLoader(tpl_dir))
    tpl = env.get_template("report.html")
    rendered_html = tpl.render(meta=meta, cover=cover, sections=sections)

    os.makedirs(out_dir, exist_ok=True)
    pdf_path = os.path.join(out_dir, f"{ref_code}_{site_name.replace(' ', '_')}.pdf")
    HTML(string=rendered_html).write_pdf(pdf_path)
    print(f"[✓] PDF created: {pdf_path}")

    if RESEND_API_KEY:
        print(f"[*] Dispatching customer dossier to {customer_email} and internal archive copy to thebrink2028@gmail.com via Resend...")

        with open(pdf_path, "rb") as f:
            pdf_b64 = base64.b64encode(f.read()).decode("utf-8")

        attachment = {
            "filename": f"Dossier_{ref_code}.pdf",
            "content": pdf_b64
        }
        resend_headers = {
            "Authorization": f"Bearer {RESEND_API_KEY}",
            "Content-Type": "application/json"
        }
        sender = os.environ.get("DOSSIER_FROM_EMAIL", "The Brink World <intel@thebrinkworld.com>")
        reply_to = os.environ.get("DOSSIER_REPLY_TO", "thebrink2028@gmail.com")

        # Customer delivery: only the customer is visible as a recipient.
        customer_payload = {
            "from": sender,
            "to": [customer_email],
            "reply_to": reply_to,
            "subject": f"Your {product['title']} — {site_name} ({ref_code})",
            "html": (
                f"<h3>The Brink World — {product['title']}</h3>"
                f"<p>Your requested location intelligence report for <strong>{site_name}</strong> "
                f"({coords_formatted}) is attached.</p>"
                f"<p>Dossier Reference: <strong>{ref_code}</strong></p>"
                f"<p>This report combines observed, official-warning and modelled data. "
                f"Source and confidence notes inside the dossier explain how each finding should be interpreted.</p>"
            ),
            "attachments": [attachment]
        }

        customer_res = requests.post(
            "https://api.resend.com/emails",
            headers=resend_headers,
            json=customer_payload,
            timeout=20
        )

        # Internal archive/operations copy: includes recipient details without
        # exposing the admin inbox to the customer.
        admin_payload = {
            "from": sender,
            "to": ["thebrink2028@gmail.com"],
            "reply_to": customer_email,
            "subject": f"[REPORT DELIVERED] {product['short_name']} · {site_name} · {ref_code}",
            "html": (
                f"<h3>The Brink World — Dossier Delivery Record</h3>"
                f"<p><strong>Reference:</strong> {ref_code}</p>"
                f"<p><strong>Client recipient:</strong> {customer_email}</p>"
                f"<p><strong>Client / contact:</strong> {meta.get('customer_name', 'Not supplied')}</p>"
                f"<p><strong>Site / location name:</strong> {site_name}</p>"
                f"<p><strong>Coordinates:</strong> {coords_formatted}</p>"
                f"<p><strong>Occupancy / use:</strong> {meta.get('occupancy_label', 'Not supplied')}</p>"
                f"<p>The exact PDF delivered to the client is attached for the internal archive.</p>"
            ),
            "attachments": [attachment]
        }

        admin_res = requests.post(
            "https://api.resend.com/emails",
            headers=resend_headers,
            json=admin_payload,
            timeout=20
        )

        customer_ok = customer_res.status_code in [200, 201]
        admin_ok = admin_res.status_code in [200, 201]

        if customer_ok:
            print("[✓] Customer dossier email accepted by Resend.")
        else:
            print(f"[!] Customer Resend error: {customer_res.status_code} - {customer_res.text}")

        if admin_ok:
            print("[✓] Internal archive copy accepted by Resend.")
        else:
            print(f"[!] Admin archive Resend error: {admin_res.status_code} - {admin_res.text}")

        if not customer_ok or not admin_ok:
            raise RuntimeError(
                "Dossier PDF was created, but email delivery failed. "
                "The order must not be marked delivered."
            )

    return pdf_path, ref_code