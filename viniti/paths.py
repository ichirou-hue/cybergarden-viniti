"""Repository locations resolved once, so a module can move without moving paths.

Root modules still own their public names (``db.PASSPORT``, ``batch.CORPUS_ROOTS``),
but they all derive the same few places from the repository root. Keeping that
derivation here means a module can later move into ``viniti/`` without changing
where the repository is: this file is one level below the root, so ``REPO_ROOT``
is ``parent.parent``.

The module is a leaf on purpose: it imports nothing but ``pathlib``, so
``batch --dry-run`` can read ``CORPUS_ROOTS`` even where pdfplumber is missing.
"""
from pathlib import Path

# viniti/paths.py -> viniti/ -> repository root.
REPO_ROOT = Path(__file__).resolve().parent.parent

DATA_DIR = REPO_ROOT / "data"
STATIC_DIR = REPO_ROOT / "static"
PROFILES_DIR = REPO_ROOT / "profiles"
PASSPORT = REPO_ROOT / "passport.json"
VOCAB = REPO_ROOT / "rag_metric_vocab.json"

# Both participant kits use the same split names (dev, test, dev_gold).
CORPUS_ROOTS = {
    "solar": REPO_ROOT / "_corpus" / "area1",
    "rag": REPO_ROOT / "_corpus" / "rag" / "RAG_ВИНИТИ_участникам_2026-11-20",
}

# server.py runs the profile parser as a subprocess from the repository root.
PARSE_SCRIPT = REPO_ROOT / "parse_document.py"
