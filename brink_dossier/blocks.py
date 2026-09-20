def build_report_blocks(meta, data, answers):
    pga = data["design_pga"]
    vs30 = data["vs30"]
    flood = data["flood_depth_100"]
    roads = data["road_count"]
    ratio = data["rate_ratio"]

    score = int(min(92, max(28, (pga * 110) + (flood * 22) + (ratio * 4.2))))
    score_tier = "VERY HIGH" if score > 70 else ("HIGH" if score > 50 else "MODERATE")
    score_color = "#7E2721" if score > 70 else ("#8A5A14" if score > 50 else "#6B5A1E")

    cover = {
        "score": score, "tier_name": f"{score_tier} EXPOSURE ({score}/100)",
        "score_color": score_color,
        "summary_line": f"Active rate elevation {ratio}x · 1-in-100 flood depth: {flood} m"
    }

    s1_blocks = [
        {"kind": "flag", "title": "WHAT TO ACT ON THIS WEEK", "severe": True, "text": "Two conditions require attention: clearing perimeter drainage gullies and inspecting unbolted machinery/pallet racking."},
        {"kind": "decision", "action": "Clear storm drainage gullies and elevate ground-level pallets", "rationale": f"1-in-100 flood depth is {flood} m. Blocked gullies cause preventable yard ponding.", "due": "Within 7 Days", "effort": "1 shift, zero spend", "loss_avoided": "Inventory damage avoided"},
        {"kind": "decision", "action": "Establish backup plan for access road blockage", "rationale": f"Only {roads} road reaches this site. Plan an alternate logistics staging point.", "due": "This Quarter", "effort": "Operational plan only", "loss_avoided": "Downtime reduction"}
    ]

    s2_blocks = [
        {"kind": "trio", "figure": f"{pga}g", "conf": "MODELLED", "conf_class": "c-mod", "label": "Expected shaking, 1-in-475-year earthquake", "what": "Ground shaking force against gravity.", "why": "Strong. Enough to crack unreinforced masonry and topple unbolted racking."},
        {"kind": "trio", "figure": f"{vs30} m/s", "conf": "MODELLED", "conf_class": "c-mod", "label": "Ground firmness (Vs30)", "what": "Shear-wave velocity in top 30m of soil.", "why": "Soft soil amplifies shaking by ~1.6x compared to rock."},
        {"kind": "trio", "figure": f"{ratio}x", "conf": "OBSERVED", "conf_class": "c-obs", "label": "Current crustal activity vs baseline", "what": "Earthquake rate vs 10-year average.", "why": "Marks an elevated stress clustering window."}
    ]
    if data.get("recent_quakes"):
        s2_blocks.append({
            "kind": "table", "title": "Recent Earthquakes in Surrounding Area",
            "headers": ["WHERE", "MAGNITUDE", "DEPTH", "DISTANCE", "WHEN"],
            "rows": [[q["place"], q["mag"], q["depth"], q["dist"], q["when"]] for q in data["recent_quakes"]]
        })

    s3_blocks = [
        {"kind": "trio", "figure": f"{flood} m", "conf": "MODELLED", "conf_class": "c-mod", "label": "1-in-100-year flood depth", "what": "Water depth in a 1% annual chance flood.", "why": "Knee deep. Unraised floor stock is lost before the structure fails."},
        {"kind": "trio", "figure": f"{int(data['elevation'])} m", "conf": "OBSERVED", "conf_class": "c-obs", "label": "Height above sea level", "what": "Elevation from Copernicus DEM.", "why": "Inland alluvium; slow natural drainage."}
    ]

    s4_blocks = [
        {"kind": "trio", "figure": f"{roads} road", "conf": "OBSERVED", "conf_class": "c-obs", "color": "#9E2B25" if roads == 1 else "var(--ink)", "label": "Separate road approaches", "what": "Distinct roads crossing perimeter.", "why": "Single point of failure if blocked."},
        {"kind": "table", "title": "Emergency Infrastructure Proximity", "headers": ["SERVICE", "DISTANCE", "IMPLICATION"], "rows": [["Fire Station", f"{data['fire_dist_km']} km", f"Estimated response time ~{int(data['fire_dist_km']*1.8)} minutes."]]}
    ]

    s5_blocks = [
        {"kind": "glossary", "term": "1-in-100-Year Flood", "def": "A flood severe enough that it has a 1% chance of occurring in any given year."},
        {"kind": "glossary", "term": "g (Peak Ground Acceleration)", "def": "How hard the ground shakes compared to gravity."},
        {"kind": "glossary", "term": "Vs30 / Soil Class", "def": "Wave velocity through soil. Soft soil amplifies shaking."},
        {"kind": "glossary", "term": "Lifelines", "def": "The roads, grid feeders, and water pipelines an asset depends on."}
    ]

    sections = [
        {"title": "What We Found", "subtitle": "Executive summary and priority actions.", "blocks": s1_blocks},
        {"title": "The Ground Under You", "subtitle": "Seismic shaking demand and crustal release rates.", "blocks": s2_blocks},
        {"title": "Water & Drainage", "subtitle": "Flood depth and elevation.", "blocks": s3_blocks},
        {"title": "Getting In and Out", "subtitle": "Road access and emergency service distance.", "blocks": s4_blocks},
        {"title": "What The Terms Mean", "subtitle": "Plain-English explanations of technical metrics.", "blocks": s5_blocks}
    ]
    return cover, sections
