import datetime
import unittest

from mzla_notion.sync.twoway import TrackerTwoWaySync
from mzla_notion.tracker.common import Issue, IssueRef, IssueTracker, UserMap

from .handlers import BaseTestCase


class StaticUserMap(UserMap):
    def tracker_mention(self, tracker_user):
        return tracker_user


class TwoWayTestTracker(IssueTracker):
    name = "TwoWayTestTracker"

    def __init__(self, issues, recent_ids=None, **kwargs):
        defaults = {
            "notion_milestones_title": "Title",
            "notion_milestones_assignee": "Owner",
            "notion_milestones_priority": "Priority",
            "notion_milestones_dates": "Dates",
            "notion_tasks_title": "Title",
            "notion_tasks_assignee": "Owner",
            "notion_tasks_dates": "Dates",
            "notion_tasks_milestone_relation": "Project",
            "notion_tasks_text_assignee": "Text Assignee",
            "notion_tasks_priority": "Priority",
            "notion_tasks_review_url": "Review URL",
            "notion_tasks_reviewers": "Peer Reviewer",
            "notion_tasks_repository": "Repository",
            "notion_tasks_labels": "Labels",
            "notion_tasks_sprint_relation": "Sprint",
            "notion_issue_field": "Issue Link",
        }
        property_names = {**defaults, **kwargs.pop("property_names", {})}
        super().__init__(property_names=property_names, **kwargs)
        self.user_map = StaticUserMap({})
        self.issues = {(issue.repo, issue.id): issue for issue in issues}
        self.recent_ids = set(recent_ids or [])
        self.updated_milestones = []
        self.updated_tasks = []

    def parse_issueref(self, ref):
        parts = ref.split("/")
        if len(parts) == 5 and parts[2] == "example.com":
            return IssueRef(repo=parts[3], id=parts[4])
        return None

    async def get_issues_by_number(self, refs, sub_issues=False):
        for ref in refs:
            issue = self.issues.get((ref.repo, ref.id))
            if issue:
                yield issue

    async def get_recent_issues_by_repo(self, since, sub_issues=False):
        repos = {}
        for repo, issue_id in self.recent_ids:
            issue = self.issues.get((repo, issue_id))
            if issue:
                repos.setdefault(repo, {})[issue_id] = issue
        return repos

    async def update_milestone_issue(self, old_issue, new_issue):
        self.updated_milestones.append((old_issue, new_issue))

    async def update_task_issue(self, old_issue, new_issue):
        self.updated_tasks.append((old_issue, new_issue))


class TwoWaySyncTest(BaseTestCase):
    async def _run_sync(self, tracker, **kwargs):
        sync = TrackerTwoWaySync(
            project_key="twoway",
            tracker=tracker,
            notion_token="NOTION_TOKEN",
            milestones_id="milestones_id",
            tasks_id="tasks_id",
            tasks_notion_prefix="[prefix] ",
            dry=False,
            **kwargs,
        )
        await sync.synchronize()

    def _issue(self, issue_id, *, updated, parents=None, title=None, state="Backlog"):
        return Issue(
            repo="repo",
            id=issue_id,
            parents=parents or [],
            title=title or f"Issue {issue_id}",
            description="desc",
            state=state,
            priority="P2",
            assignees=set(),
            labels=set(),
            url=f"https://example.com/repo/{issue_id}",
            created_date=datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc),
            updated_date=updated,
            sub_issues=[],
        )

    async def test_incremental_skips_when_not_recent(self):
        tracker = TwoWayTestTracker(
            issues=[
                self._issue("123", updated=datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc)),
                self._issue(
                    "234",
                    updated=datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc),
                    parents=[IssueRef(repo="repo", id="123")],
                ),
            ],
            recent_ids=[],
        )

        await self._run_sync(tracker, full_sync=False, incremental_lookback_days=7)
        self.assertEqual(len(tracker.updated_milestones), 0)
        self.assertEqual(len(tracker.updated_tasks), 0)
        self.assertEqual(self.respx.routes["pages_update"].calls.call_count, 0)

    async def test_full_sync_processes_linked_refs(self):
        tracker = TwoWayTestTracker(
            issues=[
                self._issue("123", updated=datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc), title="M!"),
                self._issue(
                    "234",
                    updated=datetime.datetime(2025, 1, 1, tzinfo=datetime.timezone.utc),
                    parents=[IssueRef(repo="repo", id="123")],
                    title="Task Updated",
                ),
            ],
            recent_ids=[],
        )

        await self._run_sync(tracker, full_sync=True)
        self.assertGreaterEqual(len(tracker.updated_milestones), 1)
        self.assertEqual(len(tracker.updated_tasks), 0)

    async def test_lww_tie_break_defaults(self):
        tie_ts = datetime.datetime(2022, 7, 6, 20, 25, tzinfo=datetime.timezone.utc)
        tracker = TwoWayTestTracker(
            issues=[
                self._issue("123", updated=tie_ts),
                self._issue("234", updated=tie_ts, parents=[IssueRef(repo="repo", id="123")]),
            ],
            recent_ids=[("repo", "123"), ("repo", "234")],
        )

        await self._run_sync(
            tracker,
            tasks_tracker_to_notion=True,
            tasks_notion_to_tracker=True,
            milestones_tracker_to_notion=True,
            milestones_notion_to_tracker=True,
            full_sync=False,
        )

        # Tie fallback: tracker for tasks, notion for milestones
        self.assertEqual(len(tracker.updated_tasks), 0)
        self.assertGreaterEqual(len(tracker.updated_milestones), 1)


if __name__ == "__main__":
    unittest.main()
