import os, time, requests
from jinja2 import Environment, FileSystemLoader
from weasyprint import HTML
from .geometry import preview_location
from .sources import fetch_telemetry
from .blocks import build_report_blocks

RESEND_API_KEY = os.environ.get("RESEND_API_KEY")

def produce(location, answers, site_name, customer_email, out_dir="reports"):
    pin = preview_location(location)
    if not pin["ok"]:
        raise ValueError(pin["error"])

    lat, lon = pin["lat"], pin["lon"]
    ref_code = f"BRK-{int(time.time()) % 100000:05d}"
    data = fetch_telemetry(lat, lon)

    meta = {
        "report_title": "Site Threat Dossier",
        "product_name": "The Brink World",
        "tier_label": "Site Report",
        "ref": ref_code,
        "site_name": site_name,
        "customer_name": answers.get("customer_name", "Operations Lead"),
        "occupancy_label": answers.get("occupancy", "Warehouse").title(),
        "coords_str": f"{round(lat, 4)}°N, {round(lon, 4)}°E"
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
        print(f"[*] Sending PDF to {customer_email} and thebrink2028@gmail.com...")
        with open(pdf_path, "rb") as f:
            pdf_bytes = list(f.read())

        payload = {
            "from": "The Brink World <onboarding@resend.dev>",
            "to": [customer_email, "thebrink2028@gmail.com"],
            "subject": f"[SITE DOSSIER] Exposure Report: {site_name} ({ref_code})",
            "html": f"<h3>Site Threat Dossier</h3><p><strong>Site:</strong> {site_name} ({lat}, {lon})</p><p>Ref: {ref_code}</p>",
            "attachments": [{"filename": f"Dossier_{ref_code}.pdf", "content": pdf_bytes}]
        }
        r = requests.post("https://api.resend.com/emails", headers={"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"}, json=payload, timeout=15)
        if r.status_code in [200, 201]:
            print(f"[✓] Email delivered successfully to both inboxes.")
        else:
            print(f"[!] Email delivery note: {r.text}")

    return pdf_path, ref_code
