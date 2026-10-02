/* The launcher's first-run wizard: state, words, validation, keyboard, step changes, and the calls
   that make each answer real (desktop/setup_page.go). The 3D stage around the frame (scene.js)
   hooks in through window.WizardHooks; without it the same wizard runs over a still picture. */
(function () {
  'use strict';

  var STEPS = ['welcome', 'runs', 'model', 'limit', 'telegram', 'advanced', 'summary', 'start'];
  var PROVIDERS = [
    { id: 'deepseek', name: 'DeepSeek', prefix: 'sk-' },
    { id: 'openrouter', name: 'OpenRouter', prefix: 'sk-or-' },
    { id: 'opencode', name: 'OpenCode', prefix: '' },
    { id: 'openai', name: 'OpenAI', prefix: 'sk-' },
    { id: 'anthropic', name: 'Anthropic', prefix: 'sk-ant-' },
    // Subscription plans are providers of their own: the plan's key only works on its own endpoint.
    // No prefix is checked where the vendor's key format is not fixed.
    { id: 'zai', name: 'Z.AI', prefix: '' },
    { id: 'zai_coding', name: 'Z.AI Coding', prefix: '' },
    { id: 'minimax', name: 'MiniMax', prefix: '' },
    { id: 'minimax_plan', name: 'MiniMax Plan', prefix: '' },
    { id: 'moonshot', name: 'Moonshot', prefix: 'sk-' },
    { id: 'kimi_coding', name: 'Kimi Code', prefix: '' }
  ];
  var CLIS = [
    { id: 'codex', name: 'Codex' },
    { id: 'claude', name: 'Claude Code' },
    { id: 'grok', name: 'Grok' }
  ];

  // The words and the starting state are the launcher's, rendered into the page: the words from
  // its table (desktop/i18n.go) in both languages, because the switch in the corner changes
  // language in place, and the state from what this installation already has, secrets masked.
  function readJSON(id) {
    var el = document.getElementById(id);
    try { return el ? JSON.parse(el.textContent) : null; } catch (e) { return null; }
  }
  var WORDS = readJSON('setup-words') || { en: {}, ru: {} };
  var BOOT = readJSON('setup-boot') || {};
  var CSRF = (document.querySelector('meta[name=csrf]') || {}).content || '';

  var ICONS = {
    laptop: '<rect x="4" y="5" width="16" height="11" rx="1.6"/><path d="M2 19h20"/>',
    cube: '<path d="M12 2.8 20.5 7.5v9L12 21.2 3.5 16.5v-9Z"/><path d="m3.5 7.5 8.5 4.7 8.5-4.7M12 12.2v9"/>',
    key: '<circle cx="8" cy="15" r="4"/><path d="m10.9 12.1 8.6-8.6M16.5 6.5l2.5 2.5M14 9l2 2"/>',
    term: '<rect x="3" y="4.5" width="18" height="15" rx="2"/><path d="m7 10 3 2.5L7 15M12.5 15H17"/>',
    server: '<rect x="4" y="4" width="16" height="6.5" rx="1.5"/><rect x="4" y="13.5" width="16" height="6.5" rx="1.5"/><path d="M8 7.2h.01M8 16.8h.01"/>',
    check: '<path d="m5 12.5 4.2 4.2L19 7"/>',
    edit: '<path d="M4 20h4L19.5 8.5a2.1 2.1 0 0 0-4-4L4 16Z"/>',
    eye: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z"/><circle cx="12" cy="12" r="2.8"/>',
    eyeoff: '<path d="M3 3l18 18M10.6 5.6A9.6 9.6 0 0 1 12 5.5c6 0 9.5 6.5 9.5 6.5a16.6 16.6 0 0 1-2.9 3.7M6.6 6.6C3.9 8.3 2.5 12 2.5 12S6 18.5 12 18.5a9 9 0 0 0 4.4-1.1"/><path d="M9.9 9.9a2.8 2.8 0 0 0 4.2 4.2"/>',
    refresh: '<path d="M20 11a8 8 0 0 0-14.6-4.4L4 8.5M4 4v4.5h4.5M4 13a8 8 0 0 0 14.6 4.4l1.4-1.9M20 20v-4.5h-4.5"/>',
    plug: '<path d="M9 3v5M15 3v5M6.5 8h11v3.5a5.5 5.5 0 0 1-11 0ZM12 17v4"/>',
    folder: '<path d="M3 6.5A1.5 1.5 0 0 1 4.5 5H9l2 2.5h8.5A1.5 1.5 0 0 1 21 9v9.5a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18.5Z"/>',
    chevron: '<path d="m9 6 6 6-6 6"/>',
    down: '<path d="m6 9 6 6 6-6"/>',
    arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>',
    back: '<path d="M19 12H5M11 6l-6 6 6 6"/>',
    coin: '<circle cx="12" cy="12" r="8.5"/><path d="M14.6 9.3c-.5-.9-1.5-1.3-2.6-1.3-1.5 0-2.6.8-2.6 2s1.1 1.6 2.6 2 2.7.8 2.7 2.1-1.2 2-2.7 2c-1.2 0-2.2-.5-2.7-1.4M12 6.5V8M12 16.8v1.4"/>',
    send: '<path d="M21 3 3 10.5l7 2.5 2.5 7Z"/><path d="m21 3-11 10"/>',
    sliders: '<path d="M4 7h10M18 7h2M4 17h4M12 17h8"/><circle cx="16" cy="7" r="2"/><circle cx="10" cy="17" r="2"/>',
    globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.5 2.6 3.8 5.6 3.8 9S14.5 18.4 12 21c-2.5-2.6-3.8-5.6-3.8-9S9.5 5.6 12 3Z"/>',
    power: '<path d="M12 3v8M6.4 6.6a8 8 0 1 0 11.2 0"/>',
    mic: '<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5.5 11a6.5 6.5 0 0 0 13 0M12 17.5V21"/>',
    browser: '<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 9h18M6.5 6.5h.01M9 6.5h.01"/>',
    sparkle: '<path d="M12 3c.7 4.5 2.5 6.3 7 7-4.5.7-6.3 2.5-7 7-.7-4.5-2.5-6.3-7-7 4.5-.7 6.3-2.5 7-7Z"/>'
  };
  function icon(name, cls) {
    return '<svg class="ic' + (cls ? ' ' + cls : '') + '" viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">' + ICONS[name] + '</svg>';
  }

  var params = new URLSearchParams(location.search);
  var navLang = (navigator.language || 'en').toLowerCase().indexOf('ru') === 0 ? 'ru' : 'en';
  var B = BOOT, BA = BOOT.adv || {}, BL = BOOT.local || {}, BT = BOOT.tg || {};
  var S = {
    lang: B.lang === 'ru' || B.lang === 'en' ? B.lang : navLang,
    mode: B.mode === 'docker' ? 'docker' : 'native',
    docker: !!B.docker,
    kind: B.kind === 'cli' || B.kind === 'local' ? B.kind : 'cloud',
    provider: B.provider || 'deepseek',
    keys: PROVIDERS.reduce(function (keys, p) { keys[p.id] = ''; return keys; }, {}),
    // kept: the masks of what is stored, by field. An empty field keeps the stored value.
    kept: B.kept || {},
    cli: B.cli || 'codex',
    clis: B.clis || { codex: false, claude: false, grok: false },
    scanning: false,
    local: { url: BL.url || 'http://127.0.0.1:11434/v1', key: '', model: BL.model || '', models: BL.model ? [BL.model] : [], test: BL.model ? 'kept' : null, ms: 0 },
    limit: B.limit || '20',
    tg: { token: '', owner: BT.owner || '', apiid: BT.apiid || '', apihash: '', more: !!(BT.apiid || (B.kept && B.kept['tg.apihash'])), skip: false },
    adv: { open: false, data: BA.data || '', portsAuto: BA.portsAuto !== false, port: BA.port || '8765', login: !!BA.login, browser: !!BA.browser, voice: BA.voice || 'off' },
    reveal: {},
    launch: null,
    returnTo: null
  };
  var DEFAULT_ADV = JSON.parse(JSON.stringify(S.adv));

  var hooks = window.WizardHooks || {};
  var root, frame, stage, foot, tip, countEl, progressEl;
  var cur = 0, busy = false, attempted = {};

  function t(key, vars) {
    var s = (WORDS[S.lang] && WORDS[S.lang][key]) || (hooks.words && hooks.words[S.lang] && hooks.words[S.lang][key]) || WORDS.en[key] || key;
    if (vars) for (var k in vars) s = s.split('{' + k + '}').join(vars[k]);
    return s;
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function getPath(path) {
    return path.split('.').reduce(function (o, k) { return o == null ? o : o[k]; }, S);
  }
  function setPath(path, value) {
    var keys = path.split('.'), o = S;
    for (var i = 0; i < keys.length - 1; i++) o = o[keys[i]];
    o[keys[keys.length - 1]] = value;
  }
  function reduced() {
    return document.documentElement.classList.contains('reduce') || matchMedia('(prefers-reduced-motion: reduce)').matches;
  }
  if (params.get('motion') === '0') document.documentElement.classList.add('reduce');

  /* ---- small view helpers ---- */
  function info(key) {
    return '<button type="button" class="info" data-tip="' + key + '" aria-label="' + esc(t('more')) + '" aria-expanded="false"><span aria-hidden="true">i</span></button>';
  }
  function head(id, opts) {
    opts = opts || {};
    return '<header class="s-head st">' +
      '<div class="s-title-row"><h2 class="s-title" tabindex="-1">' + esc(t(id + '.title')) + '</h2>' +
      (opts.tag ? '<span class="tag">' + esc(opts.tag) + '</span>' : '') +
      (opts.tip ? info(opts.tip) : '') + '</div>' +
      '<p class="s-line">' + esc(t(id + '.line')) + '</p></header>';
  }
  function field(o) {
    var val = getPath(o.bind);
    var secret = o.secret;
    var shown = secret && S.reveal[o.bind];
    var kept = secret && S.kept[o.bind];
    return '<div class="field st" data-field="' + o.bind + '">' +
      '<label class="f-label" for="f-' + o.bind + '">' + esc(o.label) +
      (o.optional ? ' <span class="f-opt">' + esc(t('optional')) + '</span>' : '') +
      (o.badge ? ' <span class="f-badge">' + esc(o.badge) + '</span>' : '') +
      (o.tip ? info(o.tip) : '') + '</label>' +
      '<div class="f-box">' + (o.icon ? icon(o.icon, 'f-ic') : '') +
      '<input id="f-' + o.bind + '" data-k="' + o.bind + '" data-bind="' + o.bind + '"' +
      ' type="' + (secret && !shown ? 'password' : 'text') + '"' + (o.readonly ? ' readonly' : '') +
      (o.mode ? ' inputmode="' + o.mode + '"' : '') +
      ' value="' + esc(val) + '" placeholder="' + esc(kept ? kept + ' · ' + t('secret.kept') : o.ph || '') + '" autocomplete="off" spellcheck="false"' +
      ' aria-describedby="e-' + o.bind + '"' + (o.reset ? ' data-reset="' + o.reset + '"' : '') + '>' +
      (secret ? '<button type="button" class="eye" data-act="eye" data-k="eye-' + o.bind + '" data-target="' + o.bind + '" aria-label="' + esc(shown ? t('hide') : t('show')) + '" aria-pressed="' + (shown ? 'true' : 'false') + '">' + icon(shown ? 'eyeoff' : 'eye') + '</button>' : '') +
      '</div><p class="err" id="e-' + o.bind + '" data-err="' + o.bind + '" aria-live="polite"></p></div>';
  }
  function seg(bind, options, opts) {
    opts = opts || {};
    var v = getPath(bind);
    return '<div class="seg' + (opts.cls ? ' ' + opts.cls : '') + '" role="radiogroup"' + (opts.label ? ' aria-label="' + esc(opts.label) + '"' : '') + '>' +
      options.map(function (o) {
        var on = String(v) === String(o.value);
        return '<button type="button" role="radio" aria-checked="' + on + '" tabindex="' + (on ? 0 : -1) + '" data-k="' + bind + '-' + o.value + '" data-set="' + bind + '=' + o.value + '">' + (o.icon ? icon(o.icon) : '') + '<span>' + esc(o.label) + '</span></button>';
      }).join('') + '<i class="seg-thumb" aria-hidden="true"></i></div>';
  }
  function toggle(bind, label, tipKey, iconName) {
    var on = !!getPath(bind);
    return '<div class="row st">' + (iconName ? icon(iconName, 'row-ic') : '') + '<span class="row-label" id="l-' + bind + '">' + esc(label) + (tipKey ? info(tipKey) : '') + '</span>' +
      '<button type="button" class="switch" role="switch" aria-checked="' + on + '" aria-labelledby="l-' + bind + '" data-k="' + bind + '" data-set="' + bind + '=' + (!on) + '"><i></i></button></div>';
  }
  function mask(v) {
    v = String(v || '');
    return v.length > 4 ? '••••' + esc(v.slice(-4)) : '••••';
  }

  /* ---- the steps ---- */
  var VIEW = {
    welcome: function () {
      return '<div class="s-welcome">' +
        '<div class="hero-slot st" data-slot="hero" aria-hidden="true"></div>' +
        '<h2 class="s-title xl st" tabindex="-1">' + esc(t('welcome.title')) + '</h2>' +
        '<p class="s-line st">' + esc(t('welcome.line')) + '</p>' +
        '<div class="welcome-row st">' + seg('lang', [{ value: 'en', label: 'English' }, { value: 'ru', label: 'Русский' }], { cls: 'small', label: t('adv.lang') }) +
        '<span class="tag">' + esc(t('welcome.time')) + '</span></div></div>';
    },
    runs: function () {
      function opt(value, ic, extra) {
        return '<label class="opt st" data-k="mode-' + value + '"><input type="radio" name="mode" value="' + value + '" data-bind="mode" data-k="mode-' + value + '-in"' + (S.mode === value ? ' checked' : '') + '>' +
          '<span class="opt-ic">' + icon(ic) + '</span>' +
          '<span class="opt-text"><span class="opt-name">' + esc(t('runs.' + value)) + '</span><span class="opt-sub">' + esc(t('runs.' + value + '.sub')) + '</span></span>' +
          extra + '<span class="opt-dot" aria-hidden="true"></span></label>';
      }
      return head('runs', { tip: 'tip.runs' }) + '<div class="s-body"><div class="opts" role="radiogroup" aria-label="' + esc(t('runs.title')) + '">' +
        opt('native', 'laptop', '<span class="badge">' + esc(t('runs.rec')) + '</span>') +
        opt('docker', 'cube', '<span class="badge soft" data-k="docker-badge"><i class="dot ' + (S.docker ? 'ok' : 'off') + '"></i>' + esc(t(S.docker ? 'runs.found' : 'runs.missing')) + '</span>') +
        '</div></div>';
    },
    model: function () {
      var tabs = seg('kind', [
        { value: 'cloud', label: t('model.cloud'), icon: 'key' },
        { value: 'cli', label: t('model.cli'), icon: 'term' },
        { value: 'local', label: t('model.local'), icon: 'server' }
      ], { cls: 'tabs', label: t('model.title') });
      var body = '';
      if (S.kind === 'cloud') {
        var p = PROVIDERS.filter(function (x) { return x.id === S.provider; })[0];
        body = '<div class="provs st" role="radiogroup" aria-label="Provider">' + PROVIDERS.map(function (x) {
          return '<label class="prov" data-k="prov-' + x.id + '"><input type="radio" name="provider" value="' + x.id + '" data-bind="provider" data-refresh="panel" data-k="prov-' + x.id + '-in"' + (S.provider === x.id ? ' checked' : '') + '>' +
            '<span class="prov-mark" aria-hidden="true">' + x.name.charAt(0) + '</span><span class="prov-name">' + x.name + '</span></label>';
        }).join('') + '</div>' +
          field({ bind: 'keys.' + p.id, label: p.name + ' · ' + t('model.key'), secret: true, ph: p.prefix ? p.prefix + '…' : t('model.key.ph'), tip: 'tip.key' });
      } else if (S.kind === 'cli') {
        body = '<div class="clis st" role="radiogroup" aria-label="' + esc(t('model.cli')) + '">' + CLIS.map(function (c) {
          var found = S.clis[c.id];
          return '<label class="cli' + (found ? '' : ' missing') + '" data-k="cli-' + c.id + '"><input type="radio" name="cli" value="' + c.id + '" data-bind="cli" data-k="cli-' + c.id + '-in"' + (S.cli === c.id ? ' checked' : '') + (found ? '' : ' disabled') + '>' +
            icon('term', 'cli-ic') + '<span class="cli-name">' + c.name + '</span>' +
            '<span class="cli-state"><i class="dot ' + (S.scanning ? 'busy' : found ? 'ok' : 'off') + '"></i>' + esc(S.scanning ? t('model.scanning') : found ? t('model.signed') : t('model.missing')) + '</span></label>';
        }).join('') + '</div>' +
          '<div class="cli-foot st"><button type="button" class="btn ghost small" data-act="rescan" data-k="rescan"' + (S.scanning ? ' disabled' : '') + '>' + icon('refresh', S.scanning ? 'spin' : '') + '<span>' + esc(t('model.rescan')) + '</span></button>' + info('tip.cli') + '</div>' +
          '<p class="err" data-err="cli" aria-live="polite"></p>';
      } else {
        var L = S.local;
        var status = '';
        if (L.test === 'busy') status = '<span class="status busy"><i class="dot busy"></i>' + esc(t('model.testing')) + '</span>';
        else if (L.test === 'ok') status = '<span class="status ok"><i class="dot ok"></i>' + esc(t('model.ok', { n: L.models.length, ms: L.ms })) + '</span>';
        else if (L.test === 'fail') status = '<span class="status bad" title="' + esc(L.error || '') + '"><i class="dot bad"></i>' + esc(t('model.fail')) + '</span>';
        body = '<div class="grid2">' + field({ bind: 'local.url', label: t('model.url'), ph: 'http://127.0.0.1:11434/v1', tip: 'tip.local', reset: 'test' }) +
          field({ bind: 'local.key', label: t('model.lkey'), optional: true, secret: true }) + '</div>' +
          '<div class="field st" data-field="local.model"><div class="f-row"><label class="f-label" for="f-local.model">' + esc(t('model.lmodel')) + '</label>' + status + '</div>' +
          '<div class="test-row"><div class="f-box select"><select id="f-local.model" data-bind="local.model" data-k="local.model"' + (L.models.length ? '' : ' disabled') + '>' +
          (L.models.length ? L.models.map(function (m) { return '<option' + (m === L.model ? ' selected' : '') + '>' + m + '</option>'; }).join('') : '<option>' + esc(t('model.pick')) + '</option>') +
          '</select>' + icon('down', 'sel-ic') + '</div>' +
          '<button type="button" class="btn test" data-act="test" data-k="test" aria-label="' + esc(t('model.test')) + '"' + (L.test === 'busy' ? ' disabled' : '') + '>' + icon(L.test === 'busy' ? 'refresh' : 'plug', L.test === 'busy' ? 'spin' : '') + '<span>' + esc(t('model.test')) + '</span></button></div>' +
          '<p class="err" data-err="local.model" aria-live="polite"></p></div>';
      }
      return head('model') + '<div class="s-body"><div class="st">' + tabs + '</div><div class="panel" data-panel>' + body + '</div></div>';
    },
    limit: function () {
      var n = Number(S.limit) || 0;
      return head('limit', { tip: 'tip.limit' }) + '<div class="s-body limit">' +
        '<div class="limit-big st" data-field="limit"><span class="cur">$</span>' +
        '<input class="limit-num" data-bind="limit" data-k="limit" inputmode="decimal" aria-label="' + esc(t('limit.field')) + '" aria-describedby="e-limit" value="' + esc(S.limit) + '" style="width:' + numWidth(S.limit) + 'ch">' +
        '<span class="per">' + esc(t('limit.per')) + '</span></div>' +
        '<input class="range st" type="range" min="1" max="200" step="1" data-bind="limit" data-k="limit-range" aria-label="' + esc(t('limit.field')) + '" value="' + Math.min(200, Math.max(1, n || 1)) + '" style="--v:' + (Math.min(200, Math.max(1, n || 1)) - 1) / 199 + '">' +
        '<div class="presets st">' + [5, 20, 50, 100].map(function (v) {
          return '<button type="button" class="chip" data-k="preset-' + v + '" data-set="limit=' + v + '" aria-pressed="' + (n === v) + '">$' + v + '</button>';
        }).join('') + '</div>' +
        '<p class="err center" id="e-limit" data-err="limit" aria-live="polite"></p></div>';
    },
    telegram: function () {
      return head('telegram', { tag: t('optional'), tip: 'tip.tg' }) + '<div class="s-body">' +
        '<div class="grid2">' + field({ bind: 'tg.token', label: t('tg.token'), secret: true, ph: '123456:ABC…', badge: '@BotFather', icon: 'send' }) +
        field({ bind: 'tg.owner', label: t('tg.owner'), mode: 'numeric', ph: '12345678', badge: '@userinfobot' }) + '</div>' +
        '<button type="button" class="disclose st" data-act="tgmore" data-k="tgmore" aria-expanded="' + S.tg.more + '">' + icon('chevron') + '<span>' + esc(t('tg.more')) + '</span><span class="f-opt">' + esc(t('optional')) + '</span></button>' +
        (S.tg.more ? '<div class="grid2 more">' + field({ bind: 'tg.apiid', label: t('tg.apiid'), mode: 'numeric' }) + field({ bind: 'tg.apihash', label: t('tg.apihash'), secret: true }) + '</div>' : '') +
        '</div>';
    },
    advanced: function () {
      var A = S.adv;
      var out = head('advanced') + '<div class="s-body">' +
        '<button type="button" class="disclose big st" data-act="adv" data-k="adv" aria-expanded="' + A.open + '">' + icon('sliders') + '<span>' + esc(A.open ? t('adv.hide') : t('adv.show')) + '</span>' + icon('down', 'dis-arrow') + '</button>';
      if (!A.open) {
        out += '<ul class="defaults st" aria-label="' + esc(t('sum.defaults')) + '">' +
          '<li>' + icon('folder') + '<span>' + esc(A.data) + '</span></li>' +
          '<li>' + icon('plug') + '<span>' + esc(t('adv.ports')) + ' · ' + esc(A.portsAuto ? t('auto') + ' · ' + A.port : A.port) + '</span></li>' +
          '<li>' + icon('globe') + '<span>' + (S.lang === 'ru' ? 'Русский' : 'English') + '</span></li>' +
          '<li>' + icon('power') + '<span>' + esc(t('adv.login')) + ' · ' + esc(A.login ? t('on') : t('off')) + '</span></li>' +
          '<li>' + icon('browser') + '<span>' + esc(t('adv.browser')) + ' · ' + esc(A.browser ? t('on') : t('off')) + '</span></li>' +
          '<li>' + icon('mic') + '<span>' + esc(t('adv.voice')) + ' · ' + esc(t('voice.' + voiceValue())) + '</span></li></ul>';
      } else {
        out += '<div class="adv">' +
          // The folder is where this launcher was started on, and moving it is the import
          // command's work, not a field's: shown read-only, with how to move it in the tip.
          '<div class="row st" data-field="adv.data">' + icon('folder', 'row-ic') + '<label class="row-label" for="f-adv.data">' + esc(t('adv.data')) + info('tip.data') + '</label>' +
          '<span class="f-box"><input id="f-adv.data" data-k="adv.data" value="' + esc(A.data) + '" readonly spellcheck="false" autocomplete="off" aria-describedby="e-adv.data" title="' + esc(A.data) + '"></span></div>' +
          '<p class="err right" id="e-adv.data" data-err="adv.data" aria-live="polite"></p>' +
          '<div class="row st">' + icon('plug', 'row-ic') + '<span class="row-label" id="l-adv.portsAuto">' + esc(t('adv.ports')) + info('tip.ports') + '</span>' +
          (A.portsAuto ? '' : '<span class="port-wrap" data-field="adv.port"><input class="port" data-bind="adv.port" data-k="adv.port" inputmode="numeric" aria-label="' + esc(t('adv.port')) + '" aria-describedby="e-adv.port" value="' + esc(A.port) + '"></span>') +
          '<span class="row-note">' + esc(t('adv.auto')) + '</span><button type="button" class="switch" role="switch" aria-checked="' + A.portsAuto + '" aria-labelledby="l-adv.portsAuto" data-k="adv.portsAuto" data-set="adv.portsAuto=' + (!A.portsAuto) + '"><i></i></button></div>' +
          '<p class="err right" id="e-adv.port" data-err="adv.port" aria-live="polite"></p>' +
          '<div class="row st">' + icon('globe', 'row-ic') + '<span class="row-label">' + esc(t('adv.lang')) + '</span>' + seg('lang', [{ value: 'en', label: 'EN' }, { value: 'ru', label: 'RU' }], { cls: 'small', label: t('adv.lang') }) + '</div>' +
          toggle('adv.login', t('adv.login'), 'tip.login', 'power') +
          toggle('adv.browser', t('adv.browser'), 'tip.browser', 'browser') +
          '<div class="row st">' + icon('mic', 'row-ic') + '<span class="row-label">' + esc(t('adv.voice')) + info('tip.voice') + '</span>' + seg('adv.voice', voiceOptions(), { cls: 'small', label: t('adv.voice') }) + '</div>' +
          '<p class="err right" id="e-adv.voice" data-err="adv.voice" aria-live="polite"></p>' +
          '</div>';
      }
      return out + '</div>';
    },
    summary: function () {
      function row(step, ic, value) {
        return '<li class="st"><button type="button" class="sum-row" data-act="edit" data-step="' + step + '" data-k="edit-' + step + '">' +
          icon(ic, 'sum-ic') + '<span class="sum-k">' + esc(t('name.' + step)) + '</span><span class="sum-v">' + value + '</span>' + icon('edit', 'sum-edit') + '</button></li>';
      }
      return head('summary') + '<div class="s-body"><ul class="sum">' +
        row('runs', S.mode === 'native' ? 'laptop' : 'cube', esc(t('runs.' + S.mode))) +
        row('model', S.kind === 'cloud' ? 'key' : S.kind === 'cli' ? 'term' : 'server', modelSummary()) +
        row('limit', 'coin', esc(t('sum.perday', { v: S.limit }))) +
        row('telegram', 'send', S.tg.skip || !(S.tg.token || S.kept['tg.token']) ? '<em>' + esc(t('sum.skipped')) + '</em>' : 'bot ' + (S.tg.token ? mask(S.tg.token) : esc(S.kept['tg.token'])) + ' · id ' + esc(S.tg.owner)) +
        row('advanced', 'sliders', advChanged() ? esc(t('sum.changed', { n: advChanged() })) : '<em>' + esc(t('sum.defaults')) + '</em>') +
        '</ul></div>';
    },
    start: function () {
      var L = S.launch || { i: 0, done: false };
      var list = [1, 2, 3, 4].map(function (n) {
        var st = L.done || L.i > n - 1 ? 'done' : L.i === n - 1 ? 'now' : '';
        return '<li class="' + st + '"><span class="l-mark">' + icon('check') + '</span><span>' + esc(t('start.' + n)) + '</span></li>';
      }).join('');
      return '<div class="launch' + (L.done ? ' is-done' : '') + '">' +
        '<div class="launch-slot st" data-slot="launch" aria-hidden="true"></div>' +
        '<h2 class="s-title st" tabindex="-1">' + esc(L.done ? t('start.done') : t('start.title')) + '</h2>' +
        '<ol class="launch-steps st" aria-live="polite">' + list + '</ol>' +
        '<div class="launch-bar st" aria-hidden="true"><i style="--p:' + (L.done ? 1 : L.i / 4) + '"></i></div>' +
        '<p class="launch-live st" aria-hidden="true">' + esc(L.done ? '' : L.live || '') + '</p>' +
        (L.error ? '<p class="err on center" role="alert">' + esc(L.error) + '</p>' : '') +
        '<div class="launch-cta st">' + (L.done ? '<button type="button" class="btn primary big" data-act="open" data-k="open">' + esc(t('start.open')) + icon('arrow') + '</button>' :
          L.error ? '<button type="button" class="btn ghost" data-act="retry" data-k="retry">' + icon('back') + '<span>' + esc(t('nav.summary')) + '</span></button>' : '') + '</div>' +
        '</div>';
    }
  };

  // Local speech is a piece of the native runtime; a container has no such piece to install.
  function voiceOptions() {
    var out = [{ value: 'off', label: t('voice.off') }];
    if (S.mode !== 'docker') out.push({ value: 'local', label: t('voice.local') });
    out.push({ value: 'cloud', label: t('voice.cloud') });
    return out;
  }
  function voiceValue() { return S.mode === 'docker' && S.adv.voice === 'local' ? 'off' : S.adv.voice; }
  function numWidth(v) { return (Math.max(1, String(v).length) + .1).toFixed(1); }
  function modelSummary() {
    if (S.kind === 'cloud') {
      var p = PROVIDERS.filter(function (x) { return x.id === S.provider; })[0];
      return esc(p.name) + ' · ' + (S.keys[p.id] ? mask(S.keys[p.id]) : esc(S.kept['keys.' + p.id] || ''));
    }
    if (S.kind === 'cli') return esc(CLIS.filter(function (c) { return c.id === S.cli; })[0].name) + ' · CLI';
    return esc(S.local.model || '—') + ' · ' + esc(hostOf(S.local.url));
  }
  function hostOf(u) { try { return new URL(u).host; } catch (e) { return u; } }
  function advChanged() {
    var n = 0;
    for (var k in DEFAULT_ADV) if (k !== 'open' && !(k === 'port' && S.adv.portsAuto) && String(DEFAULT_ADV[k]) !== String(S.adv[k])) n++;
    return n;
  }

  /* ---- validation ---- */
  function validate(id) {
    var e = {};
    if (id === 'model') {
      if (S.kind === 'cloud') {
        var p = PROVIDERS.filter(function (x) { return x.id === S.provider; })[0];
        var k = (S.keys[p.id] || '').trim();
        if (!k) { if (!S.kept['keys.' + p.id]) e['keys.' + p.id] = t('err.required'); }
        else if (p.prefix && k.indexOf(p.prefix) !== 0) e['keys.' + p.id] = t('err.prefix', { p: p.prefix });
        else if (k.length < 16) e['keys.' + p.id] = t('err.short');
      } else if (S.kind === 'cli') {
        if (!S.clis[S.cli]) e.cli = t('err.cli');
      } else {
        if (!/^https?:\/\/[^\s/]+/i.test(S.local.url.trim())) e['local.url'] = t('err.url');
        else if (S.local.test !== 'ok' && S.local.test !== 'kept') e['local.model'] = t('err.test');
      }
    }
    if (id === 'limit') {
      var v = Number(String(S.limit).replace(',', '.'));
      if (!(v >= 1 && v <= 1000)) e.limit = t('err.limit');
    }
    if (id === 'telegram' && !tgEmpty()) {
      if ((S.tg.token.trim() || !S.kept['tg.token']) && !/^\d{5,12}:[\w-]{20,}$/.test(S.tg.token.trim())) e['tg.token'] = t('err.token');
      if (!/^\d{3,15}$/.test(S.tg.owner.trim())) e['tg.owner'] = S.tg.owner.trim() ? t('err.digits') : t('err.required');
      if (S.tg.apiid && !/^\d+$/.test(S.tg.apiid.trim())) e['tg.apiid'] = t('err.digits');
    }
    if (id === 'advanced' && S.adv.open) {
      if (voiceValue() === 'cloud' && !S.keys.openai.trim() && !S.kept['keys.openai']) e['adv.voice'] = t('err.voice');
      if (!S.adv.portsAuto) {
        var port = Number(S.adv.port);
        if (!(port >= 1024 && port <= 65535) || !/^\d+$/.test(S.adv.port)) e['adv.port'] = t('err.port');
      }
    }
    return Object.keys(e).length ? e : null;
  }
  function tgEmpty() {
    return !S.tg.token.trim() && !S.kept['tg.token'] && !S.tg.owner.trim() && !S.tg.apiid.trim() && !S.tg.apihash.trim();
  }
  function showErrors(errs) {
    var step = currentEl();
    if (!step) return;
    step.querySelectorAll('[data-err]').forEach(function (p) {
      var msg = errs && errs[p.getAttribute('data-err')];
      p.textContent = msg || '';
      p.classList.toggle('on', !!msg);
    });
    step.querySelectorAll('[data-field]').forEach(function (f) {
      var bad = !!(errs && errs[f.getAttribute('data-field')]);
      f.classList.toggle('invalid', bad);
      var input = f.querySelector('input,select');
      if (input) input.setAttribute('aria-invalid', bad ? 'true' : 'false');
    });
  }

  /* ---- step changes ---- */
  function currentEl() { return stage.querySelector('.step.current'); }
  function build(i) {
    var el = document.createElement('section');
    el.className = 'step step-' + STEPS[i];
    el.setAttribute('aria-label', t('name.' + STEPS[i]));
    el.innerHTML = VIEW[STEPS[i]]();
    stagger(el);
    return el;
  }
  function stagger(el) {
    el.querySelectorAll('.st').forEach(function (n, k) { n.style.setProperty('--i', k); });
  }
  function stepTime() {
    var v = parseFloat(getComputedStyle(root).getPropertyValue('--t-step'));
    return reduced() ? 220 : (v || 700);
  }

  function go(i, opts) {
    opts = opts || {};
    if (i < 0 || i >= STEPS.length || (busy && !opts.force)) return;
    var dir = i === cur ? 0 : i > cur ? 1 : -1;
    var old = currentEl();
    closeTip();
    cur = i;
    var el = build(i);
    root.dataset.step = STEPS[i];
    root.style.setProperty('--step-i', i);
    root.dataset.dir = dir;
    if (!old || opts.instant) {
      if (old) old.remove();
      el.classList.add('current');
      stage.appendChild(el);
    } else {
      busy = true;
      el.dataset.dir = dir; old.dataset.dir = dir;
      el.classList.add('current', 'entering');
      old.classList.remove('current');
      old.classList.add('leaving');
      old.setAttribute('aria-hidden', 'true');
      old.inert = true;
      stage.appendChild(el);
      var done = false;
      var finish = function () {
        if (done) return; done = true;
        old.remove();
        el.classList.remove('entering');
        busy = false;
      };
      setTimeout(finish, stepTime());
    }
    renderChrome();
    if (hooks.onStep) hooks.onStep({ index: i, id: STEPS[i], dir: dir, state: S, el: el, count: STEPS.length });
    var focusTarget = el.querySelector('[data-autofocus]') || el.querySelector('.s-title');
    if (focusTarget && !opts.keepFocus) focusTarget.focus({ preventScroll: true });
    if (STEPS[i] === 'start' && !S.launch) launch();
  }

  function refresh(opts) {
    opts = opts || {};
    var el = currentEl();
    if (!el) return;
    var active = document.activeElement && document.activeElement.getAttribute && document.activeElement.getAttribute('data-k');
    var caret = null;
    if (document.activeElement && document.activeElement.tagName === 'INPUT') {
      try { caret = document.activeElement.selectionStart; } catch (e) { caret = null; }
    }
    var scroll = el.scrollTop;
    el.innerHTML = VIEW[STEPS[cur]]();
    el.classList.add('static');
    stagger(el);
    el.scrollTop = scroll;
    if (opts.pop) {
      var p = el.querySelector(opts.pop);
      if (p) { p.classList.remove('pop'); void p.offsetWidth; p.classList.add('pop'); }
    }
    if (active) {
      var n = el.querySelector('[data-k="' + active + '"]');
      if (n) {
        n.focus({ preventScroll: true });
        if (caret != null && n.setSelectionRange) try { n.setSelectionRange(caret, caret); } catch (e) { /* not a text input */ }
      }
    }
    if (attempted[STEPS[cur]]) showErrors(validate(STEPS[cur]));
    renderChrome();
    if (hooks.onRefresh) hooks.onRefresh({ index: cur, id: STEPS[cur], state: S, el: el });
  }

  function renderChrome() {
    var id = STEPS[cur];
    countEl.textContent = t('step.of', { n: String(cur + 1).padStart(2, '0'), t: String(STEPS.length).padStart(2, '0') });
    var nameEl = root.querySelector('[data-slot=name]');
    if (nameEl) nameEl.textContent = t('name.' + id);
    progressEl.innerHTML = STEPS.map(function (s, k) {
      return '<li class="' + (k < cur ? 'done' : k === cur ? 'now' : '') + '" style="--k:' + k + '"><span class="p-dot"></span><span class="p-name">' + esc(t('name.' + s)) + '</span></li>';
    }).join('');
    progressEl.style.setProperty('--p', cur / (STEPS.length - 1));
    var back = foot.querySelector('[data-act=back]');
    var skip = foot.querySelector('[data-act=skip]');
    var next = foot.querySelector('[data-act=next]');
    back.hidden = cur === 0 || id === 'start';
    // Skipping is for a Telegram that is not set up; one that is stays as it is when left alone.
    skip.hidden = id !== 'telegram' || !!S.returnTo || !!S.kept['tg.token'];
    next.hidden = id === 'start';
    foot.hidden = id === 'start';
    root.classList.toggle('on-start', id === 'start');
    back.querySelector('span').textContent = t('nav.back');
    skip.querySelector('span').textContent = t('nav.skip');
    var label = id === 'welcome' ? t('nav.begin') : id === 'summary' ? t('nav.start') : S.returnTo ? t('nav.summary') : t('nav.next');
    next.querySelector('span').textContent = label;
    next.classList.toggle('go', id === 'summary');
    document.querySelectorAll('[data-slot=lang] button').forEach(function (b) {
      var on = b.getAttribute('data-lang') === S.lang;
      b.setAttribute('aria-pressed', on);
    });
    document.documentElement.lang = S.lang;
    document.title = t('brand') + ' · ' + t('name.' + id);
  }

  function next() {
    if (busy) return;
    var id = STEPS[cur];
    if (id === 'start') return;
    var errs = validate(id);
    if (errs) {
      attempted[id] = true;
      showErrors(errs);
      var el = currentEl();
      var first = el && el.querySelector('.invalid input, .invalid select, [data-err].on');
      if (first && first.focus && first.tagName !== 'P') first.focus();
      frame.classList.remove('nope'); void frame.offsetWidth; frame.classList.add('nope');
      if (hooks.onInvalid) hooks.onInvalid({ id: id, errors: errs, state: S });
      return;
    }
    if (id === 'telegram') S.tg.skip = tgEmpty();
    if (S.returnTo && id !== 'summary') {
      S.returnTo = null;
      go(STEPS.indexOf('summary'));
      return;
    }
    go(cur + 1);
  }
  function back() {
    if (busy || cur === 0 || STEPS[cur] === 'start') return;
    if (S.returnTo) { S.returnTo = null; go(STEPS.indexOf('summary')); return; }
    go(cur - 1);
  }
  function skip() {
    if (STEPS[cur] !== 'telegram') return;
    S.tg.skip = true;
    S.tg.token = S.tg.owner = S.tg.apiid = S.tg.apihash = '';
    attempted.telegram = false;
    go(cur + 1);
  }

  /* ---- the launcher's side ---- */
  // Every call that changes or runs something carries the page's token (desktop/server.go).
  function call(url, body) {
    var json = body && !(body instanceof URLSearchParams);
    var headers = { 'X-Daedalus-Desktop': CSRF };
    if (json) headers['Content-Type'] = 'application/json';
    return fetch(url, { method: 'POST', headers: headers, body: json ? JSON.stringify(body) : body, credentials: 'same-origin', cache: 'no-store' })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (b) {
          if (!r.ok) throw new Error(b.error || r.statusText || String(r.status));
          return b;
        });
      });
  }
  function testLocal() {
    var url = S.local.url.trim();
    if (!/^https?:\/\/[^\s/]+/i.test(url)) {
      attempted.model = true; showErrors({ 'local.url': t('err.url') }); return;
    }
    S.local.test = 'busy'; S.local.models = []; S.local.model = ''; S.local.error = '';
    refresh();
    if (hooks.onChange) hooks.onChange('local.test', S);
    // The launcher asks the endpoint, not the page: the page may talk to its own origin only.
    call('/api/setup/endpoint', { url: url, key: S.local.key.trim() }).then(function (p) { return p; }, function (e) {
      return { ok: false, error: e.message, models: [] };
    }).then(function (p) {
      var ok = !!(p && p.ok && p.models && p.models.length);
      S.local.test = ok ? 'ok' : 'fail';
      S.local.ms = ok ? p.ms : 0;
      S.local.models = ok ? p.models.slice() : [];
      S.local.model = ok ? (p.models.indexOf(BL.model) >= 0 ? BL.model : p.models[0]) : '';
      S.local.error = ok ? '' : (p && p.error) || '';
      if (ok && p.url) S.local.url = p.url;
      if (STEPS[cur] === 'model') refresh({ pop: '.f-row .status' });
      if (hooks.onChange) hooks.onChange('local.test', S);
    });
  }
  function rescan() {
    S.scanning = true; refresh();
    call('/api/setup/detect', {}).then(function (d) {
      if (d.clis) S.clis = d.clis;
      S.docker = !!d.docker;
    }, function () { /* the last answer stands */ }).then(function () {
      S.scanning = false;
      if (STEPS[cur] === 'model') refresh({ pop: '.clis' });
    });
  }
  // The answers as the launcher's form takes them. A secret field left empty is not sent, which
  // keeps the stored value; Telegram skipped sends nothing, which leaves it as it was.
  function answers() {
    var f = new URLSearchParams();
    f.set('csrf', CSRF); f.set('lang', S.lang); f.set('mode', S.mode); f.set('kind', S.kind);
    f.set('handoff', '1'); f.set('start', '1');
    PROVIDERS.forEach(function (p) { var k = (S.keys[p.id] || '').trim(); if (k) f.set(p.id, k); });
    if (S.kind === 'local') {
      f.set('local_url', S.local.url.trim()); f.set('local_key', S.local.key.trim()); f.set('local_model', S.local.model);
    }
    f.set('usd_per_day', String(S.limit).replace(',', '.'));
    if (!S.tg.skip) {
      [['bot_token', S.tg.token], ['owner_id', S.tg.owner], ['api_id', S.tg.apiid], ['api_hash', S.tg.apihash]].forEach(function (pair) {
        if (String(pair[1] || '').trim()) f.set(pair[0], String(pair[1]).trim());
      });
    }
    f.set('login', S.adv.login ? 'on' : 'off');
    f.set('browser', S.adv.browser ? 'on' : 'off');
    f.set('voice', voiceValue());
    if (!S.adv.portsAuto) f.set('port', S.adv.port.trim());
    else if (BA.portsAuto === false) f.set('port', '');
    return f;
  }
  // The four lines follow the launcher's own start: written, keys put away, the runtime, the agent.
  function launch() {
    S.launch = { i: 0, done: false, error: '', live: '' };
    var seenBusy = false, began = Date.now();
    function advance(i) {
      if (!S.launch || i <= S.launch.i) return;
      S.launch.i = i;
      if (hooks.onLaunch) hooks.onLaunch({ i: Math.min(i, 3), p: i / 4, state: S });
      if (i >= 4) {
        S.launch.done = true;
        refresh({ pop: '.launch-cta' });
        var b = currentEl().querySelector('[data-act=open]');
        if (b) b.focus({ preventScroll: true });
        if (hooks.onReady) hooks.onReady({ state: S });
        return;
      }
      if (STEPS[cur] === 'start') refresh();
    }
    function poll() {
      if (!S.launch || S.launch.done || STEPS[cur] !== 'start') return;
      fetch('/api/status', { cache: 'no-store', credentials: 'same-origin' }).then(function (r) { return r.json(); }).then(function (st) {
        if (!S.launch) return;
        if (st.busy) seenBusy = true;
        var log = st.log || [];
        var live = log.length ? String(log[log.length - 1]) : '';
        if (live !== S.launch.live) { S.launch.live = live; var p = currentEl() && currentEl().querySelector('.launch-live'); if (p) p.textContent = live; }
        if (!st.busy && st.failure && seenBusy) { location.href = '/progress'; return; }
        var steps = st.steps || [];
        if (st.busy && (st.stage === steps[steps.length - 1] || /^(restart|installing)/.test(st.busy))) advance(3);
        if (!st.busy && ((seenBusy && !st.failure) || (st.running > 0 && Date.now() - began > 8000))) { advance(3); advance(4); return; }
        setTimeout(poll, 800);
      }, function () { setTimeout(poll, 1500); });
    }
    if (hooks.onLaunch) hooks.onLaunch({ i: 0, p: 0, state: S });
    call('/setup', answers()).then(function () {
      advance(1);
      setTimeout(function () { advance(2); poll(); }, reduced() ? 120 : 600);
    }, function (e) {
      if (!S.launch) return;
      S.launch.error = t('err.save', { e: e.message });
      if (STEPS[cur] === 'start') refresh();
      if (hooks.onInvalid) hooks.onInvalid({ id: 'start', errors: { start: e.message }, state: S });
    });
  }
  function openApp() {
    document.body.classList.add('handoff');
    if (hooks.onOpen) hooks.onOpen({ state: S });
    // The launcher opens the app where the launcher is shown: in the application's window it
    // replaces this page, without one it opens a browser window.
    call('/api/action/open', null).catch(function () { /* the card below still says where it is */ });
    var card = document.querySelector('.handoff-card');
    if (!card) {
      card = document.createElement('div');
      card.className = 'handoff-card';
      card.setAttribute('role', 'status');
      document.body.appendChild(card);
    }
    card.innerHTML = '<div class="h-mark" aria-hidden="true"></div><h2>' + esc(t('handoff.title')) + '</h2><p>' + esc(t('handoff.line')) + '</p>';
    setTimeout(function () { card.classList.add('on'); }, reduced() ? 50 : (hooks.handoffDelay || 1100));
  }
  function persistLang() {
    call('/api/lang', { lang: S.lang }).catch(function () { /* the form carries it as well */ });
  }

  /* ---- tooltip ---- */
  var tipOwner = null, tipPinned = false;
  function openTip(btn, pinned) {
    if (tipOwner === btn) { if (pinned) tipPinned = true; return; }
    closeTip();
    tipOwner = btn; tipPinned = !!pinned;
    tip.textContent = t(btn.getAttribute('data-tip'));
    tip.hidden = false;
    btn.setAttribute('aria-expanded', 'true');
    btn.setAttribute('aria-describedby', 'wz-tip');
    // The page zooms the wizard on a large screen: the rectangles come back in window pixels,
    // the tip's offsets are in the frame's own, so everything is divided back by the zoom.
    var z = frame.currentCSSZoom || 1;
    var fr = frame.getBoundingClientRect(), b = btn.getBoundingClientRect();
    var fw = fr.width / z, fh = fr.height / z;
    var bl = (b.left - fr.left) / z, bw = b.width / z, bt = (b.top - fr.top) / z, bb = (b.bottom - fr.top) / z;
    var w = Math.min(280, fw - 24);
    tip.style.width = w + 'px';
    var left = Math.max(12, Math.min(bl + bw / 2 - w / 2, fw - w - 12));
    var top = bb + 8;
    tip.style.left = left + 'px';
    tip.style.top = top + 'px';
    tip.style.setProperty('--ax', (bl + bw / 2 - left) + 'px');
    var th = tip.offsetHeight;
    if (top + th > fh - 8) { tip.style.top = (bt - th - 8) + 'px'; tip.classList.add('above'); } else tip.classList.remove('above');
    tip.classList.remove('on'); void tip.offsetWidth; tip.classList.add('on');
  }
  function closeTip() {
    if (!tipOwner) return;
    tipOwner.setAttribute('aria-expanded', 'false');
    tipOwner.removeAttribute('aria-describedby');
    tipOwner = null; tipPinned = false;
    tip.hidden = true; tip.classList.remove('on');
  }

  /* ---- events ---- */
  function coerce(v) { return v === 'true' ? true : v === 'false' ? false : v; }
  function onClick(e) {
    var infoBtn = e.target.closest('.info');
    if (infoBtn) { e.preventDefault(); if (tipOwner === infoBtn && tipPinned) closeTip(); else openTip(infoBtn, true); return; }
    if (tipOwner && !e.target.closest('.tip')) closeTip();
    var set = e.target.closest('[data-set]');
    if (set) {
      var parts = set.getAttribute('data-set').split('=');
      var path = parts[0], value = coerce(parts[1]);
      setPath(path, value);
      if (path === 'limit') S.limit = String(value);
      var pop = path === 'kind' ? '.panel' : path === 'adv.portsAuto' ? '.adv' : null;
      if (path === 'lang') { renderAll(); persistLang(); } else refresh({ pop: pop });
      if (hooks.onChange) hooks.onChange(path, S);
      return;
    }
    var act = e.target.closest('[data-act]');
    if (!act) return;
    var a = act.getAttribute('data-act');
    if (a === 'next') next();
    else if (a === 'back') back();
    else if (a === 'skip') skip();
    else if (a === 'eye') { var tg = act.getAttribute('data-target'); S.reveal[tg] = !S.reveal[tg]; refresh(); var inp = currentEl().querySelector('[data-k="' + tg + '"]'); if (inp) inp.focus(); }
    else if (a === 'test') testLocal();
    else if (a === 'rescan') rescan();
    else if (a === 'tgmore') { S.tg.more = !S.tg.more; refresh({ pop: '.more' }); }
    else if (a === 'adv') { S.adv.open = !S.adv.open; refresh({ pop: S.adv.open ? '.adv' : '.defaults' }); if (hooks.onChange) hooks.onChange('adv.open', S); }
    else if (a === 'edit') { S.returnTo = 'summary'; go(STEPS.indexOf(act.getAttribute('data-step'))); }
    else if (a === 'open') openApp();
    else if (a === 'retry') { S.launch = null; go(STEPS.indexOf('summary'), { force: true }); }
    else if (a === 'lang') { S.lang = act.getAttribute('data-lang'); renderAll(); persistLang(); if (hooks.onChange) hooks.onChange('lang', S); }
  }
  function onInput(e) {
    var el = e.target.closest('[data-bind]');
    if (!el) return;
    var path = el.getAttribute('data-bind');
    var v = el.type === 'checkbox' ? el.checked : el.value;
    if (el.type === 'radio' && !el.checked) return;
    setPath(path, v);
    if (path === 'limit') {
      var step = currentEl();
      var n = Number(String(v).replace(',', '.'));
      var num = step.querySelector('.limit-num');
      if (el.type === 'range' && num) num.value = v;
      if (num) num.style.width = numWidth(num.value) + 'ch';
      if (el.type !== 'range') { var rg = step.querySelector('.range'); if (rg && n >= 1) { rg.value = Math.min(200, n); } }
      var r = step.querySelector('.range');
      if (r) r.style.setProperty('--v', (Number(r.value) - 1) / 199);
      step.querySelectorAll('.presets .chip').forEach(function (c) { c.setAttribute('aria-pressed', String('limit=' + n === c.getAttribute('data-set'))); });
    }
    if (el.getAttribute('data-reset') === 'test' && S.local.test) {
      S.local.test = null; S.local.models = []; S.local.model = '';
      var st = currentEl().querySelector('.f-row .status'); if (st) st.remove();
      var sel = currentEl().querySelector('select[data-bind="local.model"]');
      if (sel) { sel.disabled = true; sel.innerHTML = '<option>' + esc(t('model.pick')) + '</option>'; }
    }
    if (el.getAttribute('data-refresh') === 'panel' || el.type === 'radio') {
      if (el.getAttribute('data-refresh') === 'panel') refresh({ pop: '[data-field]' });
    }
    if (attempted[STEPS[cur]]) showErrors(validate(STEPS[cur]));
    if (hooks.onChange) hooks.onChange(path, S);
  }
  function onKey(e) {
    var tag = e.target.tagName;
    if (e.key === 'Escape') {
      if (tipOwner) { var o = tipOwner; closeTip(); o.focus(); e.preventDefault(); return; }
      if (document.body.classList.contains('handoff')) return;
      e.preventDefault(); back(); return;
    }
    if (document.body.classList.contains('handoff')) return;
    var group = e.target.closest && e.target.closest('[role=radiogroup].seg, [role=tablist]');
    if (group && (e.key === 'ArrowLeft' || e.key === 'ArrowRight' || e.key === 'ArrowUp' || e.key === 'ArrowDown')) {
      var items = Array.prototype.slice.call(group.querySelectorAll('button'));
      var k = items.indexOf(e.target);
      if (k >= 0) {
        e.preventDefault();
        var d = e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? -1 : 1;
        var target = items[(k + d + items.length) % items.length];
        var key = target.getAttribute('data-k');
        target.click();
        var again2 = document.querySelector('[data-k="' + key + '"]');
        if (again2) again2.focus();
      }
      return;
    }
    if (e.key === 'Enter') {
      if (e.isComposing || tag === 'BUTTON' || tag === 'A' || tag === 'TEXTAREA' || tag === 'SELECT' || e.target.closest('.handoff-card')) return;
      if (e.target.closest && !e.target.closest('[data-wizard]') && tag !== 'BODY') return;
      e.preventDefault(); next(); return;
    }
    if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') {
      if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA' || (e.target.closest && e.target.closest('[role=radiogroup]'))) return;
      if (e.altKey || e.ctrlKey || e.metaKey) return;
      e.preventDefault();
      if (e.key === 'ArrowRight') next(); else back();
    }
  }

  function renderAll() {
    var el = currentEl();
    if (el) { el.setAttribute('aria-label', t('name.' + STEPS[cur])); }
    refresh();
    if (hooks.onLang) hooks.onLang(S);
  }

  function mount() {
    // The 3D stage is an ES module and runs after this classic script, so the hooks are read here.
    hooks = window.WizardHooks || hooks;
    root = document.querySelector('[data-wizard]');
    root.innerHTML =
      '<div class="frame" role="group" aria-roledescription="wizard">' +
      '<div class="f-head"><span class="f-count" data-slot="count"></span><span class="f-name" data-slot="name"></span><ol class="progress" data-slot="progress" aria-hidden="true"></ol></div>' +
      '<div class="stage"></div>' +
      '<div class="f-foot">' +
      '<button type="button" class="btn ghost" data-act="back">' + icon('back') + '<span></span></button>' +
      '<span class="f-gap"></span>' +
      '<button type="button" class="btn ghost" data-act="skip"><span></span></button>' +
      '<button type="button" class="btn primary" data-act="next"><span></span>' + icon('arrow') + '</button>' +
      '</div><div class="tip" id="wz-tip" role="tooltip" hidden></div></div>';
    frame = root.querySelector('.frame');
    stage = root.querySelector('.stage');
    foot = root.querySelector('.f-foot');
    tip = root.querySelector('.tip');
    countEl = root.querySelector('[data-slot=count]');
    progressEl = root.querySelector('[data-slot=progress]');
    document.querySelectorAll('[data-slot=lang]').forEach(function (box) {
      box.innerHTML = '<button type="button" data-act="lang" data-lang="en" aria-label="English">EN</button><button type="button" data-act="lang" data-lang="ru" aria-label="Русский">RU</button>';
    });
    document.addEventListener('click', onClick);
    document.addEventListener('input', onInput);
    document.addEventListener('change', function (e) { if (e.target.matches('select,input[type=radio]')) onInput(e); });
    document.addEventListener('keydown', onKey);
    frame.addEventListener('pointerover', function (e) {
      var b = e.target.closest('.info');
      if (b && matchMedia('(hover: hover)').matches && !tipPinned) openTip(b, false);
    });
    frame.addEventListener('pointerout', function (e) {
      var b = e.target.closest('.info');
      if (b && b === tipOwner && !tipPinned) closeTip();
    });
    stage.addEventListener('scroll', closeTip, true);
    window.addEventListener('resize', closeTip);
    var start = 0;
    if (hooks.onMount) hooks.onMount({ root: root, frame: frame, state: S, steps: STEPS });
    go(start, { instant: true, keepFocus: true });
    requestAnimationFrame(function () { document.body.classList.add('ready'); });
  }

  window.Wizard = {
    steps: STEPS, state: S, t: t, icon: icon,
    go: function (i) { busy = false; go(typeof i === 'string' ? STEPS.indexOf(i) : i, { force: true }); },
    next: next, back: back, refresh: refresh, reduced: reduced,
    current: function () { return cur; },
    busy: function () { return busy; }
  };
  // The stage (boot.js) decides first whether it runs, so the first step is staged by it or by
  // nothing, never half by each.
  function whenReady() { if (window.WizardReady) window.WizardReady.then(mount, mount); else mount(); }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', whenReady); else whenReady();
})();
