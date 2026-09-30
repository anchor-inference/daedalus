// The one script the three launcher pages share. What it does depends on which page it is on —
// the body says — and everything it writes comes out of the same message table the page was
// rendered from, so a line the browser writes is in the language of the lines around it.
//
// The pages poll rather than hold a socket open: the launcher is a small process that may be in
// the middle of a build, and a poll that misses one answer costs nothing.

const el = (id) => document.getElementById(id);
const page = document.body.dataset.page;

// The launcher answers an action only when this token comes back with it. It is minted per process
// and rendered into the page, so a request from any other page in the browser — which can reach the
// same loopback port — carries nothing and is refused.
const token = document.querySelector('meta[name="csrf"]')?.content || "";

let table = readMessages(document);
const T = (key) => table[key] ?? key;

function readMessages(doc) {
  try {
    return JSON.parse(doc.getElementById("messages")?.textContent || "{}");
  } catch (err) {
    return {};
  }
}

async function act(action) {
  await fetch("/api/action/" + action, { method: "POST", headers: { "X-Daedalus-Desktop": token } });
}

// ---- the language, in the corner of every page -------------------------------------------------

// Switching languages fetches this same page in the other one and swaps the part of it that holds
// words. A reload would do the same thing and lose whatever has been typed into the setup form, so
// the typed values are carried across instead — they never leave the page to do it.
async function switchLang(lang) {
  if (lang === document.body.dataset.lang) return;
  // The language is written beside the data before the page is swapped. If that write failed the
  // re-fetched page comes back in the old language, and a page that says it is Russian while
  // showing English is worse than one that did not switch at all.
  const saved = await fetch("/api/lang", {
    method: "POST",
    headers: { "X-Daedalus-Desktop": token, "Content-Type": "application/json" },
    body: JSON.stringify({ lang }),
  });
  if (!saved.ok) return;
  // A field is remembered by its name and, where one name covers several controls, by the value
  // that tells them apart: both mode radios are `name="mode"` and only their value says which one
  // the operator chose. What is carried across is the checked state, not the value attribute —
  // the fresh page brings back the machine's suggestion, and that is exactly what must not win.
  const name = (field) => field.name + ":" + (field.type === "checkbox" || field.type === "radio" ? field.value : "");
  const typed = new Map();
  for (const field of document.querySelectorAll("input[name]")) {
    typed.set(name(field), field.type === "checkbox" || field.type === "radio" ? field.checked : field.value);
  }
  // What was open and which provider was showing are part of where the operator was, too.
  const opened = [...document.querySelectorAll("details")].map((one) => one.open);
  const provider = document.querySelector('.seg button[aria-pressed="true"]')?.dataset.provider;
  const answer = await fetch(location.pathname + location.search, { headers: { "Accept-Language": lang } });
  const fresh = new DOMParser().parseFromString(await answer.text(), "text/html");
  document.querySelector("main").replaceWith(fresh.querySelector("main"));
  document.getElementById("messages").textContent = fresh.getElementById("messages").textContent;
  table = readMessages(document);
  document.body.dataset.lang = lang;
  document.documentElement.lang = lang;
  for (const button of document.querySelectorAll(".langs button")) {
    button.setAttribute("aria-pressed", String(button.dataset.lang === lang));
  }
  for (const field of document.querySelectorAll("input[name]")) {
    const key = name(field);
    if (!typed.has(key)) continue;
    if (field.type === "checkbox" || field.type === "radio") field.checked = typed.get(key);
    else field.value = typed.get(key);
  }
  document.querySelectorAll("details").forEach((one, i) => (one.open = opened[i] ?? false));
  if (page === "setup") setupPanels(provider);
}

document.addEventListener("click", (event) => {
  const lang = event.target.closest(".langs button");
  if (lang) switchLang(lang.dataset.lang);
});

// ---- the first page ----------------------------------------------------------------------------

// One provider key is asked for at a time. All three fields are in the form and all three are
// posted: an untouched field carries what is already on file, and the launcher reads an empty one
// as "leave it alone", so showing one panel changes nothing about what is written.
function showProvider(name) {
  document.querySelectorAll(".seg button[data-provider]").forEach((button) => {
    const chosen = button.dataset.provider === name;
    button.setAttribute("aria-pressed", String(chosen));
    document.querySelector(`[data-panel="${button.dataset.provider}"]`).hidden = !chosen;
  });
}

