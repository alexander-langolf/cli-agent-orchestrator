"""Tests for the kitty session runtime client."""

import json
import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.clients.tmux import KittyClient


def completed(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=["kitten"], returncode=0, stdout=stdout, stderr="")


@pytest.fixture
def client(tmp_path) -> KittyClient:
    return KittyClient(
        kitty_command="kitty",
        kitten_command="kitten",
        socket_dir=tmp_path / "sockets",
    )


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


class TestCreateSession:
    def test_attach_command_focuses_kitty_window(self, client):
        assert client.attach_command("ses", "win") == [
            "kitten",
            "@",
            "--to",
            f"unix:{client.socket_dir}/ses.sock",
            "focus-window",
            "--match",
            "env:CAO_WINDOW_NAME=win",
        ]

    def test_attach_session_command_focuses_kitty_session(self, client):
        assert client.attach_session_command("ses") == [
            "kitten",
            "@",
            "--to",
            f"unix:{client.socket_dir}/ses.sock",
            "focus-window",
            "--match",
            "env:CAO_SESSION_NAME=ses",
        ]

    @patch("cli_agent_orchestrator.clients.tmux.subprocess.run")
    @patch("cli_agent_orchestrator.clients.tmux.subprocess.Popen")
    def test_create_session_writes_kitty_session_file_and_launches_socket(
        self, mock_popen, mock_run, client, tmp_path
    ):
        mock_run.return_value = completed("[]")

        result = client.create_session("ses", "my-window", "tid1", str(tmp_path))

        assert result == "my-window"
        session_file = client.session_file_path("ses")
        assert session_file.exists()
        session_text = session_file.read_text(encoding="utf-8")
        assert "os_window_title ses" in session_text
        assert "os_window_size 220c 50c" in session_text
        assert f"cd {tmp_path}" in session_text
        assert "launch --title my-window" in session_text
        assert "--env CAO_SESSION_NAME=ses" in session_text
        assert "--env CAO_WINDOW_NAME=my-window" in session_text
        assert "--env CAO_TERMINAL_ID=tid1" in session_text

        command = mock_popen.call_args.args[0]
        assert command == [
            "kitty",
            "--detach",
            "--listen-on",
            f"unix:{client.socket_dir}/ses.sock",
            "-o",
            "allow_remote_control=socket-only",
            "--session",
            str(session_file),
        ]
        assert mock_run.call_args.args[0] == [
            "kitten",
            "@",
            "--to",
            f"unix:{client.socket_dir}/ses.sock",
            "ls",
        ]


class TestCreateWindow:
    @patch("cli_agent_orchestrator.clients.tmux.subprocess.run")
    def test_create_window_checks_session_and_returns_formatted_window_name(
        self, mock_run, client, tmp_path
    ):
        mock_run.side_effect = [completed("[]"), completed("42\n")]

        result = client.create_window("ses", "agent-window", "tid2", str(tmp_path))

        assert result == "agent-window"
        assert mock_run.call_args_list[0].args[0] == [
            "kitten",
            "@",
            "--to",
            f"unix:{client.socket_dir}/ses.sock",
            "ls",
        ]
        command = mock_run.call_args_list[1].args[0]
        assert command == [
            "kitten",
            "@",
            "--to",
            f"unix:{client.socket_dir}/ses.sock",
            "launch",
            "--type=window",
            "--title",
            "agent-window",
            "--cwd",
            str(tmp_path),
            "--add-to-session",
            ".",
            "--env",
            "CAO_SESSION_NAME=ses",
            "--env",
            "CAO_WINDOW_NAME=agent-window",
            "--env",
            "CAO_TERMINAL_ID=tid2",
        ]

    @patch("cli_agent_orchestrator.clients.tmux.subprocess.run")
    def test_create_window_session_not_found(self, mock_run, client, tmp_path):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["kitten"], returncode=1, stdout="", stderr="missing"
        )

        with pytest.raises(ValueError, match="not found"):
            client.create_window("missing", "w", "tid2", str(tmp_path))


class TestSendKeys:
    @patch("cli_agent_orchestrator.clients.tmux.time")
    @patch("cli_agent_orchestrator.clients.tmux.subprocess.run")
    def test_send_keys_sends_stdin_text_then_enter_keys(
        self, mock_run, mock_time, client
    ):
        mock_run.return_value = completed()

        client.send_keys("ses", "win", "hello", enter_count=2)

        commands = [entry.args[0] for entry in mock_run.call_args_list]
        assert commands[0] == [
            "kitten",
            "@",
            "--to",
            f"unix:{client.socket_dir}/ses.sock",
            "send-text",
            "--match",
            "env:CAO_WINDOW_NAME=win",
            "--stdin",
            "--bracketed-paste",
            "disable",
        ]
        assert mock_run.call_args_list[0].kwargs["input"] == "hello"
        assert commands[1] == [
            "kitten",
            "@",
            "--to",
            f"unix:{client.socket_dir}/ses.sock",
            "send-key",
            "--match",
            "env:CAO_WINDOW_NAME=win",
            "enter",
        ]
        assert commands[2] == commands[1]


