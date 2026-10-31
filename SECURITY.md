# Security

bookport reads ebooks that come from anywhere, so a book that makes it do any of these is a security bug:

- read a file outside the book's own folder and the folder one level up (or, for an archive, outside the archive), for example through an absolute, network (UNC) or `file:` link;
- write outside the output file and a temporary folder, or run code;
- hang, or use more memory than the limits below allow;
- raise anything other than `BookportError` (or its subclass `DRMError`);
- produce output that makes other readers misbehave.

## Limits

These are in `src/bookport/limits.py`. Going over one raises `BookportError` with a plain message; real books are far below all of them.

| What | Limit |
|---|---|
| One unpacked part (ZIP member, zlib or bz2 stream, LZX section, PDF stream) | 512 MB |
| Everything one book unpacks to | 2 GB |
| One HUFF/CDIC (Kindle) record | 1 MB |
| A picture | 100 million pixels |
| Element nesting | 128 levels (deeper HTML is flattened; deeper XML data files are refused) |
| Table of contents | 32 levels, 100,000 entries |
| One PDF page | 1,000,000 content tokens (a form or picture drawn counts 50) |

XML files that declare their own entities (a DOCTYPE internal subset) are refused, and entities are never expanded.

## Reporting

Please don't open a public issue. Use **Security → Report a vulnerability** on this repository's GitHub page, which only the maintainers can see. Include the file that triggers it, if you can.

You'll get a reply within a week. Fixes ship in a new release, and the changelog credits you unless you'd rather it didn't.

## Supported versions

Only the latest release gets fixes.
