import sys
import os
import unittest
import respx
import httpx
import logging
import json
import urllib.parse
import uuid

from pathlib import Path
from unittest.mock import MagicMock, patch
from freezegun import freeze_time

sys.path.insert(0, os.path.abspath(".."))
from libs.project_sync import synchronize as synchronize_project, Bugzilla


def load_fixture(name):
    with open(Path("fixtures") / name, "r") as fp:
        return json.load(fp)


def load_directory(path):
    basepath = Path("fixtures") / path
    for filename in os.listdir(basepath):
        if filename.endswith(".json"):
            with open(basepath / filename, "r") as fp:
                yield json.load(fp)


class BugzillaHandler:
    bugs = {}

    def __init__(self, respx_mock):
        self.bugs = {str(bug["id"]): bug for bug in load_directory("bugzilla")}

        respx_mock.route(name="bugs_get", method="GET", url="http://bugzilla.dev/rest/bug").mock(
            side_effect=self.query_handler
        )

    def query_handler(self, req):
        qs = urllib.parse.parse_qs(req.url.query)
        bugs = {
            "bugs": [
                bugdata
                for bugid in qs[b"id"][0].split(b",")
                if (bugdata := self.bugs.get(bugid.decode("utf-8"))) is not None
            ]
        }
        return httpx.Response(200, json=bugs)


class NotionDatabaseHandler:
    pages = []

    def __init__(self, database):
        self.pages = [data for data in load_directory(f"notion_{database}")]
        self.database_info = load_fixture(f"notion_{database}.json")

    def get_page(self, pageid):
        return next((page for page in self.pages if page["id"] == pageid), None)

    def query_handler(self, req):
        return {"results": self.pages}

    def update_handler(self, req):
        data = json.loads(req.content)

        if "description" in data:
            self.database_info["description"] = [data["description"]]

        return self.database_info

    def create_handler(self, reqjson):
        self.pages.append(reqjson)
        reqjson["id"] = str(uuid.uuid4())
        return reqjson


class NotionHandler:
    def __init__(self, respx_mock):
        self.milestones_handler = NotionDatabaseHandler("milestones_id")
        self.tasks_handler = NotionDatabaseHandler("tasks_id")

        DBID_PATTERN = r"^/v1/databases/(?P<dbid>[^/]+)$"
        QUERY_PATTERN = r"^/v1/databases/(?P<dbid>[^/]+)/query$"
        CHILD_PATTERN = r"^/v1/blocks/(?P<block>[^/]+)/children$"

        respx_mock.route(name="pages_create", method="POST", url="https://api.notion.com/v1/pages").mock(
            side_effect=self.pages_create_handler
        )
        respx_mock.route(
            name="db_info", method="GET", scheme="https", host="api.notion.com", path__regex=DBID_PATTERN
        ).mock(side_effect=self.database_info_handler)
        respx_mock.route(
            name="db_query", method="POST", scheme="https", host="api.notion.com", path__regex=QUERY_PATTERN
        ).mock(side_effect=self.database_query_handler)
        respx_mock.route(
            name="db_update", method="PATCH", scheme="https", host="api.notion.com", path__regex=DBID_PATTERN
        ).mock(side_effect=self.database_update_handler)

        respx_mock.route(
            name="pages_child_get", method="GET", scheme="https", host="api.notion.com", path__regex=CHILD_PATTERN
        ).mock(side_effect=self.blocks_child_handler)
        respx_mock.route(
            name="pages_child_update", method="PATCH", scheme="https", host="api.notion.com", path__regex=CHILD_PATTERN
        ).mock(side_effect=self.blocks_child_handler)

    def _get_handler(self, dbid):
        if dbid == "milestones_id":
            return self.milestones_handler
        elif dbid == "tasks_id":
            return self.tasks_handler

    def blocks_child_handler(self, req, block=None):
        return httpx.Response(
            200,
            json={
                "object": "list",
                "results": [],
                "next_cursor": None,
                "has_more": False,
                "type": "block",
                "block": {},
            },
        )

    def database_info_handler(self, req, dbid):
        handler = self._get_handler(dbid)
        if handler:
            return httpx.Response(200, json=handler.database_info)

        return httpx.Response(404)

    def database_update_handler(self, req, dbid=None):
        handler = self._get_handler(dbid)
        if handler:
            return httpx.Response(200, json=handler.update_handler(req))

        return httpx.Response(404)

    def database_query_handler(self, req, dbid=None):
        handler = self._get_handler(dbid)
        if handler:
            return httpx.Response(200, json=handler.query_handler(req))

        return httpx.Response(404)

    def pages_create_handler(self, req):
        reqjson = json.loads(req.content.decode("utf-8"))
        db_id = reqjson["parent"]["database_id"]
        if handler := self._get_handler(db_id):
            return httpx.Response(200, json=handler.create_handler(reqjson))

        return httpx.Response(404)


