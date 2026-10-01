"""V2 evidence-ledger and deterministic screening findings for The Brink World.

This module does not create catastrophe-loss estimates or engineering conclusions.
It converts already-retrieved evidence and client-declared facility attributes into
traceable database payloads governed by TBW-PRM v1.0.
"""

from datetime import datetime, timezone


METHODOLOGY_VERSION = "TBW-PRM v1.0"
MATERIALITY_RULES_VERSION = "TBW-MAT v1.0"
CONFIDENCE_RULES_VERSION = "TBW-CONF v1.0"
EVIDENCE_SCHEMA_VERSION = "TBW-EVID v1.0"


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _evidence(
    report_run_id,
    facility_id,
    hazard_type,
    metric_code,
    metric_name,
    source_name,
    evidence_class,
    *,
    value_numeric=None,
    value_text=None,
    unit=None,
    source_dataset=None,
    source_reference=None,
    source_version=None,
    retrieved_at=None,
    observation_start=None,
    observation_end=None,
    spatial_resolution=None,
    temporal_resolution=None,
    scenario=None,
    time_horizon=None,
    confidence="unresolved",
    confidence_reason=None,
    limitations=None,
    raw_evidence=None,
):
    return {
        "report_run_id": report_run_id,
        "facility_id": facility_id,
        "hazard_type": hazard_type,
        "metric_code": metric_code,
        "metric_name": metric_name,
        "value_numeric": value_numeric,
        "value_text": value_text,
        "unit": unit,
        "source_name": source_name,
        "source_dataset": source_dataset,
        "source_reference": source_reference,
        "source_version": source_version,
        "evidence_class": evidence_class,
        "retrieved_at": retrieved_at or _now_iso(),
        "observation_start": observation_start,
        "observation_end": observation_end,
        "spatial_resolution": spatial_resolution,
        "temporal_resolution": temporal_resolution,
        "scenario": scenario,
        "time_horizon": time_horizon,
        "confidence": confidence,
        "confidence_reason": confidence_reason,
        "limitations": limitations,
        "raw_evidence": raw_evidence or {},
    }


