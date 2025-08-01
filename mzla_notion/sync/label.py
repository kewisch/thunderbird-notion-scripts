# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import logging
import datetime

from functools import cached_property

from .base import BaseSync

logger = logging.getLogger("gh_label_sync")


class LabelSync(BaseSync):
    """This is a label-based sync between Notion and an GitHub.

    All tasks in the associated repositories are synchronized to notion. The relation to the
    milestones is done using GitHub labels and a prefix. The label `M: milestone name` will link the
    task to the milestone `milestone name`.
    """

    def __init__(self, milestone_label_prefix="", **kwargs):
        """Initialize label sync."""
        super().__init__(**kwargs)
        self.milestone_label_prefix = milestone_label_prefix

    @cached_property
    def _all_milestone_pages(self):
        return self.milestones_db.get_all_pages()

    @cached_property
    def _milestone_pages_by_title(self):
        return {
            content: page
            for page in self._all_milestone_pages
            if (content := self._get_richtext_prop(page, "notion_milestones_title"))
        }

    def _find_task_parents(self, issue):
        parent_ids = []
        milestone_pages = self._milestone_pages_by_title

        for label in issue.labels:
            if label.startswith(self.milestone_label_prefix):
                clean_label = label[len(self.milestone_label_prefix) :].strip()
                if page := milestone_pages.get(clean_label):
                    parent_ids.append(page["id"])

        return parent_ids

    def synchronize(self):
        """Synchronize all the issues!"""
        timestamp = datetime.datetime.now(datetime.UTC)

        tracker_issues = self.tracker.get_all_issues()
        tasks_issues = self._notion_tasks_issues

        # Synchronize all issues into the tasks db
        for reporef, issues in tracker_issues.items():
            for issue in issues:
                self.synchronize_single_task(issue, tasks_issues.get(issue.repo, {}).get(issue.id))

        # Update the description with the last updated timestamp
        self._update_timestamp(self.tasks_db, timestamp)


def synchronize(**kwargs):  # pragma: no cover
    """Exported method to begin synchronization."""
    LabelSync(**kwargs).synchronize()