class BugzillaProjectTest(unittest.TestCase):
    def setUp(self):
        sync_log_level = logging.DEBUG
        handler = logging.StreamHandler(sys.stderr)

        for logName in ("project_sync", "gh_label_sync", "bugzilla_sync", "notion_sync", "notion_database"):
            logger = logging.getLogger(logName)
            logger.setLevel(sync_log_level)
            logger.addHandler(handler)
            logger.propagate = False

        self._configure_mock_urlopen([])

        self.respx = respx.mock(assert_all_called=False)
        self.maxDiff = None
        self.respx.start()

        self.bugzilla = BugzillaHandler(self.respx)
        self.notion = NotionHandler(self.respx)

    def tearDown(self):
        not_called = [route for route in self.respx.routes if not route.called]
        if not_called and self.respx._assert_all_called:
            print("NOT CALLED", not_called)
        self.respx.stop()

    def _configure_mock_urlopen(self, responses):
        def make_response(payload, headers):
            mock_response = MagicMock()
            mock_response.__enter__.return_value = mock_response
            mock_response.read.return_value = payload
            mock_response.headers = headers
            return mock_response

        patcher = patch("urllib.request.urlopen")
        self.addCleanup(patcher.stop)
        mock_urlopen = patcher.start()

        mock_urlopen.side_effect = [r if isinstance(r, Exception) else make_response(*r) for r in responses]

    def _synchronize_project(self, sync_kwargs={}, tracker_kwargs={}):
        tracker_kwargs_defaults = {
            "base_url": "http://bugzilla.dev",
            "token": "BUGZILLA_TOKEN",
            "dry": True,
            "user_map": {},
        }
        tracker = Bugzilla(**{**tracker_kwargs_defaults, **tracker_kwargs})

        sync_kwargs_defaults = {
            "project_key": "test",
            "tracker": tracker,
            "notion_token": "NOTION_TOKEN",
            "milestones_id": "milestones_id",
            "tasks_id": "tasks_id",
            "sprint_id": None,
            "milestones_body_sync": False,
            "milestones_body_sync_if_empty": False,
            "tasks_body_sync": False,
            "milestones_tracker_prefix": "[meta] ",
            "milestones_extra_label": "extra-label",
            "tasks_notion_prefix": "[tasks_notion_prefix] ",
            "property_names": {
                "notion_milestones_title": "Title",
                "notion_tasks_text_assignee": "Text Assignee",
                "notion_tasks_review_url": "Review URL",
            },
            "sprints_merge_by_name": False,
            "dry": False,
        }
        synchronize_project(**{**sync_kwargs_defaults, **sync_kwargs})

    @freeze_time("2023-01-01 12:13:14")
    def test_update_sync_stamp(self):
        self.notion.milestones_handler.pages = []

        self._synchronize_project()

        self.assertEqual(self.respx.routes["db_query"].calls.call_count, 2)
        self.assertEqual(self.respx.routes["db_info"].calls.call_count, 2)
        self.assertEqual(self.respx.routes["db_update"].calls.call_count, 2)
        self.assertEqual(self.respx.calls.call_count, 6)

        self.assertEqual(self.respx.routes["db_update"].call_count, 2)
        last_update = {
            "description": [
                {
                    "type": "text",
                    "text": {"content": "Last Issue Tracker Sync (test): 2023-01-01T12:13:14Z\n\nPrevious Content"},
                }
            ]
        }
        self.assertEqual(json.loads(self.respx.routes["db_update"].calls[0].request.content), last_update)

        last_update["description"][0]["text"]["content"] = "Last Issue Tracker Sync (test): 2023-01-01T12:13:14Z\n\n"
        self.assertEqual(json.loads(self.respx.routes["db_update"].calls[1].request.content), last_update)

    def test_milestone_sync(self):
        loglevel = logging.DEBUG
        logging.basicConfig(level=loglevel)
        logging.getLogger("TEST").debug("LOG2S")
        with self.assertLogs(level="INFO") as logs:
            self._synchronize_project(
                tracker_kwargs={"user_map": {"user1@example.com": "a5fba708-e170-4a68-8392-ba6894272c70"}}
            )

        print("LOGS", logs[0])

        self.assertIn("Synchronizing 2 milestones for bugzilla.dev", logs[0])

        # Query database tasks and milestones
        self.assertEqual(self.respx.routes["db_query"].calls.call_count, 2)
        self.assertEqual(self.respx.routes["db_query"].calls[0].request.url.path, "/v1/databases/milestones_id/query")
        self.assertEqual(self.respx.routes["db_query"].calls[1].request.url.path, "/v1/databases/tasks_id/query")

        # Get connected bugs. First call gets the meta issue, second gets the task
        self.assertEqual(self.respx.routes["bugs_get"].calls.call_count, 2)
        self.assertEqual(self.respx.routes["bugs_get"].calls[0].request.url.params["id"], "1944850")
        self.assertEqual(self.respx.routes["bugs_get"].calls[1].request.url.params["id"], "1944885")

        # Create the task to synchronize
        self.assertEqual(self.respx.routes["pages_create"].calls.call_count, 1)
        self.assertEqual(
            json.loads(self.respx.routes["pages_create"].calls.last.request.content),
            {
                "parent": {"database_id": "tasks_id"},
                "properties": {
                    "Dates": {"date": None},
                    "Issue Link": {"url": "http://bugzilla.dev/show_bug.cgi?id=1944885"},
                    "Owner": {
                        "type": "people",
                        "people": [{"id": "a5fba708-e170-4a68-8392-ba6894272c70", "object": "user"}],
                    },
                    "Priority": {"select": {"name": "P3"}},
                    "Project": {"relation": [{"id": "726fac28-6b63-48ca-90ec-0066be1a2755"}]},
                    "Status": {"status": {"name": "IN REVIEW"}},
                    "Task name": {
                        "title": [
                            {
                                "text": {
                                    "content": "[tasks_notion_prefix] bug 1944885 - Read Calendar Event - Acceptance Widget"
                                }
                            }
                        ],
                        "type": "title",
                    },
                    "Text Assignee": {"rich_text": [{"text": {"content": "user1@example.com"}}]},
                    "Review URL": {"url": "https://phabricator.services.mozilla.com/D248065"},
                },
            },
        )

        # Get existing children
        self.assertEqual(self.respx.routes["pages_child_get"].calls.call_count, 1)

        # Update to task body warning
        self.assertEqual(self.respx.routes["pages_child_update"].calls.call_count, 1)
        update_content = json.loads(self.respx.routes["pages_child_update"].calls.last.request.content)

        self.assertEqual(update_content["children"][0]["paragraph"]["rich_text"][0]["text"]["content"], "ℹ️ ")
        self.assertEqual(
            update_content["children"][0]["paragraph"]["rich_text"][1]["text"]["content"],
            "This task synchronizes with Bugzilla. Any changes you make here will be overwritten.",
        )

        # Update database info
        self.assertEqual(self.respx.routes["db_info"].calls.call_count, 2)
        self.assertEqual(self.respx.routes["db_update"].calls.call_count, 2)
        self.assertEqual(self.respx.calls.call_count, 11)

        pass


if __name__ == "__main__":
    unittest.main()
