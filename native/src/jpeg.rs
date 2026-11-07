//! Baseline JPEG encoding, as bookport.djvu.jpeg.encode (the same arithmetic, so the same bytes).

const LUMA_Q: [i32; 64] = [
    16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55, 14, 13, 16, 24, 40, 57, 69, 56, 14, 17, 22, 29, 51,
    87, 80, 62, 18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81, 104, 113, 92, 49, 64, 78, 87, 103, 121, 120, 101,
    72, 92, 95, 98, 112, 100, 103, 99,
];
const CHROMA_Q: [i32; 64] = [
    17, 18, 24, 47, 99, 99, 99, 99, 18, 21, 26, 66, 99, 99, 99, 99, 24, 26, 56, 99, 99, 99, 99, 99, 47, 66, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99,
];
const DC_LUMA_N: [u8; 16] = [0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0];
const DC_CHROMA_N: [u8; 16] = [0, 3, 1, 1, 1, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0];
const AC_LUMA_N: [u8; 16] = [0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 0x7D];
const AC_CHROMA_N: [u8; 16] = [0, 2, 1, 2, 4, 4, 3, 4, 7, 5, 4, 4, 0, 1, 2, 0x77];
const AC_LUMA_V: &str = "01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a25262728292a3435\
363738393a434445464748494a535455565758595a636465666768696a737475767778797a838485868788898a92939495969798\
999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4\
f5f6f7f8f9fa";
const AC_CHROMA_V: &str = "000102031104052131061241510761711322328108144291a1b1c109233352f0156272d10a162434e125f11718191a262728292a\
35363738393a434445464748494a535455565758595a636465666768696a737475767778797a82838485868788898a9293949596\
9798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae2e3e4e5e6e7e8e9eaf2f3f4\
f5f6f7f8f9fa";

fn hex(s: &str) -> Vec<u8> {
    (0..s.len()).step_by(2).map(|i| u8::from_str_radix(&s[i..i + 2], 16).unwrap()).collect()
}

fn zigzag() -> [usize; 64] {
    let mut v: Vec<(usize, usize, usize)> = Vec::new();
    for y in 0..8 {
        for x in 0..8 {
            let s = x + y;
            v.push((s, if s % 2 == 1 { y } else { x }, y * 8 + x));
        }
    }
    v.sort();
    let mut z = [0usize; 64];
    for (i, t) in v.iter().enumerate() {
        z[i] = t.2;
    }
    z
}

fn codes(counts: &[u8; 16], values: &[u8]) -> [(u32, u32); 256] {
    let mut out = [(0u32, 0u32); 256];
    let mut code = 0u32;
    let mut k = 0;
    for len in 1..=16u32 {
        for _ in 0..counts[len as usize - 1] {
            out[values[k] as usize] = (code, len);
            code += 1;
            k += 1;
        }
        code <<= 1;
    }
    out
}

// round half to even, as Python's round(): adding and taking away 2^52 leaves a whole number,
// rounded the way the FPU rounds (to even). Exact below 2^52; llvm's roundeven is a slow library
// call on targets without SSE4.1.
#[inline(always)]
fn even(x: f64) -> f64 {
    const BIG: f64 = 4503599627370496.0;
    if x.abs() >= BIG {
        x
    } else if x >= 0.0 {
        (x + BIG) - BIG
    } else {
        -((-x + BIG) - BIG)
    }
}

fn quant(base: &[i32; 64], quality: i32) -> [i32; 64] {
    let q = quality.clamp(1, 100);
    let scale = if q < 50 { 5000 / q } else { 200 - 2 * q };
    let mut out = [0i32; 64];
    for i in 0..64 {
        out[i] = ((base[i] * scale + 50) / 100).clamp(1, 255);
    }
    out
}

struct Writer {
    out: Vec<u8>,
    acc: u64,
    n: u32,
}

impl Writer {
    fn put(&mut self, code: u32, bits: u32) {
        self.acc = (self.acc << bits) | code as u64;
        self.n += bits;
        while self.n >= 8 {
            self.n -= 8;
            let b = (self.acc >> self.n) as u8;
            self.out.push(b);
            if b == 0xFF {
                self.out.push(0);
            }
        }
        self.acc &= (1u64 << self.n) - 1;
    }
}

fn plane(px: &[u8], w: usize, h: usize, nc: usize, c: usize, pw: usize, ph: usize) -> Vec<f64> {
    let mut out = Vec::with_capacity(pw * ph);
    for y in 0..ph {
        let yy = y.min(h - 1);
        for x in 0..pw {
            let xx = x.min(w - 1);
            out.push(px[(yy * w + xx) * nc + c] as f64);
        }
    }
    out
}

