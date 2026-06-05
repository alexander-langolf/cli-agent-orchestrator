"""Cursor CLI (cursor-agent) provider implementation.

Unlike most CLIs, cursor-agent has no flags for injecting a system prompt or
MCP servers. It reads both from the *workspace*:

- System prompt → a project rule at ``.cursor/rules/cao-agent.mdc`` with
  ``alwaysApply: true``.
- MCP servers → ``.cursor/mcp.json`` (merged so existing servers survive),
  approved at launch with ``--approve-mcps``.

So ``initialize()`` writes those two files into the working directory (the
window is already ``cd``'d there) before launching the interactive TUI with
``--yolo`` (auto-trust + run-everything, required for non-interactive driving).
"""

import base64
import json
import logging
import re
import shlex
from typing import Optional

from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.constants import CURSOR_INIT_TIMEOUT
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status

logger = logging.getLogger(__name__)

ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"

# How many lines from the bottom to inspect. Cursor runs its TUI inline; the
# input bar + status bar live in the last several lines.
TAIL_LINES = 8

# Idle input-bar placeholders: "→ Plan, search, build anything" (fresh, no turn
# yet) or "→ Add a follow-up" (after a completed turn).
IDLE_PROMPT_PATTERN = r"→\s+(?:Add a follow-up|Plan, search, build anything)"
IDLE_PROMPT_PATTERN_LOG = r"(?:Add a follow-up|Plan, search, build anything)"

# Processing indicators: the spinner line "⠋ Composing", and the input-bar hint
# "ctrl+c to stop" / "esc to interrupt" shown only while a turn is running.
PROCESSING_PATTERN = r"(?:\bComposing\b|ctrl\+c to stop|esc to interrupt)"

# The status bar shows a context-usage percent ("Composer 2.5 · 7.7%") only
# after at least one turn has completed. Used to distinguish COMPLETED (idle
# after a turn) from IDLE (idle, no turn yet).
CONTEXT_PCT_PATTERN = r"·\s*\d+(?:\.\d+)?%"

# Workspace trust dialog. --yolo auto-accepts it, but it can flash on screen.
TRUST_PROMPT_PATTERN = r"Workspace Trust Required"

ERROR_PATTERN = r"(?m)^\s*(?:Error:|ERROR:|✗\s|panic:|Traceback \(most recent call last\):)"

# Where the per-workspace config files are written.
MCP_CONFIG_PATH = ".cursor/mcp.json"
RULE_PATH = ".cursor/rules/cao-agent.mdc"


