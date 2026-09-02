@app.post("/api/lead/capture")
async def capture_order_lead(
    plan: str = Form(...),            # tier1_instant_dossier, tier2_strategic_audit, tier3_corridor_watch, tier4_field_recon
    name: str = Form(...),
    email: str = Form(...),
    company: str = Form(""),
    reason: str = Form("General Risk Assessment"),
    asset_name: str = Form("Designated Operations Corridor"),
    lat: float = Form(None),
    lon: float = Form(None),
    radius_km: float = Form(300.0)
):
    plan_labels = {
        "tier1_instant_dossier": "Tier 1: Instant Site Dossier ($49)",
        "tier2_strategic_audit": "Tier 2: 15-Page Strategic Asset Audit ($349)",
        "tier3_corridor_watch": "Tier 3: 30-Day Corridor Watch Desk ($599/mo)",
        "tier4_field_recon": "Tier 4: Ground & Remote Reconnaissance ($950+)"
    }
    label = plan_labels.get(plan, plan)

    # For Tier 1, compile the printable A4 5-page PDF in-memory
    pdf_bytes = None
    if plan == "tier1_instant_dossier":
        pdf_bytes = await generate_pdf_binary(asset_name=asset_name, lat=lat, lon=lon)

    # Save to persistent database
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        INSERT INTO client_assets (client_name, client_email, asset_name, latitude, longitude, radius_km, created_at, active)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0)
    """, (
        name, email, asset_name,
        float(lat or 0.0), float(lon or 0.0), float(radius_km or 300.0),
        datetime.now(timezone.utc).isoformat()
    ))
    conn.commit()
    conn.close()

    # Deliver Intake Alert via Resend HTTPS (Port 443)
    if RESEND_API_KEY:
        body = f"""THE BRINK WORLD // NEW INTAKE NOTIFICATION
----------------------------------------------------------------------
A prospective client submitted an order/inquiry via the Advisory Desk:

SERVICE TIER:       {label}
CLIENT NAME:        {name}
CORPORATE EMAIL:    {email}
ORGANIZATION:       {company or 'Not specified'}
TARGET ASSET/ROUTE: {asset_name}
SPECIFIED DETAILS:  {reason}
COORDINATES:        Lat: {lat}, Lon: {lon}
TIMESTAMP:          {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
----------------------------------------------------------------------
"""
        if plan == "tier1_instant_dossier":
            body += f"""FULFILLMENT INSTRUCTIONS (Tier 1):
The tailored 5-page printable A4 PDF dossier is attached to this email.
Once Razorpay confirms the $49 receipt, forward this attachment directly to {email}.
"""
        elif plan == "tier2_strategic_audit":
            body += f"""ACTION REQUIRED (Tier 2 - $349):
Review the target facility ({asset_name}) and scoping details.
Assemble the 15-page analytical dossier with historical seismic catalog and DEM flood modeling, then issue the corporate invoice.
"""
        elif plan == "tier3_corridor_watch":
            body += f"""ACTION REQUIRED (Tier 3 - $599/mo):
Review the waypoint nodes ({reason}). Establish the 30-day SQLite alert geofence array.
"""
        elif plan == "tier4_field_recon":
            body += f"""ACTION REQUIRED (Tier 4 - $950+ Urgent):
Check Sentinel-1/2 satellite pass availability for {asset_name} and review regional ground-scout coverage. Respond within 4 hours.
"""

        payload = {
            "from": "The Brink Intelligence <onboarding@resend.dev>",
            "to": [ADMIN_NOTIFICATION_EMAIL],
            "subject": f"🔔 NEW CLIENT INTAKE: {name} [{label}]",
            "text": body
        }

        if pdf_bytes:
            payload["attachments"] = [
                {
                    "filename": f"TheBrink_Dossier_{int(time.time())}.pdf",
                    "content": base64.b64encode(pdf_bytes).decode("utf-8")
                }
            ]

        headers = {"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"}
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post("https://api.resend.com/emails", json=payload, headers=headers) as resp:
                    print(f"[INTAKE DISPATCH] Status: {resp.status}")
        except Exception as e:
            print(f"[INTAKE DISPATCH ERROR] {e}")

    return {"status": "success", "message": "Intake processed successfully."}