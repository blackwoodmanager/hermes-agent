#!/usr/bin/env python3
"""Send one unfinished-task review per live gateway boot."""
from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

path = Path(__file__).with_name("unfinished_task_review.py")
spec = importlib.util.spec_from_file_location("unfinished_task_review", path)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)
asyncio.run(module.deliver_task_review("restart"))
