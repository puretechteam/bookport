# Changelog

Newest first. Work in progress collects under Unreleased until the next release.

## Unreleased

- **Fixed:** on Linux and macOS, a link written with Windows backslashes (`pics\x.png`, `..\..\x.png`) was taken as one odd file name inside the book's folder, so a book made on Windows lost those pictures there (never a file from outside the folder), and a 0.2.0 test failed on Linux. Backslashes now separate folders everywhere, as they always did on Windows.
- **Tests:** CI runs on Linux as well as Windows (new in 0.2.0); the suite now passes there.

## 2026-11-01: bookport 0.2.0, faster CHM, unpacking CHM and LIT, CBZ writing, kerned PDFs

- **Fixed:** EPUBs made from Python's documentation CHM had 329 epubcheck errors; now 0. Tables with `<col>` straight in the table (as Sphinx writes them) had the columns wrapped in rows and cells, rows were mixed with row groups, and a web link with a bare "%" was left invalid. The columns now go in a `<colgroup>`, loose rows in a `<tbody>`, and the "%" is escaped.
- **New:** PDF text is kerned: letter pairs such as AV, To and r. are spaced as the font's own kerning table says (checked against fontTools for all 8,836 pairs of printable ASCII characters in each of the four Noto Serif styles). Lines fit a few more words (144,249 lines instead of 144,383 over the 11 corpus books), and typesetting takes 1.3% longer.
- **Fixed:** in a PDF, a table column exactly as wide as its longest word could break that word between letters ("PAG" and "E") because of a rounding error.
- **Fixed:** code blocks in CHM help files kept Windows line breaks when converted, so a text file had them mixed with Unix ones (42,369 of them in Python's documentation). Pages are read with one kind of line break now, as XML readers do.
- **More metadata kept** in the rarer formats: RB keeps the publisher, copyright and ISBN (its info page has keys for them); SNB and LRF keep the date they already wrote, which reading now picks up; LRF keeps every subject, not just the first.
- **New:** Plucker books keep their pictures: Palm OS bitmaps (1 to 16 bits a pixel, colour tables, transparency, scan-line, RLE and PackBits compression, bitmaps of several depths) become PNG, and composite images are joined into one. Until now they were left out.
- **New:** writes CBZ comics from books made of pictures (comics read from CBZ, CBR or a folder, fixed-layout picture books): the pages in reading order, a ComicInfo.xml with the metadata and the chapters as page bookmarks, and the same metadata in the ZIP comment for calibre's library. Books with pages of text are refused with a clear message.
- **New:** `bookport.unpack(path)` gives the files inside a CHM or LIT container, for extracting them. On Python's documentation CHM it gives the same 555 files as 7-Zip, all 46,823,729 bytes identical. DRM-protected LIT is refused, as when converting.
- Comics read from a CBZ take their chapters from ComicInfo.xml page bookmarks when it has them.
- **Faster:** CHM and LIT decompression (LZX) is 4.7 times faster: Python's documentation CHM decodes in 3.98 s instead of 18.68 s, and becomes an EPUB in 19.5 s instead of 27.1 s (best of 3 in one process; the side-by-side table in the README times a fresh process per conversion: 18.0 s). The output is byte-identical.
- **Fixed:** reading a PDF dropped a line at the top or bottom of pages when its words had digits inside them ("A320", "1880s", "word58"): such lines looked like a running header once their numbers were masked. A 144,227-word book read back as 137,927 words; now all 144,227. Only whole numbers are masked now, so page numbers and headers like "Chapter 12" are still dropped.
- **Fixed:** in a PDF bookport wrote, characters that no font had (drawn as empty boxes) all copied and searched as the first such character, so "∀x ∃y" read back as "←x ←y". They now read back as U+FFFD, the standard sign for an unknown character.
- The `BOOKPORT_FALLBACK_FONTS` environment variable (TrueType fonts tried first for characters the bundled fonts lack, such as CJK on Linux) is now documented in the README, and tested.
- **Smaller:** the bundled fonts take 0.86 MB instead of 2.4 MB: they're stored xz-compressed, without two tables bookport doesn't read (GSUB, and the glyph names in post; `tools/slim_fonts.py` makes them from Noto's release). The PDFs bookport writes are byte for byte the same (9 of 9 sample PDFs, every page size and text size); loading all five fonts takes 59 ms instead of 12 ms. The package is 1.7 MB instead of 3.2 MB.
- **Fixed:** on Python 3.10 and 3.11, a damaged ZIP-based book (EPUB, HTMLZ, DOCX and the rest) whose member claims more bytes than the file holds made reading set aside that much memory first: a 3 KB file asked for 257 MB. Such a member is now refused at once as damaged (under 20 MB peak). Python 3.12 and later already caught it.

## 2026-10-30: bookport 0.1.0

First release: bookport 0.1.0.

- **Reads and writes** EPUB 2 and 3, Kindle MOBI (joint MOBI 6 + KF8), AZW, AZW3, FictionBook 2 (and `.fb2.zip`), HTML, HTMLZ, plain text, TXTZ, Markdown, Word (DOCX), RTF, PDF, Palm database ebooks (eReader, PalmDOC, zTXT), PML and PMLZ, Psion TCR, Rocket eBook (RB), Shanda Bambook (SNB), Sony LRF and Microsoft Reader (LIT, without DRM).
- **Reads** OpenDocument text (ODT), Compiled HTML Help (CHM), Plucker, KF8 and MOBI files made by any tool (including HUFF/CDIC-compressed ones), comics (CBZ, CBR with stored pages, a folder of images, a single image) and PDFs made by any tool.
- **Refuses DRM.** Protected EPUB (Adobe, Readium LCP, Apple), Kindle, eReader, LIT, PDF (password needed) and encrypted comic archives raise `DRMError`. bookport never removes DRM.
- **EPUBs pass epubcheck** with zero errors: documents from any source are cleaned into valid XHTML5 first.
- **Its own PDF typesetter**: justified text, headings kept with the text after them, lists, tables that split across pages, images, links, a bookmark outline from the TOC, page numbers and embedded Noto font subsets, so the text can be searched and copied. Page size (phone, 6-inch e-reader, A5, A4, Letter) and text size (small, normal, large) are options.
- **Its own PDF reader**: cross-reference streams, object streams, damaged-file repair, RC4 and AES encryption with an empty password, ToUnicode text extraction reflowed into paragraphs and headings, end-of-line hyphens resolved from the book's own vocabulary, running headers and page numbers dropped, images and the outline kept.
- **Command line**: `bookport SOURCE OUTPUT`, `bookport --info BOOK`, `bookport --formats`; `--to` and `--epub-version`, and `--page-size` and `--text-size` checked before the book is read. Errors print a one-line message, never a traceback.
- **Safe with books from anywhere**: local pictures, styles, covers and pages are read only from the book's folder and one level up (never absolute, network or `file:` paths); unpacking is limited to 512 MB per part and 2 GB per book, pictures to 100 million pixels, nesting to 128 levels (a page nested 100,000 elements deep still becomes an EPUB in about 3 s), tables of contents to 32 levels; XML entity declarations are refused; damaged files of any kind raise `BookportError` and nothing else. Checked by a file built for each limit (55 of them failed before the fix) and by fuzzing 25 formats (1,250 mutants, none escape; 27 of 350 escaped before).
- **Options are checked**: an option a format doesn't take raises `BookportError` naming the ones it does.
- **Against Calibre 9.17** on the same books (median per conversion): EPUB to PDF in 1.40 s and 40 MB against 8.91 s and 1,138 MB; EPUB to MOBI in 0.96 s against 1.60 s; Kindle to EPUB with 0 epubcheck errors against 7,212; Python's documentation CHM in 20.5 s against 381.8 s. Words, TOC titles, images, metadata and covers kept are equal or better on every output format. Install size 3.0 MB against 666.5 MB.
- Pure Python with no dependencies; Pillow, when installed, widens the image formats that can be converted (WebP, TIFF).
