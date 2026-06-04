from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

from .base import BaseAgent


class MockDoneAgent(BaseAgent):
    """Agent used for runtime smoke tests; it terminates immediately."""

    def __init__(self, *args, **kwargs):
        self.model = "mock-done"
        self.done = False
        self.step_idx = 0
        self.save_path = None

    def init(self, task_description: str, display_resolution: Tuple[int, int], save_path: str):
        self.task_description = task_description
        self.display_resolution = display_resolution
        self.save_path = Path(save_path)

    def step(self, obs: Dict[str, Any], action_outputs: List[Dict[str, Any]]):
        del obs, action_outputs
        self.step_idx += 1
        self.done = True
        return []

    def finish(self, *args, **kwargs):
        if not self.save_path:
            return
        payload = {
            "model": self.model,
            "done": self.done,
            "steps": self.step_idx,
            "info": kwargs.get("info"),
        }
        (self.save_path / "mock_done_agent.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
