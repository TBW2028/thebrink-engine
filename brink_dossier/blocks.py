from datetime import datetime


def _fmt(value, suffix=""):
    if value is None or value == "":
        return "Not resolved from current source"
    return f"{value}{suffix}"


def _when(value):
    if not value:
        return "Recent"
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.strftime("%d %b %Y %H:%M UTC")
    except Exception:
        return str(value)


def _hazard_rows(items, limit=12):
    rows = []
    for h in items[:limit]:
        cat = str(h.get("category") or "hazard").replace("_", " ").title()
        name = h.get("name") or "Monitored signal"
        tier = h.get("severity_tier") or "Monitor"
        distance = f"{h.get('distance_km')} km" if h.get("distance_km") is not None else "—"
        source = h.get("source") or "Source unavailable"
        rows.append([cat, name, tier, distance, source])
    return rows


def _service_value(service, search_radius_km, label):
    if service:
        name = service.get("name") or f"Mapped {label}"
        dist = service.get("distance_km")
        phone = service.get("phone")
        value = f"{name} · {dist} km" if dist is not None else name
        if phone:
            value += f" · {phone}"
        return value
    return f"No mapped {label} resolved within {search_radius_km} km in OpenStreetMap query"


def _purpose_blocks(data, answers):
    purpose = str(answers.get("occupancy") or "").lower()
    summary = data.get("forecast_summary") or {}
    emergency = data.get("emergency_contacts") or []

    if "agric" in purpose or "farm" in purpose or "land" in purpose:
        ag = data.get("agriculture_context") or {}
        rows = [
            ["Current broad crop-season context", ag.get("season_label") or "Not resolved", ag.get("season_note") or "Season varies by crop and locality."],
            ["7-day forecast rainfall", _fmt(summary.get("rain_total_7d_mm"), " mm"), "Useful for field access, soil wetness, harvest drying and irrigation planning; compare with local observations."],
            ["Forecast rain days", _fmt(summary.get("rain_days_7d")), "Days with at least 1 mm modelled precipitation in the current seven-day window."],
            ["Highest forecast wind", _fmt(summary.get("max_wind_7d_kmh"), " km/h"), "Wind can affect spraying, exposed crops, temporary structures and field operations."],
            ["Forecast temperature range", f"{_fmt(summary.get('min_temp_7d_c'), '°C')} to {_fmt(summary.get('max_temp_7d_c'), '°C')}", "Use as near-term weather context, not a crop-specific threshold assessment."],
        ]
        blocks = [{
            "kind": "table",
            "title": "Agriculture & Field Operations",
            "headers": ["FACTOR", "CURRENT CONTEXT", "WHY IT MATTERS"],
            "rows": rows,
        }]
        blocks.append({
            "kind": "flag",
            "title": "FARM DECISION NOTE",
            "severe": False,
            "text": (
                "Sowing, irrigation, spraying and harvest timing depend on crop, variety, altitude, soil and local field conditions. "
                "For Indian locations, use the relevant IMD Agromet/GKMS district advisory alongside this dossier before making crop-specific decisions."
            )
        })
        return {"title": "Agriculture & Field Operations", "subtitle": "Weather and seasonal context relevant to farm decisions.", "blocks": blocks}

    if "supply" in purpose or "logistic" in purpose:
        rows = [
            ["Mapped major roads within 5 km", str(len(data.get("mapped_primary_roads_5km") or [])), "A map-coverage indicator for nearby primary/secondary/tertiary access, not a guarantee that routes are open."],
            ["Nearest mapped rail station", _service_value(data.get("nearest_railway_station"), data.get("transport_search_radius_km") or 120, "rail station"), "Straight-line distance; confirm timetable and road access separately."],
            ["Nearest mapped aerodrome", _service_value(data.get("nearest_aerodrome"), data.get("transport_search_radius_km") or 120, "aerodrome"), "Straight-line distance; airport capability and commercial service are not inferred."],
            ["Official warnings within 300 km", str(len(data.get("official_warnings_300km") or [])), "Warnings can indicate potential route, loading, staffing or continuity disruption."],
            ["7-day forecast rainfall", _fmt(summary.get("rain_total_7d_mm"), " mm"), "Useful for road-surface, loading-yard and delay planning, especially where drainage or slope stability is sensitive."],
        ]
        return {
            "title": "Supply Chain & Access",
            "subtitle": "Transport continuity and near-term operating context.",
            "blocks": [{"kind": "table", "title": "Logistics Decision Context", "headers": ["FACTOR", "CURRENT CONTEXT", "OPERATIONAL READING"], "rows": rows}]
        }

    if "travel" in purpose or "journey" in purpose:
        rows = [
            ["Nearest mapped medical facility", _service_value(data.get("nearest_hospital_or_clinic"), data.get("service_search_radius_km") or 80, "hospital/clinic"), "Useful emergency-access context; confirm opening hours and capability locally."],
            ["Nearest mapped police", _service_value(data.get("nearest_police"), data.get("service_search_radius_km") or 80, "police station"), "Mapped proximity only; use official emergency channels in an emergency."],
            ["Nearest mapped rail station", _service_value(data.get("nearest_railway_station"), data.get("transport_search_radius_km") or 120, "rail station"), "Straight-line distance; not a routing estimate."],
            ["Nearest mapped aerodrome", _service_value(data.get("nearest_aerodrome"), data.get("transport_search_radius_km") or 120, "aerodrome"), "Straight-line distance; not confirmation of passenger service."],
            ["7-day rainfall / max wind", f"{_fmt(summary.get('rain_total_7d_mm'), ' mm')} / {_fmt(summary.get('max_wind_7d_kmh'), ' km/h')}", "Useful for journey timing and exposed-road context; check local transport advisories before departure."],
        ]
        return {
            "title": "Travel & Journey Context",
            "subtitle": "Near-term conditions, access and emergency context for the destination.",
            "blocks": [{"kind": "table", "title": "Journey Planning Context", "headers": ["FACTOR", "CURRENT CONTEXT", "HOW TO USE IT"], "rows": rows}]
        }

    if "home" in purpose or "property" in purpose or "commercial" in purpose or "facility" in purpose:
        rows = [
            ["Nearest mapped fire station", _service_value(data.get("nearest_fire_station"), data.get("service_search_radius_km") or 80, "fire station"), "Straight-line map distance, not response time."],
            ["Nearest mapped hospital/clinic", _service_value(data.get("nearest_hospital_or_clinic"), data.get("service_search_radius_km") or 80, "hospital/clinic"), "Straight-line map distance, not travel time or service capability."],
            ["Nearest mapped police", _service_value(data.get("nearest_police"), data.get("service_search_radius_km") or 80, "police station"), "Mapped proximity only."],
            ["Mapped major roads within 5 km", str(len(data.get("mapped_primary_roads_5km") or [])), "Useful access redundancy context; route condition and passability must be checked separately."],
        ]
        return {
            "title": "Property & Emergency Access",
            "subtitle": "Mapped services and access context around the selected location.",
            "blocks": [{"kind": "table", "title": "Local Operational Context", "headers": ["FACTOR", "CURRENT CONTEXT", "INTERPRETATION"], "rows": rows}]
        }

    return None