class CursorProvider(BaseProvider):
    """Provider for the Cursor CLI (cursor-agent) interactive TUI."""

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[list] = None,
        skill_prompt: Optional[str] = None,
    ):
        super().__init__(terminal_id, session_name, window_name, allowed_tools, skill_prompt)
        self._initialized = False
        self._agent_profile = agent_profile

    @property
    def paste_enter_count(self) -> int:
        # A single Enter submits in the cursor-agent input bar.
        return 1

    # ------------------------------------------------------------------ setup

    def _build_system_prompt(self) -> str:
        """Resolve the agent profile's system prompt + soft tool constraints."""
        system_prompt = ""
        if self._agent_profile is not None:
            try:
                profile = load_agent_profile(self._agent_profile)
            except FileNotFoundError:
                raise
            except Exception as e:
                raise RuntimeError(f"Failed to load profile '{self._agent_profile}': {e}")
            system_prompt = profile.system_prompt if profile and profile.system_prompt else ""

        system_prompt = self._apply_skill_prompt(system_prompt)

        # Cursor has no native tool-restriction mechanism; soft-enforce via the
        # rule text (same approach as the Codex provider).
        if self._allowed_tools and "*" not in self._allowed_tools:
            from cli_agent_orchestrator.constants import SECURITY_PROMPT

            tools_list = ", ".join(self._allowed_tools)
            system_prompt = (
                f"{SECURITY_PROMPT}\nYou only have access to these tools: {tools_list}\n"
                + system_prompt
            )
        return system_prompt

    def _write_workspace_config_command(self) -> str:
        """Shell command (run in the window) that writes the .cursor/ config.

        - Merges cao-mcp-server into ``.cursor/mcp.json`` (preserving any
          servers already configured for the repo) with this terminal's
          CAO_TERMINAL_ID baked into the server env.
        - Writes the system prompt as an always-applied project rule.

        Content is base64-encoded so arbitrary prompt text (quotes, newlines)
        survives the trip through ``send_keys``.
        """
        rule_body = (
            "---\n"
            "description: CAO agent role and constraints\n"
            "alwaysApply: true\n"
            "---\n"
            f"{self._build_system_prompt()}\n"
        )
        rule_b64 = base64.b64encode(rule_body.encode()).decode()

        # Inline python merge so an existing project .cursor/mcp.json is kept.
        merge_py = (
            "import json,os;"
            f"p={MCP_CONFIG_PATH!r};"
            "d=json.load(open(p)) if os.path.exists(p) else {};"
            "d.setdefault('mcpServers',{})['cao-mcp-server']="
            "{'type':'stdio','command':'cao-mcp-server','env':{'CAO_TERMINAL_ID':"
            f"{self.terminal_id!r}"
            "}};"
            "json.dump(d,open(p,'w'),indent=2)"
        )

        return (
            "mkdir -p .cursor/rules && "
            f"printf %s {shlex.quote(rule_b64)} | base64 -d > {shlex.quote(RULE_PATH)} && "
            f"python3 -c {shlex.quote(merge_py)}"
        )

    def _build_launch_command(self) -> str:
        """Build the cursor-agent launch command.

        ``command`` bypasses any user shell alias. ``--yolo`` auto-trusts the
        workspace and runs all tools without prompts (CAO drives it
        non-interactively). ``--approve-mcps`` loads the project MCP servers.
        """
        parts = ["command", "cursor-agent", "--yolo", "--approve-mcps"]
        if self._agent_profile is not None:
            try:
                profile = load_agent_profile(self._agent_profile)
                if profile and profile.model:
                    parts.extend(["--model", profile.model])
            except Exception:
                pass
        return shlex.join(parts)

    # ----------------------------------------------------------- lifecycle

    def initialize(self) -> bool:
        """Write workspace config, launch cursor-agent, wait until ready."""
        if not wait_for_shell(tmux_client, self.session_name, self.window_name, timeout=10.0):
            raise TimeoutError("Shell initialization timed out after 10 seconds")

        # Warm up the shell (mirrors the Codex provider — freshly created kitty
        # windows can drop the first interactive command).
        tmux_client.send_keys(self.session_name, self.window_name, "echo ready")
        import time

        time.sleep(2.0)

        # Write .cursor/mcp.json (merged) and .cursor/rules/cao-agent.mdc.
        tmux_client.send_keys(
            self.session_name, self.window_name, self._write_workspace_config_command()
        )
        time.sleep(1.5)

        # Launch the interactive TUI.
        tmux_client.send_keys(self.session_name, self.window_name, self._build_launch_command())

        # --yolo auto-trusts, but guard in case the dialog waits for input.
        self._handle_trust_prompt(timeout=20.0)

        if not wait_until_status(
            self,
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=CURSOR_INIT_TIMEOUT,
            polling_interval=1.0,
        ):
            raise TimeoutError(
                f"Cursor initialization timed out after {CURSOR_INIT_TIMEOUT:g} seconds"
            )

        self._initialized = True
        return True

    def _handle_trust_prompt(self, timeout: float = 20.0) -> None:
        """Press [a] to trust the workspace if the dialog blocks on input."""
        import time

        start = time.time()
        while time.time() - start < timeout:
            output = tmux_client.get_history(self.session_name, self.window_name)
            if output:
                clean = re.sub(ANSI_CODE_PATTERN, "", output)
                # Already past the dialog → started.
                if re.search(IDLE_PROMPT_PATTERN, clean) or "Trusting workspace" in clean:
                    return
                if re.search(TRUST_PROMPT_PATTERN, clean):
                    logger.info("Cursor workspace trust prompt detected, selecting [a]")
                    tmux_client.send_keys(self.session_name, self.window_name, "a")
                    return
            time.sleep(1.0)
        logger.warning("Cursor trust prompt handler timed out")

    # -------------------------------------------------------------- status

    def get_status(self, tail_lines: Optional[int] = None) -> TerminalStatus:
        """Determine status from the cursor-agent TUI output."""
        output = tmux_client.get_history(self.session_name, self.window_name, tail_lines=tail_lines)
        if not output:
            return TerminalStatus.ERROR

        clean_output = re.sub(ANSI_CODE_PATTERN, "", output)
        lines = clean_output.splitlines()
        tail = "\n".join(lines[-TAIL_LINES:])

        # Trust dialog still blocking (rare with --yolo).
        if re.search(TRUST_PROMPT_PATTERN, "\n".join(lines[-15:])):
            if "Trusting workspace" not in clean_output and not re.search(
                IDLE_PROMPT_PATTERN, tail
            ):
                return TerminalStatus.WAITING_USER_ANSWER

        # Processing — match only the tail so stale spinner lines in scrollback
        # don't latch PROCESSING forever (lesson #1).
        if re.search(PROCESSING_PATTERN, tail):
            return TerminalStatus.PROCESSING

        # Idle input bar present and not processing → ready.
        if re.search(IDLE_PROMPT_PATTERN, tail):
            # Context-% in the status bar means a turn has completed.
            if re.search(CONTEXT_PCT_PATTERN, tail):
                return TerminalStatus.COMPLETED
            return TerminalStatus.IDLE

        if re.search(ERROR_PATTERN, tail):
            return TerminalStatus.ERROR

        # Banner up but input bar not yet rendered → still warming up.
        return TerminalStatus.PROCESSING

    def get_idle_pattern_for_log(self) -> str:
        return IDLE_PROMPT_PATTERN_LOG

    # ------------------------------------------------------------ extraction

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Best-effort extraction of the last agent response.

        Cursor echoes the user message, then the response, then returns to the
        idle input bar. Take the text between the last idle-bar boundary and the
        preceding content. For assigned workers the canonical result path is the
        ``send_message`` MCP tool, so this is a fallback for handoff-style use.
        """
        clean = re.sub(ANSI_CODE_PATTERN, "", script_output)
        lines = clean.splitlines()

        # Drop trailing TUI chrome (input bar, status bar, box borders).
        def is_chrome(line: str) -> bool:
            s = line.strip()
            return (
                not s
                or bool(re.search(IDLE_PROMPT_PATTERN, line))
                or bool(re.search(CONTEXT_PCT_PATTERN, line))
                or bool(re.search(PROCESSING_PATTERN, line))
                or s.startswith(("╭", "╰", "│", "▄", "▀", "→"))
                or s in {"Cursor Agent",}
                or re.match(r"^v?\d{4}\.\d", s) is not None
            )

        content = [ln for ln in lines if not is_chrome(ln)]
        text = "\n".join(content).strip()
        if not text:
            raise ValueError("No Cursor response found")
        return text

    def exit_cli(self) -> str:
        """Command to leave the cursor-agent TUI."""
        return "/quit"

    def cleanup(self) -> None:
        """Clean up provider state. The window/session teardown removes the
        process; the per-workspace .cursor/ files are left in place (untracked
        worker scratch)."""
        self._initialized = False
