import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import app


class GameStartupTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.agent = app.Agent(self.root / "data")
        tool_path = self.root / "BetterGI.exe"
        tool_path.touch()
        game_path = self.root / "GenshinImpact.exe"
        game_path.touch()
        game = next(item for item in self.agent.games if item["id"] == "genshin")
        game["exePath"] = str(tool_path)
        game["gameExePath"] = str(game_path)
        self.game = game
        self.agent.state.update({
            "status": "running",
            "queue": ["genshin"],
            "completed": [],
            "message": "测试中",
        })
        self.tool_process = Mock()
        self.tool_process.wait.return_value = 0

    def test_existing_game_skips_game_launch(self):
        with (
            patch.object(app.Agent, "find_running_game_process", return_value="GenshinImpact.exe"),
            patch.object(app.Agent, "_launch_game") as launch_game,
            patch("app.subprocess.Popen", return_value=self.tool_process) as popen,
        ):
            self.agent._run_queue(["genshin"])

        launch_game.assert_not_called()
        self.assertEqual(popen.call_args.args[0], [self.game["exePath"]])
        self.assertEqual(self.agent.state["status"], "succeeded")

    def test_missing_game_is_started_before_tool(self):
        with (
            patch.object(app.Agent, "find_running_game_process", side_effect=[None, str(self.game["gameExePath"])]),
            patch.object(app.Agent, "_launch_game") as launch_game,
            patch("app.subprocess.Popen", return_value=self.tool_process) as popen,
        ):
            self.agent._run_queue(["genshin"])

        launch_game.assert_called_once_with(Path(self.game["gameExePath"]))
        self.assertEqual(popen.call_args.args[0], [self.game["exePath"]])
        self.assertEqual(self.agent.state["status"], "succeeded")

    def test_missing_game_path_stops_before_tool(self):
        self.game["gameExePath"] = ""
        with (
            patch.object(app.Agent, "find_running_game_process", return_value=None),
            patch("app.subprocess.Popen") as popen,
        ):
            self.agent._run_queue(["genshin"])

        popen.assert_not_called()
        self.assertEqual(self.agent.state["status"], "failed")
        self.assertIn("未检测到原神", self.agent.state["message"])

    def test_wuthering_generic_unreal_process_requires_matching_install_path(self):
        game = {"id": "wuthering", "gameExePath": ""}
        with patch.object(
            app.Agent,
            "_running_processes",
            return_value=[("Client-Win64-Shipping.exe", r"C:\Games\OtherGame\Client-Win64-Shipping.exe")],
        ):
            self.assertIsNone(app.Agent.find_running_game_process(game))

        with patch.object(
            app.Agent,
            "_running_processes",
            return_value=[("Client-Win64-Shipping.exe", r"C:\Games\Wuthering Waves Game\Client-Win64-Shipping.exe")],
        ):
            self.assertIn("Wuthering Waves", app.Agent.find_running_game_process(game))


if __name__ == "__main__":
    unittest.main()
