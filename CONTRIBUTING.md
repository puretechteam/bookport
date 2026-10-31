# Contributing

Thanks for helping. Bug reports, new formats and fixes are all welcome.

## Reporting a bug

Open an issue with the book that fails, if you can share it, what you ran, and what went wrong: an error, missing text, a lost table of contents, a file another reader refuses.

Security problems go through [SECURITY.md](SECURITY.md) instead, not a public issue.

## Making a change

```
python -m venv .venv
.venv\Scripts\pip install -e .[dev]
.venv\Scripts\python -m pytest
.venv\Scripts\ruff check .
```

Some tests check bookport's output with independent tools and skip when those aren't there: calibre (`ebook-convert`), epubcheck (set `BOOKPORT_TOOLS` to the `tools.json` that `benchmarks/fetch_corpus.py --tools` writes) and PyMuPDF. GitHub Actions runs the tests on Python 3.10 and 3.13 and `ruff check` on every pull request.

- **Write from published format descriptions**: the IDPF/W3C EPUB specifications, the MobileRead wiki, ECMA-376, ODF, RTF 1.9.1, ISO 32000, Microsoft's LZX description. Never from another converter's source code, and in particular never from calibre's, which is GPL.
- **No dependencies.** bookport is pure Python with nothing to install. Pillow is optional and only widens the image formats that can be converted.
- **Hostile files**: readers must raise `BookportError` (or `DRMError`) and nothing else, never write outside a temporary folder, and never use unbounded memory. Unpack through the helpers in `limits.py` (`inflate`, `bunzip`, `zip_read`, `check_pixels`) and find local files with `limits.safe_path`, which allows only the book's own folder and one level up. A new reader gets seed files in `tests/test_fuzz.py`, and each new check gets a file in `tests/test_hostile.py` that fails without it.
- **Never remove DRM.** Protected books are refused with `DRMError`.
- **Check against real readers.** New output should be read back by at least one independent program (calibre, epubcheck, PyMuPDF), as the tests do.
- **Changelog:** add a line under `## Unreleased` in [CHANGELOG.md](CHANGELOG.md) for anything a user would notice.

## License

By contributing, you agree that your contribution is licensed under the [MIT License](LICENSE).
