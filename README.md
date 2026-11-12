# bookport

Read and write ebooks in every common and rare format, documents in nine markup languages, DjVu books, and PostScript and EPS (to PDF and back, and to SVG), in pure Python. No Calibre, no Pandoc, no DjVuLibre, no Ghostscript, no dependencies.

```
pip install git+https://github.com/puretechteam/bookport
pip install https://github.com/puretechteam/bookport/releases/download/v0.4.0/bookport_native-0.4.0-cp310-abi3-win_amd64.whl
```

The second line is optional: bookport-native, the compiled DjVu decoders for 64-bit Windows (each [release](https://github.com/puretechteam/bookport/releases) has Linux and macOS wheels too). Without it bookport reads DjVu the same way, in Python, more slowly.

```python
import bookport

book = bookport.read("novel.mobi")                  # any supported format -> Book
book.write("novel.epub")                             # format from the extension
book.write("novel.pdf", page_size="ereader", text_size="large")
bookport.convert("story.fb2", "story.azw3")
bookport.formats()                                   # {"epub": {"name": "EPUB", "read": True, "write": True}, ...}
bookport.convert("paper.tex", "paper.docx")         # LaTeX with its equations and citations
bookport.djvu.to_pdf("scan.djvu", "scan.pdf")        # a searchable PDF
bookport.ps.to_pdf("figure.eps", "figure.pdf")       # PostScript or EPS as PDF
bookport.ps.from_pdf("paper.pdf", "paper.ps")        # and PDF as PostScript (or one page as EPS)
bookport.ps.to_svg("figure.eps", "figure.svg")       # PostScript or EPS as SVG, for books
```

or from a terminal:

```
bookport novel.mobi novel.epub
bookport novel.epub novel.pdf --page-size a5 --text-size normal
bookport novel.fb2 novel.book --to epub --epub-version 2
bookport --info novel.azw3
bookport --formats
```

`--to` picks the output format when the file name's extension doesn't, and `--epub-version 2` writes EPUB 2 instead of EPUB 3. `--page-size` takes phone, ereader, a5, a4 or letter and `--text-size` small, normal or large; anything else is refused before the book is read. `--info` prints the book's metadata and contents as JSON (as UTF-8 when it goes to a pipe or a file); given an output file too, it converts as well.

## Why bookport?

Converting ebooks has meant installing Calibre: about 670 MB, GPL, and a program to drive from the outside. bookport is a 3.9 MB library (a fifth of it the embedded fonts, a sixth Adobe's font metrics for PostScript) with nothing else to install, MIT licensed, that runs anywhere Python 3.10+ does and bundles cleanly into an app.

It was written for [Latheport](https://github.com/puretechteam/Latheport), a drag-and-drop file converter and editor (called ConvertAll until 1.5), to replace Calibre there, and since 0.3.0 Pandoc (225 MB, for markup languages) and DjVuLibre (for DjVu books) as well, and since 0.4.0 Ghostscript (for PostScript and EPS). It's faster than Calibre, Pandoc and DjVuLibre on every document measured but three LaTeX files where Pandoc finds almost nothing (DjVu PDFs with bookport-native); against Ghostscript, most PostScript files take a few hundredths of a second, and a 60-page document 0.74 s against Ghostscript's 0.28 s (see the [CHANGELOG](CHANGELOG.md)). It's a sibling of [meshport](https://github.com/puretechteam/meshport) (3D), [packport](https://github.com/puretechteam/packport) (archives) and [pixport](https://github.com/puretechteam/pixport) (camera RAW and other pictures).

## bookport and Calibre, side by side

Both tools converted the same books on the same Windows 11 machine, one conversion at a time, each in a fresh process (so start-up time counts for both), with Calibre 9.17.0's `ebook-convert` and bookport on Python 3.13. Each pair is **Calibre / bookport**. The CHM row and the PDF, RB, SNB and LRF rows were measured again for bookport 0.2.0 (the two Standard Ebooks books in their current editions); the other rows are from bookport 0.1.0, and the install size is this version's.

| | Calibre | bookport |
|---|---|---|
| Install size | 666.5 MB | **3.9 MB** |
| Python documentation CHM to EPUB | 196.8 s, 7,781 epubcheck errors | **18.0 s**, 0 epubcheck errors (same 4,171 TOC entries, 100% of Calibre's words) |

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
| RB | 13/13 / 13/13 | 0.96 / **0.56** | 97 / **37** | 0.00 / 99.92 | 0.00 / 100.00 | 821 / 979 of 986 | 181 / 183 of 189 | 39 / 42 of 84 | 11 / 13 of 13 |
| SNB | 13/13 / 13/13 | 1.13 / **0.38** | 105 / **34** | 99.91 / 99.92 | 99.99 / 100.00 | 790 / 985 of 986 | 183 / 183 of 189 | 58 / 68 of 84 | 13 / 13 of 13 |
| LRF | 13/13 / 13/13 | 1.11 / **0.49** | 103 / **27** | 99.53 / 99.92 | 99.50 / 99.53 | 985 / 985 of 986 | 103 / 183 of 189 | 84 / 84 of 84 | 0 / 13 of 13 |
| LIT | 13/13 / 13/13 | 1.71 / **0.27** | 100 / **30** | 100.00 / 100.00 | 100.00 / 100.00 | 986 / 986 of 986 | 183 / 189 of 189 | 84 / 84 of 84 | 13 / 13 of 13 |
| PDF | 13/13 / 13/13 | 5.88 / **1.46** | 1140 / **46** | 98.17 / 98.50 | 81.64 / 85.15 | 986 / 986 of 986 | 183 / 183 of 189 | 65 / 81 of 84 | 13 / 13 of 13 |

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
- In the first measurement Calibre failed to convert Frankenstein to PDF (exit code 1); in the second it converted all 13.
- Calibre's RB files of two books (the Chinese novel and Les Misérables) gave no text back in either reader.

To run the comparison yourself: `python benchmarks/fetch_corpus.py <folder> --tools` downloads the books and epubcheck, then `python benchmarks/compare_calibre.py <folder>/corpus --from epub --to mobi,pdf --tools <folder>/tools/tools.json --calibre-text` runs it (add `--originals <folder>/corpus` for a folder of files made from those books).

## Formats

| Format | Read | Write |
|---|---|---|
| EPUB 2 and 3 | ✓ | ✓ (EPUB 3 with an NCX, or EPUB 2) |
| Kobo EPUB (`.kepub`), OEB zip | ✓ | |
| Kindle MOBI, AZW (MOBI 6 + KF8 in one file) | ✓ | ✓ |
| Kindle AZW3 (KF8) | ✓ | ✓ |
| Mobipocket PRC | ✓ | |
| FictionBook 2 (`.fb2`, `.fb2.zip`) | ✓ | ✓ |
| PDF | ✓ | ✓ |
| Word (DOCX) | ✓ | ✓ |
| RTF | ✓ | ✓ |
| OpenDocument text (ODT) | ✓ | ✓ |
| HTML, HTMLZ | ✓ | ✓ |
| Plain text, TXTZ, Markdown | ✓ | ✓ |
| Palm eReader, PalmDOC, zTXT (`.pdb`) | ✓ | ✓ |
| Plucker (`.pdb`) | ✓ (text, links and pictures) | |
| PML, PMLZ | ✓ | ✓ |
| Sony BBeB (LRF) | ✓ | ✓ |
| Microsoft Reader (LIT) | ✓ (and unpacked) | ✓ |
| Compiled HTML Help (CHM) | ✓ (and unpacked) | |
| Rocket eBook (RB) | ✓ | ✓ |
| Psion TCR | ✓ | ✓ |
| Shanda Bambook (SNB) | ✓ | ✓ |
| Comics: CBZ, CBR (stored pages), a folder of images, one image | ✓ (fixed layout) | ✓ CBZ, from books made of pictures |
| LaTeX, reStructuredText, Org, AsciiDoc, MediaWiki, Typst | ✓ | ✓ |
| DocBook (4 and 5), man pages, Textile | ✓ | ✓ (DocBook 5) |
| DjVu (bundled and indirect) | ✓ (and `djvu.to_pdf`, `djvu.to_text`) | |
| PostScript and EPS (`.ps`, `.eps`, DOS EPS with a preview) | ✓ (fixed layout, through PDF), and `ps.to_pdf`, `ps.to_svg` | from PDF (`ps.from_pdf`) |

Every book goes through one model, `bookport.Book`: metadata (title, authors, language, identifiers, publisher, date, description, subjects, series), a cover, a spine of XHTML documents, resources (images, stylesheets, fonts), a nested table of contents, landmarks and a page list. Anything read can be written to any writable format.

**DRM.** bookport never removes DRM. Protected books raise `bookport.DRMError` with a plain explanation: EPUB with Adobe ADEPT, Readium LCP or Apple FairPlay, encrypted Kindle and eReader books, LIT with encrypted content, PDFs that need a password, password-protected comic archives. EPUB font obfuscation (IDPF and Adobe) isn't DRM and is undone. Kindle KFX and Topaz books are refused with a clear message too.

**EPUB.** Written as EPUB 3.3 with an NCX as well, so EPUB 2 readers get a table of contents, or as pure EPUB 2 with `version=2`. Every document is cleaned into valid XHTML5 first (obsolete tags and attributes become CSS, broken links and fragments are fixed, nesting is repaired), so the result passes epubcheck with zero errors whatever the source. A cover page is added when the book has a cover.

**Kindle.** `.mobi` and `.azw` are joint files: a MOBI 6 part for old Kindles (simple HTML made from the book's styled text, with filepos links and an NCX index) and a KF8 part that keeps the book's XHTML and CSS (skeletons and fragments, kindle:pos links, NCX, skeleton, fragment and guide indexes). `.azw3` is KF8 alone. Text is PalmDOC compressed. Reading handles MOBI 6, KF8 and joint files from any tool, HUFF/CDIC compression included.

**PDF writing** is bookport's own typesetter. Text is justified where the book's CSS says so (a line is left ragged rather than stretched past 2.5 times its normal word spacing), lines also break after hyphens and dashes, headings stay with the lines after them, lists hang their markers, tables get measured columns and rows that continue across pages, images are scaled to the page, chapters start on new pages, and the cover gets a page of its own. Letter pairs are kerned as the font's own kerning table says (AV, To, Wa, r. and the rest). Fonts are embedded as TrueType subsets (Noto Serif and Noto Sans Mono, OFL) with ToUnicode maps, so the text is searchable and copyable; characters the bundled fonts don't have come from a system font (on Windows, Microsoft YaHei, SimSun, MS Gothic or Malgun Gothic; on Linux, Droid Sans Fallback, WenQuanYi Zen Hei or DejaVu Sans; on macOS, Arial Unicode). Fonts named in the `BOOKPORT_FALLBACK_FONTS` environment variable are tried first: TrueType files (`.ttf` or `.ttc`), separated by `;` on Windows and `:` elsewhere, which is how to give CJK text a font on a system without one of those. A character no font has is drawn as an empty box and reads back as U+FFFD. Links, a bookmark outline from the TOC, page numbers and the metadata are included.

| `page_size` | Size | `text_size` small / normal / large |
|---|---|---|
| `phone` | 252 × 448 pt (3.5 × 6.2 in) | 8.5 / 10 / 12 pt |
| `ereader` | 259 × 346 pt (a 6-inch e-reader screen) | 8.5 / 10 / 12 pt |
| `a5` (default) | 148 × 210 mm | 9.5 / 11 / 13 pt |
| `a4` | 210 × 297 mm | 10 / 12 / 14 pt |
| `letter` | 8.5 × 11 in | 10 / 12 / 14 pt |

**PDF reading** is bookport's own parser: cross-reference tables and streams, object streams, incremental updates, damaged-file repair, Flate, LZW, ASCII85, ASCIIHex and run-length filters with predictors, RC4 and AES (128 and 256-bit, its own AES) with an empty user password. Page content is interpreted into positioned text (ToUnicode maps, standard encodings with Differences, CID fonts, and for CID fonts without a ToUnicode map the embedded TrueType font's own character map), then reflowed: pages set in columns read a column at a time, lines into paragraphs by spacing and indents, larger text into headings, bold and italic kept, running headers, footers and page numbers dropped. A hyphen at the end of a line joins the two halves only if the joined word appears elsewhere in the book, so real compounds like "waistcoat-pocket" keep their hyphen. Images (JPEG as is, CCITT Group 4 scans and others as PNG), web links around their words, and the outline (as the TOC) are kept; JPEG 2000 and JBIG2 pictures are not read yet.

**Other formats.** DOCX reading follows styles (headings from style names and outline levels), numbered and bulleted lists, tables, images, hyperlinks, bookmarks, fields, footnotes and endnotes; writing makes real heading styles, lists, tables, images and internal links, with the publisher, ISBN and series in the document's custom properties (ODT: user-defined fields), which bookport reads back. RTF reading handles code pages per font, Unicode escapes, heading styles, tables, pictures, HYPERLINK fields, bookmarks and footnotes. CHM and LIT are decompressed with bookport's own LZX decoder (CHM text in the Windows code page of the file's language); LIT's tokenized HTML and package are decoded into XHTML, metadata and TOC; LIT writing tokenizes them back, with the documents LZX-framed, a chapter tour from the TOC, the cover and images, and a directory that grows past one chunk for long books. LRF reading and writing cover pages, text blocks, italic and bold, images, the TOC object and the info block. eReader PDB and PMLZ carry the TOC (chapter index and PML headings), links, images and metadata. CBZ writing puts each page's pictures in reading order, with a ComicInfo.xml (title, series, writers, summary, publisher, genres, language, date, right-to-left reading, and the chapters as page bookmarks, which bookport reads back) and the same metadata as ComicBookInfo in the ZIP comment, where calibre's library looks for it; a book whose pages are text is refused, since a comic archive holds only pictures. `bookport.unpack` gives the files inside a CHM or LIT as they are stored (LIT pages in their tokenized form), for tools that extract them: for Python's 9 MB documentation CHM, the same 555 files as 7-Zip, byte for byte.

**Markup languages.** LaTeX (sections, lists, tables with merged cells, figures, footnotes, theorems, the document's own macros, `\input` from its folder, cross-references numbered as LaTeX numbers them), reStructuredText with Sphinx's roles, Org, AsciiDoc with its includes and conditionals, MediaWiki, Typst, DocBook, man pages and Textile are read into the same `Book`, so each converts to every other format. Equations travel as MathML and are written as TeX, Typst, MathML or Word's own equations, whichever the format holds; BibTeX and BibLaTeX bibliographies turn `\cite`, `[@key]` and Org's citations into a reference list. These markup formats (Markdown aside) can write the pictures they refer to beside themselves (`pictures=True`). LaTeX figures in EPS or PostScript (`\includegraphics`, `\epsfig`) go into the book as SVG drawn by `ps.to_svg`, so EPUB, HTML and the other formats carry them; PDF figures keep their place and caption only. What a reader can't turn into structure stays as text, and `Book.lost` says what was left out. Typst's `#set` and `#show` rules are dropped (`#set document(title:, author:)` gives the book its title and authors) and listed in `Book.lost`.

**DjVu.** Black-and-white (JB2), photo (IW44) and compound pages, the OCR text layer, outlines and links are decoded by bookport. `bookport.djvu.to_pdf` keeps the scan's layers apart (black and white as JBIG2 or G4, backgrounds as JPEG) and lays the OCR text over them invisibly, so the PDF is searchable and about 15 times smaller than DjVuLibre's. The optional `bookport-native` package (built from `native/`) decodes pages many times faster; without it, the same pages come out of pure Python.

**PostScript and EPS.** `bookport.ps.to_pdf` runs the program in bookport's own PostScript Level 3 interpreter, written from Adobe's PostScript Language Reference: paths, clipping, colour spaces (Indexed and Separation included), pictures through every standard filter (a JPEG goes into the PDF as it is), shadings, patterns, forms, and fonts: Type 1 and CFF fonts in the file embedded (only the glyphs used, for Type 1), TrueType (Type 42) as TrueType, Type 3 as Type 3, and fonts not in the file drawn as the PDF standard 14 with Adobe's metrics, so each letter lands where PostScript put it and the text can be searched and copied. Each `showpage` is a page; an EPS page is its drawing's bounding box. Against Ghostscript's PDF of the same 25 files (matplotlib, LibreOffice, Pillow, Ghostscript's own ps2write output, a 60-page document), drawn by the same renderer, every page scores an SSIM of 0.99 or more and the 60 pages and all of Ghostscript's own files come out pixel for pixel the same. `bookport.ps.from_pdf` goes the other way: every page as Level 3 PostScript, or one as EPS, with the PDF's fonts embedded (Type 1 as it is, TrueType as Type 42, CFF as Type 1, Type 3 as Type 3) and each string shown with the PDF's own widths; a page with transparency, which PostScript doesn't have, is drawn as a picture when the caller passes `flatten` (a function that draws a page), else opaque with a note. A PDF taken to PostScript and back by bookport keeps every word, and its pages draw the same (SSIM 0.995 or more). Anything that couldn't be kept as it was is added to `notes`. `bookport.read` takes PostScript and EPS too (recognised by their `%!` header, a DOS EPS preview or a printer job around them): the program is run into a PDF, which is read as a book, so it converts to EPUB and the rest. `bookport.convert` and the command line send PostScript to `.pdf` straight through `ps.to_pdf`, every page as it was drawn. `save` and `restore` undo what changed in local VM in between (an EPS figure's own `showpage`, say), as the reference has it.

`bookport.ps.to_svg` draws one page (an EPS figure, say) as SVG 1.1, for books, which can't hold a PDF. Text is drawn as glyph outlines read from the fonts in the file (Type 1 charstrings with flex and accents, CFF, TrueType and Type 3), so it looks right without them; the standard fonts, named but not in the file, become SVG text in a similar family, each letter where PostScript put it (36 letters in three fonts, all within 0.001 pt). Clips are clip paths, axial and radial shadings gradients (the other shading types pictures), tiling patterns SVG patterns, and pictures PNG or, for JPEGs, the JPEG itself. Gradients and patterns also carry a plain colour for readers without them (MuPDF's SVG reader, which has no clip paths either, draws that rather than black). CMYK colours use the simple conversion PostScript defines, with no colour profile, so they come out brighter than in readers that use one. Drawn next to the same program's PDF (SSIM, the worst quarter of each page): 0.988 to 1.0 in PyMuPDF for paths, strokes, pictures and text in each kind of font, and 0.977 to 0.997 in Edge for clips, shadings, meshes, patterns and enlarged pictures.

**Images.** bookport decodes PNG, GIF and BMP itself and writes PNG, so formats that only take some image types get what they need. With Pillow installed it also converts WebP and TIFF.

Errors: damaged or unsupported files raise `bookport.BookportError`, and nothing else; DRM raises `bookport.DRMError` (a subclass). An option a format doesn't take raises `BookportError` too, naming the options it does take. A failed write leaves no half-written file behind, and a finished one gets the permissions any new file would.

**Books from anywhere.** bookport treats every book as hostile:

- Pictures, stylesheets, covers and other pages that a book links to on disk (HTML, Markdown, PML, the metadata of HTMLZ and TXTZ, and the includes and bibliographies of LaTeX, reStructuredText, AsciiDoc, Org and Typst) are read only from the book's own folder and one level up, such as sibling `Text/` and `Images/` folders. Absolute paths, network (UNC) paths, `file:` links and anything further out are ignored. Inside an archive, links stay inside the archive.
- Everything one book unpacks to (ZIP members, zlib, bz2, LZX, HUFF/CDIC and PDF streams) is limited to 512 MB per part and 2 GB in all, pictures to 100 million pixels, elements to 128 levels of nesting, tables of contents to 32 levels and 100,000 entries, and PDF pages to a million content tokens. Real books are far below all of these.
- XML with its own entity declarations (a DOCTYPE internal subset) is refused, so entities are never expanded.
- PostScript can't open, run, rename or delete files (`file`, `run`, `deletefile` refuse), and a program stops after 200 million steps, 64 nested calls back into PostScript, or once what it allocates passes the 2 GB budget.

## The API

```python
bookport.read(path, fmt=None) -> Book                 # format from the content, then the extension
bookport.convert(src, dst, fmt=None, **options) -> Path
bookport.detect(path) -> str                          # "epub", "azw3", "pdf", ...
bookport.formats() -> dict
bookport.unpack(path, fmt=None) -> list[(name, bytes)]  # the files inside a CHM or LIT
bookport.djvu.to_pdf(src, dst=None) -> Path | bytes   # a searchable PDF of a DjVu book
bookport.djvu.to_text(src) -> str                     # its OCR text, as djvutxt gives it
from bookport.pdf import rewrite                      # import it: `import bookport` alone doesn't load it
rewrite.edit([data, ...], pages=None, split=None, dpi=None, quality=75) -> [bytes]  # see below
rewrite.compress(data, dpi=150, quality=75) -> bytes  # pictures at most 150 dpi of the page, and smaller
rewrite.edit([data], text={0: lines})                 # an invisible text layer over page 1 (OCR), see below
bookport.ps.to_pdf(src, dst=None, eps=None, notes=None) -> Path | bytes  # PostScript or EPS (path or bytes)
bookport.ps.from_pdf(src, dst=None, eps=False, page=0, flatten=None, notes=None) -> Path | bytes
bookport.ps.to_svg(src, dst=None, notes=None, eps=None, page=0) -> Path | bytes  # one page as SVG

Book.write(path, fmt=None, **options) -> Path
Book.metadata, Book.cover, Book.spine, Book.resources, Book.toc, Book.landmarks, Book.page_list
Book.text(), Book.word_count(), Book.copy(), Book.lost  # what a reader left out
```

Options: `page_size`, `text_size` (PDF); `pictures=True` (LaTeX, reStructuredText, Org, AsciiDoc, MediaWiki, Typst, DocBook, man and Textile: write the pictures beside the file, in `images/`; a different file already there is never overwritten, the picture takes a name of its own); `version=2` (EPUB 2), `cover_page=False` (EPUB, no cover page); `compress=False` (Kindle, uncompressed text), `fonts=False` (Kindle, no embedded fonts); `format="doc"` or `"ztxt"` (PDB, default eReader); `newline`, `wrap`, `toc` (TXT); `encoding` (TXT and TCR, default UTF-8 with a byte-order mark). Other formats take no options.

`rewrite.edit` makes new PDFs from PDFs without drawing anything again (text stays text): `pages` is the pages
wanted, as `(index, quarter turns clockwise)` counted across all the PDFs given (they're joined, in order);
`split=n` gives PDFs of n pages each, each with only the objects its own pages use (only the pictures and fonts a
page draws, even where a file's pages share one list of them); `dpi` makes it smaller:
pictures saved again as JPEG (at `quality`) at most that many pixels per inch of the largest page (soft masks stay
lossless; this needs Pillow, and without it pictures are left as they are), the same picture or font stored once and
streams packed tighter. Small objects always go in compressed object streams.
Bookmarks follow their pages (those for pages taken out go; those to a web page stay, and open ones stay open), and
the document's details, links and form fields stay; links and bookmarks to a named place jump to its page, as names
repeat across joined files. Page labels (i, ii, 1, 2...) follow their pages. The tag tree (accessibility structure)
is kept when every page is kept in its order, and left out otherwise, as it would describe the old order.
A PDF locked only by permissions (it opens with no password but may not be edited) is rewritten too, and the result is locked again: an empty user password, a random owner password nobody is told, the same permission bits (the fewest of them when several are joined) and AES-256. A PDF that needs a password is refused with a BookportError, never unlocked.

`text` lays invisible text over pages (render mode 3), what OCR read on scanned pages, so the PDF can be searched and
its text copied: page index (as in `pages`) -> lines, each a list of `(word, x0, y0, x1, y1)` in points on the page as
it shows (turned as the page itself says, from the bottom left of its visible area). Each word is stretched over its
own box in Helvetica (not embedded), with a ToUnicode map, so any character can be found; the page is drawn as before,
its own drawing kept whole between `q` and `Q`, and the text goes with the page through `pages`, `split` and `dpi`.
The same layer as DjVu's hidden text gets in `djvu.topdf` (`bookport.pdf.textlayer`).

## How it's tested

- **995 tests** (55 more skip without their tools), all passing on Python 3.13 with calibre and epubcheck installed (the tests that need those skip without them; PyMuPDF and fontTools come with the `dev` extra). Any warning fails a test. A sample book with every feature (two authors, series, cover, nested TOC, bold and italic, a list, a table, a figure, an internal link) is written to every format and read back: word recall, TOC entries, images, cover and metadata are checked per format.
- **Against calibre 9.17 as an independent reader and writer**, in both directions: calibre reads bookport's EPUB, MOBI, AZW3, FB2, DOCX, RTF, PDF, PDB, PMLZ, TCR, RB, SNB, LRF and LIT output, and bookport reads calibre's output in every format calibre writes, LIT included.
- **Against epubcheck 5.3.0**: zero errors on EPUBs made from the whole corpus, including from Kindle files.
- **Against PyMuPDF** for PDF output (text, outline, links, metadata, where each kerned letter sits), and its encryption (AES-128, AES-256, RC4) for the PDF reader.
- **Against fontTools** for kerning: the pair values bookport reads from the fonts' GPOS tables match fontTools' for all 8,836 pairs of printable ASCII characters in each of the four Noto Serif styles (`test_kerning_matches_fonttools_for_every_ascii_pair`; it skips where fontTools isn't installed, and the `dev` extra installs it).
- **Against 7-Zip** for `bookport.unpack`: the same 555 files from Python's documentation CHM, byte for byte.
- **Against Ghostscript, as a black box** for PostScript (its source is never read): programs drawn by bookport and by Ghostscript are compared page by page (SSIM at 150 dpi, the same renderer for both), and the PostScript bookport writes is drawn by Ghostscript and compared with the PDF it came from; these tests skip without Ghostscript (`BOOKPORT_GS`). `benchmarks/compare_ghostscript.py` runs the comparison on a folder of files. PDFs also go to PostScript and back through bookport alone, every word checked in place.
- **SVG output against the PDF route** (`tests/test_ps_svg.py`): each program's SVG and PDF are drawn and compared (SSIM on the worst quarter of the inked area, and the mean colour difference), by PyMuPDF and, for clips, gradients and patterns, which PyMuPDF's SVG reader doesn't draw, by headless Microsoft Edge (`BOOKPORT_EDGE`; those tests skip without it). Each check was seen to fail on purpose-made mistakes: flex, CFF curves and TrueType curves drawn wrong, the fill rule ignored, clips dropped, gradients flattened, pattern cells shifted, picture colours swapped, strokes drawn without their matrix, Type 3 glyphs lost.
- **Hand-made files** for the corners: PDFs with filters (ASCII85 + Flate, LZW from the ISO 32000 example), cross-reference streams and object streams, a broken cross-reference table, RC4 encryption at revisions 2 and 3, a password-protected PDF; a CHM, Plucker files with text and pictures, Palm bitmaps in every version, depth and compression, a stored RAR comic, a CBZ with ComicInfo, an ODT, a DOCX with fields and nested numbering, an RTF with code pages, Unicode, fields, footnotes and tables; DRM'd EPUB, Kindle, eReader and comic files.
- **Codecs**: PalmDOC on random and edge-case data, HUFF/CDIC, Kindle INDX round trips, TCR, LZX, AES against the FIPS-197 test vectors.
- **Hostile files** (`tests/test_hostile.py`): a file built in code for every limit and safety check above, among them ZIP, zlib, bz2, HUFF/CDIC and LZX bombs, links to absolute, network and far-away paths, CHM, LIT, Kindle and PDF headers with impossible counts, PDF forms that draw themselves, 100,000 nested elements, Markdown that used to backtrack, and XML entity bombs. Files that used to hang run in a child process with a time limit.
- **Fuzzing** (`tests/test_fuzz.py`): 50 mutants of each of 26 sample files (flipped bytes, huge header numbers, truncation, repeated and deleted ranges, damage inside ZIP members) must raise only `BookportError`, each within 10 seconds and 256 MB. The same for PostScript (`tests/test_ps_fuzz.py`: 540 damaged programs, with stray braces, huge numbers, endless loops, eexec and restore dropped in) and for PDF to PostScript (300 mutated PDFs).
- **A real corpus**: 13 public-domain books from Project Gutenberg and Standard Ebooks (English, French, Chinese; prose, poetry, illustrated, reference), in EPUB, MOBI and AZW3, plus Python's 9 MB CHM documentation and calibre-made files in every rare format. With `BOOKPORT_CORPUS` set to the folder `benchmarks/fetch_corpus.py` fills, `tests/test_corpus.py` reads every book, checks each Kindle file against its EPUB, sends every book through every writable format and back, runs epubcheck on EPUBs made from every input, and unpacks the CHM against 7-Zip (about 8 minutes).

## Development

```
python -m venv .venv
.venv\Scripts\pip install -e .[dev]
.venv\Scripts\python -m pytest
.venv\Scripts\ruff check .
```

PyMuPDF and Pillow come with the `dev` extra; tests that check output with calibre or epubcheck skip when those aren't installed. `benchmarks/fetch_corpus.py` downloads the comparison corpus (public-domain books from Project Gutenberg and Standard Ebooks), epubcheck and a portable Java into a scratch folder; `benchmarks/compare_calibre.py` runs the comparison.

Code layout:

- `src/bookport/book.py`: the Book model. `registry.py`: formats, detection, read, write, convert.
- `src/bookport/markup.py`, `css.py`, `flow.py`, `clean.py`, `links.py`, `toc.py`, `text.py`: HTML parsing and writing, a CSS engine, styled blocks for non-HTML writers, XHTML5 cleanup, link rewriting.
- `src/bookport/fmt/`: one module per format family. `src/bookport/textwriter.py`, `mathml.py`, `bib.py`: what the markup writers share (equations, citations).
- `src/bookport/djvu/`: the DjVu decoders (ZP, BZZ, JB2, IW44, MMR), page compositing and the PDF writer; `native/`: the optional bookport-native package (Rust, PyO3), the same decoders compiled.
- `src/bookport/pdf/`: the typesetter (`typeset.py`), TrueType subsetting (`ttf.py`), the PDF writer (`objects.py`) and reader (`parser.py`, `crypt.py`, `reader.py`), rewriting (`rewrite.py`) and the invisible text layer (`textlayer.py`).
- `src/bookport/palm.py`, `mobi_index.py`, `lzx.py`, `images.py`: PDB, PalmDOC, HUFF/CDIC, Kindle indexes, LZX, image codecs.
- `src/bookport/ps/`: PostScript. The scanner (`scan.py`), files and filters (`files.py`), the interpreter (`interp.py`) and its operators (`ops_core.py`, `ops_graphics.py`, `ops_text.py`), the PDF pages it paints (`device.py`), fonts (`fonts.py`, `cff.py`) and `topdf.py`; SVG output in `tosvg.py`, with glyph outlines (Type 1, Type 2, TrueType) from `outlines.py`; the other way, `write.py` and its `write_*.py` helpers. `ps/afm/`: Adobe's Core 14 font metrics.
- `src/bookport/fonts/`: Noto Serif (four styles) and Noto Sans Mono, under the SIL Open Font License.

## License

MIT, see [LICENSE](LICENSE). The bundled Noto fonts are under the SIL Open Font License 1.1 (`src/bookport/fonts/OFL.txt`). The Core 14 AFM files in `src/bookport/ps/afm/` are Adobe's, unchanged, with their terms in `MustRead.html` beside them.
