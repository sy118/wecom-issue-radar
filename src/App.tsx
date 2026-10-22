import { useEffect, useState, useSyncExternalStore } from "react";
import { LoaderCircle } from "lucide-react";
import { Toaster, toast } from "sonner";
import { bridge } from "./lib/bridge";
import { getDesktop } from "./lib/desktop";
import { toUserErrorMessage } from "./lib/errors";
import { emptyProgress, loadProgress, looksConfigured, resetProgress, saveProgress, type OnboardingProgress } from "./lib/onboarding";
import { confirmLeave, hasUnsavedChanges } from "./lib/unsaved";
import { loadAndApplyZoom } from "./lib/zoom";
import type { AppConfig, BootstrapResult, PageId } from "./types";
import { TitleBar } from "./components/TitleBar";
import { AppSidebar } from "./components/AppSidebar";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { OnboardingWizard } from "./components/OnboardingWizard";
import { defaultPageForSection, sectionForPage } from "./navigation";
import { RunPage } from "./pages/RunPage";
import { SchedulesPage } from "./pages/SchedulesPage";
import { PromptsPage } from "./pages/PromptsPage";
import { SettingsHome } from "./pages/SettingsHome";
import { AboutPage } from "./pages/AboutPage";
import { McpServicesPage } from "./pages/McpServicesPage";
import { DifyAppsPage } from "./pages/DifyAppsPage";
import { GroupReplyPage } from "./pages/GroupReplyPage";
import { WorkbenchPage } from "./pages/WorkbenchPage";
import { PublishTargetsPage } from "./pages/PublishTargetsPage";
import { WechatWorkbenchPage } from "./pages/wechat/WechatWorkbenchPage";
import { WechatConfigPage } from "./pages/wechat/WechatConfigPage";
import { DailyReplyPage } from "./pages/daily/DailyReplyPage";
import { DailyPhrasesPage } from "./pages/daily/DailyPhrasesPage";
import { DailyDigestPage } from "./pages/daily/DailyDigestPage";
import { UpdateDialog } from "./components/UpdateDialog";
import { appUpdater } from "./lib/appUpdater";

let backgroundUpdateCheckStarted = false;

export default function App() {
  const [page, setPage] = useState<PageId>("workbench");
  const [bootstrap, setBootstrap] = useState<BootstrapResult | null>(null);
  const [error, setError] = useState("");
  const [onboarding, setOnboarding] = useState<OnboardingProgress>(() => loadProgress());
  const [wizardOpen, setWizardOpen] = useState(false);
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
    void loadAndApplyZoom();
  }, []);

  useEffect(() => {
    void bridge.bootstrap().then(setBootstrap).catch((reason) => {
      setError(toUserErrorMessage(reason, "无法读取本机配置，请稍后重试。"));
    });
  }, []);

  useEffect(() => {
    if (!bootstrap) return;
    const stored = loadProgress();
    if (!stored.completed && looksConfigured(bootstrap.config)) {
      const done = { ...stored, completed: true, skipped: true };
      saveProgress(done);
      setOnboarding(done);
      return;
    }
    setOnboarding(stored);
    if (!stored.completed && !looksConfigured(bootstrap.config)) {
      setWizardOpen(true);
    }
  }, [bootstrap]);

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
      await getDesktop().invoke("window_minimize");
    } catch (reason) {
      toast.error("窗口最小化失败", {
        description: toUserErrorMessage(reason, "请稍后重试。"),
      });
    }
  };

  const navigate = (next: PageId) => {
    if (next === page) return;
    if (!confirmLeave()) return;
    setPage(next);
  };

  useEffect(() => {
    const handler = (event: BeforeUnloadEvent) => {
      if (!hasUnsavedChanges()) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, []);

  const updateOnboarding = (next: OnboardingProgress) => {
    saveProgress(next);
    setOnboarding(next);
  };

  // Settings → 新手引导: clear the persisted progress and reopen the wizard without touching config.
  const restartOnboarding = () => {
    resetProgress();
    setOnboarding(emptyProgress());
    setWizardOpen(true);
  };

  return (
    <div className="app-shell">
      <TitleBar dark={dark} onToggleTheme={() => setDark((current) => !current)} />
      {bootstrap ? (
        <>
          <div className="app-body">
            <AppSidebar page={page} version={bootstrap.appVersion} onNavigate={navigate} />
            <main className="page-frame">
              <ErrorBoundary key={page}>
              <div hidden={page !== "wecom-issue"}>
                <RunPage config={bootstrap.config} />
              </div>
              {page === "workbench" && (
                <WorkbenchPage
                  config={bootstrap.config}
                  onboardingCompleted={onboarding.completed}
                  onboardingProgress={onboarding}
                  onOpenOnboarding={() => setWizardOpen(true)}
                  onNavigate={navigate}
                />
              )}
              {page === "wecom-schedules" && <SchedulesPage config={bootstrap.config} />}
              {page === "wecom-reply" && <GroupReplyPage />}
              {page === "wechat-workbench" && (
                <WechatWorkbenchPage onNavigate={(next) => navigate(next as PageId)} />
              )}
              {page === "wechat-export" && (
                <WechatWorkbenchPage initialView="export" onNavigate={(next) => navigate(next as PageId)} />
              )}
              {page === "wechat-config" && (
                <WechatConfigPage onNavigate={(next) => navigate(next as PageId)} />
              )}
              {page === "daily-reply" && <DailyReplyPage />}
              {page === "daily-phrases" && <DailyPhrasesPage />}
              {page === "daily-digest" && <DailyDigestPage />}
              {page === "strategy-prompts" && (
                <PromptsPage config={bootstrap.config} onSave={saveConfig} />
              )}
              {page === "strategy-publish" && (
                <PublishTargetsPage config={bootstrap.config} onSave={saveConfig} />
              )}
              {page === "strategy-mcp" && <McpServicesPage />}
              {page === "strategy-dify" && <DifyAppsPage />}
              {page === "settings" && (
                <SettingsHome
                  config={bootstrap.config}
                  configPath={bootstrap.configPath}
                  version={bootstrap.appVersion}
                  onSave={saveConfig}
                  onImport={importConfigBackup}
                  onRestartOnboarding={restartOnboarding}
                />
              )}
              {page === "about" && <AboutPage version={bootstrap.appVersion} />}
              </ErrorBoundary>
            </main>
          </div>
          {wizardOpen && (
            <OnboardingWizard
              config={bootstrap.config}
              progress={onboarding}
              onChange={updateOnboarding}
              onSaveConfig={saveConfig}
              onClose={() => setWizardOpen(false)}
              onNavigate={(next) => {
                setPage(next);
                setWizardOpen(false);
              }}
            />
          )}
        </>
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
