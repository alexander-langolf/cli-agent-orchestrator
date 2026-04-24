"""Tests for the Zellij terminal runtime client."""

import os
from subprocess import CompletedProcess
from unittest.mock import MagicMock, call, patch

import pytest

from cli_agent_orchestrator.clients.zellij import ZellijClient, ZellijTerminalInfo


@pytest.fixture
def client():
    return ZellijClient()


def completed(stdout: str = ""):
    return CompletedProcess(args=["zellij"], returncode=0, stdout=stdout, stderr="")


class TestResolveAndValidateWorkingDirectory:
    def test_defaults_to_cwd(self, client, tmp_path):
        with patch("os.getcwd", return_value=str(tmp_path)):
            result = client._resolve_and_validate_working_directory(None)
        assert result == os.path.realpath(str(tmp_path))

    def test_rejects_blocked_directory(self, client):
        with pytest.raises(ValueError, match="blocked system path"):
            client._resolve_and_validate_working_directory("/tmp")

    def test_rejects_missing_directory(self, client):
        with pytest.raises(ValueError, match="does not exist"):
            client._resolve_and_validate_working_directory("/missing/cao-zellij-dir")


class TestSessionAndTabCreation:
    def test_build_env_supplies_terminal_color_capabilities_for_detached_server(
        self, client, monkeypatch
    ):
        monkeypatch.setenv("TERM", "dumb")
        monkeypatch.setenv("NO_COLOR", "1")
        monkeypatch.delenv("COLORTERM", raising=False)

        env = client._build_env({"CAO_TERMINAL_ID": "abcd1234"})

        assert env["TERM"] == "xterm-256color"
        assert env["COLORTERM"] == "truecolor"
        assert "NO_COLOR" not in env
        assert env["CAO_TERMINAL_ID"] == "abcd1234"

    @patch("cli_agent_orchestrator.clients.zellij.subprocess.run")
    def test_create_session_creates_background_session_and_discovers_pane(
        self, mock_run, client, tmp_path
    ):
        mock_run.side_effect = [
            completed(),
            completed(),
            completed(
                '[{"id": 0, "is_plugin": false, "tab_id": 0, "tab_name": "developer-abcd"}]'
            ),
        ]

        result = client.create_session("cao-test", "developer-abcd", "abcd1234", str(tmp_path))

        assert result == ZellijTerminalInfo(
            name="developer-abcd",
            session_name="cao-test",
            tab_id=0,
            pane_id=0,
            launch_working_directory=os.path.realpath(str(tmp_path)),
        )
        assert mock_run.call_args_list[0] == call(
            ["zellij", "attach", "-b", "cao-test"],
            check=True,
            cwd=os.path.realpath(str(tmp_path)),
            env=client._build_env({"CAO_TERMINAL_ID": "abcd1234"}),
            capture_output=True,
            text=True,
        )
        assert mock_run.call_args_list[1] == call(
            [
                "zellij",
                "--session",
                "cao-test",
                "action",
                "rename-tab-by-id",
                "0",
                "developer-abcd",
            ],
            check=True,
            cwd=None,
            env=client._build_env(),
            capture_output=True,
            text=True,
        )

    @patch("cli_agent_orchestrator.clients.zellij.subprocess.run")
    def test_create_window_creates_new_tab_with_terminal_id(self, mock_run, client, tmp_path):
        mock_run.side_effect = [
            completed("7\n"),
            completed(
                '[{"id": 9, "is_plugin": false, "tab_id": 7, "tab_name": "reviewer-1234"}]'
            ),
        ]

        result = client.create_window("cao-test", "reviewer-1234", "deadbeef", str(tmp_path))

        assert result.tab_id == 7
        assert result.pane_id == 9
        create_call = mock_run.call_args_list[0]
        assert create_call.args[0][:7] == [
            "zellij",
            "--session",
            "cao-test",
            "action",
            "new-tab",
            "-n",
            "reviewer-1234",
        ]
        assert "CAO_TERMINAL_ID=deadbeef" in create_call.args[0]


