import { useEffect, useState } from "react";
import { LoaderCircle } from "lucide-react";
import { Toaster, toast } from "sonner";
import { bridge } from "./lib/bridge";
import type { AppConfig, BootstrapResult, PageId } from "./types";
import { TitleBar } from "./components/TitleBar";
import { Sidebar } from "./components/Sidebar";
import { RunPage } from "./pages/RunPage";
import { SchedulesPage } from "./pages/SchedulesPage";
import { PromptsPage } from "./pages/PromptsPage";
import { SettingsPage } from "./pages/SettingsPage";
import { AboutPage } from "./pages/AboutPage";

export default function App() {
  const [page, setPage] = useState<PageId>("run");
  const [bootstrap, setBootstrap] = useState<BootstrapResult | null>(null);
  const [error, setError] = useState("");
  const [dark, setDark] = useState(() => localStorage.getItem("issue-radar-theme") === "dark" || (!localStorage.getItem("issue-radar-theme") && matchMedia("(prefers-color-scheme: dark)").matches));

  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
    localStorage.setItem("issue-radar-theme", dark ? "dark" : "light");
  }, [dark]);

  useEffect(() => {
    void bridge.bootstrap().then(setBootstrap).catch((reason) => setError(String(reason)));
  }, []);

  const saveConfig = async (config: AppConfig) => {
    const result = await bridge.saveConfig(config);
    setBootstrap(result);
  };

  return (
    <div className="app-shell">
      <TitleBar dark={dark} onToggleTheme={() => setDark((current) => !current)} />
      {bootstrap ? (
        <div className="app-body">
          <Sidebar page={page} onNavigate={setPage} version={bootstrap.appVersion} />
          <main className="main-panel">
            {page === "run" && <RunPage config={bootstrap.config} />}
            {page === "schedules" && <SchedulesPage config={bootstrap.config} />}
            {page === "prompts" && <PromptsPage config={bootstrap.config} onSave={saveConfig} />}
            {page === "settings" && <SettingsPage config={bootstrap.config} configPath={bootstrap.configPath} onSave={saveConfig} />}
            {page === "about" && <AboutPage version={bootstrap.appVersion} />}
          </main>
        </div>
      ) : (
        <div className="bootstrap-screen">
          {error ? <><div className="bootstrap-error">启动失败</div><p>{error}</p><button onClick={() => { setError(""); void bridge.bootstrap().then(setBootstrap).catch((reason) => { setError(String(reason)); toast.error("仍无法启动"); }); }}>重试</button></> : <><LoaderCircle className="spin" /><span>正在加载本机配置…</span></>}
        </div>
      )}
      <Toaster theme={dark ? "dark" : "light"} position="bottom-right" richColors closeButton />
    </div>
  );
}
