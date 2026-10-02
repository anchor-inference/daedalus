/* Decides, before the wizard mounts, whether the live stage can run on this machine. Without WebGL
   (a remote desktop, a blocked GPU, a driver the browser distrusts) or when the stage fails to
   start, the page keeps the same wizard and shows a still picture of Daedalus in his place; the
   wizard waits for this decision so its first step is always staged by whichever one won. */
(function () {
  'use strict';
  function still() { document.documentElement.classList.add('no-gl'); }
  var ok = false;
  try {
    var probe = document.createElement('canvas');
    var gl = window.WebGLRenderingContext && (probe.getContext('webgl2') || probe.getContext('webgl'));
    ok = !!gl;
    // The probe's context is given back at once: browsers cap the number of live contexts.
    var lose = gl && gl.getExtension('WEBGL_lose_context');
    if (lose) lose.loseContext();
  } catch (e) {
    ok = false;
  }
  if (!ok) { still(); return; }
  var done;
  window.WizardReady = new Promise(function (resolve) { done = resolve; });
  // A context the GPU takes back mid-run leaves a blank canvas; the still is better than nothing.
  document.addEventListener('DOMContentLoaded', function () {
    var canvas = document.getElementById('gl');
    if (canvas) canvas.addEventListener('webglcontextlost', still);
  });
  import('/assets/setup/scene.js').then(function () { done(); }, function (e) {
    still();
    console.warn('the 3D stage did not start:', e && e.message);
    done();
  });
})();
