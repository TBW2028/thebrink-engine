export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    // 1. Investor & Advertiser Inquiry Intake
    if (url.pathname === "/api/inquire" && request.method === "POST") {
      try {
        const data = await request.json();
        
        // Dispatch instant alert to executive team via Resend
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
              subject: `[INQUIRY DESK] ${data.intent}: ${data.name}`,
              html: `
                <h3>New Executive Ingestion Received</h3>
                <p><strong>Desk:</strong> ${data.intent}</p>
                <p><strong>Name:</strong> ${data.name}</p>
                <p><strong>Email:</strong> ${data.email}</p>
                <p><strong>Thesis / Message:</strong></p>
                <blockquote style="background:#f4f4f4;padding:12px;">${data.notes}</blockquote>
              `
            })
          });
        }

        return new Response(JSON.stringify({ ok: true }), {
          headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" }
        });
      } catch (err) {
        return new Response(JSON.stringify({ error: err.message }), { status: 500 });
      }
    }

    // 2. Razorpay Order Creation
    if (url.pathname === "/api/create-razorpay-order" && request.method === "POST") {
      try {
        const body = await request.json();
        const amount = (body.amount_inr || 4900) * 100;

        const auth = btoa(`${env.RAZORPAY_KEY_ID}:${env.RAZORPAY_KEY_SECRET}`);
        const rzRes = await fetch("https://api.razorpay.com/v1/orders", {
          method: "POST",
          headers: {
            "Authorization": `Basic ${auth}`,
            "Content-Type": "application/json"
          },
          body: JSON.stringify({
            amount: amount,
            currency: "INR",
            receipt: `rcpt_${Date.now()}`
          })
        });

        const rzOrder = await rzRes.json();
        return new Response(JSON.stringify(rzOrder), {
          headers: { "Content-Type": "application/json", "Access-Control-Allow-Origin": "*" }
        });
      } catch (err) {
        return new Response(JSON.stringify({ error: err.message }), { status: 500 });
      }
    }

    // 3. Razorpay Payment Webhook
    if (url.pathname === "/api/razorpay-webhook" && request.method === "POST") {
      const payloadText = await request.text();
      const signature = request.headers.get("x-razorpay-signature");

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
              location: notes.location,
              site_name: notes.site_name,
              customer_email: payment.email,
              answers: {
                occupancy: notes.occupancy || "warehouse",
                headcount: notes.headcount || "6-25",
                tolerance: notes.tolerance || "1-2d",
                value_band: notes.value_band || "skip",
                customer_name: notes.customer_name || payment.email
              }
            }
          })
        });

        return new Response(JSON.stringify({ ok: true }), { status: 200 });
      }

      return new Response("Event skipped", { status: 200 });
    }

    return new Response("The Brink World Edge Gateway Active", { status: 200 });
  }
};

function hexToUint8(hexString) {
  if (!hexString) return new Uint8Array();
  return new Uint8Array(hexString.match(/.{1,2}/g).map(byte => parseInt(byte, 16)));
}
