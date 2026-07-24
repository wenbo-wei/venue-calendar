(() => {
  const state = { view: "conference", query: "", rank: "all" };
  const els = Object.fromEntries(["cards", "search", "empty", "updated", "conference-count", "journal-count"].map(id => [id, document.getElementById(id)]));

  const escapeHtml = value => String(value ?? "").replace(/[&<>'"]/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[char]));
  const rankOf = venue => venue.rank?.ccf || venue.rank || "N";

  function parseDeadline(value, zone) {
    if (!value || value === "TBD") return null;
    const match = value.match(/^(\d{4})-(\d{2})-(\d{2}) (\d{2}):(\d{2}):(\d{2})$/);
    if (!match) return null;
    const [, y, m, d, hh, mm, ss] = match.map(Number);
    if (zone === "AoE") zone = "UTC-12";
    const utcMatch = zone.match(/^UTC([+-]\d{1,2})(?::?(\d{2}))?$/);
    if (utcMatch) {
      const signHours = Number(utcMatch[1]);
      const minutes = signHours * 60 + Math.sign(signHours) * Number(utcMatch[2] || 0);
      return new Date(Date.UTC(y, m - 1, d, hh, mm, ss) - minutes * 60000);
    }
    if (zone === "PT") return zonedToUtc(y, m, d, hh, mm, ss, "America/Los_Angeles");
    return new Date(Date.UTC(y, m - 1, d, hh, mm, ss));
  }

  function zonedToUtc(y, m, d, hh, mm, ss, timeZone) {
    let guess = Date.UTC(y, m - 1, d, hh, mm, ss);
    const format = new Intl.DateTimeFormat("en-CA", { timeZone, year:"numeric", month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit", second:"2-digit", hourCycle:"h23" });
    for (let i = 0; i < 2; i++) {
      const parts = Object.fromEntries(format.formatToParts(new Date(guess)).map(part => [part.type, Number(part.value)]));
      const shown = Date.UTC(parts.year, parts.month - 1, parts.day, parts.hour, parts.minute, parts.second);
      guess += Date.UTC(y, m - 1, d, hh, mm, ss) - shown;
    }
    return new Date(guess);
  }

  function flattenConferences() {
    const now = new Date();
    return window.AI_CONFERENCES.venues.flatMap(venue => {
      const editions = venue.confs?.length ? venue.confs : [{
        year: venue.next_year, id: `${venue.source_slug}-${venue.next_year}`, link: venue.latest_link,
        timeline: [{ deadline:"TBD", comment:"Next edition details have not been announced" }],
        timezone:"AoE", date:"TBD", place:"TBD", link_kind:"series",
        official_page_announced:false, place_status:"not_announced"
      }];
      return editions.map(conf => {
      const points = (conf.timeline || []).flatMap(item => [
        item.abstract_deadline ? { type:"Abstract", value:item.abstract_deadline, comment:item.comment } : null,
        item.deadline ? { type:"Paper", value:item.deadline, comment:item.comment } : null
      ].filter(Boolean)).map(point => ({ ...point, date: parseDeadline(point.value, conf.timezone) }));
      const valid = points.filter(point => point.date).sort((a, b) => a.date - b.date);
      const next = valid.find(point => point.date >= now) || valid.at(-1) || { type:"To be announced", value:"TBD", date:null };
      const past = next.date ? next.date < now : Number(conf.year) < now.getFullYear();
      return { ...venue, ...conf, venueDescription: venue.description, ccfRank: rankOf(venue), next, past };
      });
    });
  }

  const allJournals = window.AI_JOURNALS.venues;
  els["conference-count"].textContent = window.AI_CONFERENCES.venues.length;
  els["journal-count"].textContent = allJournals.filter(item => !item.inactive).length;
  els.updated.textContent = `Official sites checked: ${new Date(window.AI_CONFERENCES.updated_at).toLocaleString()}`;

  function reloadLocalSnapshot() {
    const script = document.createElement("script");
    script.src = `data/conferences.js?refresh=${Date.now()}`;
    script.onload = () => {
      els["conference-count"].textContent = window.AI_CONFERENCES.venues.length;
      els.updated.textContent = `Official sites checked: ${new Date(window.AI_CONFERENCES.updated_at).toLocaleString()}`;
      render();
      script.remove();
    };
    script.onerror = () => script.remove();
    document.head.appendChild(script);
  }

  function matches(item) {
    const haystack = `${item.title} ${item.name || item.venueDescription || item.description || ""} ${item.place || ""}`.toLowerCase();
    return !state.query || haystack.includes(state.query);
  }

  function matchesRank(item) {
    return state.rank === "all" || String(item.ccfRank || item.rank) === state.rank;
  }

  function countdown(date) {
    if (!date) return "Deadline not announced";
    const delta = date - new Date();
    if (delta <= 0) return "Closed";
    const totalSeconds = Math.floor(delta / 1000);
    const days = Math.floor(totalSeconds / 86400);
    const hours = Math.floor((totalSeconds % 86400) / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const seconds = totalSeconds % 60;
    const parts = [];
    if (days) parts.push(`${days}d`);
    if (days || hours) parts.push(`${hours}h`);
    if (days || hours || minutes) parts.push(`${minutes}m`);
    parts.push(`${seconds}s`);
    return parts.join(" ");
  }

  function updateCountdowns() {
    document.querySelectorAll(".countdown[data-deadline]").forEach(element => {
      element.textContent = countdown(new Date(element.dataset.deadline));
    });
  }

  function urgencyColor(date) {
    if (!date) return null;
    const days = Math.max(0, (date - new Date()) / 86400000);
    const stops = [
      { days: 0, color: [143, 29, 44] },
      { days: 3, color: [255, 0, 0] },
      { days: 7, color: [194, 88, 19] },
      { days: 14, color: [241, 105, 16] },
      { days: 30, color: [255, 212, 0] },
      { days: 38, color: [198, 255, 0] },
      { days: 45, color: [118, 255, 3] },
      { days: 60, color: [22, 143, 234] }
    ];
    const upperIndex = stops.findIndex(stop => days <= stop.days);
    if (upperIndex === 0) return stops[0];
    if (upperIndex === -1) return stops[stops.length - 1];
    const lower = stops[upperIndex - 1];
    const upper = stops[upperIndex];
    const progress = (days - lower.days) / (upper.days - lower.days);
    const channels = lower.color.map((value, index) => Math.round(value + (upper.color[index] - value) * progress));
    return { color: `rgb(${channels.join(" ")})` };
  }

  function conferenceCard(item) {
    const dateText = item.next.date ? item.next.date.toLocaleString([], { year:"numeric", month:"short", day:"numeric", hour:"2-digit", minute:"2-digit", timeZoneName:"short" }) : "TBD";
    const color = urgencyColor(item.next.date);
    const urgencyClass = color === null ? " no-deadline" : " has-deadline";
    const urgencyStyle = color === null ? "" : ` style="--urgency-color:${color.color}"`;
    const countdownData = item.next.date ? ` data-deadline="${item.next.date.toISOString()}"` : "";
    const hasLocation = item.place && item.place !== "TBD";
    const locationLabel = hasLocation ? item.place : (item.place_status === "not_detected" ? "Location not yet verified" : "Location not announced");
    const locationContent = `<svg class="location-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M20 10c0 5-8 12-8 12S4 15 4 10a8 8 0 1 1 16 0Z"></path><circle cx="12" cy="10" r="2.5"></circle></svg><span>${escapeHtml(locationLabel)}</span>`;
    const location = `<p class="location">${locationContent}</p>`;
    const officialHref = hasLocation && item.location_source_url ? item.location_source_url : item.link;
    const linkLabel = item.link_kind === "edition" ? "Official site ↗" : "Official series ↗";
    return `<article class="card ${item.past ? "inactive" : ""}${urgencyClass}"${urgencyStyle}>
      <div class="card-top"><div class="venue"><h2>${escapeHtml(item.title)} <small>${item.year}</small></h2><p>${escapeHtml(item.venueDescription)}</p>${location}</div></div>
      <div class="deadline"><small>${escapeHtml(item.next.type)} deadline · Local time</small><div class="countdown"${countdownData}>${countdown(item.next.date)}</div><time class="deadline-date"${item.next.date ? ` datetime="${item.next.date.toISOString()}"` : ""}>${escapeHtml(dateText)}</time></div>
      <div class="card-bottom"><span class="rank ${escapeHtml(item.ccfRank)}">CCF ${escapeHtml(item.ccfRank)}</span><div class="links"><a href="${escapeHtml(officialHref)}" target="_blank" rel="noreferrer">${linkLabel}</a></div></div>
    </article>`;
  }

  function journalCard(item) {
    return `<article class="card journal-card ${item.inactive ? "inactive" : ""}">
      <div class="card-top"><div class="venue"><h2>${escapeHtml(item.title)}</h2><p>${escapeHtml(item.name)}</p></div></div>
      <div class="deadline"><small>Submission cycle</small><strong>${item.inactive ? "Discontinued" : "Rolling submissions · No fixed deadline"}</strong></div>
      <div class="card-bottom"><span class="rank ${escapeHtml(item.rank)}">CCF ${escapeHtml(item.rank)}</span><div class="links"><a href="${escapeHtml(item.url)}" target="_blank" rel="noreferrer">Journal site ↗</a></div></div>
    </article>`;
  }

  function render() {
    let items;
    if (state.view === "conference") {
      items = flattenConferences().filter(item => !item.past && matches(item) && matchesRank(item)).sort((a, b) => {
        if (a.next.date && b.next.date) return a.next.date - b.next.date;
        if (a.next.date) return -1;
        if (b.next.date) return 1;
        const yearOrder = Number(a.year) - Number(b.year);
        return yearOrder || a.title.localeCompare(b.title);
      });
      els.cards.innerHTML = items.map((item, index) => {
        const startsUndatedGroup = !item.next.date && index > 0 && items[index - 1].next.date;
        const divider = startsUndatedGroup ? '<div class="deadline-divider" role="separator" aria-label="Deadlines not yet announced"></div>' : "";
        return divider + conferenceCard(item);
      }).join("");
    } else {
      items = allJournals.filter(item => matches(item) && matchesRank(item) && !item.inactive);
      els.cards.innerHTML = items.map(journalCard).join("");
    }
    els.empty.classList.toggle("hidden", items.length > 0);
  }

  document.querySelectorAll(".tab").forEach(tab => tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(item => item.classList.remove("active"));
    tab.classList.add("active"); state.view = tab.dataset.view; render();
  }));
  document.querySelectorAll(".rank-option").forEach(option => option.addEventListener("click", () => {
    document.querySelectorAll(".rank-option").forEach(item => {
      const active = item === option;
      item.classList.toggle("active", active);
      item.setAttribute("aria-pressed", String(active));
    });
    state.rank = option.dataset.rank;
    render();
  }));
  els.search.addEventListener("input", event => { state.query = event.target.value.trim().toLowerCase(); render(); });
  requestAnimationFrame(render);
  window.addEventListener("pageshow", render);
  setInterval(updateCountdowns, 1000);
  setInterval(render, 60000);
  setInterval(reloadLocalSnapshot, 21600000);
})();
