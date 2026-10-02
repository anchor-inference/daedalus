// The upgrade's own window (desktop/upgrade_window.go): the steps of an upgrade the app started,
// polled from the process doing it. When that process is gone the page has already been told how
// it ended; a fetch that fails after that is the server shutting down, not news.
(function () {
  'use strict';
  var words = {};
  try { words = JSON.parse(document.getElementById('words').textContent); } catch (e) {}
  function t(key, to) { return (words[key] || key).replace('%s', to || ''); }
  var el = function (id) { return document.getElementById(id); };
  var finished = false;

  el('close').textContent = t('close');
  el('close').addEventListener('click', function () { window.close(); });

  function render(p) {
    var to = p.to || '';
    el('title').textContent = t('title', to);
    el('lead').textContent = t('lead');
    var list = el('steps');
    list.textContent = '';
    p.steps.forEach(function (key) {
      var item = document.createElement('li');
      if (p.done.indexOf(key) >= 0) item.className = 'done';
      else if (key === p.step && p.state === 'running') item.className = 'now';
      else if (key === p.step && p.state === 'failed') item.className = 'bad';
      var dot = document.createElement('span');
      dot.className = 'dot';
      item.appendChild(dot);
      item.appendChild(document.createTextNode(t('step.' + key)));
      list.appendChild(item);
    });
    el('bar').style.width = Math.round((p.done.length / p.steps.length) * 100) + '%';
    el('log').hidden = !p.lines.length;
    el('log').textContent = p.lines.join('\n');
    if (p.state === 'done') {
      finished = true;
      el('result').hidden = false;
      el('result').className = 'result ok';
      el('result-title').textContent = t('done', to);
      el('result-body').textContent = t('done.body');
      // The application is opening on the new version; this window has said what it had to.
      setTimeout(function () { window.close(); el('close').hidden = false; }, 2500);
    } else if (p.state === 'failed') {
      finished = true;
      el('result').hidden = false;
      el('result').className = 'result bad';
      el('result-title').textContent = t('failed');
      el('result-why').hidden = !p.message;
      el('result-why').textContent = p.message;
      el('result-body').textContent = t('failed.body');
      el('close').hidden = false;
    }
  }

  function poll() {
    if (finished) return;
    fetch('/progress', { cache: 'no-store' })
      .then(function (r) { return r.json(); })
      .then(function (p) { render(p); if (!finished) setTimeout(poll, 700); })
      .catch(function () { if (!finished) setTimeout(poll, 1500); });
  }
  poll();
})();
