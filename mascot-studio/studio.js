import { emotions, actions, props, scenarios, mascots, byId } from "./catalog.js";
import { MascotStage } from "./stage.js";
import { PROPS } from "./props.js";
import { faceSketch } from "./face.js";

const $ = (id) => document.getElementById(id);
const state = { tab: "scenarios", filter: "Все", query: "", mascot: "daedalus", emotion: "calm", action: "idle", prop: "", scenario: "", tour: null, voice: "idle", model: "", framing: "" };
let stage;
try { stage = new MascotStage($("stage")); }
catch (error) { $("stage-state").textContent = "WEBGL НЕДОСТУПЕН"; $("speech").textContent = "3D-сцена недоступна в этом браузере. Библиотеку всё ещё можно посмотреть."; console.error(error); }

const collections = { scenarios, emotions, actions, props, mascots };
const names = { scenarios: "сцен", emotions: "эмоций", actions: "движений", props: "предметов", mascots: "маскота" };
// the action a prop suggests when it is picked on its own (props.js `use`)
const propActions = Object.fromEntries(Object.entries(PROPS).map(([id, spec]) => [id, spec.use]));
const actionIcons = { idle: "∿", wave: "≋", nod: "⇣", peek: "◐", point: "➚", offer: "⇧", focus: "✎", read: "▤", write: "✐", think: "?", listen: "◖", speak: "❞", reassure: "♡", celebrate: "✦", dance: "♪", float: "☁", stretch: "⇕", sip: "♨", sleep: "z" };
const mascotIcons = { daedalus: "◈", chibi: "◇", head: "◉" };
const lines = [
  "Я здесь. Работаем в твоём темпе.", "Кажется, всё идёт неплохо.", "Можно на минутку выдохнуть.",
  "Я присмотрю за мелочами.", "О, это интересный поворот.", "Если что-то пойдёт не так, разберёмся вместе.",
  "У тебя получилось. Я рад!", "Не торопись. Я подожду.", "Хочешь, покажу, что заметил?",
];

// A line in the panel, and the mascot saying it: the mouth moves for about as long as the words.
function say(line) {
  $("speech").textContent = line;
  if (line) stage?.talk(Math.min(4.5, 0.6 + line.length * 0.055));
}
function selectedCaption() {
  $("selection-caption").textContent = [byId(mascots, state.mascot)?.title, byId(emotions, state.emotion)?.title.toLowerCase(), byId(actions, state.action)?.title.toLowerCase(), byId(props, state.prop)?.title.toLowerCase()].filter(Boolean).join(" · ");
  $("stage-state").textContent = byId(emotions, state.emotion)?.title.toUpperCase() || "";
}
function apply(patch, line = "") {
  Object.assign(state, patch);
  if (patch.mascot !== undefined) stage?.setMascot(state.mascot);
  if (patch.emotion !== undefined) stage?.setEmotion(state.emotion);
  if (patch.action !== undefined) stage?.setAction(state.action);
  if (patch.prop !== undefined) stage?.setProp(state.prop);
  if (line) say(line);
  selectedCaption();
  render();
}
function choose(tab, item) {
  if (tab === "scenarios") apply({ scenario: item.id, emotion: item.emotion, action: item.action, prop: item.prop }, item.line);
  else if (tab === "emotions") apply({ scenario: "", emotion: item.id }, item.detail);
  else if (tab === "actions") apply({ scenario: "", action: item.id, prop: item.prop || state.prop }, item.detail);
  else if (tab === "props") apply({ scenario: "", prop: item.id, action: propActions[item.id] || (item.id ? "offer" : "idle") }, item.detail);
  else if (tab === "mascots") apply({ mascot: item.id }, item.detail);
}
function selection(tab, id) { return ({ scenarios: state.scenario, emotions: state.emotion, actions: state.action, props: state.prop, mascots: state.mascot })[tab] === id; }

