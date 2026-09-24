const BACKEND_API = "https://thebrink-engine.thebrink2028.workers.dev";

async function handleUniversalSubmit(e) {
  e.preventDefault();
  const email = document.getElementById("identityEmail").value.trim();
  const prefNews = document.getElementById("subNews").checked;
  const prefEarth = document.getElementById("subEarth").checked;
  const prefHealth = document.getElementById("subHealth").checked;
  const btn = document.getElementById("identitySubmitBtn");
  const fb = document.getElementById("identityFeedback");

  btn.innerText = "UPDATING...";
  btn.disabled = true;

  try {
    const res = await fetch(`${BACKEND_API}/api/subscribe`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        email: email,
        pref_news: prefNews,
        pref_earth: prefEarth,
        pref_health: prefHealth,
        source: window.location.pathname
      })
    });

    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Update failed");

    localStorage.setItem("brink_user_email", email);
    fb.style.display = "block";
    fb.style.color = "var(--emerald)";
    fb.innerText = "Preferences saved. Dispatches updated.";

    setTimeout(() => {
      closeModal("modalUniversalIdentity");
      updateNavIdentityState();
    }, 1200);
  } catch (err) {
    fb.style.display = "block";
    fb.style.color = "var(--accent-red)";
    fb.innerText = err.message;
    btn.disabled = false;
    btn.innerText = "TRY AGAIN";
  }
}

async function handleUnsubscribeAll(e) {
  e.preventDefault();
  const email = document.getElementById("identityEmail").value.trim();
  if (!email) {
    alert("Please enter your email above to unsubscribe.");
    return;
  }

  if (confirm(`Unsubscribe ${email} from all automated dispatches?`)) {
    await fetch(`${BACKEND_API}/api/subscribe`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email: email, unsubscribe_all: true })
    });
    alert("You have been unsubscribed from automated dispatches. Your records remain safe in our registry.");
    closeModal("modalUniversalIdentity");
  }
}

function updateNavIdentityState() {
  const email = localStorage.getItem("brink_user_email");
  const authNav = document.getElementById("authNavButtons");
  if (authNav && email) {
    authNav.innerHTML = `
      <span style="font-family:var(--font-mono); font-size:12px; color:var(--emerald);">👤 ${email.split('@')[0]}</span>
      <button type="button" class="btn" style="padding:4px 9px; font-size:12px;" onclick="openModal('modalUniversalIdentity')">Preferences</button>
    `;
  }
}

window.addEventListener("DOMContentLoaded", updateNavIdentityState);