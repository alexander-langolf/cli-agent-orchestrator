"""Tests for the kitty session runtime client.

CAO runs every session as a TAB inside one shared kitty instance reached
through a single app-level control socket (``cao.sock``). Agents are split
windows within their session's tab, addressed by a compound match that
qualifies the window name with its session.
"""

import json
import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.clients.tmux import KittyClient


def completed(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["kitten"], returncode=0, stdout=stdout, stderr="")


def failed() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["kitten"], returncode=1, stdout="", stderr="missing")


@pytest.fixture
def client(tmp_path) -> KittyClient:
    return KittyClient(
        kitty_command="kitty",
        kitten_command="kitten",
        socket_dir=tmp_path / "sockets",
    )


@pytest.fixture
def app_socket(client) -> str:
    """The single shared control socket address used for every command."""
    return f"unix:{client.socket_dir}/cao.sock"


def tree_with(*windows) -> str:
    """Build a kitty `ls` JSON tree containing the given window dicts."""
    return json.dumps([{"tabs": [{"windows": list(windows)}]}])


class TestResolveAndValidateWorkingDirectory:
    def test_defaults_to_cwd(self, client, tmp_path):
        with patch("os.getcwd", return_value=str(tmp_path)):
            result = client._resolve_and_validate_working_directory(None)
        assert result == os.path.realpath(str(tmp_path))

    def test_valid_directory(self, client, tmp_path):
        result = client._resolve_and_validate_working_directory(str(tmp_path))
        assert result == os.path.realpath(str(tmp_path))

    def test_blocked_root(self, client):
        with pytest.raises(ValueError, match="blocked system path"):
            client._resolve_and_validate_working_directory("/")

    def test_nonexistent_directory(self, client):
        with pytest.raises(ValueError, match="does not exist"):
            client._resolve_and_validate_working_directory("/nonexistent/dir/xyz")


class TestMatchExpressions:
    def test_window_match_is_qualified_by_session(self, client):
        assert (
            client._window_match("ses", "win")
            == "env:CAO_SESSION_NAME=ses and env:CAO_WINDOW_NAME=win"
        )

    def test_session_match(self, client):
        assert client._session_match("ses") == "env:CAO_SESSION_NAME=ses"

    def test_attach_command_focuses_window_via_compound_match(self, client, app_socket):
        assert client.attach_command("ses", "win") == [
            "kitten",
            "@",
            "--to",
            app_socket,
            "focus-window",
            "--match",
            "env:CAO_SESSION_NAME=ses and env:CAO_WINDOW_NAME=win",
        ]

    def test_attach_session_command_focuses_tab(self, client, app_socket):
        assert client.attach_session_command("ses") == [
            "kitten",
            "@",
            "--to",
            app_socket,
            "focus-tab",
            "--match",
            "env:CAO_SESSION_NAME=ses",
        ]


