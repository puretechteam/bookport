//! IW44 (DjVu v3 specification, appendix 1), as bookport.djvu.iw44: coefficients decoded slice by
//! slice, then the inverse wavelet transform (16-bit samples, which wrap as the reference
//! decoder's do) and the colour conversion.

use crate::zp::{Zp, Zr};
use crate::{Damage, Res};

const BANDS: [(usize, usize); 10] = [(0, 1), (1, 1), (2, 1), (3, 1), (4, 4), (8, 4), (12, 4), (16, 16), (32, 16), (48, 16)];
const QUANT: [i32; 16] = [
    0x004000, 0x008000, 0x008000, 0x010000, 0x010000, 0x010000, 0x020000, 0x020000, 0x020000, 0x040000, 0x040000,
    0x040000, 0x080000, 0x040000, 0x040000, 0x080000,
];
const ZERO: u8 = 1;
const ACTIVE: u8 = 2;
const NEW: u8 = 4;
const UNK: u8 = 8;
const MAX_SLICES: usize = 4096;
const MAX_PIXELS: u64 = 100_000_000;

struct Codec {
    coef: Vec<[i16; 1024]>,
    alloc: Vec<u64>,
    lo: [i32; 16],
    hi: [i32; 10],
    band: usize,
    bit: i32,
    ctx_start: [u8; 32],
    ctx_bucket: [u8; 80],
    ctx_mant: u8,
    ctx_root: u8,
}

impl Codec {
    fn new(nblocks: usize) -> Codec {
        let q = QUANT;
        let lo = [q[0], q[1], q[2], q[3], q[4], q[4], q[4], q[4], q[5], q[5], q[5], q[5], q[6], q[6], q[6], q[6]];
        let mut hi = [0i32; 10];
        hi[1..10].copy_from_slice(&q[7..16]);
        Codec {
            coef: vec![[0i16; 1024]; nblocks],
            alloc: vec![0u64; nblocks],
            lo,
            hi,
            band: 0,
            bit: 0,
            ctx_start: [0; 32],
            ctx_bucket: [0; 80],
            ctx_mant: 0,
            ctx_root: 0,
        }
    }

    fn slice(&mut self, zp: &mut Zp) -> Zr<bool> {
        if self.bit < 0 {
            return Ok(false);
        }
        let band = self.band;
        if band == 0 {
            let mut zero = [false; 16];
            let mut all = true;
            for i in 0..16 {
                zero[i] = !(self.lo[i] > 0 && self.lo[i] < 0x8000);
                all &= zero[i];
            }
            if !all {
                for b in 0..self.coef.len() {
                    self.band0(zp, b, &zero)?;
                }
            }
        } else if self.hi[band] > 0 && self.hi[band] < 0x8000 {
            let (first, n) = BANDS[band];
            for b in 0..self.coef.len() {
                self.bucket_band(zp, b, band, first, n)?;
            }
        }
        self.band += 1;
        if self.band == 10 {
            self.band = 0;
            self.bit += 1;
            for t in self.lo.iter_mut() {
                *t >>= 1;
            }
            for t in self.hi.iter_mut() {
                *t >>= 1;
            }
            if self.hi[9] == 0 {
                self.bit = -1;
            }
        }
        Ok(true)
    }

    fn band0(&mut self, zp: &mut Zp, b: usize, zero: &[bool; 16]) -> Zr<()> {
        let allocated = self.alloc[b] & 1 != 0;
        let mut state = [0u8; 16];
        let mut bstate = 0u8;
        for i in 0..16 {
            state[i] = if zero[i] {
                ZERO
            } else if allocated && self.coef[b][i] != 0 {
                ACTIVE
            } else {
                UNK
            };
            bstate |= state[i];
        }
        if bstate & UNK != 0 || !allocated {
            let ctx = if bstate & ACTIVE != 0 { 4 } else { 0 };
            if bstate & UNK != 0 && zp.decode(&mut self.ctx_bucket[ctx])? != 0 {
                self.alloc[b] |= 1;
                let lo = self.lo;
                self.activate(zp, b, 0, &mut state, |i| lo[i], bstate & ACTIVE != 0)?;
            }
        }
        if bstate & ACTIVE != 0 {
            let lo = self.lo;
            self.refine(zp, b, 0, &state, |i| lo[i])?;
        }
        Ok(())
    }

