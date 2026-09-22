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

    // 1. Live Volcano Telemetry Proxy (Bypasses Browser CORS)
    if (url.pathname === "/api/volcanoes" && request.method === "GET") {
      try {
        const res = await fetch("https://volcanoes.usgs.gov/vsc/api/volcanoApi/vhpstatus", {
          headers: { "User-Agent": "TheBrinkEngine/1.0" }
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

    // 2. Lead Intake & Service Requests (Resend Email Dispatch)
    if (url.pathname === "/api/inquire" && request.method === "POST") {
      try {
        const data = await request.json();
        
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

    // 3. Server-Side Supabase Auth Proxy
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

    // 4. Root Edge Health Status
    return new Response("The Brink World Gateway Active", { 
      status: 200, 
      headers: { ...corsHeaders, "Content-Type": "text/plain" } 
    });
  }
};