pub fn encode(width: usize, height: usize, nc: usize, px: &[u8], quality: i32) -> Vec<u8> {
    let color = nc == 3;
    let unit = if color { 16 } else { 8 };
    let pw = (width + unit - 1) / unit * unit;
    let ph = (height + unit - 1) / unit * unit;
    let ql = quant(&LUMA_Q, quality);
    let qc = quant(&CHROMA_Q, quality);
    let zz = zigzag();
    let mut cosm = [[0f64; 8]; 8];
    for u in 0..8 {
        for x in 0..8 {
            let cu = if u == 0 { 0.5f64.sqrt() } else { 1.0 };
            cosm[u][x] = cu / 2.0 * ((2 * x + 1) as f64 * u as f64 * std::f64::consts::PI / 16.0).cos();
        }
    }
    let (ys, cbs, crs, cw);
    if color {
        let r = plane(px, width, height, 3, 0, pw, ph);
        let g = plane(px, width, height, 3, 1, pw, ph);
        let b = plane(px, width, height, 3, 2, pw, ph);
        let n = pw * ph;
        let mut y = vec![0f64; n];
        let mut cbf = vec![0f64; n];
        let mut crf = vec![0f64; n];
        for i in 0..n {
            let (a, bb, cc) = (r[i], g[i], b[i]);
            y[i] = 0.299 * a + 0.587 * bb + 0.114 * cc - 128.0;
            cbf[i] = -0.168736 * a - 0.331264 * bb + 0.5 * cc;
            crf[i] = 0.5 * a - 0.418688 * bb - 0.081312 * cc;
        }
        cw = pw / 2;
        let mut cb = vec![0f64; cw * (ph / 2)];
        let mut cr = vec![0f64; cw * (ph / 2)];
        for yy in 0..ph / 2 {
            for x in 0..cw {
                let (p, q) = ((2 * yy) * pw, (2 * yy + 1) * pw);
                cb[yy * cw + x] = (cbf[p + 2 * x] + cbf[p + 2 * x + 1] + cbf[q + 2 * x] + cbf[q + 2 * x + 1]) / 4.0;
                cr[yy * cw + x] = (crf[p + 2 * x] + crf[p + 2 * x + 1] + crf[q + 2 * x] + crf[q + 2 * x + 1]) / 4.0;
            }
        }
        ys = y;
        cbs = cb;
        crs = cr;
    } else {
        ys = plane(px, width, height, 1, 0, pw, ph).iter().map(|v| v - 128.0).collect();
        cbs = Vec::new();
        crs = Vec::new();
        cw = 0;
    }
    let dcl = codes(&DC_LUMA_N, &(0..12).collect::<Vec<u8>>());
    let acl = codes(&AC_LUMA_N, &hex(AC_LUMA_V));
    let dcc = codes(&DC_CHROMA_N, &(0..12).collect::<Vec<u8>>());
    let acc = codes(&AC_CHROMA_N, &hex(AC_CHROMA_V));
    let mut w = Writer { out: Vec::with_capacity(pw * ph / 8), acc: 0, n: 0 };
    let mut pred = [0i32; 3];
    let mut block = |w: &mut Writer, pl: &[f64], stride: usize, bx: usize, by: usize, q: &[i32; 64], dc: &[(u32, u32); 256],
                     ac: &[(u32, u32); 256], k: usize| {
        let mut data = [0f64; 64];
        for y in 0..8 {
            for x in 0..8 {
                data[y * 8 + x] = pl[(by + y) * stride + bx + x];
            }
        }
        let mut rows = [[0f64; 8]; 8];
        for y in 0..8 {
            let r = &data[y * 8..y * 8 + 8];
            for u in 0..8 {
                let c = &cosm[u];
                rows[y][u] = c[0] * r[0] + c[1] * r[1] + c[2] * r[2] + c[3] * r[3] + c[4] * r[4] + c[5] * r[5] + c[6] * r[6]
                    + c[7] * r[7];
            }
        }
        let mut f = [0f64; 64];
        for u in 0..8 {
            let col = [rows[0][u], rows[1][u], rows[2][u], rows[3][u], rows[4][u], rows[5][u], rows[6][u], rows[7][u]];
            for v in 0..8 {
                let c = &cosm[v];
                f[v * 8 + u] = c[0] * col[0] + c[1] * col[1] + c[2] * col[2] + c[3] * col[3] + c[4] * col[4]
                    + c[5] * col[5] + c[6] * col[6] + c[7] * col[7];
            }
        }
        let mut z = [0i32; 64];
        for i in 0..64 {
            z[i] = even(f[zz[i]] / q[zz[i]] as f64) as i32;
        }
        let diff = z[0] - pred[k];
        pred[k] = z[0];
        let size = 32 - diff.unsigned_abs().leading_zeros();
        let (c, l) = dc[size as usize];
        w.put(c, l);
        if size > 0 {
            w.put((if diff > 0 { diff } else { diff + (1 << size) - 1 }) as u32, size);
        }
        let mut run = 0;
        for &v in z.iter().skip(1) {
            if v == 0 {
                run += 1;
                continue;
            }
            while run > 15 {
                let (c, l) = ac[0xF0];
                w.put(c, l);
                run -= 16;
            }
            let size = 32 - v.unsigned_abs().leading_zeros();
            let (c, l) = ac[(run << 4) | size as usize];
            w.put(c, l);
            w.put((if v > 0 { v } else { v + (1 << size) - 1 }) as u32, size);
            run = 0;
        }
        if run > 0 {
            let (c, l) = ac[0];
            w.put(c, l);
        }
    };
    let mut my = 0;
    while my < ph {
        let mut mx = 0;
        while mx < pw {
            if color {
                for dy in [0, 8] {
                    for dx in [0, 8] {
                        block(&mut w, &ys, pw, mx + dx, my + dy, &ql, &dcl, &acl, 0);
                    }
                }
                block(&mut w, &cbs, cw, mx / 2, my / 2, &qc, &dcc, &acc, 1);
                block(&mut w, &crs, cw, mx / 2, my / 2, &qc, &dcc, &acc, 2);
            } else {
                block(&mut w, &ys, pw, mx, my, &ql, &dcl, &acl, 0);
            }
            mx += unit;
        }
        my += unit;
    }
    if w.n > 0 {
        let pad = 8 - w.n;
        w.put((1 << pad) - 1, pad);
    }
    let mut out = headers(width, height, color, &ql, &qc, &zz);
    out.extend_from_slice(&w.out);
    out.extend_from_slice(&[0xFF, 0xD9]);
    out
}

