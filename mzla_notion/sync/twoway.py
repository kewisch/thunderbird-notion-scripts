import asyncio
import dataclasses
import datetime
import logging

from collections import defaultdict

from notion_client.helpers import async_iterate_paginated_api

from .base import BaseSync

from ..util import ensure_date, getnestedattr
from ..tracker.common import IssueRef
from ..util import diff_dataclasses
from ..notion_data import CustomNotionToMarkdown

logger = logging.getLogger("twoway_sync")


class TrackerTwoWaySync(BaseSync):
    """Two-way tracker sync with incremental/LWW semantics."""

    def __init__(
        self,
        incremental_lookback_seconds=7 * 24 * 60 * 60,
        tasks_tracker_to_notion=True,
        tasks_notion_to_tracker=False,
        milestones_tracker_to_notion=False,
        milestones_notion_to_tracker=True,
        tasks_tracker_to_notion_create=True,
        tasks_notion_to_tracker_create=False,
        milestones_tracker_to_notion_create=False,
        milestones_notion_to_tracker_create=False,
        tasks_conflict_preference="tracker",
        milestones_conflict_preference="notion",
        full_sync=False,
        **kwargs,
    ):
        """Initialize two-way sync configuration and directional behavior."""
        super().__init__(**kwargs, logger=logging.getLogger("twoway_sync"))
        self.incremental_lookback_seconds = incremental_lookback_seconds
        self.tasks_tracker_to_notion = tasks_tracker_to_notion
        self.tasks_notion_to_tracker = tasks_notion_to_tracker
        self.milestones_tracker_to_notion = milestones_tracker_to_notion
        self.milestones_notion_to_tracker = milestones_notion_to_tracker
        self.tasks_tracker_to_notion_create = tasks_tracker_to_notion_create
        self.tasks_notion_to_tracker_create = tasks_notion_to_tracker_create
        self.milestones_tracker_to_notion_create = milestones_tracker_to_notion_create
        self.milestones_notion_to_tracker_create = milestones_notion_to_tracker_create
        self.conflict_preference = {
            "task": "notion_to_tracker" if tasks_conflict_preference == "notion" else "tracker_to_notion",
            "milestone": "notion_to_tracker" if milestones_conflict_preference == "notion" else "tracker_to_notion",
        }
        self.full_sync = full_sync
        self._task_create_cache = {}
        self._unlinked_notion_tasks = []
        self._task_discovery_since = None
        self._milestones_notion_to_tracker_create_unsupported = False

        if self.milestones_notion_to_tracker_create:
            self.logger.warning("milestones_notion_to_tracker_create is not supported in v2; skipping")
            self.milestones_notion_to_tracker_create = False
            self._milestones_notion_to_tracker_create_unsupported = True

    async def _async_init(self):
        use_single_task_query = self.tasks_notion_to_tracker and self.tasks_notion_to_tracker_create
        if not use_single_task_query:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(super()._async_init())
                milestones_issues = tg.create_task(
                    self._discover_notion_issues(
                        self.milestones_db.database_id, self.propnames["notion_milestones_team"]
                    )
                )

            self._notion_milestone_issues = milestones_issues.result()
            self._unlinked_notion_tasks = []
            return

        async with asyncio.TaskGroup() as tg:
            valid_milestones = tg.create_task(self.milestones_db.validate_props())
            valid_tasks = tg.create_task(self.tasks_db.validate_props())
            tasks_partitioned = tg.create_task(self._discover_notion_tasks_partitioned(self._task_discovery_since))
            milestones_issues = tg.create_task(
                self._discover_notion_issues(self.milestones_db.database_id, self.propnames["notion_milestones_team"])
            )

            if self.sprint_db:
                sprint_pages = tg.create_task(self.sprint_db.get_all_pages())

        if not valid_milestones.result():
            raise Exception("Milestone schema failed to validate")
        if not valid_tasks.result():
            raise Exception("Tasks schema failed to validate")

        linked, unlinked = tasks_partitioned.result()
        self._notion_tasks_issues = linked
        self._unlinked_notion_tasks = unlinked
        self._notion_milestone_issues = milestones_issues.result()

        if self.sprint_db:
            self._all_sprint_pages = sprint_pages.result()

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
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")).replace(second=0, microsecond=0)

    def _format_timestamp(self, value):
        if not value:
            return "None"
        return value.isoformat()

    def _is_closed_status(self, status_name):
        return bool(status_name and status_name in self.propnames["notion_closed_states"])

    def _is_task_issue(self, tracker_issue):
        return self.tracker.is_task_issue(
            tracker_issue,
            milestones_issue_type=self.milestones_issue_type,
            epics_issue_type=self.epics_issue_type,
        )

    def _is_milestone_issue(self, tracker_issue):
        milestone_issue_type = self.milestones_issue_type or getattr(self.tracker, "milestones_issue_type", None)
        if milestone_issue_type:
            return tracker_issue.issue_type == milestone_issue_type

        # Generic parent issues fallback
        if tracker_issue.parents:
            return False
        return bool(tracker_issue.sub_issues) or tracker_issue.title.startswith("[meta]")

    def _pick_direction(self, entity_kind, tracker_issue, notion_page, tracker_to_notion, notion_to_tracker):
        if tracker_to_notion and not notion_to_tracker:
            return "tracker_to_notion"
        if notion_to_tracker and not tracker_to_notion:
            return "notion_to_tracker"
        if not tracker_to_notion and not notion_to_tracker:
            return None

        tracker_ts = tracker_issue.updated_date or tracker_issue.created_date
        if tracker_ts:
            tracker_ts = tracker_ts.replace(second=0, microsecond=0)
        notion_ts = self._page_timestamp(notion_page)
        fallback = self.conflict_preference[entity_kind]

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

    async def synchronize_single_milestone_from_tracker(self, tracker_issue, page, candidate_debug=None):
        """Apply tracker milestone fields onto the linked Notion milestone page."""
        notion_data = self._get_milestone_notion_data_from_tracker(tracker_issue)
        changed = self.milestones_db.page_diff(notion_data, page, log=False)
        if changed:
            self.logger.info(f"Updating milestone (Tracker->Notion) {page.get('url')} - {tracker_issue.title}")
            if candidate_debug:
                self.logger.debug(f"  candidate: {candidate_debug}")
            self.logger.debug("  notion changes:")
            self.milestones_db.page_diff(notion_data, page, log=self.logger.isEnabledFor(logging.DEBUG))
            self.logger.debug("\t" + str(notion_data))
            await self.milestones_db.update_page(page, notion_data, diff_log=False)
        else:
            self.logger.info(f"Unchanged milestone {tracker_issue.repo}#{tracker_issue.id} - {tracker_issue.title}")
        return changed

    async def synchronize_single_milestone(self, tracker_issue, page, skip_unchanged_msg=False, candidate_debug=None):
        """Synchronize a single Notion milestone to the issue tracker."""
        old_issue_url = self._get_prop(page, "notion_issue_field")
        if old_issue_url and old_issue_url != tracker_issue.url:
            self.logger.warning(
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

        needs_update = self.tracker.should_update_milestone_issue(tracker_issue, new_issue)

        if needs_update:
            self.logger.info(f"Updating milestone (Notion->Tracker) {tracker_issue.url} - {new_issue.title}")
            if candidate_debug:
                self.logger.debug(f"  candidate: {candidate_debug}")
            diff_dataclasses(tracker_issue, new_issue, log=self.logger.debug)

            if not self.dry:
                await self.tracker.update_milestone_issue(tracker_issue, new_issue)
            return True
        elif not skip_unchanged_msg:
            self.logger.info(
                f"Unchanged milestone {tracker_issue.id} - {tracker_issue.title} ({tracker_issue.url} / {new_issue.notion_url})"
            )
        return False

    def _get_task_tracker_issue_from_notion(self, tracker_issue, page):
        title = self._get_richtext_prop(page, "notion_tasks_title", tracker_issue.title)
        status = (self._get_prop(page, "notion_tasks_status") or {}).get("name")
        priority = (self._get_prop(page, "notion_tasks_priority") or {}).get("name")
        estimate = tracker_issue.estimate
        if self.propnames.get("notion_tasks_estimate"):
            estimate = (self._get_prop(page, "notion_tasks_estimate") or {}).get("name")

        assignees = {
            self.tracker.new_user(notion_user=assignee["id"])
            for assignee in self._get_prop(page, "notion_tasks_assignee", [])
        }

        return dataclasses.replace(
            tracker_issue,
            title=title or tracker_issue.title,
            state=status or tracker_issue.state,
            priority=priority or tracker_issue.priority,
            estimate=estimate,
            assignees=assignees if assignees else tracker_issue.assignees,
            notion_url=page.get("url", tracker_issue.notion_url),
        )

    async def _task_needs_notion_update(self, tracker_issue, page):
        parent_pages = self._find_task_parents(tracker_issue) or []
        notion_data = await self._get_task_notion_data(
            tracker_issue=tracker_issue,
            parent_milestone_pages=parent_pages,
            old_page=page,
        )
        return self.tasks_db.page_diff(notion_data, page, log=False)

    def _task_needs_tracker_update(self, tracker_issue, page):
        new_issue = self._get_task_tracker_issue_from_notion(tracker_issue, page)
        return self.tracker.should_update_task_issue(tracker_issue, new_issue)

    async def synchronize_single_task_to_tracker(self, tracker_issue, page, candidate_debug=None):
        """Apply Notion task fields onto the linked tracker task issue."""
        new_issue = self._get_task_tracker_issue_from_notion(tracker_issue, page)
        if self._task_needs_tracker_update(tracker_issue, page):
            self.logger.info(f"Updating task (Notion->Tracker) {tracker_issue.url} - {new_issue.title}")
            if candidate_debug:
                self.logger.debug(f"  timestamps: {candidate_debug}")
            if not self.dry:
                await self.tracker.update_task_issue(tracker_issue, new_issue)
            return True
        else:
            self.logger.info(f"Unchanged task {tracker_issue.repo}#{tracker_issue.id}")
            if candidate_debug:
                self.logger.debug(f"  timestamps: {candidate_debug}")
        return False

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

    def _filter_task_issues_by_repo(self, issues_by_repo):
        task_issues = defaultdict(dict)
        skipped = 0

        for repo, issues in issues_by_repo.items():
            for issue_id, issue in issues.items():
                if self._is_task_issue(issue):
                    task_issues[repo][issue_id] = issue
                else:
                    skipped += 1

        if skipped:
            self.logger.debug("Filtered %d recent tracker issues from task processing", skipped)

        return task_issues

    def _filter_task_issues(self, issues):
        return {key: issue for key, issue in issues.items() if self._is_task_issue(issue)}

    async def _discover_unlinked_notion_tasks(self, since):
        pages = []
        issue_filter = {
            "property": self.propnames["notion_issue_field"],
            "files": {"is_empty": True},
        }
        query_filter = issue_filter

        task_team_prop = self.propnames.get("notion_tasks_team")
        if task_team_prop and self.configured_team_ids:
            team_filter = {
                "or": [
                    {"property": task_team_prop, "relation": {"contains": team}} for team in self.configured_team_ids
                ]
            }
            query_filter = {"and": [team_filter, issue_filter]}

        async for page in async_iterate_paginated_api(
            self.notion.databases.query,
            database_id=self.tasks_db.database_id,
            filter=query_filter,
        ):
            if self._get_prop(page, "notion_issue_field", []):
                continue

            if not self.full_sync:
                edited = self._page_timestamp(page)
                if edited is None or edited < since:
                    continue

            status = getnestedattr(
                lambda: self._get_prop(page, "notion_tasks_status")["name"],
                None,
            )
            if self._is_closed_status(status):
                continue

            if task_team_prop and self.configured_team_ids:
                page_teams = set(self._get_relation_ids(page, "notion_tasks_team"))
                if not page_teams.intersection(self.configured_team_ids):
                    continue

            pages.append(page)

        return pages

    async def _discover_notion_tasks_partitioned(self, since):
        linked_repos = defaultdict(dict)
        unlinked_pages = []

        query_filter = None
        task_team_prop = self.propnames.get("notion_tasks_team")
        if task_team_prop and self.configured_team_ids:
            query_filter = {
                "or": [
                    {"property": task_team_prop, "relation": {"contains": team}} for team in self.configured_team_ids
                ]
            }

        async for page in async_iterate_paginated_api(
            self.notion.databases.query,
            database_id=self.tasks_db.database_id,
            filter=query_filter,
        ):
            if task_team_prop and self.configured_team_ids:
                page_teams = set(self._get_relation_ids(page, "notion_tasks_team"))
                if not page_teams.intersection(self.configured_team_ids):
                    continue

            issue_files = self._get_prop(page, "notion_issue_field", []) or []
            if issue_files:
                issue_url = getnestedattr(lambda: issue_files[0]["external"]["url"], None)
                if issue_url:
                    ref = self.tracker.parse_issueref(issue_url)
                    if ref and self.tracker.is_repo_allowed(ref.repo):
                        linked_repos[ref.repo][ref.id] = page
                continue

            if not self.full_sync:
                edited = self._page_timestamp(page)
                if edited is None or edited < since:
                    continue

            status = getnestedattr(
                lambda: self._get_prop(page, "notion_tasks_status")["name"],
                None,
            )
            if self._is_closed_status(status):
                continue

            unlinked_pages.append(page)

        return linked_repos, unlinked_pages

    async def _create_milestone_in_notion_from_tracker(self, tracker_issue):
        notion_data = self._get_milestone_notion_data_from_tracker(tracker_issue)
        self._set_if_prop(notion_data, "notion_milestones_team", self.configured_team_ids or None)
        page = await self.milestones_db.create_page(notion_data)
        return page

    async def _link_task_page_to_issue(self, page, tracker_issue):
        notion_data = {
            self.propnames["notion_issue_field"]: [
                {"url": tracker_issue.url, "name": self.tracker.format_issueref_short(tracker_issue)}
            ]
        }
        return await self.tasks_db.update_page(page, notion_data)

    async def _get_tracker_milestones_for_create(self, since):
        milestones = {}

        if self.milestones_issue_type:
            async for milestone in self.tracker.collect_tracker_milestones(self.milestones_issue_type, sub_issues=True):
                if not self.full_sync and milestone.updated_date and milestone.updated_date < since:
                    continue
                milestones[(milestone.repo, milestone.id)] = milestone
            return milestones

        recent = await self.tracker.get_recent_issues_by_repo(since, sub_issues=False)
        for repo, issues in recent.items():
            for issue_id, issue in issues.items():
                if self._is_milestone_issue(issue):
                    milestones[(repo, issue_id)] = issue

        return milestones

    def _new_stats(self):
        return {
            "tasks_updated_from_tracker": 0,
            "tasks_updated_from_notion": 0,
            "milestones_updated_from_tracker": 0,
            "milestones_updated_from_notion": 0,
            "tasks_created_from_tracker": 0,
            "tasks_created_from_notion": 0,
            "milestones_created_from_tracker": 0,
            "tasks_create_skipped_no_parent": 0,
            "tasks_create_skipped_closed": 0,
            "tasks_create_skipped_unsupported_path": 0,
            "tasks_create_link_back_retry": 0,
            "milestones_create_skipped_unsupported_path": 1
            if self._milestones_notion_to_tracker_create_unsupported
            else 0,
        }

    async def _add_tracker_create_candidates(
        self, since, recent_tasks_by_repo, notion_task_refs, notion_milestone_refs, task_issues, milestone_issues
    ):
        if self.tasks_tracker_to_notion and self.tasks_tracker_to_notion_create:
            for repo, issues in recent_tasks_by_repo.items():
                for issue_id, issue in issues.items():
                    key = (repo, issue_id)
                    if key in notion_task_refs:
                        continue
                    if self._is_task_issue(issue):
                        task_issues[key] = issue

        if self.milestones_tracker_to_notion and self.milestones_tracker_to_notion_create:
            for key, issue in (await self._get_tracker_milestones_for_create(since)).items():
                if key in notion_milestone_refs:
                    continue
                milestone_issues[key] = issue
        elif self.milestones_tracker_to_notion:
            skipped_milestones = [
                (repo, issue_id)
                for repo, issues in recent_tasks_by_repo.items()
                for issue_id, issue in issues.items()
                if (repo, issue_id) not in notion_milestone_refs and self._is_milestone_issue(issue)
            ]
            if skipped_milestones:
                self.logger.info(
                    "Skipping %d tracker->Notion milestone create candidates because milestones_tracker_to_notion_create is disabled",
                    len(skipped_milestones),
                )

    def _candidate_debug(self, entity_kind, issue, page, tracker_to_notion, notion_to_tracker):
        tracker_ts = issue.updated_date or issue.created_date
        if tracker_ts:
            tracker_ts = tracker_ts.replace(second=0, microsecond=0)
        notion_ts = self._page_timestamp(page)
        direction = self._pick_direction(
            entity_kind,
            issue,
            page,
            tracker_to_notion,
            notion_to_tracker,
        )

        if not tracker_to_notion and not notion_to_tracker:
            reason = "No sync direction configured"
        elif not (tracker_to_notion and notion_to_tracker):
            reason = "One-way sync configured"
        elif tracker_ts and notion_ts:
            if tracker_ts > notion_ts:
                reason = "Tracker is more recent"
            elif notion_ts > tracker_ts:
                reason = "Notion is more recent"
            else:
                reason = "Tie break configured"
        elif tracker_ts:
            reason = "Only tracker timestamp available"
        elif notion_ts:
            reason = "Only Notion timestamp available"
        else:
            reason = "No timestamps available"

        action = {
            "tracker_to_notion": "Tracker->Notion",
            "notion_to_tracker": "Notion->Tracker",
        }.get(direction, "No sync direction")

        debug = (
            f"tracker_ts={self._format_timestamp(tracker_ts)} "
            f"notion_ts={self._format_timestamp(notion_ts)} "
            f"- {action} - {reason}"
        )
        return direction, debug

    async def _run_milestone_phase(self, milestone_issues, notion_milestone_refs, milestone_page_by_id, stats):
        stat_tasks = []
        async with asyncio.TaskGroup() as tg:
            for key, issue in milestone_issues.items():
                page = notion_milestone_refs.get(key)
                if page is None:
                    if self.milestones_tracker_to_notion and self.milestones_tracker_to_notion_create:
                        if self._is_milestone_issue(issue):
                            if issue.closed_date or self._is_closed_status(issue.state):
                                stats["milestones_create_skipped_unsupported_path"] += 1
                                continue
                            page = await self._create_milestone_in_notion_from_tracker(issue)
                            if not page:
                                continue
                            notion_milestone_refs[key] = page
                            self._notion_milestone_issues.setdefault(issue.repo, {})[issue.id] = page
                            milestone_page_by_id[page["id"].replace("-", "")] = (issue.repo, issue.id, page)
                            stats["milestones_created_from_tracker"] += 1
                            if "properties" in page:
                                await self.synchronize_single_milestone_from_tracker(issue, page)
                        else:
                            stats["milestones_create_skipped_unsupported_path"] += 1
                    continue

                direction, candidate_debug = self._candidate_debug(
                    "milestone",
                    issue,
                    page,
                    self.milestones_tracker_to_notion,
                    self.milestones_notion_to_tracker,
                )

                if direction == "tracker_to_notion":
                    stat_tasks.append(
                        (
                            "milestones_updated_from_tracker",
                            tg.create_task(
                                self.synchronize_single_milestone_from_tracker(issue, page, candidate_debug)
                            ),
                        )
                    )
                elif direction == "notion_to_tracker":
                    stat_tasks.append(
                        (
                            "milestones_updated_from_notion",
                            tg.create_task(
                                self.synchronize_single_milestone(issue, page, candidate_debug=candidate_debug)
                            ),
                        )
                    )

        for stat_key, task in stat_tasks:
            if task.result():
                stats[stat_key] += 1

    async def _create_tracker_task_from_notion(self, page, milestone_page_by_id, milestone_issues, stats):
        status = getnestedattr(lambda: self._get_prop(page, "notion_tasks_status")["name"], None)
        if self._is_closed_status(status):
            stats["tasks_create_skipped_closed"] += 1
            return

        page_id = page["id"]
        relation = self._get_prop(page, "notion_tasks_milestone_relation", [])
        parent_rel_id = relation[0]["id"].replace("-", "") if relation else None
        parent_info = milestone_page_by_id.get(parent_rel_id)
        if not parent_info:
            stats["tasks_create_skipped_no_parent"] += 1
            return

        milestone_repo, milestone_issue_id, milestone_page = parent_info
        parent_issue = milestone_issues.get((milestone_repo, milestone_issue_id))
        if not parent_issue:
            parent_ref = self.tracker.parse_issueref(self._get_prop(milestone_page, "notion_issue_field", ""))
            if not parent_ref:
                stats["tasks_create_skipped_no_parent"] += 1
                return
            async for fetched_parent in self.tracker.get_issues_by_number([parent_ref], sub_issues=False):
                parent_issue = fetched_parent
                break

        if not parent_issue:
            stats["tasks_create_skipped_no_parent"] += 1
            return

        title = self._get_richtext_prop(page, "notion_tasks_title", "Untitled Task")
        estimate = None
        if self.propnames.get("notion_tasks_estimate"):
            estimate = (self._get_prop(page, "notion_tasks_estimate") or {}).get("name")
        assignees = {
            self.tracker.new_user(notion_user=user["id"]) for user in self._get_prop(page, "notion_tasks_assignee", [])
        }

        created_issue = self._task_create_cache.get(page_id)
        if not created_issue:
            try:
                created_issue = await self.tracker.create_task_issue_from_notion(
                    parent_issue=parent_issue,
                    title=title,
                    description="",
                    assignees=assignees,
                    labels=None,
                    estimate=estimate,
                )
                if created_issue:
                    self._task_create_cache[page_id] = created_issue
                    stats["tasks_created_from_notion"] += 1
                else:
                    self.logger.info(
                        "Skipping dependent Notion link/update for %s because tracker create produced no issue",
                        page.get("url"),
                    )
                    return
            except NotImplementedError as exc:
                stats["tasks_create_skipped_unsupported_path"] += 1
                self.logger.warning(f"Skipping notion->tracker task create for {page.get('url')}: {exc}")
                return

        try:
            await self._link_task_page_to_issue(page, created_issue)
        except Exception:
            stats["tasks_create_link_back_retry"] += 1
            self.logger.warning(
                "Task create link-back failed for %s, will retry link update in same run before re-creating",
                page.get("url"),
            )
            return

        direction = self._pick_direction(
            "task",
            created_issue,
            page,
            self.tasks_tracker_to_notion,
            self.tasks_notion_to_tracker,
        )
        if direction == "tracker_to_notion":
            if await self._task_needs_notion_update(created_issue, page):
                await self.synchronize_single_task(created_issue, page)
        elif direction == "notion_to_tracker":
            if self._task_needs_tracker_update(created_issue, page):
                await self.synchronize_single_task_to_tracker(created_issue, page)

    async def _run_task_phase(
        self, since, task_issues, notion_task_refs, milestone_page_by_id, milestone_issues, stats
    ):
        stat_tasks = []
        async with asyncio.TaskGroup() as tg:
            for key, issue in task_issues.items():
                if not self._is_task_issue(issue):
                    self.logger.debug(
                        "Skipping task sync %s#%s because it is not a relevant task",
                        issue.repo,
                        issue.id,
                    )
                    continue

                page = notion_task_refs.get(key)
                if page is None:
                    if self.tasks_tracker_to_notion and self.tasks_tracker_to_notion_create:
                        stat_tasks.append(
                            ("tasks_created_from_tracker", tg.create_task(self.synchronize_single_task(issue, None)))
                        )
                    continue

                direction, candidate_debug = self._candidate_debug(
                    "task",
                    issue,
                    page,
                    self.tasks_tracker_to_notion,
                    self.tasks_notion_to_tracker,
                )
                if direction == "tracker_to_notion":
                    stat_tasks.append(
                        (
                            "tasks_updated_from_tracker",
                            tg.create_task(self.synchronize_single_task(issue, page, candidate_debug=candidate_debug)),
                        )
                    )
                elif direction == "notion_to_tracker":
                    stat_tasks.append(
                        (
                            "tasks_updated_from_notion",
                            tg.create_task(
                                self.synchronize_single_task_to_tracker(issue, page, candidate_debug=candidate_debug)
                            ),
                        )
                    )

            if self.tasks_notion_to_tracker and self.tasks_notion_to_tracker_create:
                unlinked_notion_tasks = self._unlinked_notion_tasks
                for page in unlinked_notion_tasks:
                    tg.create_task(
                        self._create_tracker_task_from_notion(page, milestone_page_by_id, milestone_issues, stats)
                    )

        for stat_key, task in stat_tasks:
            if task.result():
                stats[stat_key] += 1

    def _log_sync_stats(self, task_linked_count, milestone_linked_count, task_skipped, milestone_skipped, stats):
        self.logger.info("Two-way sync stats %-22s %8s %10s", "", "tasks", "milestones")
        self.logger.info(
            "Two-way sync stats %-22s %8d %10d",
            "linked",
            task_linked_count,
            milestone_linked_count,
        )
        self.logger.info(
            "Two-way sync stats %-22s %8d %10d",
            "skipped",
            task_skipped,
            milestone_skipped,
        )
        self.logger.info(
            "Two-way sync stats %-22s %8d %10d",
            "updated from tracker",
            stats["tasks_updated_from_tracker"],
            stats["milestones_updated_from_tracker"],
        )
        self.logger.info(
            "Two-way sync stats %-22s %8d %10d",
            "updated from notion",
            stats["tasks_updated_from_notion"],
            stats["milestones_updated_from_notion"],
        )
        self.logger.info(
            "Two-way sync stats %-22s %8d %10d",
            "created from tracker",
            stats["tasks_created_from_tracker"],
            stats["milestones_created_from_tracker"],
        )
        self.logger.info(
            "Two-way sync stats %-22s %8d %10s",
            "created from notion",
            stats["tasks_created_from_notion"],
            "-",
        )
        self.logger.info(
            "Two-way sync stats task create skipped no_parent=%5d unsupported=%5d link_back_retry=%5d",
            stats["tasks_create_skipped_no_parent"],
            stats["tasks_create_skipped_unsupported_path"],
            stats["tasks_create_link_back_retry"],
        )

    async def synchronize(self):
        """Run a complete incremental two-way synchronization cycle."""
        timestamp = datetime.datetime.now(datetime.UTC)
        since = timestamp - datetime.timedelta(seconds=self.incremental_lookback_seconds)
        self._task_discovery_since = since
        self.logger.info(
            "Two-way sync window full_sync=%s lookback_seconds=%d since=%s",
            self.full_sync,
            self.incremental_lookback_seconds,
            self._format_timestamp(since),
        )
        self.logger.debug(
            "Two-way sync directions tasks tracker->notion=%s notion->tracker=%s create tracker->notion=%s notion->tracker=%s",
            self.tasks_tracker_to_notion,
            self.tasks_notion_to_tracker,
            self.tasks_tracker_to_notion_create,
            self.tasks_notion_to_tracker_create,
        )
        self.logger.debug(
            "Two-way sync directions milestones tracker->notion=%s notion->tracker=%s create tracker->notion=%s notion->tracker=%s",
            self.milestones_tracker_to_notion,
            self.milestones_notion_to_tracker,
            self.milestones_tracker_to_notion_create,
            self.milestones_notion_to_tracker_create,
        )

        await self._async_init()

        recent_tasks_by_repo = {}
        recent_milestones_by_repo = {}
        fetch_recent = (
            self.tasks_tracker_to_notion
            or self.tasks_notion_to_tracker
            or self.milestones_tracker_to_notion
            or self.milestones_notion_to_tracker
            or self.tasks_tracker_to_notion_create
            or self.milestones_tracker_to_notion_create
        )
        if fetch_recent:
            fetch_since = since if not self.full_sync else datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC)
            recent_issues_by_repo = await self.tracker.get_recent_issues_by_repo(fetch_since, sub_issues=False)
            recent_tasks_by_repo = self._filter_task_issues_by_repo(recent_issues_by_repo)
            recent_milestones_by_repo = recent_issues_by_repo
        else:
            self.logger.info(
                "Two-way sync not fetching recent tracker issues because all tracker directions are disabled"
            )

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
        recent_tracker_task_count = sum(
            1 for issues in recent_tasks_by_repo.values() for issue in issues.values() if self._is_task_issue(issue)
        )
        self.logger.info(
            "Two-way sync discovered linked Notion refs tasks=%d milestones=%d recent_tracker_refs=%d recent_tracker_tasks=%d",
            len(notion_task_refs),
            len(notion_milestone_refs),
            len(recent_task_keys),
            recent_tracker_task_count,
        )

        task_candidates, task_linked_count, task_skipped = self._collect_candidates(
            notion_task_refs, recent_task_keys, since
        )
        milestone_candidates, milestone_linked_count, milestone_skipped = self._collect_candidates(
            notion_milestone_refs, recent_milestone_keys, since
        )

        task_issues = await self._load_tracker_candidates(task_candidates, recent_tasks_by_repo)
        task_issues = self._filter_task_issues(task_issues)
        milestone_issues = await self._load_tracker_candidates(milestone_candidates, recent_milestones_by_repo)
        self.logger.info(
            "Two-way sync loaded tracker candidates tasks=%d milestones=%d",
            len(task_issues),
            len(milestone_issues),
        )

        stats = self._new_stats()

        milestone_page_by_id = {
            page["id"].replace("-", ""): (repo, issue_id, page)
            for (repo, issue_id), page in notion_milestone_refs.items()
        }

        await self._add_tracker_create_candidates(
            since, recent_tasks_by_repo, notion_task_refs, notion_milestone_refs, task_issues, milestone_issues
        )
        await self._run_milestone_phase(milestone_issues, notion_milestone_refs, milestone_page_by_id, stats)
        await self._run_task_phase(since, task_issues, notion_task_refs, milestone_page_by_id, milestone_issues, stats)
        self._log_sync_stats(task_linked_count, milestone_linked_count, task_skipped, milestone_skipped, stats)
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._update_timestamp(self.milestones_db, timestamp))
            tg.create_task(self._update_timestamp(self.tasks_db, timestamp))

        await self.notion.aclose()


async def synchronize(**kwargs):  # pragma: no cover
    """Exported method to begin synchronization."""
    await TrackerTwoWaySync(**kwargs).synchronize()