def _commercial_product_section(meta, data, answers):
    product_type = str(meta.get("product_type") or "location_dossier")
    if product_type == "location_dossier":
        return None

    official = data.get("official_warnings_300km") or []
    local = data.get("live_hazards_300km") or []
    summary = data.get("forecast_summary") or {}
    quake_count = data.get("quake_count_30d_350km", 0)

    if product_type == "facility_risk_passport":
        rows = [
            ["Official warnings ≤300 km", str(len(official)), "Current authoritative-warning environment resolved by the configured feeds."],
            ["Resolved live signals ≤300 km", str(len(local)), "Current geolocated monitored events/warnings around the facility."],
            ["Earthquakes · 24h / 350 km / M1.0+", str(data.get("quake_count_24h_350km_m1", 0)), "Primary live seismic screen; includes small catalogued events."],
            ["Earthquakes · 30d / 350 km / M2.5+", str(quake_count), "Broader regional seismic context; not a structural-damage estimate."],
            ["7-day forecast rainfall", _fmt(summary.get("rain_total_7d_mm"), " mm"), "Near-term interruption and drainage context."],
            ["Peak forecast wind", _fmt(summary.get("max_wind_7d_kmh"), " km/h"), "Near-term exposed-operations context."],
            ["Mapped major roads ≤5 km", str(len(data.get("mapped_primary_roads_5km") or [])), "Access-context indicator; does not confirm current route passability."],
        ]
        return {
            "title": "Facility Monitoring Record",
            "subtitle": "Current external-risk evidence maintained for this facility.",
            "blocks": [
                {"kind": "table", "title": "Facility Risk Register Snapshot", "headers": ["MONITORED DOMAIN", "CURRENT EVIDENCE", "USE"], "rows": rows},
                {"kind": "flag", "title": "MATERIAL-CHANGE RULE", "severe": False, "text": "A recurring Facility Risk Passport should be regenerated when a new official warning, significant nearby hazard signal, or other defined monitoring trigger materially changes the facility's external operating environment."},
            ],
        }

    if product_type == "physical_risk_evidence_pack":
        rows = [
            ["Acute official-warning evidence", str(len(official)), "Current official-warning count within the analysis radius."],
            ["Current hazard-event evidence", str(len(local)), "Current resolved operational signals within the analysis radius."],
            ["Seismic observation window", f"{quake_count} events", "30-day observed USGS context within 350 km at M2.5+."],
            ["Near-term precipitation", _fmt(summary.get("rain_total_7d_mm"), " mm"), "Short-horizon modelled weather evidence."],
            ["Temperature range", f"{_fmt(summary.get('min_temp_7d_c'), '°C')} to {_fmt(summary.get('max_temp_7d_c'), '°C')}", "Near-term modelled temperature envelope."],
            ["Evidence timestamp", _when(data.get("retrieved_at")), "Evidence should be refreshed for reporting periods and material changes."],
        ]
        return {
            "title": "Physical Risk Evidence Register",
            "subtitle": "Structured evidence that can support climate-risk, sustainability and enterprise-risk assessment workflows.",
            "blocks": [
                {"kind": "table", "title": "Evidence Register", "headers": ["EVIDENCE DOMAIN", "CURRENT EVIDENCE", "REPORTING USE"], "rows": rows},
                {"kind": "flag", "title": "REPORTING BOUNDARY", "severe": False, "text": "This pack supplies external physical-risk evidence. Materiality, financial effects, transition risk, governance, strategy, scenario analysis and framework-specific disclosures remain the reporting entity's responsibility unless separately commissioned."},
            ],
        }

    if product_type == "pre_underwriting_site_intelligence":
        rows = [
            ["Current official warnings ≤300 km", str(len(official)), "Current authority-issued warning environment."],
            ["Current hazard signals ≤300 km", str(len(local)), "Observed/reported event context around the site."],
            ["Regional seismic activity", f"{quake_count} M2.5+ events / 30d / 350 km", "Seismic context only; does not estimate damage probability or loss."],
            ["Elevation", _fmt(data.get("elevation_m"), " m"), "Terrain context; not a survey elevation."],
            ["Nearest mapped fire station", _service_value(data.get("nearest_fire_station"), data.get("service_search_radius_km") or 80, "fire station"), "Mapped proximity only; not response time."],
            ["Mapped major roads ≤5 km", str(len(data.get("mapped_primary_roads_5km") or [])), "Access redundancy context only."],
        ]
        return {
            "title": "Pre-Underwriting Evidence",
            "subtitle": "External-site evidence for broker, risk-survey and commercial-property review.",
            "blocks": [
                {"kind": "table", "title": "Site Evidence Register", "headers": ["UNDERWRITING QUESTION", "CURRENT EVIDENCE", "BOUNDARY"], "rows": rows},
                {"kind": "flag", "title": "UNDERWRITING BOUNDARY", "severe": False, "text": "No premium, insurability, probable maximum loss, policy term or claims decision is inferred. The report supplies external evidence for a qualified underwriting or risk-survey process."},
            ],
        }

    if product_type == "business_continuity_threat_register":
        rows = [
            ["Severe weather / official warning", str(len(official)), "Trigger: new authority warning intersecting the monitoring radius.", "Operations / access / workforce"],
            ["Nearby hazard event", str(len(local)), "Trigger: new Significant/Severe/Critical event within the defined radius.", "Site continuity / transport / utilities"],
            ["Heavy rain / flood pressure", _fmt(summary.get("rain_total_7d_mm"), " mm forecast"), "Trigger: site-defined precipitation threshold or official flood warning.", "Access / drainage / logistics"],
            ["Wind exposure", _fmt(summary.get("max_wind_7d_kmh"), " km/h"), "Trigger: site-defined wind limit or authority warning.", "Outdoor work / loading / temporary structures"],
            ["Regional seismicity", f"{quake_count} M2.5+ events / 30d", "Trigger: significant regional event or sequence requiring facility review.", "Facility / utilities / access"],
        ]
        return {
            "title": "External Threat Register",
            "subtitle": "A structured external-threat input for business-continuity and operational-resilience review.",
            "blocks": [
                {"kind": "table", "title": "Threat Register", "headers": ["THREAT", "CURRENT EVIDENCE", "MONITORING TRIGGER", "DEPENDENCY"], "rows": rows},
                {"kind": "flag", "title": "BCMS BOUNDARY", "severe": False, "text": "The organisation should set its own impact tolerances, recovery objectives, escalation thresholds, owners and continuity actions. This register supplies external threat evidence; it is not a business-impact analysis or certification."},
            ],
        }

    return None


