# bookport

Read and write ebooks in every common and rare format, in pure Python. No Calibre, no dependencies.

```
pip install git+https://github.com/puretechteam/bookport
```

```python
import bookport

book = bookport.read("novel.mobi")                  # any supported format -> Book
book.write("novel.epub")                             # format from the extension
book.write("novel.pdf", page_size="ereader", text_size="large")
bookport.convert("story.fb2", "story.azw3")
bookport.formats()                                   # {"epub": {"name": "EPUB", "read": True, "write": True}, ...}
```

or from a terminal:

```
bookport novel.mobi novel.epub
bookport novel.epub novel.pdf --page-size a5 --text-size normal
bookport novel.fb2 novel.book --to epub --epub-version 2
bookport --info novel.azw3
bookport --formats
```

`--to` picks the output format when the file name's extension doesn't, and `--epub-version 2` writes EPUB 2 instead of EPUB 3. `--page-size` takes phone, ereader, a5, a4 or letter and `--text-size` small, normal or large; anything else is refused before the book is read.

## Why bookport?

Converting ebooks has meant installing Calibre: about 670 MB, GPL, and a program to drive from the outside. bookport is a 3 MB library (most of it the embedded fonts) with nothing else to install, MIT licensed, that runs anywhere Python 3.10+ does and bundles cleanly into an app.

