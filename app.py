#!/usr/bin/env python3
"""DailyBot local agent: a loopback-only dashboard and game-aware tool runner."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import signal
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from typing import Any


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
GAME_CATALOG = [
    {"id": "arknights", "name": "明日方舟", "tool": "MAA", "short": "AK", "downloadUrl": "https://github.com/MaaAssistantArknights/MaaAssistantArknights/releases", "repo": "MaaAssistantArknights/MaaAssistantArknights"},
    {"id": "endfield", "name": "明日方舟：终末地", "tool": "MAAEnd", "short": "ME", "downloadUrl": "https://github.com/MaaEnd/MaaEnd/releases", "repo": "MaaEnd/MaaEnd"},
    {"id": "genshin", "name": "原神", "tool": "BetterGI", "short": "GI", "downloadUrl": "https://github.com/babalae/better-genshin-impact/releases", "repo": "babalae/better-genshin-impact"},
    {"id": "wuthering", "name": "鸣潮", "tool": "OK-WW", "short": "WW", "downloadUrl": "https://github.com/ok-oldking/ok-wuthering-waves/releases", "repo": "ok-oldking/ok-wuthering-waves"},
]
TOOL_EXECUTABLES = {
    "arknights": {"maa.exe"},
    "endfield": {"maaend.exe"},
    "genshin": {"bettergi.exe"},
    "wuthering": {"ok-ww.exe"},
}
GAME_PROCESS_NAMES = {
    "arknights": {"arknights.exe"},
    "endfield": {"endfield.exe"},
    "genshin": {"yuanshen.exe", "genshinimpact.exe"},
    "wuthering": {"client-win64-shipping.exe"},
}
GAME_START_TIMEOUT_SECONDS = 120
MAA_STARTUP_TIMEOUT_SECONDS = 600
MAA_CLIENT_TYPES = {"Official", "Bilibili", "txwy", "YoStarEN", "YoStarJP", "YoStarKR"}
TOOL_REGISTRY_NAMES = {
    "genshin": ("bettergi", "better genshin impact"),
    "wuthering": ("ok-ww", "ok-wuthering-waves", "ok wuthering waves"),
}
MAX_BODY = 256 * 1024
MAX_RELEASE_METADATA = 8 * 1024 * 1024
MAX_DOWNLOAD_SIZE = 2 * 1024 * 1024 * 1024
MAX_TOOL_EXPANDED_SIZE = 2 * 1024 * 1024 * 1024
GITHUB_API = "https://api.github.com/repos/{repo}/releases/latest"
GITHUB_DOWNLOAD_HOSTS = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
DAILYBOT_GITHUB_REPO = "Jacob118/Dailybot"
APP_VERSION = "0.3.1"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def version_tuple(value: str) -> tuple[int, int, int, int] | None:
    match = re.fullmatch(r"v?(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:\.(\d+))?", value.strip(), re.IGNORECASE)
    if not match:
        return None
    return tuple(int(part or 0) for part in match.groups())


class DownloadCancelled(Exception):
    pass


class GameStartupError(Exception):
    pass


class GitHubRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request: Any, file_pointer: Any, code: int, message: str, headers: Any, new_url: str) -> Any:
        parsed = urllib.parse.urlparse(new_url)
        if parsed.scheme != "https" or parsed.hostname not in GITHUB_DOWNLOAD_HOSTS:
            raise RuntimeError("下载被重定向到非 GitHub 文件服务器，已停止")
        return super().redirect_request(request, file_pointer, code, message, headers, new_url)


class Agent:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.config_path = data_dir / "config.json"
        self.logs_dir = data_dir / "logs"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.cancel_requested = False
        self.install_cancel = threading.Event()
        self.installer_state: dict[str, Any] = {
            "status": "idle",
            "gameId": None,
            "message": "",
            "progress": None,
            "bytesDownloaded": 0,
            "totalBytes": None,
            "releaseVersion": None,
            "assetName": None,
            "sha256": None,
            "digestVerified": None,
            "updatedAt": None,
        }
        self.process: subprocess.Popen[bytes] | None = None
        self.state: dict[str, Any] = {
            "status": "idle",
            "startedAt": None,
            "finishedAt": None,
            "currentGameId": None,
            "currentGameName": None,
            "queue": [],
            "completed": [],
            "message": "等待开始",
            "logPath": None,
            "exitCode": None,
        }
        self.tool_detection: dict[str, dict[str, str]] = {}
        self.games = self.load_config()
        self.detect_installed_tools()
        self.update_state: dict[str, Any] = {
            "status": "idle",
            "currentVersion": APP_VERSION,
            "latestVersion": None,
            "releaseUrl": f"https://github.com/{DAILYBOT_GITHUB_REPO}/releases",
            "releaseNotes": "",
            "checkedAt": None,
            "message": "尚未检查更新",
        }

    def check_for_updates(self) -> dict[str, Any]:
        request = urllib.request.Request(
            GITHUB_API.format(repo=DAILYBOT_GITHUB_REPO),
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": f"DailyBot/{APP_VERSION}",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=8) as response:
                raw = response.read(MAX_RELEASE_METADATA + 1)
            if len(raw) > MAX_RELEASE_METADATA:
                raise ValueError("GitHub 发布信息过大")
            release = json.loads(raw.decode("utf-8"))
            if not isinstance(release, dict):
                raise ValueError("GitHub 返回的发布信息无效")
            tag = release.get("tag_name")
            latest = version_tuple(tag) if isinstance(tag, str) else None
            current = version_tuple(APP_VERSION)
            if latest is None or current is None:
                raise ValueError("版本号格式无效")
            release_url = f"https://github.com/{DAILYBOT_GITHUB_REPO}/releases/tag/{urllib.parse.quote(tag, safe='-._')}"
            newer = latest > current
            result = {
                "status": "update-available" if newer else "up-to-date",
                "currentVersion": APP_VERSION,
                "latestVersion": tag.removeprefix("v"),
                "releaseUrl": release_url,
                "releaseNotes": str(release.get("body") or "")[:3000],
                "checkedAt": now_iso(),
                "message": f"发现新版本 {tag.removeprefix('v')}（当前 {APP_VERSION}）" if newer else f"当前已是最新版本 {APP_VERSION}",
            }
        except HTTPError as exc:
            if exc.code == 404:
                result = {
                    "status": "no-release",
                    "currentVersion": APP_VERSION,
                    "latestVersion": None,
                    "releaseUrl": f"https://github.com/{DAILYBOT_GITHUB_REPO}/releases",
                    "releaseNotes": "",
                    "checkedAt": now_iso(),
                    "message": f"当前版本 {APP_VERSION}；DailyBot 还没有发布稳定版",
                }
            else:
                result = self._update_check_error(f"GitHub 暂时无法检查更新（HTTP {exc.code}）")
        except (OSError, URLError, TimeoutError):
            result = self._update_check_error("无法连接 GitHub，请检查网络后重试")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            result = self._update_check_error(f"读取版本信息失败：{exc}")
        with self.lock:
            self.update_state = result
            return dict(result)

    @staticmethod
    def _update_check_error(message: str) -> dict[str, Any]:
        return {
            "status": "error",
            "currentVersion": APP_VERSION,
            "latestVersion": None,
            "releaseUrl": f"https://github.com/{DAILYBOT_GITHUB_REPO}/releases",
            "releaseNotes": "",
            "checkedAt": now_iso(),
            "message": message,
        }

    def load_config(self) -> list[dict[str, Any]]:
        if self.config_path.exists():
            try:
                raw = json.loads(self.config_path.read_text(encoding="utf-8"))
                raw_games = raw.get("games", []) if isinstance(raw, dict) else []
                configured = {}
                if isinstance(raw_games, list):
                    valid_ids = {game["id"] for game in GAME_CATALOG}
                    configured = {
                        item["id"]: item
                        for item in raw_games
                        if isinstance(item, dict)
                        and isinstance(item.get("id"), str)
                        and item["id"] in valid_ids
                    }
            except (OSError, ValueError, TypeError, KeyError):
                configured = {}
        else:
            configured = {}
        games = []
        for base in GAME_CATALOG:
            item = configured.get(base["id"], {})
            exe_path = item.get("exePath", "")
            if not isinstance(exe_path, str) or "\x00" in exe_path or (exe_path and not os.path.isabs(exe_path)):
                exe_path = ""
            args = item.get("args", [])
            if not isinstance(args, list) or not all(isinstance(arg, str) and "\x00" not in arg for arg in args):
                args = []
            games.append({
                **base,
                "exePath": exe_path,
                "gameExePath": self._safe_absolute_path(item.get("gameExePath", "")),
                "maaCliPath": self._safe_absolute_path(item.get("maaCliPath", "")),
                "maaClientType": item.get("maaClientType", "Official") if isinstance(item.get("maaClientType", "Official"), str) and item.get("maaClientType", "Official") in MAA_CLIENT_TYPES else "Official",
                "args": args[:64],
                "timeoutSeconds": self.safe_timeout(item.get("timeoutSeconds", 3600)),
            })
        return games

    @staticmethod
    def _safe_absolute_path(value: Any) -> str:
        if not isinstance(value, str) or "\x00" in value or (value and not os.path.isabs(value)):
            return ""
        return value.strip()

    @staticmethod
    def _is_expected_tool_executable(path: Path, game_id: str) -> bool:
        try:
            return path.is_file() and path.name.casefold() in TOOL_EXECUTABLES[game_id]
        except OSError:
            return False

    def _find_managed_tool(self, game_id: str) -> Path | None:
        root = self.data_dir / "tools" / game_id
        if not root.is_dir():
            return None
        try:
            matches = [path for path in root.rglob("*") if self._is_expected_tool_executable(path, game_id)]
        except OSError:
            return None
        return min(matches, key=lambda path: len(path.parts)) if matches else None

    @staticmethod
    def _registered_path_candidates(value: str) -> list[Path]:
        value = os.path.expandvars(value.strip())
        if not value:
            return []
        quoted = re.match(r'^"([^"\r\n]+\.exe)"', value, flags=re.IGNORECASE)
        if quoted:
            value = quoted.group(1)
        else:
            executable = re.match(r"^(.+?\.exe)(?:,\d+)?(?:\s.*)?$", value, flags=re.IGNORECASE)
            if executable:
                value = executable.group(1).strip('"')
        path = Path(value)
        if path.suffix.casefold() == ".exe":
            return [path, path.parent]
        return [path]

    def _find_registered_tool(self, game_id: str) -> Path | None:
        names = TOOL_REGISTRY_NAMES.get(game_id)
        if not names or sys.platform != "win32":
            return None
        try:
            import winreg
        except ImportError:
            return None

        uninstall_key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
        views = (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY)
        roots = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
        for root in roots:
            for view in views:
                try:
                    with winreg.OpenKey(root, uninstall_key, 0, winreg.KEY_READ | view) as entries:
                        count = winreg.QueryInfoKey(entries)[0]
                        for index in range(count):
                            try:
                                subkey_name = winreg.EnumKey(entries, index)
                                with winreg.OpenKey(entries, subkey_name) as entry:
                                    display_name = str(winreg.QueryValueEx(entry, "DisplayName")[0]).casefold()
                                    if not any(name in display_name for name in names):
                                        continue
                                    values = []
                                    for value_name in ("InstallLocation", "InstallPath", "DisplayIcon"):
                                        try:
                                            values.append(str(winreg.QueryValueEx(entry, value_name)[0]))
                                        except OSError:
                                            pass
                            except OSError:
                                continue
                            for value in values:
                                for location in self._registered_path_candidates(value):
                                    if self._is_expected_tool_executable(location, game_id):
                                        return location
                                    if not location.is_dir():
                                        continue
                                    for executable_name in TOOL_EXECUTABLES[game_id]:
                                        direct = location / executable_name
                                        if self._is_expected_tool_executable(direct, game_id):
                                            return direct
                                    try:
                                        children = list(location.iterdir())
                                    except OSError:
                                        continue
                                    for child in children:
                                        if not child.is_dir():
                                            continue
                                        for executable_name in TOOL_EXECUTABLES[game_id]:
                                            nested = child / executable_name
                                            if self._is_expected_tool_executable(nested, game_id):
                                                return nested
                except OSError:
                    continue
        return None

    def detect_installed_tools(self) -> dict[str, dict[str, str]]:
        with self.lock:
            changed = False
            for game in self.games:
                game_id = game["id"]
                configured = Path(game["exePath"]) if game["exePath"] else None
                if configured and configured.is_file():
                    self.tool_detection[game_id] = {"status": "found", "source": "configured"}
                    continue

                found = self._find_managed_tool(game_id) or self._find_registered_tool(game_id)
                if found:
                    game["exePath"] = str(found.resolve())
                    self.tool_detection[game_id] = {"status": "found", "source": "automatic"}
                    changed = True
                elif configured:
                    self.tool_detection[game_id] = {"status": "missing", "source": "configured"}
                else:
                    self.tool_detection[game_id] = {"status": "not-found", "source": ""}
            if changed:
                self.save_config()
            return {game_id: dict(result) for game_id, result in self.tool_detection.items()}

    @staticmethod
    def safe_timeout(value: Any) -> int:
        try:
            return min(86400, max(30, int(value)))
        except (ValueError, TypeError, OverflowError):
            return 3600

    def save_config(self) -> None:
        payload = {"version": 1, "games": self.games}
        temporary = self.config_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.config_path)

    @staticmethod
    def install_is_active(status: str) -> bool:
        return status in {"fetching", "downloading", "extracting", "launching", "cancelling"}

    def start_install(self, game_id: Any, region: Any = None) -> None:
        with self.lock:
            if not (sys.platform == "win32" and platform.machine().lower() in {"amd64", "x86_64"}):
                raise RuntimeError("下载安装目前仅支持 Windows 10/11 x64；请在 Windows 电脑上运行 DailyBot")
            if self.state["status"] == "running":
                raise RuntimeError("工具队列运行中，请结束当前任务后再安装")
            if self.install_is_active(self.installer_state["status"]):
                raise RuntimeError("已有一个工具正在下载或安装")
            game = next((item for item in self.games if item["id"] == game_id), None)
            if game is None:
                raise ValueError("选择的游戏无效")
            if game_id == "wuthering" and region not in {"china", "global"}:
                raise ValueError("请选择 OK-WW 的中国服或国际服版本")
            self.install_cancel.clear()
            self.installer_state = {
                "status": "fetching",
                "gameId": game_id,
                "message": "正在读取官方最新稳定版…",
                "progress": None,
                "bytesDownloaded": 0,
                "totalBytes": None,
                "releaseVersion": None,
                "assetName": None,
                "sha256": None,
                "digestVerified": None,
                "updatedAt": now_iso(),
            }
            threading.Thread(target=self._install_tool, args=(dict(game), region), daemon=True).start()

    def cancel_install(self) -> None:
        with self.lock:
            if not self.install_is_active(self.installer_state["status"]):
                raise RuntimeError("当前没有正在进行的下载或安装")
            self.install_cancel.set()
            self.installer_state.update({"status": "cancelling", "message": "正在取消…", "updatedAt": now_iso()})

    def set_install_state(self, **changes: Any) -> None:
        with self.lock:
            self.installer_state.update(changes)
            self.installer_state["updatedAt"] = now_iso()

    @staticmethod
    def github_json(url: str) -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "DailyBot-local-agent"},
        )
        try:
            with urllib.request.urlopen(request, timeout=25) as response:
                if urllib.parse.urlparse(response.geturl()).hostname != "api.github.com":
                    raise RuntimeError("官方发布源返回了意外的地址")
                raw = response.read(MAX_RELEASE_METADATA + 1)
        except HTTPError as exc:
            if exc.code == 403:
                raise RuntimeError("GitHub 暂时限制了发布信息请求，请稍后再试") from exc
            raise RuntimeError(f"读取 GitHub 发布信息失败（HTTP {exc.code}）") from exc
        except (URLError, TimeoutError) as exc:
            raise RuntimeError("无法连接 GitHub，请检查网络后重试") from exc
        if len(raw) > MAX_RELEASE_METADATA:
            raise RuntimeError("发布信息超出允许大小")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise RuntimeError("GitHub 返回的发布信息无法读取") from exc
        if not isinstance(payload, dict) or payload.get("draft") or payload.get("prerelease"):
            raise RuntimeError("没有找到可用的稳定版发布")
        return payload

    @staticmethod
    def select_release_asset(game_id: str, assets: Any, region: str | None) -> dict[str, Any]:
        if not isinstance(assets, list):
            raise RuntimeError("发布信息中没有可下载文件")
        candidates = [asset for asset in assets if isinstance(asset, dict) and isinstance(asset.get("name"), str)]
        if game_id == "arknights":
            candidates = [asset for asset in candidates if re.search(r"^maa[-_].*win[-_]?x64.*\.zip$", asset["name"], re.I)]
        elif game_id == "endfield":
            candidates = [asset for asset in candidates if re.fullmatch(r"MaaEnd-win-x86_64-.+\.zip", asset["name"], re.I)]
        elif game_id == "genshin":
            candidates = [asset for asset in candidates if asset["name"].lower().endswith(".exe") and "install" in asset["name"].lower()]
            candidates.sort(key=lambda asset: ("bettergi.install" not in asset["name"].lower(), asset["name"].lower()))
        elif game_id == "wuthering":
            region_name = "china" if region == "china" else "global"
            candidates = [
                asset for asset in candidates
                if asset["name"].lower().endswith(".exe")
                and "setup" in asset["name"].lower()
                and region_name in asset["name"].lower()
            ]
        if not candidates:
            raise RuntimeError("最新稳定版中没有匹配的官方安装文件，请打开官方发布页检查")
        asset = candidates[0]
        url = asset.get("browser_download_url")
        if not isinstance(url, str):
            raise RuntimeError("官方发布文件缺少下载地址")
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "github.com":
            raise RuntimeError("官方发布文件地址无效，已停止下载")
        repo = next(item["repo"] for item in GAME_CATALOG if item["id"] == game_id)
        if not parsed.path.startswith(f"/{repo}/releases/download/"):
            raise RuntimeError("发布文件不属于对应的官方项目，已停止下载")
        try:
            size = int(asset.get("size", 0))
        except (TypeError, ValueError):
            size = 0
        if size < 0 or size > MAX_DOWNLOAD_SIZE:
            raise RuntimeError("发布文件超出 DailyBot 允许的下载大小")
        return asset

    def _download_release_asset(self, asset: dict[str, Any], destination: Path) -> tuple[str, bool | None]:
        destination.parent.mkdir(parents=True, exist_ok=True)
        part_path = destination.with_suffix(destination.suffix + ".part")
        try:
            part_path.unlink(missing_ok=True)
            request = urllib.request.Request(asset["browser_download_url"], headers={"User-Agent": "DailyBot-local-agent"})
            opener = urllib.request.build_opener(GitHubRedirectHandler())
            with opener.open(request, timeout=30) as response:
                final_host = urllib.parse.urlparse(response.geturl()).hostname
                if final_host not in GITHUB_DOWNLOAD_HOSTS:
                    raise RuntimeError("下载被重定向到非 GitHub 文件服务器，已停止")
                try:
                    total = int(response.headers.get("Content-Length", "0")) or int(asset.get("size", 0)) or None
                except (TypeError, ValueError):
                    total = None
                if total is not None and (total < 0 or total > MAX_DOWNLOAD_SIZE):
                    raise RuntimeError("发布文件超出 DailyBot 允许的下载大小")
                digest = hashlib.sha256()
                downloaded = 0
                last_update = 0.0
                with part_path.open("wb") as output:
                    while True:
                        if self.install_cancel.is_set():
                            raise DownloadCancelled()
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        downloaded += len(chunk)
                        if downloaded > MAX_DOWNLOAD_SIZE or (total is not None and downloaded > total):
                            raise RuntimeError("下载文件大小超出发布信息，已停止")
                        output.write(chunk)
                        digest.update(chunk)
                        now = time.monotonic()
                        if now - last_update >= 0.25:
                            self.set_install_state(
                                status="downloading",
                                bytesDownloaded=downloaded,
                                totalBytes=total,
                                progress=min(100, int(downloaded * 100 / total)) if total else None,
                                message=f"正在下载 {asset['name']}",
                            )
                            last_update = now
                if total and downloaded != total:
                    raise RuntimeError("下载未完整完成，请重试")
                actual_digest = digest.hexdigest()
                expected = asset.get("digest")
                verified = None
                if isinstance(expected, str) and expected.lower().startswith("sha256:"):
                    verified = actual_digest.lower() == expected.split(":", 1)[1].lower()
                    if not verified:
                        raise RuntimeError("SHA-256 校验失败，文件已删除；请稍后重试")
                os.replace(part_path, destination)
                self.set_install_state(
                    bytesDownloaded=downloaded,
                    totalBytes=total or downloaded,
                    progress=100,
                    sha256=actual_digest,
                    digestVerified=verified,
                )
                return actual_digest, verified
        except HTTPError as exc:
            raise RuntimeError(f"下载官方发布文件失败（HTTP {exc.code}）") from exc
        except (URLError, TimeoutError) as exc:
            raise RuntimeError("下载中断，请检查网络后重试") from exc
        finally:
            part_path.unlink(missing_ok=True)

    def _extract_tool_bundle(self, archive: Path, version: str, game_id: str, tool_name: str, executable_name: str) -> Path:
        tools_dir = self.data_dir / "tools" / game_id
        tools_dir.mkdir(parents=True, exist_ok=True)
        suffix = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        safe_version = re.sub(r"[^A-Za-z0-9._-]", "_", version)[:80] or "latest"
        final_dir = tools_dir / f"{tool_name}-{safe_version}-{suffix}"
        staging = tools_dir / f".{tool_name}-{safe_version}-{suffix}.partial"
        staging.mkdir()
        try:
            with zipfile.ZipFile(archive) as bundle:
                entries = bundle.infolist()
                declared_total = sum(entry.file_size for entry in entries if not entry.is_dir())
                if declared_total > MAX_TOOL_EXPANDED_SIZE:
                    raise RuntimeError(f"{tool_name} 压缩包解压后超出允许大小")
                extracted_total = 0
                for entry in entries:
                    if self.install_cancel.is_set():
                        raise DownloadCancelled()
                    relative = entry.filename.replace("\\", "/")
                    parts = relative.split("/")
                    if relative.startswith("/") or any(part in {"", ".", ".."} for part in parts if part):
                        raise RuntimeError(f"{tool_name} 压缩包包含不安全的文件路径")
                    if re.match(r"^[A-Za-z]:", relative):
                        raise RuntimeError(f"{tool_name} 压缩包包含不安全的文件路径")
                    mode = entry.external_attr >> 16
                    if stat.S_ISLNK(mode):
                        raise RuntimeError("MAA 压缩包包含不支持的符号链接")
                    output_path = staging.joinpath(*[part for part in parts if part])
                    if not output_path.resolve().is_relative_to(staging.resolve()):
                        raise RuntimeError("MAA 压缩包包含不安全的文件路径")
                    if entry.is_dir():
                        output_path.mkdir(parents=True, exist_ok=True)
                        continue
                    output_path.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(entry) as source, output_path.open("wb") as output:
                        while True:
                            if self.install_cancel.is_set():
                                raise DownloadCancelled()
                            chunk = source.read(1024 * 1024)
                            if not chunk:
                                break
                            extracted_total += len(chunk)
                            if extracted_total > MAX_TOOL_EXPANDED_SIZE:
                                raise RuntimeError(f"{tool_name} 压缩包解压后超出允许大小")
                            output.write(chunk)
                    self.set_install_state(status="extracting", message=f"正在解压 {tool_name}")
            executables = [path for path in staging.rglob("*") if path.is_file() and path.name.lower() == executable_name.lower()]
            if not executables:
                raise RuntimeError(f"压缩包中没有找到 {executable_name}，已停止安装")
            relative_executable = min(executables, key=lambda path: len(path.parts)).relative_to(staging)
            staging.replace(final_dir)
            return final_dir / relative_executable
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise

    def _install_tool(self, game: dict[str, Any], region: str | None) -> None:
        download_path: Path | None = None
        try:
            release = self.github_json(GITHUB_API.format(repo=game["repo"]))
            asset = self.select_release_asset(game["id"], release.get("assets"), region)
            if self.install_cancel.is_set():
                raise DownloadCancelled()
            asset_name = Path(asset["name"]).name
            if asset_name != asset["name"] or not re.fullmatch(r"[A-Za-z0-9._() +\-]+", asset_name):
                raise RuntimeError("官方发布文件名无效，已停止下载")
            version = str(release.get("tag_name") or "latest")[:100]
            safe_version = re.sub(r"[^A-Za-z0-9._-]", "_", version) or "latest"
            download_path = self.data_dir / "downloads" / game["id"] / f"{safe_version}-{asset_name}"
            self.set_install_state(
                status="downloading",
                message=f"正在下载官方稳定版 {version}",
                releaseVersion=version,
                assetName=asset_name,
                totalBytes=int(asset.get("size", 0)) or None,
                progress=0,
            )
            _, verified = self._download_release_asset(asset, download_path)
            if self.install_cancel.is_set():
                raise DownloadCancelled()
            if game["id"] in {"arknights", "endfield"}:
                executable_name = "MaaEnd.exe" if game["id"] == "endfield" else "MAA.exe"
                self.set_install_state(status="extracting", progress=None, message=f"下载完成，正在解压 {game['tool']}…")
                executable = self._extract_tool_bundle(download_path, version, game["id"], game["tool"], executable_name)
                download_path.unlink(missing_ok=True)
                with self.lock:
                    current = next(item for item in self.games if item["id"] == game["id"])
                    current["exePath"] = str(executable)
                    self.tool_detection[game["id"]] = {"status": "found", "source": "automatic"}
                    self.save_config()
                self.set_install_state(
                    status="succeeded",
                    progress=100,
                    message=f"{game['tool']} 已下载、解压并连接到 DailyBot",
                    installedPath=str(executable),
                    digestVerified=verified,
                )
                return

            self.set_install_state(status="launching", progress=100, message="正在启动官方安装向导…")
            try:
                subprocess.Popen([str(download_path)], cwd=str(download_path.parent), shell=False, close_fds=True)
            except OSError as exc:
                self.set_install_state(
                    status="downloaded",
                    progress=100,
                    message=f"安装包已下载到本机，但无法自动启动：{exc}",
                    downloadedPath=str(download_path),
                    digestVerified=verified,
                )
                return
            self.set_install_state(
                status="launched",
                progress=100,
                message="官方安装向导已启动；完成安装后点“连接工具”填写程序路径",
                downloadedPath=str(download_path),
                digestVerified=verified,
            )
        except DownloadCancelled:
            if download_path:
                download_path.unlink(missing_ok=True)
                download_path.with_suffix(download_path.suffix + ".part").unlink(missing_ok=True)
            self.set_install_state(status="cancelled", progress=None, message="已取消下载或安装")
        except Exception as exc:
            if download_path and download_path.suffix.lower() == ".zip":
                download_path.unlink(missing_ok=True)
            self.set_install_state(status="failed", progress=None, message=str(exc), error=str(exc))

    def public_state(self) -> dict[str, Any]:
        with self.lock:
            result = dict(self.state)
            result["queue"] = list(self.state["queue"])
            result["completed"] = list(self.state["completed"])
            result["games"] = []
            for game in self.games:
                public_game = dict(game)
                detection = self.tool_detection.get(game["id"], {})
                path = Path(game["exePath"]) if game["exePath"] else None
                exists = bool(path and path.is_file())
                public_game["toolStatus"] = "found" if exists else "missing" if path else "not-found"
                if exists:
                    label = "自动找到" if detection.get("source") == "automatic" else "已连接"
                    public_game["toolStatusMessage"] = f"{label}：{path}"
                elif path:
                    public_game["toolStatusMessage"] = f"保存的程序路径不存在：{path}。可以重新检测或编辑路径。"
                else:
                    public_game["toolStatusMessage"] = "未找到程序。可以重新检测，或手动连接。"
                result["games"].append(public_game)
            result["logTail"] = self.read_log_tail(self.state.get("logPath"))
            result["installer"] = dict(self.installer_state)
            result["update"] = dict(self.update_state)
            result["platform"] = platform.system()
            result["installerSupported"] = sys.platform == "win32" and platform.machine().lower() in {"amd64", "x86_64"}
            return result

    @staticmethod
    def read_log_tail(log_path: str | None) -> str:
        if not log_path:
            return ""
        try:
            with Path(log_path).open("rb") as log_file:
                log_file.seek(0, os.SEEK_END)
                size = log_file.tell()
                log_file.seek(max(0, size - 128 * 1024), os.SEEK_SET)
                tail = log_file.read()
            if size > len(tail):
                first_line = tail.find(b"\n")
                tail = tail[first_line + 1:] if first_line >= 0 else tail
            lines = tail.decode("utf-8", errors="replace").splitlines()
            return "\n".join(lines[-80:])
        except OSError:
            return ""

    def update_games(self, payload: Any) -> None:
        if not isinstance(payload, dict) or not isinstance(payload.get("games"), list):
            raise ValueError("配置格式无效")
        incoming = {item.get("id"): item for item in payload["games"] if isinstance(item, dict)}
        with self.lock:
            if self.state["status"] == "running":
                raise RuntimeError("任务运行中，暂时不能修改配置")
            updated = []
            for base in GAME_CATALOG:
                item = incoming.get(base["id"])
                if item is None:
                    raise ValueError("请保留全部游戏配置")
                exe_path = str(item.get("exePath", "")).strip()
                raw_game_exe_path = item.get("gameExePath", "")
                if not isinstance(raw_game_exe_path, str) or "\x00" in raw_game_exe_path:
                    raise ValueError(f"{base['name']} 的游戏程序路径无效")
                game_exe_path = raw_game_exe_path.strip()
                if game_exe_path and not os.path.isabs(game_exe_path):
                    raise ValueError(f"{base['name']} 的游戏程序路径必须是绝对路径")
                raw_maa_cli_path = item.get("maaCliPath", "")
                if not isinstance(raw_maa_cli_path, str) or "\x00" in raw_maa_cli_path:
                    raise ValueError("MAA 命令行程序路径无效")
                maa_cli_path = raw_maa_cli_path.strip()
                if maa_cli_path and (not os.path.isabs(maa_cli_path) or Path(maa_cli_path).suffix.casefold() != ".exe"):
                    raise ValueError("MAA 命令行程序路径必须是 .exe 文件的完整路径")
                maa_client_type = item.get("maaClientType", "Official")
                if not isinstance(maa_client_type, str) or maa_client_type not in MAA_CLIENT_TYPES:
                    raise ValueError("MAA 客户端类型无效")
                args = item.get("args", [])
                if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
                    raise ValueError(f"{base['name']} 的启动参数格式无效")
                if any("\x00" in arg for arg in args) or "\x00" in exe_path:
                    raise ValueError("路径或参数包含无效字符")
                if exe_path and not os.path.isabs(exe_path):
                    raise ValueError(f"{base['name']} 的程序路径必须是绝对路径")
                updated.append({
                    **base,
                    "exePath": exe_path,
                    "gameExePath": game_exe_path,
                    "maaCliPath": maa_cli_path,
                    "maaClientType": maa_client_type,
                    "args": args[:64],
                    "timeoutSeconds": self.safe_timeout(item.get("timeoutSeconds", 3600)),
                })
                if game_exe_path and Path(game_exe_path).suffix.casefold() != ".exe":
                    raise ValueError(f"{base['name']} 的游戏程序路径必须指向 .exe 文件")
            self.games = updated
            self.save_config()
            self.detect_installed_tools()

    def start(self, game_ids: list[str]) -> None:
        with self.lock:
            if self.state["status"] == "running":
                raise RuntimeError("已有任务正在运行")
            if self.install_is_active(self.installer_state["status"]):
                raise RuntimeError("工具下载或安装中，请完成后再运行任务")
            lookup = {game["id"]: game for game in self.games}
            if game_ids == ["all"]:
                selected = [
                    game["id"] for game in self.games
                    if game["exePath"] and Path(game["exePath"]).is_file()
                ]
            else:
                if not all(isinstance(game_id, str) for game_id in game_ids):
                    raise ValueError("选择的游戏无效")
                selected = list(dict.fromkeys(game_ids))
            if not selected:
                raise ValueError("请先配置至少一个游戏工具的程序路径")
            if any(game_id not in lookup for game_id in selected):
                raise ValueError("选择的游戏无效")
            unconfigured = [lookup[game_id]["name"] for game_id in selected if not lookup[game_id]["exePath"]]
            if unconfigured:
                raise ValueError("尚未填写程序路径：" + "、".join(unconfigured))
            missing = [lookup[game_id]["name"] for game_id in selected if not Path(lookup[game_id]["exePath"]).is_file()]
            if missing:
                raise ValueError("程序路径已失效，请重新检测或修改路径：" + "、".join(missing))
            missing_maa_cli = [
                lookup[game_id]["name"]
                for game_id in selected
                if lookup[game_id].get("maaCliPath") and not Path(lookup[game_id]["maaCliPath"]).is_file()
            ]
            if missing_maa_cli:
                raise ValueError("MAA 命令行程序路径已失效，请在明日方舟设置中重新选择：" + "、".join(missing_maa_cli))
            self.cancel_requested = False
            self.state = {
                "status": "running",
                "startedAt": now_iso(),
                "finishedAt": None,
                "currentGameId": None,
                "currentGameName": None,
                "queue": selected,
                "completed": [],
                "message": "任务队列已启动",
                "logPath": None,
                "exitCode": None,
            }
            thread = threading.Thread(target=self._run_queue, args=(selected,), daemon=True)
            thread.start()

    def _run_queue(self, game_ids: list[str]) -> None:
        lookup = {game["id"]: game for game in self.games}
        for game_id in game_ids:
            with self.lock:
                if self.cancel_requested:
                    break
                game = lookup[game_id]
                log_path = self.logs_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{game_id}.log"
                self.state.update({
                    "currentGameId": game_id,
                    "currentGameName": game["name"],
                    "message": f"正在启动 {game['name']}",
                    "logPath": str(log_path),
                    "exitCode": None,
                })
            try:
                with log_path.open("ab") as log_file:
                    log_file.write(f"\n[{now_iso()}] 检查 {game['name']} 是否运行\n".encode("utf-8"))
                    with self.lock:
                        self.state["message"] = f"正在检查 {game['name']} 是否已启动"
                    try:
                        if game["id"] == "arknights" and game.get("maaCliPath"):
                            with self.lock:
                                self.state["message"] = "正在通过 MAA 启动模拟器和明日方舟"
                            if not self._run_maa_cli_startup(game, log_file):
                                break
                            running_process = "MAA CLI 启动流程"
                        else:
                            running_process = self.find_running_game_process(game)
                    except OSError as exc:
                        raise GameStartupError(f"无法检查{game['name']}是否已启动：{exc}") from exc
                    if game["id"] == "arknights" and game.get("maaCliPath"):
                        log_file.write(f"[{now_iso()}] MAA CLI 启动成功，跳过 Windows 游戏进程检测\n".encode("utf-8"))
                    elif running_process:
                        log_file.write(f"[{now_iso()}] 已检测到游戏进程：{running_process}\n".encode("utf-8"))
                    else:
                        game_exe_path = game.get("gameExePath", "")
                        if not game_exe_path:
                            raise GameStartupError(
                                f"未检测到{game['name']}。请先手动启动游戏，或在“编辑设置”中选择游戏主程序 .exe 以便 DailyBot 自动启动。"
                            )
                        game_exe = Path(game_exe_path)
                        if not game_exe.is_file():
                            raise GameStartupError(
                                f"{game['name']} 的游戏程序路径已失效：{game_exe}。请在“编辑设置”中重新选择游戏主程序。"
                            )
                        log_file.write(f"[{now_iso()}] 游戏未运行，正在启动：{game_exe}\n".encode("utf-8"))
                        with self.lock:
                            self.state["message"] = f"正在启动 {game['name']}，等待游戏进程出现"
                        self._launch_game(game_exe)
                        deadline = time.monotonic() + GAME_START_TIMEOUT_SECONDS
                        while time.monotonic() < deadline:
                            with self.lock:
                                if self.cancel_requested:
                                    break
                            try:
                                running_process = self.find_running_game_process(game)
                            except OSError as exc:
                                raise GameStartupError(f"无法检查{game['name']}是否已启动：{exc}") from exc
                            if running_process:
                                log_file.write(f"[{now_iso()}] 已检测到游戏进程：{running_process}\n".encode("utf-8"))
                                break
                            time.sleep(1)
                        with self.lock:
                            cancelled_now = self.cancel_requested
                        if cancelled_now:
                            break
                        if not running_process:
                            raise GameStartupError(
                                f"等待 {game['name']} 启动超时。请确认选择的是游戏主程序 .exe，而不是启动器，然后重试。"
                            )
                    with self.lock:
                        if self.cancel_requested:
                            break
                        self.state["message"] = f"{game['name']} 已启动，正在启动 {game['tool']}"
                    log_file.write(f"[{now_iso()}] 启动 {game['tool']}\n".encode("utf-8"))
                    command = [game["exePath"], *game["args"]]
                    with self.lock:
                        if self.cancel_requested:
                            process = None
                        else:
                            process_options: dict[str, Any] = {}
                            if os.name == "nt":
                                process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
                            else:
                                process_options["start_new_session"] = True
                            process = subprocess.Popen(
                                command,
                                cwd=str(Path(game["exePath"]).parent),
                                stdin=subprocess.DEVNULL,
                                stdout=log_file,
                                stderr=subprocess.STDOUT,
                                shell=False,
                                **process_options,
                            )
                            self.process = process
                    if process is None:
                        break
                    try:
                        exit_code = process.wait(timeout=game["timeoutSeconds"])
                        timed_out = False
                    except subprocess.TimeoutExpired:
                        exit_code = self._stop_process_tree(process)
                        timed_out = True
                    with self.lock:
                        if self.process is process:
                            self.process = None
                        cancelled_now = self.cancel_requested
                    if cancelled_now:
                        with self.lock:
                            self.state.update({
                                "status": "cancelled",
                                "finishedAt": now_iso(),
                                "currentGameId": None,
                                "currentGameName": None,
                                "message": "已停止",
                            })
                        return
                    if timed_out:
                        with log_path.open("ab") as log_file:
                            log_file.write(b"\n[DailyBot] Reached configured time limit; process stopped.\n")
                        with self.lock:
                            self.state.update({"status": "failed", "finishedAt": now_iso(), "message": f"{game['name']} 超时，队列已停止", "exitCode": exit_code})
                        return
                    if exit_code != 0:
                        with self.lock:
                            self.state.update({"status": "failed", "finishedAt": now_iso(), "message": f"{game['name']} 启动失败（退出码 {exit_code}），队列已停止", "exitCode": exit_code})
                        return
                    with self.lock:
                        self.state["completed"].append(game_id)
                        self.state["message"] = f"{game['name']} 已结束"
            except GameStartupError as exc:
                with self.lock:
                    self.state.update({
                        "status": "failed",
                        "finishedAt": now_iso(),
                        "message": str(exc),
                        "exitCode": None,
                    })
                return
            except (OSError, ValueError) as exc:
                with self.lock:
                    self.process = None
                    self.state.update({"status": "failed", "finishedAt": now_iso(), "message": f"无法启动 {game['name']}：{exc}", "exitCode": None})
                return
        with self.lock:
            stopped = self.cancel_requested
            self.state.update({
                "status": "cancelled" if stopped else "succeeded",
                "finishedAt": now_iso(),
                "currentGameId": None,
                "currentGameName": None,
                "message": "已停止" if stopped else "工具队列已结束，请确认游戏内任务结果",
            })

    def _run_maa_cli_startup(self, game: dict[str, Any], log_file: Any) -> bool:
        cli_path = Path(game["maaCliPath"])
        client_type = game.get("maaClientType", "Official")
        if not cli_path.is_file():
            raise GameStartupError(f"找不到 MAA 命令行程序：{cli_path}。请在明日方舟设置中重新选择。")
        if client_type not in MAA_CLIENT_TYPES:
            raise GameStartupError("MAA 客户端类型无效，请重新保存明日方舟设置。")

        command = [str(cli_path), "startup", client_type]
        log_file.write(f"[{now_iso()}] 调用 MAA 启动流程：{command!r}\n".encode("utf-8"))
        process_options: dict[str, Any] = {}
        if os.name == "nt":
            process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            process_options["start_new_session"] = True
        with self.lock:
            if self.cancel_requested:
                return False
            process = subprocess.Popen(
                command,
                cwd=str(cli_path.parent),
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                shell=False,
                **process_options,
            )
            self.process = process
        try:
            exit_code = process.wait(timeout=MAA_STARTUP_TIMEOUT_SECONDS)
            timed_out = False
        except subprocess.TimeoutExpired:
            exit_code = self._stop_process_tree(process)
            timed_out = True
        with self.lock:
            if self.process is process:
                self.process = None
            cancelled_now = self.cancel_requested
        if cancelled_now:
            return False
        if timed_out:
            log_file.write(b"[DailyBot] MAA startup exceeded the 10-minute limit; process stopped.\n")
            raise GameStartupError("MAA 启动模拟器或进入游戏超时（10 分钟）。请检查 MAA 命令行配置和模拟器连接。")
        if exit_code != 0:
            raise GameStartupError(f"MAA 启动流程失败（退出码 {exit_code}）。请检查 MAA 命令行配置、客户端类型和连接设置。")
        log_file.write(f"[{now_iso()}] MAA 已完成启动流程\n".encode("utf-8"))
        return True

    @staticmethod
    def _running_processes(expected_names: set[str]) -> list[tuple[str, str]]:
        """Return process executable names and image paths for candidate names."""
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class PROCESSENTRY32W(ctypes.Structure):
                _fields_ = [
                    ("dwSize", wintypes.DWORD),
                    ("cntUsage", wintypes.DWORD),
                    ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t),
                    ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD),
                    ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", wintypes.LONG),
                    ("dwFlags", wintypes.DWORD),
                    ("szExeFile", wintypes.WCHAR * 260),
                ]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            snapshot = kernel32.CreateToolhelp32Snapshot
            snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
            snapshot.restype = wintypes.HANDLE
            process_first = kernel32.Process32FirstW
            process_first.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
            process_first.restype = wintypes.BOOL
            process_next = kernel32.Process32NextW
            process_next.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
            process_next.restype = wintypes.BOOL
            open_process = kernel32.OpenProcess
            open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            open_process.restype = wintypes.HANDLE
            query_image = kernel32.QueryFullProcessImageNameW
            query_image.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
            query_image.restype = wintypes.BOOL
            close_handle = kernel32.CloseHandle
            close_handle.argtypes = [wintypes.HANDLE]
            close_handle.restype = wintypes.BOOL

            handle = snapshot(0x00000002, 0)
            invalid_handle = ctypes.c_void_p(-1).value
            if handle == invalid_handle:
                raise OSError(ctypes.get_last_error(), "无法读取 Windows 进程列表")
            results: list[tuple[str, str]] = []
            try:
                entry = PROCESSENTRY32W()
                entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
                has_entry = process_first(handle, ctypes.byref(entry))
                while has_entry:
                    name = entry.szExeFile
                    if name.casefold() in expected_names:
                        process_handle = open_process(0x1000, False, entry.th32ProcessID)
                        image_path = ""
                        if process_handle:
                            try:
                                buffer = ctypes.create_unicode_buffer(32768)
                                size = wintypes.DWORD(len(buffer))
                                if query_image(process_handle, 0, buffer, ctypes.byref(size)):
                                    image_path = buffer.value
                            finally:
                                close_handle(process_handle)
                        results.append((name, image_path))
                    has_entry = process_next(handle, ctypes.byref(entry))
            finally:
                close_handle(handle)
            return results

        if sys.platform.startswith("linux"):
            results = []
            try:
                process_dirs = Path("/proc").iterdir()
            except OSError:
                return results
            for process_dir in process_dirs:
                if not process_dir.name.isdigit():
                    continue
                try:
                    image_path = str((process_dir / "exe").resolve())
                    name = Path(image_path).name
                except OSError:
                    try:
                        name = (process_dir / "comm").read_text(encoding="utf-8").strip()
                        image_path = ""
                    except OSError:
                        continue
                if name.casefold() in expected_names:
                    results.append((name, image_path))
            return results

        try:
            result = subprocess.run(["ps", "-A", "-o", "comm="], check=True, capture_output=True, text=True, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return []
        return [(Path(line.strip()).name, line.strip()) for line in result.stdout.splitlines() if Path(line.strip()).name.casefold() in expected_names]

    @classmethod
    def find_running_game_process(cls, game: dict[str, Any]) -> str | None:
        game_path = game.get("gameExePath", "")
        configured_name = Path(game_path).name.casefold() if game_path else ""
        expected_names = set(GAME_PROCESS_NAMES.get(game["id"], set()))
        if configured_name:
            expected_names.add(configured_name)
        running = cls._running_processes(expected_names)
        if game_path:
            target = cls._normalize_process_path(game_path)
            for name, image_path in running:
                if image_path and cls._normalize_process_path(image_path) == target:
                    return image_path
            return None
        for name, image_path in running:
            if game["id"] == "wuthering" and name.casefold() == "client-win64-shipping.exe":
                # Unreal uses this generic executable name for many games; identify WuWa by its install path.
                if "wuthering waves" not in image_path.casefold():
                    continue
            return image_path or name
        return None

    @staticmethod
    def _normalize_process_path(value: str) -> str:
        if value.startswith("\\\\?\\"):
            value = value[4:]
        return os.path.normcase(os.path.abspath(value))

    @staticmethod
    def _launch_game(game_exe: Path) -> None:
        options: dict[str, Any] = {}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
        else:
            options["start_new_session"] = True
        subprocess.Popen(
            [str(game_exe)],
            cwd=str(game_exe.parent),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            **options,
        )

    @staticmethod
    def _stop_process_tree(process: subprocess.Popen[bytes], grace_seconds: int = 5) -> int:
        if os.name == "nt":
            if process.poll() is None:
                try:
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                except (OSError, ValueError):
                    try:
                        process.terminate()
                    except OSError:
                        pass
        else:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            pass
        if os.name == "nt":
            try:
                subprocess.run(
                    ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=10,
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        return process.wait()

    def cancel(self) -> None:
        with self.lock:
            if self.state["status"] != "running":
                raise RuntimeError("当前没有运行中的任务")
            self.cancel_requested = True
            process = self.process
            self.state["message"] = "正在停止当前工具…"
        if process:
            self._stop_process_tree(process)

    def shutdown(self) -> None:
        self.install_cancel.set()
        with self.lock:
            if self.state["status"] == "running":
                self.cancel_requested = True
                process = self.process
            else:
                process = None
        if process:
            self._stop_process_tree(process)


class RequestHandler(BaseHTTPRequestHandler):
    server_version = "DailyBot/0.3"

    @property
    def agent(self) -> Agent:
        return self.server.agent  # type: ignore[attr-defined]

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self'; script-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _check_local(self, mutation: bool = False) -> bool:
        try:
            if not ipaddress_is_loopback(self.client_address[0]):
                self._json(403, {"error": "仅允许本机访问"})
                return False
        except ValueError:
            self._json(403, {"error": "访问来源无效"})
            return False
        allowed_hosts = {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}
        if self.headers.get("Host", "") not in allowed_hosts:
            self._json(403, {"error": "访问地址无效"})
            return False
        if mutation:
            origin = self.headers.get("Origin")
            if origin:
                parsed = urllib.parse.urlparse(origin)
                if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.port != self.server.server_port:
                    self._json(403, {"error": "跨站请求已拒绝"})
                    return False
        return True

    def do_GET(self) -> None:  # noqa: N802
        if not self._check_local():
            return
        if self.path == "/api/state":
            self._json(200, self.agent.public_state())
            return
        if self.path in {"/", "/index.html"}:
            try:
                self._send(200, (WEB_ROOT / "index.html").read_bytes(), "text/html; charset=utf-8")
            except OSError:
                self._json(500, {"error": "界面文件缺失"})
            return
        if self.path in {"/app.css", "/app.js"}:
            path = WEB_ROOT / self.path.lstrip("/")
            kind = "text/css; charset=utf-8" if path.suffix == ".css" else "text/javascript; charset=utf-8"
            try:
                self._send(200, path.read_bytes(), kind)
            except OSError:
                self._json(404, {"error": "文件不存在"})
            return
        self._json(404, {"error": "找不到页面"})

    def do_POST(self) -> None:  # noqa: N802
        if not self._check_local(mutation=True):
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 0 or length > MAX_BODY:
                self._json(413, {"error": "请求内容过大"})
                return
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._json(400, {"error": "请求内容无效"})
            return
        try:
            if self.path == "/api/detect":
                self.agent.detect_installed_tools()
            elif self.path == "/api/update/check":
                self.agent.check_for_updates()
            elif self.path == "/api/games":
                self.agent.update_games(payload)
            elif self.path == "/api/run":
                if not isinstance(payload, dict) or not isinstance(payload.get("gameIds"), list):
                    raise ValueError("启动请求无效")
                self.agent.start(payload["gameIds"])
            elif self.path == "/api/cancel":
                self.agent.cancel()
            elif self.path == "/api/install":
                if not isinstance(payload, dict):
                    raise ValueError("安装请求无效")
                self.agent.start_install(payload.get("gameId"), payload.get("region"))
            elif self.path == "/api/install/cancel":
                self.agent.cancel_install()
            else:
                self._json(404, {"error": "找不到接口"})
                return
            self._json(200, self.agent.public_state())
        except RuntimeError as exc:
            self._json(409, {"error": str(exc)})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})

    def log_message(self, fmt: str, *args: Any) -> None:
        # Keep the local console quiet; failures still appear in the dashboard.
        return


def ipaddress_is_loopback(address: str) -> bool:
    import ipaddress
    return ipaddress.ip_address(address).is_loopback


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class DesktopApi:
    def __init__(self) -> None:
        self.window: Any = None

    def select_executable(self) -> str | None:
        if self.window is None:
            return None
        import webview

        selected = self.window.create_file_dialog(
            webview.OPEN_DIALOG,
            allow_multiple=False,
            file_types=("Windows 程序 (*.exe)",),
        )
        return str(selected[0]) if selected else None


def main() -> int:
    parser = argparse.ArgumentParser(description="启动 DailyBot 本地控制台")
    parser.add_argument("--port", type=int, default=8765, help="本机网页端口（默认 8765）")
    parser.add_argument("--data-dir", type=Path, default=None, help="配置与日志目录")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("端口范围必须是 1 到 65535")
    if os.environ.get("LOCALAPPDATA"):
        default_data = Path(os.environ["LOCALAPPDATA"]) / "DailyBot"
    else:
        default_data = Path.home() / ".dailybot"
    data_dir = (args.data_dir or default_data).expanduser().resolve()
    agent = Agent(data_dir)
    try:
        server = LocalServer(("127.0.0.1", args.port), RequestHandler)
    except OSError as exc:
        message = f"无法启动 DailyBot 本地服务：{exc}"
        print(message, file=sys.stderr)
        if sys.platform == "win32":
            try:
                import ctypes

                ctypes.windll.user32.MessageBoxW(None, message, "DailyBot", 0x10)
            except Exception:
                pass
        return 1
    server.agent = agent  # type: ignore[attr-defined]
    url = f"http://127.0.0.1:{server.server_port}/"
    print(f"DailyBot 已启动：{url}")
    print(f"本机配置与日志：{data_dir}")
    server_thread: threading.Thread | None = None
    try:
        try:
            import webview
        except ImportError:
            print("未安装 pywebview，已改为使用系统浏览器。")
            webbrowser.open(url)
            server.serve_forever(poll_interval=0.5)
        else:
            webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
            server_thread = threading.Thread(
                target=server.serve_forever,
                kwargs={"poll_interval": 0.5},
                daemon=True,
            )
            server_thread.start()
            desktop_api = DesktopApi()
            desktop_api.window = webview.create_window(
                "DailyBot",
                url,
                width=1280,
                height=840,
                min_size=(960, 640),
                js_api=desktop_api,
            )
            webview.start()
    except KeyboardInterrupt:
        print("\nDailyBot 已关闭")
    except Exception as exc:
        message = f"DailyBot 桌面窗口启动失败：{exc}"
        print(message, file=sys.stderr)
        if sys.platform == "win32":
            try:
                import ctypes

                ctypes.windll.user32.MessageBoxW(None, message, "DailyBot", 0x10)
            except Exception:
                pass
        return 1
    finally:
        if server_thread is not None and server_thread.is_alive():
            server.shutdown()
            server_thread.join(timeout=3)
        server.server_close()
        agent.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
