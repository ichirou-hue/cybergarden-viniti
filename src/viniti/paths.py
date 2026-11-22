"""Repository locations resolved once, so a module can move without moving paths.

The installed packages live in ``src/``. This module is two levels below the
repository root.

The module is a leaf on purpose: it imports nothing but ``pathlib``, so
``batch --dry-run`` can read ``CORPUS_ROOTS`` even where pdfplumber is missing.
"""
from pathlib import Path

# src/viniti/paths.py -> src/viniti/ -> src/ -> repository root.
REPO_ROOT = Path(__file__).resolve().parents[2]

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
