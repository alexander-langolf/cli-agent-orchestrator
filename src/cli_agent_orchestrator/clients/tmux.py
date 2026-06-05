"""Kitty session client exposed through the legacy terminal-client singleton."""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

from cli_agent_orchestrator.constants import CAO_HOME_DIR, TMUX_HISTORY_LINES
from cli_agent_orchestrator.utils.terminal import validate_tmux_name

logger = logging.getLogger(__name__)


class KittyClient:
    """Subprocess wrapper around kitty sessions and ``kitten @`` remote control."""

    _BLOCKED_DIRECTORIES = frozenset(
        {
            "/",
            "/bin",
            "/sbin",
            "/usr/bin",
            "/usr/sbin",
            "/etc",
            "/var",
            "/tmp",
            "/dev",
            "/proc",
            "/sys",
            "/root",
            "/boot",
            "/lib",
            "/lib64",
            "/private/etc",
            "/private/var",
            "/private/tmp",
        }
    )

    _BLOCKED_ENV_PREFIXES = ("CLAUDE", "CODEX_", "__MISE_")
    _BLOCKED_PREFIX_ALLOWLIST = frozenset(
        {
            "CLAUDE_CODE_USE_BEDROCK",
            "CLAUDE_CODE_USE_VERTEX",
            "CLAUDE_CODE_USE_FOUNDRY",
            "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
            "CLAUDE_CODE_SKIP_VERTEX_AUTH",
            "CLAUDE_CODE_SKIP_FOUNDRY_AUTH",
        }
    )
    _MAX_ENV_VALUE_BYTES = 2048

    def __init__(
        self,
        command: str | None = None,
        socket_name: str | None = None,
        *,
        kitty_command: str | None = None,
        kitten_command: str | None = None,
        socket_dir: str | Path | None = None,
    ) -> None:
        self.kitty_command = kitty_command or command or os.environ.get("CAO_KITTY_COMMAND", "kitty")
        self.kitten_command = kitten_command or os.environ.get("CAO_KITTEN_COMMAND", "kitten")
        base_socket_dir = socket_dir or os.environ.get("CAO_KITTY_SOCKET_DIR")
        self.socket_dir = Path(base_socket_dir) if base_socket_dir else CAO_HOME_DIR / "kitty"
        self.socket_name = socket_name
        self._log_pipes: dict[tuple[str, str], tuple[threading.Event, threading.Thread]] = {}

    def _command(self, *args: str) -> list[str]:
        """Build a generic kitten command for compatibility with older callers."""
        return [self.kitten_command, "@", *args]

    def session_file_path(self, session_name: str) -> Path:
        validated_session = validate_tmux_name(session_name, "session_name")
        return self.socket_dir / f"{validated_session}.kitty-session"

    def socket_path(self, session_name: str) -> Path:
        validated_session = validate_tmux_name(session_name, "session_name")
        return self.socket_dir / f"{validated_session}.sock"

    def _socket_address(self, session_name: str) -> str:
        return f"unix:{self.socket_path(session_name)}"

    def _remote_command(self, session_name: str, *args: str) -> list[str]:
        return [
            self.kitten_command,
            "@",
            "--to",
            self._socket_address(session_name),
            *args,
        ]

    def _run(
        self,
        args: list[str],
        *,
        session_name: str | None = None,
        check: bool = True,
        capture_output: bool = True,
        text: bool = True,
        input: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = self._remote_command(session_name, *args) if session_name else args
        logger.debug("Running kitty command: %s", shlex.join(command))
        return subprocess.run(
            command,
            check=check,
            capture_output=capture_output,
            text=text,
            input=input,
        )

    def _wait_for_session_ready(self, session_name: str, timeout: float = 5.0) -> None:
        """Wait until the kitty remote-control socket answers."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.session_exists(session_name):
                return
            time.sleep(0.1)
        raise TimeoutError(f"Kitty session '{session_name}' did not become ready")

    def attach_command(self, session_name: str, window_name: str) -> list[str]:
        """Build a command that focuses a kitty window in a CAO session."""
        validated_window = validate_tmux_name(window_name, "window_name")
        return self._remote_command(
            session_name,
            "focus-window",
            "--match",
            self._window_match(validated_window),
        )

    def attach_session_command(self, session_name: str) -> list[str]:
        """Build a command that focuses the first kitty window in a CAO session."""
        validated_session = validate_tmux_name(session_name, "session_name")
        return self._remote_command(
            validated_session,
            "focus-window",
            "--match",
            f"env:CAO_SESSION_NAME={validated_session}",
        )

    def _window_match(self, window_name: str) -> str:
        validated_window = validate_tmux_name(window_name, "window_name")
        return f"env:CAO_WINDOW_NAME={validated_window}"

    def _resolve_and_validate_working_directory(self, working_directory: Optional[str]) -> str:
        """Resolve and validate working directory."""
        if working_directory is None:
            working_directory = os.getcwd()

        working_directory = os.path.expanduser(working_directory)
        real_path = os.path.realpath(os.path.abspath(working_directory))

        if not real_path.startswith("/"):
            raise ValueError(f"Working directory must be an absolute path: {working_directory}")

        if real_path in self._BLOCKED_DIRECTORIES:
            raise ValueError(
                f"Working directory not allowed: {working_directory} "
                f"(resolves to blocked system path {real_path})"
            )

        if not os.path.isdir(real_path):
            raise ValueError(f"Working directory does not exist: {working_directory}")

        return real_path

    @classmethod
    def _is_blocked_env_key(cls, key: str) -> bool:
        """Return True if ``key`` matches a blocked prefix and is not allowlisted."""
        if key in cls._BLOCKED_PREFIX_ALLOWLIST:
            return False
        return any(key.startswith(p) for p in cls._BLOCKED_ENV_PREFIXES)

    @classmethod
    def _merge_extra_env(
        cls, environment: Dict[str, str], extra_env: Optional[Dict[str, str]]
    ) -> None:
        """Merge operator-supplied env vars into ``environment`` in place."""
        if not extra_env:
            return
        for key, value in extra_env.items():
            if cls._is_blocked_env_key(key):
                logger.warning("Dropping forwarded env var with blocked prefix: %s", key)
                continue
            if len(value.encode("utf-8")) >= cls._MAX_ENV_VALUE_BYTES:
                logger.warning(
                    "Dropping forwarded env var %s - value exceeds %d bytes",
                    key,
                    cls._MAX_ENV_VALUE_BYTES,
                )
                continue
            environment[key] = value

    def _base_environment(self, terminal_id: str, extra_env: Optional[Dict[str, str]]) -> Dict[str, str]:
        essential_keys = {
            "HOME",
            "PATH",
            "SHELL",
            "USER",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "TERM",
            "SSH_AUTH_SOCK",
            "DISPLAY",
            "XDG_RUNTIME_DIR",
            "DO_NOT_TRACK",
        }
        environment = {
            k: v
            for k, v in os.environ.items()
            if (
                k in essential_keys
                or k in self._BLOCKED_PREFIX_ALLOWLIST
                or (
                    not self._is_blocked_env_key(k)
                    and k.startswith(("CAO_", "KIRO_", "MISE_", "AWS_"))
                    and len(v.encode("utf-8")) < self._MAX_ENV_VALUE_BYTES
                )
            )
        }
        self._merge_extra_env(environment, extra_env)
        environment["CAO_TERMINAL_ID"] = terminal_id
        return environment

    @staticmethod
    def _session_line(args: list[str]) -> str:
        return shlex.join(args)

    def _write_session_file(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: str,
        environment: Dict[str, str],
    ) -> Path:
        self.socket_dir.mkdir(parents=True, exist_ok=True)
        session_file = self.session_file_path(session_name)
        launch_args = ["launch", "--title", window_name]
        for key, value in environment.items():
            launch_args.extend(["--env", f"{key}={value}"])
        launch_args.extend(
            [
                "--env",
                f"CAO_SESSION_NAME={session_name}",
                "--env",
                f"CAO_WINDOW_NAME={window_name}",
                "--var",
                f"cao_session={session_name}",
                "--var",
                f"cao_window={window_name}",
            ]
        )
        session_text = "\n".join(
            [
                f"os_window_title {session_name}",
                "os_window_size 220c 50c",
                "enabled_layouts tall,stack,splits",
                "layout tall",
                self._session_line(["cd", working_directory]),
                self._session_line(launch_args),
                "",
            ]
        )
        session_file.write_text(session_text, encoding="utf-8")
        return session_file

    def create_session(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> str:
        """Create a kitty session with an initial CAO window and return the window name."""
        working_directory = self._resolve_and_validate_working_directory(working_directory)
        validated_session = validate_tmux_name(session_name, "session_name")
        validated_window = validate_tmux_name(window_name, "window_name")
        environment = self._base_environment(terminal_id, extra_env)
        session_file = self._write_session_file(
            validated_session,
            validated_window,
            terminal_id,
            working_directory,
            environment,
        )
        command = [
            self.kitty_command,
            "--detach",
            "--listen-on",
            self._socket_address(validated_session),
            "-o",
            "allow_remote_control=socket-only",
            "--session",
            str(session_file),
        ]
        logger.debug("Starting kitty session: %s", shlex.join(command))
        subprocess.Popen(command)
        self._wait_for_session_ready(validated_session)
        logger.info(
            "Created kitty session: %s with window: %s in directory: %s",
            validated_session,
            validated_window,
            working_directory,
        )
        return validated_window

    def create_window(
        self,
        session_name: str,
        window_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
        window_shell: Optional[str] = None,
        extra_env: Optional[Dict[str, str]] = None,
    ) -> str:
        """Create a kitty window in an existing CAO session and return the window name."""
        working_directory = self._resolve_and_validate_working_directory(working_directory)
        validated_session = validate_tmux_name(session_name, "session_name")
        validated_window = validate_tmux_name(window_name, "window_name")

        if not self.session_exists(validated_session):
            raise ValueError(f"Session '{validated_session}' not found")

        window_env: dict[str, str] = {}
        self._merge_extra_env(window_env, extra_env)
        window_env["CAO_SESSION_NAME"] = validated_session
        window_env["CAO_WINDOW_NAME"] = validated_window
        window_env["CAO_TERMINAL_ID"] = terminal_id

        args = [
            "launch",
            "--type=window",
            "--title",
            validated_window,
            "--cwd",
            working_directory,
            "--add-to-session",
            ".",
        ]
        for key, value in window_env.items():
            args.extend(["--env", f"{key}={value}"])
        if window_shell:
            args.extend(["sh", "-lc", window_shell])

        self._run(args, session_name=validated_session)
        logger.info(
            "Created window '%s' in kitty session '%s' in directory: %s",
            validated_window,
            validated_session,
            working_directory,
        )
        return validated_window

    def send_keys(
        self,
        session_name: str,
        window_name: str,
        keys: str,
        enter_count: int = 1,
        force_bracketed_paste: bool = False,
    ) -> None:
        """Send text to a kitty window, then send Enter key presses."""
        validated_session = validate_tmux_name(session_name, "session_name")
        match = self._window_match(window_name)
        bracketed = "enable" if force_bracketed_paste else "disable"
        self._run(
            [
                "send-text",
                "--match",
                match,
                "--stdin",
                "--bracketed-paste",
                bracketed,
            ],
            session_name=validated_session,
            input=keys,
        )
        time.sleep(0.3)
        for index in range(enter_count):
            if index > 0:
                time.sleep(0.5)
            self._run(["send-key", "--match", match, "enter"], session_name=validated_session)
        logger.debug("Sent keys to %s/%s", validated_session, window_name)

    def send_keys_via_paste(self, session_name: str, window_name: str, text: str) -> None:
        """Send text to a kitty window using the standard text path."""
        self.send_keys(session_name, window_name, text)

    @staticmethod
    def _normalize_key(key: str) -> str:
        if key.startswith("C-"):
            return f"ctrl+{key[2:].lower()}"
        if key.startswith("M-"):
            return f"alt+{key[2:].lower()}"
        return key.lower()

    def send_special_key(self, session_name: str, window_name: str, key: str) -> None:
        """Send a special key sequence to a kitty window."""
        validated_session = validate_tmux_name(session_name, "session_name")
        self._run(
            ["send-key", "--match", self._window_match(window_name), self._normalize_key(key)],
            session_name=validated_session,
        )
        logger.debug("Sent special key to %s/%s", validated_session, window_name)

    def send_literal_keys(self, session_name: str, window_name: str, keys: str) -> None:
        """Send literal bytes/escape sequences to a kitty window."""
        validated_session = validate_tmux_name(session_name, "session_name")
        self._run(
            [
                "send-text",
                "--match",
                self._window_match(window_name),
                "--stdin",
                "--bracketed-paste",
                "disable",
            ],
            session_name=validated_session,
            input=keys,
        )
        logger.debug("Sent literal keys to %s/%s", validated_session, window_name)

    def get_history(
        self,
        session_name: str,
        window_name: str,
        tail_lines: Optional[int] = None,
        strip_escapes: bool = False,
        full_history: bool = False,
    ) -> str:
        """Get window text from kitty scrollback."""
        validated_session = validate_tmux_name(session_name, "session_name")
        args = [
            "get-text",
            "--match",
            self._window_match(window_name),
            "--extent",
            "all",
        ]
        if not strip_escapes:
            args.append("--ansi")
        result = self._run(args, session_name=validated_session)
        output = (result.stdout or "").rstrip("\n")
        if full_history:
            return output
        lines = tail_lines if tail_lines is not None else TMUX_HISTORY_LINES
        return "\n".join(output.splitlines()[-lines:])

    def list_sessions(self) -> List[Dict[str, str]]:
        """List active CAO kitty sessions discovered from socket files."""
        if not self.socket_dir.exists():
            return []
        sessions: List[Dict[str, str]] = []
        for socket_path in sorted(self.socket_dir.glob("*.sock")):
            session_name = socket_path.stem
            try:
                result = self._run(["ls"], session_name=session_name, check=False)
            except Exception as e:
                logger.debug("Failed to query kitty session %s: %s", session_name, e)
                continue
            if result.returncode == 0:
                sessions.append({"id": session_name, "name": session_name, "status": "active"})
        return sessions

    @staticmethod
    def _iter_windows(tree: object):
        if not isinstance(tree, list):
            return
        for os_window in tree:
            if not isinstance(os_window, dict):
                continue
            for tab in os_window.get("tabs", []):
                if not isinstance(tab, dict):
                    continue
                for window in tab.get("windows", []):
                    if isinstance(window, dict):
                        yield window

    def _list_window_tree(self, session_name: str) -> object:
        result = self._run(["ls", "--all-env-vars"], session_name=session_name)
        try:
            return json.loads(result.stdout or "[]")
        except json.JSONDecodeError:
            logger.warning("Failed to parse kitty ls output for session %s", session_name)
            return []

    def _find_window(self, session_name: str, window_name: str) -> Optional[dict]:
        validated_window = validate_tmux_name(window_name, "window_name")
        tree = self._list_window_tree(session_name)
        for window in self._iter_windows(tree):
            env = window.get("env", {})
            if env.get("CAO_WINDOW_NAME") == validated_window or window.get("title") == validated_window:
                return window
        return None

    def get_session_windows(self, session_name: str) -> List[Dict[str, str]]:
        """Get all CAO windows in a kitty session."""
        validated_session = validate_tmux_name(session_name, "session_name")
        try:
            tree = self._list_window_tree(validated_session)
        except Exception as e:
            logger.error("Failed to get windows for session %s: %s", validated_session, e)
            return []

        windows: List[Dict[str, str]] = []
        for index, window in enumerate(self._iter_windows(tree)):
            env = window.get("env", {})
            name = env.get("CAO_WINDOW_NAME") or window.get("title")
            if name:
                windows.append({"name": str(name), "index": str(index)})
        return windows

    def kill_session(self, session_name: str) -> bool:
        """Close all kitty windows in a CAO session."""
        validated_session = validate_tmux_name(session_name, "session_name")
        result = self._run(
            [
                "close-window",
                "--match",
                f"env:CAO_SESSION_NAME={validated_session}",
                "--ignore-no-match",
            ],
            session_name=validated_session,
            check=False,
        )
        for path in (self.socket_path(validated_session), self.session_file_path(validated_session)):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        if result.returncode == 0:
            logger.info("Killed kitty session: %s", validated_session)
            return True
        return False

    def kill_window(self, session_name: str, window_name: str) -> bool:
        """Close a specific kitty window within a session."""
        validated_session = validate_tmux_name(session_name, "session_name")
        result = self._run(
            [
                "close-window",
                "--match",
                self._window_match(window_name),
                "--ignore-no-match",
            ],
            session_name=validated_session,
            check=False,
        )
        if result.returncode == 0:
            logger.info("Killed kitty window: %s/%s", validated_session, window_name)
            return True
        return False

    def session_exists(self, session_name: str) -> bool:
        """Check whether the kitty remote-control socket responds."""
        validated_session = validate_tmux_name(session_name, "session_name")
        result = self._run(["ls"], session_name=validated_session, check=False)
        return result.returncode == 0

    def get_pane_working_directory(self, session_name: str, window_name: str) -> Optional[str]:
        """Get the current working directory for a kitty window."""
        try:
            window = self._find_window(session_name, window_name)
            if not window:
                return None
            foreground_processes = window.get("foreground_processes")
            if isinstance(foreground_processes, list):
                for process in foreground_processes:
                    if isinstance(process, dict) and process.get("cwd"):
                        return str(process["cwd"])
            output = window.get("cwd")
            return str(output) if output else None
        except Exception as e:
            logger.error("Failed to get working directory for %s/%s: %s", session_name, window_name, e)
            return None

    def get_pane_current_command(self, session_name: str, window_name: str) -> Optional[str]:
        """Get the command line reported by kitty for a window."""
        try:
            window = self._find_window(session_name, window_name)
            if not window:
                return None
            foreground_processes = window.get("foreground_processes")
            if isinstance(foreground_processes, list):
                for process in foreground_processes:
                    if isinstance(process, dict):
                        process_cmdline = process.get("cmdline")
                        if isinstance(process_cmdline, list) and process_cmdline:
                            return Path(str(process_cmdline[0])).name
                        if isinstance(process_cmdline, str) and process_cmdline:
                            return Path(process_cmdline.split()[0]).name
            cmdline = window.get("cmdline")
            if isinstance(cmdline, list) and cmdline:
                return Path(str(cmdline[0])).name
            if isinstance(cmdline, str) and cmdline:
                return Path(cmdline.split()[0]).name
            return None
        except Exception as e:
            logger.error("Failed to get pane command for %s/%s: %s", session_name, window_name, e)
            return None

    def pipe_pane(self, session_name: str, window_name: str, file_path: str) -> None:
        """Continuously mirror kitty scrollback into a log file for watchdog consumers."""
        validated_session = validate_tmux_name(session_name, "session_name")
        validated_window = validate_tmux_name(window_name, "window_name")
        path = Path(file_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
        self.stop_pipe_pane(validated_session, validated_window)

        stop_event = threading.Event()

        def poll_scrollback() -> None:
            last_output: str | None = None
            while not stop_event.is_set():
                try:
                    output = self.get_history(
                        validated_session,
                        validated_window,
                        strip_escapes=True,
                        full_history=True,
                    )
                    if output != last_output:
                        path.write_text(output, encoding="utf-8")
                        last_output = output
                except Exception as e:
                    logger.debug(
                        "Failed to mirror kitty scrollback for %s/%s: %s",
                        validated_session,
                        validated_window,
                        e,
                    )
                stop_event.wait(0.5)

        thread = threading.Thread(
            target=poll_scrollback,
            name=f"cao-kitty-log-{validated_session}-{validated_window}",
            daemon=True,
        )
        self._log_pipes[(validated_session, validated_window)] = (stop_event, thread)
        thread.start()
        logger.info("Started kitty log mirror for %s/%s at %s", session_name, window_name, file_path)

    def stop_pipe_pane(self, session_name: str, window_name: str) -> None:
        """Stop the kitty scrollback mirror for a window."""
        validated_session = validate_tmux_name(session_name, "session_name")
        validated_window = validate_tmux_name(window_name, "window_name")
        pipe = self._log_pipes.pop((validated_session, validated_window), None)
        if pipe:
            stop_event, thread = pipe
            stop_event.set()
            if thread.is_alive():
                thread.join(timeout=1.0)
        logger.info("Stopped kitty log mirror for %s/%s", validated_session, validated_window)


tmux_client = KittyClient()
