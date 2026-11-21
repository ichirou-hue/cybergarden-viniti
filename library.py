from contextlib import closing
from hashlib import sha256
import json
from importlib.metadata import version
from pathlib import Path
import sqlite3

from pdf_source import extract
from rag_facts import analyze


MAX_BYTES = 32 * 1024 * 1024
EXTRACTOR_REVISION = 1


def analyze_pdf(name, data):
    if len(data) > MAX_BYTES or not data.startswith(b"%PDF-"):
        raise ValueError("Нужен PDF размером до 32 МиБ.")
    name = Path(name.replace("\\", "/")).name[:240] or "article.pdf"
    result = analyze(extract(data))
    result.update(
        id=sha256(data).hexdigest(), name=name, title=result["title"] or name,
        extraction={"revision": EXTRACTOR_REVISION, "pdfplumber": version("pdfplumber")},
    )
    return result


class Library:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS papers (
                    id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    pdf BLOB NOT NULL,
                    result TEXT NOT NULL,
                    area TEXT NOT NULL DEFAULT 'rag',
                    PRIMARY KEY (area, id)
                )
            """)
            self._migrate(db)

    def _migrate(self, db):
        """Upgrade a pre-area papers table to the (area, id) schema in place."""
        info = db.execute("PRAGMA table_info(papers)").fetchall()
        if not info:
            return
        if "area" not in {row["name"] for row in info}:
            db.execute("ALTER TABLE papers ADD COLUMN area TEXT NOT NULL DEFAULT 'rag'")
            info = db.execute("PRAGMA table_info(papers)").fetchall()
        pk = sorted((row["pk"], row["name"]) for row in info if row["pk"])
        if pk == [(1, "area"), (2, "id")]:
            return
        db.execute("""
            CREATE TABLE papers_migrated (
                id TEXT NOT NULL,
                name TEXT NOT NULL,
                pdf BLOB NOT NULL,
                result TEXT NOT NULL,
                area TEXT NOT NULL DEFAULT 'rag',
                PRIMARY KEY (area, id)
            )
        """)
        db.execute("""
            INSERT INTO papers_migrated (id, name, pdf, result, area)
            SELECT id, name, pdf, result, COALESCE(area, 'rag') FROM papers
        """)
        db.execute("DROP TABLE papers")
        db.execute("ALTER TABLE papers_migrated RENAME TO papers")

    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        return db

    def list(self, area=None):
        sql = """
            SELECT area, id, name, json_extract(result, '$.title') AS title,
                   json_extract(result, '$.pages') AS pages,
                   json_array_length(result, '$.facts') AS facts
            FROM papers
        """
        params = ()
        if area is not None:
            sql += " WHERE area = ?"
            params = (area,)
        sql += " ORDER BY rowid DESC"
        with closing(self.connect()) as db:
            rows = db.execute(sql, params)
            return [dict(row) for row in rows]

    def get(self, paper_id, *, with_pdf=False, area="rag"):
        fields = "result, pdf" if with_pdf else "result"
        with closing(self.connect()) as db:
            row = db.execute(
                f"SELECT {fields} FROM papers WHERE area = ? AND id = ?",
                (area, paper_id),
            ).fetchone()
        if row is None:
            raise KeyError((area, paper_id))
        document = json.loads(row["result"])
        return (document, row["pdf"]) if with_pdf else document

    def ingest(self, name, data, area="rag"):
        if len(data) > MAX_BYTES or not data.startswith(b"%PDF-"):
            raise ValueError("Нужен PDF размером до 32 МиБ.")
        paper_id = sha256(data).hexdigest()
        try:
            return self.get(paper_id, area=area)
        except KeyError:
            pass
        result = analyze_pdf(name, data)
        with closing(self.connect()) as db, db:
            db.execute(
                "INSERT OR IGNORE INTO papers (id, name, pdf, result, area) VALUES (?, ?, ?, ?, ?)",
                (paper_id, result["name"], data,
                 json.dumps(result, ensure_ascii=False, allow_nan=False), area),
            )
        return self.get(paper_id, area=area)
