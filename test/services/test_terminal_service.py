"""Unit tests for terminal service get_working_directory and send_special_key functions."""

from unittest.mock import patch

import pytest

from cli_agent_orchestrator.services.terminal_service import (
    get_working_directory,
    send_special_key,
)


class TestTerminalServiceWorkingDirectory:
    """Test terminal service working directory functionality."""

    @patch("cli_agent_orchestrator.services.terminal_service.zellij_client")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_working_directory_success(self, mock_get_metadata, mock_zellij_client):
        """Test successful working directory retrieval."""
        # Arrange
        terminal_id = "test-terminal-123"
        expected_dir = "/home/user/project"
        mock_get_metadata.return_value = {
            "session_name": "test-session",
            "terminal_name": "test-window",
            "launch_working_directory": expected_dir,
        }
        mock_zellij_client.get_pane_working_directory.return_value = expected_dir

        # Act
        result = get_working_directory(terminal_id)

        # Assert
        assert result == expected_dir
        mock_get_metadata.assert_called_once_with(terminal_id)
        mock_zellij_client.get_pane_working_directory.assert_called_once_with(
            "test-session", "test-window", expected_dir
        )

    @patch("cli_agent_orchestrator.services.terminal_service.zellij_client")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_working_directory_terminal_not_found(self, mock_get_metadata, mock_zellij_client):
        """Test ValueError when terminal not found."""
        # Arrange
        terminal_id = "nonexistent-terminal"
        mock_get_metadata.return_value = None

        # Act & Assert
        with pytest.raises(ValueError, match="Terminal 'nonexistent-terminal' not found"):
            get_working_directory(terminal_id)

        mock_get_metadata.assert_called_once_with(terminal_id)
        mock_zellij_client.get_pane_working_directory.assert_not_called()

    @patch("cli_agent_orchestrator.services.terminal_service.zellij_client")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_working_directory_returns_none(self, mock_get_metadata, mock_zellij_client):
        """Test when pane has no working directory."""
        # Arrange
        terminal_id = "test-terminal-456"
        mock_get_metadata.return_value = {
            "session_name": "test-session",
            "terminal_name": "test-window",
            "launch_working_directory": None,
        }
        mock_zellij_client.get_pane_working_directory.return_value = None

        # Act
        result = get_working_directory(terminal_id)

        # Assert
        assert result is None
        mock_get_metadata.assert_called_once_with(terminal_id)
        mock_zellij_client.get_pane_working_directory.assert_called_once_with(
            "test-session", "test-window", None
        )

    @patch("cli_agent_orchestrator.services.terminal_service.zellij_client")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_working_directory_returns_directory_from_Zellij_pane(
        self, mock_get_metadata, mock_zellij_client
    ):
        """Test that get_working_directory returns the directory obtained from Zellij pane."""
        # Arrange
        terminal_id = "test-terminal-789"
        pane_dir = "/workspace/my-project/src"
        mock_get_metadata.return_value = {
            "session_name": "cao-workspace",
            "terminal_name": "developer-xyz",
            "launch_working_directory": pane_dir,
        }
        mock_zellij_client.get_pane_working_directory.return_value = pane_dir

        # Act
        result = get_working_directory(terminal_id)

        # Assert
        assert result == pane_dir
        mock_zellij_client.get_pane_working_directory.assert_called_once_with(
            "cao-workspace", "developer-xyz", pane_dir
        )

    @patch("cli_agent_orchestrator.services.terminal_service.zellij_client")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_working_directory_raises_for_nonexistent_terminal(
        self, mock_get_metadata, mock_zellij_client
    ):
        """Test that get_working_directory raises ValueError for a terminal that does not exist."""
        # Arrange
        mock_get_metadata.return_value = None

        # Act & Assert
        with pytest.raises(ValueError, match="Terminal 'does-not-exist' not found"):
            get_working_directory("does-not-exist")

        mock_zellij_client.get_pane_working_directory.assert_not_called()


class TestSendSpecialKey:
    """Tests for send_special_key function."""

    @patch("cli_agent_orchestrator.services.terminal_service.update_last_active")
    @patch("cli_agent_orchestrator.services.terminal_service.zellij_client")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_send_special_key_sends_key_via_zellij_client(
        self, mock_get_metadata, mock_zellij_client, mock_update_last_active
    ):
        """Test that send_special_key sends the key via Zellij client."""
        # Arrange
        terminal_id = "test-terminal-001"
        mock_get_metadata.return_value = {
            "session_name": "cao-session",
            "terminal_name": "developer-abcd",
        }

        # Act
        result = send_special_key(terminal_id, "C-d")

        # Assert
        assert result is True
        mock_zellij_client.send_special_key.assert_called_once_with(
            "cao-session", "developer-abcd", "C-d"
        )
        mock_update_last_active.assert_called_once_with(terminal_id)

    @patch("cli_agent_orchestrator.services.terminal_service.update_last_active")
    @patch("cli_agent_orchestrator.services.terminal_service.zellij_client")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_send_special_key_ctrl_c(
        self, mock_get_metadata, mock_zellij_client, mock_update_last_active
    ):
        """Test that send_special_key can send C-c (Ctrl+C) to a terminal."""
        # Arrange
        terminal_id = "test-terminal-002"
        mock_get_metadata.return_value = {
            "session_name": "cao-session",
            "terminal_name": "reviewer-efgh",
        }

        # Act
        result = send_special_key(terminal_id, "C-c")

        # Assert
        assert result is True
        mock_zellij_client.send_special_key.assert_called_once_with(
            "cao-session", "reviewer-efgh", "C-c"
        )

    @patch("cli_agent_orchestrator.services.terminal_service.zellij_client")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_send_special_key_terminal_not_found(self, mock_get_metadata, mock_zellij_client):
        """Test that send_special_key raises ValueError when terminal not found."""
        # Arrange
        mock_get_metadata.return_value = None

        # Act & Assert
        with pytest.raises(ValueError, match="Terminal 'nonexistent' not found"):
            send_special_key("nonexistent", "C-d")

        mock_zellij_client.send_special_key.assert_not_called()

    @patch("cli_agent_orchestrator.services.terminal_service.zellij_client")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_send_special_key_propagates_Zellij_errors(self, mock_get_metadata, mock_zellij_client):
        """Test that send_special_key propagates exceptions from Zellij client."""
        # Arrange
        terminal_id = "test-terminal-003"
        mock_get_metadata.return_value = {
            "session_name": "cao-session",
            "terminal_name": "developer-ijkl",
        }
        mock_zellij_client.send_special_key.side_effect = Exception("Zellij send error")

        # Act & Assert
        with pytest.raises(Exception, match="Zellij send error"):
            send_special_key(terminal_id, "Escape")

    @patch("cli_agent_orchestrator.services.terminal_service.update_last_active")
    @patch("cli_agent_orchestrator.services.terminal_service.zellij_client")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_send_special_key_escape(
        self, mock_get_metadata, mock_zellij_client, mock_update_last_active
    ):
        """Test that send_special_key can send Escape key."""
        # Arrange
        terminal_id = "test-terminal-004"
        mock_get_metadata.return_value = {
            "session_name": "cao-session",
            "terminal_name": "developer-mnop",
        }

        # Act
        result = send_special_key(terminal_id, "Escape")

        # Assert
        assert result is True
        mock_zellij_client.send_special_key.assert_called_once_with(
            "cao-session", "developer-mnop", "Escape"
        )