// The camera follows the library: the face for moods, the upper body for the voice, the whole
// figure otherwise, unless the toggle on the stage asked for a frame.
function setFraming(mode, manual) {
  if (manual) state.framing = mode;
  const auto = state.tab === "emotions" ? "portrait" : state.tab === "voice" ? "medium" : "full";
  const frame = state.framing || auto;
  stage?.setFraming(frame);
  document.querySelectorAll(".framing button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.framing === frame || (frame === "medium" && b.dataset.framing === "full"))));
}
function setTab(tab) {
  if (state.tab === "voice" && tab !== "voice") stopVoiceInputs();
  state.tab = tab; state.filter = "Все"; state.query = ""; $("search").value = "";
  state.framing = "";
  document.querySelectorAll(".tab").forEach((el) => { el.classList.toggle("active", el.dataset.tab === tab); el.setAttribute("aria-selected", String(el.dataset.tab === tab)); });
  $("library-tools").hidden = tab === "voice" || tab === "ai";
  setFraming();
  render();
}

// A small drawing of a mood's face for its card, made from the same numbers as the 3D face.
const SVG = "http://www.w3.org/2000/svg";
let iconSeq = 0;
function faceIcon(emotion) {
  const f = faceSketch(emotion), id = `fi${++iconSeq}`;
  const el = (name, attrs, parent) => { const n = document.createElementNS(SVG, name); for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v); parent?.appendChild(n); return n; };
  const pts = (list) => list.map(([x, y]) => `${x.toFixed(3)},${y.toFixed(3)}`).join(" ");
  const svg = el("svg", { viewBox: "-1.1 -1.0 2.2 2.0", "aria-hidden": "true" });
  const g = el("g", { transform: "scale(1,-1)" }, svg);
  el("rect", { x: -1, y: -0.86, width: 2, height: 1.72, rx: 0.42, fill: "#20282c" }, g);
  if (f.blush > 0.2) for (const s of [-1, 1]) el("ellipse", { cx: s * 0.62, cy: -0.17, rx: 0.17, ry: 0.11, fill: "#ff8f86", opacity: Math.min(0.85, f.blush * 0.8) }, g);
  f.eyes.forEach((eye, i) => {
    if (eye.white) {
      const clip = el("clipPath", { id: `${id}e${i}` }, g);
      el("polygon", { points: pts(eye.white) }, clip);
      el("polygon", { points: pts(eye.white), fill: "#f4f1ea" }, g);
      el("circle", { cx: eye.pupil[0], cy: eye.pupil[1], r: eye.pupil[2], fill: "#14171a", "clip-path": `url(#${id}e${i})` }, g);
    } else el("polyline", { points: pts(eye.closed), fill: "none", stroke: "#7af3d6", "stroke-width": 0.07, "stroke-linecap": "round" }, g);
  });
  f.brows.forEach((b) => el("polyline", { points: pts(b), fill: "none", stroke: "#7af3d6", "stroke-width": 0.065, "stroke-linecap": "round" }, g));
  const m = f.mouth;
  if (m.open) el("polygon", { points: pts(m.upper.concat(m.lower.slice().reverse())), fill: "#0a1416", stroke: "#7af3d6", "stroke-width": 0.06, "stroke-linejoin": "round" }, g);
  else el("polyline", { points: pts(m.upper), fill: "none", stroke: "#7af3d6", "stroke-width": 0.065, "stroke-linecap": "round" }, g);
  return svg;
}