class TestInputAndKeys:
    @patch("cli_agent_orchestrator.clients.zellij.time")
    @patch("cli_agent_orchestrator.clients.zellij.subprocess.run")
    def test_send_keys_uses_bracketed_paste_and_repeated_enter(self, mock_run, mock_time, client):
        mock_run.side_effect = [
            completed('[{"id": 4, "is_plugin": false, "tab_name": "developer-abcd"}]'),
            completed(),
            completed(),
            completed(),
        ]

        client.send_keys("cao-test", "developer-abcd", "hello", enter_count=2)

        assert mock_run.call_args_list[1].args[0] == [
            "zellij",
            "--session",
            "cao-test",
            "action",
            "paste",
            "-p",
            "4",
            "hello",
        ]
        assert mock_run.call_args_list[2].args[0] == [
            "zellij",
            "--session",
            "cao-test",
            "action",
            "send-keys",
            "-p",
            "4",
            "Enter",
        ]
        assert mock_run.call_args_list[3].args[0][-1] == "Enter"

    @patch("cli_agent_orchestrator.clients.zellij.subprocess.run")
    def test_send_special_key_maps_cao_key_names(self, mock_run, client):
        mock_run.side_effect = [
            completed('[{"id": 4, "is_plugin": false, "tab_name": "developer-abcd"}]'),
            completed(),
        ]

        client.send_special_key("cao-test", "developer-abcd", "C-d")

        assert mock_run.call_args_list[1].args[0] == [
            "zellij",
            "--session",
            "cao-test",
            "action",
            "send-keys",
            "-p",
            "4",
            "Ctrl d",
        ]

    @patch("cli_agent_orchestrator.clients.zellij.subprocess.run")
    def test_send_raw_bytes_writes_escape_sequence(self, mock_run, client):
        mock_run.side_effect = [
            completed('[{"id": 4, "is_plugin": false, "tab_name": "developer-abcd"}]'),
            completed(),
        ]

        client.send_raw_bytes("cao-test", "developer-abcd", b"\x1b[B")

        assert mock_run.call_args_list[1].args[0] == [
            "zellij",
            "--session",
            "cao-test",
            "action",
            "write",
            "-p",
            "4",
            "27",
            "91",
            "66",
        ]


class TestHistorySessionsAndCleanup:
    @patch("cli_agent_orchestrator.clients.zellij.subprocess.run")
    def test_get_history_dumps_screen_with_full_scrollback_and_tail(self, mock_run, client):
        mock_run.side_effect = [
            completed('[{"id": 4, "is_plugin": false, "tab_name": "developer-abcd"}]'),
            completed("one\ntwo\nthree\n"),
        ]

        result = client.get_history("cao-test", "developer-abcd", tail_lines=2)

        assert result == "two\nthree"
        assert mock_run.call_args_list[1].args[0] == [
            "zellij",
            "--session",
            "cao-test",
            "action",
            "dump-screen",
            "-p",
            "4",
            "--full",
            "--ansi",
        ]

    @patch("cli_agent_orchestrator.clients.zellij.subprocess.run")
    def test_list_sessions_marks_exited_sessions_terminated(self, mock_run, client):
        mock_run.return_value = completed(
            "cao-live [Created 1s ago]\n"
            "cao-old [Created 1s ago] (EXITED - attach to resurrect)\n"
        )

        result = client.list_sessions()

        assert result == [
            {"id": "cao-live", "name": "cao-live", "status": "active"},
            {"id": "cao-old", "name": "cao-old", "status": "terminated"},
        ]

    @patch("cli_agent_orchestrator.clients.zellij.subprocess.Popen")
    def test_start_log_subscription_stores_process_by_terminal(self, mock_popen, client, tmp_path):
        process = MagicMock()
        mock_popen.return_value = process

        client.start_log_subscription("terminal1", "cao-test", 4, str(tmp_path / "term.log"))

        assert client._log_processes["terminal1"] is process
        assert mock_popen.call_args.args[0] == [
            "zellij",
            "--session",
            "cao-test",
            "subscribe",
            "--pane-id",
            "4",
            "--scrollback",
            "200",
            "--ansi",
        ]

    @patch("cli_agent_orchestrator.clients.zellij.subprocess.Popen")
    def test_stop_log_subscription_terminates_process(self, mock_popen, client, tmp_path):
        process = MagicMock()
        mock_popen.return_value = process
        client.start_log_subscription("terminal1", "cao-test", 4, str(tmp_path / "term.log"))

        client.stop_log_subscription("terminal1")

        process.terminate.assert_called_once_with()
        assert "terminal1" not in client._log_processes
