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


def build_report_blocks(meta, data, answers):
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
                ["Regional earthquakes, 30 days / 350 km", str(data.get("quake_count_30d_350km", 0)), "Observed USGS catalog events at M2.5+ used for regional context; this is not a site-specific engineering hazard model."],
            ],
        },
    ]

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
        "figure": str(data.get("quake_count_30d_350km", 0)),
        "conf": "OBSERVED",
        "conf_class": "c-obs",
        "label": "USGS earthquakes in 30 days within 350 km (M2.5+)",
        "what": "A count of catalogued earthquakes around the selected point during the stated observation window.",
        "why": "It describes recent regional seismic activity. It does not by itself estimate building damage, recurrence probability or future earthquake likelihood.",
    }]
    if quakes:
        seismic_blocks.append({
            "kind": "table", "title": "Recent Regional Earthquakes",
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
        ["Elevation returned for point", _fmt(data.get("elevation_m"), " m"), "Open-Meteo coordinate response; useful terrain context, not a survey."],
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

    purpose_section = _purpose_blocks(data, answers)
    if purpose_section:
        sections.append(purpose_section)

    sections.extend([
        {"title": "Operational Context", "subtitle": "Mapped access, emergency-service and helpline context around the selected point.", "blocks": access_blocks},
        {"title": "Sources & Confidence", "subtitle": "Evidence provenance and the rules used to interpret it.", "blocks": evidence_blocks},
        {"title": "Terms, Limits & Responsible Use", "subtitle": "What this dossier can and cannot establish.", "blocks": limitations_blocks},
    ])
    return cover, sections
