"""
ATLAS Session Search — FTS5 full-text search across all session history.

SQLite database at memory/atlas_search.db with FTS5 virtual tables for
sessions, skills, and playbook entries.  Triggers maintain the FTS5 index
automatically on insert/update/delete.

Voice commands:
  "ATLAS search our history for [query]"
  "ATLAS when did we work on [topic]"
  "ATLAS find the code we wrote for [feature]"
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path
from typing import List, Optional, Tuple

log = logging.getLogger(__name__)

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

-- ── Sessions ────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS sessions (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    date    TEXT    NOT NULL,
    role    TEXT    NOT NULL,   -- user | assistant
    content TEXT,
    tags    TEXT    DEFAULT ''
);

CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
    content,
    tags,
    content='sessions',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS sessions_ai AFTER INSERT ON sessions BEGIN
    INSERT INTO sessions_fts(rowid, content, tags)
    VALUES (new.id, COALESCE(new.content,''), COALESCE(new.tags,''));
END;

CREATE TRIGGER IF NOT EXISTS sessions_au AFTER UPDATE ON sessions BEGIN
    INSERT INTO sessions_fts(sessions_fts, rowid, content, tags)
    VALUES ('delete', old.id, COALESCE(old.content,''), COALESCE(old.tags,''));
    INSERT INTO sessions_fts(rowid, content, tags)
    VALUES (new.id, COALESCE(new.content,''), COALESCE(new.tags,''));
END;

CREATE TRIGGER IF NOT EXISTS sessions_ad AFTER DELETE ON sessions BEGIN
    INSERT INTO sessions_fts(sessions_fts, rowid, content, tags)
    VALUES ('delete', old.id, COALESCE(old.content,''), COALESCE(old.tags,''));
END;

-- ── Skills ───────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS skills_index (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT    NOT NULL,
    description TEXT,
    body        TEXT,
    last_updated TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts USING fts5(
    name,
    description,
    body,
    content='skills_index',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS skills_ai AFTER INSERT ON skills_index BEGIN
    INSERT INTO skills_fts(rowid, name, description, body)
    VALUES (new.id, COALESCE(new.name,''), COALESCE(new.description,''), COALESCE(new.body,''));
END;

CREATE TRIGGER IF NOT EXISTS skills_au AFTER UPDATE ON skills_index BEGIN
    INSERT INTO skills_fts(skills_fts, rowid, name, description, body)
    VALUES ('delete', old.id, COALESCE(old.name,''), COALESCE(old.description,''), COALESCE(old.body,''));
    INSERT INTO skills_fts(rowid, name, description, body)
    VALUES (new.id, COALESCE(new.name,''), COALESCE(new.description,''), COALESCE(new.body,''));
END;

CREATE TRIGGER IF NOT EXISTS skills_ad AFTER DELETE ON skills_index BEGIN
    INSERT INTO skills_fts(skills_fts, rowid, name, description, body)
    VALUES ('delete', old.id, COALESCE(old.name,''), COALESCE(old.description,''), COALESCE(old.body,''));
END;

-- ── Playbook ──────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS playbook_index (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    type        TEXT    NOT NULL,   -- strategy | pitfall | preference
    title       TEXT    NOT NULL,
    body        TEXT,
    category    TEXT    DEFAULT '',
    last_updated TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS playbook_fts USING fts5(
    title,
    body,
    category,
    content='playbook_index',
    content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS playbook_ai AFTER INSERT ON playbook_index BEGIN
    INSERT INTO playbook_fts(rowid, title, body, category)
    VALUES (new.id, COALESCE(new.title,''), COALESCE(new.body,''), COALESCE(new.category,''));
END;

CREATE TRIGGER IF NOT EXISTS playbook_au AFTER UPDATE ON playbook_index BEGIN
    INSERT INTO playbook_fts(playbook_fts, rowid, title, body, category)
    VALUES ('delete', old.id, COALESCE(old.title,''), COALESCE(old.body,''), COALESCE(old.category,''));
    INSERT INTO playbook_fts(rowid, title, body, category)
    VALUES (new.id, COALESCE(new.title,''), COALESCE(new.body,''), COALESCE(new.category,''));
END;

CREATE TRIGGER IF NOT EXISTS playbook_ad AFTER DELETE ON playbook_index BEGIN
    INSERT INTO playbook_fts(playbook_fts, rowid, title, body, category)
    VALUES ('delete', old.id, COALESCE(old.title,''), COALESCE(old.body,''), COALESCE(old.category,''));
END;
"""


