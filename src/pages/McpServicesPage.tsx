import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  Braces,
  Check,
  ChevronRight,
  CircleOff,
  DatabaseZap,
  Edit3,
  KeyRound,
  LoaderCircle,
  Network,
  Plus,
  RefreshCw,
  Save,
  ServerCog,
  ShieldCheck,
  Trash2,
  Wrench,
  X,
} from "lucide-react";
import { toast } from "sonner";
import { Button, Field, Input, SectionHeader, Switch } from "../components/ui";
import { bridge } from "../lib/bridge";
import { toUserErrorMessage } from "../lib/errors";
import {
  createCommand,
  createQuery,
  mcpCatalogAllowsGrants,
  mcpCatalogUnavailableLabel,
  parseRecordSecretEdit,
} from "../lib/replyRuntimeUi";
import type { McpServerSummary, McpToolSummary, McpTransportType } from "../types";

type SecretMode = "keep" | "replace" | "clear";

export interface McpDraft {
  id?: string;
  revision?: number;
  name: string;
  enabled: boolean;
  transportType: McpTransportType;
  url: string;
  command: string;
  argsText: string;
  envMode: SecretMode;
  envText: string;
  headersMode: SecretMode;
  headersText: string;
  envConfigured: boolean;
  headersConfigured: boolean;
  secretFingerprint: string;
}

interface CatalogEntry {
  serverId: string;
  tools: McpToolSummary[];
  error?: unknown;
  updatedAt?: string;
}

type McpServerWithCatalog = McpServerSummary & {
  catalog?: { toolCount?: number; updatedAt?: string; error?: unknown };
};

const newDraft = (): McpDraft => ({
  name: "",
  enabled: true,
  transportType: "streamable-http",
  url: "",
  command: "",
  argsText: "",
  envMode: "replace",
  envText: "{}",
  headersMode: "replace",
  headersText: "{}",
  envConfigured: false,
  headersConfigured: false,
  secretFingerprint: "",
});

export function mcpDraftFromServer(server: McpServerSummary, revision: number): McpDraft {
  return {
    id: server.id,
    revision,
    name: server.name,
    enabled: server.enabled,
    transportType: server.transportType,
    url: server.url ?? "",
    command: server.command ?? "",
    argsText: (server.args ?? []).join("\n"),
    envMode: "keep",
    envText: "{}",
    headersMode: "keep",
    headersText: "{}",
    envConfigured: Boolean(server.secrets?.envConfigured),
    headersConfigured: Boolean(server.secrets?.headersConfigured),
    secretFingerprint: server.secrets?.fingerprint ?? "",
  };
}

function collection<T>(value: unknown, keys: string[]): T[] {
  if (Array.isArray(value)) return value as T[];
  if (!value || typeof value !== "object") return [];
  const record = value as Record<string, unknown>;
  for (const key of keys) if (Array.isArray(record[key])) return record[key] as T[];
  return [];
}

function statusLabel(server: McpServerSummary) {
  if (!server.enabled) return { label: "已停用", tone: "neutral" };
  if (server.connectionStatus === "connected") return { label: "连接正常", tone: "success" };
  if (server.connectionStatus === "failed") return { label: "连接失败", tone: "danger" };
  if (server.connectionStatus === "connecting") return { label: "连接中", tone: "progress" };
  return { label: "等待测试", tone: "neutral" };
}

function transportLabel(value: McpTransportType) {
  if (value === "streamable-http") return "Streamable HTTP";
  return value === "stdio" ? "stdio" : "SSE";
}

function schemaLabel(tool: McpToolSummary) {
  if (tool.schemaStatus === "changed") return "Schema 已变化";
  if (tool.schemaStatus === "current") return "Schema 已确认";
  return "Schema 未确认";
}