    fn bucket_band(&mut self, zp: &mut Zp, b: usize, band: usize, first: usize, n: usize) -> Zr<()> {
        let mut bucket_state = [0u8; 16];
        let mut states = [[0u8; 16]; 16];
        let mut bstate = 0u8;
        for j in 0..n {
            let k = first + j;
            if self.alloc[b] >> k & 1 == 0 {
                bucket_state[j] = UNK;
            } else {
                let mut bs = 0u8;
                for i in 0..16 {
                    let s = if self.coef[b][k * 16 + i] != 0 { ACTIVE } else { UNK };
                    states[j][i] = s;
                    bs |= s;
                }
                bucket_state[j] = bs;
            }
            bstate |= bucket_state[j];
        }
        let thres = self.hi[band];
        let go = if n < 16 || bstate & ACTIVE != 0 {
            true
        } else if bstate & UNK != 0 {
            zp.decode(&mut self.ctx_root)? != 0
        } else {
            false
        };
        if go {
            let act = if bstate & ACTIVE != 0 { 4 } else { 0 };
            for j in 0..n {
                if bucket_state[j] & UNK != 0 {
                    let k = (first + j) << 2;
                    let pb = k >> 4;
                    let mut c = 0usize;
                    if self.alloc[b] >> pb & 1 != 0 {
                        let p = &self.coef[b][pb * 16..pb * 16 + 16];
                        let k = k & 15;
                        c = (p[k] != 0) as usize + (p[k + 1] != 0) as usize + (p[k + 2] != 0) as usize;
                        if c < 3 && p[k + 3] != 0 {
                            c += 1;
                        }
                    }
                    if zp.decode(&mut self.ctx_bucket[band * 8 + (c | act)])? != 0 {
                        bucket_state[j] |= NEW;
                    }
                }
            }
            for j in 0..n {
                if bucket_state[j] & NEW != 0 {
                    let k = first + j;
                    if self.alloc[b] >> k & 1 == 0 {
                        self.alloc[b] |= 1 << k;
                        states[j] = [UNK; 16];
                    }
                    let mut st = states[j];
                    self.activate(zp, b, k, &mut st, |_| thres, bucket_state[j] & ACTIVE != 0)?;
                    states[j] = st;
                }
            }
        }
        if bstate & ACTIVE != 0 {
            for j in 0..n {
                if bucket_state[j] & ACTIVE != 0 {
                    let st = states[j];
                    self.refine(zp, b, first + j, &st, |_| thres)?;
                }
            }
        }
        Ok(())
    }

    fn activate<F: Fn(usize) -> i32>(
        &mut self,
        zp: &mut Zp,
        b: usize,
        bucket: usize,
        state: &mut [u8; 16],
        thres: F,
        active: bool,
    ) -> Zr<()> {
        let act = if active { 8 } else { 0 };
        let mut gotcha = state.iter().filter(|&&s| s & UNK != 0).count();
        for i in 0..16 {
            if state[i] & UNK != 0 {
                let t = thres(i);
                if zp.decode(&mut self.ctx_start[gotcha.min(7) | act])? != 0 {
                    state[i] |= NEW;
                    let half = t >> 1;
                    let v = t + half - (half >> 2);
                    let neg = zp.iw_passthru()? != 0;
                    self.coef[b][bucket * 16 + i] = (if neg { -v } else { v }) as i16;
                    gotcha = 0;
                } else if gotcha > 0 {
                    gotcha -= 1;
                }
            }
        }
        Ok(())
    }

    fn refine<F: Fn(usize) -> i32>(&mut self, zp: &mut Zp, b: usize, bucket: usize, state: &[u8; 16], thres: F) -> Zr<()> {
        for i in 0..16 {
            if state[i] & ACTIVE != 0 {
                let t = thres(i);
                let c = self.coef[b][bucket * 16 + i] as i32;
                let mut v = c.abs();
                if v <= 3 * t {
                    v += t >> 2;
                    if zp.decode(&mut self.ctx_mant)? != 0 {
                        v += t >> 1;
                    } else {
                        v = v - t + (t >> 1);
                    }
                } else if zp.iw_passthru()? != 0 {
                    v += t >> 1;
                } else {
                    v = v - t + (t >> 1);
                }
                self.coef[b][bucket * 16 + i] = (if c > 0 { v } else { -v }) as i16;
            }
        }
        Ok(())
    }
}

