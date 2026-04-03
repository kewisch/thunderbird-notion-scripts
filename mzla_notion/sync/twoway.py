import asyncio
import dataclasses
import datetime
import logging

from collections import defaultdict

from .base import BaseSync

from ..util import ensure_date
from ..tracker.common import IssueRef
from ..util import diff_dataclasses
from ..notion_data import CustomNotionToMarkdown

logger = logging.getLogger("project_sync")


class TrackerTwoWaySync(BaseSync):
    """Two-way tracker sync with incremental/LWW semantics."""

    def __init__(
        self,
        incremental_lookback_days=7,
        tasks_tracker_to_notion=True,
        tasks_notion_to_tracker=False,
        milestones_tracker_to_notion=False,
        milestones_notion_to_tracker=True,
        full_sync=False,
        **kwargs,
    ):
        """Initialize two-way sync configuration and directional behavior."""
        super().__init__(**kwargs)
        self.incremental_lookback_days = incremental_lookback_days
        self.tasks_tracker_to_notion = tasks_tracker_to_notion
        self.tasks_notion_to_tracker = tasks_notion_to_tracker
        self.milestones_tracker_to_notion = milestones_tracker_to_notion
        self.milestones_notion_to_tracker = milestones_notion_to_tracker
        self.full_sync = full_sync

    async def _async_init(self):
        async with asyncio.TaskGroup() as tg:
            tg.create_task(super()._async_init())
            milestones_issues = tg.create_task(
                self._discover_notion_issues(self.milestones_db.database_id, self.propnames["notion_milestones_team"])
            )

        self._notion_milestone_issues = milestones_issues.result()

    def _find_task_parents(self, tracker_issue):
        milestone_issues = self._notion_milestone_issues

        found_milestone_parents = [
            milestone_parent
            for parent in tracker_issue.parents
            if (milestone_parent := milestone_issues.get(parent.repo, {}).get(parent.id, None)) is not None
        ]

        return found_milestone_parents

    def _page_timestamp(self, page):
        value = page.get("last_edited_time")
        if not value:
            return None
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))

    def _format_timestamp(self, value):
        if not value:
            return "None"
        return value.isoformat()

    def _pick_direction(self, entity_kind, tracker_issue, notion_page, tracker_to_notion, notion_to_tracker):
        if tracker_to_notion and not notion_to_tracker:
            return "tracker_to_notion"
        if notion_to_tracker and not tracker_to_notion:
            return "notion_to_tracker"
        if not tracker_to_notion and not notion_to_tracker:
            return None

        tracker_ts = tracker_issue.updated_date or tracker_issue.created_date
        notion_ts = self._page_timestamp(notion_page)
        fallback = "tracker_to_notion" if entity_kind == "task" else "notion_to_tracker"

        if tracker_ts and notion_ts:
            if tracker_ts > notion_ts:
                return "tracker_to_notion"
            if notion_ts > tracker_ts:
                return "notion_to_tracker"
            return fallback

        if tracker_ts and not notion_ts:
            return "tracker_to_notion"
        if notion_ts and not tracker_ts:
            return "notion_to_tracker"
        return fallback

    def _get_milestone_notion_data_from_tracker(self, tracker_issue):
        notion_data = {
            self.propnames["notion_milestones_title"]: tracker_issue.title,
            self.propnames["notion_issue_field"]: tracker_issue.url,
        }

        assignees = [user.notion_user for user in tracker_issue.assignees if user.notion_user is not None]
        self._set_if_prop(notion_data, "notion_milestones_assignee", assignees or None)
        self._set_if_prop(notion_data, "notion_milestones_priority", tracker_issue.priority)

        state = tracker_issue.state
        if not state:
            state = (
                self.propnames["notion_closed_states"][0]
                if tracker_issue.closed_date
                else self.propnames["notion_default_open_state"]
            )
        self._set_if_prop(notion_data, "notion_milestones_status", state)

        self._set_if_date_prop(
            notion_data,
            "notion_milestones_dates",
            ensure_date(tracker_issue.start_date),
            ensure_date(tracker_issue.end_date or tracker_issue.closed_date),
        )

        return notion_data

    async def synchronize_single_milestone_from_tracker(self, tracker_issue, page):
        """Apply tracker milestone fields onto the linked Notion milestone page."""
        notion_data = self._get_milestone_notion_data_from_tracker(tracker_issue)
        changed = await self.milestones_db.update_page(page, notion_data)
        if changed:
            logger.info(f"Updating milestone {tracker_issue.repo}#{tracker_issue.id} from tracker")
        else:
            logger.info(f"Unchanged milestone {tracker_issue.repo}#{tracker_issue.id}")

    async def synchronize_single_milestone(self, tracker_issue, page, skip_unchanged_msg=False):
        """Synchronize a single Notion milestone to the issue tracker."""
        old_issue_url = self._get_prop(page, "notion_issue_field")
        if old_issue_url and old_issue_url != tracker_issue.url:
            logger.warning(
                f"Milestone URL changed for {tracker_issue.repo}#{tracker_issue.id}: {old_issue_url} -> {tracker_issue.url}"
            )

        body = tracker_issue.description
        if self.milestones_body_sync or (self.milestones_body_sync_if_empty and not len(tracker_issue.description)):
            blocks = await self.milestones_db.get_page_contents(page["id"])
            converter = CustomNotionToMarkdown(self.notion, strip_images=True, tracker=self.tracker)
            body = await converter.convert(blocks) or ""

        community_assignees = {assignee for assignee in tracker_issue.assignees if assignee.notion_user is None}
        milestone_assignees = {
            self.tracker.new_user(notion_user=assignee["id"])
            for assignee in self._get_prop(page, "notion_milestones_assignee", [])
        }

        title = self._get_richtext_prop(page, "notion_milestones_title", "")
        labels = set(tracker_issue.labels)
        if self.milestones_extra_label:
            labels.add(self.milestones_extra_label)

        start_date, end_date = self._get_date_prop(page, "notion_milestones_dates")

        new_issue = dataclasses.replace(
            tracker_issue,
            title=self.milestones_tracker_prefix + title,
            labels=labels,
            description=body,
            state=(self._get_prop(page, "notion_milestones_status") or {}).get("name"),
            priority=(self._get_prop(page, "notion_milestones_priority") or {}).get("name"),
            assignees=community_assignees.union(milestone_assignees),
            notion_url=page.get("url", ""),
            start_date=ensure_date(start_date) if start_date else None,
            end_date=ensure_date(end_date) if end_date else None,
        )

        if self.milestones_issue_type:
            new_issue.issue_type = self.milestones_issue_type

        if tracker_issue != new_issue:
            logger.info(
                f"Updating milestone {tracker_issue.id} - {tracker_issue.title} ({tracker_issue.url} / {new_issue.notion_url})"
            )
            diff_dataclasses(tracker_issue, new_issue, log=logger.debug)

            if not self.dry:
                await self.tracker.update_milestone_issue(tracker_issue, new_issue)
        elif not skip_unchanged_msg:
            logger.info(
                f"Unchanged milestone {tracker_issue.id} - {tracker_issue.title} ({tracker_issue.url} / {new_issue.notion_url})"
            )

    def _get_task_tracker_issue_from_notion(self, tracker_issue, page):
        title = self._get_richtext_prop(page, "notion_tasks_title", tracker_issue.title)
        status = (self._get_prop(page, "notion_tasks_status") or {}).get("name")
        priority = (self._get_prop(page, "notion_tasks_priority") or {}).get("name")

        assignees = {
            self.tracker.new_user(notion_user=assignee["id"])
            for assignee in self._get_prop(page, "notion_tasks_assignee", [])
        }

        return dataclasses.replace(
            tracker_issue,
            title=title or tracker_issue.title,
            state=status or tracker_issue.state,
            priority=priority or tracker_issue.priority,
            assignees=assignees if assignees else tracker_issue.assignees,
            notion_url=page.get("url", tracker_issue.notion_url),
        )

    async def synchronize_single_task_to_tracker(self, tracker_issue, page):
        """Apply Notion task fields onto the linked tracker task issue."""
        new_issue = self._get_task_tracker_issue_from_notion(tracker_issue, page)
        if new_issue != tracker_issue:
            logger.info(f"Updating task {tracker_issue.repo}#{tracker_issue.id} from notion")
            if not self.dry:
                await self.tracker.update_task_issue(tracker_issue, new_issue)
        else:
            logger.info(f"Unchanged task {tracker_issue.repo}#{tracker_issue.id}")

    def _collect_candidates(self, notion_refs, recent_refs, since):
        linked = set(notion_refs.keys())
        if self.full_sync:
            return linked, len(linked), 0

        notion_recent = {
            key for key, page in notion_refs.items() if (ts := self._page_timestamp(page)) is not None and ts >= since
        }
        tracker_recent = linked.intersection(recent_refs)

        candidates = notion_recent.union(tracker_recent)
        return candidates, len(linked), len(linked - candidates)

    async def _load_tracker_candidates(self, candidates, recent_by_repo):
        tracker_issues = {}
        missing = defaultdict(list)

        for repo, issue_id in candidates:
            issue = recent_by_repo.get(repo, {}).get(issue_id)
            if issue:
                tracker_issues[(repo, issue_id)] = issue
            else:
                missing[repo].append(IssueRef(repo=repo, id=issue_id))

        for repo, refs in missing.items():
            async for issue in self.tracker.get_issues_by_number(refs):
                tracker_issues[(repo, issue.id)] = issue

        return tracker_issues

    async def synchronize(self):
        """Run a complete incremental two-way synchronization cycle."""
        await self._async_init()

        timestamp = datetime.datetime.now(datetime.UTC)
        since = timestamp - datetime.timedelta(days=self.incremental_lookback_days)

        recent_tasks_by_repo = {}
        recent_milestones_by_repo = {}

        if not self.full_sync and (
            self.tasks_tracker_to_notion
            or self.tasks_notion_to_tracker
            or self.milestones_tracker_to_notion
            or self.milestones_notion_to_tracker
        ):
            recent_tasks_by_repo = await self.tracker.get_recent_issues_by_repo(since, sub_issues=False)
            recent_milestones_by_repo = recent_tasks_by_repo

        notion_task_refs = {
            (repo, issue_id): page
            for repo, issues in self._notion_tasks_issues.items()
            for issue_id, page in issues.items()
        }
        notion_milestone_refs = {
            (repo, issue_id): page
            for repo, issues in self._notion_milestone_issues.items()
            for issue_id, page in issues.items()
        }

        recent_task_keys = {(repo, issue_id) for repo, issues in recent_tasks_by_repo.items() for issue_id in issues}
        recent_milestone_keys = {
            (repo, issue_id) for repo, issues in recent_milestones_by_repo.items() for issue_id in issues
        }

        task_candidates, task_linked_count, task_skipped = self._collect_candidates(
            notion_task_refs, recent_task_keys, since
        )
        milestone_candidates, milestone_linked_count, milestone_skipped = self._collect_candidates(
            notion_milestone_refs, recent_milestone_keys, since
        )

        task_issues = await self._load_tracker_candidates(task_candidates, recent_tasks_by_repo)
        milestone_issues = await self._load_tracker_candidates(milestone_candidates, recent_milestones_by_repo)

        stats = {
            "tasks_updated_from_tracker": 0,
            "tasks_updated_from_notion": 0,
            "milestones_updated_from_tracker": 0,
            "milestones_updated_from_notion": 0,
            "conflicts_resolved": 0,
        }

        async with asyncio.TaskGroup() as tg:
            for key, issue in task_issues.items():
                page = notion_task_refs[key]
                direction = self._pick_direction(
                    "task",
                    issue,
                    page,
                    self.tasks_tracker_to_notion,
                    self.tasks_notion_to_tracker,
                )
                if self.tasks_tracker_to_notion and self.tasks_notion_to_tracker:
                    stats["conflicts_resolved"] += 1

                if direction == "tracker_to_notion":
                    stats["tasks_updated_from_tracker"] += 1
                    tg.create_task(self.synchronize_single_task(issue, page))
                elif direction == "notion_to_tracker":
                    stats["tasks_updated_from_notion"] += 1
                    tg.create_task(self.synchronize_single_task_to_tracker(issue, page))

            for key, issue in milestone_issues.items():
                page = notion_milestone_refs[key]
                tracker_ts = issue.updated_date or issue.created_date
                notion_ts = self._page_timestamp(page)
                both_directions = self.milestones_tracker_to_notion and self.milestones_notion_to_tracker
                direction = self._pick_direction(
                    "milestone",
                    issue,
                    page,
                    self.milestones_tracker_to_notion,
                    self.milestones_notion_to_tracker,
                )

                newer_side = "unknown"
                decision_source = "config-forced"
                if both_directions:
                    decision_source = "lww"
                    if tracker_ts and notion_ts:
                        if tracker_ts > notion_ts:
                            newer_side = "tracker"
                        elif notion_ts > tracker_ts:
                            newer_side = "notion"
                        else:
                            newer_side = "tie-fallback"
                    elif tracker_ts and not notion_ts:
                        newer_side = "tracker"
                    elif notion_ts and not tracker_ts:
                        newer_side = "notion"

                if direction in ("tracker_to_notion", "notion_to_tracker"):
                    logger.info(
                        "Updating milestone %s#%s candidate: tracker_ts=%s notion_ts=%s decision=%s newer=%s direction=%s",
                        issue.repo,
                        issue.id,
                        self._format_timestamp(tracker_ts),
                        self._format_timestamp(notion_ts),
                        decision_source,
                        newer_side,
                        direction,
                    )

                if self.milestones_tracker_to_notion and self.milestones_notion_to_tracker:
                    stats["conflicts_resolved"] += 1

                if direction == "tracker_to_notion":
                    stats["milestones_updated_from_tracker"] += 1
                    tg.create_task(self.synchronize_single_milestone_from_tracker(issue, page))
                elif direction == "notion_to_tracker":
                    stats["milestones_updated_from_notion"] += 1
                    tg.create_task(self.synchronize_single_milestone(issue, page))

        logger.info(
            "Two-way sync candidates tasks=%d milestones=%d skipped_tasks=%d skipped_milestones=%d",
            task_linked_count,
            milestone_linked_count,
            task_skipped,
            milestone_skipped,
        )
        logger.info("Two-way sync stats: %s", stats)

        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._update_timestamp(self.milestones_db, timestamp))
            tg.create_task(self._update_timestamp(self.tasks_db, timestamp))

        await self.notion.aclose()


async def synchronize(**kwargs):  # pragma: no cover
    """Exported method to begin synchronization."""
    await TrackerTwoWaySync(**kwargs).synchronize()
