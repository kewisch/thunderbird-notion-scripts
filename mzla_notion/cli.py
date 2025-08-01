# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import argparse
import logging
import os
import sys
import tomllib

from .sync.label import synchronize as synchronize_gh_label
from .sync.project import synchronize as synchronize_project
from .sync.board import synchronize as synchronize_board
from .tracker.github import GitHub
from .tracker.bugzilla import Bugzilla

logger = logging.getLogger("notion_sync")


def cmd_list_synchronizers(config):
    """Just list synchronizers."""
    with open(config, "rb") as fp:
        settings = tomllib.load(fp)

    enabled = [key for key, project in settings["sync"].items() if project.get("enabled", True)]
    print("\n".join(enabled))


def cmd_synchronize(projects, config, verbose=0, user_map_file=None, dry_run=False, synchronous=False):
    """This is the main cli. Please use --help on how to use it."""
    logging.basicConfig(
        format="%(levelname)s [%(asctime)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    with open(config, "rb") as fp:
        settings = tomllib.load(fp)

    if user_map_file and os.path.isfile(user_map_file):
        with open(user_map_file, "rb") as fp:
            user_map = tomllib.load(fp)
    else:
        user_map = {
            "bugzilla": tomllib.loads(os.environ.get("NOTION_SYNC_BUGZILLA_USERMAP", "")),
            "github": tomllib.loads(os.environ.get("NOTION_SYNC_GITHUB_USERMAP", "")),
        }

    httpx_log_level = (
        [logging.WARNING, logging.INFO, logging.INFO, logging.DEBUG][verbose] if verbose <= 3 else logging.DEBUG
    )
    sync_log_level = [logging.INFO, logging.INFO, logging.DEBUG][verbose] if verbose <= 2 else logging.DEBUG

    logging.getLogger("httpx").setLevel(httpx_log_level)
    logging.getLogger("httpcore").setLevel(httpx_log_level)
    logging.getLogger("sgqlc.endpoint.http").setLevel(httpx_log_level)

    logging.getLogger("project_sync").setLevel(sync_log_level)
    logging.getLogger("board_sync").setLevel(sync_log_level)
    logging.getLogger("gh_label_sync").setLevel(sync_log_level)
    logging.getLogger("bugzilla_sync").setLevel(sync_log_level)
    logging.getLogger("notion_sync").setLevel(sync_log_level)
    logging.getLogger("notion_database").setLevel(sync_log_level)

    # This will list the GitHub project ids for you
    # import libs.ghhelper
    # libs.ghhelper.GitHubProjectV2.list("thunderbird", "thunderbird-android")
    # sys.exit()

    # This will give you a list of users and their ids
    # from notion_client import Client
    # from pprint import pprint
    # notion = Client(auth=os.environ["NOTION_TOKEN"])
    # pprint(notion.users.list())
    # sys.exit()

    # This will give you the properties
    # from pprint import pprint
    # from notion_client import Client
    # notion = Client(auth=os.environ["NOTION_TOKEN"])
    # pprint(notion.databases.retrieve(database_id="DB_ID_HERE"))
    # pprint(notion.pages.retrieve("PAGE_ID_HERE"))
    # sys.exit()

    if settings.get("dry", False):
        if dry_run is None or dry_run is True:
            logger.info("Forcing a dry run via configuration, no changes will be made")
            dry_run = True
        elif dry_run is False:
            logger.info("Ignoring dry run from configuration due to --no-dry-run")
            dry_run = False
    elif dry_run:
        logger.info("Doing a dry run, no changes will be made")

    if not projects:
        projects = settings["sync"].keys()

    for key in projects:
        if key not in settings["sync"]:
            logger.error(f"Error: Could not find project {key}")
            return 1

        project = settings["sync"][key]

        if not project.get("enabled", True):
            if verbose > 0:
                logger.warning(f"Skipping project {key} because it is disabled")
            continue

        logger.info(f"Synchronizing project {key}...")

        if project["method"].endswith("_project"):
            if project["method"] == "bugzilla_project":
                tracker = Bugzilla(
                    base_url=project["bugzilla_base"],
                    token=os.environ["BUGZILLA_TOKEN"],
                    dry=dry_run or project.get("tracker_dry_run", False),
                    user_map=user_map.get("bugzilla") or {},
                    property_names=project.get("properties", {}),
                )
            elif project["method"] == "github_project":
                tracker = GitHub(
                    token=os.environ["GITHUB_TOKEN"],
                    repositories=project["repositories"],
                    dry=dry_run or project.get("tracker_dry_run", False),
                    user_map=user_map.get("github") or {},
                    property_names=project.get("properties", {}),
                )

            else:
                raise Exception(f"Unknown synchronization {project['method']}")

            synchronize_project(
                project_key=key,
                tracker=tracker,
                notion_token=os.environ["NOTION_TOKEN"],
                milestones_id=project["notion_milestones_id"],
                tasks_id=project["notion_tasks_id"],
                sprint_id=project.get("notion_sprints_id", None),
                milestones_body_sync=project.get("milestones_body_sync", False),
                milestones_body_sync_if_empty=project.get("milestones_body_sync_if_empty", False),
                tasks_body_sync=project.get("tasks_body_sync", False),
                milestones_tracker_prefix=project.get("milestones_tracker_prefix", ""),
                milestones_extra_label=project.get("milestones_extra_label", ""),
                tasks_notion_prefix=project.get("tasks_notion_prefix", ""),
                sprints_merge_by_name=project.get("sprints_merge_by_name", False),
                dry=dry_run,
                synchronous=synchronous,
            )
        elif project["method"] == "github_labels":
            tracker = GitHub(
                token=os.environ["GITHUB_TOKEN"],
                repositories=project["repositories"],
                dry=dry_run or project.get("tracker_dry_run", False),
                user_map=user_map.get("github") or {},
                property_names=project.get("properties", {}),
            )
            synchronize_gh_label(
                project_key=key,
                tracker=tracker,
                notion_token=os.environ["NOTION_TOKEN"],
                milestones_id=project["notion_milestones_id"],
                tasks_id=project["notion_tasks_id"],
                sprint_id=project.get("notion_sprints_id", None),
                milestones_body_sync=project.get("milestones_body_sync", False),
                milestones_body_sync_if_empty=project.get("milestones_body_sync_if_empty", False),
                tasks_body_sync=project.get("tasks_body_sync", False),
                milestones_tracker_prefix=project.get("milestones_tracker_prefix", ""),
                milestones_extra_label=project.get("milestones_extra_label", ""),
                tasks_notion_prefix=project.get("tasks_notion_prefix", ""),
                sprints_merge_by_name=project.get("sprints_merge_by_name", False),
                milestone_label_prefix=project.get("milestone_label_prefix", "M: "),
                dry=dry_run,
                synchronous=synchronous,
            )
        elif project["method"] == "project_board":
            synchronize_board(
                project_key=key,
                notion_token=os.environ["NOTION_TOKEN"],
                board_id=project["notion_board_id"],
                properties=project.get("properties", {}),
                dry=dry_run,
                synchronous=synchronous,
            )
        else:
            raise Exception(f"Unknown synchronization {project['method']}")

        logger.info(f"Synchronizing project {key} completed")

    return 0


def main():
    """Main mzla-notion program."""
    parser = argparse.ArgumentParser(description="Notion Synchronization for MZLA")
    parser.add_argument(
        "-c",
        "--config",
        default="sync_settings.toml",
        help="Use a different config file, defaults to sync_settings.toml.",
    )
    parser.add_argument(
        "-u",
        "--usermap",
        default="sync_usermap.toml",
        help="The usermap file to use if not specified via environment.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Enable verbose logging. Use multiple times for more.",
    )
    parser.add_argument(
        "--synchronous",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Run requests in order, for debugging",
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Run the script without making changes",
    )
    parser.add_argument("-l", "--list", action="store_true", help="List synchronizers and exit")
    parser.add_argument(
        "projects",
        nargs="*",
        default=None,
        help="The keys of the projects to synchronize. Defaults to all projects.",
    )

    args = parser.parse_args()

    if args.list:
        cmd_list_synchronizers(args.config)
    else:
        sys.exit(
            cmd_synchronize(
                args.projects,
                config=args.config,
                verbose=args.verbose,
                user_map_file=args.usermap,
                dry_run=args.dry_run,
                synchronous=args.synchronous,
            )
        )
