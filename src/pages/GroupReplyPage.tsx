import { useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  Gauge,
  History,
  Inbox,
  Network,
  Plus,
  RefreshCw,
  Sparkles,
} from "lucide-react";
import { toast } from "sonner";
import { Button, SectionHeader } from "../components/ui";
import { bridge } from "../lib/bridge";
import { toUserErrorMessage } from "../lib/errors";
import {
  automaticDeliveryBlockers,
  buildListenerSaveBody,
  buildWorkActionBody,
  createCommand,
  createQuery,
  executeListenerSave,
  isMcpToolGrantable,
  runtimeAvailabilityCopy,
  toggleMcpServerGrant,
  toolGrantSelectionKey,
  tuningValidationErrors,
  type WorkActionKind,
} from "../lib/replyRuntimeUi";
import type { DifyAppSummary, ReplyListenerSummary, ReplyWorkItem } from "../types";
import { ListenerEditorDrawer } from "./groupreply/ListenerEditorDrawer";
import { ListenerList } from "./groupreply/ListenerList";
import { WorkDetailDrawer } from "./groupreply/WorkDetailDrawer";
import { WorkQueue } from "./groupreply/WorkQueue";
import {
  emptyWorkFilters,
  listenerDraft,
  listenerFromWire,
  listenerSaveResultFromWire,
  type ListenerDraft,
  type TestChallenge,
  type ToolGroup,
  type ViewMode,
  type WorkFilters,
} from "./groupreply/shared";
import { useGroupReplyData } from "./groupreply/useGroupReplyData";

export * from "./groupreply/shared";