def _titleize(value):
    return str(value or "").replace("_", " ").title()


def _public_gap_reason(hazard, reason):
    """Translate internal source/API failures into client-safe evidence-status language."""
    text = str(reason or "").lower()
    hazard_label = _titleize(hazard)
    if not reason:
        return "Required evidence was not available for this reporting cycle."
    if "licen" in text or "required licences" in text:
        return "The required source entitlement/terms were not active for this reporting cycle; the evidence remains unresolved."
    if "cost limits exceeded" in text or "request is too large" in text:
        return "The historical source request could not be completed within provider retrieval limits; the evidence remains unresolved."
    if "aqueduct" in text and ("could not be identified" in text or "baseline" in text):
        return "The water-risk source package could not be resolved into a defensible site-level result for this reporting cycle."
    if "firms" in text or "map_key" in text:
        return "Operational satellite fire data were not available to the reporting pipeline for this cycle."
    if "403" in text or "forbidden" in text or "http" in text or "api/" in text:
        return "The external source could not be retrieved successfully for this reporting cycle."
    return f"{hazard_label} evidence remains unresolved from the current source set."


def _risk_finding_rows(risk_findings):
    rows = []
    for spec in risk_findings or []:
        rec = spec.get("record") or {}
        rows.append([
            _titleize(rec.get("hazard_type")),
            _titleize(rec.get("materiality")),
            _titleize(rec.get("facility_sensitivity")),
            _titleize(rec.get("confidence")),
            rec.get("finding_text") or "—",
        ])
    return rows