function setupPanels(provider) {
  document.querySelectorAll(".seg button[data-provider]").forEach((button) => {
    button.addEventListener("click", () => showProvider(button.dataset.provider));
  });
  if (provider) showProvider(provider);
  // The language the form was filled in is the language the installation keeps.
  const field = el("lang-field");
  if (field) field.value = document.body.dataset.lang;
}

// ---- the waiting page --------------------------------------------------------------------------

function drawProgress(status) {
  const steps = [...document.querySelectorAll("#steps .row")];
  const at = status.steps ? status.steps.indexOf(status.stage) : -1;
  const ready = status.running > 0 && !status.busy;
  steps.forEach((row, i) => {
    row.classList.toggle("done", ready || (at >= 0 && i < at));
    row.classList.toggle("now", !ready && i === at);
  });

  const track = el("track");
  track.hidden = !status.busy && !ready;
  track.className = "track";
  if (ready) {
    track.classList.add("done");
  } else if (status.size > 0) {
    track.classList.add("determinate");
    track.firstElementChild.style.width = Math.min(100, Math.round((status.done / status.size) * 100)) + "%";
  } else {
    track.classList.add("indeterminate");
  }

  const failed = Boolean(status.failure) && !status.busy;
  el("working").hidden = failed;
  el("trouble").hidden = !failed;
  if (failed) {
    // What the launcher can name, it names — in the language of the page. What it cannot stays as
    // the program said it, which is a visible gap rather than a silent one. Either way the original
    // text is under "What happened", above the log.
    // A failure it cannot name gets a plain lead-in and the program's own words under it, set in
    // the log's type: bare, "exit status 137: …" read like the launcher's sentence.
    const known = status.docker_missing || status.failure_key;
    el("what").textContent = status.docker_missing ? T("docker.missing") : status.failure_key ? T(status.failure_key) : T("progress.error.raw");
    el("what-raw").hidden = Boolean(known);
    el("what-raw").textContent = known ? "" : status.failure;
    el("trouble-log").textContent = [status.failure, ...(status.log || [])].filter(Boolean).join("\n");
    return;
  }
  const idle = !status.busy && !ready;
  el("idle").hidden = !idle;
  // Two lines: what is happening, in the operator's language, and under it the launcher's own
  // commentary, which is machine output and looks like it. Nothing happening is a state of its
  // own: it once fell through to "Working…", printed right above the Start button it contradicts.
  el("live").textContent = ready
    ? T("progress.done")
    : idle
      ? T("progress.idle")
      : status.stage
        ? T("live." + status.stage)
        : T("progress.working");
  el("livelog").textContent = ready ? "" : (status.log || []).slice(-1)[0] || "";
  el("livelog").title = el("livelog").textContent;
  if (ready) setTimeout(() => (location.href = "/status"), 900);
}

// ---- the status page ---------------------------------------------------------------------------

