// A browser recording is WebM or MP4. The local speech model reads WAV (or Ogg via opusdec);
// ffmpeg is not in the runtime image. Decode with Web Audio and wrap PCM as a WAV the host
// already knows how to open.
//
// The WAV is resampled to 16 kHz on the way. Every recogniser here hears 16 kHz anyway, and the
// capture rate of 48 kHz made a WAV three times heavier than it needed to be: the hosted endpoint
// refuses a file past about ten megabytes, which 48 kHz reached in under two minutes of talking.

/** What a recording is sent at: the rate the speech models read, and a third of a microphone's. */
export const SPEECH_RATE = 16000;

export function pcmToWav(samples: Float32Array, sampleRate: number): Blob {
  const n = samples.length;
  const bytes = n * 2;
  const buf = new ArrayBuffer(44 + bytes);
  const view = new DataView(buf);
  const ascii = (offset: number, text: string) => {
    for (let i = 0; i < text.length; i++) view.setUint8(offset + i, text.charCodeAt(i));
  };
  ascii(0, "RIFF");
  view.setUint32(4, 36 + bytes, true);
  ascii(8, "WAVE");
  ascii(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * 2, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  ascii(36, "data");
  view.setUint32(40, bytes, true);
  let offset = 44;
  for (let i = 0; i < n; i++) {
    const clamped = Math.max(-1, Math.min(1, samples[i]));
    view.setInt16(offset, clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff, true);
    offset += 2;
  }
  return new Blob([buf], { type: "audio/wav" });
}

export function mixMono(channels: Float32Array[]): Float32Array {
  if (channels.length === 0) return new Float32Array();
  if (channels.length === 1) return channels[0];
  const n = channels[0].length;
  const out = new Float32Array(n);
  for (const channel of channels) {
    for (let i = 0; i < n; i++) out[i] += channel[i];
  }
  const scale = 1 / channels.length;
  for (let i = 0; i < n; i++) out[i] *= scale;
  return out;
}

export async function blobToWav(blob: Blob): Promise<Blob> {
  if (/\bwav\b/i.test(blob.type)) return blob;
  const Ctx = window.AudioContext ?? window.webkitAudioContext;
  if (!Ctx) return blob;
  const ctx = new Ctx();
  try {
    const raw = await blob.arrayBuffer();
    const decoded = await ctx.decodeAudioData(raw.slice(0));
    const resampled = await toSpeechRate(decoded);
    if (resampled) return pcmToWav(resampled, SPEECH_RATE);
    const channels = Array.from({ length: decoded.numberOfChannels }, (_, i) => decoded.getChannelData(i));
    return pcmToWav(mixMono(channels), decoded.sampleRate);
  } catch {
    return blob;
  } finally {
    await ctx.close().catch(() => undefined);
  }
}

/** The decoded recording as mono at {@link SPEECH_RATE}, or null where the browser cannot render offline. */
async function toSpeechRate(decoded: AudioBuffer): Promise<Float32Array | null> {
  const Offline = window.OfflineAudioContext;
  if (!Offline || decoded.sampleRate === SPEECH_RATE) return null;
  try {
    const length = Math.max(1, Math.ceil(decoded.duration * SPEECH_RATE));
    // One output channel: the context mixes the source down to mono as it renders.
    const offline = new Offline(1, length, SPEECH_RATE);
    const source = offline.createBufferSource();
    source.buffer = decoded;
    source.connect(offline.destination);
    source.start();
    const rendered = await offline.startRendering();
    return rendered.getChannelData(0);
  } catch {
    return null;
  }
}