export function McpServicesPage() {
  const [servers, setServers] = useState<McpServerSummary[]>([]);
  const [catalog, setCatalog] = useState<CatalogEntry[]>([]);
  const [runtimeRevision, setRuntimeRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busyId, setBusyId] = useState("");
  const [editorOpen, setEditorOpen] = useState(false);
  const [draft, setDraft] = useState<McpDraft>(newDraft);
  const [showSecrets, setShowSecrets] = useState(false);

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    try {
      const [listResult, catalogResult] = await Promise.all([
        bridge.replyRuntimeQuery<Record<string, unknown>>(createQuery({ kind: "mcp.list" })),
        bridge.replyRuntimeQuery<Record<string, unknown>>(createQuery({ kind: "mcp.catalog" })),
      ]);
      const nextServers = collection<McpServerWithCatalog>(listResult, ["items", "servers"]);
      setRuntimeRevision(Number(listResult.revision ?? 0));
      setServers(nextServers.map((server) => {
        const detail = server;
        return {
          ...server,
          toolCount: detail.catalog?.toolCount ?? server.toolCount,
          connectionStatus: detail.catalog?.error
            ? "failed"
            : detail.catalog?.updatedAt
              ? "connected"
              : "untested",
        };
      }));
      const catalogs = collection<CatalogEntry>(catalogResult, ["catalogs", "items", "servers", "catalog"])
        .map((entry) => ({
          ...entry,
          tools: (entry.tools ?? []).map((tool) => ({
            ...tool,
            schemaStatus: tool.schemaStatus ?? (tool.schemaSha256 ? "current" : "unknown"),
          })),
        }));
      setCatalog(catalogs);
    } catch (error) {
      setCatalog((current) => current.map((entry) => ({
        ...entry,
        error: entry.error || { code: "CATALOG_REFRESH_FAILED" },
      })));
      toast.error("无法读取 MCP 服务", {
        description: toUserErrorMessage(error, "请确认后台运行模块已经启动。"),
      });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    let unlisten: (() => void) | undefined;
    void bridge.onReplyRuntimeEvent((event) => {
      if (String(event.kind ?? "").startsWith("mcp.")) void load(true);
    }).then((dispose) => { unlisten = dispose; });
    return () => unlisten?.();
  }, [load]);

  const toolsByServer = useMemo(() => new Map(catalog.map((entry) => [entry.serverId, entry.tools])), [catalog]);
  const catalogByServer = useMemo(() => new Map(catalog.map((entry) => [entry.serverId, entry])), [catalog]);
  const serverById = useMemo(() => new Map(servers.map((server) => [server.id, server])), [servers]);
  const grantableToolCount = useMemo(() => catalog.reduce((count, entry) => {
    if (!mcpCatalogAllowsGrants(serverById.get(entry.serverId), entry)) return count;
    return count + entry.tools.filter((tool) => tool.schemaStatus === "current").length;
  }, 0), [catalog, serverById]);

  const openCreate = () => {
    setDraft({ ...newDraft(), revision: runtimeRevision });
    setShowSecrets(false);
    setEditorOpen(true);
  };

  const openEdit = (server: McpServerSummary) => {
    setDraft(mcpDraftFromServer(server, runtimeRevision));
    setShowSecrets(false);
    setEditorOpen(true);
  };

  const save = async () => {
    if (!draft.name.trim()) {
      toast.error("请填写 MCP 服务名称");
      return;
    }
    if (draft.transportType === "stdio" ? !draft.command.trim() : !draft.url.trim()) {
      toast.error(draft.transportType === "stdio" ? "请填写启动命令" : "请填写服务地址");
      return;
    }
    try {
      parseRecordSecretEdit(draft.envMode, draft.envText);
      parseRecordSecretEdit(draft.headersMode, draft.headersText);
    } catch {
      toast.error("秘密配置必须是合法的 JSON 对象");
      return;
    }

    setBusyId(draft.id ?? "new");
    try {
      await bridge.replyRuntimeExecute(createCommand({
        kind: "mcp.save",
        server: {
          ...(draft.id ? { id: draft.id } : {}),
          name: draft.name.trim(),
          enabled: draft.enabled,
          transportType: draft.transportType,
          url: draft.transportType === "stdio" ? null : draft.url.trim(),
          command: draft.transportType === "stdio" ? draft.command.trim() : null,
          args: draft.transportType === "stdio"
            ? draft.argsText.split("\n").map((item) => item.trim()).filter(Boolean)
            : [],
        },
        secretPatch: {
          env: parseRecordSecretEdit(draft.envMode, draft.envText),
          headers: parseRecordSecretEdit(draft.headersMode, draft.headersText),
        },
      }, draft.revision));
      toast.success(draft.id ? "MCP 服务已更新" : "MCP 服务已创建");
      setEditorOpen(false);
      await load(true);
    } catch (error) {
      toast.error("保存失败", { description: toUserErrorMessage(error, "请检查连接配置后重试。") });
    } finally {
      setBusyId("");
    }
  };

  const remove = async (server: McpServerSummary) => {
    if (!confirm(`删除 MCP 服务“${server.name}”？已授权该工具的监听器将停止自动处理。`)) return;
    setBusyId(server.id);
    try {
      await bridge.replyRuntimeExecute(createCommand({ kind: "mcp.delete", serverId: server.id }, runtimeRevision));
      toast.success("MCP 服务已删除");
      await load(true);
    } catch (error) {
      toast.error("删除失败", { description: toUserErrorMessage(error, "请稍后重试。") });
    } finally {
      setBusyId("");
    }
  };

  const test = async (server: McpServerSummary) => {
    setBusyId(server.id);
    try {
      const result = await bridge.replyRuntimeExecute<{ tools?: McpToolSummary[] }>(createCommand({
        kind: "mcp.test",
        serverId: server.id,
      }, runtimeRevision));
      toast.success("连接成功", {
        description: `已发现 ${result?.tools?.length ?? 0} 个工具，Schema 指纹已刷新。`,
      });
      await load(true);
    } catch (error) {
      toast.error("连接测试失败", { description: toUserErrorMessage(error, "请检查地址、命令和秘密配置。") });
      await load(true);
    } finally {
      setBusyId("");
    }
  };

  return (
    <div className="page-content runtime-page mcp-page">
      <SectionHeader
        title="MCP 服务"
        description="连接知识与业务系统，发现工具，并以 Schema 指纹锁定监听器真正可调用的能力。"
        action={(
          <div className="runtime-header-actions">
            <Button variant="secondary" onClick={() => void load()} disabled={loading}>
              <RefreshCw size={13} className={loading ? "spin" : undefined} />刷新
            </Button>
            <Button onClick={openCreate}><Plus size={14} />新增服务</Button>
          </div>
        )}
      />

      <div className="runtime-kpi-strip">
        <div><ServerCog size={16} /><span><strong>{servers.length}</strong><small>已登记服务</small></span></div>
        <div><Activity size={16} /><span><strong>{servers.filter((server) => server.connectionStatus === "connected").length}</strong><small>连接正常</small></span></div>
        <div><Wrench size={16} /><span><strong>{servers.reduce((sum, server) => sum + (toolsByServer.get(server.id)?.length ?? server.toolCount ?? 0), 0)}</strong><small>已发现工具</small></span></div>
        <div><ShieldCheck size={16} /><span><strong>{grantableToolCount}</strong><small>可授权工具</small></span></div>
      </div>

      {loading && servers.length === 0 ? (
        <div className="runtime-empty"><LoaderCircle className="spin" /><strong>正在读取 MCP 服务</strong><span>连接信息和秘密不会写入运行日志。</span></div>
      ) : servers.length === 0 ? (
        <div className="runtime-empty runtime-empty-framed">
          <DatabaseZap size={30} />
          <strong>还没有 MCP 服务</strong>
          <span>添加一个 SSE、stdio 或 Streamable HTTP 服务，然后测试连接并发现工具。</span>
          <Button onClick={openCreate}><Plus size={14} />添加第一个服务</Button>
        </div>
      ) : (
        <div className="mcp-service-list">
          {servers.map((server) => {
            const status = statusLabel(server);
            const tools = toolsByServer.get(server.id) ?? server.tools ?? [];
            const catalogEntry = catalogByServer.get(server.id);
            const catalogUnavailable = mcpCatalogUnavailableLabel(server, catalogEntry);
            const configuredSecretCount = Number(Boolean(server.secrets?.envConfigured)) + Number(Boolean(server.secrets?.headersConfigured));
            return (
              <article className="mcp-service-card" key={server.id}>
                <div className="mcp-service-rail" data-status={status.tone} />
                <div className="mcp-service-main">
                  <div className="mcp-service-heading">
                    <div className="runtime-icon"><Network size={16} /></div>
                    <div>
                      <h3>{server.name}</h3>
                      <p>{server.transportType === "stdio" ? [server.command, ...(server.args ?? [])].filter(Boolean).join(" ") : server.url}</p>
                    </div>
                    <span className={`runtime-status runtime-status-${status.tone}`}>{status.label}</span>
                    <span className="runtime-transport">{transportLabel(server.transportType)}</span>
                  </div>

                  <details className="mcp-secret-disclosure">
                    <summary>
                      <span className="mcp-secret-disclosure-label"><KeyRound size={11} />连接凭据</span>
                      <span className={configuredSecretCount ? "configured" : ""}>
                        {configuredSecretCount
                          ? `${configuredSecretCount} 项已配置`
                          : "未配置"}
                      </span>
                      <ChevronRight className="mcp-secret-chevron" size={12} />
                    </summary>
                    <div className="mcp-secret-summary" aria-label="MCP 凭据配置状态">
                      <span className={server.secrets?.envConfigured ? "configured" : ""}><KeyRound size={10} />Env <strong>{server.secrets?.envConfigured ? "已配置" : "未配置"}</strong></span>
                      <span className={server.secrets?.headersConfigured ? "configured" : ""}><KeyRound size={10} />Headers <strong>{server.secrets?.headersConfigured ? "已配置" : "未配置"}</strong></span>
                      {server.secrets?.fingerprint && <span className="fingerprint">指纹 {server.secrets.fingerprint.slice(0, 12)}</span>}
                    </div>
                  </details>

                  <div className="mcp-tool-band">
                    <div className="mcp-tool-band-title"><Braces size={12} />已发现工具 <span>{tools.length}</span></div>
                    {catalogUnavailable && <p className="mcp-tool-empty"><AlertTriangle size={12} />{catalogUnavailable}。请恢复服务并重新测试、发现工具。</p>}
                    {tools.length ? (
                      <div className="mcp-tool-grid">
                        {tools.map((tool) => (
                          <div className="mcp-tool-chip" key={tool.name} title={tool.description}>
                            <span><Wrench size={11} />{tool.title || tool.name}</span>
                            <small className={catalogUnavailable || tool.schemaStatus === "changed" ? "schema-changed" : ""}>
                              {catalogUnavailable || tool.schemaStatus === "changed" ? <AlertTriangle size={10} /> : <Check size={10} />}
                              {catalogUnavailable || schemaLabel(tool)}
                            </small>
                          </div>
                        ))}
                      </div>
                    ) : <p className="mcp-tool-empty">测试连接后在这里查看并授权精确工具。</p>}
                  </div>
                </div>
                <div className="mcp-service-actions">
                  <Button variant="secondary" disabled={busyId === server.id} onClick={() => void test(server)}>
                    {busyId === server.id ? <LoaderCircle className="spin" size={13} /> : <Activity size={13} />}测试并发现
                  </Button>
                  <button title="编辑" onClick={() => openEdit(server)}><Edit3 size={14} /></button>
                  <button className="danger-icon" title="删除" onClick={() => void remove(server)}><Trash2 size={14} /></button>
                  <ChevronRight size={14} />
                </div>
              </article>
            );
          })}
        </div>
      )}

      {editorOpen && (
        <div className="modal-backdrop schedule-modal-backdrop" onMouseDown={(event) => {
          if (event.currentTarget === event.target) setEditorOpen(false);
        }}>
          <section className="schedule-modal runtime-drawer" role="dialog" aria-modal="true" aria-label={draft.id ? "编辑 MCP 服务" : "新增 MCP 服务"}>
            <header className="schedule-modal-header">
              <div><span className="drawer-eyebrow">CAPABILITY CONNECTION</span><h2>{draft.id ? "编辑 MCP 服务" : "新增 MCP 服务"}</h2></div>
              <button onClick={() => setEditorOpen(false)} aria-label="关闭"><X size={16} /></button>
            </header>
            <div className="schedule-modal-body runtime-drawer-body">
              <section className="runtime-form-section">
                <div className="runtime-form-heading"><Network size={14} /><span><strong>连接身份</strong><small>给运营人员可识别的名称，并选择 transport。</small></span></div>
                <Field label="服务名称"><Input value={draft.name} placeholder="例如：售后知识库" onChange={(event) => setDraft({ ...draft, name: event.target.value })} /></Field>
                <Field label="传输方式">
                  <select className="input" value={draft.transportType} onChange={(event) => setDraft({ ...draft, transportType: event.target.value as McpTransportType })}>
                    <option value="streamable-http">Streamable HTTP</option>
                    <option value="sse">SSE</option>
                    <option value="stdio">stdio</option>
                  </select>
                </Field>
                {draft.transportType === "stdio" ? (
                  <>
                    <Field label="启动命令" hint="只填写可执行程序，不要把秘密写进参数。"><Input value={draft.command} placeholder="uvx" onChange={(event) => setDraft({ ...draft, command: event.target.value })} /></Field>
                    <Field label="参数" hint="每行一个参数。"><textarea className="input runtime-textarea compact" value={draft.argsText} placeholder={"mcp-atlassian\n--read-only"} onChange={(event) => setDraft({ ...draft, argsText: event.target.value })} /></Field>
                  </>
                ) : (
                  <Field label="服务地址"><Input value={draft.url} placeholder={draft.transportType === "sse" ? "http://127.0.0.1:3000/sse" : "http://127.0.0.1:3000/mcp"} onChange={(event) => setDraft({ ...draft, url: event.target.value })} /></Field>
                )}
                <Switch checked={draft.enabled} onChange={(enabled) => setDraft({ ...draft, enabled })} label="启用该服务" description="停用后保留配置，但监听器不会连接或调用工具。" />
              </section>

              <section className="runtime-form-section">
                <div className="runtime-form-heading"><KeyRound size={14} /><span><strong>秘密处理</strong><small>已保存的值永不回显；修改必须明确选择保留、替换或清空。</small></span></div>
                <button className="runtime-secret-reveal" type="button" onClick={() => setShowSecrets((value) => !value)}>
                  <ShieldCheck size={13} />{showSecrets ? "收起秘密编辑" : "编辑 Env / Headers"}<ChevronRight size={12} />
                </button>
                {showSecrets && (
                  <div className="runtime-secret-grid">
                    <SecretEditor label="Env JSON" mode={draft.envMode} value={draft.envText} configured={draft.envConfigured} fingerprint={draft.secretFingerprint} onMode={(envMode) => setDraft({ ...draft, envMode })} onValue={(envText) => setDraft({ ...draft, envText })} disabled={draft.transportType !== "stdio"} />
                    <SecretEditor label="Headers JSON" mode={draft.headersMode} value={draft.headersText} configured={draft.headersConfigured} fingerprint={draft.secretFingerprint} onMode={(headersMode) => setDraft({ ...draft, headersMode })} onValue={(headersText) => setDraft({ ...draft, headersText })} disabled={draft.transportType === "stdio"} />
                  </div>
                )}
              </section>

              <div className="runtime-safety-note"><CircleOff size={14} /><span><strong>保存不会自动扩大权限</strong>监听器只能调用之后明确勾选、且 Schema 指纹未变化的工具。</span></div>
            </div>
            <footer className="schedule-modal-footer">
              <Button variant="secondary" onClick={() => setEditorOpen(false)}>取消</Button>
              <Button onClick={() => void save()} disabled={Boolean(busyId)}>{busyId ? <LoaderCircle className="spin" size={13} /> : <Save size={13} />}保存服务</Button>
            </footer>
          </section>
        </div>
      )}
    </div>
  );
}

