// The extension's icon, drawn here rather than kept as image files: a rounded square in the
// product's blue with a white ring, at the sizes the manifest names. A notification needs an icon,
// and Chrome takes the toolbar's from the same files. Pure Node (zlib) so the build needs nothing
// more than it had.
import { deflateSync } from "node:zlib";

const BLUE = [31, 111, 235, 255];
const WHITE = [255, 255, 255, 255];
const CLEAR = [0, 0, 0, 0];

function crc32(bytes) {
  let crc = ~0;
  for (const byte of bytes) {
    crc ^= byte;
    for (let k = 0; k < 8; k++) crc = (crc >>> 1) ^ (0xedb88320 & -(crc & 1));
  }
  return ~crc >>> 0;
}

function chunk(type, data) {
  const head = Buffer.alloc(8);
  head.writeUInt32BE(data.length, 0);
  head.write(type, 4, "latin1");
  const tail = Buffer.alloc(4);
  tail.writeUInt32BE(crc32(Buffer.concat([head.subarray(4), data])), 0);
  return Buffer.concat([head, data, tail]);
}

/** The icon at `size` pixels square, as PNG bytes. */
export function iconPng(size) {
  const centre = (size - 1) / 2;
  const rows = [];
  for (let y = 0; y < size; y++) {
    const row = [0];
    for (let x = 0; x < size; x++) {
      const dx = Math.max(Math.abs(x - centre) - size * 0.3, 0);
      const dy = Math.max(Math.abs(y - centre) - size * 0.3, 0);
      const inside = Math.hypot(dx, dy) <= size * 0.16;
      const d = Math.hypot(x - centre, y - centre);
      row.push(...(!inside ? CLEAR : d >= size * 0.2 && d <= size * 0.3 ? WHITE : BLUE));
    }
    rows.push(Buffer.from(row));
  }
  const header = Buffer.alloc(13);
  header.writeUInt32BE(size, 0);
  header.writeUInt32BE(size, 4);
  header.set([8, 6, 0, 0, 0], 8);
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk("IHDR", header),
    chunk("IDAT", deflateSync(Buffer.concat(rows), { level: 9 })),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

export const ICON_SIZES = [16, 48, 128];
