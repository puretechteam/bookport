//! bookport's DjVu decoders in Rust: the same results as its pure-Python ones, faster. bookport
//! uses them when this module is installed and falls back to Python when it isn't (or when
//! BOOKPORT_PURE=1). Damage is reported as bookport's own BookportError, as the Python code does;
//! nothing here panics on bad input (out-of-range reads are checked).

// The decoders follow bookport's Python code line for line (so the two can be read side by side and
// give the same bytes), which some of clippy's style rules would rearrange.
#![allow(
    clippy::needless_range_loop,
    clippy::manual_div_ceil,
    clippy::too_many_arguments,
    clippy::type_complexity,
    clippy::int_plus_one,
    clippy::implicit_saturating_sub,
    clippy::assign_op_pattern,
    clippy::unnecessary_map_or,
    clippy::manual_is_multiple_of,
    clippy::needless_late_init
)]

use pyo3::prelude::*;
use pyo3::pybacked::PyBackedBytes;
use pyo3::types::PyBytes;

mod bzz;
mod g4;
mod iw44;
mod jb2;
mod jbig2;
mod jpeg;
mod scale;
mod zp;

/// A decoding failure, carried out of code that runs without the GIL.
#[derive(Debug)]
pub struct Damage(pub String);

impl Damage {
    pub fn new(msg: &str) -> Self {
        Damage(msg.to_string())
    }
    pub fn too_big(limit: usize) -> Self {
        Damage(format!("Part of this book unpacks to more than {} MB, so bookport won't read it", limit / (1024 * 1024)))
    }
}

impl From<zp::EndOfData> for Damage {
    fn from(_: zp::EndOfData) -> Damage {
        Damage::new("Damaged DjVu file: compressed data ends too soon")
    }
}

pub type Res<T> = Result<T, Damage>;

fn raise(py: Python<'_>, d: Damage) -> PyErr {
    match py.import("bookport.errors").and_then(|m| m.getattr("BookportError")) {
        Ok(cls) => PyErr::from_value(cls.call1((d.0,)).unwrap_or_else(|e| e.into_value(py).into_bound(py).into_any())),
        Err(e) => e,
    }
}

#[pyfunction]
fn bzz_decode<'py>(py: Python<'py>, data: PyBackedBytes, limit: usize) -> PyResult<Bound<'py, PyBytes>> {
    let out = py.detach(|| bzz::decode(&data, limit)).map_err(|e| raise(py, e))?;
    Ok(PyBytes::new(py, &out))
}

/// A decoded JB2 image or dictionary.
#[pyclass(frozen, module = "bookport_native")]
struct Jb2 {
    data: jb2::Jb2Data,
}

#[pymethods]
impl Jb2 {
    #[getter]
    fn width(&self) -> usize {
        self.data.width
    }
    #[getter]
    fn height(&self) -> usize {
        self.data.height
    }
    #[getter]
    fn truncated(&self) -> bool {
        self.data.truncated
    }
    #[getter]
    fn blit_count(&self) -> usize {
        self.data.blits.len()
    }
    #[getter]
    fn shape_count(&self) -> usize {
        self.data.shapes.len()
    }
    /// The placements (all, or those listed by number) as packed rows, top first, 1 = black.
    #[pyo3(signature = (which=None))]
    fn mask<'py>(&self, py: Python<'py>, which: Option<Vec<usize>>) -> Bound<'py, PyBytes> {
        let out = py.detach(|| jb2::mask(&self.data, which.as_deref()));
        PyBytes::new(py, &out)
    }
}

#[pyfunction]
#[pyo3(signature = (data, dictionary=None, is_dict=false))]
fn jb2_decode(py: Python<'_>, data: PyBackedBytes, dictionary: Option<PyRef<'_, Jb2>>, is_dict: bool) -> PyResult<Jb2> {
    let dict = dictionary.as_ref().map(|d| &d.data);
    let out = py.detach(|| jb2::decode(&data, dict, is_dict)).map_err(|e| raise(py, e))?;
    Ok(Jb2 { data: out })
}

#[pyfunction]
fn iw44_decode<'py>(py: Python<'py>, chunks: Vec<PyBackedBytes>) -> PyResult<(usize, usize, &'static str, Bound<'py, PyBytes>)> {
    let (w, h, nc, px) = py
        .detach(|| {
            let refs: Vec<&[u8]> = chunks.iter().map(|c| &c[..]).collect();
            iw44::decode(&refs)
        })
        .map_err(|e| raise(py, e))?;
    Ok((w, h, if nc == 3 { "RGB" } else { "L" }, PyBytes::new(py, &px)))
}

