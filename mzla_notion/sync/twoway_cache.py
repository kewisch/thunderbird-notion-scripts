import json
import sqlite3

from collections import defaultdict
from pathlib import Path


class TwoWayNotionCache:
    """SQLite-backed advisory cache for two-way Notion page links."""

    SCHEMA_VERSION = 1

    def __init__(self, path, project_key):
        """Open the SQLite cache for a single sync project."""
        self.path = Path(path)
        self.project_key = project_key
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self):
        self.conn.executescript(
            """
            PRAGMA journal_mode=DELETE;

            CREATE TABLE IF NOT EXISTS cache_metadata (
                project_key TEXT PRIMARY KEY,
                schema_version INTEGER NOT NULL,
                config_fingerprint TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS notion_page_links (
                project_key TEXT NOT NULL,
                entity_kind TEXT NOT NULL,
                database_id TEXT NOT NULL,
                page_id TEXT NOT NULL,
                issue_repo TEXT,
                issue_id TEXT,
                issue_url TEXT,
                notion_url TEXT,
                last_edited_time TEXT,
                archived INTEGER NOT NULL DEFAULT 0,
                page_json TEXT NOT NULL,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (project_key, entity_kind, database_id, page_id)
            );

            CREATE INDEX IF NOT EXISTS notion_page_links_issue_idx
                ON notion_page_links (project_key, entity_kind, database_id, issue_repo, issue_id);
            """
        )
        self.conn.commit()

    def close(self):
        """Close the SQLite connection."""
        self.conn.close()

    def is_valid(self, config_fingerprint):
        """Return whether this project cache matches the current sync configuration."""
        row = self.conn.execute(
            """
            SELECT schema_version, config_fingerprint
              FROM cache_metadata
             WHERE project_key = ?
            """,
            (self.project_key,),
        ).fetchone()
        return bool(
            row and row["schema_version"] == self.SCHEMA_VERSION and row["config_fingerprint"] == config_fingerprint
        )

    def reset(self, config_fingerprint):
        """Clear this project's cached links and store the current configuration fingerprint."""
        with self.conn:
            self.conn.execute("DELETE FROM notion_page_links WHERE project_key = ?", (self.project_key,))
            self.conn.execute(
                """
                INSERT INTO cache_metadata (project_key, schema_version, config_fingerprint)
                VALUES (?, ?, ?)
                ON CONFLICT(project_key) DO UPDATE SET
                    schema_version = excluded.schema_version,
                    config_fingerprint = excluded.config_fingerprint,
                    updated_at = CURRENT_TIMESTAMP
                """,
                (self.project_key, self.SCHEMA_VERSION, config_fingerprint),
            )

    def load_linked_pages(self, entity_kind, database_id):
        """Load cached linked pages grouped by tracker repo and issue id."""
        rows = self.conn.execute(
            """
            SELECT issue_repo, issue_id, page_json
              FROM notion_page_links
             WHERE project_key = ?
               AND entity_kind = ?
               AND database_id = ?
               AND archived = 0
               AND issue_repo IS NOT NULL
               AND issue_id IS NOT NULL
             ORDER BY issue_repo, issue_id, page_id
            """,
            (self.project_key, entity_kind, database_id),
        ).fetchall()

        grouped = defaultdict(list)
        for row in rows:
            grouped[(row["issue_repo"], row["issue_id"])].append(json.loads(row["page_json"]))

        refs = defaultdict(dict)
        duplicates = set()
        for (repo, issue_id), pages in grouped.items():
            if len(pages) > 1:
                duplicates.add((repo, issue_id))
                continue

            page = pages[0]
            page["_twoway_cache_snapshot"] = True
            refs[repo][issue_id] = page

        return refs, duplicates

    def duplicate_issue_keys(self, entity_kind, database_id):
        """Return issue refs that are linked to more than one active cached page."""
        rows = self.conn.execute(
            """
            SELECT issue_repo, issue_id, COUNT(*) AS page_count
              FROM notion_page_links
             WHERE project_key = ?
               AND entity_kind = ?
               AND database_id = ?
               AND archived = 0
               AND issue_repo IS NOT NULL
               AND issue_id IS NOT NULL
             GROUP BY issue_repo, issue_id
            HAVING page_count > 1
            """,
            (self.project_key, entity_kind, database_id),
        ).fetchall()
        return {(row["issue_repo"], row["issue_id"]) for row in rows}

    def upsert_page(self, entity_kind, database_id, page, issue_ref=None, issue_url=None):
        """Insert or replace a cached Notion page link."""
        page_id = page["id"]
        page_json = dict(page)
        page_json.pop("_twoway_cache_snapshot", None)

        with self.conn:
            self.conn.execute(
                """
                DELETE FROM notion_page_links
                 WHERE project_key = ?
                   AND entity_kind = ?
                   AND database_id = ?
                   AND page_id = ?
                """,
                (self.project_key, entity_kind, database_id, page_id),
            )
            self.conn.execute(
                """
                INSERT INTO notion_page_links (
                    project_key,
                    entity_kind,
                    database_id,
                    page_id,
                    issue_repo,
                    issue_id,
                    issue_url,
                    notion_url,
                    last_edited_time,
                    archived,
                    page_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.project_key,
                    entity_kind,
                    database_id,
                    page_id,
                    issue_ref.repo if issue_ref else None,
                    issue_ref.id if issue_ref else None,
                    issue_url,
                    page.get("url"),
                    page.get("last_edited_time"),
                    1 if page.get("archived") else 0,
                    json.dumps(page_json, sort_keys=True),
                ),
            )

    def delete_page(self, entity_kind, database_id, page_id):
        """Delete one cached Notion page link."""
        with self.conn:
            self.conn.execute(
                """
                DELETE FROM notion_page_links
                 WHERE project_key = ?
                   AND entity_kind = ?
                   AND database_id = ?
                   AND page_id = ?
                """,
                (self.project_key, entity_kind, database_id, page_id),
            )