def _v2_institutional_sections(meta, data, answers, risk_findings):
    profile = answers.get("facility_profile") or {}
    sections = []

    profile_rows = [
        ["Construction", profile.get("construction_type") or "Not supplied"],
        ["Year built", profile.get("year_built") or "Not supplied"],
        ["Floors above ground", profile.get("floors_above_ground") or "Not supplied"],
        ["Basement / below-ground area", profile.get("basement_present") or "Not supplied"],
        ["Critical equipment level", profile.get("critical_equipment_level") or "Not supplied"],
        ["Backup power", profile.get("backup_power") or "Not supplied"],
        ["Water dependency", profile.get("water_dependency") or "Not supplied"],
        ["Cooling / HVAC dependency", profile.get("cooling_dependency") or "Not supplied"],
        ["Practical road access routes", profile.get("practical_access_routes") or "Not supplied"],
        ["Drainage / flood protection", profile.get("drainage_protection") or "Not supplied"],
    ]
    sections.append({
        "title": "Site & Asset Profile",
        "subtitle": "Client-declared facility characteristics used to interpret external hazard evidence.",
        "blocks": [
            {"kind": "kvtable", "title": "Facility Vulnerability Profile", "rows": profile_rows},
            {"kind": "flag", "title": "CLIENT-DECLARED DATA", "severe": False, "text": "Facility characteristics are client-declared unless explicitly stated otherwise. They have not been independently inspected, measured or certified by The Brink World."},
        ],
    })

    finding_rows = _risk_finding_rows(risk_findings)
    sections.append({
        "title": "Physical-Risk Materiality Screen",
        "subtitle": "Deterministic screening findings under the current Brink methodology.",
        "blocks": [
            {"kind": "table", "title": "Hazard Materiality Register", "headers": ["HAZARD", "MATERIALITY", "SENSITIVITY", "CONFIDENCE", "CURRENT FINDING"], "rows": finding_rows or [["—","—","—","—","No V2 findings generated."]]},
            {"kind": "flag", "title": "MATERIALITY RULE", "severe": False, "text": "Material means a plausible facility-impact pathway is supported by the available hazard evidence and sensitivity information. Monitor means evidence is relevant but not sufficient for a material conclusion. Evidence Gap means the required evidence is unresolved. Low Relevance is used only where the current evidence supports that narrower conclusion."},
        ],
    })

    heat = data.get("historical_heat") or {}
    rain = data.get("historical_rainfall") or {}
    flood = data.get("river_flood") or {}
    water = data.get("water_risk") or {}
    terrain = data.get("terrain") or {}
    cyclone = data.get("cyclone_history") or {}
    fire = data.get("fire_context") or {}

    if heat.get("status") == "ok":
        heat_rows = [
            ["Historical baseline", heat.get("baseline_period") or "Not available"],
            ["P95 daily maximum temperature", _fmt(heat.get("p95_tmax_c"), "°C")],
            ["P99 daily maximum temperature", _fmt(heat.get("p99_tmax_c"), "°C")],
            ["Mean annual days ≥35°C", _fmt(heat.get("mean_annual_days_ge_35c"), " days/year")],
            ["Mean annual days ≥40°C", _fmt(heat.get("mean_annual_days_ge_40c"), " days/year")],
            ["Mean annual nights ≥25°C", _fmt(heat.get("mean_annual_nights_ge_25c"), " nights/year")],
            ["Recent mean annual days ≥35°C", _fmt(heat.get("recent_mean_annual_days_ge_35c"), " days/year")],
        ]
        heat_blocks = [
            {"kind": "kvtable", "title": "Heat Evidence", "rows": heat_rows},
            {"kind": "flag", "title": "HEAT LIMITATION", "severe": False, "text": heat.get("limitations") or "Historical heat evidence is unavailable."},
        ]
    else:
        heat_blocks = [{
            "kind": "flag",
            "title": "HEAT EVIDENCE STATUS — UNRESOLVED",
            "severe": False,
            "text": _public_gap_reason("heat", heat.get("reason")),
        }]
    sections.append({
        "title": "Extreme Heat",
        "subtitle": "Historical heat baseline from ERA5-Land with facility-sensitivity interpretation.",
        "blocks": heat_blocks,
    })

    flood_rows = []
    if flood.get("status") == "ok":
        for rp in ("10","20","50","75","100","200","500"):
            rec = (flood.get("depths") or {}).get(rp) or {}
            flood_rows.append([
                f"{rp}-year",
                _fmt(rec.get("point_depth_m"), " m"),
                _fmt(rec.get("nearby_max_depth_m"), " m"),
                _fmt(rec.get("nearest_inundated_cell_distance_km"), " km"),
            ])
    else:
        flood_rows = [["—", "Unavailable", "Unavailable", "Unavailable"]]

    flood_blocks = []
    if rain.get("status") == "ok":
        rain_rows = [
            ["Historical baseline", rain.get("baseline_period") or "Not available"],
            ["Mean annual precipitation", _fmt(rain.get("mean_annual_precip_mm"), " mm/year")],
            ["Mean annual wet days", _fmt(rain.get("mean_annual_wet_days"), " days/year")],
            ["Mean annual days ≥20 mm", _fmt(rain.get("mean_annual_days_ge_20mm"), " days/year")],
            ["Mean annual days ≥50 mm", _fmt(rain.get("mean_annual_days_ge_50mm"), " days/year")],
            ["P95 wet-day precipitation", _fmt(rain.get("p95_wet_day_mm"), " mm/day")],
            ["Mean annual Rx1day", _fmt(rain.get("mean_annual_rx1day_mm"), " mm/day")],
            ["Mean annual Rx5day", _fmt(rain.get("mean_annual_rx5day_mm"), " mm/5 days")],
        ]
        flood_blocks.append({"kind": "kvtable", "title": "Extreme Rainfall Evidence", "rows": rain_rows})
    else:
        flood_blocks.append({
            "kind": "flag",
            "title": "EXTREME-RAINFALL EVIDENCE STATUS — UNRESOLVED",
            "severe": False,
            "text": _public_gap_reason("extreme rainfall", rain.get("reason")),
        })

    if flood.get("status") == "ok":
        flood_blocks.append({
            "kind": "table",
            "title": "Riverine Flood Depth Screen",
            "headers": ["RETURN PERIOD", "SITE GRID CELL", "NEIGHBOURHOOD MAX", "NEAREST ≥0.1 M CELL"],
            "rows": flood_rows,
        })
    else:
        flood_blocks.append({
            "kind": "flag",
            "title": "RIVERINE-FLOOD EVIDENCE STATUS — UNRESOLVED",
            "severe": False,
            "text": _public_gap_reason("flood", flood.get("reason")),
        })
    flood_blocks.append({
        "kind": "flag",
        "title": "FLOOD BOUNDARY",
        "severe": False,
        "text": "Riverine flood mapping does not establish pluvial drainage flooding, finished-floor elevation, building ingress or local drainage performance. Neighbourhood maximum depth is not an on-site depth; the nearest-inundated-cell distance is provided to make that distinction explicit.",
    })
    sections.append({
        "title": "Flood & Extreme Rainfall",
        "subtitle": "Historical rainfall intensity plus modelled riverine inundation screening.",
        "blocks": flood_blocks,
    })

    if water.get("status") == "ok":
        wb = water.get("baseline") or {}
        future = water.get("future_water_stress") or {}
        water_rows = [
            ["Baseline water stress", wb.get("water_stress_label") or "Not available"],
            ["Baseline water depletion", wb.get("water_depletion_label") or "Not available"],
            ["Interannual variability", wb.get("interannual_variability_label") or "Not available"],
            ["Seasonal variability", wb.get("seasonal_variability_label") or "Not available"],
            ["Drought risk", wb.get("drought_risk_label") or "Not available"],
        ]
        future_rows = []
        for scenario, years in future.items():
            for year in ("2030","2050","2080"):
                rec = (years or {}).get(year) or {}
                if rec:
                    future_rows.append([
                        scenario.replace("_"," ").title(),
                        year,
                        rec.get("label") or rec.get("category") or _fmt(rec.get("score")),
                    ])
        water_blocks = [
            {"kind": "kvtable", "title": "Baseline Water-Risk Context", "rows": water_rows},
            {"kind": "table", "title": "Future Water-Stress Scenarios", "headers": ["SCENARIO", "HORIZON", "PROJECTED CATEGORY"], "rows": future_rows or [["—","—","Future values not resolved."]]},
            {"kind": "flag", "title": "WATER-RISK BOUNDARY", "severe": False, "text": water.get("limitations") or "Aqueduct evidence is unavailable."},
        ]
    else:
        water_blocks = [{
            "kind": "flag",
            "title": "WATER-RISK EVIDENCE STATUS — UNRESOLVED",
            "severe": False,
            "text": _public_gap_reason("water stress", water.get("reason")),
        }]
    sections.append({
        "title": "Water Stress & Drought",
        "subtitle": "Basin-level Aqueduct 4.0 baseline and forward-looking water-stress screening.",
        "blocks": water_blocks,
    })

    t250 = terrain.get("metrics_250m") or {}
    t1k = terrain.get("metrics_1km") or {}
    terrain_rows = [
        ["Point elevation", _fmt(terrain.get("point_elevation_m"), " m")],
        ["250 m mean slope", _fmt(t250.get("slope_mean_deg"), "°")],
        ["250 m P95 slope", _fmt(t250.get("slope_p95_deg"), "°")],
        ["250 m relief", _fmt(t250.get("relief_m"), " m")],
        ["1 km mean slope", _fmt(t1k.get("slope_mean_deg"), "°")],
        ["1 km P95 slope", _fmt(t1k.get("slope_p95_deg"), "°")],
        ["1 km maximum slope", _fmt(t1k.get("slope_max_deg"), "°")],
        ["1 km relief", _fmt(t1k.get("relief_m"), " m")],
    ]
    sections.append({
        "title": "Terrain, Landslide & Access",
        "subtitle": "DEM-derived terrain characterization and access sensitivity.",
        "blocks": [
            {"kind": "kvtable", "title": "Terrain Evidence", "rows": terrain_rows},
            {"kind": "flag", "title": "LANDSLIDE BOUNDARY", "severe": False, "text": terrain.get("limitations") or "Terrain evidence is unavailable. DEM slope and relief do not establish landslide probability."},
        ],
    })

    cyclone_rows = [
        ["Storm tracks within 100 km since 1980", _fmt(cyclone.get("storm_count_within_100km"))],
        ["Storm tracks within 250 km since 1980", _fmt(cyclone.get("storm_count_within_250km"))],
        ["Storm tracks within 500 km since 1980", _fmt(cyclone.get("storm_count_within_500km"))],
        ["Nearest historical track", _fmt((cyclone.get("nearest_storm") or {}).get("nearest_distance_km"), " km")],
        ["Peak reported WMO storm intensity within 250 km", _fmt(cyclone.get("max_reported_wmo_wind_within_250km_kt"), " kt")],
    ]
    sections.append({
        "title": "Wind, Cyclone & Severe Weather",
        "subtitle": "Historical tropical-cyclone track context, separate from structural design-wind assessment.",
        "blocks": [
            {"kind": "kvtable", "title": "Tropical-Cyclone History", "rows": cyclone_rows},
            {"kind": "flag", "title": "WIND BOUNDARY", "severe": False, "text": cyclone.get("limitations") or "Historical tropical-cyclone evidence is unavailable. Non-tropical severe wind and structural design loads remain separate evidence needs."},
        ],
    })

    if fire.get("status") == "ok":
        fire_rows = [
            ["Thermal anomalies ≤5 km / 5 days", _fmt(fire.get("detection_count_within_5km"))],
            ["Thermal anomalies ≤10 km / 5 days", _fmt(fire.get("detection_count_within_10km"))],
            ["Thermal anomalies ≤25 km / 5 days", _fmt(fire.get("detection_count_within_25km"))],
            ["Nearest thermal anomaly", _fmt((fire.get("nearest_detection") or {}).get("distance_km"), " km")],
            ["Peak nearby Fire Radiative Power", _fmt((fire.get("peak_frp_detection") or {}).get("frp_mw"), " MW")],
        ]
        fire_blocks = [
            {"kind": "kvtable", "title": "Operational Fire Context", "rows": fire_rows},
            {"kind": "flag", "title": "WILDFIRE BOUNDARY", "severe": False, "text": fire.get("limitations") or "Long-horizon wildfire susceptibility remains a separate evidence need."},
        ]
    else:
        fire_blocks = [{
            "kind": "flag",
            "title": "WILDFIRE EVIDENCE STATUS — UNRESOLVED",
            "severe": False,
            "text": _public_gap_reason("wildfire", fire.get("reason")),
        }]
    sections.append({
        "title": "Wildfire & Fire Activity",
        "subtitle": "Current satellite thermal-anomaly context with long-horizon wildfire kept separate.",
        "blocks": fire_blocks,
    })

    action_rows = []
    gap_rows = []
    for spec in risk_findings or []:
        rec = spec.get("record") or {}
        hazard = _titleize(rec.get("hazard_type"))
        mat = str(rec.get("materiality") or "").lower()
        if rec.get("recommended_action"):
            action_rows.append([
                hazard,
                _titleize(rec.get("action_type")),
                rec.get("recommended_action"),
                "Yes" if rec.get("specialist_review_required") else "No",
            ])
        if mat == "evidence_gap":
            gap_rows.append([
                hazard,
                rec.get("finding_text") or "Evidence unresolved",
                _public_gap_reason(rec.get("hazard_type"), rec.get("confidence_reason")),
            ])

    sections.append({
        "title": "Resilience & Due-Diligence Action Register",
        "subtitle": "Actions generated from current findings; these are decision-support priorities, not engineering instructions.",
        "blocks": [
            {"kind": "table", "title": "Action Register", "headers": ["DOMAIN", "ACTION TYPE", "RECOMMENDED NEXT STEP", "SPECIALIST REVIEW"], "rows": action_rows or [["—","—","No action generated.","—"]]},
        ],
    })

    sections.append({
        "title": "Data Gaps & Reliance Conditions",
        "subtitle": "Unresolved evidence that constrains the conclusions in this dossier.",
        "blocks": [
            {"kind": "table", "title": "Evidence Gaps", "headers": ["DOMAIN", "UNRESOLVED QUESTION", "WHY CONFIDENCE IS LIMITED"], "rows": gap_rows or [["—","No material evidence gaps recorded by the current rules.","—"]]},
            {"kind": "flag", "title": "NO SILENT ASSUMPTIONS", "severe": False, "text": "Unknown or unavailable evidence is not converted into a Low Relevance finding. The Brink World retains unresolved conditions explicitly until the required evidence is available."},
        ],
    })

    sections.append({
        "title": "Methodology & Model Governance",
        "subtitle": "Versioned rules governing this dossier.",
        "blocks": [
            {"kind": "kvtable", "title": "Governance Versions", "rows": [
                ["Methodology", meta.get("methodology_version") or "Not recorded"],
                ["Materiality rules", meta.get("materiality_rules_version") or "Not recorded"],
                ["Confidence rules", meta.get("confidence_rules_version") or "Not recorded"],
                ["Evidence schema", meta.get("evidence_schema_version") or "Not recorded"],
                ["Evidence retrieval timestamp", _when(data.get("retrieved_at"))],
            ]},
            {"kind": "flag", "title": "MODEL GOVERNANCE", "severe": False, "text": "This dossier combines observed, official-warning, mapped, client-declared and modelled evidence. Findings are generated by versioned deterministic rules. The Brink World does not infer insured loss, engineering adequacy, legal compliance, probable maximum loss or catastrophe-model output unless separately commissioned and supported by an appropriate specialist method."},
        ],
    })

    return sections