def build_evidence_items(report_run_id, facility, profile, telemetry):
    """Build V2 evidence-ledger payloads from the current V1 telemetry/profile.

    These are deliberately screening-level records. Long-horizon flood, heat,
    water-stress, wind, wildfire and landslide evidence will be added by later
    source modules rather than inferred from current weather.
    """
    items = []
    facility_id = facility["id"]
    retrieved = telemetry.get("retrieved_at") or _now_iso()

    warnings = telemetry.get("official_warnings_300km") or []
    signals = telemetry.get("live_hazards_300km") or []
    quakes = telemetry.get("recent_quakes") or []
    current = telemetry.get("weather_current") or {}
    strongest = telemetry.get("strongest_local_signal")

    items.append(_evidence(
        report_run_id, facility_id, "multi_hazard_operational",
        "official_warning_count_300km", "Current official warnings resolved within 300 km",
        "The Brink World operational hazard layer", "official_warning",
        value_numeric=len(warnings), unit="count", retrieved_at=retrieved,
        temporal_resolution="current operational retrieval",
        confidence="medium",
        confidence_reason="Aggregates configured authoritative warning feeds, but geographic/feed coverage varies by source and country.",
        limitations="A zero resolved-warning count is not evidence that no hazard exists.",
        raw_evidence={"warnings": warnings[:25]},
    ))

    items.append(_evidence(
        report_run_id, facility_id, "multi_hazard_operational",
        "resolved_signal_count_300km", "Resolved live hazard signals within 300 km",
        "The Brink World operational hazard layer", "observed",
        value_numeric=len(signals), unit="count", retrieved_at=retrieved,
        temporal_resolution="current operational retrieval",
        confidence="medium",
        confidence_reason="Traceable operational feeds with usable coordinates; coverage varies by source.",
        limitations="Signal presence/absence does not establish long-term site hazard.",
        raw_evidence={"signals": signals[:25]},
    ))

    if strongest:
        items.append(_evidence(
            report_run_id, facility_id, str(strongest.get("category") or "multi_hazard").lower(),
            "strongest_live_signal_300km", "Strongest resolved live signal within 300 km",
            str(strongest.get("source") or "The Brink World operational hazard layer"), "observed",
            value_numeric=strongest.get("distance_km"), unit="km",
            value_text=f"{strongest.get('category') or 'Hazard'} · {strongest.get('severity_tier') or 'Unclassified'}",
            retrieved_at=retrieved,
            confidence="medium",
            confidence_reason="Observed/reported signal is geolocated and distance-resolved, subject to source-feed completeness.",
            limitations="A live signal is event context and is not a site-specific loss or structural-hazard estimate.",
            raw_evidence=strongest,
        ))

    items.append(_evidence(
        report_run_id, facility_id, "seismic",
        "usgs_quake_count_30d_350km", "USGS earthquakes M2.5+ within 350 km over 30 days",
        "USGS Earthquake Catalog", "observed",
        value_numeric=telemetry.get("quake_count_30d_350km") or 0, unit="count",
        source_dataset="USGS FDSN Event Web Service", retrieved_at=retrieved,
        observation_start=None, observation_end=retrieved,
        spatial_resolution="350 km radial screen", temporal_resolution="30 days",
        confidence="high",
        confidence_reason="Authoritative catalog query for the defined magnitude, radius and observation window.",
        limitations="Recent earthquake count does not estimate long-term ground-shaking hazard, recurrence or building damage.",
        raw_evidence={"events": quakes[:12]},
    ))

    weather_specs = [
        ("current_temperature_c", "heat", "Current modelled near-surface temperature", current.get("temperature_c"), "°C"),
        ("current_precipitation_mm", "rainfall", "Current modelled precipitation", current.get("precipitation_mm"), "mm"),
        ("current_wind_kmh", "wind", "Current modelled 10 m wind speed", current.get("wind_kmh"), "km/h"),
    ]
    for code, hazard, name, value, unit in weather_specs:
        if value is None:
            continue
        items.append(_evidence(
            report_run_id, facility_id, hazard, code, name,
            "Open-Meteo", "modelled",
            value_numeric=value, unit=unit, source_dataset="Open-Meteo Forecast API",
            retrieved_at=retrieved, temporal_resolution="current modelled condition",
            confidence="medium",
            confidence_reason="Established numerical weather source at the assessed coordinate, but not an on-site observation.",
            limitations="Current weather is operational context and must not be used alone as long-horizon physical-risk evidence.",
        ))

    if telemetry.get("elevation_m") is not None:
        items.append(_evidence(
            report_run_id, facility_id, "terrain",
            "elevation_m", "Modelled/mapped elevation at assessed coordinate",
            "Open-Meteo", "modelled",
            value_numeric=telemetry.get("elevation_m"), unit="m",
            source_dataset="Open-Meteo Forecast API", retrieved_at=retrieved,
            confidence="medium",
            confidence_reason="Coordinate-based elevation returned by the source.",
            limitations="Not survey-grade elevation and not sufficient to establish flood or landslide exposure.",
        ))

    roads = telemetry.get("mapped_primary_roads_5km") or []
    items.append(_evidence(
        report_run_id, facility_id, "operational_access",
        "mapped_major_roads_5km_count", "Mapped primary/secondary/tertiary roads within 5 km",
        "OpenStreetMap / Overpass", "mapped",
        value_numeric=len(roads), unit="count", retrieved_at=retrieved,
        spatial_resolution="5 km radial query",
        confidence="low",
        confidence_reason="Open mapping completeness varies materially by locality.",
        limitations="A zero query result is not evidence that no road exists. Local/authoritative verification is required before reliance.",
        raw_evidence={"mapped_roads": roads},
    ))

    service_specs = [
        ("nearest_fire_station", "mapped_fire_station", "Nearest mapped fire station"),
        ("nearest_hospital_or_clinic", "mapped_medical_service", "Nearest mapped hospital/clinic"),
        ("nearest_police", "mapped_police_station", "Nearest mapped police station"),
    ]
    for source_key, metric_code, metric_name in service_specs:
        rec = telemetry.get(source_key)
        if rec:
            items.append(_evidence(
                report_run_id, facility_id, "emergency_access",
                metric_code, metric_name,
                "OpenStreetMap / Overpass", "mapped",
                value_numeric=rec.get("distance_km"), unit="km",
                value_text=rec.get("name") or "Mapped facility resolved",
                retrieved_at=retrieved,
                spatial_resolution=f"{telemetry.get('service_search_radius_km') or 80} km service query",
                confidence="low",
                confidence_reason="Open mapping provides useful proximity context but does not establish response time, capability or completeness.",
                limitations="Mapped straight-line proximity only; authoritative/local verification is required for operational reliance.",
                raw_evidence=rec,
            ))
        else:
            items.append(_evidence(
                report_run_id, facility_id, "emergency_access",
                metric_code, metric_name,
                "OpenStreetMap / Overpass", "mapped",
                value_text="Not resolved from current source", retrieved_at=retrieved,
                spatial_resolution=f"{telemetry.get('service_search_radius_km') or 80} km service query",
                confidence="low",
                confidence_reason="The open-map query did not resolve a defensible facility result.",
                limitations="Non-return from OpenStreetMap is not evidence that the real-world service does not exist.",
            ))

    if profile:
        profile_fields = [
            ("construction_type", "facility", "Facility construction type"),
            ("year_built", "facility", "Approximate year built"),
            ("floors_above_ground", "facility", "Floors above ground"),
            ("basement_present", "flood", "Basement / below-ground operational area"),
            ("critical_equipment_level", "facility", "Critical equipment location"),
            ("backup_power", "utilities", "Backup electricity"),
            ("water_dependency", "water_stress", "Water dependency"),
            ("cooling_dependency", "heat", "Cooling / HVAC dependency"),
            ("practical_access_routes", "operational_access", "Practical road access routes"),
            ("drainage_protection", "flood", "Known drainage / flood protection"),
        ]
        for field, hazard, label in profile_fields:
            value = profile.get(field)
            if value is None or str(value).strip() == "":
                continue
            unknown = str(value).lower() == "unknown"
            numeric = value if isinstance(value, (int, float)) and not isinstance(value, bool) else None
            items.append(_evidence(
                report_run_id, facility_id, hazard,
                f"client_{field}", label,
                "Client facility declaration", "client_declared",
                value_numeric=numeric,
                value_text=None if numeric is not None else str(value),
                retrieved_at=profile.get("declared_at") or retrieved,
                confidence="unresolved" if unknown else "medium",
                confidence_reason=(
                    "Client reported this attribute as unknown."
                    if unknown else
                    "Client-declared facility information has not been independently inspected or verified by The Brink World."
                ),
                limitations="Client-declared information may require documentary or specialist verification for high-stakes reliance.",
            ))

    return items