class TestCreateSession:
    @patch.object(KittyClient, "_wait_for_session_ready", lambda self, s: None)
    @patch.object(KittyClient, "_wait_for_app_ready", lambda self: None)
    @patch("cli_agent_orchestrator.clients.tmux.subprocess.run")
    @patch("cli_agent_orchestrator.clients.tmux.subprocess.Popen")
    def test_first_session_boots_shared_instance_via_session_file(
        self, mock_popen, mock_run, client, app_socket, tmp_path
    ):
        # No socket file on disk -> shared instance is not running -> boot it.
        result = client.create_session("ses", "my-window", "tid1", str(tmp_path))

        assert result == "my-window"
        session_file = client.session_file_path("ses")
        assert session_file.exists()
        session_text = session_file.read_text(encoding="utf-8")
        assert "os_window_title CAO" in session_text
        assert "new_tab ses" in session_text
        assert "layout tall:bias=65" in session_text
        assert f"cd {tmp_path}" in session_text
        assert "launch --title my-window" in session_text
        assert "--env CAO_SESSION_NAME=ses" in session_text
        assert "--env CAO_WINDOW_NAME=my-window" in session_text
        assert "--env CAO_TERMINAL_ID=tid1" in session_text

        assert mock_popen.call_args.args[0] == [
            "kitty",
            "--detach",
            "--listen-on",
            app_socket,
            "-o",
            "allow_remote_control=socket-only",
            "--session",
            str(session_file),
        ]

    @patch.object(KittyClient, "_base_environment", lambda self, tid, extra: {"CAO_TERMINAL_ID": tid})
    @patch.object(KittyClient, "_wait_for_session_ready", lambda self, s: None)
    @patch.object(KittyClient, "_app_running", lambda self: True)
    @patch("cli_agent_orchestrator.clients.tmux.subprocess.run")
    @patch("cli_agent_orchestrator.clients.tmux.subprocess.Popen")
    def test_subsequent_session_adds_tab_to_running_instance(
        self, mock_popen, mock_run, client, app_socket, tmp_path
    ):
        mock_run.side_effect = [completed(), completed()]

        result = client.create_session("ses", "my-window", "tid1", str(tmp_path))

        assert result == "my-window"
        # A running instance is reused: no new kitty process is spawned.
        mock_popen.assert_not_called()
        launch = mock_run.call_args_list[0].args[0]
        # Base env (here just CAO_TERMINAL_ID) is forwarded, then the session
        # adds CAO_SESSION_NAME / CAO_WINDOW_NAME.
        assert launch == [
            "kitten",
            "@",
            "--to",
            app_socket,
            "launch",
            "--type=tab",
            "--tab-title",
            "ses",
            "--title",
            "my-window",
            "--cwd",
            str(tmp_path),
            "--var",
            "cao_session=ses",
            "--var",
            "cao_window=my-window",
            "--env",
            "CAO_TERMINAL_ID=tid1",
            "--env",
            "CAO_SESSION_NAME=ses",
            "--env",
            "CAO_WINDOW_NAME=my-window",
        ]
        goto_layout = mock_run.call_args_list[1].args[0]
        assert goto_layout == [
            "kitten",
            "@",
            "--to",
            app_socket,
            "goto-layout",
            "--match",
            "env:CAO_SESSION_NAME=ses",
            "tall:bias=65",
        ]


class TestCreateWindow:
    @patch.object(KittyClient, "session_exists", lambda self, s: True)
    @patch("cli_agent_orchestrator.clients.tmux.subprocess.run")
    def test_create_window_splits_into_session_tab(
        self, mock_run, client, app_socket, tmp_path
    ):
        mock_run.return_value = completed("42\n")

        result = client.create_window("ses", "agent-window", "tid2", str(tmp_path))

        assert result == "agent-window"
        assert mock_run.call_args.args[0] == [
            "kitten",
            "@",
            "--to",
            app_socket,
            "launch",
            "--type=window",
            "--match",
            "env:CAO_SESSION_NAME=ses",
            "--title",
            "agent-window",
            "--cwd",
            str(tmp_path),
            "--var",
            "cao_window=agent-window",
            "--env",
            "CAO_SESSION_NAME=ses",
            "--env",
            "CAO_WINDOW_NAME=agent-window",
            "--env",
            "CAO_TERMINAL_ID=tid2",
        ]

    @patch.object(KittyClient, "session_exists", lambda self, s: False)
    def test_create_window_session_not_found(self, client, tmp_path):
        with pytest.raises(ValueError, match="not found"):
            client.create_window("missing", "w", "tid2", str(tmp_path))


