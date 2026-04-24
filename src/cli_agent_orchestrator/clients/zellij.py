"""Zellij client for CAO terminal runtime operations."""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from cli_agent_orchestrator.constants import TERMINAL_HISTORY_LINES

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ZellijTerminalInfo:
    """Runtime metadata for one CAO terminal backed by a Zellij tab/pane."""

    name: str
    session_name: str
    tab_id: int
    pane_id: int
    launch_working_directory: str


class ZellijClient:
    """Small subprocess-based wrapper around Zellij CLI automation."""

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

    def __init__(self) -> None:
        self.socket_dir = os.environ.get("CAO_ZELLIJ_SOCKET_DIR", "/tmp/zellij-cao")
        self._log_processes: Dict[str, subprocess.Popen] = {}
        Path(self.socket_dir).mkdir(parents=True, exist_ok=True)

    def _build_env(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        env = os.environ.copy()
        env["ZELLIJ_SOCKET_DIR"] = self.socket_dir
        env.pop("NO_COLOR", None)
        if env.get("TERM") in (None, "", "dumb"):
            env["TERM"] = "xterm-256color"
        if not env.get("COLORTERM"):
            env["COLORTERM"] = "truecolor"
        if extra:
            env.update(extra)
        return env

    def _run(
        self,
        args: List[str],
        *,
        cwd: Optional[str] = None,
        env_extra: Optional[Dict[str, str]] = None,
    ) -> subprocess.CompletedProcess[str]:
        logger.debug("Running Zellij command: %s", shlex.join(args))
        return subprocess.run(
            args,
            check=True,
            cwd=cwd,
            env=self._build_env(env_extra),
            capture_output=True,
            text=True,
        )

    def _zellij(self, session_name: str, *args: str) -> subprocess.CompletedProcess[str]:
        return self._run(["zellij", "--session", session_name, *args])

    def _resolve_and_validate_working_directory(self, working_directory: Optional[str]) -> str:
        """Resolve and validate a terminal launch directory."""
        if working_directory is None:
            working_directory = os.getcwd()

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

    def _terminal_panes(self, session_name: str) -> List[Dict]:
        result = self._zellij(
            session_name,
            "action",
            "list-panes",
            "-j",
            "-a",
            "-c",
            "-t",
        )
        panes = json.loads(result.stdout or "[]")
        return [p for p in panes if not p.get("is_plugin")]

    def _find_terminal_pane(
        self,
        session_name: str,
        terminal_name: Optional[str] = None,
        *,
        tab_id: Optional[int] = None,
    ) -> Dict:
        panes = self._terminal_panes(session_name)
        for pane in panes:
            if tab_id is not None and int(pane.get("tab_id")) == int(tab_id):
                return pane
            if terminal_name is not None and pane.get("tab_name") == terminal_name:
                return pane
        target = f"tab_id={tab_id}" if tab_id is not None else f"name={terminal_name}"
        raise ValueError(f"Terminal pane not found in session '{session_name}' for {target}")

    def _find_terminal_pane_with_retry(
        self,
        session_name: str,
        terminal_name: Optional[str] = None,
        *,
        tab_id: Optional[int] = None,
        attempts: int = 10,
        delay: float = 0.2,
    ) -> Dict:
        last_error: Optional[ValueError] = None
        for _ in range(attempts):
            try:
                return self._find_terminal_pane(session_name, terminal_name, tab_id=tab_id)
            except ValueError as exc:
                last_error = exc
                time.sleep(delay)
        raise last_error or ValueError("Terminal pane not found")

    def _shell_command(self, terminal_id: str) -> List[str]:
        shell = os.environ.get("SHELL") or "/bin/sh"
        return ["env", f"CAO_TERMINAL_ID={terminal_id}", shell, "-l"]

    def create_session(
        self,
        session_name: str,
        terminal_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
    ) -> ZellijTerminalInfo:
        """Create a detached Zellij session with its first terminal tab."""
        launch_dir = self._resolve_and_validate_working_directory(working_directory)
        try:
            self._run(
                ["zellij", "attach", "-b", session_name],
                cwd=launch_dir,
                env_extra={"CAO_TERMINAL_ID": terminal_id},
            )
            self._zellij(
                session_name,
                "action",
                "rename-tab-by-id",
                "0",
                terminal_name,
            )
            pane = self._find_terminal_pane_with_retry(session_name, terminal_name, tab_id=0)
            logger.info("Created Zellij session %s with tab %s", session_name, terminal_name)
            return ZellijTerminalInfo(
                name=terminal_name,
                session_name=session_name,
                tab_id=int(pane["tab_id"]),
                pane_id=int(pane["id"]),
                launch_working_directory=launch_dir,
            )
        except Exception as exc:
            logger.error("Failed to create Zellij session %s: %s", session_name, exc)
            raise

    def create_window(
        self,
        session_name: str,
        terminal_name: str,
        terminal_id: str,
        working_directory: Optional[str] = None,
    ) -> ZellijTerminalInfo:
        """Create a new Zellij tab in an existing CAO session."""
        launch_dir = self._resolve_and_validate_working_directory(working_directory)
        try:
            command = [
                "zellij",
                "--session",
                session_name,
                "action",
                "new-tab",
                "-n",
                terminal_name,
                "--cwd",
                launch_dir,
                "--",
                *self._shell_command(terminal_id),
            ]
            result = self._run(command)
            tab_id = int((result.stdout or "").strip())
            pane = self._find_terminal_pane_with_retry(session_name, terminal_name, tab_id=tab_id)
            logger.info("Created Zellij tab %s in session %s", terminal_name, session_name)
            return ZellijTerminalInfo(
                name=terminal_name,
                session_name=session_name,
                tab_id=tab_id,
                pane_id=int(pane["id"]),
                launch_working_directory=launch_dir,
            )
        except Exception as exc:
            logger.error("Failed to create Zellij tab in session %s: %s", session_name, exc)
            raise

    def _pane_id(self, session_name: str, terminal_name: str) -> int:
        return int(self._find_terminal_pane(session_name, terminal_name)["id"])

    def send_keys(
        self,
        session_name: str,
        terminal_name: str,
        keys: str,
        enter_count: int = 1,
    ) -> None:
        """Paste text with bracketed paste mode, then submit with Enter."""
        pane_id = self._pane_id(session_name, terminal_name)
        try:
            self._zellij(
                session_name,
                "action",
                "paste",
                "-p",
                str(pane_id),
                keys,
            )
            time.sleep(0.3)
            for index in range(enter_count):
                if index > 0:
                    time.sleep(0.5)
                self._zellij(
                    session_name,
                    "action",
                    "send-keys",
                    "-p",
                    str(pane_id),
                    "Enter",
                )
        except Exception as exc:
            logger.error("Failed to send input to %s:%s: %s", session_name, terminal_name, exc)
            raise

    def send_keys_via_paste(self, session_name: str, terminal_name: str, text: str) -> None:
        """Compatibility wrapper for providers that call explicit paste delivery."""
        self.send_keys(session_name, terminal_name, text)

    @staticmethod
    def _map_special_key(key: str) -> str:
        if key.startswith("C-") and len(key) > 2:
            return f"Ctrl {key[2:]}"
        if key.startswith("M-") and len(key) > 2:
            return f"Alt {key[2:]}"
        return key

    def send_special_key(self, session_name: str, terminal_name: str, key: str) -> None:
        """Send one non-text key sequence to the target terminal pane."""
        pane_id = self._pane_id(session_name, terminal_name)
        self._zellij(
            session_name,
            "action",
            "send-keys",
            "-p",
            str(pane_id),
            self._map_special_key(key),
        )

    def send_raw_bytes(self, session_name: str, terminal_name: str, data: bytes) -> None:
        """Write raw byte values to the pane for sequences Zellij does not name."""
        pane_id = self._pane_id(session_name, terminal_name)
        self._zellij(
            session_name,
            "action",
            "write",
            "-p",
            str(pane_id),
            *[str(byte) for byte in data],
        )

    def get_history(
        self,
        session_name: str,
        terminal_name: str,
        tail_lines: Optional[int] = None,
    ) -> str:
        """Return pane scrollback from Zellij dump-screen."""
        pane_id = self._pane_id(session_name, terminal_name)
        result = self._zellij(
            session_name,
            "action",
            "dump-screen",
            "-p",
            str(pane_id),
            "--full",
            "--ansi",
        )
        output = (result.stdout or "").rstrip("\n")
        if tail_lines is not None:
            return "\n".join(output.splitlines()[-tail_lines:])
        return "\n".join(output.splitlines()[-TERMINAL_HISTORY_LINES:])

    def list_sessions(self) -> List[Dict[str, str]]:
        """List Zellij sessions in CAO's session shape."""
        try:
            result = self._run(["zellij", "list-sessions", "--no-formatting"])
        except Exception as exc:
            logger.error("Failed to list Zellij sessions: %s", exc)
            return []

        sessions: List[Dict[str, str]] = []
        for raw_line in (result.stdout or "").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            name = line.split(" ", 1)[0]
            status = "terminated" if "(EXITED" in line else "active"
            sessions.append({"id": name, "name": name, "status": status})
        return sessions

    def get_session_windows(self, session_name: str) -> List[Dict[str, str]]:
        """Return Zellij tabs in the old service-level window shape."""
        try:
            result = self._zellij(session_name, "action", "list-tabs", "-j", "-a")
            tabs = json.loads(result.stdout or "[]")
            return [
                {"name": str(tab.get("name", "")), "index": str(tab.get("position", ""))}
                for tab in tabs
            ]
        except Exception as exc:
            logger.error("Failed to get Zellij tabs for %s: %s", session_name, exc)
            return []

    def kill_session(self, session_name: str) -> bool:
        try:
            self._run(["zellij", "delete-session", "-f", session_name])
            logger.info("Killed Zellij session: %s", session_name)
            return True
        except Exception as exc:
            logger.error("Failed to kill Zellij session %s: %s", session_name, exc)
            return False

    def kill_window(
        self,
        session_name: str,
        terminal_name: str,
        tab_id: Optional[int] = None,
    ) -> bool:
        try:
            if tab_id is None:
                pane = self._find_terminal_pane(session_name, terminal_name)
                tab_id = int(pane["tab_id"])
            self._zellij(session_name, "action", "close-tab-by-id", str(tab_id))
            logger.info("Killed Zellij tab %s:%s", session_name, tab_id)
            return True
        except Exception as exc:
            logger.error("Failed to kill Zellij tab %s:%s: %s", session_name, terminal_name, exc)
            return False

    def session_exists(self, session_name: str) -> bool:
        return any(session["id"] == session_name for session in self.list_sessions())

    def focus_tab(self, session_name: str, tab_id: int) -> None:
        """Focus the tab before attaching a user-facing PTY to the session."""
        self._zellij(session_name, "action", "go-to-tab-by-id", str(tab_id))

    def get_pane_working_directory(
        self,
        session_name: str,
        terminal_name: str,
        launch_working_directory: Optional[str] = None,
    ) -> Optional[str]:
        """Zellij CLI does not expose live pane cwd; return launch cwd."""
        if launch_working_directory is None and isinstance(terminal_name, ZellijTerminalInfo):
            launch_working_directory = terminal_name.launch_working_directory
        return launch_working_directory

    def start_log_subscription(
        self,
        terminal_id: str,
        session_name: str,
        pane_id: int,
        file_path: str,
    ) -> None:
        """Stream pane rendering updates into the per-terminal log file."""
        self.stop_log_subscription(terminal_id)
        log_file = open(file_path, "ab")
        try:
            process = subprocess.Popen(
                [
                    "zellij",
                    "--session",
                    session_name,
                    "subscribe",
                    "--pane-id",
                    str(pane_id),
                    "--scrollback",
                    str(TERMINAL_HISTORY_LINES),
                    "--ansi",
                ],
                stdout=log_file,
                stderr=subprocess.DEVNULL,
                env=self._build_env(),
                close_fds=True,
            )
            self._log_processes[terminal_id] = process
            logger.info("Started Zellij log subscription for terminal %s", terminal_id)
        finally:
            log_file.close()

    def stop_log_subscription(self, terminal_id: str) -> None:
        process = self._log_processes.pop(terminal_id, None)
        if process is None:
            return
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()


zellij_client = ZellijClient()
