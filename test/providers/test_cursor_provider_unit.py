"""Unit tests for the Cursor (cursor-agent) provider.

Status fixtures are captured from the real cursor-agent TUI (v2026.05.09).
"""

from unittest.mock import patch

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.cursor_agent import CursorProvider

# --- real TUI captures ------------------------------------------------------

IDLE_FRESH = """\
  Cursor Agent
  v2026.05.09-0afadcc
 ▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄
  → Plan, search, build anything
 ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀
  Composer 2.5                                  Auto-run
  /private/tmp/curtest
"""

PROCESSING = """\
  Cursor Agent
  What is 2+2? Reply with just the number.
 ⠘⠣ Composing
 ▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄
  → Add a follow-up                       ctrl+c to stop
 ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀
  Composer 2.5                                  Auto-run
  /private/tmp/curtest
"""

COMPLETED = """\
  Cursor Agent
  What is 2+2? Reply with just the number.
  4
 ▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄▄
  → Add a follow-up
 ▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀▀
  Composer 2.5 · 7.7%                            Auto-run
  /private/tmp/curtest
"""

TRUST = """\
  ╭──────────────────────────────────────────╮
  │  ⚠ Workspace Trust Required                │
  │  Do you trust the contents of this dir?   │
  │    [a] Trust this workspace               │
  │    [q] Quit                               │
  ╰──────────────────────────────────────────╯
"""


def _provider():
    return CursorProvider("tid-1234", "sess", "win-0", agent_profile=None, allowed_tools=["*"])


class TestCursorStatus:
    @patch("cli_agent_orchestrator.providers.cursor_agent.tmux_client")
    def test_idle_fresh(self, mock_tmux):
        mock_tmux.get_history.return_value = IDLE_FRESH
        assert _provider().get_status() == TerminalStatus.IDLE

    @patch("cli_agent_orchestrator.providers.cursor_agent.tmux_client")
    def test_processing(self, mock_tmux):
        mock_tmux.get_history.return_value = PROCESSING
        assert _provider().get_status() == TerminalStatus.PROCESSING

    @patch("cli_agent_orchestrator.providers.cursor_agent.tmux_client")
    def test_completed_after_turn(self, mock_tmux):
        mock_tmux.get_history.return_value = COMPLETED
        assert _provider().get_status() == TerminalStatus.COMPLETED

    @patch("cli_agent_orchestrator.providers.cursor_agent.tmux_client")
    def test_trust_prompt_waits(self, mock_tmux):
        mock_tmux.get_history.return_value = TRUST
        assert _provider().get_status() == TerminalStatus.WAITING_USER_ANSWER

    @patch("cli_agent_orchestrator.providers.cursor_agent.tmux_client")
    def test_empty_is_error(self, mock_tmux):
        mock_tmux.get_history.return_value = ""
        assert _provider().get_status() == TerminalStatus.ERROR

    @patch("cli_agent_orchestrator.providers.cursor_agent.tmux_client")
    def test_stale_spinner_in_scrollback_does_not_latch(self, mock_tmux):
        # An old "Composing" high in the buffer must not force PROCESSING once
        # the bottom shows the idle bar (lesson #1: match processing on tail).
        mock_tmux.get_history.return_value = (
            " ⠘⠣ Composing\n" + ("filler\n" * 20) + IDLE_FRESH
        )
        assert _provider().get_status() == TerminalStatus.IDLE


class TestCursorBuild:
    def test_launch_command_flags(self):
        cmd = _provider()._build_launch_command()
        assert "cursor-agent" in cmd
        assert "--yolo" in cmd
        assert "--approve-mcps" in cmd

    def test_setup_command_writes_mcp_and_rule(self):
        cmd = _provider()._write_workspace_config_command()
        assert ".cursor/rules/cao-agent.mdc" in cmd
        assert ".cursor/mcp.json" in cmd
        # terminal id baked into the MCP env
        assert "tid-1234" in cmd

    def test_paste_enter_count_is_one(self):
        assert _provider().paste_enter_count == 1
