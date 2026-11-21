"""Revision of the deterministic solar extractor.

It lives in its own module because two places need it: the parser writes it to
``documents.parser_version`` and the batch harness writes it to
``jobs.extractor_revision``. Sharing one constant keeps them from drifting, and
the module has no imports, so ``batch --dry-run`` can read it even where
pdfplumber is not installed.
"""

# Bump together with the extractor. The old filler-based extractor used 1, so a
# new value also makes ``batch --resume`` reprocess its old ``ok`` rows.
PARSER_VERSION = 2
