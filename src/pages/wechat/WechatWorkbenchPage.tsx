import { useEffect, useRef } from "react";
import { toast } from "sonner";

import { bridge } from "../../lib/bridge";
import { providerLabel } from "./logic";
import { useWechatWorkbench } from "./useWechatWorkbench";
import { ConversationPane } from "../../components/wechat/ConversationPane";
import { DraftPanel } from "../../components/wechat/DraftPanel";
import { ExportDialog } from "../../components/wechat/ExportDialog";
import { ProjectEditorDrawer } from "../../components/wechat/ProjectEditorDrawer";
import { ProjectRail } from "../../components/wechat/ProjectRail";
import { WechatOnboarding } from "../../components/wechat/WechatOnboarding";
import { WechatSettingsDrawer } from "../../components/wechat/WechatSettingsDrawer";
import "../../wechat.css";

export interface WechatWorkbenchPageProps {
  onNavigate?: (page: string) => void;
  /** App can mount the same page for the standalone export route. */
  initialView?: "workbench" | "export";
}

export function WechatWorkbenchPage({ onNavigate, initialView = "workbench" }: WechatWorkbenchPageProps) {
  const wb = useWechatWorkbench();
  const selected = wb.selected;
  const providerName = providerLabel(wb.providers, selected?.providerId ?? "");
  const autoExportOpened = useRef(false);

  useEffect(() => {
    if (initialView !== "export" || autoExportOpened.current) return;
    if (!wb.projects.length) return;
    autoExportOpened.current = true;
    wb.setExportOpen(true);
  }, [initialView, wb.projects.length, wb.setExportOpen]);

  const openResultPath = (path: string) => {
    if (!path) return;
    void bridge.openPath(path).catch((error: unknown) => {
      toast.error("无法打开结果目录", {
        description: error instanceof Error ? error.message : "路径可能已被移动或删除。",
      });
    });
  };

  const openExport = () => {
    if (!wb.projects.length) {
      toast.warning("先创建一个会话项目，再导出聊天记录。");
      return;
    }
    wb.setExportOpen(true);
  };

  const openConfig = () => {
    if (onNavigate) onNavigate("wechat-config");
    else wb.setSettingsOpen(true);
  };

  return (
    <div className="wechat-workspace">
      <ProjectRail
        projects={wb.projects}
        selectedId={selected?.id ?? ""}
        detect={wb.detect}
        detectLoading={wb.detectLoading}
        backendAvailable={wb.backendAvailable}
        loading={wb.projectsLoading}
        error={wb.projectsError}
        unreadFor={wb.unreadFor}
        onSelect={wb.selectProject}
        onCreate={wb.openNewProject}
        onRefresh={() => void wb.refreshProjects()}
        onExport={openExport}
        onOpenConfig={openConfig}
      />

      {selected ? (
        <ConversationPane
          project={selected}
          projectCount={wb.projects.length}
          messages={wb.selectedMessages}
          loading={wb.messagesLoading}
          loadingMore={wb.loadingMore}
          hasMore={wb.hasMoreMessages}
          error={wb.messagesError}
          onLoadMore={() => void wb.loadMoreMessages()}
          onRefresh={() => void wb.refreshSelectedMessages()}
          onEdit={() => wb.openEditProject(selected)}
          onExport={openExport}
          onCreate={wb.openNewProject}
          onLoadImage={wb.loadMedia}
        />
      ) : (
        <WechatOnboarding
          detect={wb.detect}
          detectLoading={wb.detectLoading}
          detectError={wb.detectError}
          scan={wb.scan}
          scanning={wb.scanning}
          backendAvailable={wb.backendAvailable}
          projectCount={wb.projects.length}
          onDetect={() => void wb.detectWechat()}
          onScanKeys={(dbDir) => void wb.scanKeys(dbDir)}
          onCreateProject={wb.openNewProject}
          onOpenConfig={openConfig}
          onNavigate={onNavigate}
        />
      )}

      <DraftPanel
        project={selected}
        draft={wb.selectedDraft}
        stale={wb.staleDraft}
        generating={wb.generating}
        rewriting={wb.rewriting}
        saving={wb.savingDraft}
        filling={wb.filling}
        providerName={providerName}
        rewriteStyles={wb.replyStyles}
        onGenerate={() => void wb.generateDraft()}
        onRewrite={(style) => void wb.rewriteDraft(style)}
        onSave={(content) => void wb.saveDraft(content)}
        onCopy={(content) => void wb.copyDraft(content)}
        onFill={(content) => void wb.fillDraft(content)}
        onOpenConfig={openConfig}
      />

      {wb.editor && (
        <ProjectEditorDrawer
          state={wb.editor}
          chats={wb.chats}
          chatsLoading={wb.chatsLoading}
          chatsError={wb.chatsError}
          providers={wb.providers}
          mcpServers={wb.mcpServers}
          projectDefaults={{
            tone: typeof wb.settings?.defaultTone === "string" ? wb.settings.defaultTone : undefined,
            triggerMode: (wb.settings?.defaultTriggerMode || undefined) as
              | "auto"
              | "manual"
              | "rules"
              | undefined,
          }}
          saving={wb.savingProject}
          onClose={wb.closeEditor}
          onReloadChats={() => void wb.loadChats()}
          onSave={wb.saveProject}
          onDelete={(project) => void wb.deleteProject(project)}
        />
      )}

      {wb.settingsOpen && (
        <WechatSettingsDrawer
          detect={wb.detect}
          detectLoading={wb.detectLoading}
          detectError={wb.detectError}
          scan={wb.scan}
          scanning={wb.scanning}
          settings={wb.settings}
          providers={wb.providers}
          mcpServers={wb.mcpServers}
          configLoading={wb.configLoading}
          configError={wb.configError}
          savingSettings={wb.savingSettings}
          backendAvailable={wb.backendAvailable}
          selectingAccount={wb.selectingAccount}
          onClose={() => wb.setSettingsOpen(false)}
          onDetect={() => void wb.detectWechat()}
          onScanKeys={(dbDir) => void wb.scanKeys(dbDir)}
          onSelectAccount={(dbDir) => void wb.selectAccount(dbDir)}
          onReloadConfig={() => void wb.loadConfig()}
          onSaveSettings={wb.saveSettings}
        />
      )}

      {wb.exportOpen && (
        <ExportDialog
          projects={wb.projects}
          initialProjectId={selected?.id ?? ""}
          exporting={wb.exporting}
          onClose={() => wb.setExportOpen(false)}
          onExport={wb.exportProject}
          onOpenPath={openResultPath}
        />
      )}
    </div>
  );
}

export default WechatWorkbenchPage;