def build_risk_findings(report_run_id, facility, profile, telemetry):
    """Return deterministic screening findings plus metric-code support links."""
    facility_id = facility["id"]
    warnings = telemetry.get("official_warnings_300km") or []
    signals = telemetry.get("live_hazards_300km") or []
    quake_count = telemetry.get("quake_count_30d_350km") or 0
    profile = profile or {}
    findings = []

    def add(
        hazard_type,
        materiality,
        finding_text,
        *,
        sensitivity="unresolved",
        reasoning=None,
        consequence=None,
        confidence="unresolved",
        confidence_reason=None,
        action_type=None,
        action=None,
        specialist=False,
        gross=None,
        residual=None,
        support=None,
    ):
        findings.append({
            "record": {
                "report_run_id": report_run_id,
                "facility_id": facility_id,
                "hazard_type": hazard_type,
                "materiality": materiality,
                "facility_sensitivity": sensitivity,
                "finding_text": finding_text,
                "reasoning_summary": reasoning,
                "operational_consequence": consequence,
                "confidence": confidence,
                "confidence_reason": confidence_reason,
                "action_type": action_type,
                "recommended_action": action,
                "specialist_review_required": specialist,
                "gross_risk_context": gross,
                "residual_concern": residual,
                "methodology_version": METHODOLOGY_VERSION,
                "materiality_rules_version": MATERIALITY_RULES_VERSION,
                "confidence_rules_version": CONFIDENCE_RULES_VERSION,
            },
            "supporting_metric_codes": support or [],
        })

    add(
        "multi_hazard_operational",
        "monitor",
        (
            f"{len(warnings)} current official warning(s) and {len(signals)} resolved live signal(s) "
            "were identified within the 300 km operational screen."
        ),
        reasoning="This is a current operational state only; it does not establish long-horizon physical risk.",
        confidence="medium",
        confidence_reason="Current configured warning/event feeds are traceable but coverage varies by jurisdiction and source.",
        action_type="monitor",
        action="Continue operational monitoring and follow local authorities during any active emergency.",
        support=["official_warning_count_300km", "resolved_signal_count_300km", "strongest_live_signal_300km"],
    )

    seismic_signals = [
        s for s in signals
        if "earth" in str(s.get("category") or "").lower()
    ]
    if seismic_signals or quake_count:
        add(
            "seismic",
            "monitor",
            "Recent regional seismic activity is present in the operational/event evidence, but long-term site-specific ground-shaking hazard remains unresolved.",
            reasoning="Recent event occurrence is relevant context but cannot substitute for probabilistic seismic-hazard evidence.",
            confidence="medium",
            confidence_reason="Recent-event evidence is traceable; structural vulnerability and long-horizon hazard are not yet characterised.",
            action_type="verify",
            action="Add probabilistic seismic-hazard evidence and verify construction/design information before making structural or loss inferences.",
            specialist=True,
            support=["usgs_quake_count_30d_350km", "strongest_live_signal_300km"],
        )
    else:
        add(
            "seismic",
            "evidence_gap",
            "Long-term site-specific seismic hazard is not established by the current recent-event screen.",
            reasoning="No recent-event count can establish low long-term seismic hazard.",
            confidence="unresolved",
            confidence_reason="Probabilistic ground-shaking evidence has not yet been added to V2.",
            action_type="verify",
            action="Add probabilistic seismic-hazard evidence before classifying long-term seismic materiality.",
            specialist=True,
            support=["usgs_quake_count_30d_350km"],
        )

    # Current weather is deliberately insufficient for long-horizon materiality.
    add(
        "heat", "evidence_gap",
        "Long-horizon extreme-heat exposure has not yet been characterised.",
        sensitivity=(
            "high" if profile.get("cooling_dependency") in ("high", "critical")
            else "moderate" if profile.get("cooling_dependency") == "moderate"
            else "low" if profile.get("cooling_dependency") == "low"
            else "unresolved"
        ),
        reasoning="Current temperature and a seven-day forecast are operational weather context, not a historical/future heat-risk assessment.",
        confidence="unresolved",
        confidence_reason="Historical heat extremes and forward-looking climate projections are not yet in the evidence ledger.",
        action_type="verify",
        action="Add historical heat metrics and future scenario evidence before classifying heat materiality.",
        support=["current_temperature_c", "client_cooling_dependency"],
    )

    add(
        "flood", "evidence_gap",
        "Riverine and surface-water flood exposure have not yet been characterised to institutional screening standard.",
        sensitivity=(
            "high" if profile.get("basement_present") == "yes" or profile.get("critical_equipment_level") in ("basement", "ground_floor")
            else "unresolved"
        ),
        reasoning="Current precipitation and elevation do not establish flood depth, return period, drainage performance or finished-floor exposure.",
        confidence="unresolved",
        confidence_reason="A defensible flood-hazard layer has not yet been added.",
        action_type="verify",
        action="Add flood-hazard evidence and verify finished-floor/drainage characteristics.",
        specialist=False,
        support=["current_precipitation_mm", "elevation_m", "client_basement_present", "client_critical_equipment_level", "client_drainage_protection"],
    )

    add(
        "water_stress", "evidence_gap",
        "Baseline and future water-stress exposure have not yet been characterised.",
        sensitivity=(
            "high" if profile.get("water_dependency") in ("high", "critical")
            else "moderate" if profile.get("water_dependency") == "moderate"
            else "low" if profile.get("water_dependency") == "low"
            else "unresolved"
        ),
        reasoning="Facility water dependency can be recorded now, but regional baseline/future water-stress evidence is not yet present.",
        confidence="unresolved",
        confidence_reason="No V2 water-stress dataset has yet been added.",
        action_type="verify",
        action="Add baseline and future water-stress evidence before materiality classification.",
        support=["client_water_dependency"],
    )

    add(
        "wind", "evidence_gap",
        "Extreme-wind and severe-storm exposure have not yet been characterised.",
        reasoning="Current 10 m wind speed is near-term weather and does not establish design-level or climatological wind exposure.",
        confidence="unresolved",
        confidence_reason="Historical extreme-wind/cyclone evidence has not yet been added.",
        action_type="verify",
        action="Add appropriate historical severe-wind/cyclone evidence where geographically relevant.",
        support=["current_wind_kmh"],
    )

    add(
        "wildfire", "evidence_gap",
        "Wildfire relevance has not yet been characterised.",
        confidence="unresolved",
        confidence_reason="No wildfire exposure/history layer has yet been added.",
        action_type="verify",
        action="Screen wildfire exposure using appropriate fire-history and land-context evidence.",
    )

    add(
        "landslide", "evidence_gap",
        "Landslide and terrain susceptibility have not yet been characterised.",
        reasoning="Point elevation alone does not establish slope, terrain instability or rainfall-triggered landslide susceptibility.",
        confidence="unresolved",
        confidence_reason="Slope/terrain susceptibility evidence has not yet been added.",
        action_type="verify",
        action="Add DEM-derived slope and appropriate landslide susceptibility evidence.",
        support=["elevation_m"],
    )

    access_routes = str(profile.get("practical_access_routes") or "unknown").lower()
    if access_routes == "1":
        add(
            "operational_access", "monitor",
            "The client reports one practical road access route, creating a potential single-point access dependency.",
            sensitivity="high",
            reasoning="A single practical route can amplify flood, storm, landslide or local-disruption consequences even when the primary hazard remains separately assessed.",
            consequence="Potential interruption to staff access, deliveries, dispatch or emergency access if the sole practical route becomes unavailable.",
            confidence="medium",
            confidence_reason="The route count is client-declared and has not yet been independently network-verified.",
            action_type="verify",
            action="Verify the primary route, alternate emergency access and relevant route-level hazard exposure.",
            support=["client_practical_access_routes", "mapped_major_roads_5km_count"],
        )
    elif access_routes in ("2", "3_plus"):
        add(
            "operational_access", "monitor",
            "The client reports multiple practical access routes; route-level resilience remains to be verified.",
            sensitivity="moderate",
            confidence="medium",
            confidence_reason="Access redundancy is client-declared; route geometry and hazard exposure are not yet independently verified.",
            action_type="verify",
            action="Confirm route redundancy and whether the routes share common chokepoints or hazard exposure.",
            support=["client_practical_access_routes", "mapped_major_roads_5km_count"],
        )
    else:
        add(
            "operational_access", "evidence_gap",
            "Site access redundancy is unresolved.",
            confidence="unresolved",
            confidence_reason="The practical access-route count is unknown or absent and open-map results alone cannot establish operational access resilience.",
            action_type="verify",
            action="Confirm practical primary and alternate site access routes.",
            support=["mapped_major_roads_5km_count", "client_practical_access_routes"],
        )

    return findings
