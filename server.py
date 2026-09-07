import os
import io
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from supabase import create_client, Client
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib import colors

app = Flask(__name__)

# Enable CORS specifically for your production domains and local testing
CORS(app, resources={
    r"/*": {
        "origins": [
            "https://thebrinkworld.com",
            "https://www.thebrinkworld.com",
            "http://localhost:3000",
            "http://127.0.0.1:5500"
        ]
    }
})

# Supabase Initialization (Use service role key securely on backend)
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# 1. Multi-Hazard Data Collectors
def fetch_seismic_and_volcano_data():
    # USGS Live Earthquakes Feed (Magnitude 4.5+)
    usgs_res = requests.get("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson").json()
    events = []
    for feature in usgs_res.get("features", []):
        props = feature["properties"]
        geom = feature["geometry"]
        events.append({
            "type": "Earthquake",
            "title": props["title"],
            "magnitude": props["mag"],
            "coordinates": geom["coordinates"][:2],
            "time": props["time"]
        })
    return events

def fetch_weather_and_flood_data(lat, lon):
    # Open-Meteo API for precipitation / heatwave metrics
    url = f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,precipitation"
    res = requests.get(url).json()
    return res.get("current", {})

# 2. PDF Report Generator (ReportLab)
def generate_pdf_dossier(data):
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter, rightMargin=36, leftMargin=36, topMargin=36, bottomMargin=36)
    story = []
    styles = getSampleStyleSheet()
    
    title_style = ParagraphStyle(
        'DocTitle',
        parent=styles['Heading1'],
        fontSize=18,
        textColor=colors.HexColor("#1A365D"),
        spaceAfter=12
    )
    
    story.append(Paragraph(f"IN-DEPTH INTELLIGENCE DOSSIER: {data['hazard_type'].upper()}", title_style))
    story.append(Paragraph(f"Target Asset: {data['asset_name']} | Coordinates: {data['latitude']}, {data['longitude']}", styles['Normal']))
    story.append(Spacer(1, 15))
    
    # Risk Assessment Matrix Table
    table_data = [
        ["Parameter", "Status / Value"],
        ["Hazard Category", data['hazard_type']],
        ["Base Risk Index", "High (Class III Alert Equivalent)"],
        ["Recommended Mitigation", "Review structural integrity & enforce local zoning codes."]
    ]
    t = Table(table_data, colWidths=[200, 304])
    t.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,0), colors.HexColor("#2B6CB0")),
        ('TEXTCOLOR', (0,0), (-1,0), colors.whitesmoke),
        ('ALIGN', (0,0), (-1,-1), 'LEFT'),
        ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
        ('BOTTOMPADDING', (0,0), (-1,0), 8),
        ('BACKGROUND', (0,1), (-1,-1), colors.HexColor("#F7FAFC")),
        ('GRID', (0,0), (-1,-1), 0.5, colors.HexColor("#CBD5E0"))
    ]))
    story.append(t)
    
    doc.build(story)
    buffer.seek(0)
    return buffer.getvalue()

# Health-check endpoint to support keep-alive pings (prevents free-tier cold starts)
@app.route('/api/health', methods=['GET'])
def health_check():
    return jsonify({"status": "active", "system": "synchronized"})

# 3. Lead Capture & Order Logging Endpoint
@app.route('/api/lead/capture', methods=['POST'])
def capture_lead():
    payload = request.json or {}
    client_name = payload.get("client_name")
    client_email = payload.get("client_email")
    hazard_type = payload.get("hazard_type")
    asset_name = payload.get("asset_name")
    lat = payload.get("latitude")
    lon = payload.get("longitude")
    amount_paid = payload.get("amount_paid", "$49 / ₹4,500")

    # Insert into Supabase (Bypasses RLS using Service Role Key)
    db_response = supabase.table("report_orders").insert({
        "client_name": client_name,
        "client_email": client_email,
        "hazard_type": hazard_type,
        "asset_name": asset_name,
        "latitude": lat,
        "longitude": lon,
        "amount_paid": amount_paid,
        "payment_status": "pending"
    }).execute()

    return jsonify({"status": "success", "message": "Order captured, awaiting payment verification.", "data": db_response.data})

if __name__ == '__main__':
    app.run(port=5000, debug=True)