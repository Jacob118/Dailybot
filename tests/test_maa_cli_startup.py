import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app import Agent, GameStartupError


class FakeProcess:
    def __init__(self, exit_code=0):
        self.exit_code = exit_code

    def wait(self, timeout=None):
        return self.exit_code


class MaaCliStartupTests(unittest.TestCase):
    def make_agent(self, directory):
        agent = Agent.__new__(Agent)
        agent.lock = threading.RLock()
        agent.cancel_requested = False
        agent.process = None
        agent.state = {"status": "running", "message": ""}
        agent.logs_dir = Path(directory)
        return agent

    def test_startup_uses_selected_client_and_returns_on_success(self):
        with tempfile.TemporaryDirectory() as directory:
            cli_path = Path(directory) / "maa-cli.exe"
            cli_path.touch()
            agent = self.make_agent(directory)
            game = {"maaCliPath": str(cli_path), "maaClientType": "Bilibili"}
            with (Path(directory) / "run.log").open("ab") as log_file:
                with patch("app.subprocess.Popen", return_value=FakeProcess()) as popen:
                    self.assertTrue(agent._run_maa_cli_startup(game, log_file))

            self.assertEqual(popen.call_args.args[0], [str(cli_path), "startup", "Bilibili"])
            self.assertIsNone(agent.process)

    def test_startup_failure_stops_before_queue_continues(self):
        with tempfile.TemporaryDirectory() as directory:
            cli_path = Path(directory) / "maa.exe"
            cli_path.touch()
            agent = self.make_agent(directory)
            game = {"maaCliPath": str(cli_path), "maaClientType": "Official"}
            with (Path(directory) / "run.log").open("ab") as log_file:
                with patch("app.subprocess.Popen", return_value=FakeProcess(5)):
                    with self.assertRaisesRegex(GameStartupError, "退出码 5"):
                        agent._run_maa_cli_startup(game, log_file)

            self.assertIsNone(agent.process)

    def test_queue_uses_maa_startup_instead_of_direct_game_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            cli_path = Path(directory) / "maa-cli.exe"
            gui_path = Path(directory) / "MAA.exe"
            cli_path.touch()
            gui_path.touch()
            agent = self.make_agent(directory)
            agent.games = [{
                "id": "arknights",
                "name": "明日方舟",
                "tool": "MAA",
                "exePath": str(gui_path),
                "gameExePath": str(Path(directory) / "Arknights.exe"),
                "maaCliPath": str(cli_path),
                "maaClientType": "Official",
                "args": [],
                "timeoutSeconds": 30,
            }]
            agent.state.update({
                "currentGameId": None,
                "currentGameName": None,
                "queue": ["arknights"],
                "completed": [],
                "logPath": None,
                "exitCode": None,
            })
            with patch.object(agent, "_run_maa_cli_startup", return_value=True) as maa_startup, \
                    patch.object(agent, "find_running_game_process") as find_game, \
                    patch.object(agent, "_launch_game") as direct_launch, \
                    patch("app.subprocess.Popen", return_value=FakeProcess()):
                agent._run_queue(["arknights"])

            maa_startup.assert_called_once()
            find_game.assert_not_called()
            direct_launch.assert_not_called()
            self.assertEqual(agent.state["status"], "succeeded")


if __name__ == "__main__":
    unittest.main()