class TestSendSpecialKey:
    @patch("cli_agent_orchestrator.clients.tmux.subprocess.run")
    def test_send_special_key_uses_kitty_send_key(self, mock_run, client):
        mock_run.return_value = completed()

        client.send_special_key("ses", "win", "C-d")

        assert mock_run.call_args.args[0] == [
            "kitten",
            "@",
            "--to",
            f"unix:{client.socket_dir}/ses.sock",
            "send-key",
            "--match",
            "env:CAO_WINDOW_NAME=win",
            "ctrl+d",
        ]

    @patch("cli_agent_orchestrator.clients.tmux.subprocess.run")
    def test_send_literal_keys_uses_literal_flag(self, mock_run, client):
        mock_run.return_value = completed()

        client.send_literal_keys("ses", "win", "\x1b[B")

        assert mock_run.call_args.args[0] == [
            "kitten",
            "@",
            "--to",
            f"unix:{client.socket_dir}/ses.sock",
            "send-text",
            "--match",
            "env:CAO_WINDOW_NAME=win",
            "--stdin",
            "--bracketed-paste",
            "disable",
        ]
        assert mock_run.call_args.kwargs["input"] == "\x1b[B"


class TestHistorySessionsAndWindows:
    @patch("cli_agent_orchestrator.clients.tmux.subprocess.run")
    def test_get_history_captures_ansi_tail(self, mock_run, client):
        mock_run.return_value = completed("line1\nline2\nline3\n")

        result = client.get_history("ses", "win", tail_lines=2)

        assert result == "line2\nline3"
        assert mock_run.call_args.args[0] == [
            "kitten",
            "@",
            "--to",
            f"unix:{client.socket_dir}/ses.sock",
            "get-text",
            "--match",
            "env:CAO_WINDOW_NAME=win",
            "--extent",
            "all",
            "--ansi",
        ]

    @patch("cli_agent_orchestrator.clients.tmux.subprocess.run")
    def test_list_sessions_discovers_kitty_socket_files(self, mock_run, client):
        client.socket_dir.mkdir(parents=True)
        (client.socket_dir / "cao-one.sock").touch()
        (client.socket_dir / "cao-two.sock").touch()
        mock_run.return_value = completed("[]")

        result = client.list_sessions()

        assert result == [
            {"id": "cao-one", "name": "cao-one", "status": "active"},
            {"id": "cao-two", "name": "cao-two", "status": "active"},
        ]

    @patch("cli_agent_orchestrator.clients.tmux.subprocess.run")
    def test_get_session_windows_parses_kitty_json(self, mock_run, client):
        mock_run.return_value = completed(
            json.dumps(
                [
                    {
                        "tabs": [
                            {
                                "windows": [
                                    {"title": "worker", "env": {"CAO_WINDOW_NAME": "worker"}},
                                    {"title": "reviewer", "env": {"CAO_WINDOW_NAME": "reviewer"}},
                                ]
                            }
                        ]
                    }
                ]
            )
        )

        result = client.get_session_windows("ses")

        assert result == [{"name": "worker", "index": "0"}, {"name": "reviewer", "index": "1"}]


class TestCleanupAndPaneMetadata:
    @patch("cli_agent_orchestrator.clients.tmux.subprocess.run")
    def test_session_exists_uses_kitty_socket_returncode(self, mock_run, client):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["kitten"], returncode=0, stdout="[]", stderr=""
        )

        assert client.session_exists("ses") is True

    @patch("cli_agent_orchestrator.clients.tmux.subprocess.run")
    def test_get_pane_working_directory_uses_kitty_ls(self, mock_run, client):
        mock_run.return_value = completed(
            json.dumps([{"tabs": [{"windows": [{"title": "win", "cwd": "/tmp/project"}]}]}])
        )

        assert client.get_pane_working_directory("ses", "win") == "/tmp/project"

    @patch("cli_agent_orchestrator.clients.tmux.subprocess.run")
    def test_get_pane_working_directory_prefers_foreground_process_cwd(self, mock_run, client):
        mock_run.return_value = completed(
            json.dumps(
                [
                    {
                        "tabs": [
                            {
                                "windows": [
                                    {
                                        "title": "win",
                                        "cwd": "/tmp/project",
                                        "foreground_processes": [{"cwd": "/tmp/project/subdir"}],
                                    }
                                ]
                            }
                        ]
                    }
                ]
            )
        )

        assert client.get_pane_working_directory("ses", "win") == "/tmp/project/subdir"

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
