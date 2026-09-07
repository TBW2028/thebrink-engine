import os
import io
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
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
    
    story.append(Paragraph(f"IN-DEPTH INTELLIGENCE DOSSIER: {data.get('hazard_type', 'GENERAL').upper()}", title_style))
    story.append(Paragraph(f"Target Asset: {data.get('asset_name', 'N/A')} | Coordinates: {data.get('latitude', 'N/A')}, {data.get('longitude', 'N/A')}", styles['Normal']))
    story.append(Spacer(1, 15))
    
    # Risk Assessment Matrix Table
    table_data = [
        ["Parameter", "Status / Value"],
        ["Hazard Category", data.get('hazard_type', 'N/A')],
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
    return jsonify({"status": "active", "service": "The Brink Hazard Engine"})

# Root route to prevent 404s when hitting the base URL
@app.route('/', methods=['GET'])
def root_home():
    return jsonify({"status": "online", "service": "The Brink Hazard Engine"})

if __name__ == '__main__':
    app.run(port=5000, debug=True)