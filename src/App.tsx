import { useEffect, useState, useSyncExternalStore } from "react";
import { LoaderCircle } from "lucide-react";
import { Toaster, toast } from "sonner";
import { bridge } from "./lib/bridge";
import { getDesktop } from "./lib/desktop";
import { toUserErrorMessage } from "./lib/errors";
import { loadProgress, looksConfigured, saveProgress, type OnboardingProgress } from "./lib/onboarding";
import { confirmLeave, hasUnsavedChanges } from "./lib/unsaved";
import { loadAndApplyZoom } from "./lib/zoom";
import type { AppConfig, BootstrapResult, PageId } from "./types";
import { TitleBar } from "./components/TitleBar";
import { TopNav, SubNav } from "./components/TopNav";
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
import { PlaceholderPage } from "./pages/PlaceholderPage";
import { PublishTargetsPage } from "./pages/PublishTargetsPage";
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

  const section = sectionForPage(page);
  const updateOnboarding = (next: OnboardingProgress) => {
    saveProgress(next);
    setOnboarding(next);
  };

  return (
    <div className="app-shell">
      <TitleBar dark={dark} onToggleTheme={() => setDark((current) => !current)} />
      {bootstrap ? (
        <>
          <TopNav
            section={section}
            version={bootstrap.appVersion}
            onSelectSection={(next) => {
              const target = defaultPageForSection(next);
              if (target === page) return;
              if (!confirmLeave()) return;
              setPage(target);
            }}
          />
          <div className="app-body">
            <SubNav section={section} page={page} onNavigate={navigate} />
            <main className="page-frame">
              <div hidden={page !== "wecom-issue"}>
                <RunPage config={bootstrap.config} />
              </div>
              {page === "workbench" && (
                <WorkbenchPage
                  config={bootstrap.config}
                  onboardingCompleted={onboarding.completed}
                  onOpenOnboarding={() => setWizardOpen(true)}
                  onNavigate={navigate}
                />
              )}
              {page === "wecom-schedules" && <SchedulesPage config={bootstrap.config} />}
              {page === "wecom-reply" && <GroupReplyPage />}
              {page === "wechat-workbench" && (
                <PlaceholderPage
                  title="个人微信会话草稿"
                  milestone="M2"
                  description="融合 ProxyTalk 后，这里会显示会话列表、项目绑定和可审核的回复草稿。"
                  bullets={[
                    "只读本地微信数据库，不修改原始记录",
                    "草稿可编辑，支持更短/更礼貌/换一个",
                    "只复制或填入空输入框，永不自动发送",
                  ]}
                  onBack={() => navigate("workbench")}
                  onNavigate={navigate}
                />
              )}
              {page === "wechat-export" && (
                <PlaceholderPage
                  title="导出个人微信会话"
                  milestone="M2"
                  description="按项目和范围导出 Markdown、JSON 与媒体目录，无法解密的媒体会保留原因。"
                  bullets={[
                    "按会话和时间范围导出",
                    "图片/文件单独存放，缺失时标注原因",
                    "导出内容只在本机",
                  ]}
                  onBack={() => navigate("workbench")}
                />
              )}
              {page === "daily-search" && (
                <PlaceholderPage
                  title="聊天查找"
                  milestone="M3"
                  description="跨企业微信群聊与个人微信会话的本地全文检索，结果直接定位到上下文。"
                  bullets={[
                    "关键词 + 发送者 + 时间 + 消息类型筛选",
                    "本地 FTS5 索引，不上传聊天内容",
                    "为周报、待办、群内速查提供底座",
                  ]}
                  onBack={() => navigate("workbench")}
                />
              )}
              {page === "daily-reply" && (
                <PlaceholderPage
                  title="高情商回复"
                  milestone="M3"
                  description="给领导、客户或同事的消息生成稳重/亲和/简短三版草稿，只复制或填入，不自动发送。"
                  bullets={[
                    "选中消息或粘贴文本即可生成",
                    "三种风格可编辑、可换一版",
                    "明确的合规边界：不点发送",
                  ]}
                  onBack={() => navigate("workbench")}
                />
              )}
              {page === "daily-report" && (
                <PlaceholderPage
                  title="周报总结"
                  milestone="M3"
                  description="把本周群聊、个人微信和已处理问题整理成可编辑的周报草稿。"
                  bullets={[
                    "进展 / 问题 / 下周计划三段式",
                    "可编辑并导出到发布目标",
                    "默认本地生成，外发需显式开启",
                  ]}
                  onBack={() => navigate("workbench")}
                />
              )}
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
                />
              )}
              {page === "about" && <AboutPage version={bootstrap.appVersion} />}
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
