// ==========================================
// THE BRINK WORLD // LIVE TERMINAL & SUBSCRIPTION ENGINE
// ==========================================

const CONFIG = {
  endpoint: typeof scriptURL !== 'undefined' ? scriptURL : '',
  geoIpService: 'https://ipapi.co/json/',
  timeoutMs: 2500,
  animationDuration: 3500
};

/**
 * Handles terminal subscription actions from multiple UI contexts.
 * @param {Event} e - The DOM click event.
 * @param {string} contextSource - The origin component identifier.
 */
function handleTerminalSubscribe(e, contextSource) {
  if (e && typeof e.preventDefault === 'function') {
    e.preventDefault();
  }

  const isMainDashboard = contextSource === 'Index Main Dashboard';
  const inputId = isMainDashboard ? 'index-terminal-input' : 'footer-sub-value';
  const btnId = isMainDashboard ? 'index-terminal-btn' : 'footer-sub-btn';
  
  const inputEl = document.getElementById(inputId);
  const val = inputEl ? inputEl.value.trim() : '';
  const btn = document.getElementById(btnId);

  if (!val) {
    highlightInputError(inputEl);
    return;
  }

  setButtonState(btn, "SAVING...", true);

  resolveUserLocation()
    .then(location => {
      dispatchSubscriptionPayload(val, contextSource, location, btn, inputEl);
    })
    .catch(() => {
      dispatchSubscriptionPayload(val, contextSource, "Global User", btn, inputEl);
    });
}

/**
 * Resolves user geographic location via GeoIP with a hard abort timeout.
 * @returns {Promise<string>} Formatted location string or fallback.
 */
function resolveUserLocation() {
  return new Promise((resolve) => {
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), CONFIG.timeoutMs);

    fetch(CONFIG.geoIpService, { signal: controller.signal })
      .then(res => {
        clearTimeout(timeoutId);
        if (!res.ok) throw new Error('Network response failed');
        return res.json();
      })
      .then(loc => {
        if (loc && loc.city && loc.country_name) {
          resolve(`${loc.city}, ${loc.country_name}`);
        } else {
          resolve("Global User");
        }
      })
      .catch(() => {
        resolve("Global User");
      });
  });
}

/**
 * Dispatches the subscription payload via URLSearchParams to the backend.
 */
function dispatchSubscriptionPayload(contact, sourceContext, location, btn, inputEl) {
  if (!CONFIG.endpoint) {
    console.error("Critical Error: scriptURL endpoint is undefined.");
    handleSubmissionError(btn, "CONFIG ERROR");
    return;
  }

  const formData = new URLSearchParams();
  formData.append('email', contact);
  formData.append('source', `${sourceContext} (${location})`);

  fetch(CONFIG.endpoint, { 
    method: 'POST', 
    body: formData 
  })
    .then(res => {
      if (!res.ok) throw new Error('Submission server returned an error');
      handleSubmissionSuccess(btn, inputEl);
    })
    .catch(() => {
      handleSubmissionError(btn, "ERROR — TRY AGAIN");
    });
}

/**
 * Updates button state to success configuration and sets a reset timer.
 */
function handleSubmissionSuccess(btn, inputEl) {
  if (btn) {
    btn.innerText = "SUBSCRIBED ✓";
    btn.style.background = "var(--accent-green, #10B981)";
    btn.style.color = "#ffffff";
  }
  if (inputEl) {
    inputEl.value = "";
  }

  setTimeout(() => {
    if (btn) {
      const isFooter = btn.id && btn.id.includes('footer');
      btn.innerText = isFooter ? "Keep Me Updated" : "GET BRIEFS →";
      btn.disabled = false;
      btn.style.background = "";
      btn.style.color = "";
    }
  }, CONFIG.animationDuration);
}

/**
 * Handles submission errors and restores UI interaction.
 */
function handleSubmissionError(btn, errorMessage) {
  if (btn) {
    btn.innerText = errorMessage;
    btn.disabled = false;
    btn.style.background = "var(--accent-red, #EF4444)";
    btn.style.color = "#ffffff";
  }
  setTimeout(() => {
    if (btn) {
      btn.style.background = "";
      btn.style.color = "";
    }
  }, 3000);
}

/**
 * Briefly highlights input field on validation failure.
 */
function highlightInputError(inputEl) {
  if (!inputEl) return;
  inputEl.style.borderColor = "var(--accent-red, #EF4444)";
  inputEl.focus();
  setTimeout(() => {
    inputEl.style.borderColor = "";
  }, 1500);
}

/**
 * Helper to safely modify button text and disabled state.
 */
function setButtonState(btn, text, isDisabled) {
  if (!btn) return;
  btn.innerText = text;
  btn.disabled = isDisabled;
}

// Enable pressing 'Enter' key inside terminal inputs to submit automatically
document.addEventListener('DOMContentLoaded', () => {
  ['index-terminal-input', 'footer-sub-value'].forEach(id => {
    const el = document.getElementById(id);
    if (el) {
      el.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          const source = id === 'index-terminal-input' ? 'Index Main Dashboard' : 'Footer';
          handleTerminalSubscribe(e, source);
        }
      });
    }
  });
});