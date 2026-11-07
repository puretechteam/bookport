//! JB2 (DjVu v3 specification, appendix 2), as bookport.djvu.jb2: symbols and their placements,
//! then the placements drawn as packed 1-bit rows.

use crate::zp::{EndOfData, Zp};
use crate::{Damage, Res};
use std::sync::Arc;

const BIGPOSITIVE: i32 = 262142;
const BIGNEGATIVE: i32 = -262143;
const MAX_CELLS: usize = 4_000_000;
const MAX_RECORDS: usize = 4_000_000;
pub const MAX_PIXELS: u64 = 100_000_000;
const PAD_L: usize = 2;
const PAD_R: usize = 3;

const C_COMMENT_BYTE: usize = 0;
const C_COMMENT_LEN: usize = 1;
const C_RECORD: usize = 2;
const C_MATCH: usize = 3;
const C_ABS_X: usize = 4;
const C_ABS_Y: usize = 5;
const C_SIZE_X: usize = 6;
const C_SIZE_Y: usize = 7;
const C_IMAGE_SIZE: usize = 8;
const C_INHERIT: usize = 9;
const C_REL_X_CUR: usize = 10;
const C_REL_X_LAST: usize = 11;
const C_REL_Y_CUR: usize = 12;
const C_REL_Y_LAST: usize = 13;
const C_REL_W: usize = 14;
const C_REL_H: usize = 15;

pub struct Shape {
    pub w: usize,
    pub h: usize,
    /// rows top first, each w + PAD_L + PAD_R bytes of 0 or 1
    pub bits: Vec<u8>,
    /// left, bottom, right, top of the black pixels (rows counted from the bottom)
    pub bbox: (i32, i32, i32, i32),
    /// rows packed (column 0 in the top bit), for drawing
    pub packed: Vec<u8>,
}

impl Shape {
    fn new(w: usize, h: usize, bits: Vec<u8>) -> Shape {
        let stride = w + PAD_L + PAD_R;
        let (mut left, mut bottom, mut right, mut top) = (0i32, 0i32, -1i32, -1i32);
        let mut first_t: Option<usize> = None;
        let mut last_t = 0usize;
        let mut minc = usize::MAX;
        let mut maxc = 0usize;
        for t in 0..h {
            let row = &bits[t * stride..(t + 1) * stride];
            let mut any = false;
            for (c, &v) in row.iter().enumerate() {
                if v != 0 {
                    any = true;
                    minc = minc.min(c);
                    maxc = maxc.max(c);
                }
            }
            if any {
                if first_t.is_none() {
                    first_t = Some(t);
                }
                last_t = t;
            }
        }
        if let Some(ft) = first_t {
            top = (h - 1 - ft) as i32;
            bottom = (h - 1 - last_t) as i32;
            left = minc as i32 - PAD_L as i32;
            right = maxc as i32 - PAD_L as i32;
        }
        let nb = (w + 7) / 8;
        let mut packed = vec![0u8; nb * h];
        for t in 0..h {
            let row = &bits[t * stride + PAD_L..t * stride + PAD_L + w];
            for (x, &v) in row.iter().enumerate() {
                if v != 0 {
                    packed[t * nb + x / 8] |= 0x80 >> (x % 8);
                }
            }
        }
        Shape { w, h, bits, bbox: (left, bottom, right, top), packed }
    }
}

pub struct Jb2Data {
    pub width: usize,
    pub height: usize,
    pub shapes: Vec<Arc<Shape>>,
    pub blits: Vec<(i32, i32, u32)>,
    pub inherited: usize,
    pub truncated: bool,
}

enum Slot {
    Root(usize),
    Left(usize),
    Right(usize),
}

struct NumCoder {
    cells: Vec<u8>,
    left: Vec<u32>,
    right: Vec<u32>,
    roots: [u32; 16],
}

impl NumCoder {
    fn new() -> NumCoder {
        NumCoder { cells: vec![0], left: vec![0], right: vec![0], roots: [0; 16] }
    }
    fn reset(&mut self) {
        *self = NumCoder::new();
    }
}

enum Stop {
    End,
    Damage(Damage),
}

impl From<EndOfData> for Stop {
    fn from(_: EndOfData) -> Stop {
        Stop::End
    }
}

impl From<Damage> for Stop {
    fn from(d: Damage) -> Stop {
        Stop::Damage(d)
    }
}

type Sr<T> = Result<T, Stop>;

fn damage(msg: &str) -> Stop {
    Stop::Damage(Damage::new(msg))
}

