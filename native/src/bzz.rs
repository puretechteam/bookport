//! BZZ (DjVu v3 specification, appendix 4), as bookport.djvu.bzz.

use crate::zp::Zp;
use crate::{Damage, Res};

const MAX_BLOCK: usize = 4096 * 1024;

fn raw(zp: &mut Zp, bits: u32) -> Res<usize> {
    let mut n = 1usize;
    let m = 1usize << bits;
    while n < m {
        n = (n << 1) | zp.passthru()? as usize;
    }
    Ok(n - m)
}

pub fn decode(data: &[u8], limit: usize) -> Res<Vec<u8>> {
    let mut zp = Zp::new(data)?;
    let mut ctx = [0u8; 300];
    let mut out: Vec<u8> = Vec::new();
    loop {
        let size = raw(&mut zp, 24)?;
        if size == 0 {
            break;
        }
        if size > MAX_BLOCK {
            return Err(Damage::new("Damaged DjVu file: a compressed block is too large"));
        }
        if out.len() + size - 1 > limit {
            return Err(Damage::too_big(limit));
        }
        block(&mut zp, &mut ctx, size, &mut out)?;
    }
    Ok(out)
}

fn block(zp: &mut Zp, ctx: &mut [u8; 300], size: usize, out: &mut Vec<u8>) -> Res<()> {
    let mut fshift = 0u32;
    if zp.passthru()? != 0 {
        fshift = if zp.passthru()? != 0 { 2 } else { 1 };
    }
    let mut mtf = [0u8; 256];
    for (i, m) in mtf.iter_mut().enumerate() {
        *m = i as u8;
    }
    let mut freq = [0u32; 4];
    let mut fadd: u32 = 4;
    let mut mtfno: usize = 3;
    let mut marker: Option<usize> = None;
    let mut data = vec![0u8; size];
    for i in 0..size {
        let c = if mtfno < 2 { mtfno } else { 2 };
        if zp.decode(&mut ctx[c])? != 0 {
            mtfno = 0;
        } else if zp.decode(&mut ctx[c + 3])? != 0 {
            mtfno = 1;
        } else {
            mtfno = 256;
            let mut base = 6usize;
            for bits in 1..8u32 {
                if zp.decode(&mut ctx[base])? != 0 {
                    let mut n = 1usize;
                    let top = 1usize << bits;
                    while n < top {
                        n = (n << 1) | zp.decode(&mut ctx[base + n])? as usize;
                    }
                    mtfno = n;
                    break;
                }
                base += 1 << bits;
            }
            if mtfno == 256 {
                data[i] = 0;
                marker = Some(i);
                continue;
            }
        }
        let sym = mtf[mtfno];
        data[i] = sym;
        fadd += fadd >> fshift;
        if fadd > 0x1000_0000 {
            fadd >>= 24;
            for f in freq.iter_mut() {
                *f >>= 24;
            }
        }
        let fc = fadd + if mtfno < 4 { freq[mtfno] } else { 0 };
        let mut k = mtfno;
        while k > 3 {
            mtf[k] = mtf[k - 1];
            k -= 1;
        }
        while k > 0 && fc >= freq[k - 1] {
            mtf[k] = mtf[k - 1];
            freq[k] = freq[k - 1];
            k -= 1;
        }
        mtf[k] = sym;
        freq[k] = fc;
    }
    let marker = match marker {
        Some(m) => m,
        None => return Err(Damage::new("Damaged DjVu file: a compressed block has no end marker")),
    };
    // the inverse Burrows-Wheeler transform
    let mut count = [0usize; 256];
    let mut posn = vec![0u32; size];
    for (i, &c) in data.iter().enumerate() {
        if i != marker {
            posn[i] = count[c as usize] as u32;
            count[c as usize] += 1;
        }
    }
    let mut last = 1usize;
    for c in count.iter_mut() {
        let t = *c;
        *c = last;
        last += t;
    }
    let start = out.len();
    out.resize(start + size - 1, 0);
    let mut k = 0usize;
    for at in (0..size - 1).rev() {
        let c = data[k];
        out[start + at] = c;
        k = count[c as usize] + posn[k] as usize;
        if k >= size {
            return Err(Damage::new("Damaged DjVu file: a compressed block doesn't decode"));
        }
    }
    if k != marker {
        return Err(Damage::new("Damaged DjVu file: a compressed block doesn't decode"));
    }
    Ok(())
}
