// Минимальный QR-кодировщик: версия 1 (21×21), уровень L, числовой режим, маска 0.
// Хватает на 13-значное время в мс (вместимость 41 цифра). Без зависимостей.
'use strict';
const QR = (() => {
  const SIZE = 21, DATA_CW = 19, EC_CW = 7;

  function gfMul(x, y) {
    let z = 0;
    for (let i = 7; i >= 0; i--) {
      z = (z << 1) ^ ((z >>> 7) * 0x11d);
      z ^= ((y >>> i) & 1) * x;
    }
    return z & 0xff;
  }
  function rsDivisor(degree) {
    const r = new Array(degree - 1).fill(0); r.push(1);
    let root = 1;
    for (let i = 0; i < degree; i++) {
      for (let j = 0; j < r.length; j++) {
        r[j] = gfMul(r[j], root);
        if (j + 1 < r.length) r[j] ^= r[j + 1];
      }
      root = gfMul(root, 2);
    }
    return r;
  }
  const DIV = rsDivisor(EC_CW);
  function rsRemainder(data) {
    const r = new Array(EC_CW).fill(0);
    for (const b of data) {
      const f = b ^ r.shift(); r.push(0);
      DIV.forEach((c, i) => { r[i] ^= gfMul(c, f); });
    }
    return r;
  }

  function encodeDigits(digits) {
    const bits = [];
    const put = (v, n) => { for (let i = n - 1; i >= 0; i--) bits.push((v >>> i) & 1); };
    put(1, 4);                // numeric mode
    put(digits.length, 10);   // char count (v1..9)
    for (let i = 0; i < digits.length; i += 3) {
      const g = digits.substr(i, 3);
      put(parseInt(g, 10), g.length === 3 ? 10 : g.length === 2 ? 7 : 4);
    }
    const cap = DATA_CW * 8;
    put(0, Math.min(4, cap - bits.length));
    while (bits.length % 8) bits.push(0);
    const bytes = [];
    for (let i = 0; i < bits.length; i += 8) bytes.push(parseInt(bits.slice(i, i + 8).join(''), 2));
    for (let p = 0; bytes.length < DATA_CW; p ^= 1) bytes.push(p ? 0x11 : 0xec);
    return bytes.concat(rsRemainder(bytes));
  }

  function matrix(digits) {
    const m = [], fn = [];
    for (let y = 0; y < SIZE; y++) { m.push(new Array(SIZE).fill(false)); fn.push(new Array(SIZE).fill(false)); }
    const set = (x, y, v) => { m[y][x] = v; fn[y][x] = true; };
    for (let i = 0; i < SIZE; i++) { set(6, i, i % 2 === 0); set(i, 6, i % 2 === 0); }
    const finder = (cx, cy) => {
      for (let dy = -4; dy <= 4; dy++) for (let dx = -4; dx <= 4; dx++) {
        const x = cx + dx, y = cy + dy;
        if (x < 0 || y < 0 || x >= SIZE || y >= SIZE) continue;
        const d = Math.max(Math.abs(dx), Math.abs(dy));
        set(x, y, d !== 2 && d !== 4);
      }
    };
    finder(3, 3); finder(SIZE - 4, 3); finder(3, SIZE - 4);
    // format bits: ECC L (01) + mask 0
    const data = (1 << 3) | 0;
    let rem = data;
    for (let i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
    const bits = ((data << 10) | rem) ^ 0x5412;
    const gb = i => ((bits >>> i) & 1) === 1;
    for (let i = 0; i <= 5; i++) set(8, i, gb(i));
    set(8, 7, gb(6)); set(8, 8, gb(7)); set(7, 8, gb(8));
    for (let i = 9; i < 15; i++) set(14 - i, 8, gb(i));
    for (let i = 0; i < 8; i++) set(SIZE - 1 - i, 8, gb(i));
    for (let i = 8; i < 15; i++) set(8, SIZE - 15 + i, gb(i));
    set(8, SIZE - 8, true);
    // data
    const cw = encodeDigits(digits);
    let i = 0;
    for (let right = SIZE - 1; right >= 1; right -= 2) {
      if (right === 6) right = 5;
      for (let vert = 0; vert < SIZE; vert++) {
        for (let j = 0; j < 2; j++) {
          const x = right - j;
          const up = ((right + 1) & 2) === 0;
          const y = up ? SIZE - 1 - vert : vert;
          if (!fn[y][x] && i < cw.length * 8) {
            m[y][x] = ((cw[i >>> 3] >>> (7 - (i & 7))) & 1) === 1;
            i++;
          }
        }
      }
    }
    for (let y = 0; y < SIZE; y++) for (let x = 0; x < SIZE; x++)
      if (!fn[y][x] && (x + y) % 2 === 0) m[y][x] = !m[y][x];
    return m;
  }

  // Рисует QR в canvas: белый фон, тихая зона 4 модуля.
  function draw(canvas, digits) {
    const m = matrix(digits);
    const q = 4, n = SIZE + 2 * q;
    const ctx = canvas.getContext('2d');
    const s = Math.floor(Math.min(canvas.width, canvas.height) / n);
    const off = Math.floor((canvas.width - s * n) / 2);
    ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, canvas.width, canvas.height);
    ctx.fillStyle = '#000';
    for (let y = 0; y < SIZE; y++) for (let x = 0; x < SIZE; x++)
      if (m[y][x]) ctx.fillRect(off + (x + q) * s, off + (y + q) * s, s, s);
  }
  return { matrix, draw };
})();