struct Decoder<'a> {
    zp: Zp<'a>,
    num: NumCoder,
    flags: [u8; 2],
    direct: Vec<u8>,
    cross: Vec<u8>,
    dict: Option<&'a Jb2Data>,
    is_dict: bool,
    out: Jb2Data,
    lib: Vec<u32>,
    started: bool,
    budget: i64,
    last_left: i32,
    last_row_left: i32,
    last_row_bottom: i32,
    last_right: i32,
    last_bottom: i32,
    short: [i32; 3],
    short_pos: usize,
}

impl<'a> Decoder<'a> {
    fn code_num(&mut self, mut low: i32, mut high: i32, which: usize) -> Sr<i32> {
        let mut slot = Slot::Root(which);
        let mut cutoff: i32 = 0;
        let mut phase = 1;
        let mut rng: i64 = -1;
        let mut negative = false;
        while rng != 1 {
            let cur = match slot {
                Slot::Root(i) => self.num.roots[i],
                Slot::Left(i) => self.num.left[i],
                Slot::Right(i) => self.num.right[i],
            };
            let cell = if cur == 0 {
                let c = self.num.cells.len();
                if c >= MAX_CELLS {
                    return Err(damage("Damaged DjVu file: a JB2 stream uses too many contexts"));
                }
                self.num.cells.push(0);
                self.num.left.push(0);
                self.num.right.push(0);
                match slot {
                    Slot::Root(i) => self.num.roots[i] = c as u32,
                    Slot::Left(i) => self.num.left[i] = c as u32,
                    Slot::Right(i) => self.num.right[i] = c as u32,
                }
                c
            } else {
                cur as usize
            };
            let decision = if low >= cutoff {
                true
            } else if high >= cutoff {
                self.zp.decode(&mut self.num.cells[cell])? != 0
            } else {
                false
            };
            slot = if decision { Slot::Right(cell) } else { Slot::Left(cell) };
            if phase == 1 {
                negative = !decision;
                if negative {
                    let t = -low - 1;
                    low = -high - 1;
                    high = t;
                }
                phase = 2;
                cutoff = 1;
            } else if phase == 2 {
                if !decision {
                    phase = 3;
                    rng = ((cutoff as i64) + 1) >> 1;
                    cutoff = if rng == 1 { 0 } else { cutoff - (rng >> 1) as i32 };
                } else {
                    cutoff = 2 * cutoff + 1;
                }
            } else {
                rng >>= 1;
                if rng != 1 {
                    if decision {
                        cutoff += (rng >> 1) as i32;
                    } else {
                        cutoff -= (rng >> 1) as i32;
                    }
                } else if !decision {
                    cutoff -= 1;
                }
            }
        }
        Ok(if negative { -cutoff - 1 } else { cutoff })
    }

    fn new_rows(&mut self, w: usize, h: usize) -> Sr<Vec<u8>> {
        if w > 0xFFFF || h > 0xFFFF {
            return Err(damage("Damaged DjVu file: a JB2 symbol is too large"));
        }
        self.budget -= (w * h) as i64;
        if self.budget < 0 {
            return Err(damage("Damaged DjVu file: a JB2 stream draws more than its page holds"));
        }
        Ok(vec![0u8; (w + PAD_L + PAD_R) * h])
    }

    fn direct_bitmap(&mut self, w: usize, h: usize) -> Sr<Vec<u8>> {
        let mut rows = self.new_rows(w, h)?;
        let stride = w + PAD_L + PAD_R;
        let zero = vec![0u8; stride];
        for t in 0..h {
            let (before, rest) = rows.split_at_mut(t * stride);
            let up0 = &mut rest[..stride];
            let up1: &[u8] = if t >= 1 { &before[(t - 1) * stride..t * stride] } else { &zero };
            let up2: &[u8] = if t >= 2 { &before[(t - 2) * stride..(t - 1) * stride] } else { &zero };
            let mut c: usize = ((up2[1] as usize) << 9)
                | ((up2[2] as usize) << 8)
                | ((up2[3] as usize) << 7)
                | ((up1[0] as usize) << 6)
                | ((up1[1] as usize) << 5)
                | ((up1[2] as usize) << 4)
                | ((up1[3] as usize) << 3)
                | ((up1[4] as usize) << 2);
            for x in 0..w {
                let n = self.zp.decode(&mut self.direct[c])? as usize;
                if n != 0 {
                    up0[x + 2] = 1;
                }
                c = ((c << 1) & 0x37A) | ((up1[x + 5] as usize) << 2) | ((up2[x + 4] as usize) << 7) | n;
            }
        }
        Ok(rows)
    }

