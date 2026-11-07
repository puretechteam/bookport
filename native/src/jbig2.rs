//! JBIG2 (ITU-T T.88) generic-region encoding of a 1-bit mask for PDF's JBIG2Decode: the MQ
//! arithmetic coder with the 16-pixel template 0 and typical prediction (rows equal to the one
//! above cost one decision). Lossless; typically well under the size of CCITT G4 on scanned text.
//! The stream is PDF's embedded form: a page information segment and one immediate lossless
//! generic region segment.

// probability estimation (T.88 table E.1): Qe, next state after MPS, after LPS, switch MPS
const QE: [(u32, u8, u8, bool); 47] = [
    (0x5601, 1, 1, true), (0x3401, 2, 6, false), (0x1801, 3, 9, false), (0x0AC1, 4, 12, false),
    (0x0521, 5, 29, false), (0x0221, 38, 33, false), (0x5601, 7, 6, true), (0x5401, 8, 14, false),
    (0x4801, 9, 14, false), (0x3801, 10, 14, false), (0x3001, 11, 17, false), (0x2401, 12, 18, false),
    (0x1C01, 13, 20, false), (0x1601, 29, 21, false), (0x5601, 15, 14, true), (0x5401, 16, 14, false),
    (0x5101, 17, 15, false), (0x4801, 18, 16, false), (0x3801, 19, 17, false), (0x3401, 20, 18, false),
    (0x3001, 21, 19, false), (0x2801, 22, 19, false), (0x2401, 23, 20, false), (0x2201, 24, 21, false),
    (0x1C01, 25, 22, false), (0x1801, 26, 23, false), (0x1601, 27, 24, false), (0x1401, 28, 25, false),
    (0x1201, 29, 26, false), (0x1101, 30, 27, false), (0x0AC1, 31, 28, false), (0x09C1, 32, 29, false),
    (0x08A1, 33, 30, false), (0x0521, 34, 31, false), (0x0441, 35, 32, false), (0x02A1, 36, 33, false),
    (0x0221, 37, 34, false), (0x0141, 38, 35, false), (0x0111, 39, 36, false), (0x0085, 40, 37, false),
    (0x0049, 41, 38, false), (0x0025, 42, 39, false), (0x0015, 43, 40, false), (0x0009, 44, 41, false),
    (0x0005, 45, 42, false), (0x0001, 45, 43, false), (0x5601, 46, 46, false),
];

struct Mq {
    a: u32,
    c: u32,
    ct: u32,
    b: u32,
    started: bool,
    out: Vec<u8>,
    state: Vec<u8>, // per context: index << 1 | mps
}

impl Mq {
    fn new(contexts: usize) -> Mq {
        Mq { a: 0x8000, c: 0, ct: 12, b: 0, started: false, out: Vec::new(), state: vec![0; contexts] }
    }

    fn emit(&mut self) {
        if self.started {
            self.out.push(self.b as u8);
        }
        self.started = true;
    }

    fn byteout(&mut self) {
        if self.b == 0xFF {
            self.emit();
            self.b = self.c >> 20;
            self.c &= 0xFFFFF;
            self.ct = 7;
            return;
        }
        if self.c >= 0x8000000 {
            self.b += 1;
            if self.b == 0xFF {
                self.c &= 0x7FFFFFF;
                self.emit();
                self.b = self.c >> 20;
                self.c &= 0xFFFFF;
                self.ct = 7;
                return;
            }
        }
        self.emit();
        self.b = self.c >> 19;
        self.c &= 0x7FFFF;
        self.ct = 8;
    }

    #[inline]
    fn renorm(&mut self) {
        loop {
            self.a <<= 1;
            self.c <<= 1;
            self.ct -= 1;
            if self.ct == 0 {
                self.byteout();
            }
            if self.a & 0x8000 != 0 {
                break;
            }
        }
    }

    #[inline]
    fn encode(&mut self, cx: usize, d: u32) {
        let st = self.state[cx];
        let (i, mps) = ((st >> 1) as usize, (st & 1) as u32);
        let (qe, nmps, nlps, switch) = QE[i];
        self.a -= qe;
        if d == mps {
            if self.a & 0x8000 == 0 {
                if self.a < qe {
                    self.a = qe;
                } else {
                    self.c += qe;
                }
                self.state[cx] = (nmps << 1) | mps as u8;
                self.renorm();
            } else {
                self.c += qe;
            }
        } else {
            if self.a < qe {
                self.c += qe;
            } else {
                self.a = qe;
            }
            let m = if switch { mps ^ 1 } else { mps };
            self.state[cx] = (nlps << 1) | m as u8;
            self.renorm();
        }
    }

