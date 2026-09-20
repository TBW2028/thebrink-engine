export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // Standard CORS headers for Cloudflare Pages frontend
    const corsHeaders = {
      "Access-Control-Allow-Origin": "*",
      "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type, Authorization, x-razorpay-signature",
      "Access-Control-Max-Age": "86400"
    };

    // 0. Handle CORS preflight
    if (request.method === "OPTIONS") {
      return new Response(null, { headers: corsHeaders });
    }

    // 1. Investor & Contact Inquiry Intake
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
              subject: `[INQUIRY DESK] ${data.intent || 'General'}: ${data.name || 'Anonymous'}`,
              html: `
                <h3>New Platform Telemetry / Lead Intake</h3>
                <p><strong>Intent:</strong> ${data.intent || 'None'}</p>
                <p><strong>Name:</strong> ${data.name || 'None'}</p>
                <p><strong>Email:</strong> ${data.email || 'None'}</p>
                <p><strong>Facility / Location:</strong> ${data.location || data.asset_name || data.route || 'N/A'}</p>
                <p><strong>Scope / Notes:</strong></p>
                <blockquote style="background:#f4f4f4;padding:12px;">${data.scope || data.notes || data.reason || 'None provided'}</blockquote>
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

    // 2. Razorpay Order Creation (Supports both /api/create-order and /api/create-razorpay-order)
    if ((url.pathname === "/api/create-order" || url.pathname === "/api/create-razorpay-order") && request.method === "POST") {
      try {
        const body = await request.json();

        // Calculate amount in smallest currency unit (paise or cents)
        const rawAmount = body.amount || body.amount_inr || 3999;
        const currency = (body.currency || "INR").toUpperCase();
        const amount = Math.round(rawAmount * 100);

        const auth = btoa(`${env.RAZORPAY_KEY_ID}:${env.RAZORPAY_KEY_SECRET}`);
        
        // Pass location and site details in 'notes' so Razorpay preserves them on payment.captured
        const rzRes = await fetch("https://api.razorpay.com/v1/orders", {
          method: "POST",
          headers: {
            "Authorization": `Basic ${auth}`,
            "Content-Type": "application/json"
          },
          body: JSON.stringify({
            amount: amount,
            currency: currency,
            receipt: body.receipt || `rcpt_${Date.now()}`,
            notes: {
              location: body.facility || body.location || "18.5204, 73.8567",
              site_name: body.facility || body.site_name || "Industrial Facility",
              customer_name: body.customer_name || "Lead Officer",
              customer_email: body.customer_email || "thebrink2028@gmail.com"
            }
          })
        });

        const rzOrder = await rzRes.json();
        return new Response(JSON.stringify(rzOrder), {
          headers: { ...corsHeaders, "Content-Type": "application/json" }
        });
      } catch (err) {
        return new Response(JSON.stringify({ error: err.message }), { 
          status: 500, 
          headers: { ...corsHeaders, "Content-Type": "application/json" } 
        });
      }
    }

    // 3. Razorpay Payment Webhook
    if (url.pathname === "/api/razorpay-webhook" && request.method === "POST") {
      const payloadText = await request.text();
      const signature = request.headers.get("x-razorpay-signature");

      if (!env.RAZORPAY_WEBHOOK_SECRET) {
        return new Response("Webhook secret not configured", { status: 500 });
      }

      const encoder = new TextEncoder();
      const key = await crypto.subtle.importKey(
        "raw",
        encoder.encode(env.RAZORPAY_WEBHOOK_SECRET),
        { name: "HMAC", hash: "SHA-256" },
        false,
        ["verify"]
      );

      const verified = await crypto.subtle.verify(
        "HMAC",
        key,
        hexToUint8(signature),
        encoder.encode(payloadText)
      );

      if (!verified) {
        return new Response("Invalid signature", { status: 400 });
      }

      const event = JSON.parse(payloadText);
      if (event.event === "payment.captured") {
        const payment = event.payload.payment.entity;
        const notes = payment.notes || {};

        // Dispatch job to GitHub Actions Python Runner
        if (env.GITHUB_PAT && env.GITHUB_REPO) {
          await fetch(`https://api.github.com/repos/${env.GITHUB_REPO}/dispatches`, {
            method: "POST",
            headers: {
              "Authorization": `Bearer ${env.GITHUB_PAT}`,
              "Accept": "application/vnd.github+json",
              "User-Agent": "TheBrink-Cloudflare-Worker"
            },
            body: JSON.stringify({
              event_type: "order_paid",
              client_payload: {
                location: notes.location || "18.5204, 73.8567",
                site_name: notes.site_name || "Industrial Facility",
                customer_email: payment.email || notes.customer_email || "thebrink2028@gmail.com",
                answers: {
                  occupancy: notes.occupancy || "warehouse",
                  customer_name: notes.customer_name || payment.email || "Lead Officer"
                }
              }
            })
          });
        }

        return new Response(JSON.stringify({ ok: true }), { 
          status: 200, 
          headers: { ...corsHeaders, "Content-Type": "application/json" } 
        });
      }

      return new Response("Event skipped", { status: 200, headers: corsHeaders });
    }

    // 4. Default Root Health Check
    return new Response("The Brink World Edge Gateway Active", { 
      status: 200, 
      headers: { ...corsHeaders, "Content-Type": "text/plain" } 
    });
  }
};

function hexToUint8(hexString) {
  if (!hexString) return new Uint8Array();
  return new Uint8Array(hexString.match(/.{1,2}/g).map(byte => parseInt(byte, 16)));
}