export function GroupReplyPage() {
  const [view, setView] = useState<ViewMode>("listeners");
  const [busy, setBusy] = useState("");
  const [editorOpen, setEditorOpen] = useState(false);
  const [draft, setDraft] = useState<ListenerDraft>(listenerDraft);
  const [groupSearch, setGroupSearch] = useState("");
  const [persistedGroupId, setPersistedGroupId] = useState("");
  const [expandedMcpServers, setExpandedMcpServers] = useState<string[]>([]);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [workFilters, setWorkFilters] = useState<WorkFilters>(emptyWorkFilters);
  const [testChallenge, setTestChallenge] = useState<TestChallenge | null>(null);
  const {
    listeners,
    activePage,
    pendingPage,
    historyPage,
    setActivePage,
    setPendingPage,
    setHistoryPage,
    groups,
    tools,
    difyApps,
    snapshot,
    runtimeRevision,
    setRuntimeRevision,
    loading,
    detail,
    load,
    loadEditorOptions,
    openWorkDetail,
    closeWorkDetail,
    activeItems,
    pendingItems,
    historyItems,
  } = useGroupReplyData(workFilters);

  const openCreate = () => {
    const firstGrantableServerId = tools.find((tool) => isMcpToolGrantable(tool, "v2"))?.serverId;
    const defaultServerToolKeys = firstGrantableServerId
      ? tools.filter((tool) => (
          tool.serverId === firstGrantableServerId
          && isMcpToolGrantable(tool, "v2")
        )).map((tool) => tool.key)
      : [];
    setDraft({
      ...listenerDraft(),
      revision: runtimeRevision,
      selectedTools: toggleMcpServerGrant([], defaultServerToolKeys),
    });
    setPersistedGroupId("");
    setExpandedMcpServers([]);
    setAdvancedOpen(false);
    setTestChallenge(null);
    setEditorOpen(true);
  };

  const openEdit = (listener: ReplyListenerSummary) => {
    setDraft({
      id: listener.id,
      revision: runtimeRevision,
      name: listener.name,
      enabled: listener.enabled,
      groupId: listener.groupId,
      groupName: listener.groupName,
      answerEngine: listener.answerEngine,
      runtimeVersion: listener.runtimeVersion,
      difyAppId: listener.difyAppId,
      systemPrompt: listener.systemPrompt,
      webhookUrl: "",
      webhookMode: "keep",
      webhookConfigured: listener.webhookConfigured,
      webhookVerified: listener.webhookVerified,
      deliveryMode: listener.deliveryMode,
      requireIndependentReview: listener.requireIndependentReview,
      reviewPrompt: listener.reviewPrompt,
      selectedTools: listener.tools.map((grant) => toolGrantSelectionKey(grant.serverId, grant.toolName, grant.schemaSha256)),
      toolStrategies: Object.fromEntries(listener.tools.map((grant) => [
        toolGrantSelectionKey(grant.serverId, grant.toolName, grant.schemaSha256),
        {
          bootstrap: Boolean(grant.bootstrap),
          bootstrapArgumentsText: JSON.stringify(grant.bootstrapArguments ?? {}, null, 2),
        },
      ])),
      tuning: { ...listener.tuning },
    });
    setPersistedGroupId(listener.groupId);
    setExpandedMcpServers([]);
    setAdvancedOpen(false);
    setTestChallenge(null);
    setEditorOpen(true);
  };

  const unavailableToolKeys = useMemo(
    () => draft.selectedTools.filter((key) => !tools.some((candidate) => (
      candidate.key === key && isMcpToolGrantable(candidate, draft.runtimeVersion)
    ))),
    [draft.runtimeVersion, draft.selectedTools, tools],
  );

  const toolGroups = useMemo<ToolGroup[]>(() => {
    const grouped = new Map<string, ToolGroup>();
    for (const tool of tools) {
      const group = grouped.get(tool.serverId) ?? {
        serverId: tool.serverId,
        serverName: tool.serverName,
        tools: [],
      };
      group.tools.push(tool);
      grouped.set(tool.serverId, group);
    }
    return [...grouped.values()];
  }, [tools]);

  const selectedToolGrants = useMemo(() => draft.selectedTools.flatMap((key) => {
    const tool = tools.find((candidate) => candidate.key === key);
    return tool && isMcpToolGrantable(tool, draft.runtimeVersion)
      ? [{
        serverId: tool.serverId,
        toolName: tool.name,
        schemaSha256: tool.schemaSha256 ?? "",
        safetyClass: tool.safetyClass,
      }]
      : [];
  }).filter((grant) => grant.serverId && grant.toolName && grant.schemaSha256), [draft.runtimeVersion, draft.selectedTools, tools]);

  const selectedDifyApp = difyApps.find((app) => app.id === draft.difyAppId);

  const saveBlockersFor = (candidate: ListenerDraft) => automaticDeliveryBlockers({
    deliveryMode: candidate.deliveryMode,
    webhookConfigured: candidate.webhookMode === "replace"
      ? Boolean(candidate.webhookUrl.trim())
      : candidate.webhookConfigured,
    webhookVerified: candidate.webhookMode === "keep" && candidate.webhookVerified,
    selectedToolCount: selectedToolGrants.length,
    answerEngine: candidate.answerEngine,
    difyAppId: candidate.difyAppId,
    difyAppEnabled: difyApps.find((app) => app.id === candidate.difyAppId)?.enabled,
    difyConnectionTestCurrent: difyApps.find((app) => app.id === candidate.difyAppId)?.connectionTestCurrent,
  });

  const saveBlockers = saveBlockersFor(draft);
  const editorIssueCount = [
    !draft.groupId,
    !draft.name.trim(),
    draft.answerEngine === "mcp" && !selectedToolGrants.length,
    draft.answerEngine === "dify" && !draft.difyAppId,
    unavailableToolKeys.length > 0,
    tuningValidationErrors(draft.tuning).length > 0,
  ].filter(Boolean).length;

  const validateDraft = (candidate: ListenerDraft, requireWebhook = false): boolean => {
    if (!candidate.groupId) {
      toast.error("请选择一个群聊");
      return false;
    }
    if (!candidate.name.trim()) {
      toast.error("请填写监听器名称");
      return false;
    }
    if (candidate.answerEngine === "mcp" && !selectedToolGrants.length) {
      toast.error("请至少授权一个已发现的 MCP 工具");
      return false;
    }
    if (candidate.answerEngine === "dify" && !candidate.difyAppId) {
      toast.error("请选择一个 Dify Chatflow 应用");
      return false;
    }
    if (candidate.answerEngine === "dify" && !difyApps.some((app) => app.id === candidate.difyAppId)) {
      toast.error("所选 Dify Chatflow 应用已不存在，请刷新后重新选择");
      return false;
    }
    const timingErrors = tuningValidationErrors(candidate.tuning);
    if (timingErrors.length) {
      toast.error("时序或并发设置无效", { description: timingErrors[0] });
      return false;
    }
    for (const key of candidate.selectedTools) {
      const strategy = candidate.toolStrategies[key];
      if (!strategy?.bootstrap) continue;
      try {
        const parsed = JSON.parse(strategy.bootstrapArgumentsText || "{}") as unknown;
        if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error();
      } catch {
        toast.error("首轮必查参数无效", { description: "固定参数必须是有效的 JSON 对象。" });
        return false;
      }
    }
    if (candidate.webhookMode === "replace"
      && !candidate.webhookUrl.trim().startsWith("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=")) {
      toast.error("请填写官方企微群机器人 webhook 地址");
      return false;
    }
    const webhookConfigured = candidate.webhookMode === "replace"
      ? Boolean(candidate.webhookUrl.trim())
      : candidate.webhookMode === "keep" && candidate.webhookConfigured;
    if (requireWebhook && !webhookConfigured) {
      toast.error("请先配置企微群机器人 webhook，再发送测试消息");
      return false;
    }
    const blockers = saveBlockersFor(candidate);
    if (blockers.length) {
      toast.error("自动发送尚未通过安全门", { description: blockers[0] });
      return false;
    }
    return true;
  };

  const persistListenerDraft = async (candidate: ListenerDraft): Promise<ListenerDraft> => {
    const webhookPatch = candidate.webhookMode === "replace"
      ? { mode: "replace" as const, value: candidate.webhookUrl.trim() }
      : candidate.webhookMode === "clear" ? { mode: "clear" as const } : { mode: "keep" as const };
    const body = buildListenerSaveBody({
      draft: candidate,
      toolGrants: selectedToolGrants,
      webhookEdit: webhookPatch,
    });
    const rawResult = await executeListenerSave<unknown>({
      draft: candidate,
      body,
      readRevision: async () => {
        const latest = await bridge.replyRuntimeQuery<Record<string, unknown>>(createQuery({ kind: "listener.list" }));
        const revision = latest.revision;
        if (typeof revision !== "number" || !Number.isSafeInteger(revision) || revision < 0) {
          throw new Error("后台没有返回有效的配置版本，请刷新后重试。");
        }
        return revision;
      },
      execute: (command) => bridge.replyRuntimeExecute<unknown>(command),
    });
    const result = listenerSaveResultFromWire(rawResult);
    const savedListener = listenerFromWire(result.listener);
    if (!savedListener.id) throw new Error("后台保存成功，但没有返回监听器 ID");
    const nextRevision = result.revision;
    return {
      ...candidate,
      id: savedListener.id,
      revision: nextRevision,
      webhookUrl: "",
      webhookMode: savedListener.webhookConfigured ? "keep" : "replace",
      webhookConfigured: savedListener.webhookConfigured,
      webhookVerified: savedListener.webhookVerified,
      deliveryMode: savedListener.deliveryMode,
    };
  };

  const save = async () => {
    if (!validateDraft(draft)) return;
    setBusy("save");
    try {
      const savedDraft = await persistListenerDraft(draft);
      setRuntimeRevision(savedDraft.revision ?? runtimeRevision);
      setPersistedGroupId(savedDraft.groupId);
      toast.success(draft.id ? "监听器已更新" : "监听器已创建");
      setEditorOpen(false);
      await load(true);
    } catch (error) {
      toast.error("保存失败", { description: toUserErrorMessage(error, "请检查群聊、工具授权和 webhook。") });
    } finally {
      setBusy("");
    }
  };

  const remove = async (listener: ReplyListenerSummary) => {
    if (!confirm(`删除监听器“${listener.name}”？历史记录会保留，但不会再监听新消息。`)) return;
    setBusy(listener.id);
    try {
      await bridge.replyRuntimeExecute(createCommand({ kind: "listener.delete", listenerId: listener.id }, runtimeRevision));
      toast.success("监听器已删除");
      await load(true);
    } catch (error) {
      toast.error("删除失败", { description: toUserErrorMessage(error, "请稍后重试。") });
    } finally {
      setBusy("");
    }
  };

  const testRequiresSave = !draft.id
    || draft.webhookMode !== "keep"
    || draft.groupId !== persistedGroupId;

  const testWebhook = async () => {
    const safeDraft = testRequiresSave && (!draft.webhookVerified || draft.groupId !== persistedGroupId)
      ? { ...draft, deliveryMode: "review" as const }
      : draft;
    if (testRequiresSave) {
      if (!validateDraft(safeDraft, true)) return;
    } else if (!draft.webhookConfigured) {
      return void toast.error("请先配置企微群机器人 webhook，再发送测试消息");
    }
    setBusy("webhook-test");
    let persistedDraft = safeDraft;
    if (testRequiresSave) {
      try {
        persistedDraft = await persistListenerDraft(safeDraft);
        setDraft(persistedDraft);
        setPersistedGroupId(persistedDraft.groupId);
        setRuntimeRevision(persistedDraft.revision ?? runtimeRevision);
      } catch (error) {
        toast.error("保存失败", {
          description: toUserErrorMessage(error, "配置尚未保存，请检查群聊、工具授权和 webhook。"),
        });
        setBusy("");
        return;
      }
    }
    try {
      if (!persistedDraft.id) throw new Error("监听器尚未持久化，无法发送测试消息");
      const testExpectedRevision = testRequiresSave
        ? persistedDraft.revision
        : runtimeRevision;
      const result = await bridge.replyRuntimeExecute<Record<string, unknown>>(createCommand({
        kind: "listener.test_webhook",
        listenerId: persistedDraft.id,
      }, testExpectedRevision));
      const nextRevision = Number(result.revision ?? persistedDraft.revision ?? runtimeRevision);
      setDraft({ ...persistedDraft, revision: nextRevision });
      setTestChallenge({
        listenerId: persistedDraft.id,
        challengeId: String(result.challengeId ?? result.testId ?? ""),
        code: String(result.code ?? result.testCode ?? ""),
        revision: nextRevision,
      });
      setRuntimeRevision(nextRevision);
      toast.success("测试消息已发送", {
        description: testRequiresSave
          ? "配置已安全保存；请到当前选择的群聊确认随机码。"
          : "请到当前选择的群聊确认随机码。",
      });
    } catch (error) {
      toast.error(testRequiresSave ? "配置已保存，但 webhook 测试失败" : "webhook 测试失败", {
        description: toUserErrorMessage(error, "请检查机器人地址和群配置。"),
      });
    } finally {
      setBusy("");
    }
  };

  const confirmWebhook = async () => {
    if (!testChallenge) return;
    setBusy("webhook-confirm");
    try {
      const result = await bridge.replyRuntimeExecute<Record<string, unknown>>(createCommand({
        kind: "listener.confirm_webhook",
        listenerId: testChallenge.listenerId,
        challengeId: testChallenge.challengeId,
        appearedInSelectedGroup: true,
      }, testChallenge.revision));
      const nextRevision = Number(result.revision ?? testChallenge.revision);
      setRuntimeRevision(nextRevision);
      setDraft((current) => ({ ...current, revision: nextRevision, webhookVerified: true, webhookConfigured: true, webhookMode: "keep" }));
      setTestChallenge(null);
      toast.success("群归属已确认", { description: "现在可以选择自动发送。" });
      await load(true);
    } catch (error) {
      toast.error("确认失败", { description: toUserErrorMessage(error, "测试可能已经过期，请重新发送。") });
    } finally {
      setBusy("");
    }
  };

  const workAction = async (item: ReplyWorkItem, kind: WorkActionKind) => {
    const retryingUnknownDelivery = item.stage === "delivery_unknown" && kind !== "work.discard";
    if (retryingUnknownDelivery && !confirm("请先到群里核实：确认上一条消息确实没有出现后再重新发送。即使已核实，网络延迟仍可能造成重复消息。是否继续？")) return;
    if (kind === "work.send_plain_at" && !confirm("普通文本 @姓名 不会触发企微真正提醒。仍然发送吗？")) return;
    if (kind === "work.continue_without_images" && !confirm("缺失的图片不会参与分析，回答可能不完整。确认只使用当前可读的文字和图片继续吗？")) return;
    if (kind === "work.discard" && !confirm(item.stage === "needs_image"
      ? "放弃这条等待图片的任务？此操作不会在群里发送任何消息。"
      : "放弃这条待发送回复？此操作不会在群里发送任何消息。")) return;
    setBusy(`${kind}:${item.id}`);
    try {
      await bridge.replyRuntimeExecute(createCommand(
        buildWorkActionBody(kind, item.id, item.version, retryingUnknownDelivery),
        runtimeRevision,
      ));
      const successCopy: Record<WorkActionKind, string> = {
        "work.send": "回复已提交发送",
        "work.send_plain_at": "回复已提交发送",
        "work.discard": "任务已放弃",
        "work.retry": "任务已重新进入检索队列",
        "work.retry_images": "已重新开始读取图片",
        "work.continue_without_images": "已使用当前可读内容重新分析",
      };
      toast.success(successCopy[kind]);
      closeWorkDetail();
      await load(true);
    } catch (error) {
      toast.error("操作失败", { description: toUserErrorMessage(error, "状态可能已经变化，请刷新后重试。") });
    } finally {
      setBusy("");
    }
  };

  const exportDiagnostics = async (item: ReplyWorkItem) => {
    setBusy(`work.export_diagnostics:${item.id}`);
    try {
      const result = await bridge.replyRuntimeExecute<Record<string, unknown>>(createCommand({
        kind: "work.export_diagnostics",
        workId: item.id,
      }, runtimeRevision));
      const path = String(result.path ?? "");
      toast.success("诊断包已导出", { description: path || "已写入 Agent 日志目录。" });
      if (path) await bridge.openPath(path);
    } catch (error) {
      toast.error("导出失败", { description: toUserErrorMessage(error, "请检查日志目录是否可写。") });
    } finally {
      setBusy("");
    }
  };

  const filteredGroups = groups.filter((group) => {
    const label = String(group.display_name ?? group.name ?? group.id ?? group.conversation_id ?? "");
    return !groupSearch || label.toLowerCase().includes(groupSearch.toLowerCase());
  });
  const runtimeAvailability = runtimeAvailabilityCopy(snapshot.running);

  const changeWorkFilters = (next: WorkFilters, resetPages: boolean) => {
    setWorkFilters(next);
    if (!resetPages) return;
    setActivePage((value) => ({ ...value, page: 1 }));
    setPendingPage((value) => ({ ...value, page: 1 }));
    setHistoryPage((value) => ({ ...value, page: 1 }));
  };

  return (
    <div className="page-content runtime-page reply-page">
      <SectionHeader
        title="群监听回复"
        description="只在识别到问题、等过群友、取得可复核业务证据并独立审核后，才允许生成发送动作。"
        action={(
          <div className="runtime-header-actions">
            <Button
              variant="secondary"
              onClick={() => { void load(); void loadEditorOptions(); }}
              disabled={loading}
            ><RefreshCw size={13} className={loading ? "spin" : undefined} />刷新</Button>
            <Button onClick={openCreate} disabled={loading}><Plus size={14} />新增监听</Button>
          </div>
        )}
      />

      {!loading && listeners.length === 0 && (
        <div className="runtime-onboarding-banner">
          <div><Sparkles size={18} /><span><strong>三步启用群内自动答疑</strong><small>1. 选择群聊与回答引擎 · 2. 授权只读 MCP 工具或选择 Dify · 3. 完成企微机器人群内测试后启用</small></span></div>
          <Button onClick={openCreate}><Plus size={14} />创建第一个监听</Button>
        </div>
      )}

      <div className="runtime-command-bar">
        <div className={`runtime-live-indicator ${snapshot.running === true ? "" : "is-stopped"}`}>
          <span className="runtime-live-pulse" />
          <span><strong>{runtimeAvailability.label}</strong><small>{runtimeAvailability.description}</small></span>
        </div>
        <div className="runtime-command-metrics">
          <span><Gauge size={12} />处理中 <strong>{activePage.total}</strong></span>
          <span><Inbox size={12} />待发送 <strong>{snapshot.pendingCount ?? pendingPage.total}</strong></span>
          <span><AlertTriangle size={12} />近期异常 <strong>{snapshot.recentFailures ?? historyItems.filter((item) => item.status === "failed").length}</strong></span>
        </div>
      </div>

      <div className="runtime-tabs" role="tablist">
        <button className={view === "listeners" ? "selected" : ""} onClick={() => setView("listeners")}><Network size={13} />监听配置 <span>{listeners.length}</span></button>
        <button className={view === "active" ? "selected" : ""} onClick={() => setView("active")}><Activity size={13} />处理中 <span>{activePage.total}</span></button>
        <button className={view === "pending" ? "selected" : ""} onClick={() => setView("pending")}><Inbox size={13} />待发送 <span>{pendingPage.total}</span></button>
        <button className={view === "history" ? "selected" : ""} onClick={() => setView("history")}><History size={13} />处理历史 <span>{historyPage.total}</span></button>
      </div>

      {view !== "listeners" && (
        <WorkQueue
          view={view}
          filters={workFilters}
          onFiltersChange={changeWorkFilters}
          activeItems={activeItems}
          pendingItems={pendingItems}
          historyItems={historyItems}
          activePage={activePage}
          pendingPage={pendingPage}
          historyPage={historyPage}
          onActivePageChange={(page) => setActivePage((current) => ({ ...current, page }))}
          onPendingPageChange={(page) => setPendingPage((current) => ({ ...current, page }))}
          onHistoryPageChange={(page) => setHistoryPage((current) => ({ ...current, page }))}
          onOpenWork={openWorkDetail}
        />
      )}

      {view === "listeners" && (
        <ListenerList
          loading={loading}
          listeners={listeners}
          difyApps={difyApps}
          onCreate={openCreate}
          onEdit={openEdit}
          onRemove={remove}
        />
      )}

      {editorOpen && (
        <ListenerEditorDrawer
          draft={draft}
          busy={busy}
          onDraftChange={setDraft}
          onClose={() => setEditorOpen(false)}
          filteredGroups={filteredGroups}
          groupSearch={groupSearch}
          onGroupSearchChange={setGroupSearch}
          onClearTestChallenge={() => setTestChallenge(null)}
          difyApps={difyApps}
          selectedDifyApp={selectedDifyApp}
          toolGroups={toolGroups}
          unavailableToolKeys={unavailableToolKeys}
          expandedMcpServers={expandedMcpServers}
          onExpandedMcpServersChange={setExpandedMcpServers}
          advancedOpen={advancedOpen}
          onAdvancedOpenChange={setAdvancedOpen}
          testChallenge={testChallenge}
          testRequiresSave={testRequiresSave}
          saveBlockers={saveBlockers}
          editorIssueCount={editorIssueCount}
          onSave={save}
          onTestWebhook={testWebhook}
          onConfirmWebhook={confirmWebhook}
        />
      )}

      {detail && (
        <WorkDetailDrawer
          detail={detail}
          busy={busy}
          onClose={closeWorkDetail}
          workAction={workAction}
          onExportDiagnostics={exportDiagnostics}
        />
      )}
    </div>
  );
}