    fn cross_bitmap(&mut self, w: usize, h: usize, rf: &Shape) -> Sr<Vec<u8>> {
        let mut rows = self.new_rows(w, h)?;
        let stride = w + PAD_L + PAD_R;
        let (left, bottom, right, top) = rf.bbox;
        let (wi, hi) = (w as i32, h as i32);
        let xd2c = (wi / 2 - wi + 1) - ((right - left + 1) / 2 - right);
        let yd2c = (hi / 2 - hi + 1) - ((top - bottom + 1) / 2 - top);
        let ydiff = rf.h as i32 - hi - yd2c;
        let rstride = rf.w + PAD_L + PAD_R;
        let zero = vec![0u8; stride];
        // the reference rows aligned under the new symbol's columns
        let align = |r: i64| -> Vec<u8> {
            if r < 0 || r >= rf.h as i64 {
                return zero.clone();
            }
            let src = &rf.bits[r as usize * rstride..(r as usize + 1) * rstride];
            let mut got = vec![0u8; stride];
            let lo = (-(PAD_L as i64)).max(-(PAD_L as i64) - xd2c as i64);
            let hi2 = (w as i64 + PAD_R as i64).min(rf.w as i64 + PAD_R as i64 - xd2c as i64);
            let mut c = lo;
            while c < hi2 {
                got[(c + PAD_L as i64) as usize] = src[(c + xd2c as i64 + PAD_L as i64) as usize];
                c += 1;
            }
            got
        };
        let mut xup1 = align(ydiff as i64 - 1);
        let mut xup0 = align(ydiff as i64);
        for t in 0..h {
            let r = t as i64 + ydiff as i64;
            let xdn1 = align(r + 1);
            let (before, rest) = rows.split_at_mut(t * stride);
            let up0 = &mut rest[..stride];
            let up1: &[u8] = if t >= 1 { &before[(t - 1) * stride..t * stride] } else { &zero };
            let mut c: usize = ((up1[1] as usize) << 10)
                | ((up1[2] as usize) << 9)
                | ((up1[3] as usize) << 8)
                | ((xup1[2] as usize) << 6)
                | ((xup0[1] as usize) << 5)
                | ((xup0[2] as usize) << 4)
                | ((xup0[3] as usize) << 3)
                | ((xdn1[1] as usize) << 2)
                | ((xdn1[2] as usize) << 1)
                | (xdn1[3] as usize);
            for x in 0..w {
                let n = self.zp.decode(&mut self.cross[c])? as usize;
                if n != 0 {
                    up0[x + 2] = 1;
                }
                c = ((c << 1) & 0x636)
                    | ((up1[x + 4] as usize) << 8)
                    | ((xup1[x + 3] as usize) << 6)
                    | ((xup0[x + 4] as usize) << 3)
                    | (xdn1[x + 4] as usize)
                    | (n << 7);
            }
            xup1 = std::mem::replace(&mut xup0, xdn1);
        }
        Ok(rows)
    }

    fn start_image(&mut self, w: i32, h: i32) {
        self.last_left = w + 1;
        self.last_row_left = 0;
        self.last_row_bottom = h;
        self.last_right = 0;
        self.last_bottom = h;
        self.short = [h, h, h];
        self.short_pos = 0;
        self.started = true;
    }

    fn relative_location(&mut self, rows: i32, cols: i32) -> Sr<(i32, i32)> {
        if !self.started {
            return Err(damage("Damaged DjVu file: a JB2 symbol comes before the start of the image"));
        }
        let (left, bottom);
        if self.zp.decode(&mut self.flags[1])? != 0 {
            left = self.last_row_left + self.code_num(BIGNEGATIVE, BIGPOSITIVE, C_REL_X_LAST)?;
            let top = self.last_row_bottom + self.code_num(BIGNEGATIVE, BIGPOSITIVE, C_REL_Y_LAST)?;
            let right = left + cols - 1;
            bottom = top - rows + 1;
            self.last_left = left;
            self.last_row_left = left;
            self.last_right = right;
            self.last_bottom = bottom;
            self.last_row_bottom = bottom;
            self.short = [bottom, bottom, bottom];
            self.short_pos = 0;
        } else {
            left = self.last_right + self.code_num(BIGNEGATIVE, BIGPOSITIVE, C_REL_X_CUR)?;
            bottom = self.last_bottom + self.code_num(BIGNEGATIVE, BIGPOSITIVE, C_REL_Y_CUR)?;
            let right = left + cols - 1;
            self.last_left = left;
            self.last_right = right;
            self.short_pos = (self.short_pos + 1) % 3;
            self.short[self.short_pos] = bottom;
            let mut s = self.short;
            s.sort();
            self.last_bottom = s[1];
        }
        Ok((left - 1, bottom - 1))
    }

