/**
 * Global Multi-Basin Storm & Cyclone Feed
 * Uses open public mirrors directly to bypass worker routing errors.
 */
window.STORM_FEED_REGISTRY = [
  // 1. NOAA NHC (Atlantic & Eastern/Central Pacific)
  {
    id: "NOAA_NHC",
    name: "NOAA National Hurricane Center",
    enabled: true,
    fetch: async () => {
      try {
        const res = await fetch("https://api.allorigins.win/raw?url=" + encodeURIComponent("https://www.nhc.noaa.gov/CurrentStorms.json"));
        if (!res.ok) return [];
        const text = await res.text();
        if (!text.includes("activeStorms")) return [];
        const data = JSON.parse(text);
        const storms = [];

        for (const s of data.activeStorms || []) {
          if (s.latitude && s.longitude) {
            const windKt = parseInt(s.intensity, 10) || 0;
            let catStr = "TROPICAL STORM";
            if (windKt >= 137) catStr = "CATEGORY 5 HURRICANE";
            else if (windKt >= 113) catStr = "CATEGORY 4 HURRICANE";
            else if (windKt >= 96) catStr = "CATEGORY 3 HURRICANE";
            else if (windKt >= 83) catStr = "CATEGORY 2 HURRICANE";
            else if (windKt >= 64) catStr = "CATEGORY 1 HURRICANE";
            else if (windKt < 34) catStr = "TROPICAL DEPRESSION";

            storms.push({
              id: `NOAA-${s.id || s.name}`,
              name: (s.name || "UNNAMED").toUpperCase(),
              title: `${(s.name || "UNNAMED").toUpperCase()} · ${catStr}`,
              category: catStr,
              severity: windKt >= 96 ? "EXTREME DANGER" : "GALE / STORM",
              lat: parseFloat(s.latitude),
              lon: parseFloat(s.longitude),
              windSpeed: `${windKt} knots`,
              barometricPressure: s.pressure ? `${s.pressure} hPa` : "Live Monitored",
              time: s.lastUpdate || new Date().toISOString(),
              summary: `NOAA NHC active advisory for ${s.name}: Max sustained winds ${windKt} kt (${s.intensityMPH || Math.round(windKt * 1.15)} mph). Central pressure: ${s.pressure ? s.pressure + ' mb' : 'N/A'}.`
            });
          }
        }
        return storms;
      } catch (err) {
        console.warn("NOAA NHC fetch failed:", err);
        return [];
      }
    }
  },

  // 2. GDACS Global (Worldwide Multi-Basin)
  {
    id: "GDACS_GLOBAL",
    name: "GDACS Global Multi-Basin",
    enabled: true,
    fetch: async () => {
      try {
        const res = await fetch("https://api.allorigins.win/raw?url=" + encodeURIComponent("https://www.gdacs.org/datareport/resources/TC/events.geojson"));
        if (!res.ok) return [];
        const text = await res.text();
        if (!text.includes("features")) return [];
        const data = JSON.parse(text);
        const storms = [];

        for (const f of data.features || []) {
          const p = f.properties || {};
          const coords = f.geometry?.coordinates || [];
          if (coords.length >= 2) {
            const windKt = Number(p.wind_speed_kts) || Math.round((Number(p.wind_speed_kmh) || 60) / 1.852);
            const stormName = (p.name || 'CYCLONE').toUpperCase();
            
            let catStr = "TROPICAL CYCLONE";
            if (windKt >= 137) catStr = "CAT 5 SUPER TYPHOON";
            else if (windKt >= 113) catStr = "CAT 4 SEVERE CYCLONE";
            else if (windKt >= 96) catStr = "CAT 3 MAJOR CYCLONE";
            else if (windKt >= 64) catStr = "CAT 1-2 CYCLONE";
            else if (windKt < 34) catStr = "TROPICAL DEPRESSION";

            storms.push({
              id: `GDACS-${p.eventid || stormName}`,
              name: stormName,
              title: `${stormName} · ${catStr}`,
              category: catStr,
              severity: p.alertlevel === 'Red' ? "CRITICAL HAZARD" : "CYCLONIC WATCH",
              lat: coords[1],
              lon: coords[0],
              windSpeed: `${windKt} knots`,
              barometricPressure: p.pressure ? `${p.pressure} hPa` : "Live Monitored",
              time: p.fromdate || new Date().toISOString(),
              summary: `GDACS Multi-Basin Alert for ${stormName}. Alert Level: ${p.alertlevel || 'Active'}. Peak winds: ${windKt} kt.`
            });
          }
        }
        return storms;
      } catch (err) {
        console.warn("GDACS fetch failed:", err);
        return [];
      }
    }
  }
];

window.fetchGlobalStorms = async function() {
  const feeds = (window.STORM_FEED_REGISTRY || []).filter(f => f.enabled);
  const results = await Promise.allSettled(feeds.map(f => f.fetch()));
  
  const rawList = [];
  results.forEach(r => {
    if (r.status === "fulfilled" && Array.isArray(r.value)) {
      rawList.push(...r.value);
    }
  });

  const deduped = [];
  const seen = new Set();
  for (const s of rawList) {
    const key = s.name.trim().toUpperCase();
    if (!seen.has(key)) {
      seen.add(key);
      deduped.push(s);
    }
  }
  return deduped;
};