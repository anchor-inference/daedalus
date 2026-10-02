import * as THREE from './vendor/three.module.min.js';
import { makeChibi, poseChibi } from './chibi.js';

const ru = new URLSearchParams(location.search).get('lang') === 'ru';
const words = ru ? { idle:'Готов к работе', running:'Агент работает', waiting:'Нужен ваш ответ', failed:'Нужна проверка', open:'Открыть Daedalus', hide:'Скрыть маскота' } : { idle:'Ready', running:'Agent working', waiting:'Needs your reply', failed:'Needs attention', open:'Open Daedalus', hide:'Hide mascot' };
const canvas = document.querySelector('canvas');
const status = document.querySelector('.status');
const open = document.querySelector('.open');
open.textContent = words.open;
open.onclick = () => window.pet.open();
const hide = document.querySelector('.hide');
hide.setAttribute('aria-label', words.hide);
hide.title = words.hide;
hide.onclick = () => window.pet.hide();
canvas.ondblclick = () => window.pet.open();

let state = 'idle';
let reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
let frame = 0;
let scene, renderer, camera, chibi;
function paint(time = 0) {
  frame = 0;
  if (!renderer || document.hidden) return;
  const busy = state === 'running';
  poseChibi(chibi, { t:reduced ? 0 : time / 1000, seed:0.5, run:0, phase:0, happy:state === 'idle' ? 0.3 : 0, lookUp:state === 'waiting' ? 0.18 : 0, flap:busy ? 0.3 : 0, gazeX:0.2 });
  chibi.ledM.color.setHex(state === 'idle' ? 0x5ee2c4 : state === 'failed' ? 0xf0625d : 0xffb35c);
  chibi.ledCoreM.color.copy(chibi.ledM.color);
  renderer.render(scene, camera);
  if (!reduced) frame = requestAnimationFrame(paint);
}
function update() {
  status.textContent = words[state];
  if (!frame) paint();
}
window.pet.onState((next) => {
  state = Object.hasOwn(words, next.state) ? next.state : 'idle';
  reduced = !!next.reduced;
  if (reduced && frame) { cancelAnimationFrame(frame); frame = 0; }
  update();
});
document.addEventListener('visibilitychange', () => {
  if (document.hidden && frame) { cancelAnimationFrame(frame); frame = 0; }
  else if (!document.hidden) update();
});
try {
  renderer = new THREE.WebGLRenderer({ canvas, alpha:true, antialias:true, powerPreference:'low-power' });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 1.5));
  renderer.setSize(180, 210, false);
  renderer.setClearColor(0, 0);
  scene = new THREE.Scene();
  camera = new THREE.PerspectiveCamera(35, 180 / 210, 0.1, 30);
  camera.position.set(0, 2.1, 7.8); camera.lookAt(0, 1.8, 0);
  scene.add(new THREE.HemisphereLight(0xffffff, 0x293138, 2.2));
  const light = new THREE.DirectionalLight(0xffead5, 3); light.position.set(-3, 4, 5); scene.add(light);
  const fill = new THREE.DirectionalLight(0x5ee2c4, 1.5); fill.position.set(3, 2, -2); scene.add(fill);
  chibi = makeChibi(1); chibi.root.rotation.y = -0.12; scene.add(chibi.root);
  update();
} catch {
  canvas.hidden = true;
  status.textContent = words.idle;
}