function card(item, tab) {
  const button = document.createElement("button");
  button.className = `card${selection(tab, item.id) ? " selected" : ""}`;
  button.type = "button";
  button.setAttribute("aria-pressed", String(selection(tab, item.id)));
  const symbol = document.createElement("span"); symbol.className = "card-icon";
  if (tab === "emotions") symbol.append(faceIcon(item.id));
  else if (tab === "scenarios") symbol.append(faceIcon(item.emotion));
  else symbol.textContent = tab === "actions" ? actionIcons[item.id] || "↝" : tab === "mascots" ? mascotIcons[item.id] || "◈" : item.icon || "✳";
  const copy = document.createElement("span"); copy.className = "card-copy";
  const title = document.createElement("strong"); title.textContent = item.title;
  const detail = document.createElement("small"); detail.textContent = item.detail;
  copy.append(title, detail);
  if (tab === "scenarios") {
    const code = document.createElement("span"); code.className = "code";
    code.textContent = [byId(emotions, item.emotion)?.title, byId(actions, item.action)?.title, byId(props, item.prop)?.title].filter(Boolean).join(" · ");
    copy.append(code);
  }
  if (tab === "props" && item.id) {
    const code = document.createElement("span"); code.className = "code";
    code.textContent = byId(actions, propActions[item.id])?.title || "";
    copy.append(code);
  }
  button.append(symbol, copy);
  button.addEventListener("click", () => { stopTour(); choose(tab, item); });
  return button;
}
function renderCards() {
  const tab = state.tab;
  const items = tab === "props" ? [{ id: "", title: "Без предмета", group: "Все", detail: "Оставить только персонажа на сцене.", icon: "∅" }, ...props] : collections[tab];
  const groups = ["Все", ...new Set(items.map((item) => item.group).filter((group) => group && group !== "Все"))];
  const filterBar = $("filters"); filterBar.replaceChildren();
  groups.forEach((group) => {
    const b = document.createElement("button"); b.className = `filter${state.filter === group ? " active" : ""}`; b.type = "button"; b.textContent = group;
    b.addEventListener("click", () => { state.filter = group; render(); }); filterBar.append(b);
  });
  filterBar.hidden = groups.length < 2;
  // what a card says is what a search finds: a scene by its mood, action and prop, a prop by
  // the action it suggests
  const words = (item) => [item.title, item.detail, item.group, tab === "scenarios" ? [byId(emotions, item.emotion)?.title, byId(actions, item.action)?.title, byId(props, item.prop)?.title].join(" ") : "", tab === "props" && item.id ? byId(actions, propActions[item.id])?.title : ""].join(" ").toLocaleLowerCase("ru");
  const q = state.query.toLocaleLowerCase("ru");
  const visible = items.filter((item) => (state.filter === "Все" || item.group === state.filter) && words(item).includes(q));
  $("library-count").textContent = `${tab === "props" ? props.length : items.length} ${names[tab]}`.toUpperCase();
  const body = $("library-body"); body.replaceChildren();
  if (!visible.length) { const e = document.createElement("div"); e.className = "empty"; e.textContent = "Ничего не нашлось. Попробуй другой запрос."; body.append(e); return; }
  const grid = document.createElement("div"); grid.className = "cards";
  visible.forEach((item) => grid.append(card(item, tab)));
  body.append(grid);
}