    fn add_shape(&mut self, w: usize, h: usize, rows: Vec<u8>) -> u32 {
        self.out.shapes.push(Arc::new(Shape::new(w, h, rows)));
        (self.out.shapes.len() - 1) as u32
    }

    fn matched(&mut self) -> Sr<u32> {
        if self.lib.is_empty() {
            return Err(damage("Damaged DjVu file: a JB2 symbol refers to an empty library"));
        }
        let n = self.lib.len() as i32;
        let i = self.code_num(0, n - 1, C_MATCH)?;
        Ok(self.lib[i as usize])
    }

    fn records(&mut self) -> Sr<()> {
        let image = !self.is_dict;
        for _ in 0..MAX_RECORDS {
            let kind = self.code_num(0, 11, C_RECORD)?;
            match kind {
                0 => {
                    if self.started {
                        return Err(damage("Damaged DjVu file: a JB2 stream starts twice"));
                    }
                    let w = self.code_num(0, BIGPOSITIVE, C_IMAGE_SIZE)?;
                    let h = self.code_num(0, BIGPOSITIVE, C_IMAGE_SIZE)?;
                    if image {
                        if w == 0 || h == 0 {
                            return Err(damage("Damaged DjVu file: a JB2 image has no size"));
                        }
                        if w as u64 * h as u64 > MAX_PIXELS {
                            return Err(Stop::Damage(Damage::new(&format!(
                                "A picture in this book is {} x {} pixels, more than bookport reads",
                                w, h
                            ))));
                        }
                    } else if w != 0 || h != 0 {
                        return Err(damage("Damaged DjVu file: a JB2 dictionary has an image size"));
                    }
                    self.out.width = w as usize;
                    self.out.height = h as usize;
                    self.budget = if image { 4 * w as i64 * h as i64 + 4_000_000 } else { MAX_PIXELS as i64 };
                    self.zp.decode(&mut self.flags[0])?;
                    self.start_image(w, h);
                    if let Some(d) = self.dict {
                        if self.out.inherited > 0 {
                            self.out.shapes = d.shapes[..self.out.inherited].to_vec();
                            self.lib = (0..self.out.inherited as u32).collect();
                        }
                    }
                }
                9 => {
                    if self.started {
                        self.num.reset();
                        continue;
                    }
                    let n = self.code_num(0, BIGPOSITIVE, C_INHERIT)? as usize;
                    if n > 0 && self.dict.map_or(true, |d| d.shapes.len() < n) {
                        return Err(damage("Damaged DjVu file: a page needs a shape dictionary it doesn't have"));
                    }
                    self.out.inherited = n;
                }
                11 => return Ok(()),
                10 => {
                    let n = self.code_num(0, BIGPOSITIVE, C_COMMENT_LEN)?;
                    for _ in 0..n {
                        self.code_num(0, 255, C_COMMENT_BYTE)?;
                    }
                }
                _ if !self.started => {
                    return Err(damage("Damaged DjVu file: a JB2 symbol comes before the start of the image"));
                }
                1 | 2 | 3 | 8 => {
                    if !image && kind != 2 {
                        return Err(damage("Damaged DjVu file: a JB2 dictionary holds an image record"));
                    }
                    let w = self.code_num(0, BIGPOSITIVE, C_SIZE_X)? as usize;
                    let h = self.code_num(0, BIGPOSITIVE, C_SIZE_Y)? as usize;
                    let rows = self.direct_bitmap(w, h)?;
                    let mut place = (0, 0);
                    if kind == 8 {
                        let left = self.code_num(1, self.out.width as i32, C_ABS_X)?;
                        let top = self.code_num(1, self.out.height as i32, C_ABS_Y)?;
                        place = (left - 1, top - h as i32);
                    } else if kind != 2 {
                        place = self.relative_location(h as i32, w as i32)?;
                    }
                    let shape = self.add_shape(w, h, rows);
                    if kind == 1 || kind == 2 {
                        self.lib.push(shape);
                    }
                    if kind != 2 {
                        self.out.blits.push((place.0, place.1, shape));
                    }
                }
                4..=6 => {
                    if !image && kind != 5 {
                        return Err(damage("Damaged DjVu file: a JB2 dictionary holds an image record"));
                    }
                    let m = self.matched()?;
                    let rf = self.out.shapes[m as usize].clone();
                    let (left, bottom, right, top) = rf.bbox;
                    let w = right - left + 1 + self.code_num(BIGNEGATIVE, BIGPOSITIVE, C_REL_W)?;
                    let h = top - bottom + 1 + self.code_num(BIGNEGATIVE, BIGPOSITIVE, C_REL_H)?;
                    if w < 0 || h < 0 {
                        return Err(damage("Damaged DjVu file: a JB2 symbol has a negative size"));
                    }
                    let rows = self.cross_bitmap(w as usize, h as usize, &rf)?;
                    let mut place = (0, 0);
                    if kind != 5 {
                        place = self.relative_location(h, w)?;
                    }
                    let shape = self.add_shape(w as usize, h as usize, rows);
                    if kind != 6 {
                        self.lib.push(shape);
                    }
                    if kind != 5 {
                        self.out.blits.push((place.0, place.1, shape));
                    }
                }
                7 => {
                    if !image {
                        return Err(damage("Damaged DjVu file: a JB2 dictionary holds an image record"));
                    }
                    let shape = self.matched()?;
                    let (left, bottom, right, top) = self.out.shapes[shape as usize].bbox;
                    let (x, y) = self.relative_location(top - bottom + 1, right - left + 1)?;
                    self.out.blits.push((x - left, y - bottom, shape));
                }
                _ => return Err(damage("Damaged DjVu file: a JB2 record has an unknown type")),
            }
        }
        Err(damage("Damaged DjVu file: a JB2 stream has too many records"))
    }
}