class TestSendKeys:
    @patch("cli_agent_orchestrator.clients.tmux.time")
    @patch("cli_agent_orchestrator.clients.tmux.subprocess.run")
    def test_send_keys_sends_stdin_text_then_enter_keys(
        self, mock_run, mock_time, client, app_socket
    ):
        mock_run.return_value = completed()

        client.send_keys("ses", "win", "hello", enter_count=2)

        commands = [entry.args[0] for entry in mock_run.call_args_list]
        assert commands[0] == [
            "kitten",
            "@",
            "--to",
            app_socket,
            "send-text",
            "--match",
            "env:CAO_SESSION_NAME=ses and env:CAO_WINDOW_NAME=win",
            "--stdin",
            "--bracketed-paste",
            "disable",
        ]
        assert mock_run.call_args_list[0].kwargs["input"] == "hello"
        assert commands[1] == [
            "kitten",
            "@",
            "--to",
            app_socket,
            "send-key",
            "--match",
            "env:CAO_SESSION_NAME=ses and env:CAO_WINDOW_NAME=win",
            "enter",
        ]
        assert commands[2] == commands[1]


class TestSendSpecialKey:
    @patch("cli_agent_orchestrator.clients.tmux.subprocess.run")
    def test_send_special_key_uses_kitty_send_key(self, mock_run, client, app_socket):
        mock_run.return_value = completed()

        client.send_special_key("ses", "win", "C-d")

        assert mock_run.call_args.args[0] == [
            "kitten",
            "@",
            "--to",
            app_socket,
            "send-key",
            "--match",
            "env:CAO_SESSION_NAME=ses and env:CAO_WINDOW_NAME=win",
            "ctrl+d",
        ]

    @patch("cli_agent_orchestrator.clients.tmux.subprocess.run")
    def test_send_literal_keys_uses_literal_flag(self, mock_run, client, app_socket):
        mock_run.return_value = completed()

        client.send_literal_keys("ses", "win", "\x1b[B")

        assert mock_run.call_args.args[0] == [
            "kitten",
            "@",
            "--to",
            app_socket,
            "send-text",
            "--match",
            "env:CAO_SESSION_NAME=ses and env:CAO_WINDOW_NAME=win",
            "--stdin",
            "--bracketed-paste",
            "disable",
        ]
        assert mock_run.call_args.kwargs["input"] == "\x1b[B"


class TestHistorySessionsAndWindows:
    @patch("cli_agent_orchestrator.clients.tmux.subprocess.run")
    def test_get_history_captures_ansi_tail(self, mock_run, client, app_socket):
        mock_run.return_value = completed("line1\nline2\nline3\n")

        result = client.get_history("ses", "win", tail_lines=2)

        assert result == "line2\nline3"
        assert mock_run.call_args.args[0] == [
            "kitten",
            "@",
            "--to",
            app_socket,
            "get-text",
            "--match",
            "env:CAO_SESSION_NAME=ses and env:CAO_WINDOW_NAME=win",
            "--extent",
            "all",
            "--ansi",
        ]

    @patch.object(KittyClient, "_app_running", lambda self: True)
    @patch("cli_agent_orchestrator.clients.tmux.subprocess.run")
    def test_list_sessions_groups_tabs_by_session_env(self, mock_run, client):
        mock_run.return_value = completed(
            tree_with(
                {"title": "supervisor", "env": {"CAO_SESSION_NAME": "cao-two"}},
                {"title": "supervisor", "env": {"CAO_SESSION_NAME": "cao-one"}},
                {"title": "worker", "env": {"CAO_SESSION_NAME": "cao-one"}},
            )
        )

        result = client.list_sessions()

        assert result == [
            {"id": "cao-one", "name": "cao-one", "status": "active"},
            {"id": "cao-two", "name": "cao-two", "status": "active"},
        ]

    @patch("cli_agent_orchestrator.clients.tmux.subprocess.run")
    def test_get_session_windows_filters_to_session(self, mock_run, client):
        mock_run.return_value = completed(
            tree_with(
                {"title": "worker", "env": {"CAO_SESSION_NAME": "ses", "CAO_WINDOW_NAME": "worker"}},
                {"title": "other", "env": {"CAO_SESSION_NAME": "other-ses", "CAO_WINDOW_NAME": "x"}},
                {"title": "reviewer", "env": {"CAO_SESSION_NAME": "ses", "CAO_WINDOW_NAME": "reviewer"}},
            )
        )

        result = client.get_session_windows("ses")

        assert result == [{"name": "worker", "index": "0"}, {"name": "reviewer", "index": "1"}]