pub struct Layer {
    width: usize,
    height: usize,
    codecs: Vec<Codec>,
    serial: u32,
    slices: usize,
    delay: usize,
    half: bool,
    color: bool,
}

impl Layer {
    pub fn new() -> Layer {
        Layer { width: 0, height: 0, codecs: Vec::new(), serial: 0, slices: 0, delay: 0, half: false, color: false }
    }

    pub fn add(&mut self, data: &[u8]) -> Res<()> {
        if data.len() < 2 {
            return Err(Damage::new("Damaged DjVu file: an IW44 chunk is too short"));
        }
        let (serial, nslices) = (data[0] as u32, data[1] as usize);
        let mut pos = 2;
        if serial != self.serial {
            return Err(Damage::new("Damaged DjVu file: IW44 chunks are out of order"));
        }
        if serial == 0 {
            if data.len() < 9 {
                return Err(Damage::new("Damaged DjVu file: an IW44 chunk is too short"));
            }
            let (major, minor) = (data[2], data[3]);
            if major & 0x7F > 1 {
                return Err(Damage::new("This DjVu file uses a newer IW44 version that bookport can't read"));
            }
            let w = (data[4] as usize) << 8 | data[5] as usize;
            let h = (data[6] as usize) << 8 | data[7] as usize;
            pos = 8;
            let mut delay = 0;
            if minor >= 2 {
                delay = (data[8] & 0x7F) as usize;
                self.half = data[8] & 0x80 == 0;
                pos = 9;
            }
            if w == 0 || h == 0 {
                return Err(Damage::new("Damaged DjVu file: an IW44 image has no size"));
            }
            if (w * h) as u64 > MAX_PIXELS {
                return Err(Damage::new(&format!(
                    "A picture in this book is {} x {} pixels, more than bookport reads",
                    w, h
                )));
            }
            self.width = w;
            self.height = h;
            self.color = major & 0x80 == 0;
            self.delay = delay;
            let nb = ((w + 31) / 32) * ((h + 31) / 32);
            self.codecs = (0..if self.color { 3 } else { 1 }).map(|_| Codec::new(nb)).collect();
        } else if self.codecs.is_empty() {
            return Err(Damage::new("Damaged DjVu file: an IW44 chunk comes before the first one"));
        }
        self.serial += 1;
        let _ = self.slices_of(&data[pos..], nslices);
        Ok(())
    }

    fn slices_of(&mut self, data: &[u8], nslices: usize) -> Zr<()> {
        let mut zp = Zp::new(data)?;
        for _ in 0..nslices {
            if self.slices >= MAX_SLICES {
                break;
            }
            let mut more = self.codecs[0].slice(&mut zp)?;
            if self.color && self.slices >= self.delay {
                more |= self.codecs[1].slice(&mut zp)?;
                more |= self.codecs[2].slice(&mut zp)?;
            }
            self.slices += 1;
            if !more {
                break;
            }
        }
        Ok(())
    }

    /// (width, height, channels, pixels top row first)
    pub fn image(&self) -> Res<(usize, usize, usize, Vec<u8>)> {
        let (w, h) = (self.width, self.height);
        if self.codecs.is_empty() {
            return Err(Damage::new("Damaged DjVu file: an IW44 layer has no data"));
        }
        let planes: Vec<Vec<i16>> = self
            .codecs
            .iter()
            .enumerate()
            .map(|(i, c)| reconstruct(c, w, h, if i > 0 && self.half { 2 } else { 1 }))
            .collect();
        let bw = (w + 31) & !31;
        if planes.len() == 1 {
            let mut out = Vec::with_capacity(w * h);
            for y in (0..h).rev() {
                for x in 0..w {
                    out.push((127 - clamp8(planes[0][y * bw + x])) as u8);
                }
            }
            return Ok((w, h, 1, out));
        }
        let mut out = Vec::with_capacity(w * h * 3);
        for y in (0..h).rev() {
            for x in 0..w {
                let yy = clamp8(planes[0][y * bw + x]);
                let b = clamp8(planes[1][y * bw + x]);
                let r = clamp8(planes[2][y * bw + x]);
                let t2 = r + (r >> 1);
                let t3 = yy + 128 - (b >> 2);
                out.push((yy + 128 + t2).clamp(0, 255) as u8);
                out.push((t3 - (t2 >> 1)).clamp(0, 255) as u8);
                out.push((t3 + (b << 1)).clamp(0, 255) as u8);
            }
        }
        Ok((w, h, 3, out))
    }
}

