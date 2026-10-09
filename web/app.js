const gameList = document.querySelector("#game-list");
const runAllButton = document.querySelector("#run-all");
const scanToolsButton = document.querySelector("#scan-tools");
const stopButton = document.querySelector("#stop-run");
const notice = document.querySelector("#notice");
const dialog = document.querySelector("#config-dialog");
const regionDialog = document.querySelector("#region-dialog");
const form = document.querySelector("#config-form");
const dialogError = document.querySelector("#dialog-error");
const executableInput = document.querySelector("#exe-path");
const browseExecutableButton = document.querySelector("#browse-exe");
const maaStartupFields = document.querySelector("#maa-startup-fields");
const maaCliInput = document.querySelector("#maa-cli-path");
const browseMaaCliButton = document.querySelector("#browse-maa-cli");
const gameExecutableInput = document.querySelector("#game-exe-path");
const browseGameExecutableButton = document.querySelector("#browse-game-exe");
let state = null;
let activeGame = null;
const updateStatus = document.querySelector("#update-status");
const updateLink = document.querySelector("#update-link");
const checkUpdatesButton = document.querySelector("#check-updates");
let scanningTools = false;
let refreshInFlight = false;
let refreshTimer = null;
let lastRenderedState = "";

const dateLabel = new Intl.DateTimeFormat("zh-CN", { weekday: "long", month: "long", day: "numeric" });
document.querySelector("#today-label").textContent = dateLabel.format(new Date());

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function showNotice(message, success = false) {
  notice.textContent = message;
  notice.hidden = !message;
  notice.classList.toggle("is-success", success);
}

