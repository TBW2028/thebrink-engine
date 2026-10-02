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
    quake_sources = sorted({str(q.get("source") or "Unknown") for q in quakes})
    quake_source_name = " / ".join(quake_sources) if quake_sources else "USGS Earthquake Catalog"
    current = telemetry.get("weather_current") or {}
    strongest = telemetry.get("strongest_local_signal")
    historical_heat = telemetry.get("historical_heat") or {}
    historical_rainfall = telemetry.get("historical_rainfall") or {}
    river_flood = telemetry.get("river_flood") or {}
    water_risk = telemetry.get("water_risk") or {}
    terrain_ctx = telemetry.get("terrain") or {}
    cyclone_history = telemetry.get("cyclone_history") or {}
    fire_context = telemetry.get("fire_context") or {}

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
        "quake_count_24h_350km_m1", "Catalogued earthquakes M1.0+ within 350 km over the latest 24 hours",
        quake_source_name, "observed",
        value_numeric=telemetry.get("quake_count_24h_350km_m1") or 0, unit="count",
        source_dataset="NCS India operational feed and/or USGS FDSN Event Web Service", retrieved_at=retrieved,
        observation_start=None, observation_end=retrieved,
        spatial_resolution="350 km radial screen", temporal_resolution="24 hours",
        confidence="high",
        confidence_reason="Catalogued seismic events are source-traceable; NCS India is preferred for matching India-facing events and USGS provides global coverage.",
        limitations="Small-event occurrence is operational context and does not estimate long-term ground-shaking hazard, recurrence or building damage.",
        raw_evidence={"events": quakes[:100]},
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
        raw_evidence={"note": "30-day M2.5+ count retained as broader regional context; event detail table uses the latest 24-hour M1.0+ window."},
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
            "MET Norway Locationforecast 2.0", "modelled",
            value_numeric=value, unit=unit, source_dataset="MET Norway Locationforecast 2.0",
            retrieved_at=retrieved, temporal_resolution="current modelled condition",
            confidence="medium",
            confidence_reason="Established numerical weather source at the assessed coordinate, but not an on-site observation.",
            limitations="Current weather is operational context and must not be used alone as long-horizon physical-risk evidence.",
        ))

    if historical_heat.get("status") == "ok":
        heat_specs = [
            ("baseline_p95_tmax_c", "Historical baseline 95th percentile daily maximum temperature", historical_heat.get("p95_tmax_c"), "°C"),
            ("baseline_p99_tmax_c", "Historical baseline 99th percentile daily maximum temperature", historical_heat.get("p99_tmax_c"), "°C"),
            ("baseline_days_ge_35c", "Historical mean annual days with maximum temperature ≥35°C", historical_heat.get("mean_annual_days_ge_35c"), "days/year"),
            ("baseline_days_ge_40c", "Historical mean annual days with maximum temperature ≥40°C", historical_heat.get("mean_annual_days_ge_40c"), "days/year"),
            ("baseline_nights_ge_25c", "Historical mean annual nights with minimum temperature ≥25°C", historical_heat.get("mean_annual_nights_ge_25c"), "nights/year"),
            ("recent_days_ge_35c", "Recent mean annual days with maximum temperature ≥35°C", historical_heat.get("recent_mean_annual_days_ge_35c"), "days/year"),
        ]
        for code, name, value, unit in heat_specs:
            if value is None:
                continue
            items.append(_evidence(
                report_run_id, facility_id, "heat",
                code, name,
                "Copernicus Climate Change Service (C3S) — ERA5-Land", "modelled",
                value_numeric=value, unit=unit,
                source_dataset="ERA5-Land",
                source_reference="Copernicus Climate Data Store",
                source_version="ERA5-Land",
                retrieved_at=retrieved,
                observation_start="1991-01-01T00:00:00+00:00" if code.startswith("baseline_") else "2021-01-01T00:00:00+00:00",
                observation_end="2020-12-31T23:59:59+00:00" if code.startswith("baseline_") else "2025-12-31T23:59:59+00:00",
                spatial_resolution=historical_heat.get("spatial_resolution"),
                temporal_resolution="hourly reanalysis aggregated to daily extrema",
                confidence="medium",
                confidence_reason="Established ERA5-Land reanalysis provides a consistent historical gridded baseline; it is not an on-site observation.",
                limitations=historical_heat.get("limitations"),
            ))

        hottest = historical_heat.get("hottest_day") or {}
        if hottest.get("temperature_c") is not None:
            items.append(_evidence(
                report_run_id, facility_id, "heat",
                "historical_hottest_day", "Highest daily maximum temperature in retrieved historical series",
                "Copernicus Climate Change Service (C3S) — ERA5-Land", "modelled",
                value_numeric=hottest.get("temperature_c"), unit="°C",
                value_text=hottest.get("date"),
                source_dataset="ERA5-Land", retrieved_at=retrieved,
                spatial_resolution=historical_heat.get("spatial_resolution"),
                temporal_resolution="daily maximum",
                confidence="medium",
                confidence_reason="Reanalysis extreme at the model grid cell, not an on-site thermometer observation.",
                limitations=historical_heat.get("limitations"),
            ))
    else:
        items.append(_evidence(
            report_run_id, facility_id, "heat",
            "historical_heat_baseline_status", "Historical heat baseline availability",
            "Copernicus Climate Change Service (C3S) — ERA5-Land", "modelled",
            value_text=str(historical_heat.get("status") or "not_available"),
            retrieved_at=retrieved,
            confidence="unresolved",
            confidence_reason=historical_heat.get("reason") or "Historical heat baseline was not available.",
            limitations="Heat materiality must remain unresolved until historical heat evidence is available.",
        ))

    if historical_rainfall.get("status") == "ok":
        rainfall_specs = [
            ("baseline_mean_annual_precip_mm", "Historical mean annual precipitation", historical_rainfall.get("mean_annual_precip_mm"), "mm/year"),
            ("baseline_mean_annual_wet_days", "Historical mean annual wet days ≥1 mm", historical_rainfall.get("mean_annual_wet_days"), "days/year"),
            ("baseline_days_ge_20mm", "Historical mean annual days with precipitation ≥20 mm", historical_rainfall.get("mean_annual_days_ge_20mm"), "days/year"),
            ("baseline_days_ge_50mm", "Historical mean annual days with precipitation ≥50 mm", historical_rainfall.get("mean_annual_days_ge_50mm"), "days/year"),
            ("baseline_p95_wet_day_mm", "Historical 95th percentile wet-day precipitation", historical_rainfall.get("p95_wet_day_mm"), "mm/day"),
            ("baseline_rx1day_mm", "Historical mean annual maximum 1-day precipitation", historical_rainfall.get("mean_annual_rx1day_mm"), "mm/day"),
            ("baseline_rx5day_mm", "Historical mean annual maximum consecutive 5-day precipitation", historical_rainfall.get("mean_annual_rx5day_mm"), "mm/5 days"),
            ("recent_days_ge_20mm", "Recent mean annual days with precipitation ≥20 mm", historical_rainfall.get("recent_mean_annual_days_ge_20mm"), "days/year"),
            ("recent_rx1day_mm", "Recent mean annual maximum 1-day precipitation", historical_rainfall.get("recent_mean_annual_rx1day_mm"), "mm/day"),
        ]
        for code, name, value, unit in rainfall_specs:
            if value is None:
                continue
            items.append(_evidence(
                report_run_id, facility_id, "extreme_rainfall",
                code, name,
                "Copernicus Climate Change Service (C3S) — ERA5-Land precipitation", "modelled",
                value_numeric=value, unit=unit,
                source_dataset="ERA5-Land",
                source_reference="Copernicus Climate Data Store",
                source_version="ERA5-Land",
                retrieved_at=retrieved,
                observation_start="1991-01-01T00:00:00+00:00" if code.startswith("baseline_") else "2021-01-01T00:00:00+00:00",
                observation_end="2020-12-31T23:59:59+00:00" if code.startswith("baseline_") else "2025-12-31T23:59:59+00:00",
                spatial_resolution=historical_rainfall.get("spatial_resolution"),
                temporal_resolution="daily statistics derived from hourly reanalysis",
                confidence="medium",
                confidence_reason="ERA5-Land provides a consistent gridded historical rainfall baseline; it is not an on-site rain gauge or flood-depth model.",
                limitations=historical_rainfall.get("limitations"),
            ))

        wettest = historical_rainfall.get("wettest_day") or {}
        if wettest.get("precipitation_mm") is not None:
            items.append(_evidence(
                report_run_id, facility_id, "extreme_rainfall",
                "historical_wettest_day", "Highest daily precipitation in retrieved historical series",
                "Copernicus Climate Change Service (C3S) — ERA5-Land precipitation", "modelled",
                value_numeric=wettest.get("precipitation_mm"), unit="mm/day",
                value_text=wettest.get("date"),
                source_dataset="ERA5-Land", retrieved_at=retrieved,
                spatial_resolution=historical_rainfall.get("spatial_resolution"),
                temporal_resolution="daily total derived from hourly reanalysis",
                confidence="medium",
                confidence_reason="Grid-cell reanalysis extreme, not an on-site rain-gauge observation.",
                limitations=historical_rainfall.get("limitations"),
            ))
    else:
        items.append(_evidence(
            report_run_id, facility_id, "extreme_rainfall",
            "historical_rainfall_baseline_status", "Historical rainfall baseline availability",
            "Copernicus Climate Change Service (C3S) — ERA5-Land precipitation", "modelled",
            value_text=str(historical_rainfall.get("status") or "not_available"),
            retrieved_at=retrieved,
            confidence="unresolved",
            confidence_reason=historical_rainfall.get("reason") or "Historical rainfall baseline was not available.",
            limitations="Extreme-rainfall materiality remains unresolved until historical rainfall evidence is available.",
        ))

    if river_flood.get("status") == "ok":
        for rp in sorted(int(x) for x in river_flood.get("depths", {}).keys()):
            rec = river_flood["depths"].get(str(rp)) or {}
            point_depth = rec.get("point_depth_m")
            nearby_depth = rec.get("nearby_max_depth_m")
            if point_depth is not None:
                items.append(_evidence(
                    report_run_id, facility_id, "flood",
                    f"river_flood_rp{rp}_point_depth_m",
                    f"Modelled riverine flood depth at site grid cell — {rp}-year return period",
                    "JRC/CEMS GloFAS Global river flood hazard maps v2.1.2", "modelled",
                    value_numeric=point_depth, unit="m",
                    source_dataset="Global river flood hazard maps v2.1.2",
                    source_reference=rec.get("url"),
                    retrieved_at=retrieved,
                    spatial_resolution=river_flood.get("resolution"),
                    temporal_resolution=f"{rp}-year return-period hazard",
                    confidence="medium",
                    confidence_reason="Established global riverine flood-hazard model sampled at the assessed grid cell; not an official local flood map.",
                    limitations=river_flood.get("limitations"),
                    raw_evidence={"tile": river_flood.get("tile"), "return_period_years": rp},
                ))
            if nearby_depth is not None:
                items.append(_evidence(
                    report_run_id, facility_id, "flood",
                    f"river_flood_rp{rp}_nearby_max_depth_m",
                    f"Maximum modelled riverine flood depth in small site neighbourhood — {rp}-year return period",
                    "JRC/CEMS GloFAS Global river flood hazard maps v2.1.2", "modelled",
                    value_numeric=nearby_depth, unit="m",
                    source_dataset="Global river flood hazard maps v2.1.2",
                    source_reference=rec.get("url"),
                    retrieved_at=retrieved,
                    spatial_resolution=river_flood.get("resolution"),
                    temporal_resolution=f"{rp}-year return-period hazard",
                    confidence="low",
                    confidence_reason="Neighbourhood maximum is useful for screening nearby inundation but is not an on-site depth.",
                    limitations=river_flood.get("limitations"),
                    raw_evidence={"tile": river_flood.get("tile"), "return_period_years": rp},
                ))

        items.append(_evidence(
            report_run_id, facility_id, "flood",
            "river_flood_lowest_point_exposure_rp",
            "Lowest return period with modelled riverine inundation at site grid cell",
            "JRC/CEMS GloFAS Global river flood hazard maps v2.1.2", "modelled",
            value_numeric=river_flood.get("lowest_point_exposure_rp_years"),
            unit="years" if river_flood.get("lowest_point_exposure_rp_years") is not None else None,
            value_text="No ≥0.1 m point exposure in sampled return periods" if river_flood.get("lowest_point_exposure_rp_years") is None else None,
            source_dataset="Global river flood hazard maps v2.1.2",
            retrieved_at=retrieved,
            spatial_resolution=river_flood.get("resolution"),
            confidence="medium",
            confidence_reason="Derived directly from sampled return-period depth rasters.",
            limitations=river_flood.get("limitations"),
        ))
        items.append(_evidence(
            report_run_id, facility_id, "flood",
            "river_flood_lowest_nearby_exposure_rp",
            "Lowest return period with modelled riverine inundation in small site neighbourhood",
            "JRC/CEMS GloFAS Global river flood hazard maps v2.1.2", "modelled",
            value_numeric=river_flood.get("lowest_nearby_exposure_rp_years"),
            unit="years" if river_flood.get("lowest_nearby_exposure_rp_years") is not None else None,
            value_text="No ≥0.1 m nearby exposure in sampled return periods" if river_flood.get("lowest_nearby_exposure_rp_years") is None else None,
            source_dataset="Global river flood hazard maps v2.1.2",
            retrieved_at=retrieved,
            spatial_resolution=river_flood.get("resolution"),
            confidence="low",
            confidence_reason="Derived from a small neighbourhood screen, not from the exact site grid cell.",
            limitations=river_flood.get("limitations"),
        ))
        if river_flood.get("artifact_caution"):
            items.append(_evidence(
                report_run_id, facility_id, "flood",
                "river_flood_artifact_caution",
                "Riverine flood raster artefact caution",
                "JRC/CEMS GloFAS Global river flood hazard maps v2.1.2", "interpreted",
                value_text="Very high modelled depth detected; specialist/local verification required before reliance.",
                retrieved_at=retrieved,
                confidence="low",
                confidence_reason="Global flood-map documentation cautions that very high depths can reflect modelling artefacts.",
                limitations=river_flood.get("limitations"),
            ))
    else:
        items.append(_evidence(
            report_run_id, facility_id, "flood",
            "river_flood_map_status", "Mapped riverine flood exposure availability",
            "JRC/CEMS GloFAS Global river flood hazard maps v2.1.2", "modelled",
            value_text=str(river_flood.get("status") or "not_available"),
            retrieved_at=retrieved,
            confidence="unresolved",
            confidence_reason=river_flood.get("reason") or "Mapped riverine flood evidence was not available.",
            limitations="Flood materiality must remain unresolved without an appropriate mapped hazard layer.",
        ))

    if water_risk.get("status") == "ok":
        baseline = water_risk.get("baseline") or {}
        water_specs = [
            ("aqueduct_baseline_water_stress_raw", "Baseline water stress ratio / raw value", baseline.get("water_stress_raw"), None),
            ("aqueduct_baseline_water_stress_score", "Baseline water stress score", baseline.get("water_stress_score"), None),
            ("aqueduct_baseline_water_depletion_raw", "Baseline water depletion raw value", baseline.get("water_depletion_raw"), None),
            ("aqueduct_baseline_water_depletion_score", "Baseline water depletion score", baseline.get("water_depletion_score"), None),
            ("aqueduct_baseline_interannual_variability_raw", "Baseline interannual variability raw value", baseline.get("interannual_variability_raw"), None),
            ("aqueduct_baseline_seasonal_variability_raw", "Baseline seasonal variability raw value", baseline.get("seasonal_variability_raw"), None),
            ("aqueduct_baseline_drought_risk_raw", "Baseline drought risk raw value", baseline.get("drought_risk_raw"), None),
            ("aqueduct_baseline_drought_risk_score", "Baseline drought risk score", baseline.get("drought_risk_score"), None),
        ]
        for code, name, value, unit in water_specs:
            if value is None:
                continue
            items.append(_evidence(
                report_run_id, facility_id, "water_stress",
                code, name,
                "World Resources Institute — Aqueduct 4.0", "modelled",
                value_numeric=value if isinstance(value, (int, float)) else None,
                value_text=None if isinstance(value, (int, float)) else str(value),
                unit=unit,
                source_dataset="Aqueduct 4.0 Current and Future Global Maps Data",
                source_reference="https://www.wri.org/research/aqueduct-40-updated-decision-relevant-global-water-risk-indicators",
                retrieved_at=retrieved,
                spatial_resolution="Hydrological basin / Aqueduct spatial unit",
                confidence="medium",
                confidence_reason="Established global water-risk screening dataset; basin-level and not a local utility or hydrogeological study.",
                limitations=water_risk.get("limitations"),
            ))

        for code, name, value in [
            ("aqueduct_baseline_water_stress_label", "Baseline water stress category", baseline.get("water_stress_label")),
            ("aqueduct_baseline_water_depletion_label", "Baseline water depletion category", baseline.get("water_depletion_label")),
            ("aqueduct_baseline_interannual_variability_label", "Baseline interannual variability category", baseline.get("interannual_variability_label")),
            ("aqueduct_baseline_seasonal_variability_label", "Baseline seasonal variability category", baseline.get("seasonal_variability_label")),
            ("aqueduct_baseline_drought_risk_label", "Baseline drought risk category", baseline.get("drought_risk_label")),
        ]:
            if value is None:
                continue
            items.append(_evidence(
                report_run_id, facility_id, "water_stress",
                code, name,
                "World Resources Institute — Aqueduct 4.0", "modelled",
                value_text=str(value),
                source_dataset="Aqueduct 4.0 Current and Future Global Maps Data",
                source_reference="https://www.wri.org/research/aqueduct-40-updated-decision-relevant-global-water-risk-indicators",
                retrieved_at=retrieved,
                confidence="medium",
                confidence_reason="Aqueduct category assigned at basin level.",
                limitations=water_risk.get("limitations"),
            ))

        future = water_risk.get("future_water_stress") or {}
        for scenario, years in future.items():
            scenario_label = (water_risk.get("future_scenarios") or {}).get(scenario)
            for year, rec in (years or {}).items():
                if not isinstance(rec, dict):
                    continue
                items.append(_evidence(
                    report_run_id, facility_id, "water_stress",
                    f"aqueduct_future_water_stress_{scenario}_{year}",
                    f"Projected water stress — {scenario.replace('_',' ')} — {year}",
                    "World Resources Institute — Aqueduct 4.0", "modelled",
                    value_numeric=rec.get("score") if isinstance(rec.get("score"), (int, float)) else None,
                    value_text=str(rec.get("label") or rec.get("category") or "") or None,
                    source_dataset="Aqueduct 4.0 Future Global Maps Data",
                    source_reference="https://www.wri.org/research/aqueduct-40-updated-decision-relevant-global-water-risk-indicators",
                    retrieved_at=retrieved,
                    scenario=scenario_label or scenario,
                    time_horizon=str(year),
                    spatial_resolution="Hydrological basin / Aqueduct spatial unit",
                    confidence="medium",
                    confidence_reason="CMIP6-based Aqueduct future water-stress projection intended for global screening and prioritization.",
                    limitations=water_risk.get("limitations"),
                    raw_evidence={"raw": rec.get("raw"), "score": rec.get("score"), "label": rec.get("label"), "category": rec.get("category")},
                ))
    else:
        items.append(_evidence(
            report_run_id, facility_id, "water_stress",
            "aqueduct_water_risk_status", "Aqueduct 4.0 water-risk evidence availability",
            "World Resources Institute — Aqueduct 4.0", "modelled",
            value_text=str(water_risk.get("status") or "not_available"),
            retrieved_at=retrieved,
            confidence="unresolved",
            confidence_reason=water_risk.get("reason") or "Aqueduct 4.0 evidence was not available.",
            limitations="Water-stress materiality must remain unresolved until basin-level evidence is available.",
        ))

    if terrain_ctx.get("status") == "ok":
        t250 = terrain_ctx.get("metrics_250m") or {}
        t1k = terrain_ctx.get("metrics_1km") or {}
        terrain_specs = [
            ("terrain_point_elevation_m", "Copernicus DEM point elevation", terrain_ctx.get("point_elevation_m"), "m"),
            ("terrain_slope_mean_250m_deg", "Mean derived slope in ~250 m neighbourhood", t250.get("slope_mean_deg"), "degrees"),
            ("terrain_slope_p95_250m_deg", "95th percentile derived slope in ~250 m neighbourhood", t250.get("slope_p95_deg"), "degrees"),
            ("terrain_relief_250m_m", "Local elevation relief in ~250 m neighbourhood", t250.get("relief_m"), "m"),
            ("terrain_slope_mean_1km_deg", "Mean derived slope in ~1 km neighbourhood", t1k.get("slope_mean_deg"), "degrees"),
            ("terrain_slope_p95_1km_deg", "95th percentile derived slope in ~1 km neighbourhood", t1k.get("slope_p95_deg"), "degrees"),
            ("terrain_slope_max_1km_deg", "Maximum derived slope in ~1 km neighbourhood", t1k.get("slope_max_deg"), "degrees"),
            ("terrain_relief_1km_m", "Local elevation relief in ~1 km neighbourhood", t1k.get("relief_m"), "m"),
            ("terrain_elevation_std_1km_m", "Elevation standard deviation in ~1 km neighbourhood", t1k.get("elevation_std_m"), "m"),
        ]
        for code, name, value, unit in terrain_specs:
            if value is None:
                continue
            items.append(_evidence(
                report_run_id, facility_id, "terrain",
                code, name,
                "Copernicus DEM GLO-30 Public", "modelled",
                value_numeric=value, unit=unit,
                source_dataset="Copernicus DEM GLO-30 Public 2021",
                source_reference=terrain_ctx.get("url"),
                source_version=terrain_ctx.get("release"),
                retrieved_at=retrieved,
                spatial_resolution=terrain_ctx.get("resolution"),
                confidence="medium",
                confidence_reason="Derived from the public Copernicus ~30 m digital surface model at and around the assessed coordinate.",
                limitations=terrain_ctx.get("limitations"),
                raw_evidence={"tile": terrain_ctx.get("tile")},
            ))
    else:
        items.append(_evidence(
            report_run_id, facility_id, "terrain",
            "terrain_dem_status", "Copernicus DEM terrain evidence availability",
            "Copernicus DEM GLO-30 Public", "modelled",
            value_text=str(terrain_ctx.get("status") or "not_available"),
            retrieved_at=retrieved,
            confidence="unresolved",
            confidence_reason=terrain_ctx.get("reason") or "Terrain evidence was not available.",
            limitations="Terrain and landslide-related screening remain unresolved without a suitable terrain model.",
        ))

    if cyclone_history.get("status") == "ok":
        for radius in (100, 250, 500):
            items.append(_evidence(
                report_run_id, facility_id, "tropical_cyclone",
                f"ibtracs_storm_count_{radius}km_since1980",
                f"Unique tropical cyclones with IBTrACS track points within {radius} km since 1980",
                "NOAA NCEI — IBTrACS v04r01", "observed",
                value_numeric=cyclone_history.get(f"storm_count_within_{radius}km") or 0,
                unit="storms",
                source_dataset="IBTrACS v04r01 since1980",
                source_reference="https://www.ncei.noaa.gov/products/international-best-track-archive",
                retrieved_at=retrieved,
                observation_start="1980-01-01T00:00:00+00:00",
                observation_end=retrieved,
                spatial_resolution=f"{radius} km radial track-proximity screen",
                confidence="high",
                confidence_reason="NOAA IBTrACS is the global consolidated historical tropical-cyclone best-track archive; the count is deterministic for the defined radius and period.",
                limitations=cyclone_history.get("limitations"),
            ))

        nearest = cyclone_history.get("nearest_storm") or {}
        if nearest.get("nearest_distance_km") is not None:
            items.append(_evidence(
                report_run_id, facility_id, "tropical_cyclone",
                "ibtracs_nearest_track_distance_km",
                "Nearest historical tropical-cyclone track distance since 1980",
                "NOAA NCEI — IBTrACS v04r01", "observed",
                value_numeric=nearest.get("nearest_distance_km"), unit="km",
                value_text=f"{nearest.get('name') or 'Unnamed cyclone'} · {nearest.get('season') or 'year unavailable'}",
                source_dataset="IBTrACS v04r01 since1980",
                source_reference="https://www.ncei.noaa.gov/products/international-best-track-archive",
                retrieved_at=retrieved,
                confidence="high",
                confidence_reason="Great-circle distance derived from NOAA IBTrACS best-track coordinates.",
                limitations=cyclone_history.get("limitations"),
                raw_evidence=nearest,
            ))

        peak_wind = cyclone_history.get("max_reported_wmo_wind_within_250km_kt")
        if peak_wind is not None:
            items.append(_evidence(
                report_run_id, facility_id, "tropical_cyclone",
                "ibtracs_max_wmo_wind_nearby_250km_kt",
                "Highest reported WMO storm intensity at an IBTrACS track point within 250 km",
                "NOAA NCEI — IBTrACS v04r01", "observed",
                value_numeric=peak_wind, unit="knots",
                source_dataset="IBTrACS v04r01 since1980",
                source_reference="https://www.ncei.noaa.gov/products/international-best-track-archive",
                retrieved_at=retrieved,
                confidence="medium",
                confidence_reason="Historical WMO-reported storm intensity is traceable, but agency wind-averaging periods differ and IBTrACS does not normalize them.",
                limitations=cyclone_history.get("limitations"),
                raw_evidence=cyclone_history.get("max_reported_wmo_wind_storm") or {},
            ))
    else:
        items.append(_evidence(
            report_run_id, facility_id, "tropical_cyclone",
            "ibtracs_cyclone_history_status",
            "Historical tropical-cyclone evidence availability",
            "NOAA NCEI — IBTrACS v04r01", "observed",
            value_text=str(cyclone_history.get("status") or "not_available"),
            retrieved_at=retrieved,
            confidence="unresolved",
            confidence_reason=cyclone_history.get("reason") or "IBTrACS historical cyclone evidence was not available.",
            limitations="Cyclone-history materiality must remain unresolved without a suitable best-track dataset.",
        ))

    if fire_context.get("status") == "ok":
        for radius in (5, 10, 25, 50, 100):
            items.append(_evidence(
                report_run_id, facility_id, "wildfire_operational",
                f"firms_thermal_anomaly_count_{radius}km_5d",
                f"NASA FIRMS thermal-anomaly detections within {radius} km over the latest 5-day operational window",
                "NASA LANCE FIRMS — VIIRS NOAA-20/21 NRT", "observed",
                value_numeric=fire_context.get(f"detection_count_within_{radius}km") or 0,
                unit="detections",
                source_dataset="FIRMS VIIRS NOAA-20/21 NRT",
                source_reference=fire_context.get("source_reference"),
                retrieved_at=retrieved,
                temporal_resolution="latest 5-day FIRMS NRT window",
                spatial_resolution=f"{radius} km radial proximity screen",
                confidence="medium",
                confidence_reason="Satellite thermal-anomaly detections are directly retrieved from NASA FIRMS, but they are not verified wildfire perimeters.",
                limitations=fire_context.get("limitations"),
            ))

        nearest = fire_context.get("nearest_detection") or {}
        if nearest.get("distance_km") is not None:
            items.append(_evidence(
                report_run_id, facility_id, "wildfire_operational",
                "firms_nearest_thermal_anomaly_km",
                "Nearest NASA FIRMS thermal-anomaly detection in latest 5-day window",
                "NASA LANCE FIRMS — VIIRS NOAA-20/21 NRT", "observed",
                value_numeric=nearest.get("distance_km"), unit="km",
                value_text=nearest.get("acq_date"),
                source_dataset="FIRMS VIIRS NOAA-20/21 NRT",
                source_reference=fire_context.get("source_reference"),
                retrieved_at=retrieved,
                confidence="medium",
                confidence_reason="Great-circle proximity is derived from satellite-detection coordinates.",
                limitations=fire_context.get("limitations"),
                raw_evidence=nearest,
            ))

        peak = fire_context.get("peak_frp_detection") or {}
        if peak.get("frp_mw") is not None:
            items.append(_evidence(
                report_run_id, facility_id, "wildfire_operational",
                "firms_peak_frp_mw_5d",
                "Highest Fire Radiative Power among nearby FIRMS detections in latest 5-day window",
                "NASA LANCE FIRMS — VIIRS NOAA-20/21 NRT", "observed",
                value_numeric=peak.get("frp_mw"), unit="MW",
                value_text=peak.get("acq_date"),
                source_dataset="FIRMS VIIRS NOAA-20/21 NRT",
                source_reference=fire_context.get("source_reference"),
                retrieved_at=retrieved,
                confidence="medium",
                confidence_reason="FRP is a satellite-derived characteristic of the detected thermal anomaly; it is not a structure-level exposure metric.",
                limitations=fire_context.get("limitations"),
                raw_evidence=peak,
            ))
    else:
        items.append(_evidence(
            report_run_id, facility_id, "wildfire_operational",
            "firms_operational_status",
            "NASA FIRMS operational thermal-anomaly evidence availability",
            "NASA LANCE FIRMS — VIIRS NOAA-20/21 NRT", "observed",
            value_text=str(fire_context.get("status") or "not_available"),
            retrieved_at=retrieved,
            confidence="unresolved",
            confidence_reason=fire_context.get("reason") or "NASA FIRMS evidence was not available.",
            limitations="Current thermal-anomaly context cannot be assessed without FIRMS access.",
        ))

    if telemetry.get("elevation_m") is not None:
        items.append(_evidence(
            report_run_id, facility_id, "terrain",
            "elevation_m", "Modelled/mapped elevation at assessed coordinate",
            "MET Norway Locationforecast 2.0", "modelled",
            value_numeric=telemetry.get("elevation_m"), unit="m",
            source_dataset="MET Norway Locationforecast 2.0", retrieved_at=retrieved,
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
    quake_count_24h = telemetry.get("quake_count_24h_350km_m1") or 0
    quake_count_30d = telemetry.get("quake_count_30d_350km") or 0
    quake_count = quake_count_24h or quake_count_30d
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
            support=["quake_count_24h_350km_m1", "usgs_quake_count_30d_350km", "strongest_live_signal_300km"],
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
            support=["quake_count_24h_350km_m1", "usgs_quake_count_30d_350km"],
        )

    heat_ctx = telemetry.get("historical_heat") or {}
    heat_sensitivity = (
        "high" if profile.get("cooling_dependency") in ("high", "critical")
        else "moderate" if profile.get("cooling_dependency") == "moderate"
        else "low" if profile.get("cooling_dependency") == "low"
        else "unresolved"
    )
    if heat_ctx.get("status") == "ok":
        days35 = heat_ctx.get("mean_annual_days_ge_35c")
        days40 = heat_ctx.get("mean_annual_days_ge_40c")
        p99 = heat_ctx.get("p99_tmax_c")
        heat_metric_codes = [
            "baseline_p95_tmax_c", "baseline_p99_tmax_c",
            "baseline_days_ge_35c", "baseline_days_ge_40c",
            "baseline_nights_ge_25c", "recent_days_ge_35c",
            "client_cooling_dependency"
        ]

        if heat_sensitivity == "high" and ((days35 or 0) >= 30 or (days40 or 0) >= 5):
            add(
                "heat", "material",
                "Historical reanalysis indicates recurrent high-temperature exposure and the facility reports high or critical cooling dependence.",
                sensitivity=heat_sensitivity,
                reasoning="Historical heat exposure combines with a client-declared critical operational dependency, creating a plausible heat-to-operation pathway.",
                consequence="Elevated cooling demand, HVAC/power stress or loss of temperature-controlled operations may become operationally relevant during extreme heat.",
                confidence="medium",
                confidence_reason="Historical ERA5-Land evidence is established at grid scale, while facility sensitivity is client-declared and not independently verified.",
                action_type="verify",
                action="Verify cooling redundancy, backup power capability and critical temperature tolerances; add forward-looking climate projections before long-horizon reliance.",
                support=heat_metric_codes,
            )
        elif (days35 or 0) >= 15 or (days40 or 0) >= 1 or (p99 or 0) >= 38:
            add(
                "heat", "monitor",
                "Historical reanalysis indicates meaningful extreme-heat exposure at the assessed grid cell.",
                sensitivity=heat_sensitivity,
                reasoning="The historical baseline supports heat relevance, but full future materiality requires facility vulnerability and forward-looking scenario evidence.",
                confidence="medium",
                confidence_reason="ERA5-Land provides a consistent historical reanalysis baseline but does not resolve building-scale microclimate.",
                action_type="verify",
                action="Verify facility heat sensitivity and add 2030/2050 climate projections before determining long-horizon heat materiality.",
                support=heat_metric_codes,
            )
        else:
            add(
                "heat", "monitor",
                "Historical heat has been characterised at screening level; forward-looking heat change remains unresolved.",
                sensitivity=heat_sensitivity,
                reasoning="A historical baseline is now available, but V2 does not yet include future climate scenario evidence.",
                confidence="medium",
                confidence_reason="Historical reanalysis is available; future trend and building-scale exposure remain outside this finding.",
                action_type="monitor",
                action="Retain the historical baseline and add forward-looking climate projections for 2030/2050 assessment.",
                support=heat_metric_codes,
            )
    else:
        add(
            "heat", "evidence_gap",
            "Long-horizon extreme-heat exposure has not yet been characterised.",
            sensitivity=heat_sensitivity,
            reasoning="Current temperature and a seven-day forecast are operational weather context, not a historical/future heat-risk assessment.",
            confidence="unresolved",
            confidence_reason=heat_ctx.get("reason") or "Historical heat evidence is unavailable.",
            action_type="verify",
            action="Resolve the ERA5-Land historical heat baseline and add forward-looking climate scenario evidence before classifying heat materiality.",
            support=["historical_heat_baseline_status", "current_temperature_c", "client_cooling_dependency"],
        )

    rainfall_ctx = telemetry.get("historical_rainfall") or {}
    flood_sensitivity = (
        "high" if profile.get("basement_present") == "yes"
        or profile.get("critical_equipment_level") in ("basement", "ground_floor")
        else "unresolved"
    )

    if rainfall_ctx.get("status") == "ok":
        rx1 = rainfall_ctx.get("mean_annual_rx1day_mm")
        days50 = rainfall_ctx.get("mean_annual_days_ge_50mm")
        p95wet = rainfall_ctx.get("p95_wet_day_mm")
        rainfall_support = [
            "baseline_mean_annual_precip_mm",
            "baseline_mean_annual_wet_days",
            "baseline_days_ge_20mm",
            "baseline_days_ge_50mm",
            "baseline_p95_wet_day_mm",
            "baseline_rx1day_mm",
            "baseline_rx5day_mm",
            "recent_days_ge_20mm",
            "recent_rx1day_mm",
            "client_drainage_protection",
            "client_basement_present",
            "client_critical_equipment_level",
        ]
        if flood_sensitivity == "high" and ((rx1 or 0) >= 75 or (days50 or 0) >= 1):
            add(
                "extreme_rainfall", "material",
                "Historical reanalysis indicates substantial heavy-rainfall exposure and the facility has client-declared ground/below-ground sensitivity.",
                sensitivity=flood_sensitivity,
                reasoning="Historical rainfall intensity combines with facility vulnerability, creating a plausible rainfall-to-ingress or drainage-stress pathway.",
                consequence="Heavy rainfall may contribute to drainage overload, water ingress, access disruption or ground-level equipment exposure; mapped flood depth is not yet established.",
                confidence="medium",
                confidence_reason="ERA5-Land provides established historical rainfall evidence, while facility sensitivity is client-declared and parcel-scale drainage is unresolved.",
                action_type="verify",
                action="Verify site drainage, finished-floor elevation and water-entry pathways; add mapped riverine/pluvial flood evidence before treating this as a flood-depth conclusion.",
                support=rainfall_support,
            )
        elif (rx1 or 0) >= 50 or (days50 or 0) >= 0.25 or (p95wet or 0) >= 20:
            add(
                "extreme_rainfall", "monitor",
                "Historical reanalysis indicates meaningful heavy-rainfall exposure at the assessed grid cell.",
                sensitivity=flood_sensitivity,
                reasoning="The historical baseline supports rainfall relevance, but parcel-scale drainage and mapped inundation remain unresolved.",
                confidence="medium",
                confidence_reason="ERA5-Land supports historical rainfall screening but does not resolve local drainage or flood depth.",
                action_type="verify",
                action="Verify site drainage and add mapped riverine/pluvial flood evidence before classifying flood materiality.",
                support=rainfall_support,
            )
        else:
            add(
                "extreme_rainfall", "monitor",
                "Historical rainfall has been characterised at screening level; site-specific inundation susceptibility remains unresolved.",
                sensitivity=flood_sensitivity,
                reasoning="A historical rainfall baseline is available, but flood depth and drainage capacity require separate evidence.",
                confidence="medium",
                confidence_reason="Historical reanalysis is available; parcel-scale hydrology is outside this finding.",
                action_type="monitor",
                action="Retain the historical rainfall baseline and add site-appropriate flood-hazard evidence.",
                support=rainfall_support,
            )
    else:
        add(
            "extreme_rainfall", "evidence_gap",
            "Historical extreme-rainfall exposure has not yet been characterised.",
            sensitivity=flood_sensitivity,
            reasoning="Current precipitation and short-range forecasts do not establish historical heavy-rainfall frequency or intensity.",
            confidence="unresolved",
            confidence_reason=rainfall_ctx.get("reason") or "Historical rainfall evidence is unavailable.",
            action_type="verify",
            action="Resolve the ERA5-Land historical rainfall baseline before classifying rainfall materiality.",
            support=["historical_rainfall_baseline_status", "current_precipitation_mm"],
        )

    flood_ctx = telemetry.get("river_flood") or {}
    if flood_ctx.get("status") == "ok":
        point_rp = flood_ctx.get("lowest_point_exposure_rp_years")
        nearby_rp = flood_ctx.get("lowest_nearby_exposure_rp_years")
        point100 = ((flood_ctx.get("depths") or {}).get("100") or {}).get("point_depth_m")
        support = [
            "river_flood_lowest_point_exposure_rp",
            "river_flood_lowest_nearby_exposure_rp",
            "river_flood_rp100_point_depth_m",
            "river_flood_rp100_nearby_max_depth_m",
            "baseline_rx1day_mm",
            "baseline_rx5day_mm",
            "client_basement_present",
            "client_critical_equipment_level",
            "client_drainage_protection",
        ]

        if point_rp is not None and point_rp <= 100 and flood_sensitivity == "high":
            add(
                "flood", "material",
                "Global riverine flood mapping indicates modelled inundation at the assessed site grid cell within the sampled 100-year return-period range, and the facility has client-declared ground/below-ground sensitivity.",
                sensitivity=flood_sensitivity,
                reasoning="Mapped riverine inundation and facility vulnerability combine into a plausible site-impact pathway.",
                consequence="Potential water ingress, ground-level equipment exposure, access disruption or operational interruption requires site verification.",
                confidence="medium",
                confidence_reason="The hazard layer is an established global screening model at approximately 90 m; building-level elevation, local drainage and official/local flood mapping remain unresolved.",
                action_type="specialist_assessment",
                action="Verify against authoritative/local flood mapping and obtain site-specific finished-floor/drainage assessment before underwriting or engineering reliance.",
                specialist=True,
                support=support,
            )
        elif point_rp is not None:
            add(
                "flood", "monitor",
                "Global riverine flood mapping indicates modelled inundation at the assessed site grid cell for at least one sampled return period.",
                sensitivity=flood_sensitivity,
                reasoning="Mapped riverine exposure is present, but facility vulnerability and local flood behaviour require further verification.",
                confidence="medium",
                confidence_reason="Global 90 m screening evidence supports riverine exposure; parcel/building conditions remain unresolved.",
                action_type="verify",
                action="Verify local flood mapping, finished-floor elevation, drainage and critical-equipment exposure.",
                support=support,
            )
        elif nearby_rp is not None:
            add(
                "flood", "monitor",
                "No ≥0.1 m riverine inundation was resolved at the exact site grid cell in the sampled return periods, but modelled inundation occurs within the small surrounding neighbourhood.",
                sensitivity=flood_sensitivity,
                reasoning="Nearby modelled inundation may be operationally relevant to access or adjacent drainage, but it is not an on-site flood-depth conclusion.",
                confidence="low",
                confidence_reason="Neighbourhood screening indicates nearby exposure while exact-site exposure is not resolved.",
                action_type="verify",
                action="Check authoritative/local flood mapping and route/drainage conditions before treating nearby modelled inundation as site exposure.",
                support=support,
            )
        else:
            add(
                "flood", "monitor",
                "No ≥0.1 m riverine inundation was resolved at the assessed grid cell or small neighbourhood across the sampled JRC/CEMS return-period layers.",
                sensitivity=flood_sensitivity,
                reasoning="The global riverine flood screen does not show mapped exposure in the sampled layers, but pluvial flooding, drainage failure and smaller/local watercourses remain outside this result.",
                confidence="medium",
                confidence_reason="The global riverine screen is available and traceable, but its approximately 90 m resolution and model scope do not justify a Low Relevance conclusion for all flood mechanisms.",
                action_type="verify",
                action="Retain riverine monitoring and separately assess pluvial drainage flooding where relevant to the site.",
                support=support,
            )
    else:
        add(
            "flood", "evidence_gap",
            "Mapped riverine flood depth and return-period exposure remain unresolved.",
            sensitivity=flood_sensitivity,
            reasoning="Historical rainfall intensity does not establish riverine or pluvial flood depth, drainage performance or finished-floor exposure.",
            confidence="unresolved",
            confidence_reason=flood_ctx.get("reason") or "The mapped riverine flood layer was unavailable.",
            action_type="verify",
            action="Resolve mapped riverine flood evidence and separately assess pluvial/drainage exposure.",
            specialist=False,
            support=["river_flood_map_status", "baseline_rx1day_mm", "baseline_rx5day_mm", "elevation_m", "client_basement_present", "client_critical_equipment_level", "client_drainage_protection"],
        )

    water_ctx = telemetry.get("water_risk") or {}
    water_sensitivity = (
        "high" if profile.get("water_dependency") in ("high", "critical")
        else "moderate" if profile.get("water_dependency") == "moderate"
        else "low" if profile.get("water_dependency") == "low"
        else "unresolved"
    )

    if water_ctx.get("status") == "ok":
        baseline = water_ctx.get("baseline") or {}
        label = str(baseline.get("water_stress_label") or "").lower()
        drought_label = str(baseline.get("drought_risk_label") or "").lower()
        future = water_ctx.get("future_water_stress") or {}

        future_labels = []
        for scenario in future.values():
            for rec in (scenario or {}).values():
                if isinstance(rec, dict) and rec.get("label"):
                    future_labels.append(str(rec.get("label")).lower())

        high_baseline = any(term in label for term in ("extremely high", "high"))
        medium_baseline = "medium" in label
        high_drought = any(term in drought_label for term in ("extremely high", "high"))
        future_high = any(any(term in x for term in ("extremely high", "high")) for x in future_labels)

        support = [
            "aqueduct_baseline_water_stress_raw",
            "aqueduct_baseline_water_stress_score",
            "aqueduct_baseline_water_stress_label",
            "aqueduct_baseline_water_depletion_label",
            "aqueduct_baseline_drought_risk_label",
            "aqueduct_future_water_stress_business_as_usual_2030",
            "aqueduct_future_water_stress_business_as_usual_2050",
            "aqueduct_future_water_stress_pessimistic_2050",
            "client_water_dependency",
        ]

        if water_sensitivity == "high" and (high_baseline or high_drought or future_high):
            add(
                "water_stress", "material",
                "Aqueduct 4.0 indicates elevated basin-level water stress and/or drought exposure, and the facility reports high or critical dependence on uninterrupted water supply.",
                sensitivity=water_sensitivity,
                reasoning="Basin-level water scarcity evidence combines with client-declared operational dependency, creating a plausible supply-disruption pathway.",
                consequence="Operational continuity may be sensitive to restrictions, reduced availability, supply interruptions or competition for water during stressed periods.",
                confidence="medium",
                confidence_reason="Aqueduct is a well-established global screening dataset, while actual utility reliability, local groundwater conditions and facility dependency are not independently verified.",
                action_type="verify",
                action="Verify local utility/source reliability, storage, alternate supply, historical restrictions and business-continuity arrangements for water interruption.",
                support=support,
            )
        elif high_baseline or medium_baseline or high_drought or future_high:
            add(
                "water_stress", "monitor",
                "Aqueduct 4.0 indicates relevant basin-level water-stress or drought conditions for the assessed location.",
                sensitivity=water_sensitivity,
                reasoning="Regional water-risk evidence is relevant, but facility consequence depends on actual source, storage, dependency and local supply management.",
                confidence="medium",
                confidence_reason="Aqueduct supports basin-level prioritization, not site-specific utility reliability or hydrogeological conclusions.",
                action_type="verify",
                action="Confirm actual water source, reliability, storage and contingency arrangements; retain future water-stress monitoring.",
                support=support,
            )
        else:
            add(
                "water_stress", "monitor",
                "Aqueduct 4.0 water-risk indicators are available and do not currently indicate a strong basin-level stress signal under the captured baseline evidence.",
                sensitivity=water_sensitivity,
                reasoning="Global screening evidence is available, but local utility reliability and facility-specific supply resilience remain outside the dataset.",
                confidence="medium",
                confidence_reason="Aqueduct provides consistent basin-level screening but cannot establish local supply sufficiency.",
                action_type="monitor",
                action="Retain periodic water-risk reassessment and verify local supply resilience if water is operationally important.",
                support=support,
            )
    else:
        add(
            "water_stress", "evidence_gap",
            "Baseline and future water-stress exposure have not yet been characterised.",
            sensitivity=water_sensitivity,
            reasoning="Facility water dependency can be recorded now, but basin-level water-risk evidence is unavailable.",
            confidence="unresolved",
            confidence_reason=water_ctx.get("reason") or "Aqueduct 4.0 evidence is unavailable.",
            action_type="verify",
            action="Resolve Aqueduct 4.0 water-risk evidence before materiality classification.",
            support=["aqueduct_water_risk_status", "client_water_dependency"],
        )

    cyclone_ctx = telemetry.get("cyclone_history") or {}
    if cyclone_ctx.get("status") == "ok":
        count100 = cyclone_ctx.get("storm_count_within_100km") or 0
        count250 = cyclone_ctx.get("storm_count_within_250km") or 0
        count500 = cyclone_ctx.get("storm_count_within_500km") or 0
        nearest = cyclone_ctx.get("nearest_storm") or {}
        support = [
            "ibtracs_storm_count_100km_since1980",
            "ibtracs_storm_count_250km_since1980",
            "ibtracs_storm_count_500km_since1980",
            "ibtracs_nearest_track_distance_km",
            "ibtracs_max_wmo_wind_nearby_250km_kt",
            "client_construction_type",
            "client_backup_power",
        ]

        if count100 > 0:
            add(
                "tropical_cyclone", "monitor",
                f"NOAA IBTrACS records {count100} tropical cyclone(s) with best-track points within 100 km of the assessed location since 1980.",
                sensitivity="unresolved",
                reasoning="Close historical cyclone tracks establish tropical-cyclone relevance, but historical proximity does not provide facility wind load, damage probability or future occurrence probability.",
                consequence="Cyclone-related wind, rainfall, utility interruption and access disruption may be relevant depending on building design and operational resilience.",
                confidence="high",
                confidence_reason="Historical track proximity is derived directly from NOAA IBTrACS; facility vulnerability and design-wind adequacy remain unresolved.",
                action_type="verify",
                action="Verify applicable structural/design wind standard, roof/cladding condition, backup power and critical outdoor equipment; retain official cyclone-warning monitoring.",
                support=support,
            )
        elif count250 > 0:
            add(
                "tropical_cyclone", "monitor",
                f"NOAA IBTrACS records {count250} tropical cyclone(s) with best-track points within 250 km of the assessed location since 1980.",
                sensitivity="unresolved",
                reasoning="Regional historical tropical-cyclone exposure is present, although site wind intensity and structural vulnerability are not established by track proximity.",
                confidence="medium",
                confidence_reason="IBTrACS supports historical proximity screening; translating storm-track intensity to site-specific wind requires dedicated wind-hazard modelling.",
                action_type="monitor",
                action="Retain tropical-cyclone monitoring and verify design-wind/roof vulnerability where operationally material.",
                support=support,
            )
        elif count500 > 0:
            add(
                "tropical_cyclone", "monitor",
                "Historical tropical-cyclone tracks occur within the wider 500 km regional screen, but no track was resolved within 250 km in the modern IBTrACS record.",
                sensitivity="unresolved",
                reasoning="The wider regional record establishes basin-level cyclone context without demonstrating close historical site exposure.",
                confidence="medium",
                confidence_reason="Historical best-track evidence is strong for the defined screen; site-level wind exposure remains unresolved.",
                action_type="monitor",
                action="Maintain official cyclone-warning monitoring; add site-specific wind-hazard evidence if the asset or decision requires it.",
                support=support,
            )
        else:
            add(
                "tropical_cyclone", "low_relevance",
                "No NOAA IBTrACS tropical-cyclone best-track point was resolved within 500 km of the assessed location in the modern since-1980 record.",
                sensitivity="unresolved",
                reasoning="The modern historical record supports limited tropical-cyclone track relevance for this location under the defined 500 km screen.",
                confidence="medium",
                confidence_reason="IBTrACS provides a strong modern historical track archive, but absence of past nearby tracks does not guarantee future absence and does not address non-tropical severe wind.",
                action_type="routine_reassessment",
                action="Reassess periodically and continue authoritative severe-weather monitoring; evaluate non-tropical wind separately.",
                support=support,
            )
    else:
        add(
            "tropical_cyclone", "evidence_gap",
            "Historical tropical-cyclone proximity could not be characterised.",
            sensitivity="unresolved",
            confidence="unresolved",
            confidence_reason=cyclone_ctx.get("reason") or "IBTrACS historical evidence was unavailable.",
            action_type="verify",
            action="Resolve NOAA IBTrACS historical cyclone evidence before classifying tropical-cyclone relevance.",
            support=["ibtracs_cyclone_history_status"],
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

    fire_ctx = telemetry.get("fire_context") or {}
    if fire_ctx.get("status") == "ok":
        fire5 = fire_ctx.get("detection_count_within_5km") or 0
        fire10 = fire_ctx.get("detection_count_within_10km") or 0
        fire25 = fire_ctx.get("detection_count_within_25km") or 0
        support = [
            "firms_thermal_anomaly_count_5km_5d",
            "firms_thermal_anomaly_count_10km_5d",
            "firms_thermal_anomaly_count_25km_5d",
            "firms_nearest_thermal_anomaly_km",
            "firms_peak_frp_mw_5d",
        ]
        if fire5 > 0:
            add(
                "wildfire_operational", "monitor",
                f"NASA FIRMS detected {fire5} thermal anomaly/anomalies within 5 km of the facility in the latest five-day window.",
                sensitivity="unresolved",
                reasoning="Very close satellite thermal anomalies warrant operational attention, but FIRMS detections are not verified wildfire perimeters.",
                consequence="Potential smoke, fire-response, outdoor-work, access or utility disruption may require local verification and authority guidance.",
                confidence="medium",
                confidence_reason="NASA FIRMS provides near-real-time satellite detections; exact fire perimeter, cause and facility exposure are not established.",
                action_type="verify",
                action="Check local fire authorities/incident sources immediately where detections are current and verify whether the anomaly represents an active wildfire affecting the facility.",
                support=support,
            )
        elif fire10 > 0 or fire25 > 0:
            add(
                "wildfire_operational", "monitor",
                "NASA FIRMS detected nearby thermal anomalies in the latest five-day window.",
                sensitivity="unresolved",
                reasoning="Nearby satellite thermal anomalies are operational context, not proof of wildfire exposure at the facility.",
                confidence="medium",
                confidence_reason="Thermal anomalies are directly observed by satellite but require incident/local-source confirmation.",
                action_type="monitor",
                action="Monitor official/local fire information and operational impacts such as smoke, access and power disruption.",
                support=support,
            )
        else:
            add(
                "wildfire_operational", "low_relevance",
                "No NASA FIRMS thermal anomaly was resolved within 25 km in the latest five-day operational window.",
                sensitivity="unresolved",
                reasoning="The current satellite screen does not indicate a nearby thermal anomaly, but this says nothing about long-term wildfire susceptibility.",
                confidence="medium",
                confidence_reason="The conclusion is limited to the current five-day FIRMS operational window.",
                action_type="routine_reassessment",
                action="Continue operational fire monitoring during relevant seasons and emergencies.",
                support=support,
            )

        add(
            "wildfire", "evidence_gap",
            "Long-horizon wildfire susceptibility and burn probability remain unresolved.",
            sensitivity="unresolved",
            reasoning="Near-real-time FIRMS thermal anomalies do not characterize historical burn frequency, fuels, vegetation, drought-conditioned fire weather or future burn probability.",
            confidence="unresolved",
            confidence_reason="A defensible long-horizon wildfire hazard/susceptibility layer has not yet been integrated.",
            action_type="verify",
            action="Add a validated wildfire susceptibility/burn-history layer before classifying long-term wildfire materiality.",
            support=support,
        )
    else:
        add(
            "wildfire", "evidence_gap",
            "Wildfire relevance has not yet been characterised.",
            confidence="unresolved",
            confidence_reason=fire_ctx.get("reason") or "NASA FIRMS operational evidence is unavailable and no long-horizon wildfire layer is integrated.",
            action_type="verify",
            action="Configure FIRMS operational monitoring and add a validated long-horizon wildfire exposure/history layer.",
            support=["firms_operational_status"],
        )

    terrain = telemetry.get("terrain") or {}
    if terrain.get("status") == "ok":
        t250 = terrain.get("metrics_250m") or {}
        t1k = terrain.get("metrics_1km") or {}
        slope95 = t1k.get("slope_p95_deg")
        relief = t1k.get("relief_m")
        terrain_support = [
            "terrain_point_elevation_m",
            "terrain_slope_mean_250m_deg",
            "terrain_slope_p95_250m_deg",
            "terrain_relief_250m_m",
            "terrain_slope_mean_1km_deg",
            "terrain_slope_p95_1km_deg",
            "terrain_slope_max_1km_deg",
            "terrain_relief_1km_m",
            "terrain_elevation_std_1km_m",
            "client_practical_access_routes",
        ]

        if (slope95 or 0) >= 30 or (relief or 0) >= 250:
            add(
                "landslide", "monitor",
                "Copernicus DEM screening indicates steep and/or high-relief terrain around the assessed facility; landslide susceptibility itself remains unresolved.",
                sensitivity="high" if str(profile.get("practical_access_routes") or "").lower() == "1" else "unresolved",
                reasoning="Steep/high-relief terrain can increase the relevance of slope-instability and access-disruption pathways, but DEM-derived slope is not a landslide model.",
                consequence="Potential slope-related access disruption or site-adjacent instability warrants dedicated susceptibility and local/geotechnical verification where material.",
                confidence="medium",
                confidence_reason="Terrain metrics are traceable at ~30 m resolution, but geology, soils, faults, land cover and rainfall-triggered landslide susceptibility are not yet integrated.",
                action_type="verify",
                action="Add a dedicated landslide susceptibility/nowcast layer and verify site geology, cut slopes, retaining structures and critical access routes where applicable.",
                support=terrain_support,
            )
        elif (slope95 or 0) >= 15 or (relief or 0) >= 100:
            add(
                "landslide", "monitor",
                "Terrain screening indicates moderate slope or relief around the assessed facility; dedicated landslide susceptibility remains unresolved.",
                sensitivity="unresolved",
                reasoning="The terrain context is relevant enough to retain monitoring, but DEM slope alone does not establish slope instability.",
                confidence="medium",
                confidence_reason="Copernicus DEM supports terrain characterization, not landslide probability.",
                action_type="verify",
                action="Add dedicated landslide susceptibility evidence before classifying landslide materiality.",
                support=terrain_support,
            )
        else:
            add(
                "landslide", "evidence_gap",
                "Terrain around the assessed site is not strongly steep in the current DEM screen, but landslide susceptibility remains unresolved.",
                sensitivity="unresolved",
                reasoning="Low-to-moderate DEM slope does not by itself rule out landslide mechanisms, cut-slope failure, local geology or route-level exposure.",
                confidence="unresolved",
                confidence_reason="A dedicated landslide susceptibility model has not yet been integrated.",
                action_type="verify",
                action="Add landslide susceptibility evidence where the decision or local terrain warrants it.",
                support=terrain_support,
            )
    else:
        add(
            "landslide", "evidence_gap",
            "Landslide and terrain susceptibility have not yet been characterised.",
            reasoning="A suitable terrain model was unavailable and point elevation alone cannot establish slope instability.",
            confidence="unresolved",
            confidence_reason=terrain.get("reason") or "Terrain evidence is unavailable.",
            action_type="verify",
            action="Resolve DEM-derived terrain evidence and add an appropriate landslide susceptibility source.",
            support=["terrain_dem_status", "elevation_m"],
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
