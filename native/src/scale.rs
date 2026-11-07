//! Putting a page together, as bookport.djvu.composite: the background scaled up with DjVuLibre's
//! bilinear interpolation (4 fraction bits), the foreground by repeating pixels, and the mask's
//! pixels painted in their colours.

fn coords(inmax: usize, outmax: usize, inn: i64, out: i64) -> Vec<i64> {
    let length = inn * 16;
    let mut y = (length + out).div_euclid(2 * out) - 8;
    let mut z = out / 2;
    let lim = (inmax as i64 - 1) * 16;
    let mut res = Vec::with_capacity(outmax);
    for _ in 0..outmax {
        res.push(if y < lim { y } else { lim });
        z += length;
        y += z / out;
        z %= out;
    }
    res
}

#[inline]
fn interp(frac: i64, a: u8, b: u8) -> u8 {
    let j = b as i64 - a as i64;
    (a as i64 + ((j * frac + 8) >> 4)) as u8
}

/// A layer reduced by `red` scaled up to width x height, rows top first.
pub fn upscale(w: usize, h: usize, nc: usize, px: &[u8], width: usize, height: usize, red: usize) -> Vec<u8> {
    let rowlen = w * nc;
    let vco = coords(h, height, 1, red as i64);
    let hco = coords(w, width, 1, red as i64);
    let pairs: Vec<(usize, i64)> = hco.iter().map(|&n| (((n >> 4) + 1) as usize, n & 15)).collect();
    let mut out = vec![0u8; width * height * nc];
    let mut line = vec![0u8; rowlen];
    let mut ch = vec![0u8; w + 2];
    let mut row = vec![0u8; width * nc];
    let mut last: Option<(i64, i64)> = None;
    for y_bu in 0..height {
        let fy = vco[y_bu];
        let lo = (fy >> 4).max(0);
        let hi = ((fy >> 4) + 1).min(h as i64 - 1);
        let frac = fy & 15;
        let key = (lo, if hi != lo { frac } else { 0 });
        if last != Some(key) {
            let lower = &px[(h - 1 - lo as usize) * rowlen..(h - lo as usize) * rowlen];
            if hi != lo && frac != 0 {
                let upper = &px[(h - 1 - hi as usize) * rowlen..(h - hi as usize) * rowlen];
                for i in 0..rowlen {
                    line[i] = interp(frac, lower[i], upper[i]);
                }
            } else {
                line.copy_from_slice(lower);
            }
            for c in 0..nc {
                for x in 0..w {
                    ch[x + 1] = line[x * nc + c];
                }
                ch[0] = ch[1];
                ch[w + 1] = ch[w];
                for (x, &(i, f)) in pairs.iter().enumerate() {
                    row[x * nc + c] = if f != 0 { interp(f, ch[i], ch[i + 1]) } else { ch[i] };
                }
            }
            last = Some(key);
        }
        let t = height - 1 - y_bu;
        out[t * width * nc..(t + 1) * width * nc].copy_from_slice(&row);
    }
    out
}

/// A layer reduced by `red` scaled up by repeating pixels (rows counted from the bottom).
pub fn blocks(w: usize, h: usize, nc: usize, px: &[u8], width: usize, height: usize, red: usize) -> Vec<u8> {
    let mut out = Vec::with_capacity(width * height * nc);
    let mut line = vec![0u8; width * nc];
    let mut last = i64::MIN;
    for y in 0..height {
        let fy = (h as i64 - 1 - ((height - 1 - y) / red) as i64).max(0);
        if fy != last {
            let src = &px[fy as usize * w * nc..(fy as usize + 1) * w * nc];
            for x in 0..width {
                let sx = (x / red).min(w - 1);
                line[x * nc..x * nc + nc].copy_from_slice(&src[sx * nc..sx * nc + nc]);
            }
            last = fy;
        }
        out.extend_from_slice(&line);
    }
    out
}

/// Paint each mask (packed rows) over `base` in its colour, or in the foreground picture's.
pub fn paint(w: usize, h: usize, nc: usize, base: &mut [u8], masks: &[(Option<[u8; 3]>, &[u8])], fg: Option<&[u8]>) {
    let nb = (w + 7) / 8;
    for (color, bits) in masks {
        for y in 0..h {
            let row = &bits[y * nb..(y + 1) * nb];
            if row.iter().all(|&b| b == 0) {
                continue;
            }
            for x in 0..w {
                if row[x / 8] & (0x80 >> (x % 8)) == 0 {
                    continue;
                }
                let o = (y * w + x) * nc;
                match color {
                    Some(c) => base[o..o + nc].copy_from_slice(&c[..nc]),
                    None => {
                        if let Some(f) = fg {
                            base[o..o + nc].copy_from_slice(&f[o..o + nc]);
                        }
                    }
                }
            }
        }
    }
}
