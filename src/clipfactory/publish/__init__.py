"""Publishing module (module: publish).

Turns a rendered clip + `PostMetadata` into a live post on a platform.
`get_publisher()` is the single entry point the pipeline uses; concrete
publishers live in `local.py`, `youtube.py`, `instagram.py`, `tiktok.py`.

See docs/PUBLISHERS.md for how to obtain credentials for each platform.
"""

from __future__ import annotations

from clipfactory.publish.base import Publisher, PublishError, get_publisher

__all__ = ["Publisher", "PublishError", "get_publisher"]
