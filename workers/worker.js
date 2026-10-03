
const DOSSIER_FREE_LIMIT = 2;
const DOSSIER_SAMPLE_RADIUS_KM = 300;
const DOSSIER_NEARBY_RADIUS_KM = 1000;

function jsonResponse(body, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...extraHeaders }
  });
}

function normalizeEmail(value) {
  return String(value || "").trim().toLowerCase();
}

function validEmail(value) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value);
}

function haversineKm(lat1, lon1, lat2, lon2) {
  const R = 6371.0088;
  const toRad = d => d * Math.PI / 180;
  const dLat = toRad(lat2 - lat1);
  const dLon = toRad(lon2 - lon1);
  const a = Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(lat1)) * Math.cos(toRad(lat2)) *
    Math.sin(dLon / 2) ** 2;
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

async function sha256Hex(value) {
  const bytes = new TextEncoder().encode(value);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, "0")).join("");
}

async function hmacHex(secret, value) {
  if (!secret || !value) return null;
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(value));
  return [...new Uint8Array(sig)].map(b => b.toString(16).padStart(2, "0")).join("");
}

function networkContext(request) {
  const cf = request.cf || {};
  return {
    ip: request.headers.get("CF-Connecting-IP") || "",
    country: cf.country || null,
    region: cf.region || cf.regionCode || null,
    city: cf.city || null
  };
}

function sbHeaders(key, prefer = null) {
  const headers = {
    "apikey": key,
    "Authorization": `Bearer ${key}`,
    "Content-Type": "application/json"
  };
  if (prefer) headers["Prefer"] = prefer;
  return headers;
}

async function geocodeRequestedLocation(body) {
  const lat = Number(body.latitude);
  const lon = Number(body.longitude);
  if (Number.isFinite(lat) && Number.isFinite(lon) &&
      lat >= -90 && lat <= 90 && lon >= -180 && lon <= 180) {
    return {
      label: String(body.location || "Selected location").trim().slice(0, 220),
      lat, lon,
      country: body.country || null,
      countryCode: body.country_code || null,
      geocoder: "device_coordinates"
    };
  }

  const rawQuery = String(body.location || "").trim();
  if (!rawQuery) throw new Error("Please enter a location.");

  const parts = rawQuery
    .split(",")
    .map(x => x.trim())
    .filter(Boolean);

  // ---- Primary: Open-Meteo / GeoNames ----
  // Open-Meteo matches best when name is a locality plus at most one qualifier.
  const openMeteoCandidates = [];
  if (parts[0]) openMeteoCandidates.push(parts[0]);
  if (parts[0] && parts.length >= 2) {
    openMeteoCandidates.push(`${parts[0]}, ${parts[parts.length - 1]}`);
  }
  if (parts[0] && parts.length >= 3) {
    openMeteoCandidates.push(`${parts[0]}, ${parts[parts.length - 2]}`);
  }

  const context = parts.slice(1).join(" ").toLowerCase();
  const tokens = new Set(
    context
      .replace(/[^a-z0-9]+/gi, " ")
      .split(/\s+/)
      .filter(x => x.length >= 3)
  );

  let best = null;
  let bestScore = -1;

  for (const candidate of [...new Set(openMeteoCandidates)].slice(0, 3)) {
    try {
      const u = new URL("https://geocoding-api.open-meteo.com/v1/search");
      u.searchParams.set("name", candidate);
      u.searchParams.set("count", "20");
      u.searchParams.set("language", "en");
      u.searchParams.set("format", "json");

      const res = await fetch(u.toString());
      if (!res.ok) continue;

      const data = await res.json();
      const rows = data.results || [];

      for (const row of rows) {
        const haystack = [
          row.name,
          row.admin1,
          row.admin2,
          row.admin3,
          row.admin4,
          row.country,
          row.country_code
        ].filter(Boolean).join(" ").toLowerCase();

        let score = 0;
        if (String(row.name || "").trim().toLowerCase() === String(parts[0] || candidate).trim().toLowerCase()) {
          score += 8;
        }

        for (const token of tokens) {
          if (haystack.includes(token)) score += 2;
        }

        if (row.population) score += 1;
        if (row.admin1) score += 0.5;
        if (row.country) score += 0.5;

        if (score > bestScore) {
          bestScore = score;
          best = row;
        }
      }

      if (best && bestScore >= 10) break;
    } catch (_) {
      // Fall through to the next provider.
    }
  }

  if (best && Number.isFinite(Number(best.latitude)) && Number.isFinite(Number(best.longitude))) {
    return {
      label: [best.name, best.admin2, best.admin1, best.country].filter(Boolean).join(", "),
      lat: Number(best.latitude),
      lon: Number(best.longitude),
      country: best.country || null,
      countryCode: best.country_code || null,
      geocoder: "open_meteo_geonames"
    };
  }

  // ---- Fallback: OpenStreetMap Nominatim ----
  // This is called only after the visitor explicitly submits a location; it is
  // not autocomplete or bulk geocoding. One request per submitted query.
  try {
    const u = new URL("https://nominatim.openstreetmap.org/search");
    u.searchParams.set("q", rawQuery);
    u.searchParams.set("format", "jsonv2");
    u.searchParams.set("addressdetails", "1");
    u.searchParams.set("limit", "5");

    const nomRes = await fetch(u.toString(), {
      headers: {
        "User-Agent": "TheBrinkWorld/1.0 (+https://thebrinkworld.com; contact: thebrink2028@gmail.com)",
        "Referer": "https://thebrinkworld.com/",
        "Accept-Language": "en"
      }
    });

    if (nomRes.ok) {
      const rows = await nomRes.json();
      if (Array.isArray(rows) && rows.length) {
        const locality = String(parts[0] || "").toLowerCase();
        let chosen = null;
        let chosenScore = -1;

        for (const row of rows) {
          const address = row.address || {};
          const haystack = [
            row.display_name,
            address.city,
            address.town,
            address.village,
            address.hamlet,
            address.county,
            address.state,
            address.country
          ].filter(Boolean).join(" ").toLowerCase();

          let score = 0;
          if (locality && haystack.includes(locality)) score += 8;
          for (const token of tokens) {
            if (haystack.includes(token)) score += 2;
          }
          score += Math.min(Number(row.importance || 0), 1);

          if (score > chosenScore) {
            chosenScore = score;
            chosen = row;
          }
        }

        if (chosen && Number.isFinite(Number(chosen.lat)) && Number.isFinite(Number(chosen.lon))) {
          const address = chosen.address || {};
          const countryCode = String(address.country_code || "").toUpperCase() || null;
          return {
            label: chosen.display_name || rawQuery,
            lat: Number(chosen.lat),
            lon: Number(chosen.lon),
            country: address.country || null,
            countryCode,
            geocoder: "openstreetmap_nominatim"
          };
        }
      }
    }
  } catch (err) {
    console.warn("Nominatim geocoding failed:", err?.message || err);
  }

  // ---- Final fallback: Photon / OpenStreetMap ----
  // Useful when a provider blocks datacenter/edge traffic or temporarily fails.
  try {
    const u = new URL("https://photon.komoot.io/api/");
    u.searchParams.set("q", rawQuery);
    u.searchParams.set("limit", "10");
    u.searchParams.set("lang", "en");

    const photonRes = await fetch(u.toString(), {
      headers: {
        "Accept": "application/json"
      }
    });

    if (photonRes.ok) {
      const data = await photonRes.json();
      const features = Array.isArray(data?.features) ? data.features : [];
      const locality = String(parts[0] || "").toLowerCase();
      let chosen = null;
      let chosenScore = -1;

      for (const feature of features) {
        const p = feature?.properties || {};
        const coords = feature?.geometry?.coordinates || [];
        const lon = Number(coords[0]);
        const lat = Number(coords[1]);
        if (!Number.isFinite(lat) || !Number.isFinite(lon)) continue;

        const haystack = [
          p.name,
          p.city,
          p.district,
          p.county,
          p.state,
          p.country,
          p.countrycode
        ].filter(Boolean).join(" ").toLowerCase();

        let score = 0;
        if (locality && haystack.includes(locality)) score += 8;
        for (const token of tokens) {
          if (haystack.includes(token)) score += 2;
        }
        if (p.city || p.name) score += 1;
        if (p.state) score += 0.5;
        if (p.country) score += 0.5;

        if (score > chosenScore) {
          chosenScore = score;
          chosen = { feature, lat, lon };
        }
      }

      if (chosen) {
        const p = chosen.feature.properties || {};
        return {
          label: [p.name || p.city, p.district || p.county, p.state, p.country].filter(Boolean).join(", ") || rawQuery,
          lat: chosen.lat,
          lon: chosen.lon,
          country: p.country || null,
          countryCode: String(p.countrycode || "").toUpperCase() || null,
          geocoder: "photon_openstreetmap"
        };
      }
    }
  } catch (err) {
    console.warn("Photon geocoding failed:", err?.message || err);
  }

  throw new Error("We could not locate that place. Try a nearby town/city, postcode, or latitude/longitude.");
}


async function fetchLead(sbUrl, sbKey, email) {
  const u = new URL(`${sbUrl}/rest/v1/dossier_leads`);
  u.searchParams.set("email", `eq.${email}`);
  u.searchParams.set("select", "id,email,email_verified,sample_count,marketing_opt_in");
  u.searchParams.set("limit", "1");
  const res = await fetch(u.toString(), { headers: sbHeaders(sbKey) });
  if (!res.ok) throw new Error(`Lead lookup failed: ${await res.text()}`);
  const rows = await res.json();
  return rows[0] || null;
}

async function countRecentVerifications(sbUrl, sbKey, field, value, minutes = 60) {
  if (!value) return 0;
  const since = new Date(Date.now() - minutes * 60 * 1000).toISOString();
  const u = new URL(`${sbUrl}/rest/v1/dossier_verifications`);
  u.searchParams.set(field, `eq.${value}`);
  u.searchParams.set("created_at", `gte.${since}`);
  u.searchParams.set("select", "id");
  const res = await fetch(u.toString(), { headers: sbHeaders(sbKey) });
  if (!res.ok) return 0;
  return (await res.json()).length;
}

async function buildThreatSnapshot(sbUrl, sbKey, location, sampleNumber, remainingFree) {
  const select = [
    "id","category","name","country","iso3","severity_tier","signal_mode",
    "record_type","source","latitude","longitude","magnitude","depth_km",
    "population_50km","observed_at","expires_at"
  ].join(",");
  const res = await fetch(
    `${sbUrl}/rest/v1/live_hazards?select=${encodeURIComponent(select)}&limit=1000`,
    { headers: sbHeaders(sbKey) }
  );
  if (!res.ok) throw new Error(`Hazard lookup failed: ${await res.text()}`);
  const now = Date.now();
  const hazards = (await res.json())
    .filter(h => Number.isFinite(Number(h.latitude)) && Number.isFinite(Number(h.longitude)))
    .filter(h => !h.expires_at || new Date(h.expires_at).getTime() >= now)
    .map(h => ({
      ...h,
      distance_km: Math.round(haversineKm(
        location.lat, location.lon,
        Number(h.latitude), Number(h.longitude)
      ))
    }));

  const nearby = hazards
    .filter(h => h.distance_km <= DOSSIER_NEARBY_RADIUS_KM)
    .sort((a, b) => a.distance_km - b.distance_km);

  const local = nearby.filter(h => h.distance_km <= DOSSIER_SAMPLE_RADIUS_KM);
  const officialWarnings = local.filter(h => h.signal_mode === "official_warning");

  const severityRank = { Critical: 4, Severe: 3, Significant: 2, Monitor: 1 };
  const strongest = [...local].sort(
    (a, b) => (severityRank[b.severity_tier] || 0) - (severityRank[a.severity_tier] || 0) ||
              a.distance_km - b.distance_km
  )[0] || null;

  const top = nearby.slice(0, 3).map(h => ({
    category: h.category,
    name: h.name,
    distance_km: h.distance_km,
    severity_tier: h.severity_tier,
    signal_mode: h.signal_mode,
    source: h.source,
    magnitude: h.magnitude,
    depth_km: h.depth_km,
    population_50km: h.population_50km
  }));

  const categoryCounts = {};
  for (const h of local) categoryCounts[h.category] = (categoryCounts[h.category] || 0) + 1;

  let observation = "No active monitored hazard signal is currently resolved within 300 km of this location. That is not a guarantee of safety; it reflects the current feeds and their coverage.";
  if (strongest) {
    observation = `The strongest currently resolved nearby signal is ${strongest.category.replaceAll("_", " ")} at ${strongest.distance_km} km, classified ${strongest.severity_tier} by this monitoring layer.`;
  }
  if (officialWarnings.length) {
    observation = `${officialWarnings.length} official warning signal${officialWarnings.length === 1 ? "" : "s"} currently resolve within 300 km. ${observation}`;
  }

  return {
    generated_at: new Date().toISOString(),
    sample_number: sampleNumber,
    remaining_free: remainingFree,
    location: {
      label: location.label,
      latitude: location.lat,
      longitude: location.lon,
      country: location.country,
      country_code: location.countryCode
    },
    scope: {
      local_radius_km: DOSSIER_SAMPLE_RADIUS_KM,
      nearby_radius_km: DOSSIER_NEARBY_RADIUS_KM,
      note: "This is a concise live-feed snapshot, not the full Location Threat Dossier."
    },
    local_signal_count: local.length,
    official_warning_count: officialWarnings.length,
    highest_current_signal: strongest ? {
      category: strongest.category,
      severity_tier: strongest.severity_tier,
      distance_km: strongest.distance_km,
      source: strongest.source
    } : null,
    category_counts: categoryCounts,
    nearest_signals: top,
    observation,
    locked_sections: [
      "Historical hazard pattern",
      "Climate and extreme-temperature context",
      "Flood and rainfall profile",
      "Seismic and volcanic context",
      "Compound-hazard interpretation",
      "Full sourced location dossier"
    ]
  };
}