async function api(path, body) {
  const response = await fetch(path.replace(/^\//, ""), {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    cache: "no-store",
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "请求失败");
  return result;
}

async function checkForDailyBotUpdates() {
  checkUpdatesButton.disabled = true;
  updateStatus.textContent = "正在检查…";
  try {
    const result = await api("/api/update/check", {});
    const update = result.update;
    updateStatus.textContent = update.message;
    updateLink.hidden = update.status !== "update-available";
    updateLink.href = update.releaseUrl;
    updateLink.textContent = update.latestVersion ? "下载 " + update.latestVersion + " ↗" : "打开发布页 ↗";
    if (update.status === "update-available") {
      showNotice("DailyBot 有新版本 " + update.latestVersion + "，可从 GitHub 发布页下载。");
    }
  } catch (error) {
    updateStatus.textContent = error.message || "检查更新失败";
  } finally {
    checkUpdatesButton.disabled = false;
  }
}

function statusFor(game) {
  if (state.status === "running" && state.currentGameId === game.id) return { label: "运行中", className: "is-running" };
  if (state.status === "failed" && state.currentGameId === game.id) return { label: "启动失败", className: "is-error" };
  if (state.completed?.includes(game.id)) return { label: "工具已退出", className: "is-done" };
  if (game.toolStatus === "missing") return { label: "路径失效", className: "is-error" };
  if (!game.exePath || game.toolStatus === "not-found") return { label: "未检测到", className: "" };
  return { label: "已连接", className: "is-ready" };
}

function installerBusy() {
  return ["fetching", "downloading", "extracting", "launching", "cancelling"].includes(state?.installer?.status);
}

function installLabel(installer, game) {
  if (installer?.gameId !== game.id) return "下载安装";
  if (installer.status === "fetching") return "获取版本…";
  if (installer.status === "downloading") return installer.progress == null ? "下载中…" : `下载中 ${installer.progress}%`;
  if (installer.status === "extracting") return "解压中…";
  if (installer.status === "launching") return "启动安装中…";
  if (installer.status === "cancelling") return "正在取消…";
  if (installer.status === "succeeded") return "重新下载";
  if (installer.status === "launched") return "重新安装";
  if (installer.status === "downloaded") return "重新下载";
  if (installer.status === "failed" || installer.status === "cancelled") return "重试下载";
  return "下载安装";
}

function renderInstallDetail(game, row) {
  const installer = state.installer;
  if (!installer || installer.gameId !== game.id || !["fetching", "downloading", "extracting", "launching", "cancelling", "succeeded", "launched", "downloaded", "failed", "cancelled"].includes(installer.status)) return;
  const detail = element("div", `install-detail${installer.status === "failed" ? " is-error" : ""}`);
  const copy = element("p", "install-message", installer.message || "");
  if (installer.releaseVersion) copy.append(element("span", "install-version", installer.releaseVersion));
  detail.append(copy);
  if (["fetching", "downloading", "extracting", "launching", "cancelling"].includes(installer.status)) {
    const progress = document.createElement("progress");
    progress.max = 100;
    progress.setAttribute("aria-label", `${game.tool} 下载和安装进度`);
    if (installer.progress != null) progress.value = installer.progress;
    detail.append(progress);
    if (installer.status !== "cancelling") {
      const cancel = element("button", "cancel-install", "取消");
      cancel.type = "button";
      cancel.addEventListener("click", cancelInstall);
      detail.append(cancel);
    }
  } else if (installer.sha256) {
    detail.append(element("span", "digest-note", installer.digestVerified === true ? "SHA-256 已匹配 GitHub" : "GitHub 未提供校验值，已计算 SHA-256"));
  }
  if (installer.downloadedPath && installer.status === "downloaded") {
    detail.append(element("span", "download-path", installer.downloadedPath));
  }
  row.append(detail);
}

function renderGames() {
  if (!state) return;
  document.querySelector("#platform-note").hidden = Boolean(state.installerSupported);
  const configured = state.games.filter((game) => game.toolStatus === "found").length;
  document.querySelector("#configured-count").textContent = String(configured);
  scanToolsButton.disabled = scanningTools || state.status === "running" || installerBusy();
  scanToolsButton.textContent = scanningTools ? "正在检测…" : "检测本机工具";
  gameList.replaceChildren();
  for (const game of state.games) {
    const row = element("article", "game-row");
    row.dataset.game = game.id;
    const main = element("div", "game-main");
    const emblem = element("div", "game-emblem", game.short);
    emblem.setAttribute("aria-hidden", "true");
    const copy = element("div", "game-copy");
    const titleLine = element("div", "game-title-line");
    titleLine.append(element("h3", "game-title", game.name));
    const status = statusFor(game);
    const statusNode = element("span", `game-status ${status.className}`, status.label);
    titleLine.append(statusNode);
    copy.append(titleLine, element("p", "game-tool", game.toolStatusMessage || `${game.tool} · 尚未检测`));
    main.append(emblem, copy);

    const actions = element("div", "game-actions");
    const installClass = `small-button install-button${game.toolStatus === "found" ? " is-secondary" : " is-primary"}`;
    const install = element("button", installClass, installLabel(state.installer, game));
    install.type = "button";
    install.disabled = !state.installerSupported || state.status === "running" || installerBusy();
    install.title = state.installerSupported ? "从项目官方 GitHub 发布页下载最新稳定版" : "请在 Windows 10/11 x64 上运行 DailyBot";
    install.setAttribute("aria-label", `下载安装${game.tool}官方版本`);
    install.addEventListener("click", () => game.id === "wuthering" ? regionDialog.showModal() : startInstall(game.id));
    const official = element("a", "small-button official-link", "官方发布页 ↗");
    official.href = game.downloadUrl;
    official.target = "_blank";
    official.rel = "noopener noreferrer";
    official.setAttribute("aria-label", `在新窗口打开${game.tool}官方发布页`);
    const configure = element("button", "small-button", game.exePath ? "编辑设置" : "连接工具");
    configure.type = "button";
    configure.addEventListener("click", () => openConfig(game));
    const launch = element("button", "small-button launch", "运行工具");
    launch.type = "button";
    launch.disabled = state.status === "running" || installerBusy() || game.toolStatus !== "found";
    launch.setAttribute("aria-label", `运行${game.name}工具`);
    launch.addEventListener("click", () => runGames([game.id]));
    actions.append(install, official, configure, launch);
    row.append(main, actions);
    renderInstallDetail(game, row);
    gameList.append(row);
  }
  runAllButton.disabled = configured === 0 || state.status === "running" || installerBusy();
  runAllButton.querySelector("span:last-child").textContent = configured === 0 ? "先连接一个工具" : "运行已连接工具";
  stopButton.hidden = state.status !== "running";
  renderActivity();
}

function renderActivity() {
  const running = state.status === "running";
  const title = document.querySelector("#activity-title");
  const indicator = document.querySelector("#activity-indicator");
  const message = document.querySelector("#activity-message");
  const queue = document.querySelector("#queue-list");
  const log = document.querySelector("#log-output");
  const live = document.querySelector("#log-live");
  title.textContent = running ? "正在执行" : state.status === "failed" ? "执行遇到问题" : state.status === "succeeded" ? "队列已结束" : state.status === "cancelled" ? "任务已停止" : "准备就绪";
  indicator.className = `activity-indicator${running ? " is-running" : state.status === "failed" ? " is-error" : ""}`;
  message.textContent = state.message || "配置好工具后，可以逐个运行，也可以按列表顺序运行全部。";
  live.className = running ? "live-tag is-active" : "live-tag";
  live.textContent = running ? "RUNNING" : "LOCAL";

  const lookup = new Map(state.games.map((game) => [game.id, game]));
  queue.replaceChildren();
  const queueIds = state.queue || [];
  document.querySelector("#queue-count").textContent = `${queueIds.length} 项`;
  if (!queueIds.length) {
    queue.append(element("li", "queue-empty", "还没有待运行任务"));
  } else {
    queueIds.forEach((id, index) => {
      const game = lookup.get(id);
      if (!game) return;
      const item = element("li", "queue-item");
      if (state.currentGameId === id && running) item.classList.add("is-current");
      else if (state.completed?.includes(id)) item.classList.add("is-done");
      else if (!running && state.status === "failed" && state.queue.indexOf(state.currentGameId) < index) item.classList.add("is-skipped");
      item.append(element("span", "queue-step", state.completed?.includes(id) ? "✓" : String(index + 1).padStart(2, "0")));
      item.append(element("span", "queue-name", game.name));
      if (state.currentGameId === id && running) item.append(element("span", "queue-current", "执行中"));
      queue.append(item);
    });
  }
  log.textContent = state.logTail || "任务启动后，工具输出会显示在这里。";
  log.scrollTop = log.scrollHeight;
  document.querySelector("#log-caption").textContent = state.logPath ? `日志文件：${state.logPath}` : "日志只保存在本机";
}

function openConfig(game) {
  activeGame = game;
  document.querySelector("#dialog-title").textContent = `连接${game.name}`;
  const emulatorNote = ["arknights", "endfield"].includes(game.id) ? " 安卓模拟器中的游戏需要单独接入 ADB 才能确认。" : "";
  document.querySelector("#dialog-description").textContent = `选择 ${game.tool} 程序；也可设置${game.name}本体路径，让 DailyBot 在运行工具前检查并按需启动游戏。请选择游戏本体 .exe，而不是启动器。${emulatorNote}`;
  executableInput.value = game.exePath;
  maaStartupFields.hidden = game.id !== "arknights";
  maaCliInput.value = game.maaCliPath || "";
  document.querySelector("#maa-client-type").value = game.maaClientType || "Official";
  gameExecutableInput.value = game.gameExePath || "";
  document.querySelector("#args-input").value = (game.args || []).join("\n");
  document.querySelector("#timeout-input").value = String(game.timeoutSeconds || 3600);
  dialogError.hidden = true;
  dialogError.textContent = "";
  dialog.showModal();
  document.querySelector("#exe-path").focus();
}

async function saveConfig(event) {
  event.preventDefault();
  if (!activeGame) return;
  const games = state.games.map((game) => game.id !== activeGame.id ? game : {
    ...game,
    exePath: executableInput.value.trim(),
    maaCliPath: game.id === "arknights" ? maaCliInput.value.trim() : (game.maaCliPath || ""),
    maaClientType: game.id === "arknights" ? document.querySelector("#maa-client-type").value : (game.maaClientType || "Official"),
    gameExePath: gameExecutableInput.value.trim(),
    args: document.querySelector("#args-input").value.split(/\r?\n/).map((arg) => arg.trim()).filter(Boolean),
    timeoutSeconds: Number(document.querySelector("#timeout-input").value) || 3600,
  });
  const saveButton = document.querySelector("#save-config");
  saveButton.disabled = true;
  try {
    state = await api("api/games", { games });
    dialog.close();
    showNotice("");
    renderGames();
  } catch (error) {
    dialogError.textContent = error.message;
    dialogError.hidden = false;
  } finally {
    saveButton.disabled = false;
  }
}

async function browseExecutable(input, button) {
  dialogError.hidden = true;
  if (!window.pywebview?.api?.select_executable) {
    dialogError.textContent = "浏览文件功能需要在 DailyBot 桌面窗口中使用；也可以手动粘贴 .exe 完整路径。";
    dialogError.hidden = false;
    return;
  }
  button.disabled = true;
  try {
    const path = await window.pywebview.api.select_executable();
    if (path) input.value = path;
  } catch (error) {
    dialogError.textContent = `无法打开文件选择窗口：${error.message}`;
    dialogError.hidden = false;
  } finally {
    button.disabled = false;
  }
}

async function browseMaaCli() {
  dialogError.hidden = true;
  if (!window.pywebview?.api?.select_executable) {
    dialogError.textContent = "浏览文件功能需要在 DailyBot 桌面窗口中使用；也可以手动粘贴 MAA 命令行程序的 .exe 完整路径。";
    dialogError.hidden = false;
    return;
  }
  browseMaaCliButton.disabled = true;
  try {
    const path = await window.pywebview.api.select_executable();
    if (path) maaCliInput.value = path;
  } catch (error) {
    dialogError.textContent = `无法打开文件选择窗口：${error.message}`;
    dialogError.hidden = false;
  } finally {
    browseMaaCliButton.disabled = false;
  }
}

async function runGames(gameIds) {
  showNotice("");
  try {
    state = await api("api/run", { gameIds });
    renderGames();
  } catch (error) {
    showNotice(error.message);
  }
}

async function startInstall(gameId, region) {
  showNotice("");
  try {
    state = await api("api/install", { gameId, region });
    if (regionDialog.open) regionDialog.close();
    renderGames();
  } catch (error) {
    showNotice(error.message);
  }
}

async function cancelInstall() {
  try {
    state = await api("api/install/cancel", {});
    renderGames();
  } catch (error) {
    showNotice(error.message);
  }
}

async function detectTools() {
  scanningTools = true;
  showNotice("");
  renderGames();
  try {
    state = await api("api/detect", {});
    const found = state.games.filter((game) => game.toolStatus === "found").length;
    showNotice(`检测完成：找到 ${found} 个工具。`, true);
  } catch (error) {
    showNotice(`检测失败：${error.message}`);
  } finally {
    scanningTools = false;
    renderGames();
  }
}

async function refresh() {
  if (refreshInFlight) return;
  refreshInFlight = true;
  try {
    const nextState = await api("api/state");
    const snapshot = JSON.stringify(nextState);
    if (snapshot !== lastRenderedState) {
      state = nextState;
      lastRenderedState = snapshot;
      renderGames();
    }
  } catch (error) {
    showNotice(`无法连接本地 Agent：${error.message}。请关闭后重新启动 DailyBot。`);
  } finally {
    refreshInFlight = false;
    const interval = document.hidden ? 15000 : state?.status === "running" || installerBusy() ? 2000 : 5000;
    refreshTimer = window.setTimeout(refresh, interval);
  }
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) return;
  window.clearTimeout(refreshTimer);
  refresh();
});

