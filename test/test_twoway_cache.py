import tempfile
import unittest

from pathlib import Path

from mzla_notion.sync.twoway_cache import TwoWayNotionCache
from mzla_notion.tracker.common import IssueRef


def make_page(page_id, issue_url):
    return {
        "object": "page",
        "id": page_id,
        "last_edited_time": "2025-01-01T00:00:00.000Z",
        "url": f"https://notion.so/example/{page_id}",
        "properties": {
            "Issue Link": {
                "type": "files",
                "files": [{"name": issue_url.rsplit("/", 2)[-1], "external": {"url": issue_url}}],
            }
        },
    }


class TwoWayNotionCacheTest(unittest.TestCase):
    def _cache(self):
        tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(tmpdir.cleanup)
        cache = TwoWayNotionCache(Path(tmpdir.name) / "cache.sqlite3", "project")
        self.addCleanup(cache.close)
        cache.reset("fingerprint")
        return cache

    def test_cache_validates_metadata(self):
        cache = self._cache()

        self.assertTrue(cache.is_valid("fingerprint"))
        self.assertFalse(cache.is_valid("other-fingerprint"))

    def test_upsert_moves_page_between_issue_refs(self):
        cache = self._cache()
        page = make_page("page-1", "https://example.com/repo/1")

        cache.upsert_page("task", "tasks-db", page, IssueRef(repo="repo", id="1"), "https://example.com/repo/1")
        cache.upsert_page("task", "tasks-db", page, IssueRef(repo="repo", id="2"), "https://example.com/repo/2")

        refs, duplicates = cache.load_linked_pages("task", "tasks-db")
        self.assertEqual(duplicates, set())
        self.assertNotIn("1", refs["repo"])
        self.assertEqual(refs["repo"]["2"]["id"], "page-1")

    def test_duplicate_issue_refs_are_reported_and_not_loaded(self):
        cache = self._cache()
        url = "https://example.com/repo/1"
        cache.upsert_page("task", "tasks-db", make_page("page-1", url), IssueRef(repo="repo", id="1"), url)
        cache.upsert_page("task", "tasks-db", make_page("page-2", url), IssueRef(repo="repo", id="1"), url)

        refs, duplicates = cache.load_linked_pages("task", "tasks-db")
        self.assertEqual(duplicates, {("repo", "1")})
        self.assertNotIn("1", refs["repo"])

    def test_delete_page_removes_mapping(self):
        cache = self._cache()
        page = make_page("page-1", "https://example.com/repo/1")

        cache.upsert_page("task", "tasks-db", page, IssueRef(repo="repo", id="1"), "https://example.com/repo/1")
        cache.delete_page("task", "tasks-db", "page-1")

        refs, _ = cache.load_linked_pages("task", "tasks-db")
        self.assertNotIn("1", refs["repo"])


if __name__ == "__main__":
    unittest.main()