async function triggerHazardIngestion(env, scheduledTime = null) {
  if (!env.GITHUB_PAT || !env.GITHUB_REPO) {
    throw new Error("GitHub ingestion trigger environment is incomplete.");
  }
  const response = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${env.GITHUB_PAT}`,
      "Accept": "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "TheBrinkWorld-Hazard-Cron"
    },
    body: JSON.stringify({
      event_type: "hazard_tick",
      client_payload: {
        source: "cloudflare_cron",
        scheduled_time: scheduledTime || new Date().toISOString()
      }
    })
  });
  if (!response.ok) throw new Error(`Hazard ingestion dispatch failed (${response.status}): ${await response.text()}`);
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    const corsHeaders = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type, Authorization",
      "Access-Control-Max-Age": "86400"
    };

    // 0. Handle CORS preflight
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders });
    }

    // 1. Live NOAA NHC Active Storms Proxy (CORS-Bypass + Edge Cache)
    if (url.pathname === "/api/storms/noaa" && request.method === "GET") {
      try {
        const upstream = await fetch("https://www.nhc.noaa.gov/CurrentStorms.json", {
          headers: { "User-Agent": "TheBrinkEngine/1.0" },
          cf: { cacheTtl: 300, cacheEverything: true }
        });
        if (!upstream.ok) throw new Error(`NOAA upstream status ${upstream.status}`);
        const data = await upstream.text();
        return new Response(data, {
          headers: { ...corsHeaders, "Content-Type": "application/json" }
        });
      } catch (err) {
        return new Response(JSON.stringify({ error: err.message, activeStorms: [] }), {
          status: 502,
          headers: { ...corsHeaders, "Content-Type": "application/json" }
        });
      }
    }

    // 1b. NCS India Recent Earthquakes Proxy
    if (url.pathname === "/api/ncs/recent" && request.method === "GET") {
      try {
        const upstream = await fetch("https://riseq.seismo.gov.in/riseq/earthquake/recent_earthquake", {
          headers: {
            "User-Agent": "TheBrinkEngine/1.0 (+https://thebrinkworld.com)",
            "Accept": "text/html,application/xhtml+xml"
          },
          cf: { cacheTtl: 180, cacheEverything: true }
        });
        if (!upstream.ok) throw new Error(`NCS upstream status ${upstream.status}`);
        const html = await upstream.text();

        const decode = value => String(value || "")
          .replace(/<[^>]+>/g, " ")
          .replace(/&nbsp;/gi, " ")
          .replace(/&amp;/gi, "&")
          .replace(/&#39;/g, "'")
          .replace(/&quot;/g, '"')
          .replace(/\s+/g, " ")
          .trim();

        const rows = [];
        const trRegex = /<tr[^>]*>([\s\S]*?)<\/tr>/gi;
        let tr;
        while ((tr = trRegex.exec(html)) !== null) {
          const cells = [];
          const tdRegex = /<t[dh][^>]*>([\s\S]*?)<\/t[dh]>/gi;
          let td;
          while ((td = tdRegex.exec(tr[1])) !== null) cells.push(decode(td[1]));
          if (cells.length < 7) continue;

          const origin = cells[0];
          const lat = Number(cells[1]);
          const lon = Number(cells[2]);
          const depth = Number(cells[3]);
          const magnitude = Number(cells[4]);
          const region = cells[5];
          const location = cells[6];

          if (!origin.match(/^\d{4}-\d{2}-\d{2}/) || !Number.isFinite(lat) || !Number.isFinite(lon) || !Number.isFinite(magnitude)) continue;

          const indiaFacing =
            /india/i.test(location) ||
            /india/i.test(region) ||
            (lat >= 6 && lat <= 38.5 && lon >= 68 && lon <= 98.5);

          if (!indiaFacing) continue;

          const m = origin.match(/^(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})/);
          let observedAt = null;
          if (m) {
            const utcMs = Date.parse(`${m[1]}T${m[2]}+05:30`);
            if (Number.isFinite(utcMs)) observedAt = new Date(utcMs).toISOString();
          }

          rows.push({
            observed_at: observedAt,
            latitude: lat,
            longitude: lon,
            depth_km: Number.isFinite(depth) ? depth : null,
            magnitude,
            region,
            name: location || region || "India region earthquake",
            source: "NCS India"
          });
        }

        rows.sort((a,b) => new Date(b.observed_at || 0) - new Date(a.observed_at || 0));

        return new Response(JSON.stringify({ events: rows.slice(0, 30), source: "National Centre for Seismology, India" }), {
          headers: { ...corsHeaders, "Content-Type": "application/json" }
        });
      } catch (err) {
        return new Response(JSON.stringify({ error: err.message, events: [] }), {
          status: 502,
          headers: { ...corsHeaders, "Content-Type": "application/json" }
        });
      }
    }

    // 2. GDACS Global Tropical Cyclones Proxy (Worldwide Multi-Basin)
    if (url.pathname === "/api/storms/gdacs" && request.method === "GET") {
      try {
        const upstream = await fetch("https://www.gdacs.org/datareport/resources/TC/events.geojson", {
          headers: { "User-Agent": "TheBrinkEngine/1.0" },
          cf: { cacheTtl: 300, cacheEverything: true }
        });
        if (!upstream.ok) throw new Error(`GDACS upstream status ${upstream.status}`);
        const data = await upstream.text();
        return new Response(data, {
          headers: { ...corsHeaders, "Content-Type": "application/json" }
        });
      } catch (err) {
        return new Response(JSON.stringify({ error: err.message, features: [] }), {
          status: 502,
          headers: { ...corsHeaders, "Content-Type": "application/json" }
        });
      }
    }

    // 3. Live Volcano Telemetry Proxy (USGS Volcano Hazards Program)
    if (url.pathname === "/api/volcanoes" && request.method === "GET") {
      try {
        const res = await fetch("https://volcanoes.usgs.gov/vsc/api/volcanoApi/vhpstatus", {
          headers: { "User-Agent": "TheBrinkEngine/1.0" },
          cf: { cacheTtl: 600, cacheEverything: true }
        });
        if (!res.ok) throw new Error(`USGS upstream status ${res.status}`);
        const data = await res.json();
        return new Response(JSON.stringify(data), {
          headers: { ...corsHeaders, "Content-Type": "application/json" }
        });
      } catch (err) {
        return new Response(JSON.stringify({ error: err.message }), {
          status: 500,
          headers: { ...corsHeaders, "Content-Type": "application/json" }
        });
      }
    }

    // 4. NOAA DSCOVR Satellite Solar Wind Plasma Stream
    if (url.pathname === "/api/space/solar-wind" && request.method === "GET") {
      try {
        const res = await fetch("https://services.swpc.noaa.gov/products/solar-wind/plasma-1-hour.json", {
          headers: { "User-Agent": "TheBrinkEngine/1.0" },
          cf: { cacheTtl: 180, cacheEverything: true }
        });
        if (!res.ok) throw new Error(`NOAA SWPC upstream status ${res.status}`);
        const data = await res.text();
        return new Response(data, {
          headers: { ...corsHeaders, "Content-Type": "application/json" }
        });
      } catch (err) {
        return new Response(JSON.stringify([]), {
          status: 502,
          headers: { ...corsHeaders, "Content-Type": "application/json" }
        });
      }
    }

    // 4b. Unified NOAA Space Weather Status
    if (url.pathname === "/api/space/status" && request.method === "GET") {
      try {
        const fetchJson = async (target, ttl = 120) => {
          const r = await fetch(target, {
            headers: { "User-Agent": "TheBrinkEngine/1.0 (+https://thebrinkworld.com)" },
            cf: { cacheTtl: ttl, cacheEverything: true }
          });
          if (!r.ok) throw new Error(`NOAA upstream status ${r.status} for ${target}`);
          return await r.json();
        };

        const [plasmaR, magR, kpR, scalesR, alertsR] = await Promise.allSettled([
          fetchJson("https://services.swpc.noaa.gov/products/solar-wind/plasma-1-hour.json", 120),
          fetchJson("https://services.swpc.noaa.gov/products/solar-wind/mag-1-hour.json", 120),
          fetchJson("https://services.swpc.noaa.gov/json/planetary_k_index_1m.json", 120),
          fetchJson("https://services.swpc.noaa.gov/products/noaa-scales.json", 300),
          fetchJson("https://services.swpc.noaa.gov/products/alerts.json", 180)
        ]);

        const lastTableRow = (data) => {
          if (!Array.isArray(data) || data.length < 2 || !Array.isArray(data[0])) return {};
          const header = data[0];
          for (let i = data.length - 1; i >= 1; i--) {
            if (!Array.isArray(data[i])) continue;
            const out = {};
            header.forEach((key, idx) => { out[key] = data[i][idx]; });
            return out;
          }
          return {};
        };

        const plasma = plasmaR.status === "fulfilled" ? lastTableRow(plasmaR.value) : {};
        const mag = magR.status === "fulfilled" ? lastTableRow(magR.value) : {};

        let kp = null;
        let kpTime = null;
        if (kpR.status === "fulfilled" && Array.isArray(kpR.value) && kpR.value.length) {
          const latest = kpR.value[kpR.value.length - 1] || {};
          const raw = latest.kp_index !== undefined ? latest.kp_index : latest.kp;
          kp = Number.isFinite(Number(raw)) ? Number(raw) : null;
          kpTime = latest.time_tag || latest.time || null;
        }

        const alerts = alertsR.status === "fulfilled" && Array.isArray(alertsR.value) ? alertsR.value : [];
        const geomagneticAlerts = alerts
          .filter(a => /geomagnetic storm|geomagnetic k-index/i.test(String(a.message || "")))
          .sort((a,b) => new Date(b.issue_datetime || 0) - new Date(a.issue_datetime || 0));
        const geomagnetic = geomagneticAlerts[0] || null;

        let driver = null;
        let watchText = null;
        if (geomagnetic && geomagnetic.message) {
          const comment = String(geomagnetic.message).match(/Comment:\s*([^\r\n]+)/i);
          if (comment && comment[1].trim()) driver = comment[1].trim();
          const watch = String(geomagnetic.message).match(/(?:WATCH|WARNING|ALERT):\s*([^\r\n]+)/i);
          if (watch && watch[1].trim()) watchText = watch[1].trim();
        }

        const num = v => Number.isFinite(Number(v)) ? Number(v) : null;
        const body = {
          observed_at: new Date().toISOString(),
          current: {
            kp,
            kp_time: kpTime,
            solar_wind_speed_km_s: num(plasma.speed),
            proton_density_cm3: num(plasma.density),
            plasma_temperature_k: num(plasma.temperature),
            plasma_time: plasma.time_tag || null,
            bt_nt: num(mag.bt),
            bz_nt: num(mag.bz_gsm !== undefined ? mag.bz_gsm : mag.bz),
            bx_nt: num(mag.bx_gsm !== undefined ? mag.bx_gsm : mag.bx),
            by_nt: num(mag.by_gsm !== undefined ? mag.by_gsm : mag.by),
            magnetic_time: mag.time_tag || null
          },
          scales: scalesR.status === "fulfilled" ? scalesR.value : null,
          latest_geomagnetic_message: geomagnetic ? {
            product_id: geomagnetic.product_id || null,
            issue_datetime: geomagnetic.issue_datetime || null,
            headline: watchText,
            driver
          } : null,
          source: "NOAA Space Weather Prediction Center"
        };

        return new Response(JSON.stringify(body), {
          headers: { ...corsHeaders, "Content-Type": "application/json" }
        });
      } catch (err) {
        return new Response(JSON.stringify({ error: err.message }), {
          status: 502,
          headers: { ...corsHeaders, "Content-Type": "application/json" }
        });
      }
    }

    // 5. Unified Identity & Topic Vector Preferences
    if (url.pathname === "/api/subscribe" && request.method === "POST") {
      try {
        const body = await request.json();
        const email = (body.email || "").trim().toLowerCase();

        if (!email || !email.includes("@")) {
          return new Response(JSON.stringify({ error: "Invalid email address" }), {
            status: 400,
            headers: { ...corsHeaders, "Content-Type": "application/json" }
          });
        }

        const sbUrl = env.SUPABASE_URL || "https://jxapuzsgyoetrpnmohct.supabase.co";
        const sbKey = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_ANON_KEY;

        const payload = {
          email: email,
          pref_news: body.pref_news !== undefined ? body.pref_news : true,
          pref_earth: body.pref_earth !== undefined ? body.pref_earth : true,
          pref_health: body.pref_health !== undefined ? body.pref_health : true,
          status: body.unsubscribe_all ? "unsubscribed" : "active",
          source: body.source || "universal_gate",
          location: body.location || "Global Reader",
          updated_at: new Date().toISOString()
        };

        if (sbKey) {
          const sbRes = await fetch(`${sbUrl}/rest/v1/subscribers?on_conflict=email`, {
            method: "POST",
            headers: {
              "apikey": sbKey,
              "Authorization": `Bearer ${sbKey}`,
              "Content-Type": "application/json",
              "Prefer": "resolution=merge-duplicates,return=minimal"
            },
            body: JSON.stringify(payload)
          });

          if (!sbRes.ok) {
            const errText = await sbRes.text();
            throw new Error(`Database error: ${errText}`);
          }
        }

        if (env.RESEND_API_KEY && !body.unsubscribe_all) {
          await fetch("https://api.resend.com/emails", {
            method: "POST",
            headers: {
              "Authorization": `Bearer ${env.RESEND_API_KEY}`,
              "Content-Type": "application/json"
            },
            body: JSON.stringify({
              from: "The Brink World <intel@thebrinkworld.com>",
              to: [email],
              subject: "Confirmed: The Brink World Dispatches",
              html: `
                <h3>Intel Subscription Confirmed</h3>
                <p>Your dispatch channels are active:</p>
                <ul>
                  <li>News &amp; Macro Shifts: <strong>${payload.pref_news ? 'Active' : 'Muted'}</strong></li>
                  <li>Earth &amp; Planetary Hazards: <strong>${payload.pref_earth ? 'Active' : 'Muted'}</strong></li>
                  <li>Health &amp; Outbreak Radar: <strong>${payload.pref_health ? 'Active' : 'Muted'}</strong></li>
                </ul>
                <p>Manage your sensors live on <a href="https://thebrinkworld.com/watch.html">thebrinkworld.com/watch.html</a>.</p>
              `
            })
          }).catch(e => console.warn("Resend email dispatch error:", e));
        }

        return new Response(JSON.stringify({ ok: true, preferences: payload }), {
          status: 200,
          headers: { ...corsHeaders, "Content-Type": "application/json" }
        });
      } catch (err) {
        return new Response(JSON.stringify({ error: err.message }), {
          status: 500,
          headers: { ...corsHeaders, "Content-Type": "application/json" }
        });
      }
    }

    // 6. Lead Intake & Service Requests (Resend Email Dispatch + Supabase Logging)
    if (url.pathname === "/api/inquire" && request.method === "POST") {
      try {
        const data = await request.json();
        const sbUrl = env.SUPABASE_URL || "https://jxapuzsgyoetrpnmohct.supabase.co";
        const sbKey = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_ANON_KEY;

        if (sbKey) {
          await fetch(`${sbUrl}/rest/v1/audit_orders`, {
            method: "POST",
            headers: {
              "apikey": sbKey,
              "Authorization": `Bearer ${sbKey}`,
              "Content-Type": "application/json",
              "Prefer": "return=minimal"
            },
            body: JSON.stringify({
              customer_name: data.name || "Anonymous",
              customer_email: data.email,
              location_query: data.location,
              service_tier: data.service_requested || data.tier || "Single Facility Dossier",
              notes: data.notes || data.scope || "",
              payment_status: "manual_pending",
              created_at: new Date().toISOString()
            })
          });
        }

        if (env.RESEND_API_KEY) {
          await fetch("https://api.resend.com/emails", {
            method: "POST",
            headers: {
              "Authorization": `Bearer ${env.RESEND_API_KEY}`,
              "Content-Type": "application/json"
            },
            body: JSON.stringify({
              from: "The Brink World <onboarding@resend.dev>",
              to: ["thebrink2028@gmail.com"],
              subject: `[AUDIT ORDER / LEAD] ${data.service_requested || data.tier || 'Manual Order'}: ${data.name}`,
              html: `
                <h3>New Asset Audit Intake (Manual Payment Flow)</h3>
                <p><strong>Customer Name:</strong> ${data.name || 'N/A'}</p>
                <p><strong>Email:</strong> ${data.email || 'N/A'}</p>
                <p><strong>Monitored Location / Coordinates:</strong> ${data.location || 'N/A'}</p>
                <p><strong>Service Requested:</strong> ${data.service_requested || data.tier || 'Single Facility Dossier'}</p>
                <p><strong>Notes / Scope:</strong></p>
                <blockquote style="background:#f4f4f4;padding:12px;border-left:4px solid #00f3ff;">
                  ${data.notes || data.scope || 'Customer forwarded to Razorpay Payment Link.'}
                </blockquote>
              `
            })
          });
        }

        return new Response(JSON.stringify({ ok: true }), {
          headers: { ...corsHeaders, "Content-Type": "application/json" }
        });
      } catch (err) {
        return new Response(JSON.stringify({ error: err.message }), { 
          status: 500, 
          headers: { ...corsHeaders, "Content-Type": "application/json" } 
        });
      }
    }


    // 7. Free Location Threat Snapshot Funnel
    if (url.pathname === "/api/dossier/request-code" && request.method === "POST") {
      try {
        const body = await request.json();
        const email = normalizeEmail(body.email);
        if (!validEmail(email)) {
          return jsonResponse({ error: "Enter a valid email address." }, 400, corsHeaders);
        }

        const sbUrl = env.SUPABASE_URL;
        const sbKey = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SERVICE_KEY;
        if (!sbUrl || !sbKey) throw new Error("Dossier database environment is incomplete.");

        const lead = await fetchLead(sbUrl, sbKey, email);
        if (lead && Number(lead.sample_count || 0) >= DOSSIER_FREE_LIMIT) {
          return jsonResponse({
            error: "free_sample_limit_reached",
            message: "You have used both complimentary Location Threat Snapshots.",
            remaining_free: 0,
            paid_price: { usd: 29, inr: 2499, usdt: 29 }
          }, 402, corsHeaders);
        }

        const location = await geocodeRequestedLocation(body);
        const net = networkContext(request);
        const ipHash = await hmacHex(env.IP_HASH_SECRET, net.ip);

        const recentEmail = await countRecentVerifications(sbUrl, sbKey, "email", email, 60);
        const recentIp = await countRecentVerifications(sbUrl, sbKey, "ip_hash", ipHash, 60);
        if (recentEmail >= 5 || recentIp >= 12) {
          return jsonResponse({ error: "Too many verification requests. Please try again later." }, 429, corsHeaders);
        }

        const code = String(Math.floor(100000 + Math.random() * 900000));
        const codeHash = await sha256Hex(`${email}:${code}:${env.VERIFICATION_SECRET || ""}`);
        const verification = {
          email,
          code_hash: codeHash,
          requested_location: location.label,
          requested_lat: location.lat,
          requested_lon: location.lon,
          requested_country: location.country,
          requested_country_code: location.countryCode,
          marketing_opt_in: body.marketing_opt_in === true,
          ip_hash: ipHash,
          ip_country: net.country,
          ip_region: net.region,
          ip_city: net.city,
          expires_at: new Date(Date.now() + 10 * 60 * 1000).toISOString()
        };

        const saveRes = await fetch(`${sbUrl}/rest/v1/dossier_verifications`, {
          method: "POST",
          headers: sbHeaders(sbKey, "return=minimal"),
          body: JSON.stringify(verification)
        });
        if (!saveRes.ok) throw new Error(`Verification save failed: ${await saveRes.text()}`);

        if (!env.RESEND_API_KEY) throw new Error("Email verification service is not configured.");
        const mailRes = await fetch("https://api.resend.com/emails", {
          method: "POST",
          headers: {
            "Authorization": `Bearer ${env.RESEND_API_KEY}`,
            "Content-Type": "application/json"
          },
          body: JSON.stringify({
            from: env.DOSSIER_FROM_EMAIL || "The Brink World <intel@thebrinkworld.com>",
            to: [email],
            reply_to: env.DOSSIER_REPLY_TO || "thebrink2028@gmail.com",
            subject: "Your Location Threat Snapshot verification code",
            html: `
              <div style="font-family:Arial,sans-serif;max-width:560px;margin:auto;color:#111">
                <p style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#555">The Brink World · Location Threat Snapshot</p>
                <h2 style="margin-bottom:8px">Your verification code is ${code}</h2>
                <p>Use this code within 10 minutes to test:</p>
                <p><strong>${location.label.replace(/[<>&"]/g, "")}</strong></p>
                <p style="color:#666">Each verified email receives two complimentary snapshots. A snapshot is a concise live-feed view, not the full paid Location Threat Dossier.</p>
              </div>
            `
          })
        });
        if (!mailRes.ok) throw new Error(`Verification email failed: ${await mailRes.text()}`);

        return jsonResponse({
          ok: true,
          message: "Verification code sent.",
          location,
          remaining_free: lead ? DOSSIER_FREE_LIMIT - Number(lead.sample_count || 0) : DOSSIER_FREE_LIMIT
        }, 200, corsHeaders);
      } catch (err) {
        return jsonResponse({ error: err.message }, 500, corsHeaders);
      }
    }

    if (url.pathname === "/api/dossier/verify" && request.method === "POST") {
      try {
        const body = await request.json();
        const email = normalizeEmail(body.email);
        const code = String(body.code || "").trim();
        if (!validEmail(email) || !/^\d{6}$/.test(code)) {
          return jsonResponse({ error: "Enter the six-digit verification code." }, 400, corsHeaders);
        }

        const sbUrl = env.SUPABASE_URL;
        const sbKey = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SERVICE_KEY;
        if (!sbUrl || !sbKey) throw new Error("Dossier database environment is incomplete.");

        const u = new URL(`${sbUrl}/rest/v1/dossier_verifications`);
        u.searchParams.set("email", `eq.${email}`);
        u.searchParams.set("consumed_at", "is.null");
        u.searchParams.set("expires_at", `gte.${new Date().toISOString()}`);
        u.searchParams.set("select", "*");
        u.searchParams.set("order", "created_at.desc");
        u.searchParams.set("limit", "1");

        const lookup = await fetch(u.toString(), { headers: sbHeaders(sbKey) });
        if (!lookup.ok) throw new Error(`Verification lookup failed: ${await lookup.text()}`);
        const rows = await lookup.json();
        const v = rows[0];
        if (!v) return jsonResponse({ error: "This code has expired. Request a new one." }, 400, corsHeaders);

        if (Number(v.attempts || 0) >= 6) {
          return jsonResponse({ error: "Too many incorrect attempts. Request a new code." }, 429, corsHeaders);
        }

        const codeHash = await sha256Hex(`${email}:${code}:${env.VERIFICATION_SECRET || ""}`);
        if (codeHash !== v.code_hash) {
          await fetch(`${sbUrl}/rest/v1/dossier_verifications?id=eq.${encodeURIComponent(v.id)}`, {
            method: "PATCH",
            headers: sbHeaders(sbKey, "return=minimal"),
            body: JSON.stringify({ attempts: Number(v.attempts || 0) + 1 })
          });
          return jsonResponse({ error: "Incorrect verification code." }, 400, corsHeaders);
        }

        const claimRes = await fetch(`${sbUrl}/rest/v1/rpc/claim_dossier_sample`, {
          method: "POST",
          headers: sbHeaders(sbKey),
          body: JSON.stringify({
            p_email: email,
            p_requested_location: v.requested_location,
            p_requested_lat: v.requested_lat,
            p_requested_lon: v.requested_lon,
            p_requested_country: v.requested_country,
            p_requested_country_code: v.requested_country_code,
            p_ip_hash: v.ip_hash,
            p_ip_country: v.ip_country,
            p_ip_region: v.ip_region,
            p_ip_city: v.ip_city,
            p_marketing_opt_in: v.marketing_opt_in === true
          })
        });

        if (!claimRes.ok) {
          const detail = await claimRes.text();
          if (detail.includes("free_sample_limit_reached")) {
            return jsonResponse({
              error: "free_sample_limit_reached",
              message: "You have used both complimentary Location Threat Snapshots.",
              remaining_free: 0,
              paid_price: { usd: 29, inr: 2499, usdt: 29 }
            }, 402, corsHeaders);
          }
          throw new Error(`Sample claim failed: ${detail}`);
        }

        const claimRows = await claimRes.json();
        const claim = claimRows[0];
        const location = {
          label: v.requested_location,
          lat: Number(v.requested_lat),
          lon: Number(v.requested_lon),
          country: v.requested_country,
          countryCode: v.requested_country_code
        };

        const snapshot = await buildThreatSnapshot(
          sbUrl, sbKey, location,
          Number(claim.sample_number),
          Number(claim.remaining_free)
        );

        await Promise.all([
          fetch(`${sbUrl}/rest/v1/dossier_verifications?id=eq.${encodeURIComponent(v.id)}`, {
            method: "PATCH",
            headers: sbHeaders(sbKey, "return=minimal"),
            body: JSON.stringify({ consumed_at: new Date().toISOString() })
          }),
          fetch(`${sbUrl}/rest/v1/dossier_requests?id=eq.${encodeURIComponent(claim.request_id)}`, {
            method: "PATCH",
            headers: sbHeaders(sbKey, "return=minimal"),
            body: JSON.stringify({
              status: "delivered",
              sample_snapshot: snapshot,
              delivered_at: new Date().toISOString()
            })
          })
        ]);

        return jsonResponse({
          ok: true,
          snapshot,
          paid_price: { usd: 29, inr: 2499, usdt: 29 }
        }, 200, corsHeaders);
      } catch (err) {
        return jsonResponse({ error: err.message }, 500, corsHeaders);
      }
    }


    // 8. Paid Location Threat Dossier — manual payment verification
    if (url.pathname === "/api/dossier/payment-submit" && request.method === "POST") {
      try {
        const body = await request.json();
        const email = normalizeEmail(body.email);
        const clientName = String(body.client_name || "").trim();
        const organization = String(body.organization || "").trim();
        const purpose = String(body.purpose || "").trim();
        const concern = String(body.concern || "").trim();
        const rawPurposeDetails = body.purpose_details && typeof body.purpose_details === "object" ? body.purpose_details : {};
        const purposeDetails = {
          crop: String(rawPurposeDetails.crop || "").trim() || null,
          crop_stage: String(rawPurposeDetails.crop_stage || "").trim() || null,
          sowing_date: String(rawPurposeDetails.sowing_date || "").trim() || null
        };
        const paymentMethod = String(body.payment_method || "").trim();
        const paymentReference = String(body.payment_reference || "").trim();

        if (!validEmail(email)) return jsonResponse({ error: "Enter a valid client email." }, 400, corsHeaders);
        if (!clientName) return jsonResponse({ error: "Enter the client name." }, 400, corsHeaders);
        const priceBook = {
          inr_razorpay: { currency: "INR", amount: 2499, label: "Razorpay INR" },
          usd_razorpay: { currency: "USD", amount: 29, label: "Razorpay USD" },
          usdt_trc20: { currency: "USDT", amount: 29, label: "USDT · TRON (TRC20)" }
        };
        const price = priceBook[paymentMethod];
        if (!price) return jsonResponse({ error: "Choose a valid payment method." }, 400, corsHeaders);

        const sbUrl = env.SUPABASE_URL;
        const sbKey = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SERVICE_KEY;
        if (!sbUrl || !sbKey) throw new Error("Dossier database environment is incomplete.");

        const location = await geocodeRequestedLocation(body);
        const net = networkContext(request);
        const ipHash = await hmacHex(env.IP_HASH_SECRET, net.ip);

        const uuid = crypto.randomUUID();
        const orderCode = "BRK-LTD-" + uuid.replaceAll("-", "").slice(0, 8).toUpperCase();
        const approvalToken = crypto.randomUUID() + crypto.randomUUID();
        const approvalHash = await sha256Hex(
          orderCode + ":" + approvalToken + ":" + (env.VERIFICATION_SECRET || "")
        );
        const now = new Date().toISOString();
        const approvalExpiry = new Date(Date.now() + 7 * 24 * 60 * 60 * 1000).toISOString();

        const order = {
          order_code: orderCode,
          email,
          request_type: "paid_full",
          requested_location: location.label,
          requested_lat: location.lat,
          requested_lon: location.lon,
          requested_country: location.country,
          requested_country_code: location.countryCode,
          client_name: clientName,
          organization: organization || null,
          purpose: purpose || "General location intelligence",
          concern: concern || null,
          purpose_details: purposeDetails,
          payment_method: paymentMethod,
          payment_reference: paymentReference || null,
          payment_currency: price.currency,
          payment_amount: price.amount,
          payment_status: "submitted",
          payment_submitted_at: now,
          approval_token_hash: approvalHash,
          approval_token_expires_at: approvalExpiry,
          status: "awaiting_payment_verification",
          ip_hash: ipHash,
          ip_country: net.country,
          ip_region: net.region,
          ip_city: net.city,
          created_at: now
        };

        const saveRes = await fetch(`${sbUrl}/rest/v1/dossier_requests`, {
          method: "POST",
          headers: sbHeaders(sbKey, "return=representation"),
          body: JSON.stringify(order)
        });
        if (!saveRes.ok) throw new Error(`Order save failed: ${await saveRes.text()}`);
        const savedRows = await saveRes.json();
        const saved = savedRows[0];

        if (!env.RESEND_API_KEY) throw new Error("Admin email service is not configured.");

        const reviewUrl = new URL("/api/dossier/review", url.origin);
        reviewUrl.searchParams.set("order", orderCode);
        reviewUrl.searchParams.set("token", approvalToken);

        const sender = env.DOSSIER_FROM_EMAIL || "The Brink World <intel@thebrinkworld.com>";
        const adminMail = await fetch("https://api.resend.com/emails", {
          method: "POST",
          headers: {
            "Authorization": `Bearer ${env.RESEND_API_KEY}`,
            "Content-Type": "application/json"
          },
          body: JSON.stringify({
            from: sender,
            to: ["thebrink2028@gmail.com"],
            reply_to: email,
            subject: `[PAYMENT TO VERIFY] ${orderCode} · ${price.currency} ${price.amount} · ${clientName}`,
            html: `
              <div style="font-family:Arial,sans-serif;max-width:680px;margin:auto;color:#111">
                <p style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#555">The Brink World · Manual Payment Verification</p>
                <h2 style="margin-bottom:8px">Payment submitted — verify before generating</h2>
                <table style="border-collapse:collapse;width:100%">
                  <tr><td style="padding:6px 0;color:#666">Order</td><td><strong>${orderCode}</strong></td></tr>
                  <tr><td style="padding:6px 0;color:#666">Client</td><td>${clientName}</td></tr>
                  <tr><td style="padding:6px 0;color:#666">Email</td><td>${email}</td></tr>
                  <tr><td style="padding:6px 0;color:#666">Organisation</td><td>${organization || "—"}</td></tr>
                  <tr><td style="padding:6px 0;color:#666">Location</td><td>${location.label}</td></tr>
                  <tr><td style="padding:6px 0;color:#666">Purpose</td><td>${purpose || "General location intelligence"}</td></tr>
                  ${purpose === "Agriculture / land" ? `
                  <tr><td style="padding:6px 0;color:#666">Crop</td><td>${purposeDetails.crop || "—"}</td></tr>
                  <tr><td style="padding:6px 0;color:#666">Crop stage</td><td>${purposeDetails.crop_stage || "—"}</td></tr>
                  <tr><td style="padding:6px 0;color:#666">Sowing date</td><td>${purposeDetails.sowing_date || "—"}</td></tr>` : ""}
                  <tr><td style="padding:6px 0;color:#666">Payment</td><td><strong>${price.label} · ${price.currency} ${price.amount}</strong></td></tr>
                  <tr><td style="padding:6px 0;color:#666">Reference / TxID</td><td style="word-break:break-all"><strong>${paymentReference || "Not supplied — verify by client/order details"}</strong></td></tr>
                </table>
                <p style="margin-top:18px">Check the payment independently in Razorpay or TRON before approving.</p>
                <p><a href="${reviewUrl.toString()}" style="display:inline-block;background:#0b0d11;color:#fff;padding:12px 18px;text-decoration:none;border-radius:4px">REVIEW & APPROVE PAYMENT</a></p>
                <p style="font-size:12px;color:#777">This link expires in 7 days. Opening it does not generate the report; final confirmation is required on the review page.</p>
              </div>
            `
          })
        });
        if (!adminMail.ok) throw new Error(`Admin verification email failed: ${await adminMail.text()}`);

        // Customer acknowledgement — no claim that payment is verified.
        await fetch("https://api.resend.com/emails", {
          method: "POST",
          headers: {
            "Authorization": `Bearer ${env.RESEND_API_KEY}`,
            "Content-Type": "application/json"
          },
          body: JSON.stringify({
            from: sender,
            to: [email],
            reply_to: env.DOSSIER_REPLY_TO || "thebrink2028@gmail.com",
            subject: `Payment submitted for verification — ${orderCode}`,
            html: `
              <div style="font-family:Arial,sans-serif;max-width:620px;margin:auto;color:#111">
                <p style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#555">The Brink World · Location Threat Dossier</p>
                <h2>We received your payment reference.</h2>
                <p><strong>Order:</strong> ${orderCode}</p>
                <p><strong>Location:</strong> ${location.label}</p>
                <p><strong>Payment submitted:</strong> ${price.currency} ${price.amount} via ${price.label}</p>
                <p>Your payment will be checked manually before the report is generated. Submission of a reference or transaction hash is not confirmation of payment.</p>
              </div>
            `
          })
        }).catch(() => {});

        return jsonResponse({
          ok: true,
          order_code: orderCode,
          status: "awaiting_payment_verification",
          message: "Payment reference submitted. The Brink World will verify it before generating the dossier."
        }, 200, corsHeaders);
      } catch (err) {
        return jsonResponse({ error: err.message }, 500, corsHeaders);
      }
    }

    if (url.pathname === "/api/dossier/review" && request.method === "GET") {
      const orderCode = String(url.searchParams.get("order") || "");
      const token = String(url.searchParams.get("token") || "");
      const sbUrl = env.SUPABASE_URL;
      const sbKey = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SERVICE_KEY;
      if (!orderCode || !token || !sbUrl || !sbKey) {
        return new Response("Invalid review link.", { status: 400, headers: { "Content-Type": "text/plain" } });
      }

      const lookupUrl = new URL(`${sbUrl}/rest/v1/dossier_requests`);
      lookupUrl.searchParams.set("order_code", `eq.${orderCode}`);
      lookupUrl.searchParams.set("select", "*");
      lookupUrl.searchParams.set("limit", "1");
      const lookup = await fetch(lookupUrl.toString(), { headers: sbHeaders(sbKey) });
      const rows = lookup.ok ? await lookup.json() : [];
      const order = rows[0];
      const suppliedHash = await sha256Hex(orderCode + ":" + token + ":" + (env.VERIFICATION_SECRET || ""));
      const expired = !order?.approval_token_expires_at || new Date(order.approval_token_expires_at).getTime() < Date.now();

      if (!order || suppliedHash !== order.approval_token_hash || expired) {
        return new Response("This review link is invalid or expired.", { status: 403, headers: { "Content-Type": "text/plain" } });
      }

      const esc = value => String(value ?? "").replace(/[&<>"']/g, ch => ({
        "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"
      }[ch]));

      const already = ["generating", "delivered"].includes(order.status);
      const html = `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
      <title>Verify ${esc(orderCode)} · The Brink World</title>
      <style>
        body{font-family:Arial,sans-serif;background:#080b10;color:#eef2f7;margin:0;padding:28px}
        .card{max-width:720px;margin:auto;background:#111722;border:1px solid #263242;border-radius:10px;padding:24px}
        h1{font-size:24px;margin:0 0 18px}.muted{color:#9aa8b8}.row{padding:9px 0;border-bottom:1px solid #263242}
        .k{display:inline-block;width:180px;color:#9aa8b8}.v{font-weight:600}.warn{background:#241b0d;border:1px solid #6d4b16;padding:12px;border-radius:6px;margin:16px 0}
        button{background:#00f3ff;color:#061018;border:0;border-radius:5px;padding:12px 18px;font-weight:800;cursor:pointer}
        button:disabled{opacity:.5}.ref{word-break:break-all}
      </style></head><body><div class="card">
        <div class="muted" style="font-size:12px;letter-spacing:.08em;text-transform:uppercase">The Brink World · Payment Verification</div>
        <h1>${esc(orderCode)}</h1>
        <div class="row"><span class="k">Client</span><span class="v">${esc(order.client_name)}</span></div>
        <div class="row"><span class="k">Email</span><span class="v">${esc(order.email)}</span></div>
        <div class="row"><span class="k">Location</span><span class="v">${esc(order.requested_location)}</span></div>
        <div class="row"><span class="k">Payment method</span><span class="v">${esc(order.payment_method)}</span></div>
        <div class="row"><span class="k">Expected amount</span><span class="v">${esc(order.payment_currency)} ${esc(order.payment_amount)}</span></div>
        <div class="row"><span class="k">Reference / TxID</span><span class="v ref">${esc(order.payment_reference || "Not supplied")}</span></div>
        <div class="warn"><strong>Manual check required.</strong><br>Confirm the payment independently in Razorpay or TRON. Do not approve based only on the reference supplied by the client.</div>
        ${already ? '<p><strong>This order has already been approved or is being fulfilled.</strong></p>' : `
        <form method="post" action="/api/dossier/approve">
          <input type="hidden" name="order" value="${esc(orderCode)}">
          <input type="hidden" name="token" value="${esc(token)}">
          <label style="display:block;margin:12px 0"><input type="checkbox" name="confirmed" value="yes" required> I independently verified that the expected payment was received.</label>
          <button type="submit">APPROVE PAYMENT & GENERATE DOSSIER</button>
        </form>`}
      </div></body></html>`;
      return new Response(html, { headers: { "Content-Type": "text/html; charset=utf-8" } });
    }

    if (url.pathname === "/api/dossier/approve" && request.method === "POST") {
      try {
        const form = await request.formData();
        const orderCode = String(form.get("order") || "");
        const token = String(form.get("token") || "");
        const confirmed = String(form.get("confirmed") || "") === "yes";
        if (!orderCode || !token || !confirmed) throw new Error("Approval confirmation is incomplete.");

        const sbUrl = env.SUPABASE_URL;
        const sbKey = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SERVICE_KEY;
        if (!sbUrl || !sbKey) throw new Error("Dossier database environment is incomplete.");

        const lookupUrl = new URL(`${sbUrl}/rest/v1/dossier_requests`);
        lookupUrl.searchParams.set("order_code", `eq.${orderCode}`);
        lookupUrl.searchParams.set("select", "*");
        lookupUrl.searchParams.set("limit", "1");
        const lookup = await fetch(lookupUrl.toString(), { headers: sbHeaders(sbKey) });
        if (!lookup.ok) throw new Error("Order lookup failed.");
        const rows = await lookup.json();
        const order = rows[0];
        if (!order) throw new Error("Order not found.");

        const suppliedHash = await sha256Hex(orderCode + ":" + token + ":" + (env.VERIFICATION_SECRET || ""));
        if (suppliedHash !== order.approval_token_hash) throw new Error("Invalid approval token.");
        if (!order.approval_token_expires_at || new Date(order.approval_token_expires_at).getTime() < Date.now()) {
          throw new Error("Approval link expired.");
        }
        if (order.status === "delivered") {
          return new Response("This dossier has already been delivered.", { status: 200, headers: { "Content-Type": "text/plain" } });
        }

        const verifiedAt = new Date().toISOString();

        // Claim the order before dispatching. The status filter makes approval idempotent:
        // only an order awaiting verification, or a previously failed dispatch, can be claimed.
        const claimUrl = new URL(`${sbUrl}/rest/v1/dossier_requests`);
        claimUrl.searchParams.set("order_code", `eq.${orderCode}`);
        claimUrl.searchParams.set("status", "in.(awaiting_payment_verification,dispatch_failed)");
        const patch = await fetch(claimUrl.toString(), {
          method: "PATCH",
          headers: sbHeaders(sbKey, "return=representation"),
          body: JSON.stringify({
            payment_status: "verified",
            payment_verified_at: verifiedAt,
            status: "generating"
          })
        });
        if (!patch.ok) throw new Error(`Could not mark payment verified: ${await patch.text()}`);
        const claimedRows = await patch.json();
        if (!Array.isArray(claimedRows) || claimedRows.length === 0) {
          return new Response(
            "This order has already been approved or is already being generated.",
            { status: 200, headers: { "Content-Type": "text/plain; charset=utf-8" } }
          );
        }

        if (!env.GITHUB_PAT || !env.GITHUB_REPO) {
          throw new Error("GitHub report-dispatch environment is incomplete.");
        }

        const dispatch = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`, {
          method: "POST",
          headers: {
            "Authorization": `Bearer ${env.GITHUB_PAT}`,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "TheBrinkWorld-Dossier-Approval"
          },
          body: JSON.stringify({
            event_type: "order_paid",
            client_payload: {
              order_code: orderCode,
              order_id: order.id,
              location: `${order.requested_lat},${order.requested_lon}`,
              site_name: order.requested_location,
              customer_email: order.email,
              answers: {
                customer_name: order.client_name,
                organization: order.organization,
                occupancy: order.purpose || "general",
                concern: order.concern || "",
                purpose_details: order.purpose_details || {},
                country: order.requested_country || null,
                country_code: order.requested_country_code || null,
                order_code: orderCode
              }
            }
          })
        });
        if (!dispatch.ok) {
          const dispatchText = await dispatch.text();
          await fetch(`${sbUrl}/rest/v1/dossier_requests?order_code=eq.${encodeURIComponent(orderCode)}`, {
            method: "PATCH",
            headers: sbHeaders(sbKey, "return=minimal"),
            body: JSON.stringify({ status: "dispatch_failed" })
          });
          if (dispatch.status === 401) {
            throw new Error("GitHub dispatch authentication failed. The Worker GITHUB_PAT is invalid, expired, revoked, or not the current token.");
          }
          if (dispatch.status === 403) {
            throw new Error("GitHub dispatch was forbidden. The Worker token does not have permission to dispatch this repository.");
          }
          throw new Error(`GitHub dispatch failed (${dispatch.status}): ${dispatchText}`);
        }

        return new Response(
          `<!doctype html><html><body style="font-family:Arial,sans-serif;background:#080b10;color:#eef2f7;padding:40px"><div style="max-width:650px;margin:auto"><h2>Payment approved.</h2><p>${orderCode} has been sent to the dossier engine for generation and delivery.</p><p>The client and thebrink2028@gmail.com will receive separate copies after generation.</p></div></body></html>`,
          { headers: { "Content-Type": "text/html; charset=utf-8" } }
        );
      } catch (err) {
        return new Response(
          `Approval failed: ${err.message}`,
          { status: 500, headers: { "Content-Type": "text/plain; charset=utf-8" } }
        );
      }
    }


    // 8b. B2B Commercial Facility Intake + Activation
    if (url.pathname === "/api/commercial/request" && request.method === "POST") {
      try {
        const body = await request.json();
        const email = normalizeEmail(body.email);
        const contactName = String(body.contact_name || "").trim();
        const organization = String(body.organization || "").trim();
        const facilityName = String(body.facility_name || "").trim();
        const facilityType = String(body.facility_type || "Commercial property / facility").trim();
        const productType = String(body.product_type || "").trim();
        const cadence = String(body.cadence || "monthly").trim().toLowerCase();
        const criticalFunction = String(body.critical_function || "").trim();
        const notes = String(body.notes || "").trim();
        const paymentMethod = String(body.payment_method || "").trim().toLowerCase();
        const paymentReference = String(body.payment_reference || "").trim();
        const sourcePage = String(body.source_page || "facility-risk.html").trim();

        const termsVersion = "TBW-TOS-2026-10-01";
        const privacyVersion = "TBW-PRIVACY-2026-10-01";
        const authorityConfirmed = body.authority_confirmed === true;
        const clientDeclarationConfirmed = body.client_declaration_confirmed === true;
        const termsAccepted = body.terms_accepted === true;
        const privacyAccepted = body.privacy_accepted === true;

        const safeInt = value => {
          const text = String(value ?? "").trim();
          if (!text) return null;
          const parsed = Number.parseInt(text, 10);
          return Number.isFinite(parsed) ? parsed : null;
        };

        const constructionType = String(body.construction_type || "unknown").trim() || "unknown";
        const yearBuilt = safeInt(body.year_built);
        const floorsAboveGround = safeInt(body.floors_above_ground);
        const basementPresent = String(body.basement_present || "unknown").trim().toLowerCase();
        const criticalEquipmentLevel = String(body.critical_equipment_level || "unknown").trim().toLowerCase();
        const backupPower = String(body.backup_power || "unknown").trim().toLowerCase();
        const waterDependency = String(body.water_dependency || "unknown").trim().toLowerCase();
        const coolingDependency = String(body.cooling_dependency || "unknown").trim().toLowerCase();
        const practicalAccessRoutes = String(body.practical_access_routes || "unknown").trim().toLowerCase();
        const drainageProtection = String(body.drainage_protection || "").trim();
        const previousDisruptions = String(body.previous_disruptions || "").trim();
        const criticalDependencies = String(body.critical_dependencies || "").trim();
        const resilienceMeasures = String(body.resilience_measures || "").trim();

        const allowedProducts = new Set([
          "location_dossier",
          "facility_risk_passport",
          "physical_risk_evidence_pack",
          "pre_underwriting_site_intelligence",
          "business_continuity_threat_register"
        ]);
        const allowedCadence = new Set(["weekly","monthly","quarterly","annual","one_off"]);

        if (!validEmail(email)) return jsonResponse({ error: "Enter a valid business email." }, 400, corsHeaders);
        if (!contactName || !organization || !facilityName) {
          return jsonResponse({ error: "Contact name, organisation and facility name are required." }, 400, corsHeaders);
        }
        if (!allowedProducts.has(productType)) return jsonResponse({ error: "Choose a valid commercial product." }, 400, corsHeaders);
        if (!allowedCadence.has(cadence)) return jsonResponse({ error: "Choose a valid reporting cadence." }, 400, corsHeaders);
        if (!authorityConfirmed || !clientDeclarationConfirmed || !termsAccepted || !privacyAccepted) {
          return jsonResponse({ error: "Commercial Terms, Privacy Notice, authority and information declaration must be accepted." }, 400, corsHeaders);
        }

        const allowedBasement = new Set(["yes","no","unknown"]);
        const allowedDependency = new Set(["low","moderate","high","critical","unknown"]);
        if (!allowedBasement.has(basementPresent)) return jsonResponse({ error: "Choose a valid basement status." }, 400, corsHeaders);
        if (!allowedDependency.has(waterDependency) || !allowedDependency.has(coolingDependency)) {
          return jsonResponse({ error: "Choose valid water and cooling dependency levels." }, 400, corsHeaders);
        }
        if (yearBuilt !== null && (yearBuilt < 1800 || yearBuilt > 2100)) {
          return jsonResponse({ error: "Enter a plausible construction year or leave it unknown." }, 400, corsHeaders);
        }
        if (floorsAboveGround !== null && (floorsAboveGround < 0 || floorsAboveGround > 300)) {
          return jsonResponse({ error: "Enter a plausible number of floors or leave it unknown." }, 400, corsHeaders);
        }

        const sbUrl = env.SUPABASE_URL;
        const sbKey = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SERVICE_KEY;
        if (!sbUrl || !sbKey) throw new Error("Commercial database environment is incomplete.");

        const location = await geocodeRequestedLocation(body);
        const now = new Date().toISOString();

        const facilityPayload = {
          organization_name: organization,
          facility_name: facilityName,
          location_label: location.label,
          latitude: location.lat,
          longitude: location.lon,
          country: location.country || null,
          country_code: location.countryCode || null,
          facility_type: facilityType || null,
          critical_function: criticalFunction || null,
          dependencies: {},
          contact_name: contactName,
          contact_email: email,
          status: "active",
          created_at: now,
          updated_at: now
        };

        const facilityRes = await fetch(`${sbUrl}/rest/v1/brink_facilities`, {
          method: "POST",
          headers: sbHeaders(sbKey, "return=representation"),
          body: JSON.stringify(facilityPayload)
        });
        if (!facilityRes.ok) throw new Error(`Facility save failed: ${await facilityRes.text()}`);
        const facilityRows = await facilityRes.json();
        const facility = facilityRows[0];
        if (!facility) throw new Error("Facility record was not returned.");

        const subscriptionId = crypto.randomUUID();
        const approvalToken = crypto.randomUUID() + crypto.randomUUID();
        const approvalHash = await sha256Hex(subscriptionId + ":" + approvalToken + ":" + (env.VERIFICATION_SECRET || ""));
        const approvalExpiry = new Date(Date.now() + 14 * 24 * 60 * 60 * 1000).toISOString();

        const subPayload = {
          id: subscriptionId,
          facility_id: facility.id,
          product_type: productType,
          cadence,
          status: "pending_review",
          requested_at: now,
          approval_token_hash: approvalHash,
          approval_token_expires_at: approvalExpiry,
          commercial_terms: {
            notes: notes || null,
            requested_product: productType,
            requested_cadence: cadence,
            source: sourcePage,
            payment_method: paymentMethod || null,
            payment_reference: paymentReference || null,
            terms_version: termsVersion,
            privacy_version: privacyVersion,
            facility_profile_schema: "TBW-FACILITY-PROFILE-v1"
          },
          created_at: now
        };

        const subRes = await fetch(`${sbUrl}/rest/v1/brink_monitoring_subscriptions`, {
          method: "POST",
          headers: sbHeaders(sbKey, "return=representation"),
          body: JSON.stringify(subPayload)
        });
        if (!subRes.ok) {
          // Clean up the facility if the paired subscription cannot be created.
          await fetch(`${sbUrl}/rest/v1/brink_facilities?id=eq.${encodeURIComponent(facility.id)}`, {
            method: "DELETE",
            headers: sbHeaders(sbKey, "return=minimal")
          }).catch(() => {});
          throw new Error(`Subscription save failed: ${await subRes.text()}`);
        }

        const profilePayload = {
          facility_id: facility.id,
          profile_version: 1,
          construction_type: constructionType,
          year_built: yearBuilt,
          floors_above_ground: floorsAboveGround,
          basement_present: basementPresent,
          critical_equipment_level: criticalEquipmentLevel,
          backup_power: backupPower,
          water_dependency: waterDependency,
          cooling_dependency: coolingDependency,
          practical_access_routes: practicalAccessRoutes,
          drainage_protection: drainageProtection || null,
          previous_disruptions: previousDisruptions ? [{ description: previousDisruptions }] : [],
          critical_dependencies: criticalDependencies ? { client_notes: criticalDependencies } : {},
          resilience_measures: resilienceMeasures ? { client_notes: resilienceMeasures } : {},
          source: "client_declared",
          declared_by: contactName,
          declared_at: now,
          valid_from: now,
          is_current: true,
          notes: notes || null
        };

        const profileRes = await fetch(`${sbUrl}/rest/v1/brink_facility_profiles`, {
          method: "POST",
          headers: sbHeaders(sbKey, "return=representation"),
          body: JSON.stringify(profilePayload)
        });
        if (!profileRes.ok) {
          await fetch(`${sbUrl}/rest/v1/brink_facilities?id=eq.${encodeURIComponent(facility.id)}`, {
            method: "DELETE",
            headers: sbHeaders(sbKey, "return=minimal")
          }).catch(() => {});
          throw new Error(`Facility profile save failed: ${await profileRes.text()}`);
        }

        const acceptancePayload = {
          facility_id: facility.id,
          subscription_id: subscriptionId,
          contact_name: contactName,
          contact_email: email,
          organization_name: organization,
          terms_version: termsVersion,
          privacy_version: privacyVersion,
          authority_confirmed: true,
          client_declaration_confirmed: true,
          accepted_at: now,
          acceptance_context: {
            source: sourcePage,
            product_type: productType,
            payment_method: paymentMethod || null,
            payment_reference: paymentReference || null,
            cadence,
            facility_profile_schema: "TBW-FACILITY-PROFILE-v1"
          }
        };

        const acceptanceRes = await fetch(`${sbUrl}/rest/v1/brink_contract_acceptances`, {
          method: "POST",
          headers: sbHeaders(sbKey, "return=representation"),
          body: JSON.stringify(acceptancePayload)
        });
        if (!acceptanceRes.ok) {
          await fetch(`${sbUrl}/rest/v1/brink_facilities?id=eq.${encodeURIComponent(facility.id)}`, {
            method: "DELETE",
            headers: sbHeaders(sbKey, "return=minimal")
          }).catch(() => {});
          throw new Error(`Contract acceptance save failed: ${await acceptanceRes.text()}`);
        }

        const reviewUrl = new URL("/api/commercial/review", url.origin);
        reviewUrl.searchParams.set("subscription", subscriptionId);
        reviewUrl.searchParams.set("token", approvalToken);

        const sender = env.DOSSIER_FROM_EMAIL || "The Brink World <intel@thebrinkworld.com>";
        if (env.RESEND_API_KEY) {
          const safe = x => String(x || "").replace(/[<>&"]/g, "");
          const productLabels = {
            location_dossier: "Location Threat Dossier",
            facility_risk_passport: "Facility Risk Passport",
            physical_risk_evidence_pack: "Physical Risk Evidence Pack",
            pre_underwriting_site_intelligence: "Pre-Underwriting Site Intelligence",
            business_continuity_threat_register: "External Threat Register"
          };

          const adminMail = await fetch("https://api.resend.com/emails", {
            method: "POST",
            headers: { "Authorization": `Bearer ${env.RESEND_API_KEY}`, "Content-Type": "application/json" },
            body: JSON.stringify({
              from: sender,
              to: ["thebrink2028@gmail.com"],
              reply_to: email,
              subject: `[COMMERCIAL FACILITY REVIEW] ${facilityName} · ${productLabels[productType]}`,
              html: `
                <div style="font-family:Arial,sans-serif;max-width:680px;margin:auto;color:#111">
                  <p style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#555">The Brink World · Commercial Facility Intake</p>
                  <h2>${safe(facilityName)}</h2>
                  <table style="border-collapse:collapse;width:100%">
                    <tr><td style="padding:6px;color:#666">Organisation</td><td><strong>${safe(organization)}</strong></td></tr>
                    <tr><td style="padding:6px;color:#666">Contact</td><td>${safe(contactName)} · ${safe(email)}</td></tr>
                    <tr><td style="padding:6px;color:#666">Location</td><td>${safe(location.label)}</td></tr>
                    <tr><td style="padding:6px;color:#666">Product</td><td>${safe(productLabels[productType])}</td></tr>
                    <tr><td style="padding:6px;color:#666">Cadence</td><td>${safe(cadence)}</td></tr>
                    <tr><td style="padding:6px;color:#666">Critical function</td><td>${safe(criticalFunction || "—")}</td></tr>
                    <tr><td style="padding:6px;color:#666">Facility profile</td><td>Client-declared V2 profile captured</td></tr>
                    <tr><td style="padding:6px;color:#666">Payment method</td><td>${safe(paymentMethod || "Not supplied")}</td></tr>
                    <tr><td style="padding:6px;color:#666">Payment reference</td><td>${safe(paymentReference || "Not supplied")}</td></tr>
                    <tr><td style="padding:6px;color:#666">Terms accepted</td><td>${safe(termsVersion)} · ${safe(privacyVersion)}</td></tr>
                  </table>
                  <p><strong>Requested workflow:</strong> ${safe(notes || "Not supplied")}</p>
                  <p><a href="${reviewUrl.toString()}" style="display:inline-block;background:#0b0d11;color:#fff;padding:12px 18px;text-decoration:none">Review request & payment</a></p>
                </div>
              `
            })
          });
          if (!adminMail.ok) {
            console.error("Commercial admin email failed:", adminMail.status, await adminMail.text());
          }

          const customerMail = await fetch("https://api.resend.com/emails", {
            method: "POST",
            headers: { "Authorization": `Bearer ${env.RESEND_API_KEY}`, "Content-Type": "application/json" },
            body: JSON.stringify({
              from: sender,
              to: [email],
              reply_to: "thebrink2028@gmail.com",
              subject: "Your facility risk request has been received",
              html: `
                <div style="font-family:Arial,sans-serif;max-width:620px;margin:auto;color:#111">
                  <p style="font-size:12px;letter-spacing:.08em;text-transform:uppercase;color:#555">The Brink World · Facility Risk Intelligence</p>
                  <h2>We received your facility request.</h2>
                  <p><strong>${safe(facilityName)}</strong><br>${safe(location.label)}</p>
                  <p>We will review the requested scope before any paid monitoring or recurring reporting is activated.</p>
                  <p>Reference: <strong>${subscriptionId.slice(0,8).toUpperCase()}</strong></p>
                </div>
              `
            })
          });
          if (!customerMail.ok) {
            console.error("Commercial customer acknowledgement failed:", customerMail.status, await customerMail.text());
          }
        }

        return jsonResponse({
          ok: true,
          reference: subscriptionId.slice(0,8).toUpperCase(),
          status: "pending_review",
          terms_version: termsVersion,
          privacy_version: privacyVersion,
          facility_profile_saved: true
        }, 200, corsHeaders);
      } catch (err) {
        return jsonResponse({ error: err.message }, 500, corsHeaders);
      }
    }

    if (url.pathname === "/api/commercial/review" && request.method === "GET") {
      const subscriptionId = String(url.searchParams.get("subscription") || "");
      const token = String(url.searchParams.get("token") || "");
      const sbUrl = env.SUPABASE_URL;
      const sbKey = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SERVICE_KEY;
      if (!subscriptionId || !token || !sbUrl || !sbKey) {
        return new Response("Invalid commercial review link.", { status: 400, headers: { "Content-Type": "text/plain" } });
      }

      const subUrl = new URL(`${sbUrl}/rest/v1/brink_monitoring_subscriptions`);
      subUrl.searchParams.set("id", `eq.${subscriptionId}`);
      subUrl.searchParams.set("select", "*");
      subUrl.searchParams.set("limit", "1");
      const subRes = await fetch(subUrl.toString(), { headers: sbHeaders(sbKey) });
      const subs = subRes.ok ? await subRes.json() : [];
      const sub = subs[0];

      if (!sub) return new Response("Subscription request not found.", { status: 404, headers: { "Content-Type": "text/plain" } });

      const suppliedHash = await sha256Hex(subscriptionId + ":" + token + ":" + (env.VERIFICATION_SECRET || ""));
      const expired = !sub.approval_token_expires_at || new Date(sub.approval_token_expires_at).getTime() < Date.now();
      if (suppliedHash !== sub.approval_token_hash || expired) {
        return new Response("This review link is invalid or expired.", { status: 403, headers: { "Content-Type": "text/plain" } });
      }

      const fUrl = new URL(`${sbUrl}/rest/v1/brink_facilities`);
      fUrl.searchParams.set("id", `eq.${sub.facility_id}`);
      fUrl.searchParams.set("select", "*");
      fUrl.searchParams.set("limit", "1");
      const fRes = await fetch(fUrl.toString(), { headers: sbHeaders(sbKey) });
      const fs = fRes.ok ? await fRes.json() : [];
      const facility = fs[0];
      if (!facility) return new Response("Facility record not found.", { status: 404, headers: { "Content-Type": "text/plain" } });

      const esc = value => String(value ?? "").replace(/[&<>"']/g, ch => ({
        "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"
      }[ch]));
      const labels = {
        location_dossier: "Location Threat Dossier",
        facility_risk_passport: "Facility Risk Passport",
        physical_risk_evidence_pack: "Physical Risk Evidence Pack",
        pre_underwriting_site_intelligence: "Pre-Underwriting Site Intelligence",
        business_continuity_threat_register: "External Threat Register"
      };
      const already = sub.status !== "pending_review";

      const html = `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
      <title>Commercial Review · The Brink World</title>
      <style>
      body{font-family:Arial,sans-serif;background:#080b10;color:#eef2f7;margin:0;padding:28px}.card{max-width:760px;margin:auto;background:#111722;border:1px solid #263348;border-radius:10px;padding:24px}
      h1{margin-top:4px}.row{display:grid;grid-template-columns:180px 1fr;gap:12px;padding:8px 0;border-bottom:1px solid #202b3a}.k{color:#8fa1b6}.v{font-weight:700}.warn{margin:18px 0;padding:12px;border:1px solid #a97821;background:#2a2111;border-radius:6px;color:#f2ddb3}
      button{background:#00f3ff;color:#061018;border:0;border-radius:5px;padding:12px 18px;font-weight:800;cursor:pointer}@media(max-width:560px){.row{grid-template-columns:1fr;gap:3px}}
      </style></head><body><div class="card">
      <div style="font-size:12px;color:#8fa1b6;text-transform:uppercase;letter-spacing:.08em">The Brink World · Commercial Activation</div>
      <h1>${esc(facility.facility_name)}</h1>
      <div class="row"><span class="k">Organisation</span><span class="v">${esc(facility.organization_name)}</span></div>
      <div class="row"><span class="k">Contact</span><span class="v">${esc(facility.contact_name)} · ${esc(facility.contact_email)}</span></div>
      <div class="row"><span class="k">Location</span><span class="v">${esc(facility.location_label)}</span></div>
      <div class="row"><span class="k">Product</span><span class="v">${esc(labels[sub.product_type] || sub.product_type)}</span></div>
      <div class="row"><span class="k">Cadence</span><span class="v">${esc(sub.cadence)}</span></div>
      <div class="row"><span class="k">Critical function</span><span class="v">${esc(facility.critical_function || "—")}</span></div>
      <div class="row"><span class="k">Requested workflow</span><span class="v">${esc(sub.commercial_terms?.notes || "—")}</span></div>
      <div class="row"><span class="k">Payment method</span><span class="v">${esc(sub.commercial_terms?.payment_method || "Not supplied")}</span></div>
      <div class="row"><span class="k">Payment reference</span><span class="v">${esc(sub.commercial_terms?.payment_reference || "Not supplied")}</span></div>
      <div class="warn"><strong>Activation starts draft generation.</strong><br>Confirm the commercial scope/payment separately before activating. The generated report is sent to The Brink World for internal review first; client delivery is a separate approval step.</div>
      ${already ? `<p><strong>Status: ${esc(sub.status)}</strong></p>` : `
      <form method="post" action="/api/commercial/activate">
        <input type="hidden" name="subscription" value="${esc(subscriptionId)}">
        <input type="hidden" name="token" value="${esc(token)}">
        <label style="display:block;margin:14px 0"><input type="checkbox" name="confirmed" value="yes" required> I have reviewed this request and confirm the commercial scope/payment is approved. Generate the internal draft only; do not send it to the client yet.</label>
        <button type="submit">APPROVE SCOPE/PAYMENT & GENERATE DRAFT</button>
      </form>`}
      </div></body></html>`;

      return new Response(html, { headers: { "Content-Type": "text/html; charset=utf-8" } });
    }

    if (url.pathname === "/api/commercial/activate" && request.method === "POST") {
      try {
        const form = await request.formData();
        const subscriptionId = String(form.get("subscription") || "");
        const token = String(form.get("token") || "");
        const confirmed = String(form.get("confirmed") || "") === "yes";
        if (!subscriptionId || !token || !confirmed) throw new Error("Activation confirmation is incomplete.");

        const sbUrl = env.SUPABASE_URL;
        const sbKey = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SERVICE_KEY;
        if (!sbUrl || !sbKey) throw new Error("Commercial database environment is incomplete.");

        const lookupUrl = new URL(`${sbUrl}/rest/v1/brink_monitoring_subscriptions`);
        lookupUrl.searchParams.set("id", `eq.${subscriptionId}`);
        lookupUrl.searchParams.set("select", "*");
        lookupUrl.searchParams.set("limit", "1");
        const lookup = await fetch(lookupUrl.toString(), { headers: sbHeaders(sbKey) });
        if (!lookup.ok) throw new Error("Subscription lookup failed.");
        const rows = await lookup.json();
        const sub = rows[0];
        if (!sub) throw new Error("Subscription not found.");

        const suppliedHash = await sha256Hex(subscriptionId + ":" + token + ":" + (env.VERIFICATION_SECRET || ""));
        if (suppliedHash !== sub.approval_token_hash) throw new Error("Invalid activation token.");
        if (!sub.approval_token_expires_at || new Date(sub.approval_token_expires_at).getTime() < Date.now()) {
          throw new Error("Activation link expired.");
        }
        if (sub.status !== "pending_review") {
          return new Response("This subscription is already active, completed, or otherwise processed.", { status: 200, headers: { "Content-Type":"text/plain" } });
        }

        const now = new Date().toISOString();
        const patch = await fetch(`${sbUrl}/rest/v1/brink_monitoring_subscriptions?id=eq.${encodeURIComponent(subscriptionId)}&status=eq.pending_review`, {
          method: "PATCH",
          headers: sbHeaders(sbKey, "return=representation"),
          body: JSON.stringify({
            status: "active",
            approved_at: now,
            approved_by: "The Brink World",
            next_report_at: now
          })
        });
        if (!patch.ok) throw new Error(`Activation failed: ${await patch.text()}`);
        const activated = await patch.json();
        if (!Array.isArray(activated) || activated.length === 0) {
          return new Response("Subscription was already processed.", { status: 200, headers: { "Content-Type":"text/plain" } });
        }

        if (!env.GITHUB_PAT || !env.GITHUB_REPO) throw new Error("GitHub commercial dispatch environment is incomplete.");
        const dispatch = await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`, {
          method:"POST",
          headers:{
            "Authorization":`Bearer ${env.GITHUB_PAT}`,
            "Accept":"application/vnd.github+json",
            "X-GitHub-Api-Version":"2022-11-28",
            "User-Agent":"TheBrinkWorld-Commercial-Activation"
          },
          body:JSON.stringify({
            event_type:"commercial_tick",
            client_payload:{subscription_id:subscriptionId, source:"commercial_activation"}
          })
        });

        if (!dispatch.ok) {
          await fetch(`${sbUrl}/rest/v1/brink_monitoring_subscriptions?id=eq.${encodeURIComponent(subscriptionId)}`, {
            method:"PATCH",
            headers:sbHeaders(sbKey,"return=minimal"),
            body:JSON.stringify({status:"activation_dispatch_failed"})
          });
          throw new Error(`Commercial report dispatch failed (${dispatch.status}): ${await dispatch.text()}`);
        }

        return new Response(
          '<!doctype html><html><body style="font-family:Arial,sans-serif;background:#080b10;color:#eef2f7;padding:40px"><div style="max-width:650px;margin:auto"><h2>Facility monitoring activated.</h2><p>The first commercial report has been queued for internal review. The client will not receive the generated report until The Brink World approves client delivery.</p></div></body></html>',
          { headers:{ "Content-Type":"text/html; charset=utf-8" } }
        );
      } catch (err) {
        return new Response(`Activation failed: ${err.message}`, { status:500, headers:{ "Content-Type":"text/plain; charset=utf-8" } });
      }
    }


    // Commercial report delivery approval gate.
    // Draft generation is complete before this stage; GET only displays review metadata.
    if (url.pathname === "/api/commercial/report-review" && request.method === "GET") {
      try {
        const runId = String(url.searchParams.get("run") || "");
        const token = String(url.searchParams.get("token") || "");
        const sbUrl = env.SUPABASE_URL;
        const sbKey = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SERVICE_KEY;
        if (!runId || !token || !sbUrl || !sbKey) {
          throw new Error("Invalid report-review link.");
        }

        const runUrl = new URL(`${sbUrl}/rest/v1/brink_report_runs`);
        runUrl.searchParams.set("id", `eq.${runId}`);
        runUrl.searchParams.set(
          "select",
          "id,facility_id,subscription_id,product_type,report_ref,report_status,evidence_as_of,evidence_summary,output_location,completed_at"
        );
        runUrl.searchParams.set("limit", "1");
        const runRes = await fetch(runUrl.toString(), { headers: sbHeaders(sbKey) });
        if (!runRes.ok) throw new Error("Report-run lookup failed.");
        const reportRun = (await runRes.json())[0];
        if (!reportRun) throw new Error("Report draft not found.");

        const summary = reportRun.evidence_summary || {};
        const suppliedHash = await sha256Hex(token);
        const expiresAt = summary.delivery_approval_expires_at
          ? new Date(summary.delivery_approval_expires_at).getTime()
          : 0;
        if (!summary.delivery_approval_hash || suppliedHash !== summary.delivery_approval_hash || expiresAt < Date.now()) {
          return new Response("This report-review link is invalid or expired.", {
            status: 403,
            headers: { "Content-Type": "text/plain; charset=utf-8" }
          });
        }

        const fUrl = new URL(`${sbUrl}/rest/v1/brink_facilities`);
        fUrl.searchParams.set("id", `eq.${reportRun.facility_id}`);
        fUrl.searchParams.set("select", "*");
        fUrl.searchParams.set("limit", "1");
        const facilityRes = await fetch(fUrl.toString(), { headers: sbHeaders(sbKey) });
        const facility = facilityRes.ok ? (await facilityRes.json())[0] : null;
        if (!facility) throw new Error("Facility record not found.");

        const subUrl = new URL(`${sbUrl}/rest/v1/brink_monitoring_subscriptions`);
        subUrl.searchParams.set("id", `eq.${reportRun.subscription_id}`);
        subUrl.searchParams.set("select", "id,product_type,cadence,status,approved_at,approved_by");
        subUrl.searchParams.set("limit", "1");
        const subRes = await fetch(subUrl.toString(), { headers: sbHeaders(sbKey) });
        const sub = subRes.ok ? (await subRes.json())[0] : null;

        const esc = value => String(value ?? "").replace(/[&<>"']/g, ch => ({
          "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#039;"
        }[ch]));
        const productLabels = {
          location_dossier: "Location Threat Dossier",
          facility_risk_passport: "Facility Risk Passport",
          physical_risk_evidence_pack: "Physical Risk Evidence Pack",
          pre_underwriting_site_intelligence: "Pre-Underwriting Site Intelligence",
          business_continuity_threat_register: "External Threat Register"
        };
        const alreadyDelivered = reportRun.report_status === "delivered";
        const canDeliver = reportRun.report_status === "awaiting_approval";

        const html = `<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
        <title>Report Delivery Review · The Brink World</title>
        <style>
        body{font-family:Arial,sans-serif;background:#080b10;color:#eef2f7;margin:0;padding:28px}
        .card{max-width:760px;margin:auto;background:#111722;border:1px solid #263348;border-radius:10px;padding:24px}
        h1{margin:6px 0 18px}.row{display:grid;grid-template-columns:190px 1fr;gap:12px;padding:9px 0;border-bottom:1px solid #202b3a}
        .k{color:#8fa1b6}.v{font-weight:700}.ok{margin:18px 0;padding:12px;border:1px solid #1c8c61;background:#0f2a22;border-radius:6px;color:#c8f7e6}
        .warn{margin:18px 0;padding:12px;border:1px solid #a97821;background:#2a2111;border-radius:6px;color:#f2ddb3}
        button{background:#00f3ff;color:#061018;border:0;border-radius:5px;padding:12px 18px;font-weight:800;cursor:pointer}
        @media(max-width:560px){.row{grid-template-columns:1fr;gap:3px}}
        </style></head><body><div class="card">
        <div style="font-size:12px;color:#8fa1b6;text-transform:uppercase;letter-spacing:.08em">The Brink World · Final Client Delivery Gate</div>
        <h1>${esc(facility.facility_name)}</h1>
        <div class="row"><span class="k">Report reference</span><span class="v">${esc(reportRun.report_ref)}</span></div>
        <div class="row"><span class="k">Organisation</span><span class="v">${esc(facility.organization_name)}</span></div>
        <div class="row"><span class="k">Client</span><span class="v">${esc(facility.contact_name)} · ${esc(facility.contact_email)}</span></div>
        <div class="row"><span class="k">Location</span><span class="v">${esc(facility.location_label)}</span></div>
        <div class="row"><span class="k">Product</span><span class="v">${esc(productLabels[reportRun.product_type] || reportRun.product_type)}</span></div>
        <div class="row"><span class="k">Cadence</span><span class="v">${esc(sub?.cadence || "—")}</span></div>
        <div class="row"><span class="k">Draft status</span><span class="v">${esc(reportRun.report_status)}</span></div>
        ${alreadyDelivered ? '<div class="ok"><strong>Already delivered.</strong> The client-delivery step has already completed.</div>' : ''}
        ${canDeliver ? `
          <div class="warn"><strong>Final approval.</strong><br>The PDF attached to your review email is the file that will be sent to the client. Confirm only after checking facility, location, profile, evidence and payment/scope.</div>
          <form method="post" action="/api/commercial/report-deliver">
            <input type="hidden" name="run" value="${esc(runId)}">
            <input type="hidden" name="token" value="${esc(token)}">
            <label style="display:block;margin:16px 0"><input type="checkbox" name="confirmed" value="yes" required> I reviewed the draft and approve this exact report for delivery to the client.</label>
            <button type="submit">APPROVE & SEND TO CLIENT</button>
          </form>` : (!alreadyDelivered ? `<div class="warn">This report is not currently eligible for client delivery. Status: ${esc(reportRun.report_status)}</div>` : '')}
        </div></body></html>`;

        return new Response(html, { headers: { "Content-Type": "text/html; charset=utf-8" } });
      } catch (err) {
        return new Response(`Report review failed: ${err.message}`, {
          status: 500,
          headers: { "Content-Type": "text/plain; charset=utf-8" }
        });
      }
    }

    if (url.pathname === "/api/commercial/report-deliver" && request.method === "POST") {
      let deliveryClaimed = false;
      let customerSent = false;
      let activeRunId = "";
      let activeSbUrl = "";
      let activeSbKey = "";
      try {
        const form = await request.formData();
        const runId = String(form.get("run") || "");
        activeRunId = runId;
        const token = String(form.get("token") || "");
        const confirmed = String(form.get("confirmed") || "") === "yes";
        if (!runId || !token || !confirmed) throw new Error("Report delivery confirmation is incomplete.");

        const sbUrl = env.SUPABASE_URL;
        const sbKey = env.SUPABASE_SERVICE_ROLE_KEY || env.SUPABASE_SERVICE_KEY;
        activeSbUrl = sbUrl || "";
        activeSbKey = sbKey || "";
        if (!sbUrl || !sbKey || !env.RESEND_API_KEY) {
          throw new Error("Commercial delivery environment is incomplete.");
        }

        const runUrl = new URL(`${sbUrl}/rest/v1/brink_report_runs`);
        runUrl.searchParams.set("id", `eq.${runId}`);
        runUrl.searchParams.set(
          "select",
          "id,facility_id,subscription_id,product_type,report_ref,report_status,evidence_summary,output_location"
        );
        runUrl.searchParams.set("limit", "1");
        const runLookup = await fetch(runUrl.toString(), { headers: sbHeaders(sbKey) });
        if (!runLookup.ok) throw new Error("Report-run lookup failed.");
        const reportRun = (await runLookup.json())[0];
        if (!reportRun) throw new Error("Report draft not found.");
        if (reportRun.report_status === "delivered") {
          return new Response("This report has already been delivered.", {
            status: 200,
            headers: { "Content-Type":"text/plain; charset=utf-8" }
          });
        }

        const summary = reportRun.evidence_summary || {};
        const suppliedHash = await sha256Hex(token);
        const expiresAt = summary.delivery_approval_expires_at
          ? new Date(summary.delivery_approval_expires_at).getTime()
          : 0;
        if (!summary.delivery_approval_hash || suppliedHash !== summary.delivery_approval_hash || expiresAt < Date.now()) {
          return new Response("This report-delivery approval is invalid or expired.", {
            status: 403,
            headers: { "Content-Type":"text/plain; charset=utf-8" }
          });
        }
        if (reportRun.report_status !== "awaiting_approval") {
          throw new Error(`Report status is ${reportRun.report_status}; expected awaiting_approval.`);
        }

        const fUrl = new URL(`${sbUrl}/rest/v1/brink_facilities`);
        fUrl.searchParams.set("id", `eq.${reportRun.facility_id}`);
        fUrl.searchParams.set("select", "*");
        fUrl.searchParams.set("limit", "1");
        const facilityRes = await fetch(fUrl.toString(), { headers: sbHeaders(sbKey) });
        const facility = facilityRes.ok ? (await facilityRes.json())[0] : null;
        const deliveryEmail = String(summary.client_email || facility?.contact_email || "");
        const deliveryName = String(summary.client_name || facility?.contact_name || "");
        if (!facility || !deliveryEmail.includes("@")) throw new Error("Client delivery address is unavailable.");

        const subUrl = new URL(`${sbUrl}/rest/v1/brink_monitoring_subscriptions`);
        subUrl.searchParams.set("id", `eq.${reportRun.subscription_id}`);
        subUrl.searchParams.set("select", "*");
        subUrl.searchParams.set("limit", "1");
        const subRes = await fetch(subUrl.toString(), { headers: sbHeaders(sbKey) });
        const sub = subRes.ok ? (await subRes.json())[0] : null;
        if (!sub) throw new Error("Monitoring subscription not found.");

        const bucket = String(summary.draft_bucket || "commercial-report-drafts");
        const objectPath = String(summary.draft_object_path || "");
        if (!objectPath) throw new Error("Stored draft PDF path is unavailable.");
        const encodedPath = objectPath.split("/").map(encodeURIComponent).join("/");
        const storageHeaders = {
          "apikey": sbKey,
          "Authorization": `Bearer ${sbKey}`
        };
        let pdfRes = await fetch(
          `${sbUrl}/storage/v1/object/authenticated/${encodeURIComponent(bucket)}/${encodedPath}`,
          { headers: storageHeaders }
        );
        if (!pdfRes.ok) {
          pdfRes = await fetch(
            `${sbUrl}/storage/v1/object/${encodeURIComponent(bucket)}/${encodedPath}`,
            { headers: storageHeaders }
          );
        }
        if (!pdfRes.ok) {
          throw new Error(`Stored draft PDF could not be retrieved (${pdfRes.status}).`);
        }
        const pdfBytes = new Uint8Array(await pdfRes.arrayBuffer());

        // Chunk-safe base64 conversion for Worker runtime.
        let binary = "";
        const chunkSize = 0x8000;
        for (let i = 0; i < pdfBytes.length; i += chunkSize) {
          binary += String.fromCharCode(...pdfBytes.subarray(i, i + chunkSize));
        }
        const pdfB64 = btoa(binary);

        if (summary.draft_sha256) {
          const digest = await crypto.subtle.digest("SHA-256", pdfBytes);
          const actualHash = Array.from(new Uint8Array(digest))
            .map(b => b.toString(16).padStart(2, "0"))
            .join("");
          if (actualHash !== String(summary.draft_sha256)) {
            throw new Error("Stored draft integrity check failed. Client delivery has been blocked.");
          }
        }

        // Claim only after all pre-send checks and draft-integrity verification pass.
        const claim = await fetch(
          `${sbUrl}/rest/v1/brink_report_runs?id=eq.${encodeURIComponent(runId)}&report_status=eq.awaiting_approval`,
          {
            method: "PATCH",
            headers: sbHeaders(sbKey, "return=representation"),
            body: JSON.stringify({ report_status: "delivering" })
          }
        );
        if (!claim.ok) throw new Error(`Could not claim report for delivery: ${await claim.text()}`);
        const claimed = await claim.json();
        if (!Array.isArray(claimed) || claimed.length === 0) {
          return new Response("This report is already being processed.", {
            status: 409,
            headers: { "Content-Type":"text/plain; charset=utf-8" }
          });
        }
        deliveryClaimed = true;



        const productLabels = {
          location_dossier: "Location Threat Dossier",
          facility_risk_passport: "Facility Risk Passport",
          physical_risk_evidence_pack: "Physical Risk Evidence Pack",
          pre_underwriting_site_intelligence: "Pre-Underwriting Site Intelligence",
          business_continuity_threat_register: "External Threat Register"
        };
        const productTitle = productLabels[reportRun.product_type] || "Facility Risk Report";
        const siteName = facility.facility_name || facility.location_label || "Monitored Facility";
        const sender = env.DOSSIER_FROM_EMAIL || "The Brink World <intel@thebrinkworld.com>";
        const attachment = {
          filename: `Dossier_${reportRun.report_ref || runId.slice(0,8)}.pdf`,
          content: pdfB64
        };
        const resendHeaders = {
          "Authorization": `Bearer ${env.RESEND_API_KEY}`,
          "Content-Type": "application/json"
        };

        const customerPayload = {
          from: sender,
          to: [deliveryEmail],
          reply_to: "thebrink2028@gmail.com",
          subject: `Your ${productTitle} — ${siteName} (${reportRun.report_ref})`,
          html: `
            <h3>The Brink World — ${productTitle}</h3>
            <p>Your approved facility risk report for <strong>${siteName}</strong> is attached.</p>
            <p><strong>Location:</strong> ${facility.location_label || "Not supplied"}</p>
            <p><strong>Reference:</strong> ${reportRun.report_ref}</p>
            <p>The evidence classes, confidence notes and reliance limits inside the dossier explain how each finding should be interpreted.</p>
          `,
          attachments: [attachment]
        };
        const customerMail = await fetch("https://api.resend.com/emails", {
          method: "POST",
          headers: resendHeaders,
          body: JSON.stringify(customerPayload)
        });
        if (!customerMail.ok) {
          await fetch(`${sbUrl}/rest/v1/brink_report_runs?id=eq.${encodeURIComponent(runId)}`, {
            method: "PATCH",
            headers: sbHeaders(sbKey, "return=minimal"),
            body: JSON.stringify({ report_status: "delivery_failed" })
          });
          throw new Error(`Client email failed (${customerMail.status}): ${await customerMail.text()}`);
        }
        customerSent = true;

        // Internal archive copy is best-effort after successful client delivery.
        await fetch("https://api.resend.com/emails", {
          method: "POST",
          headers: resendHeaders,
          body: JSON.stringify({
            from: sender,
            to: ["thebrink2028@gmail.com"],
            reply_to: deliveryEmail,
            subject: `[REPORT DELIVERED] ${productTitle} · ${siteName} · ${reportRun.report_ref}`,
            html: `
              <h3>The Brink World — Delivery Record</h3>
              <p><strong>Client:</strong> ${deliveryName || "—"} · ${deliveryEmail}</p>
              <p><strong>Facility:</strong> ${siteName}</p>
              <p><strong>Location:</strong> ${facility.location_label || "—"}</p>
              <p><strong>Reference:</strong> ${reportRun.report_ref}</p>
              <p>The exact PDF delivered to the client is attached.</p>
            `,
            attachments: [attachment]
          })
        }).catch(() => {});

        const deliveredAt = new Date();
        const addMonths = (date, months) => {
          const d = new Date(date.getTime());
          const originalDay = d.getUTCDate();
          d.setUTCDate(1);
          d.setUTCMonth(d.getUTCMonth() + months);
          const lastDay = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 0)).getUTCDate();
          d.setUTCDate(Math.min(originalDay, lastDay));
          return d;
        };
        let nextReportAt = null;
        const cadence = String(sub.cadence || "monthly").toLowerCase();
        if (cadence === "weekly") nextReportAt = new Date(deliveredAt.getTime() + 7 * 86400000);
        else if (cadence === "quarterly") nextReportAt = addMonths(deliveredAt, 3);
        else if (cadence === "annual") nextReportAt = addMonths(deliveredAt, 12);
        else if (cadence !== "one_off") nextReportAt = addMonths(deliveredAt, 1);

        await fetch(`${sbUrl}/rest/v1/brink_report_runs?id=eq.${encodeURIComponent(runId)}`, {
          method: "PATCH",
          headers: sbHeaders(sbKey, "return=minimal"),
          body: JSON.stringify({
            report_status: "delivered",
            completed_at: deliveredAt.toISOString()
          })
        });

        await fetch(`${sbUrl}/rest/v1/brink_monitoring_subscriptions?id=eq.${encodeURIComponent(sub.id)}`, {
          method: "PATCH",
          headers: sbHeaders(sbKey, "return=minimal"),
          body: JSON.stringify({
            status: cadence === "one_off" ? "completed" : "active",
            last_report_at: deliveredAt.toISOString(),
            next_report_at: nextReportAt ? nextReportAt.toISOString() : null
          })
        });

        return new Response(
          '<!doctype html><html><body style="font-family:Arial,sans-serif;background:#080b10;color:#eef2f7;padding:40px"><div style="max-width:650px;margin:auto"><h2>Report delivered.</h2><p>The approved PDF has now been sent to the client and the delivery record has been updated.</p></div></body></html>',
          { headers: { "Content-Type":"text/html; charset=utf-8" } }
        );
      } catch (err) {
        if (deliveryClaimed && !customerSent && activeRunId && activeSbUrl && activeSbKey) {
          try {
            await fetch(`${activeSbUrl}/rest/v1/brink_report_runs?id=eq.${encodeURIComponent(activeRunId)}&report_status=eq.delivering`, {
              method: "PATCH",
              headers: sbHeaders(activeSbKey, "return=minimal"),
              body: JSON.stringify({ report_status: "awaiting_approval" })
            });
          } catch (_) {}
        }
        return new Response(`Report delivery failed: ${err.message}`, {
          status: 500,
          headers: { "Content-Type":"text/plain; charset=utf-8" }
        });
      }
    }

    // 9. Server-Side Supabase Auth Proxy


    const sbUrl = env.SUPABASE_URL || "https://jxapuzsgyoetrpnmohct.supabase.co";
    const sbKey = env.SUPABASE_ANON_KEY || env.SUPABASE_SERVICE_ROLE_KEY;

    if (url.pathname === "/api/auth/signup" && request.method === "POST") {
      try {
        const body = await request.json();
        if (!sbKey) throw new Error("Supabase secrets missing from environment.");

        const sbRes = await fetch(`${sbUrl}/auth/v1/signup`, {
          method: "POST",
          headers: { "apikey": sbKey, "Content-Type": "application/json" },
          body: JSON.stringify({
            email: body.email,
            password: body.password,
            data: { full_name: body.full_name }
          })
        });

        const sbData = await sbRes.json();
        if (!sbRes.ok) throw new Error(sbData.msg || sbData.error_description || sbData.message || "Registration failed");

        return new Response(JSON.stringify({ ok: true, user: sbData.user }), {
          headers: { ...corsHeaders, "Content-Type": "application/json" }
        });
      } catch (err) {
        return new Response(JSON.stringify({ error: err.message }), {
          status: 400,
          headers: { ...corsHeaders, "Content-Type": "application/json" }
        });
      }
    }

    if (url.pathname === "/api/auth/login" && request.method === "POST") {
      try {
        const body = await request.json();
        if (!sbKey) throw new Error("Supabase secrets missing from environment.");

        const sbRes = await fetch(`${sbUrl}/auth/v1/token?grant_type=password`, {
          method: "POST",
          headers: { "apikey": sbKey, "Content-Type": "application/json" },
          body: JSON.stringify({
            email: body.email,
            password: body.password
          })
        });

        const sbData = await sbRes.json();
        if (!sbRes.ok) throw new Error(sbData.msg || sbData.error_description || sbData.message || "Invalid credentials");

        return new Response(JSON.stringify({
          ok: true,
          token: sbData.access_token,
          user: sbData.user
        }), {
          headers: { ...corsHeaders, "Content-Type": "application/json" }
        });
      } catch (err) {
        return new Response(JSON.stringify({ error: err.message }), {
          status: 400,
          headers: { ...corsHeaders, "Content-Type": "application/json" }
        });
      }
    }

    if (url.pathname === "/api/auth/verify" && request.method === "GET") {
      try {
        const authHeader = request.headers.get("Authorization");
        if (!authHeader || !sbKey) throw new Error("Unauthorized");

        const sbRes = await fetch(`${sbUrl}/auth/v1/user`, {
          headers: { "apikey": sbKey, "Authorization": authHeader }
        });

        if (!sbRes.ok) throw new Error("Session invalid");
        const userData = await sbRes.json();

        return new Response(JSON.stringify(userData), {
          headers: { ...corsHeaders, "Content-Type": "application/json" }
        });
      } catch (err) {
        return new Response(JSON.stringify({ error: err.message }), {
          status: 401,
          headers: { ...corsHeaders, "Content-Type": "application/json" }
        });
      }
    }

    // 9. Unknown API routes should always return JSON so browser clients
    // never fail with a cryptic JSON.parse error.
    if (url.pathname.startsWith("/api/")) {
      return new Response(JSON.stringify({
        error: "api_route_not_found",
        path: url.pathname,
        message: "This API route is not available on the currently deployed Worker."
      }), {
        status: 404,
        headers: { ...corsHeaders, "Content-Type": "application/json" }
      });
    }

    // 10. Root Gateway Status
    return new Response("The Brink World Gateway Active", { 
      status: 200, 
      headers: { ...corsHeaders, "Content-Type": "text/plain" } 
    });
  },

  async scheduled(controller, env, ctx) {
    ctx.waitUntil(
      triggerHazardIngestion(
        env,
        new Date(controller.scheduledTime || Date.now()).toISOString()
      ).catch(err => console.error("Scheduled hazard ingestion trigger failed:", err))
    );
  }
};