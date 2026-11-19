from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

from pdf_source import extract
from rag_facts import analyze


MAX_BYTES = 20 * 1024 * 1024


class Library:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS papers (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    pdf BLOB NOT NULL,
                    result TEXT NOT NULL
                )
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def list(self):
        with closing(self.connect()) as db:
            rows = db.execute("""
                SELECT id, name, json_extract(result, '$.title') AS title,
                       json_extract(result, '$.pages') AS pages,
                       json_array_length(result, '$.facts') AS facts
                FROM papers ORDER BY rowid DESC
            """)
            return [dict(row) for row in rows]

    def get(self, paper_id, *, with_pdf=False):
        fields = "result, pdf" if with_pdf else "result"
        with closing(self.connect()) as db:
            row = db.execute(f"SELECT {fields} FROM papers WHERE id = ?", (paper_id,)).fetchone()
        if row is None:
            raise KeyError(paper_id)
        document = json.loads(row["result"])
        return (document, row["pdf"]) if with_pdf else document

    def ingest(self, name, data):
        if len(data) > MAX_BYTES or not data.startswith(b"%PDF-"):
            raise ValueError("Нужен PDF размером до 20 МиБ.")
        paper_id = sha256(data).hexdigest()
        try:
            return self.get(paper_id)
        except KeyError:
            pass
        name = Path(name.replace("\\", "/")).name[:240] or "article.pdf"
        result = analyze(extract(data))
        result.update(id=paper_id, name=name, title=result["title"] or name)
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT OR IGNORE INTO papers VALUES (?, ?, ?, ?)",
                (paper_id, name, data, json.dumps(result, ensure_ascii=False, allow_nan=False)),
            )
        return self.get(paper_id)