function drawStatus(status) {
  // In native mode there is no Docker to report and the same tile says what the agent runs on.
  el("docker").textContent = status.mode === "native" ? T("status.tile.machine") : status.docker || T("status.unavailable");
  el("containers").textContent = status.running + " " + T("status.running");
  if (el("ports")) el("ports").textContent = status.ports || T("status.none");
  el("telegram").textContent = status.telegram ? T("status.on") : T("status.off");
  el("appurl").textContent = status.app_url;
  el("appurl").href = status.app_url;
  // The operation in words — "Updating…" — beside a turning mark; the bare action name ("update…")
  // read as a stray label. An action without a line of its own ("installing …") is already a phrase.
  el("busy").textContent = status.busy ? table["busy." + status.busy] ?? status.busy + "…" : "";
  el("busy").classList.toggle("on", Boolean(status.busy));
  for (const button of document.querySelectorAll("button[data-action]")) {
    button.disabled = Boolean(status.busy);
  }
  // The configuration is a link, not a button, so `disabled` means nothing to it — and saving the
  // form in the middle of a start or an update rewrites the mode and the .env under the work in
  // flight. It is held off for the same span the buttons are.
  for (const link of document.querySelectorAll("a.button[data-busy-off]")) {
    if (status.busy) link.setAttribute("aria-disabled", "true");
    else link.removeAttribute("aria-disabled");
    link.tabIndex = status.busy ? -1 : 0;
  }
  // The agent's own code. A change waiting for a restart is the only thing on this page the
  // operator has to act on, so it gets a card of its own and the button that applies it.
  const change = status.change || {};
  el("change").hidden = !change.commit;
  // A change that was reversed or never applied is a failure the operator should notice, and the
  // card's green edge said "all is well" over a sentence saying the opposite.
  el("change").classList.toggle("failed", !change.pending && (change.status === "rolled_back" || change.status === "failed"));
  if (change.commit) {
    el("change-title").textContent = change.pending ? T("change.pending") : changeOutcome(change.status);
    el("change-body").textContent = change.pending ? change.summary : change.detail || change.summary;
    // The whole row goes, not only its button: an empty row kept its spacing and left a finished
    // card with a blank strip at the bottom where the button had been.
    el("change-actions").hidden = !change.pending;
  }
  // A newer launcher release. The page only tells: installing it is `daedalus-desktop upgrade` with
  // the launcher closed, which asks, backs up and checks the backup first.
  const offer = status.upgrade;
  if (el("upgrade")) {
    el("upgrade").hidden = !offer;
    if (offer) {
      el("upgrade-title").textContent = T("upgrade.card.title").replace("%s", String(offer.to).replace(/^desktop-v/, ""));
      el("upgrade-body").textContent = T("upgrade.card.body").replace("%s", String(offer.from).replace(/^desktop-v/, ""));
      if (offer.command) el("upgrade-command").textContent = offer.command;
    }
  }
  // What the data folder's switches kept, and anything about them that needs the operator. The
  // lines come from the control folder beside the data; the words from the page's own table.
  const switches = status.switches || { items: [] };
  if (el("switches")) {
    const items = switches.items || [];
    el("switches").hidden = items.length === 0;
    el("switches").classList.toggle("decide", Boolean(switches.trouble));
    el("switches-title").textContent = T(switches.trouble ? "switch.card.trouble" : "switch.card.title");
    const list = el("switches-items");
    list.replaceChildren(...items.map((item) => {
      const li = document.createElement("li");
      li.textContent = T("switch.item." + item.kind).replace("%s", item.path);
      if (item.detail && item.kind !== "unscanned" && item.kind !== "unfinished") li.title = item.detail;
      return li;
    }));
  }
  // One primary button per page. A pending change owns it, because restarting is the step that
  // matters; with nothing running, opening the app leads to a page that does not answer, so the
  // button steps back instead of being the brightest thing under a warning that says so.
  const open = document.querySelector('button[data-action="open"]');
  open.classList.toggle("primary", !change.pending && status.running > 0);
  open.classList.toggle("quiet", !status.running);
  open.title = status.running ? "" : T("status.open.idle");
  if (status.docker_missing) showAlert(T("alert.docker"), T("docker.missing"));
  else if (status.failure) showAlert(T("alert.failure"), status.failure);
  else el("alert").hidden = true;
  const log = status.log || [];
  el("log").textContent = log.length ? log.join("\n") : T("status.log.empty");
}

function showAlert(title, body) {
  el("alert-title").textContent = title;
  el("alert-body").textContent = body;
  el("alert").hidden = false;
}

// The supervisor's word for what happened, in the operator's.
function changeOutcome(status) {
  if (status === "applied") return T("change.applied");
  if (status === "rolled_back") return T("change.reverted");
  return T("change.failed");
}

// ---- the poll ----------------------------------------------------------------------------------

async function refresh() {
  let status;
  try {
    status = await (await fetch("/api/status")).json();
  } catch (err) {
    if (page === "status") showAlert(T("alert.silent"), T("status.silent"));
    else if (el("live")) el("live").textContent = T("status.silent");
    return;
  }
  if (page === "status") drawStatus(status);
  if (page === "progress") drawProgress(status);
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  button.disabled = true;
  await act(button.dataset.action);
  refresh();
});

if (page === "setup") setupPanels();
if (page === "status" || page === "progress") {
  refresh();
  setInterval(refresh, page === "progress" ? 1000 : 2000);
}
