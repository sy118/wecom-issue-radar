import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { LoaderCircle } from "lucide-react";
import { Toaster, toast } from "sonner";
import { bridge } from "./lib/bridge";
import { getDesktop } from "./lib/desktop";
import { toUserErrorMessage } from "./lib/errors";
import { emptyProgress, loadProgress, looksConfigured, resetProgress, saveProgress, type OnboardingProgress } from "./lib/onboarding";
import { hasUnsavedChanges } from "./lib/unsaved";
import { loadAndApplyZoom } from "./lib/zoom";
import type { AppConfig, BootstrapResult, PageId } from "./types";
import { TitleBar } from "./components/TitleBar";
import { AppSidebar } from "./components/AppSidebar";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { OnboardingWizard } from "./components/OnboardingWizard";
import { QuickSwitcher } from "./components/QuickSwitcher";
import { UnsavedChangesDialog } from "./components/UnsavedChangesDialog";
import { defaultPageForSection, pageLabel, sectionForPage } from "./navigation";
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
  const [quickSwitchOpen, setQuickSwitchOpen] = useState(false);
  const [pendingPage, setPendingPage] = useState<PageId | null>(null);
  const pageFrameRef = useRef<HTMLElement | null>(null);
  const previousPageRef = useRef<PageId>(page);
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

  // Ctrl/Cmd+K opens the page switcher from anywhere, including while typing.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.isComposing) return;
      if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === "k") {
        event.preventDefault();
        setQuickSwitchOpen((current) => !current);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  // Every in-app navigation starts a new page: reset the single page scroller,
  // move focus into the content region and expose the page name to assistive
  // technology and the window title.
  useEffect(() => {
    document.title = `${pageLabel(page)} · 企微问题雷达`;
    if (previousPageRef.current === page) return;
    previousPageRef.current = page;
    const frame = pageFrameRef.current;
    if (!frame) return;
    frame.scrollTop = 0;
    frame.focus({ preventScroll: true });
  }, [page]);

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
    if (hasUnsavedChanges()) {
      setPendingPage(next);
      return;
    }
    setPage(next);
  };

  const leaveWithUnsavedChanges = () => {
    const next = pendingPage;
    setPendingPage(null);
    if (next) setPage(next);
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
      <a className="skip-link" href="#main-content" onClick={(event) => {
        event.preventDefault();
        const frame = pageFrameRef.current;
        if (!frame) return;
        frame.scrollTop = 0;
        frame.focus({ preventScroll: true });
      }}>跳到主要内容</a>
      <TitleBar dark={dark} onToggleTheme={() => setDark((current) => !current)} />
      {bootstrap ? (
        <>
          <div className="app-body">
            <AppSidebar
              page={page}
              version={bootstrap.appVersion}
              onNavigate={navigate}
              onOpenQuickSwitch={() => setQuickSwitchOpen(true)}
            />
            <main
              className="page-frame"
              id="main-content"
              ref={pageFrameRef}
              tabIndex={-1}
              aria-label={`${pageLabel(page)} 主要内容`}
            >
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
          <QuickSwitcher
            open={quickSwitchOpen}
            onClose={() => setQuickSwitchOpen(false)}
            onNavigate={navigate}
          />
          <UnsavedChangesDialog
            open={pendingPage !== null}
            onStay={() => setPendingPage(null)}
            onLeave={leaveWithUnsavedChanges}
          />
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
      <Toaster
        theme={dark ? "dark" : "light"}
        position="bottom-right"
        offset={{ bottom: 80, right: 20 }}
        richColors
        closeButton
      />
    </div>
  );
}