#[inline]
fn clamp8(v: i16) -> i32 {
    ((v as i32 + 32) >> 6).clamp(-128, 127)
}

fn zigzag() -> ([u8; 1024], [u8; 1024]) {
    let mut rows = [0u8; 1024];
    let mut cols = [0u8; 1024];
    for i in 0..1024usize {
        let (mut r, mut c) = (0usize, 0usize);
        for k in 0..5 {
            r |= ((i >> (2 * k + 1)) & 1) << (4 - k);
            c |= ((i >> (2 * k)) & 1) << (4 - k);
        }
        rows[i] = r as u8;
        cols[i] = c as u8;
    }
    (rows, cols)
}

fn reconstruct(codec: &Codec, w: usize, h: usize, end: usize) -> Vec<i16> {
    let bw = (w + 31) & !31;
    let bh = (h + 31) & !31;
    let mut p = vec![0i16; bw * bh];
    let nbx = bw / 32;
    let (zr, zc) = zigzag();
    for (n, blk) in codec.coef.iter().enumerate() {
        let alloc = codec.alloc[n];
        if alloc == 0 {
            continue;
        }
        let y0 = (n / nbx) * 32;
        let x0 = (n % nbx) * 32;
        for bucket in 0..64 {
            if alloc >> bucket & 1 == 0 {
                continue;
            }
            for i in bucket * 16..bucket * 16 + 16 {
                let v = blk[i];
                if v != 0 {
                    p[(y0 + zr[i] as usize) * bw + x0 + zc[i] as usize] = v;
                }
            }
        }
    }
    let mut s = 16;
    while s >= end {
        // columns, then rows
        let kmax = (h - 1) / s;
        let mut x = 0;
        while x < w {
            lift(&mut p, x, s * bw, kmax);
            x += s;
        }
        let kmax = (w - 1) / s;
        let mut y = 0;
        while y < h {
            lift(&mut p, y * bw, s, kmax);
            y += s;
        }
        s >>= 1;
    }
    if end == 2 {
        let mut y = 0;
        while y < h {
            let mut x = 0;
            while x < w {
                p[y * bw + x + 1] = p[y * bw + x];
                x += 2;
            }
            if y + 1 < bh {
                let (a, b) = p.split_at_mut((y + 1) * bw);
                b[..bw].copy_from_slice(&a[y * bw..(y + 1) * bw]);
            }
            y += 2;
        }
    }
    p
}

/// The 1-D inverse step on samples start, start + stride, ... (kmax + 1 of them).
fn lift(p: &mut [i16], start: usize, stride: usize, kmax: usize) {
    let at = |p: &[i16], k: i64| -> i32 {
        if k < 0 || k > kmax as i64 {
            0
        } else {
            p[start + k as usize * stride] as i32
        }
    };
    let mut k = 0i64;
    while k <= kmax as i64 {
        let (a, b, c, d) = (at(p, k - 1), at(p, k + 1), at(p, k - 3), at(p, k + 3));
        let i = start + k as usize * stride;
        p[i] = (p[i] as i32 - ((9 * (a + b) - c - d + 16) >> 5)) as i16;
        k += 2;
    }
    let mut k = 1i64;
    while k <= kmax as i64 {
        let i = start + k as usize * stride;
        let o = p[i] as i32;
        let v = if k >= 3 && k + 3 <= kmax as i64 {
            let (a, b, c, d) = (at(p, k - 1), at(p, k + 1), at(p, k - 3), at(p, k + 3));
            o + ((9 * (a + b) - c - d + 8) >> 4)
        } else if k + 1 <= kmax as i64 {
            o + ((at(p, k - 1) + at(p, k + 1) + 1) >> 1)
        } else {
            o + at(p, k - 1)
        };
        p[i] = v as i16;
        k += 2;
    }
}

pub fn decode(chunks: &[&[u8]]) -> Res<(usize, usize, usize, Vec<u8>)> {
    let mut layer = Layer::new();
    for c in chunks {
        layer.add(c)?;
    }
    layer.image()
}