// ---- the voice tab: the states of a voice conversation, the browser's own voice reading the
// line aloud, and the microphone's level (measured here, never recorded or sent) ----
const voiceStates = [
  { id: "idle", title: "Тихо ждёт", detail: "Дышит, но не требует внимания.", scenario: "" },
  { id: "listen", title: "Слушает", detail: "Наклоняется к голосу, свет в груди дышит в такт.", scenario: "voice_listen" },
  { id: "think", title: "Думает", detail: "Небольшая пауза с идеей рядом.", scenario: "idea" },
  { id: "speak", title: "Отвечает", detail: "Рот и мягкий жест следуют за голосом.", scenario: "voice_reply" },
];
const voice = { mic: null, raf: 0, level: 0, speaking: false, manual: 0 };
function voiceStatus(text, error) { const s = $("voice-status"); if (s) { s.textContent = text; s.classList.toggle("error", !!error); } }
function setLevel(n) {
  voice.level = n;
  stage?.setVoiceLevel(n);
  const fill = $("voice-meter"); if (fill) fill.style.width = `${Math.round(n * 100)}%`;
}
function pickVoice(id) {
  state.voice = id;
  const item = voiceStates.find((v) => v.id === id);
  if (id === "idle") apply({ scenario: "", emotion: "calm", action: "idle", prop: "" }, "Я здесь. Слушаю, когда понадоблюсь.");
  else choose("scenarios", byId(scenarios, item.scenario));
}
// The browser's speech synthesis reads the line; it gives no audio level, so the mouth follows
// a syllable rhythm while the voice speaks, quickened at the word boundaries it reports.
function speakLine() {
  if (state.voice !== "speak") pickVoice("speak");
  const line = $("speech").textContent;
  const synth = window.speechSynthesis;
  const ru = synth && synth.getVoices().find((v) => v.lang && v.lang.toLowerCase().startsWith("ru"));
  if (!synth || !ru) {
    voiceStatus("В этом браузере нет русского голоса — показываю ритм реплики без звука.");
    simulateSpeech(Math.min(5, 0.6 + line.length * 0.06));
    return;
  }
  synth.cancel();
  const u = new SpeechSynthesisUtterance(line);
  u.voice = ru; u.lang = ru.lang; u.rate = 1; u.pitch = 1.15;
  let beat = 0;
  u.onboundary = () => { beat = performance.now(); };
  u.onstart = () => {
    voice.speaking = true; voiceStatus(`Голос браузера: ${ru.name}`);
    const tick = () => {
      if (!voice.speaking) return;
      const t = performance.now() / 1000, since = (performance.now() - beat) / 1000;
      setLevel(Math.min(1, 0.25 + 0.45 * Math.abs(Math.sin(t * 9.5)) + 0.3 * Math.exp(-since * 6)));
      voice.raf = requestAnimationFrame(tick);
    };
    tick();
  };
  u.onend = u.onerror = () => { voice.speaking = false; cancelAnimationFrame(voice.raf); setLevel(voice.manual); };
  synth.speak(u);
}
function simulateSpeech(seconds) {
  const t0 = performance.now();
  voice.speaking = true;
  const tick = () => {
    const t = (performance.now() - t0) / 1000;
    if (t > seconds || !voice.speaking) { voice.speaking = false; setLevel(voice.manual); return; }
    setLevel(Math.max(0, 0.2 + 0.5 * Math.abs(Math.sin(t * 9.5)) * (0.6 + 0.4 * Math.sin(t * 2.3))));
    voice.raf = requestAnimationFrame(tick);
  };
  cancelAnimationFrame(voice.raf); tick();
}
async function toggleMic() {
  if (voice.mic) { stopMic(); voiceStatus("Микрофон выключен."); return; }
  try {
    if (!navigator.mediaDevices?.getUserMedia) throw Object.assign(new Error("браузер не даёт доступа к микрофону"), { name: "Unsupported" });
    const stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
    const ctx = new AudioContext(), source = ctx.createMediaStreamSource(stream), analyser = ctx.createAnalyser();
    analyser.fftSize = 512; source.connect(analyser);
    const data = new Uint8Array(analyser.fftSize);
    voice.mic = { stream, ctx };
    if (state.voice !== "listen") pickVoice("listen");
    voiceStatus("Микрофон слушает. Звук никуда не уходит — считается только громкость.");
    const b = $("voice-mic"); if (b) b.textContent = "■ Выключить микрофон";
    const tick = () => {
      if (!voice.mic) return;
      analyser.getByteTimeDomainData(data);
      let sum = 0; for (const v of data) sum += ((v - 128) / 128) ** 2;
      setLevel(Math.min(1, voice.level * 0.7 + Math.min(1, Math.sqrt(sum / data.length) * 5) * 0.3));
      voice.raf = requestAnimationFrame(tick);
    };
    tick();
  } catch (error) {
    const why = error.name === "NotAllowedError" ? "нет разрешения" : error.name === "NotFoundError" ? "устройство не найдено" : error.message || error.name;
    voiceStatus(`Микрофон недоступен: ${why}.`, true);
  }
}
function stopMic() {
  if (!voice.mic) return;
  voice.mic.stream.getTracks().forEach((t) => t.stop());
  voice.mic.ctx.close();
  voice.mic = null;
  cancelAnimationFrame(voice.raf);
  setLevel(voice.manual);
  const b = $("voice-mic"); if (b) b.textContent = "◉ Подключить микрофон";
}
function stopVoiceInputs() { stopMic(); window.speechSynthesis?.cancel(); voice.speaking = false; voice.manual = 0; setLevel(0); }
function renderVoice() {
  $("library-count").textContent = "4 СОСТОЯНИЯ";
  const body = $("library-body"); body.replaceChildren();
  const panel = document.createElement("div"); panel.className = "feature-panel";
  const h = document.createElement("h3"); h.textContent = "Голосовой режим";
  const p = document.createElement("p"); p.textContent = "Сцена вместо большой кнопки микрофона: состояние разговора меняет мимику, позу и свет в груди маскота.";
  panel.append(h, p);
  const grid = document.createElement("div"); grid.className = "voice-states";
  voiceStates.forEach((item) => {
    const b = document.createElement("button"); b.type = "button"; b.className = `voice-state${state.voice === item.id ? " active" : ""}`;
    b.setAttribute("aria-pressed", String(state.voice === item.id));
    const strong = document.createElement("strong"); strong.textContent = item.title;
    const small = document.createElement("small"); small.textContent = item.detail;
    b.append(strong, small);
    b.addEventListener("click", () => { stopTour(); pickVoice(item.id); });
    grid.append(b);
  });
  panel.append(grid);
  const controls = document.createElement("div"); controls.className = "voice-controls";
  const play = document.createElement("button"); play.type = "button"; play.className = "button button-primary"; play.textContent = "▶ Произнести реплику";
  play.addEventListener("click", () => { stopTour(); speakLine(); });
  const mic = document.createElement("button"); mic.type = "button"; mic.id = "voice-mic"; mic.className = "button button-ghost"; mic.textContent = voice.mic ? "■ Выключить микрофон" : "◉ Подключить микрофон";
  mic.addEventListener("click", () => { stopTour(); toggleMic(); });
  controls.append(play, mic);
  panel.append(controls);
  const meter = document.createElement("div"); meter.className = "meter";
  const meterLabel = document.createElement("span"); meterLabel.textContent = "Уровень";
  const track = document.createElement("div"); track.className = "meter-track";
  const fill = document.createElement("div"); fill.className = "meter-fill"; fill.id = "voice-meter";
  track.append(fill); meter.append(meterLabel, track); panel.append(meter);
  const rangeRow = document.createElement("label"); rangeRow.className = "range-row";
  rangeRow.textContent = "Вручную";
  const range = document.createElement("input"); range.type = "range"; range.min = "0"; range.max = "100"; range.value = String(Math.round(voice.manual * 100));
  range.setAttribute("aria-label", "Уровень голоса вручную");
  range.addEventListener("input", () => { voice.manual = Number(range.value) / 100; if (!voice.mic && !voice.speaking) setLevel(voice.manual); });
  rangeRow.append(range); panel.append(rangeRow);
  const status = document.createElement("p"); status.className = "status-line"; status.id = "voice-status"; status.setAttribute("role", "status"); panel.append(status);
  const tip = document.createElement("p"); tip.className = "tip"; tip.textContent = "В приложении сцена получает уровень голоса из уже существующего аудиопотока. Здесь его дают голос браузера, микрофон (громкость считается на месте) или ползунок.";
  panel.append(tip);
  body.append(panel);
  setLevel(voice.mic || voice.speaking ? voice.level : voice.manual);
}