    fn flush(mut self) -> Vec<u8> {
        let tempc = self.c + self.a;
        self.c |= 0xFFFF;
        if self.c >= tempc {
            self.c -= 0x8000;
        }
        self.c <<= self.ct;
        self.byteout();
        self.c <<= self.ct;
        self.byteout();
        self.emit();
        if self.b != 0xFF {
            self.out.push(0xFF);
        }
        self.out.push(0xAC);
        self.out
    }
}

fn segment(out: &mut Vec<u8>, number: u32, kind: u8, data: &[u8]) {
    out.extend_from_slice(&number.to_be_bytes());
    out.push(kind);
    out.push(0); // refers to no other segment
    out.push(1); // page 1
    out.extend_from_slice(&(data.len() as u32).to_be_bytes());
    out.extend_from_slice(data);
}

/// The JBIG2 stream for packed rows (column 0 in the top bit, 1 = black).
pub fn encode(packed: &[u8], w: usize, h: usize) -> Vec<u8> {
    let nb = (w + 7) / 8;
    // rows as bytes with 4 zero columns on each side
    let stride = w + 8;
    let px = |row: &[u8], x: usize| -> u8 { (row[x / 8] >> (7 - x % 8)) & 1 };
    let mut rows = vec![0u8; stride * (h + 2)]; // two blank rows above the first
    for y in 0..h {
        let src = &packed[y * nb..(y + 1) * nb];
        let dst = &mut rows[(y + 2) * stride + 4..(y + 2) * stride + 4 + w];
        for (x, d) in dst.iter_mut().enumerate() {
            *d = px(src, x);
        }
    }
    let mut mq = Mq::new(65536);
    let mut ltp = 0u32;
    for y in 0..h {
        let r0 = (y + 2) * stride + 4;
        let r1 = r0 - stride;
        let r2 = r1 - stride;
        let same = rows[r0..r0 + w] == rows[r1..r1 + w];
        let want = same as u32;
        mq.encode(0x9B25, want ^ ltp);
        ltp = want;
        if same {
            continue;
        }
        let g = |i: usize| rows[i] as usize;
        // the template at x = 0, then slid one column at a time (bits: 0-3 this row x-1..x-4,
        // 4-10 the row above x+3..x-3, 11-15 two rows up x+2..x-2)
        let mut cx = g(r1 + 3) << 4 | g(r1 + 2) << 5 | g(r1 + 1) << 6 | g(r1) << 7 | g(r2 + 2) << 11 | g(r2 + 1) << 12
            | g(r2) << 13;
        for x in 0..w {
            let d = rows[r0 + x] as usize;
            mq.encode(cx, d as u32);
            cx = ((cx << 1) & 0xF7EE) | d | g(r1 + x + 4) << 4 | g(r2 + x + 3) << 11;
        }
    }
    let coded = mq.flush();
    let mut out = Vec::with_capacity(coded.len() + 64);
    let mut page = Vec::new();
    page.extend_from_slice(&(w as u32).to_be_bytes());
    page.extend_from_slice(&(h as u32).to_be_bytes());
    page.extend_from_slice(&[0; 8]); // resolution unknown
    page.push(1); // eventually lossless
    page.extend_from_slice(&[0, 0]); // not striped
    segment(&mut out, 0, 48, &page);
    let mut region = Vec::new();
    region.extend_from_slice(&(w as u32).to_be_bytes());
    region.extend_from_slice(&(h as u32).to_be_bytes());
    region.extend_from_slice(&[0; 8]); // at 0, 0
    region.push(0); // combination: OR
    region.push(0b1000); // arithmetic, template 0, typical prediction on
    region.extend_from_slice(&[3, 0xFF, 0xFD, 0xFF, 2, 0xFE, 0xFE, 0xFE]); // A1..A4 at their usual places
    region.extend_from_slice(&coded);
    segment(&mut out, 1, 39, &region);
    out
}
