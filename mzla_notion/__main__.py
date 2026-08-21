# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.

import asyncio

from mzla_notion.cli import async_main


def main():
    """Run the mzla-notion command line interface."""
    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
