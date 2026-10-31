# Changelog

Newest first. Work in progress collects under Unreleased until the next release.

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
