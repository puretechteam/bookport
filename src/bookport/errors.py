"""Exceptions raised by bookport."""


class BookportError(Exception):
    """A book that can't be read or written: damaged, unsupported, or a bad option."""


class DRMError(BookportError):
    """The book is protected by DRM. bookport never removes DRM."""

    def __init__(self, fmt: str, detail: str = ""):
        self.format = fmt
        message = f"This {fmt} book is protected by DRM (copy protection), so it can't be converted"
        super().__init__(message + (f": {detail}" if detail else "."))