fn headers(width: usize, height: usize, color: bool, ql: &[i32; 64], qc: &[i32; 64], zz: &[usize; 64]) -> Vec<u8> {
    let mut out = vec![0xFF, 0xD8, 0xFF, 0xE0, 0, 16];
    out.extend_from_slice(b"JFIF\0");
    out.extend_from_slice(&[1, 1, 0, 0, 1, 0, 1, 0, 0]);
    let mut tables: Vec<(u8, &[i32; 64])> = vec![(0, ql)];
    if color {
        tables.push((1, qc));
    }
    for (tid, q) in tables {
        out.extend_from_slice(&[0xFF, 0xDB, 0, 67, tid]);
        for i in 0..64 {
            out.push(q[zz[i]] as u8);
        }
    }
    let comps: Vec<[u8; 3]> = if color { vec![[1, 0x22, 0], [2, 0x11, 1], [3, 0x11, 1]] } else { vec![[1, 0x11, 0]] };
    let len = 8 + 3 * comps.len();
    out.extend_from_slice(&[0xFF, 0xC0, (len >> 8) as u8, len as u8, 8]);
    out.extend_from_slice(&[(height >> 8) as u8, height as u8, (width >> 8) as u8, width as u8, comps.len() as u8]);
    for c in &comps {
        out.extend_from_slice(c);
    }
    let dcv: Vec<u8> = (0..12).collect();
    let (acl, acc) = (hex(AC_LUMA_V), hex(AC_CHROMA_V));
    let mut specs: Vec<(u8, &[u8; 16], &[u8])> = vec![(0x00, &DC_LUMA_N, &dcv), (0x10, &AC_LUMA_N, &acl)];
    if color {
        specs.push((0x01, &DC_CHROMA_N, &dcv));
        specs.push((0x11, &AC_CHROMA_N, &acc));
    }
    for (tc, counts, values) in specs {
        let len = 3 + 16 + values.len();
        out.extend_from_slice(&[0xFF, 0xC4, (len >> 8) as u8, len as u8, tc]);
        out.extend_from_slice(counts);
        out.extend_from_slice(values);
    }
    let sel: Vec<[u8; 2]> = if color { vec![[1, 0x00], [2, 0x11], [3, 0x11]] } else { vec![[1, 0x00]] };
    let len = 6 + 2 * sel.len();
    out.extend_from_slice(&[0xFF, 0xDA, (len >> 8) as u8, len as u8, sel.len() as u8]);
    for s in &sel {
        out.extend_from_slice(s);
    }
    out.extend_from_slice(&[0x00, 0x3F, 0x00]);
    out
}