let modelTimer;
async function loadModels(q = "flash") {
  const status = $("ai-status");
  status.textContent = "Загружаю список моделей…"; status.classList.remove("error");
  try {
    const response = await fetch(`/api/models?q=${encodeURIComponent(q)}`);
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Не удалось загрузить модели");
    const select = $("ai-model"); if (!select) return;
    select.replaceChildren();
    data.models.forEach((item) => { const option = document.createElement("option"); option.value = item.id; option.textContent = `${item.name} · ${item.id}`; select.append(option); });
    const preferred = data.models.find((item) => item.id === state.model) || data.models.find((item) => item.id === "google/gemini-2.5-flash-lite") || data.models[0];
    if (preferred) { select.value = preferred.id; state.model = preferred.id; }
    status.textContent = `${data.total} моделей по запросу. Выбранная модель получит только текст события.`;
  } catch (error) { status.textContent = String(error.message || error); status.classList.add("error"); }
}
function renderAI() {
  $("library-count").textContent = "РЕАКЦИИ ПО СОБЫТИЯМ";
  const body = $("library-body"); body.replaceChildren();
  const panel = document.createElement("div"); panel.className = "feature-panel ai-panel";
  panel.innerHTML = `<h3>Режиссёр под капотом</h3><p>Модель выбирает короткую реплику, мимику, действие и предмет. Вызов происходит только по кнопке; reasoning отключается там, где модель это позволяет. История чата не отправляется.</p>
    <label for="ai-search">Модель OpenRouter</label><input id="ai-search" type="search" value="flash" placeholder="Поиск по каталогу моделей">
    <label for="ai-model">Выбрать модель</label><select id="ai-model"></select>
    <label for="ai-event">Событие в Daedalus</label><textarea id="ai-event" maxlength="240">Пользователь завершил сложную задачу.</textarea>
    <div class="mini-metrics"><span>≤ 150 токенов ответа</span><span>пауза 8 с</span><span>30 проб в день</span></div>
    <button id="ai-generate" class="button button-primary" type="button">✦ Сгенерировать реакцию</button>
    <p id="ai-status" class="status" role="status"></p><div id="ai-chips" class="reaction-chips"></div>`;
  const chips = document.createElement("div"); chips.className = "filters";
  ["Начата задача", "Нужен ответ пользователя", "Ответ готов", "Ошибка инструмента", "Долгое ожидание", "Открыт голосовой режим"].forEach((event) => {
    const b = document.createElement("button"); b.className = "filter"; b.type = "button"; b.textContent = event;
    b.addEventListener("click", () => { $("ai-event").value = event; }); chips.append(b);
  });
  panel.insertBefore(chips, panel.querySelector(".mini-metrics"));
  const tip = document.createElement("p"); tip.className = "tip"; tip.textContent = "Для продукта: реагировать только на значимые события с редкой частотой, не повторять реплики, давать маскоту замолчать и разрешать пользователю отключить нейронку.";
  panel.append(tip);
  body.append(panel);
  $("ai-search").addEventListener("input", () => { clearTimeout(modelTimer); modelTimer = setTimeout(() => loadModels($("ai-search").value), 350); });
  $("ai-model").addEventListener("change", () => { state.model = $("ai-model").value; });
  $("ai-generate").addEventListener("click", generateReaction);
  loadModels();
}
async function generateReaction() {
  const status = $("ai-status"), button = $("ai-generate");
  button.disabled = true; status.textContent = "Модель выбирает реакцию…"; status.classList.remove("error");
  try {
    const response = await fetch("/api/react", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model: $("ai-model").value, event: $("ai-event").value }) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "Не удалось получить реакцию");
    const r = data.reaction;
    stopTour();
    apply({ scenario: "", emotion: r.emotion, action: r.action, prop: r.prop }, r.line);
    const tokens = data.usage?.total_tokens;
    status.textContent = `Реакция получена${tokens ? ` · ${tokens} токенов` : ""}.${data.forced_reasoning ? " Эта модель требует reasoning." : ""}`;
    const row = $("ai-chips"); row.replaceChildren();
    [byId(emotions, r.emotion)?.title, byId(actions, r.action)?.title, byId(props, r.prop)?.title || "без предмета"].filter(Boolean).forEach((text) => { const s = document.createElement("span"); s.textContent = text; row.append(s); });
  } catch (error) { status.textContent = String(error.message || error); status.classList.add("error"); }
  finally { button.disabled = false; }
}
function render() {
  if (state.tab === "voice") renderVoice();
  else if (state.tab === "ai") { if (!$("ai-generate")) renderAI(); }
  else renderCards();
}

