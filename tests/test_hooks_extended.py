"""test_hooks_extended.py — Extended tests for UserPromptSubmit, Stop, PreCompact, Notification hooks."""

import asyncio
import unittest
from pathlib import Path
import tempfile
import shutil
import json

from src.hooks.registry import HookRegistry


# Helper to run async tests with unittest
def patch_async_test(func):
    """Decorator to run async test in unittest."""
    def wrapper(*args, **kwargs):
        return asyncio.run(func(*args, **kwargs))
    return wrapper


class TestHookEventsExtended(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.tmp_path = Path(self.tmp_dir)
        self.hooks_path = self.tmp_path / "hooks.json"

    def tearDown(self):
        shutil.rmtree(self.tmp_dir)

    def _write_hooks(self, data: dict):
        self.hooks_path.parent.mkdir(parents=True, exist_ok=True)
        self.hooks_path.write_text(json.dumps(data), encoding="utf-8")

    # --- UserPromptSubmit ---
    @patch_async_test
    async def test_user_prompt_submit_block(self):
        self._write_hooks({
            "UserPromptSubmit": [
                {
                    "matcher": "*",
                    "hooks": [
                        {"type": "block", "message": "Prompt contains forbidden word"}
                    ]
                }
            ]
        })
        reg = HookRegistry(config_dirs=[self.tmp_path])
        reg.reload()

        errors = await reg.run_user_prompt_submit("This is a test message")
        self.assertIn("Prompt contains forbidden word", errors)

    @patch_async_test
    async def test_user_prompt_submit_no_match(self):
        self._write_hooks({
            "UserPromptSubmit": [
                {
                    "matcher": "tool == \"Bash\"",
                    "hooks": [{"type": "block", "message": "No bash"}]
                }
            ]
        })
        reg = HookRegistry(config_dirs=[self.tmp_path])
        reg.reload()

        errors = await reg.run_user_prompt_submit("Hello world")
        self.assertEqual(errors, [])

    # --- Stop ---
    @patch_async_test
    async def test_stop_hook_runs(self):
        self._write_hooks({
            "Stop": [
                {
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": "echo 'stopped'"}]
                }
            ]
        })
        reg = HookRegistry(config_dirs=[self.tmp_path])
        reg.reload()

        results = await reg.run_stop(summary="Agent finished task")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].hook_name, "*")
        self.assertEqual(results[0].errors, [])

    # --- PreCompact ---
    @patch_async_test
    async def test_pre_compact_hook(self):
        self._write_hooks({
            "PreCompact": [
                {
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": "echo 'saving session {tokens}'"}]
                }
            ]
        })
        reg = HookRegistry(config_dirs=[self.tmp_path])
        reg.reload()

        actions = await reg.run_pre_compact({"tokens": 15000, "round": 5})
        self.assertGreater(len(actions), 0)
        self.assertIn("saving session", actions[0])

    # --- Notification ---
    @patch_async_test
    async def test_notification_hook(self):
        self._write_hooks({
            "Notification": [
                {
                    "matcher": "tool == \"Write\"",
                    "hooks": [{"type": "command", "command": "echo 'approval {payload}'"}]
                }
            ]
        })
        reg = HookRegistry(config_dirs=[self.tmp_path])
        reg.reload()

        results = await reg.run_notification("Write", {"file": "test.py"})
        self.assertIn("approved", results)

    @patch_async_test
    async def test_notification_block(self):
        self._write_hooks({
            "Notification": [
                {
                    "matcher": "*",
                    "hooks": [{"type": "block", "message": "Permission denied"}]
                }
            ]
        })
        reg = HookRegistry(config_dirs=[self.tmp_path])
        reg.reload()

        results = await reg.run_notification("SensitiveOp", {})
        self.assertIn("blocked: Permission denied", results)


# Helper to run async tests with unittest
# (Moved to top)
if __name__ == "__main__":
    unittest.main()