function SecretEditor({
  label,
  mode,
  value,
  configured,
  fingerprint,
  onMode,
  onValue,
  disabled,
}: {
  label: string;
  mode: SecretMode;
  value: string;
  configured: boolean;
  fingerprint: string;
  onMode: (mode: SecretMode) => void;
  onValue: (value: string) => void;
  disabled: boolean;
}) {
  return (
    <div className={`runtime-secret-editor ${disabled ? "is-disabled" : ""}`}>
      <div className="runtime-secret-heading"><strong>{label}</strong><small>{disabled ? "当前 transport 不使用" : configured ? `已配置 · 指纹 ${fingerprint.slice(0, 12) || "已生成"}` : "未配置"}</small></div>
      <div className="runtime-segmented">
        <button type="button" className={mode === "keep" ? "selected" : ""} disabled={disabled} onClick={() => onMode("keep")}>保留</button>
        <button type="button" className={mode === "replace" ? "selected" : ""} disabled={disabled} onClick={() => onMode("replace")}>替换</button>
        <button type="button" className={mode === "clear" ? "selected danger" : ""} disabled={disabled} onClick={() => onMode("clear")}>清空</button>
      </div>
      {mode === "replace" && !disabled && <textarea className="input runtime-textarea compact mono" spellCheck={false} value={value} onChange={(event) => onValue(event.target.value)} />}
      {mode === "keep" && !disabled && <p>保持当前已保存的秘密，不从前端读取。</p>}
      {mode === "clear" && !disabled && <p className="danger-copy">保存后永久清除当前秘密。</p>}
    </div>
  );
}