// ---- quick actions ----
function stopTour() {
  if (!state.tour) return;
  clearInterval(state.tour); state.tour = null;
  $("tour").classList.remove("active"); $("tour").setAttribute("aria-pressed", "false"); $("tour").textContent = "▶ Автопоказ";
}
const pick = (list) => list[Math.floor(Math.random() * list.length)];
// A surprise that still makes sense: an action with something that suits it, and, more often
// than not, a mood that suits the action.
function surprise() {
  stopTour();
  const action = pick(actions.filter((a) => a.id !== state.action));
  const suited = Object.keys(propActions).filter((id) => propActions[id] === action.id);
  const prop = action.prop || (suited.length && Math.random() < 0.8 ? pick(suited) : "");
  const moods = { celebrate: ["joy", "proud"], dance: ["joy", "affectionate"], sleep: ["sleepy"], sip: ["calm", "affectionate"], focus: ["focused", "determined"], write: ["focused"], read: ["curious", "calm"], think: ["curious", "focused"], reassure: ["worried", "affectionate"] };
  const emotion = moods[action.id] && Math.random() < 0.75 ? pick(moods[action.id]) : pick(emotions).id;
  apply({ scenario: "", emotion, action: action.id, prop }, pick(lines));
}

document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));
document.querySelectorAll(".framing button").forEach((b) => b.addEventListener("click", () => setFraming(b.dataset.framing, true)));
$("search").addEventListener("input", () => { state.query = $("search").value.trim(); render(); });
$("camera-reset").addEventListener("click", () => stage?.resetCamera());
$("new-line").addEventListener("click", () => say(pick(lines.filter((l) => l !== $("speech").textContent))));
$("surprise").addEventListener("click", surprise);
$("tour").addEventListener("click", () => {
  if (state.tour) { stopTour(); return; }
  let index = 0;
  choose("scenarios", scenarios[index]);
  state.tour = setInterval(() => { index = (index + 1) % scenarios.length; choose("scenarios", scenarios[index]); }, 7500);
  $("tour").classList.add("active"); $("tour").setAttribute("aria-pressed", "true"); $("tour").textContent = "■ Остановить";
});
document.addEventListener("visibilitychange", () => { if (document.hidden) { stopMic(); window.speechSynthesis?.cancel(); } });
selectedCaption(); setFraming(); render();

export { stage, apply, state };
