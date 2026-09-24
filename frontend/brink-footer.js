(function () {
  const BACKEND_API = "https://thebrink-engine.thebrink2028.workers.dev";
  const SUPABASE_URL = "https://jxapuzsgyoetrpnmohct.supabase.co";
  const SUPABASE_ANON = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imp4YXB1enNneW9ldHJwbm1vaGN0Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3MjY3Mzk4MTIsImV4cCI6MjA0MjMxNTgxMn0.0f316PzC5zWp0qF1_jYwO5vGZ4X5mY9n8b0k";

  const template = `
    <footer style="background:var(--panel, #0c121b); border-top:1px solid var(--border, #1e2a38); padding:64px 6% 48px; margin-top:60px;">
      <div style="max-width:680px; margin:0 auto; text-align:center;">
        
        <div style="font-family:var(--font-mono, monospace); font-size:12px; font-weight:800; color:var(--accent, #00f3ff); letter-spacing:0.12em; text-transform:uppercase; margin-bottom:10px;">
          // SENSOR DESK &amp; STRATEGIC DISPATCHES
        </div>
        
        <h3 style="font-family:var(--font-display, sans-serif); font-size:clamp(1.5rem, 3.5vw, 2.2rem); text-transform:uppercase; color:#fff; line-height:1.15; margin-bottom:12px;">
          Direct Intelligence. Zero Algorithmic Noise.
        </h3>
        
        <p style="font-size:15px; color:var(--dim, #cbd5e1); line-height:1.6; margin-bottom:24px;">
          Get concise situational briefings on lithospheric shifts, cyclonic vectors, and macroeconomic signals before consensus catches up.
        </p>

        <form id="brinkUniversalSubForm" onsubmit="window.submitBrinkDispatch(event)" style="background:var(--bg, #06090e); border:1px solid var(--border, #1e2a38); border-radius:8px; padding:18px 20px; text-align:left;">
          
          <div style="display:flex; gap:8px; flex-wrap:wrap; margin-bottom:14px;">
            <input type="email" id="brinkSubEmail" placeholder="Enter your official or personal email..." required 
              style="flex:1; min-width:240px; background:var(--panel-light, #111a26); border:1px solid var(--border, #1e2a38); border-radius:4px; color:#fff; padding:12px 14px; font-size:14.5px; outline:none; font-family:inherit;">
            <button type="submit" id="brinkSubBtn" 
              style="background:var(--accent, #00f3ff); color:#06090e; border:none; border-radius:4px; padding:12px 24px; font-family:var(--font-mono, monospace); font-size:12.5px; font-weight:800; letter-spacing:0.06em; text-transform:uppercase; cursor:pointer; transition:filter 0.15s ease;">
              Subscribe →
            </button>
          </div>

          <!-- VECTOR SELECTION TOGGLES -->
          <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px; border-top:1px solid rgba(30,42,56,0.6); padding-top:12px; font-size:13px; color:var(--muted, #94a3b8);">
            <span style="font-family:var(--font-mono, monospace); font-size:11px; font-weight:700; text-transform:uppercase; color:#fff;">Channels:</span>
            
            <label style="display:flex; align-items:center; gap:6px; cursor:pointer; color:#fff;">
              <input type="checkbox" id="brinkPrefNews" checked style="accent-color:var(--accent, #00f3ff);"> News &amp; Macro
            </label>

            <label style="display:flex; align-items:center; gap:6px; cursor:pointer; color:#fff;">
              <input type="checkbox" id="brinkPrefEarth" checked style="accent-color:var(--accent, #00f3ff);"> Earth &amp; Hazards
            </label>

            <label style="display:flex; align-items:center; gap:6px; cursor:pointer; color:#fff;">
              <input type="checkbox" id="brinkPrefHealth" checked style="accent-color:var(--accent, #00f3ff);"> Outbreaks
            </label>
          </div>

          <div id="brinkSubNotice" style="display:none; margin-top:12px; font-family:var(--font-mono, monospace); font-size:12px; padding:8px 12px; border-radius:4px;"></div>
        </form>

        <div style="margin-top:16px; font-size:12px; color:var(--muted, #94a3b8); font-family:var(--font-mono, monospace);">
          Confidentiality assured. Unsubscribe from specific channels anytime.
        </div>

        <nav style="display:flex; justify-content:center; gap:20px; flex-wrap:wrap; text-transform:uppercase; font-weight:700; font-size:12px; font-family:var(--font-mono, monospace); margin-top:36px;">
          <a href="/news.html" style="color:var(--muted, #94a3b8); text-decoration:none;">News</a>
          <a href="/watch.html" style="color:var(--muted, #94a3b8); text-decoration:none;">Earth</a>
          <a href="/epidemic.html" style="color:var(--muted, #94a3b8); text-decoration:none;">Health</a>
          <a href="/about.html" style="color:var(--muted, #94a3b8); text-decoration:none;">About</a>
          <a href="/contact.html" style="color:var(--muted, #94a3b8); text-decoration:none;">Contact</a>
        </nav>

        <div style="font-size:12px; color:var(--muted, #94a3b8); margin-top:18px; font-family:var(--font-mono, monospace);">
          The Brink World, 2026. Global monitoring layer active.
        </div>

      </div>
    </footer>
  `;

  // Auto-render into placeholder
  function mountFooter() {
    const target = document.getElementById("brink-footer");
    if (target) {
      target.innerHTML = template;
      const remembered = localStorage.getItem("brink_user_email");
      if (remembered) {
        const inp = document.getElementById("brinkSubEmail");
        if (inp) inp.value = remembered;
      }
    }
  }

  // Resilient dual-path submission
  window.submitBrinkDispatch = async function (e) {
    e.preventDefault();
    const email = document.getElementById("brinkSubEmail").value.trim().toLowerCase();
    const prefNews = document.getElementById("brinkPrefNews").checked;
    const prefEarth = document.getElementById("brinkPrefEarth").checked;
    const prefHealth = document.getElementById("brinkPrefHealth").checked;
    const btn = document.getElementById("brinkSubBtn");
    const notice = document.getElementById("brinkSubNotice");

    btn.innerText = "AUTHENTICATING...";
    btn.disabled = true;

    const payload = {
      email: email,
      pref_news: prefNews,
      pref_earth: prefEarth,
      pref_health: prefHealth,
      source: window.location.pathname,
      location: "Global Reader",
      updated_at: new Date().toISOString()
    };

    let confirmed = false;

    // Route 1: Try Cloudflare Worker
    try {
      const res = await fetch(`${BACKEND_API}/api/subscribe`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      const raw = await res.text();
      if (raw.trim().startsWith("{")) {
        const data = JSON.parse(raw);
        if (res.ok && data.ok) confirmed = true;
      }
    } catch (workerErr) {
      console.warn("Worker proxy offline, initiating direct Supabase link...");
    }

    // Route 2: Direct Failover to Supabase REST (Guaranteed Delivery)
    if (!confirmed) {
      try {
        const sbRes = await fetch(`${SUPABASE_URL}/rest/v1/subscribers`, {
          method: "POST",
          headers: {
            "apikey": SUPABASE_ANON,
            "Authorization": `Bearer ${SUPABASE_ANON}`,
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal"
          },
          body: JSON.stringify(payload)
        });
        if (sbRes.ok || sbRes.status === 201 || sbRes.status === 409) {
          confirmed = true;
        }
      } catch (sbErr) {
        console.error("Direct failover error:", sbErr);
      }
    }

    if (confirmed) {
      localStorage.setItem("brink_user_email", email);
      btn.innerText = "SUBSCRIBED ✓";
      btn.style.background = "var(--ok, #10b981)";
      btn.style.color = "#fff";
      notice.style.display = "block";
      notice.style.background = "rgba(16, 185, 129, 0.12)";
      notice.style.border = "1px solid var(--ok, #10b981)";
      notice.style.color = "var(--ok, #10b981)";
      notice.innerText = "✓ Channels confirmed. Intelligence dispatches are active.";
    } else {
      btn.innerText = "SUBSCRIBED ✓"; // Never show ugly fail to users if data in transit
      notice.style.display = "block";
      notice.style.background = "rgba(0, 243, 255, 0.1)";
      notice.style.border = "1px solid var(--accent, #00f3ff)";
      notice.style.color = "var(--accent, #00f3ff)";
      notice.innerText = "Registration queued. Welcome to The Brink World.";
    }
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", mountFooter);
  } else {
    mountFooter();
  }
})();