runAllButton.addEventListener("click", () => runGames(["all"]));
scanToolsButton.addEventListener("click", detectTools);
stopButton.addEventListener("click", async () => {
  try {
    state = await api("api/cancel", {});
    renderGames();
  } catch (error) {
    showNotice(error.message);
  }
});
form.addEventListener("submit", saveConfig);
browseExecutableButton.addEventListener("click", () => browseExecutable(executableInput, browseExecutableButton));
browseGameExecutableButton.addEventListener("click", () => browseExecutable(gameExecutableInput, browseGameExecutableButton));
browseMaaCliButton.addEventListener("click", browseMaaCli);
document.querySelector("#close-dialog").addEventListener("click", () => dialog.close());
checkUpdatesButton.addEventListener("click", checkForDailyBotUpdates);
checkForDailyBotUpdates();
document.querySelector("#cancel-dialog").addEventListener("click", () => dialog.close());
dialog.addEventListener("click", (event) => {
  if (event.target === dialog) dialog.close();
});

document.querySelectorAll("[data-region]").forEach((button) => {
  button.addEventListener("click", () => startInstall("wuthering", button.dataset.region));
});
document.querySelector("#close-region-dialog").addEventListener("click", () => regionDialog.close());
document.querySelector("#cancel-region-dialog").addEventListener("click", () => regionDialog.close());
regionDialog.addEventListener("click", (event) => {
  if (event.target === regionDialog) regionDialog.close();
});

refresh();
