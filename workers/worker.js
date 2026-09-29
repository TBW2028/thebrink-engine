
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
      countryCode: body.country_code || null
    };
  }

  const query = String(body.location || "").trim();
  if (!query) throw new Error("Please enter a location.");

  const u = new URL("https://geocoding-api.open-meteo.com/v1/search");
  u.searchParams.set("name", query);
  u.searchParams.set("count", "8");
  u.searchParams.set("language", "en");
  u.searchParams.set("format", "json");

  const res = await fetch(u.toString());
  if (!res.ok) throw new Error("Location lookup is temporarily unavailable.");
  const data = await res.json();
  const rows = data.results || [];
  if (!rows.length) throw new Error("We could not confidently locate that place. Try city, state/province and country.");

  const first = rows[0];
  return {
    label: [first.name, first.admin1, first.country].filter(Boolean).join(", "),
    lat: Number(first.latitude),
    lon: Number(first.longitude),
    country: first.country || null,
    countryCode: first.country_code || null
  };
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
        const sbKey = env.SUPABASE_SERVICE_ROLE_KEY;
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
        const sbKey = env.SUPABASE_SERVICE_ROLE_KEY;
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

    // 8. Server-Side Supabase Auth Proxy

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

    // 8. Root Gateway Status
    return new Response("The Brink World Gateway Active", { 
      status: 200, 
      headers: { ...corsHeaders, "Content-Type": "text/plain" } 
    });
  }
};