def build_report_blocks(meta, data, answers, risk_findings=None):
    strongest = data.get("strongest_local_signal")
    local = data.get("live_hazards_300km") or []
    nearby = data.get("live_hazards_1000km") or []
    official = data.get("official_warnings_300km") or []
    quakes = data.get("recent_quakes") or []
    weather = data.get("weather_current") or {}
    forecast = data.get("forecast_days") or []

    posture = strongest.get("severity_tier") if strongest else "No Current Resolved Signal"
    posture_color = {
        "Critical": "#9E2B25",
        "Severe": "#B44A32",
        "Significant": "#B8892B",
        "Monitor": "#496A86",
        "No Current Resolved Signal": "#3F6B4A",
    }.get(posture, "#5C6472")

    if strongest:
        category = str(strongest.get("category") or "hazard").replace("_", " ")
        summary_line = (
            f"Strongest resolved signal within 300 km: {category} · "
            f"{strongest.get('severity_tier', 'Monitor')} · {strongest.get('distance_km', '—')} km"
        )
    else:
        summary_line = "No currently active geolocated signal is resolved within 300 km in the configured operational feeds."

    cover = {
        "posture": posture,
        "posture_color": posture_color,
        "summary_line": summary_line,
        "signal_count": len(local),
        "warning_count": len(official),
        "retrieved_at": data.get("retrieved_at"),
    }

    executive_blocks = [
        {
            "kind": "flag",
            "title": "PURPOSE OF THIS REPORT",
            "severe": False,
            "text": meta.get("product_intended_use") or "Location intelligence and external-risk context.",
        },
        {
            "kind": "flag",
            "title": "EXECUTIVE READING",
            "severe": posture in {"Critical", "Severe"},
            "text": (
                f"{len(local)} active geolocated hazard signal(s) are resolved within 300 km of the selected point, "
                f"including {len(official)} official warning signal(s). {summary_line} "
                "This is a current operational picture, not a prediction that a disaster will or will not occur."
            ),
        },
        {
            "kind": "table",
            "title": "Management Snapshot",
            "headers": ["QUESTION", "CURRENT ANSWER", "HOW TO READ IT"],
            "rows": [
                ["Official warnings within 300 km", str(len(official)), "A warning count describes what participating authorities currently publish and what the system can geolocate; zero does not prove zero hazard."],
                ["Resolved live signals within 300 km", str(len(local)), "Includes monitored event and warning feeds with usable coordinates."],
                ["Earthquakes, last 24 hours / 350 km", str(data.get("quake_count_24h_350km_m1", 0)), "All USGS catalog events returned at M1.0+ in the latest 24 hours within 350 km. A separate 30-day M2.5+ count is retained for broader context."],
            ],
        },
    ]

    materiality_rank = {"material": 0, "monitor": 1, "evidence_gap": 2, "low_relevance": 3}
    finding_records = [spec.get("record") or {} for spec in (risk_findings or [])]
    priority_findings = sorted(
        finding_records,
        key=lambda rec: (
            materiality_rank.get(str(rec.get("materiality") or "").lower(), 9),
            str(rec.get("hazard_type") or ""),
        ),
    )[:5]
    if priority_findings:
        executive_blocks.append({
            "kind": "table",
            "title": "Top Physical-Risk Findings",
            "headers": ["DOMAIN", "MATERIALITY", "CONFIDENCE", "MANAGEMENT READING"],
            "rows": [[
                _titleize(rec.get("hazard_type")),
                _titleize(rec.get("materiality")),
                _titleize(rec.get("confidence")),
                rec.get("finding_text") or "—",
            ] for rec in priority_findings],
        })
        priority_actions = [
            rec for rec in priority_findings
            if rec.get("recommended_action")
        ][:3]
        if priority_actions:
            executive_blocks.append({
                "kind": "table",
                "title": "Priority Due-Diligence Actions",
                "headers": ["DOMAIN", "NEXT STEP"],
                "rows": [[_titleize(rec.get("hazard_type")), rec.get("recommended_action")] for rec in priority_actions],
            })

    if official:
        warning_blocks = [
            {"kind": "table", "title": "Current Official Warning Signals", "headers": ["HAZARD", "WARNING / AREA", "TIER", "DISTANCE", "AUTHORITY / SOURCE"], "rows": _hazard_rows(official, 15)},
            {"kind": "flag", "title": "HOW TO READ THIS", "severe": False, "text": "Official warnings are authoritative notices from participating agencies or aggregators. Geographic coverage, warning geometry and publication practices differ by country and authority."},
        ]
    else:
        warning_blocks = [{
            "kind": "flag",
            "title": "NO CURRENT GEOLOCATED OFFICIAL WARNING WITHIN 300 KM",
            "severe": False,
            "text": "No matching active official warning is currently resolved over the 300 km analysis radius in the configured feeds. This is not a guarantee of safety and should not replace local emergency channels.",
        }]

    regional_blocks = [{
        "kind": "table",
        "title": "Nearest Resolved Hazard Signals",
        "headers": ["HAZARD", "SIGNAL", "TIER", "DISTANCE", "SOURCE"],
        "rows": _hazard_rows(nearby, 15) or [["—", "No geolocated monitored signal within 1,000 km", "—", "—", "—"]],
    }]

    seismic_blocks = [{
        "kind": "trio",
        "figure": str(data.get("quake_count_24h_350km_m1", 0)),
        "conf": "OBSERVED",
        "conf_class": "c-obs",
        "label": "USGS earthquakes in the last 24 hours within 350 km (M1.0+)",
        "what": "All USGS events returned within 350 km during the latest 24 hours at magnitude 1.0 and above.",
        "why": "This is the live operational seismic picture. Small events are included for awareness; magnitude, distance, depth and site vulnerability must be considered separately.",
    }]
    if quakes:
        seismic_blocks.append({
            "kind": "table", "title": "Earthquakes in the Last 24 Hours (M1.0+)",
            "headers": ["LOCATION", "MAGNITUDE", "DEPTH", "DISTANCE", "OBSERVED"],
            "rows": [[q.get("place"), f"M{q.get('mag')}", f"{q.get('depth_km')} km", f"{q.get('distance_km')} km", _when(q.get("observed_at"))] for q in quakes[:12]],
        })

    weather_blocks = [
        {"kind": "trio", "figure": _fmt(weather.get("temperature_c"), "°C"), "conf": "MODELLED", "conf_class": "c-mod", "label": "Current near-surface temperature", "what": "Current atmospheric estimate returned for the analysed coordinates.", "why": "Useful as present weather context; it is not a long-term climate-normal comparison."},
        {"kind": "trio", "figure": _fmt(weather.get("precipitation_mm"), " mm"), "conf": "MODELLED", "conf_class": "c-mod", "label": "Current precipitation", "what": "Modelled/current precipitation at the selected point.", "why": "A point value should be read alongside official rain/flood warnings and local observations, particularly in complex terrain."},
        {"kind": "trio", "figure": _fmt(weather.get("wind_kmh"), " km/h"), "conf": "MODELLED", "conf_class": "c-mod", "label": "Current 10 m wind speed", "what": "Near-surface wind estimate at the selected coordinates.", "why": "Local gusts and terrain effects can differ materially from a grid-point model value."},
    ]
    if forecast:
        weather_blocks.append({
            "kind": "table", "title": "Seven-Day Weather Outlook",
            "headers": ["DATE", "MAX / MIN", "PRECIPITATION", "MAX WIND"],
            "rows": [[d.get("date"), f"{_fmt(d.get('tmax_c'), '°C')} / {_fmt(d.get('tmin_c'), '°C')}", _fmt(d.get("precip_mm"), " mm"), _fmt(d.get("wind_max_kmh"), " km/h")] for d in forecast[:7]],
        })

    search_radius = data.get("service_search_radius_km") or 80
    access_rows = [
        ["Elevation at assessed point", _fmt(data.get("elevation_m"), " m"), "Copernicus DEM GLO-30 terrain context; not a survey-grade elevation."],
        ["Mapped primary/secondary/tertiary roads within 5 km", str(len(data.get("mapped_primary_roads_5km") or [])), "OpenStreetMap completeness varies by locality."],
        ["Nearest mapped fire station", _service_value(data.get("nearest_fire_station"), search_radius, "fire station"), "Straight-line map distance, not response time."],
        ["Nearest mapped hospital/clinic", _service_value(data.get("nearest_hospital_or_clinic"), search_radius, "hospital/clinic"), "Straight-line map distance, not travel time or service capability."],
        ["Nearest mapped police", _service_value(data.get("nearest_police"), search_radius, "police station"), "Mapped proximity only."],
    ]
    access_blocks = [{"kind": "table", "title": "Operational Context", "headers": ["CONTEXT", "VALUE", "INTERPRETATION"], "rows": access_rows}]

    emergency = data.get("emergency_contacts") or []
    if emergency:
        access_blocks.append({
            "kind": "table",
            "title": "Emergency Contact",
            "headers": ["SERVICE", "NUMBER", "SCOPE / SOURCE"],
            "rows": [[e.get("service"), e.get("number"), f"{e.get('scope')} · {e.get('source')}"] for e in emergency],
        })
    access_blocks.append({
        "kind": "flag",
        "title": "MAPPED-SERVICE LIMITATION",
        "severe": False,
        "text": f"Emergency-service facilities were searched in OpenStreetMap within {search_radius} km. A facility not returned by this query may still exist; absence from the map source is not evidence of absence in the real world.",
    })

    source_rows = [[s.get("name"), s.get("type"), s.get("note")] for s in data.get("sources", []) if s]
    evidence_blocks = [
        {"kind": "table", "title": "Source & Confidence Register", "headers": ["SOURCE", "EVIDENCE CLASS", "USE / LIMITATION"], "rows": source_rows},
        {"kind": "flag", "title": "EVIDENCE DISCIPLINE", "severe": False, "text": "OBSERVED means a reported event or mapped observation. OFFICIAL WARNING means an alert issued by an authority or authoritative aggregation. MODELLED means a numerical model or forecast. INTERPRETED means The Brink World's operational reading of those inputs. These classes are kept separate throughout the dossier."},
    ]

    limitations_blocks = [
        {"kind": "glossary", "term": "Distance", "def": "Unless otherwise stated, hazard and service distances are straight-line great-circle distances from the analysed coordinates, not road distance."},
        {"kind": "glossary", "term": "Official Warning", "def": "A warning published by a participating national authority or authoritative warning aggregation. Coverage differs between jurisdictions."},
        {"kind": "glossary", "term": "Modelled", "def": "A value generated by a numerical model or gridded dataset rather than directly measured at the exact site."},
        {"kind": "glossary", "term": "Not resolved from current source", "def": "The source queried did not return a defensible value. This wording does not mean the facility, hazard or condition does not exist."},
        {"kind": "flag", "title": "IMPORTANT LIMITATION", "severe": False, "text": (
            (meta.get("product_limitation") or "Decision-support intelligence only.") +
            " It is not an emergency alerting service or guarantee of future conditions. "
            "Always follow local authorities during an active emergency."
        )},
    ]

    sections = [
        {"title": "Executive Brief", "subtitle": "The management-level reading of current conditions.", "blocks": executive_blocks},
        {"title": "Official Warning Environment", "subtitle": "Active authoritative warnings resolved near the analysed point.", "blocks": warning_blocks},
        {"title": "Regional Hazard Signals", "subtitle": "Nearest current monitored signals across the operational feed.", "blocks": regional_blocks},
        {"title": "Seismic Context", "subtitle": "Observed USGS earthquake activity in the regional window.", "blocks": seismic_blocks},
        {"title": "Weather & Near-Term Conditions", "subtitle": "Current modelled atmospheric context and seven-day outlook.", "blocks": weather_blocks},
    ]

    institutional_sections = _v2_institutional_sections(meta, data, answers, risk_findings)
    sections.extend(institutional_sections)

    purpose_section = _purpose_blocks(data, answers)
    if purpose_section:
        sections.append(purpose_section)

    commercial_section = _commercial_product_section(meta, data, answers)
    if commercial_section:
        sections.append(commercial_section)

    sections.extend([
        {"title": "Operational Context", "subtitle": "Mapped access, emergency-service and helpline context around the selected point.", "blocks": access_blocks},
        {"title": "Sources & Confidence", "subtitle": "Evidence provenance and the rules used to interpret it.", "blocks": evidence_blocks},
        {"title": "Terms, Limits & Responsible Use", "subtitle": "What this dossier can and cannot establish.", "blocks": limitations_blocks},
    ])
    return cover, sections