pub fn decode(data: &[u8], dict: Option<&Jb2Data>, is_dict: bool) -> Res<Jb2Data> {
    let out = Jb2Data { width: 0, height: 0, shapes: Vec::new(), blits: Vec::new(), inherited: 0, truncated: false };
    let zp = match Zp::new(data) {
        Ok(z) => z,
        Err(_) => return Err(Damage::new("Damaged DjVu file: a JB2 stream has no start record")),
    };
    let mut d = Decoder {
        zp,
        num: NumCoder::new(),
        flags: [0; 2],
        direct: vec![0; 1024],
        cross: vec![0; 2048],
        dict,
        is_dict,
        out,
        lib: Vec::new(),
        started: false,
        budget: 0,
        last_left: 0,
        last_row_left: 0,
        last_row_bottom: 0,
        last_right: 0,
        last_bottom: 0,
        short: [0; 3],
        short_pos: 0,
    };
    match d.records() {
        Ok(()) => {}
        Err(Stop::End) => d.out.truncated = true,
        Err(Stop::Damage(e)) => return Err(e),
    }
    if !d.started {
        return Err(Damage::new("Damaged DjVu file: a JB2 stream has no start record"));
    }
    Ok(d.out)
}

/// The placements (all, or the ones listed) drawn as packed rows, top first, 1 = black.
pub fn mask(img: &Jb2Data, which: Option<&[usize]>) -> Vec<u8> {
    let (w, h) = (img.width, img.height);
    let nb = (w + 7) / 8;
    let mut out = vec![0u8; nb * h];
    let mut draw = |i: usize| {
        let (left, bottom, s) = img.blits[i];
        let shape = &img.shapes[s as usize];
        let sh = shape.h as i64;
        let top = h as i64 - bottom as i64 - sh;
        let snb = (shape.w + 7) / 8;
        for t in 0..sh {
            let y = top + t;
            if y < 0 || y >= h as i64 {
                continue;
            }
            let src = &shape.packed[t as usize * snb..(t as usize + 1) * snb];
            let dst = &mut out[y as usize * nb..(y as usize + 1) * nb];
            or_row(dst, src, shape.w, left as i64, w);
        }
    };
    match which {
        None => {
            for i in 0..img.blits.len() {
                draw(i);
            }
        }
        Some(list) => {
            for &i in list {
                if i < img.blits.len() {
                    draw(i);
                }
            }
        }
    }
    out
}

/// OR a packed row of `sw` pixels into `dst` at column `left` (clipped to `w` columns).
fn or_row(dst: &mut [u8], src: &[u8], sw: usize, left: i64, w: usize) {
    let shift = left.rem_euclid(8) as u32;
    let first = left.div_euclid(8);
    let n = dst.len() as i64;
    let used = (sw + 7) / 8;
    for (k, &b) in src[..used].iter().enumerate() {
        if b == 0 {
            continue;
        }
        let at = first + k as i64;
        if (0..n).contains(&at) {
            dst[at as usize] |= b >> shift;
        }
        if shift != 0 && (0..n).contains(&(at + 1)) {
            dst[at as usize + 1] |= ((b as u32) << (8 - shift)) as u8;
        }
    }
    // no bits past the page's last column
    if w % 8 != 0 && n > 0 {
        let last = dst.len() - 1;
        dst[last] &= !(0xFFu8 >> (w % 8));
    }
}