It was written for [ConvertAll](https://github.com/puretechteam/ConvertAll), a drag-and-drop file converter, to replace Calibre there. It's a sibling of [meshport](https://github.com/puretechteam/meshport) (3D) and [packport](https://github.com/puretechteam/packport) (archives).

## bookport and Calibre, side by side

Both tools converted the same books on the same Windows 11 machine, one conversion at a time, each in a fresh process (so start-up time counts for both), with Calibre 9.17.0's `ebook-convert` and bookport on Python 3.13. Each pair is **Calibre / bookport**.

| | Calibre | bookport |
|---|---|---|
| Install size | 666.5 MB | **3.0 MB** |
| Python documentation CHM to EPUB | 381.8 s | **20.5 s** (same 4,171 TOC entries, 100% of Calibre's words) |

**From EPUB** (13 books from Project Gutenberg and Standard Ebooks, among them a Chinese novel, poetry, and books with tables, footnotes and pictures):

| Output | converted (Calibre / bookport) | median s | peak MB | words kept %, worst book, read by bookport | words kept %, worst book, read by Calibre | TOC titles kept | images kept | metadata fields kept | cover kept |
|---|---|---|---|---|---|---|---|---|---|
| MOBI | 13/13 / 13/13 | 1.60 / **0.96** | 103 / **30** | 99.95 / 100.00 | 100.00 / 100.00 | 986 / 986 of 986 | 186 / 189 of 189 | 84 / 84 of 84 | 13 / 13 of 13 |
| AZW3 | 13/13 / 13/13 | 0.93 / **0.49** | 102 / **25** | 100.00 / 100.00 | 100.00 / 100.00 | 986 / 986 of 986 | 186 / 189 of 189 | 84 / 84 of 84 | 13 / 13 of 13 |
| FB2 | 13/13 / 13/13 | 1.08 / **0.36** | 106 / **30** | 99.92 / 99.96 | 100.00 / 100.00 | 11 / 983 of 986 | 183 / 183 of 189 | 84 / 84 of 84 | 13 / 13 of 13 |
| TXT | 13/13 / 13/13 | 0.97 / **0.44** | 90 / **26** | 99.92 / 99.96 | 99.83 / 99.84 | 908 / 923 of 986 | 0 / 0 of 189 | 49 / 49 of 84 | 0 / 0 of 13 |
| HTMLZ | 13/13 / 13/13 | 0.93 / **0.31** | 98 / **24** | 100.00 / 100.00 | 100.00 / 100.00 | 980 / 980 of 986 | 183 / 183 of 189 | 84 / 84 of 84 | 13 / 13 of 13 |
| TXTZ | 13/13 / 13/13 | 0.99 / **0.33** | 90 / **23** | 99.92 / 99.99 | 100.00 / 100.00 | 908 / 979 of 986 | 0 / 183 of 189 | 49 / 84 of 84 | 0 / 13 of 13 |
| DOCX | 13/13 / 13/13 | 2.26 / **0.54** | 106 / **30** | 99.98 / 100.00 | 100.00 / 100.00 | 942 / 979 of 986 | 183 / 183 of 189 | 81 / 81 of 84 | 13 / 13 of 13 |
| RTF | 13/13 / 13/13 | 1.00 / **0.51** | 100 / **27** | 99.91 / 99.95 | 99.99 / 100.00 | 967 / 979 of 986 | 174 / 186 of 189 | 52 / 84 of 84 | 3 / 13 of 13 |
| eReader PDB | 13/13 / 13/13 | 0.83 / **0.70** | 92 / **42** | 2.43 / 99.92 | 2.41 / 2.87 | 908 / 982 of 986 | 0 / 183 of 189 | 49 / 55 of 84 | 0 / 13 of 13 |
| PMLZ | 13/13 / 13/13 | 1.63 / **0.77** | 102 / **40** | 2.43 / 99.92 | 2.43 / 2.87 | 975 / 982 of 986 | 183 / 183 of 189 | 26 / 84 of 84 | 13 / 13 of 13 |
| TCR | 13/13 / 13/13 | 16.70 / **0.59** | 112 / **37** | 99.92 / 99.96 | 99.83 / 99.84 | 908 / 923 of 986 | 0 / 0 of 189 | 49 / 49 of 84 | 0 / 0 of 13 |
| RB | 13/13 / 13/13 | 1.00 / **0.64** | 97 / **39** | 0.00 / 99.92 | 0.00 / 100.00 | 821 / 979 of 986 | 181 / 183 of 189 | 39 / 39 of 84 | 11 / 13 of 13 |
| SNB | 13/13 / 13/13 | 1.44 / **0.54** | 105 / **34** | 99.91 / 99.92 | 99.99 / 100.00 | 790 / 985 of 986 | 183 / 183 of 189 | 58 / 58 of 84 | 13 / 13 of 13 |
| LRF | 13/13 / 13/13 | 1.15 / **0.60** | 104 / **27** | 99.53 / 99.92 | 99.50 / 99.53 | 985 / 985 of 986 | 103 / 183 of 189 | 71 / 71 of 84 | 0 / 13 of 13 |
| LIT | 13/13 / 13/13 | 1.71 / **0.27** | 100 / **30** | 100.00 / 100.00 | 100.00 / 100.00 | 986 / 986 of 986 | 183 / 189 of 189 | 84 / 84 of 84 | 13 / 13 of 13 |
| PDF | 12/13 / 13/13 | 8.91 / **1.40** | 1138 / **40** | 0.00 / 98.50 | 0.00 / 85.15 | 954 / 986 of 986 | 182 / 183 of 189 | 60 / 81 of 84 | 12 / 13 of 13 |

**To EPUB** (the Gutenberg and Standard Ebooks Kindle files, and 34 files made from three of the books in the formats that are hard to find on the web: PDF, DOCX, RTF, FB2, eReader, PMLZ, TCR, RB, SNB, LRF, LIT and Plucker):

| Input | converted (Calibre / bookport) | median s | peak MB | epubcheck errors | words kept %, worst book, read by bookport | words kept %, worst book, read by Calibre | TOC titles kept | images kept | metadata fields kept | cover kept |
|---|---|---|---|---|---|---|---|---|---|---|
| MOBI and AZW3 (23 files) | 23/23 / 23/23 | 0.88 / **0.38** | 89 / **28** | 7212 / **0** | 100.00 / 100.00 | 51.66 / 100.00 | 1845 / 1845 of 1845 | 358 / 390 of 390 | 141 / 141 of 141 | 23 / 23 of 23 |
| Other formats (34 files) | 34/34 / 34/34 | 1.09 / **0.30** | 106 / **21** | 2016 / **0** | 99.99 / 100.00 | 99.99 / 100.00 | 650 / 870 of 870 | 39 / 39 of 39 | 147 / 150 of 150 | 24 / 24 of 24 |

How to read the columns:

- **median s** and **peak MB**: the whole process tree's time and peak memory, the median over all the books.
- **words kept**: the share of the source's words found in the output, for the worst book. The text of both is extracted by the same reader: bookport, then Calibre. For "Other formats", the reference is the EPUB those files were made from, so neither tool is scored against its own reading of a lossy file.
- **TOC titles kept**: entries whose titles match the source's, so headings a tool invents don't count.
- **epubcheck errors**: errors that [epubcheck](https://github.com/w3c/epubcheck) 5.3.0 reports, summed over all the books.

Where both columns are low, the format is the limit, not the converter:

- Calibre's eReader and PML readers can't read Chinese, so the Chinese novel scores under 3% in the "read by Calibre" column whichever tool wrote it. bookport reads its own files of that book back at 100%.
- Calibre failed to convert Frankenstein to PDF (exit code 1), which is why its PDF recall is 0.00.
- Calibre's RB files of two books (the Chinese novel and Les Misérables) gave no text back in either reader.

To run the comparison yourself: `python benchmarks/fetch_corpus.py <folder> --tools` downloads the books and epubcheck, then `python benchmarks/compare_calibre.py <folder>/corpus --from epub --to mobi,pdf --tools <folder>/tools/tools.json --calibre-text` runs it (add `--originals <folder>/corpus` for a folder of files made from those books).

## Formats

| Format | Read | Write |
|---|---|---|
| EPUB 2 and 3 | ✓ | ✓ (EPUB 3 with an NCX, or EPUB 2) |
| Kindle MOBI, AZW (MOBI 6 + KF8 in one file) | ✓ | ✓ |
| Kindle AZW3 (KF8) | ✓ | ✓ |
| FictionBook 2 (`.fb2`, `.fb2.zip`) | ✓ | ✓ |
| PDF | ✓ | ✓ |
| Word (DOCX) | ✓ | ✓ |
| RTF | ✓ | ✓ |
| OpenDocument text (ODT) | ✓ | |
| HTML, HTMLZ | ✓ | ✓ |
| Plain text, TXTZ, Markdown | ✓ | ✓ |
| Palm eReader, PalmDOC, zTXT (`.pdb`) | ✓ | ✓ |
| Plucker (`.pdb`) | ✓ (text and links) | |
| PML, PMLZ | ✓ | ✓ |
| Sony BBeB (LRF) | ✓ | ✓ |
| Microsoft Reader (LIT) | ✓ | ✓ |
| Compiled HTML Help (CHM) | ✓ | |
| Rocket eBook (RB) | ✓ | ✓ |
| Psion TCR | ✓ | ✓ |
| Shanda Bambook (SNB) | ✓ | ✓ |
| Comics: CBZ, CBR (stored pages), a folder of images, one image | ✓ (fixed layout) | |

Every book goes through one model, `bookport.Book`: metadata (title, authors, language, identifiers, publisher, date, description, subjects, series), a cover, a spine of XHTML documents, resources (images, stylesheets, fonts), a nested table of contents, landmarks and a page list. Anything read can be written to any writable format.

**DRM.** bookport never removes DRM. Protected books raise `bookport.DRMError` with a plain explanation: EPUB with Adobe ADEPT, Readium LCP or Apple FairPlay, encrypted Kindle and eReader books, LIT with encrypted content, PDFs that need a password, password-protected comic archives. EPUB font obfuscation (IDPF and Adobe) isn't DRM and is undone. Kindle KFX and Topaz books are refused with a clear message too.

**EPUB.** Written as EPUB 3.3 with an NCX as well, so EPUB 2 readers get a table of contents, or as pure EPUB 2 with `version=2`. Every document is cleaned into valid XHTML5 first (obsolete tags and attributes become CSS, broken links and fragments are fixed, nesting is repaired), so the result passes epubcheck with zero errors whatever the source. A cover page is added when the book has a cover.

**Kindle.** `.mobi` and `.azw` are joint files: a MOBI 6 part for old Kindles (simple HTML made from the book's styled text, with filepos links and an NCX index) and a KF8 part that keeps the book's XHTML and CSS (skeletons and fragments, kindle:pos links, NCX, skeleton, fragment and guide indexes). `.azw3` is KF8 alone. Text is PalmDOC compressed. Reading handles MOBI 6, KF8 and joint files from any tool, HUFF/CDIC compression included.

**PDF writing** is bookport's own typesetter. Text is justified where the book's CSS says so (a line is left ragged rather than stretched past 2.5 times its normal word spacing), lines also break after hyphens and dashes, headings stay with the lines after them, lists hang their markers, tables get measured columns and rows that continue across pages, images are scaled to the page, chapters start on new pages, and the cover gets a page of its own. Fonts are embedded as TrueType subsets (Noto Serif and Noto Sans Mono, OFL) with ToUnicode maps, so the text is searchable and copyable; characters the bundled fonts don't have come from a system font (on Windows, Microsoft YaHei, SimSun, MS Gothic or Malgun Gothic). Links, a bookmark outline from the TOC, page numbers and the metadata are included.

| `page_size` | Size | `text_size` small / normal / large |
|---|---|---|
| `phone` | 252 × 448 pt (3.5 × 6.2 in) | 8.5 / 10 / 12 pt |
| `ereader` | 259 × 346 pt (a 6-inch e-reader screen) | 8.5 / 10 / 12 pt |
| `a5` (default) | 148 × 210 mm | 9.5 / 11 / 13 pt |
| `a4` | 210 × 297 mm | 10 / 12 / 14 pt |
| `letter` | 8.5 × 11 in | 10 / 12 / 14 pt |

**PDF reading** is bookport's own parser: cross-reference tables and streams, object streams, incremental updates, damaged-file repair, Flate, LZW, ASCII85, ASCIIHex and run-length filters with predictors, RC4 and AES (128 and 256-bit, its own AES) with an empty user password. Page content is interpreted into positioned text (ToUnicode maps, standard encodings with Differences, CID fonts), then reflowed: lines into paragraphs by spacing and indents, larger text into headings, bold and italic kept, running headers, footers and page numbers dropped. A hyphen at the end of a line joins the two halves only if the joined word appears elsewhere in the book, so real compounds like "waistcoat-pocket" keep their hyphen. Images (JPEG as is, others as PNG) and the outline (as the TOC) are kept.

**Other formats.** DOCX reading follows styles (headings from style names and outline levels), numbered and bulleted lists, tables, images, hyperlinks, bookmarks, fields, footnotes and endnotes; writing makes real heading styles, lists, tables, images and internal links. RTF reading handles code pages per font, Unicode escapes, heading styles, tables, pictures, HYPERLINK fields, bookmarks and footnotes. CHM and LIT are decompressed with bookport's own LZX decoder; LIT's tokenized HTML and package are decoded into XHTML, metadata and TOC; LIT writing tokenizes them back, with the documents LZX-framed, a chapter tour from the TOC, the cover and images, and a directory that grows past one chunk for long books. LRF reading and writing cover pages, text blocks, italic and bold, images, the TOC object and the info block. eReader PDB and PMLZ carry the TOC (chapter index and PML headings), links, images and metadata.

**Images.** bookport decodes PNG, GIF and BMP itself and writes PNG, so formats that only take some image types get what they need. With Pillow installed it also converts WebP and TIFF.

Errors: damaged or unsupported files raise `bookport.BookportError`, and nothing else; DRM raises `bookport.DRMError` (a subclass). An option a format doesn't take raises `BookportError` too, naming the options it does take. A failed write leaves no half-written file behind, and a finished one gets the permissions any new file would.

**Books from anywhere.** bookport treats every book as hostile:

- Pictures, stylesheets, covers and other pages that a book links to on disk (HTML, Markdown, PML, and the metadata of HTMLZ and TXTZ) are read only from the book's own folder and one level up, such as sibling `Text/` and `Images/` folders. Absolute paths, network (UNC) paths, `file:` links and anything further out are ignored. Inside an archive, links stay inside the archive.
- Everything one book unpacks to (ZIP members, zlib, bz2, LZX, HUFF/CDIC and PDF streams) is limited to 512 MB per part and 2 GB in all, pictures to 100 million pixels, elements to 128 levels of nesting, tables of contents to 32 levels and 100,000 entries, and PDF pages to a million content tokens. Real books are far below all of these.
- XML with its own entity declarations (a DOCTYPE internal subset) is refused, so entities are never expanded.

## The API

```python
bookport.read(path, fmt=None) -> Book                 # format from the content, then the extension
bookport.convert(src, dst, fmt=None, **options) -> Path
bookport.detect(path) -> str                          # "epub", "azw3", "pdf", ...
bookport.formats() -> dict

Book.write(path, fmt=None, **options) -> Path
Book.metadata, Book.cover, Book.spine, Book.resources, Book.toc, Book.landmarks, Book.page_list
Book.text(), Book.word_count(), Book.copy()
```

Options: `page_size`, `text_size` (PDF); `version=2` (EPUB 2), `cover_page=False` (EPUB, no cover page); `compress=False` (Kindle, uncompressed text), `fonts=False` (Kindle, no embedded fonts); `format="doc"` or `"ztxt"` (PDB, default eReader); `newline`, `wrap`, `toc` (TXT); `encoding` (TXT and TCR, default UTF-8 with a byte-order mark). Other formats take no options.

## How it's tested

- **237 tests**, all passing on Python 3.13 with calibre, epubcheck and PyMuPDF installed (the tests that need those skip without them). A sample book with every feature (two authors, series, cover, nested TOC, bold and italic, a list, a table, a figure, an internal link) is written to every format and read back: word recall, TOC entries, images, cover and metadata are checked per format.
- **Against calibre 9.17 as an independent reader and writer**, in both directions: calibre reads bookport's EPUB, MOBI, AZW3, FB2, DOCX, RTF, PDF, PDB, PMLZ, TCR, RB, SNB, LRF and LIT output, and bookport reads calibre's output in every format calibre writes, LIT included.
- **Against epubcheck 5.3.0**: zero errors on EPUBs made from the whole corpus, including from Kindle files.
- **Against PyMuPDF** for PDF output (text, outline, links, metadata), and its encryption (AES-128, AES-256, RC4) for the PDF reader.
- **Hand-made files** for the corners: PDFs with filters (ASCII85 + Flate, LZW from the ISO 32000 example), cross-reference streams and object streams, a broken cross-reference table, RC4 encryption at revisions 2 and 3, a password-protected PDF; a CHM, a Plucker file, a stored RAR comic, an ODT, a DOCX with fields and nested numbering, an RTF with code pages, Unicode, fields, footnotes and tables; DRM'd EPUB, Kindle, eReader and comic files.
- **Codecs**: PalmDOC on random and edge-case data, HUFF/CDIC, Kindle INDX round trips, TCR, LZX, AES against the FIPS-197 test vectors.
- **Hostile files** (`tests/test_hostile.py`): a file built in code for every limit and safety check above, among them ZIP, zlib, bz2, HUFF/CDIC and LZX bombs, links to absolute, network and far-away paths, CHM, LIT, Kindle and PDF headers with impossible counts, PDF forms that draw themselves, 100,000 nested elements, Markdown that used to backtrack, and XML entity bombs. Files that used to hang run in a child process with a time limit.
- **Fuzzing** (`tests/test_fuzz.py`): 50 mutants of a sample file in each of 25 formats (flipped bytes, huge header numbers, truncation, repeated and deleted ranges, damage inside ZIP members) must raise only `BookportError`, each within 10 seconds and 256 MB.
- **A real corpus**: 13 public-domain books from Project Gutenberg and Standard Ebooks (English, French, Chinese; prose, poetry, illustrated, reference), in EPUB, MOBI and AZW3, plus Python's 9 MB CHM documentation and calibre-made files in every rare format.

## Development

```
python -m venv .venv
.venv\Scripts\pip install -e .[dev]
.venv\Scripts\python -m pytest
.venv\Scripts\ruff check .
```

Tests that check output with calibre, epubcheck or PyMuPDF skip when those aren't installed. `benchmarks/fetch_corpus.py` downloads the comparison corpus (public-domain books from Project Gutenberg and Standard Ebooks), epubcheck and a portable Java into a scratch folder; `benchmarks/compare_calibre.py` runs the comparison.

Code layout:

- `src/bookport/book.py`: the Book model. `registry.py`: formats, detection, read, write, convert.
- `src/bookport/markup.py`, `css.py`, `flow.py`, `clean.py`, `links.py`, `toc.py`, `text.py`: HTML parsing and writing, a CSS engine, styled blocks for non-HTML writers, XHTML5 cleanup, link rewriting.
- `src/bookport/fmt/`: one module per format family.
- `src/bookport/pdf/`: the typesetter (`typeset.py`), TrueType subsetting (`ttf.py`), the PDF writer (`objects.py`) and reader (`parser.py`, `crypt.py`, `reader.py`).
- `src/bookport/palm.py`, `mobi_index.py`, `lzx.py`, `images.py`: PDB, PalmDOC, HUFF/CDIC, Kindle indexes, LZX, image codecs.
- `src/bookport/fonts/`: Noto Serif (four styles) and Noto Sans Mono, under the SIL Open Font License.

## License

MIT, see [LICENSE](LICENSE). The bundled Noto fonts are under the SIL Open Font License 1.1 (`src/bookport/fonts/OFL.txt`).