fn check(py: Python<'_>, w: usize, h: usize, nc: usize, px: &[u8], width: usize, height: usize, red: usize) -> PyResult<()> {
    if w == 0 || h == 0 || red == 0 || px.len() < w * h * nc || (width as u64) * (height as u64) > jb2::MAX_PIXELS {
        return Err(raise(py, Damage::new("Damaged DjVu file: a layer doesn't fit its page")));
    }
    Ok(())
}

#[pyfunction]
fn upscale<'py>(py: Python<'py>, w: usize, h: usize, nc: usize, px: PyBackedBytes, width: usize, height: usize,
                red: usize) -> PyResult<Bound<'py, PyBytes>> {
    check(py, w, h, nc, &px, width, height, red)?;
    let out = py.detach(|| scale::upscale(w, h, nc, &px, width, height, red));
    Ok(PyBytes::new(py, &out))
}

#[pyfunction]
fn blocks<'py>(py: Python<'py>, w: usize, h: usize, nc: usize, px: PyBackedBytes, width: usize, height: usize,
               red: usize) -> PyResult<Bound<'py, PyBytes>> {
    check(py, w, h, nc, &px, width, height, red)?;
    let out = py.detach(|| scale::blocks(w, h, nc, &px, width, height, red));
    Ok(PyBytes::new(py, &out))
}

#[pyfunction]
#[pyo3(signature = (w, h, nc, base, masks, fg=None))]
fn paint<'py>(py: Python<'py>, w: usize, h: usize, nc: usize, base: PyBackedBytes,
              masks: Vec<(Option<(u8, u8, u8)>, PyBackedBytes)>, fg: Option<PyBackedBytes>) -> PyResult<Bound<'py, PyBytes>> {
    let nb = (w + 7) / 8;
    if base.len() < w * h * nc || masks.iter().any(|(_, m)| m.len() < nb * h)
        || fg.as_ref().is_some_and(|f| f.len() < w * h * nc) {
        return Err(raise(py, Damage::new("Damaged DjVu file: a layer doesn't fit its page")));
    }
    let mut out = base.to_vec();
    py.detach(|| {
        let ms: Vec<(Option<[u8; 3]>, &[u8])> = masks.iter().map(|(c, m)| (c.map(|(r, g, b)| [r, g, b]), &m[..])).collect();
        scale::paint(w, h, nc, &mut out, &ms, fg.as_deref());
    });
    Ok(PyBytes::new(py, &out))
}

#[pyfunction]
fn g4_encode<'py>(py: Python<'py>, packed: PyBackedBytes, w: usize, h: usize) -> PyResult<Bound<'py, PyBytes>> {
    if packed.len() < (w + 7) / 8 * h {
        return Err(raise(py, Damage::new("A mask is shorter than its size")));
    }
    let out = py.detach(|| g4::encode(&packed, w, h));
    Ok(PyBytes::new(py, &out))
}

#[pyfunction]
fn jbig2_encode<'py>(py: Python<'py>, packed: PyBackedBytes, w: usize, h: usize) -> PyResult<Bound<'py, PyBytes>> {
    if packed.len() < (w + 7) / 8 * h {
        return Err(raise(py, Damage::new("A mask is shorter than its size")));
    }
    let out = py.detach(|| jbig2::encode(&packed, w, h));
    Ok(PyBytes::new(py, &out))
}

#[pyfunction]
fn jpeg_encode<'py>(py: Python<'py>, w: usize, h: usize, nc: usize, px: PyBackedBytes, quality: i32) -> PyResult<Bound<'py, PyBytes>> {
    if w == 0 || h == 0 || w > 0xFFFF || h > 0xFFFF || px.len() < w * h * nc || !(nc == 1 || nc == 3) {
        return Err(raise(py, Damage::new("A picture doesn't fit its size")));
    }
    let out = py.detach(|| jpeg::encode(w, h, nc, &px, quality));
    Ok(PyBytes::new(py, &out))
}

#[pymodule]
fn bookport_native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add("__version__", env!("CARGO_PKG_VERSION"))?;
    m.add_class::<Jb2>()?;
    m.add_function(wrap_pyfunction!(bzz_decode, m)?)?;
    m.add_function(wrap_pyfunction!(jb2_decode, m)?)?;
    m.add_function(wrap_pyfunction!(iw44_decode, m)?)?;
    m.add_function(wrap_pyfunction!(upscale, m)?)?;
    m.add_function(wrap_pyfunction!(blocks, m)?)?;
    m.add_function(wrap_pyfunction!(paint, m)?)?;
    m.add_function(wrap_pyfunction!(g4_encode, m)?)?;
    m.add_function(wrap_pyfunction!(jbig2_encode, m)?)?;
    m.add_function(wrap_pyfunction!(jpeg_encode, m)?)?;
    Ok(())
}