class TestCleanupAndPaneMetadata:
    @patch.object(KittyClient, "_app_running", lambda self: True)
    @patch("cli_agent_orchestrator.clients.tmux.subprocess.run")
    def test_session_exists_true_when_tab_present(self, mock_run, client):
        mock_run.return_value = completed(
            tree_with({"title": "supervisor", "env": {"CAO_SESSION_NAME": "ses"}})
        )

        assert client.session_exists("ses") is True

    @patch.object(KittyClient, "_app_running", lambda self: True)
    @patch("cli_agent_orchestrator.clients.tmux.subprocess.run")
    def test_session_exists_false_when_tab_absent(self, mock_run, client):
        mock_run.return_value = completed(
            tree_with({"title": "supervisor", "env": {"CAO_SESSION_NAME": "other"}})
        )

        assert client.session_exists("ses") is False

    @patch.object(KittyClient, "_app_running", lambda self: False)
    def test_session_exists_false_when_instance_down(self, client):
        assert client.session_exists("ses") is False

    @patch("cli_agent_orchestrator.clients.tmux.subprocess.run")
    def test_get_pane_working_directory_uses_kitty_ls(self, mock_run, client):
        mock_run.return_value = completed(
            tree_with(
                {
                    "title": "win",
                    "cwd": "/tmp/project",
                    "env": {"CAO_SESSION_NAME": "ses", "CAO_WINDOW_NAME": "win"},
                }
            )
        )

        assert client.get_pane_working_directory("ses", "win") == "/tmp/project"

    @patch("cli_agent_orchestrator.clients.tmux.subprocess.run")
    def test_get_pane_working_directory_prefers_foreground_process_cwd(self, mock_run, client):
        mock_run.return_value = completed(
            tree_with(
                {
                    "title": "win",
                    "cwd": "/tmp/project",
                    "env": {"CAO_SESSION_NAME": "ses", "CAO_WINDOW_NAME": "win"},
                    "foreground_processes": [{"cwd": "/tmp/project/subdir"}],
                }
            )
        )

        assert client.get_pane_working_directory("ses", "win") == "/tmp/project/subdir"

    @patch.object(KittyClient, "_app_running", lambda self: True)
    @patch("cli_agent_orchestrator.clients.tmux.subprocess.run")
    def test_kill_session_closes_tab_by_session_match(self, mock_run, client, app_socket):
        mock_run.return_value = completed()

        # _app_running is patched True throughout, so the post-close socket
        # cleanup branch is skipped; assert the close-window call shape.
        client.kill_session("ses")

        assert mock_run.call_args_list[0].args[0] == [
            "kitten",
            "@",
            "--to",
            app_socket,
            "close-window",
            "--match",
            "env:CAO_SESSION_NAME=ses",
            "--ignore-no-match",
        ]

    @patch("cli_agent_orchestrator.clients.tmux.threading.Thread")
    def test_pipe_pane_starts_kitty_log_mirror(self, mock_thread_cls, client, tmp_path):
        mock_thread = MagicMock()
        mock_thread.is_alive.return_value = False
        mock_thread_cls.return_value = mock_thread
        log_path = tmp_path / "with space" / "log.txt"

        client.pipe_pane("ses", "win", str(log_path))

        assert log_path.exists()
        assert ("ses", "win") in client._log_pipes
        mock_thread.start.assert_called_once()

    def test_stop_pipe_pane_stops_kitty_log_mirror(self, client):
        stop_event = MagicMock()
        thread = MagicMock()
        thread.is_alive.return_value = True
        client._log_pipes[("ses", "win")] = (stop_event, thread)

        client.stop_pipe_pane("ses", "win")

        stop_event.set.assert_called_once()
        thread.join.assert_called_once_with(timeout=1.0)
        assert ("ses", "win") not in client._log_pipes
