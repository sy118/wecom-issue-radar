import { useEffect, useState, useSyncExternalStore } from "react";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { LoaderCircle } from "lucide-react";
import { Toaster, toast } from "sonner";
import { bridge } from "./lib/bridge";
import { toUserErrorMessage } from "./lib/errors";
import type { AppConfig, BootstrapResult, PageId } from "./types";
import { TitleBar } from "./components/TitleBar";
import { Sidebar } from "./components/Sidebar";
import { RunPage } from "./pages/RunPage";
import { SchedulesPage } from "./pages/SchedulesPage";
import { PromptsPage } from "./pages/PromptsPage";
import { SettingsPage } from "./pages/SettingsPage";
import { AboutPage } from "./pages/AboutPage";
import { McpServicesPage } from "./pages/McpServicesPage";
import { GroupReplyPage } from "./pages/GroupReplyPage";
import { UpdateDialog } from "./components/UpdateDialog";
import { appUpdater } from "./lib/appUpdater";

let backgroundUpdateCheckStarted = false;

export default function App() {
  const [page, setPage] = useState<PageId>("run");
  const [bootstrap, setBootstrap] = useState<BootstrapResult | null>(null);
  const [error, setError] = useState("");
  const [dark, setDark] = useState(() => localStorage.getItem("issue-radar-theme") === "dark" || (!localStorage.getItem("issue-radar-theme") && matchMedia("(prefers-color-scheme: dark)").matches));
  const updaterState = useSyncExternalStore(
    appUpdater.subscribe,
    appUpdater.getState,
    appUpdater.getState,
  );

  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    localStorage.setItem("issue-radar-theme", dark ? "dark" : "light");
  }, [dark]);

  useEffect(() => {
    void bridge.bootstrap().then(setBootstrap).catch((reason) => {
      setError(toUserErrorMessage(reason, "无法读取本机配置，请稍后重试。"));
    });
  }, []);

  useEffect(() => {
    if (!bootstrap || backgroundUpdateCheckStarted) return;
    backgroundUpdateCheckStarted = true;
    void appUpdater.check();
  }, [bootstrap]);

  const retryBootstrap = async () => {
    setError("");
    try {
      setBootstrap(await bridge.bootstrap());
    } catch (reason) {
      const message = toUserErrorMessage(reason, "无法读取本机配置，请稍后重试。");
      setError(message);
      toast.error("仍无法启动", { description: message });
    }
  };

  const saveConfig = async (config: AppConfig) => {
    const result = await bridge.saveConfig(config);
    setBootstrap(result);
  };

  const importConfigBackup = async (path: string) => {
    const result = await bridge.importConfigBackup(path);
    setBootstrap(result);
  };

  const minimizeWindow = async () => {
    try {
      await getCurrentWindow().minimize();
    } catch (reason) {
      toast.error("窗口最小化失败", {
        description: toUserErrorMessage(reason, "请稍后重试。"),
      });
    }
  };

  return (
    <div className="app-shell">
      <TitleBar dark={dark} onToggleTheme={() => setDark((current) => !current)} />
      {bootstrap ? (
        <div className="app-body">
          <Sidebar
            page={page}
            onNavigate={setPage}
            version={bootstrap.appVersion}
            onCheckForUpdates={() => void appUpdater.check({ interactive: true })}
            checkingForUpdates={updaterState.status === "checking"}
            updateAvailable={updaterState.canInstall}
          />
          <main className="main-panel">
            <div hidden={page !== "run"}>
              <RunPage config={bootstrap.config} />
            </div>
            {page === "schedules" && <SchedulesPage config={bootstrap.config} />}
            {page === "prompts" && <PromptsPage config={bootstrap.config} onSave={saveConfig} />}
            {page === "settings" && <SettingsPage config={bootstrap.config} configPath={bootstrap.configPath} onSave={saveConfig} onImport={importConfigBackup} />}
            {page === "mcp" && <McpServicesPage />}
            {page === "reply" && <GroupReplyPage />}
            {page === "about" && <AboutPage version={bootstrap.appVersion} />}
          </main>
        </div>
      ) : (
        <div className="bootstrap-screen">
          {error ? (
            <>
              <div className="bootstrap-error">启动失败</div>
              <p>{error}</p>
              <button onClick={() => void retryBootstrap()}>重试</button>
            </>
          ) : (
            <>
              <LoaderCircle className="spin" />
              <span>正在加载本机配置…</span>
            </>
          )}
        </div>
      )}
      {bootstrap && (
        <UpdateDialog
          state={updaterState}
          currentVersion={bootstrap.appVersion}
          onInstall={() => void appUpdater.install()}
          onRetry={() => {
            if (updaterState.canInstall) void appUpdater.install();
            else void appUpdater.check({ interactive: true });
          }}
          onDismiss={appUpdater.dismiss}
          onMinimize={() => void minimizeWindow()}
        />
      )}
      <Toaster theme={dark ? "dark" : "light"} position="bottom-right" richColors closeButton />
    </div>
  );
}