class SessionSearch:
    """
    Full-text search index for ATLAS history, skills, and playbook.

    Usage:
        search = SessionSearch(db_path)
        search.index_message("user", "how do I set up APScheduler?")
        results = search.search_sessions("APScheduler")
    """

    def __init__(self, db_path: Path, brain=None):
        self._path  = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._brain = brain
        self._lock  = threading.Lock()
        self._conn  = self._connect()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._path), check_same_thread=False)
        conn.executescript(_SCHEMA)
        conn.commit()
        log.info("SessionSearch: FTS5 database ready at %s", self._path)
        return conn

    # ── Indexing ───────────────────────────────────────────────────────────────

    def index_message(self, role: str, content: str, tags: str = "") -> None:
        """Index a single conversation message."""
        if not content:
            return
        today = date.today().isoformat()
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO sessions(date, role, content, tags) VALUES (?,?,?,?)",
                    (today, role, content[:4000], tags),
                )
                self._conn.commit()
            except Exception as exc:
                log.warning("SessionSearch: index_message failed: %s", exc)

    def index_skill(self, name: str, description: str, body: str) -> None:
        """Upsert a skill into the skills_index."""
        today = date.today().isoformat()
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT id FROM skills_index WHERE name = ?", (name,)
                ).fetchone()
                if row:
                    self._conn.execute(
                        "UPDATE skills_index SET description=?, body=?, last_updated=? WHERE id=?",
                        (description[:500], body[:4000], today, row[0]),
                    )
                else:
                    self._conn.execute(
                        "INSERT INTO skills_index(name,description,body,last_updated) VALUES(?,?,?,?)",
                        (name, description[:500], body[:4000], today),
                    )
                self._conn.commit()
            except Exception as exc:
                log.warning("SessionSearch: index_skill failed: %s", exc)

    def index_playbook_entry(self, entry_type: str, title: str,
                             body: str, category: str = "") -> None:
        """Upsert a playbook entry."""
        today = date.today().isoformat()
        with self._lock:
            try:
                row = self._conn.execute(
                    "SELECT id FROM playbook_index WHERE title = ? AND type = ?",
                    (title, entry_type),
                ).fetchone()
                if row:
                    self._conn.execute(
                        "UPDATE playbook_index SET body=?, category=?, last_updated=? WHERE id=?",
                        (body[:4000], category, today, row[0]),
                    )
                else:
                    self._conn.execute(
                        "INSERT INTO playbook_index(type,title,body,category,last_updated) "
                        "VALUES(?,?,?,?,?)",
                        (entry_type, title, body[:4000], category, today),
                    )
                self._conn.commit()
            except Exception as exc:
                log.warning("SessionSearch: index_playbook failed: %s", exc)

    # ── Searching ──────────────────────────────────────────────────────────────

    def search_sessions(self, query: str, limit: int = 5) -> List[dict]:
        """FTS5 ranked search across conversation history."""
        with self._lock:
            try:
                rows = self._conn.execute(
                    """
                    SELECT s.date, s.role, s.content, s.tags,
                           rank
                    FROM sessions_fts
                    JOIN sessions s ON sessions_fts.rowid = s.id
                    WHERE sessions_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (query, limit),
                ).fetchall()
                return [
                    {"date": r[0], "role": r[1], "content": r[2], "tags": r[3]}
                    for r in rows
                ]
            except Exception as exc:
                log.warning("SessionSearch: search_sessions failed: %s", exc)
                return []

    def search_skills(self, query: str, limit: int = 3) -> List[dict]:
        with self._lock:
            try:
                rows = self._conn.execute(
                    """
                    SELECT si.name, si.description, si.body, si.last_updated, rank
                    FROM skills_fts
                    JOIN skills_index si ON skills_fts.rowid = si.id
                    WHERE skills_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                    """,
                    (query, limit),
                ).fetchall()
                return [
                    {"name": r[0], "description": r[1], "body": r[2], "last_updated": r[3]}
                    for r in rows
                ]
            except Exception as exc:
                log.warning("SessionSearch: search_skills failed: %s", exc)
                return []

    def search_all(self, query: str) -> dict:
        """Search sessions, skills, and playbook simultaneously."""
        return {
            "sessions": self.search_sessions(query, limit=5),
            "skills":   self.search_skills(query, limit=3),
        }

    def format_results_for_voice(self, results: dict, query: str) -> str:
        """Summarise search results into a voice-friendly response."""
        sessions = results.get("sessions", [])
        skills   = results.get("skills", [])

        if not sessions and not skills:
            return f"I couldn't find anything about '{query}' in our history, Boss."

        parts = []
        if sessions:
            # Group by date
            by_date: dict[str, list] = {}
            for r in sessions[:5]:
                by_date.setdefault(r["date"], []).append(r["content"][:80])
            dates_str = "; ".join(
                f"on {d}: {' / '.join(msgs[:2])}"
                for d, msgs in list(by_date.items())[:3]
            )
            parts.append(f"Session history: {dates_str}")
        if skills:
            skill_names = ", ".join(r["name"] for r in skills[:3])
            parts.append(f"Related skills: {skill_names}")

        return " | ".join(parts)

    # ── Bulk indexing from vault ───────────────────────────────────────────────

    def bulk_index_from_vault(self, vault_brain) -> int:
        """Index all existing skills and playbook entries from vault."""
        count = 0
        try:
            for p in vault_brain.list_notes(vault_brain.skills_dir):
                r = vault_brain.read_note(p)
                if r:
                    fm, body = r
                    self.index_skill(
                        fm.get("title", p.stem),
                        fm.get("description", ""),
                        body,
                    )
                    count += 1
            for p in vault_brain.list_notes(vault_brain.strat_dir):
                r = vault_brain.read_note(p)
                if r:
                    fm, body = r
                    self.index_playbook_entry(
                        "strategy", fm.get("title", p.stem), body, fm.get("category", "")
                    )
                    count += 1
            log.info("SessionSearch: bulk-indexed %d items from vault.", count)
        except Exception as exc:
            log.warning("SessionSearch: bulk index failed: %s", exc)
        return count

    # ── Voice commands ─────────────────────────────────────────────────────────

    def handle(self, text: str) -> Optional[str]:
        lower = text.lower().strip()

        # "ATLAS search our history for X"
        if any(p in lower for p in ("atlas search our history for",
                                     "atlas search history for",
                                     "search our history for")):
            query = None
            for phrase in ("atlas search our history for", "atlas search history for",
                           "search our history for"):
                if phrase in lower:
                    query = lower.split(phrase, 1)[-1].strip()
                    break
            if not query:
                return "What should I search for, Boss?"
            results = self.search_all(query)
            return self.format_results_for_voice(results, query)

        # "ATLAS when did we work on X"
        if any(p in lower for p in ("atlas when did we work on",
                                     "when did we work on",
                                     "atlas when did we last work on")):
            for phrase in ("atlas when did we last work on", "atlas when did we work on",
                           "when did we work on"):
                if phrase in lower:
                    query = lower.split(phrase, 1)[-1].strip()
                    break
            else:
                query = ""
            if not query:
                return "What topic should I look for, Boss?"
            results = self.search_sessions(query, limit=3)
            if not results:
                return f"I don't have any history about '{query}', Boss."
            last = results[0]
            return f"I last saw '{query}' on {last['date']}: {last['content'][:100]}"

        # "ATLAS find the code we wrote for X"
        if any(p in lower for p in ("atlas find the code we wrote for",
                                     "find the code for",
                                     "atlas find code for")):
            for phrase in ("atlas find the code we wrote for", "find the code for",
                           "atlas find code for"):
                if phrase in lower:
                    query = lower.split(phrase, 1)[-1].strip()
                    break
            else:
                query = ""
            if not query:
                return "What feature's code should I find, Boss?"
            results = self.search_sessions(query + " code", limit=5)
            code_hits = [r for r in results if any(
                kw in r.get("content", "").lower()
                for kw in ("def ", "class ", "import ", "```")
            )]
            if not code_hits:
                return f"I couldn't find code related to '{query}' in our history, Boss."
            return (f"Found code discussion on {code_hits[0]['date']}: "
                    f"{code_hits[0]['content'][:120]}")

        return None

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
