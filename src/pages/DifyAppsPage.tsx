import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  Bot,
  CheckCircle2,
  ChevronRight,
  CircleOff,
  CloudCog,
  Edit3,
  FileUp,
  KeyRound,
  LoaderCircle,
  Plus,
  RefreshCw,
  Save,
  ShieldCheck,
  Trash2,
  X,
} from "lucide-react";
import { toast } from "sonner";
import { Button, Field, Input, SectionHeader, Switch } from "../components/ui";
import { bridge } from "../lib/bridge";
import { toUserErrorMessage } from "../lib/errors";
import { createCommand, createQuery } from "../lib/replyRuntimeUi";
import type { DifyAppSummary } from "../types";

type SecretMode = "keep" | "replace" | "clear";

interface DifyDraft {
  id?: string;
  revision: number;
  name: string;
  enabled: boolean;
  baseUrl: string;
  inputsText: string;
  apiKeyMode: SecretMode;
  apiKey: string;
  apiKeyConfigured: boolean;
  secretFingerprint: string;
}

const newDraft = (revision = 0): DifyDraft => ({
  revision,
  name: "",
  enabled: true,
  baseUrl: "https://api.dify.ai/v1",
  inputsText: "{}",
  apiKeyMode: "replace",
  apiKey: "",
  apiKeyConfigured: false,
  secretFingerprint: "",
});

function collection<T>(value: unknown, key: string): T[] {
  if (Array.isArray(value)) return value as T[];
  if (!value || typeof value !== "object") return [];
  const candidate = (value as Record<string, unknown>)[key];
  return Array.isArray(candidate) ? candidate as T[] : [];
}

export function difyAppFromWire(raw: Record<string, unknown>): DifyAppSummary {
  const secrets = (raw.secrets ?? {}) as Record<string, unknown>;
  const lastTest = (raw.lastTest ?? {}) as Record<string, unknown>;
  const capabilities = (raw.capabilities ?? {}) as DifyAppSummary["capabilities"];
  return {
    id: String(raw.id ?? ""),
    revision: Number(raw.revision ?? 0),
    name: String(raw.name ?? "未命名 Chatflow"),
    enabled: Boolean(raw.enabled),
    baseUrl: String(raw.baseUrl ?? ""),
    inputs: raw.inputs && typeof raw.inputs === "object" && !Array.isArray(raw.inputs)
      ? raw.inputs as Record<string, unknown>
      : {},
    secrets: {
      apiKeyConfigured: Boolean(secrets.apiKeyConfigured),
      fingerprint: String(secrets.fingerprint ?? ""),
    },
    capabilities,
    lastTest: {
      status: ["success", "failed"].includes(String(lastTest.status))
        ? String(lastTest.status) as "success" | "failed"
        : "never",
      testedAt: String(lastTest.testedAt ?? "") || undefined,
      error: lastTest.error,
    },
    connectionTestCurrent: Boolean(raw.connectionTestCurrent),
    updatedAt: String(raw.updatedAt ?? "") || undefined,
  };
}

function parseInputs(text: string): Record<string, unknown> {
  const value = JSON.parse(text || "{}") as unknown;
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("固定 inputs 必须是 JSON 对象");
  }
  return value as Record<string, unknown>;
}

function testState(app: DifyAppSummary) {
  if (!app.enabled) return { label: "已停用", tone: "neutral", detail: "配置保留，但监听器不会调用。" };
  if (app.connectionTestCurrent) return { label: "连接已验证", tone: "success", detail: "当前地址、inputs 与 API Key 已通过参数检查。" };
  if (app.lastTest?.status === "failed") {
    const error = app.lastTest.error && typeof app.lastTest.error === "object"
      ? String((app.lastTest.error as Record<string, unknown>).message ?? "")
      : String(app.lastTest.error ?? "");
    return { label: "最近测试失败", tone: "danger", detail: error || "请检查地址、API Key 和 Chatflow 发布状态。" };
  }
  if (app.lastTest?.status === "success") return { label: "配置已变化", tone: "warning", detail: "连接参数已改变，请重新测试。" };
  return { label: "尚未测试", tone: "neutral", detail: "保存后执行只读参数测试，不会产生对话。" };
}

export function DifyAppsPage() {
  const [apps, setApps] = useState<DifyAppSummary[]>([]);
  const [revision, setRevision] = useState(0);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState("");
  const [draft, setDraft] = useState<DifyDraft>(newDraft());
  const [editorOpen, setEditorOpen] = useState(false);

  const load = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    try {
      const value = await bridge.replyRuntimeQuery<Record<string, unknown>>(
        createQuery({ kind: "dify.list" }),
      );
      setApps(collection<Record<string, unknown>>(value, "apps").map(difyAppFromWire));
      setRevision(Number(value.revision ?? 0));
    } catch (error) {
      toast.error("无法读取 Dify 应用", {
        description: toUserErrorMessage(error, "请确认后台运行模块已经启动。"),
      });
    } finally {
      if (!quiet) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    let unlisten: (() => void) | undefined;
    void bridge.onReplyRuntimeEvent((event) => {
      if (String(event.kind ?? "").startsWith("dify.")) void load(true);
    }).then((dispose) => { unlisten = dispose; });
    return () => unlisten?.();
  }, [load]);

  const openCreate = () => {
    setDraft(newDraft(revision));
    setEditorOpen(true);
  };

  const openEdit = (app: DifyAppSummary) => {
    setDraft({
      id: app.id,
      revision,
      name: app.name,
      enabled: app.enabled,
      baseUrl: app.baseUrl,
      inputsText: JSON.stringify(app.inputs ?? {}, null, 2),
      apiKeyMode: app.secrets?.apiKeyConfigured ? "keep" : "replace",
      apiKey: "",
      apiKeyConfigured: Boolean(app.secrets?.apiKeyConfigured),
      secretFingerprint: app.secrets?.fingerprint ?? "",
    });
    setEditorOpen(true);
  };

  const save = async () => {
    if (!draft.name.trim()) return void toast.error("请填写 Dify 应用名称");
    let url: URL;
    try {
      url = new URL(draft.baseUrl.trim());
      if (!["http:", "https:"].includes(url.protocol)) throw new Error("invalid protocol");
    } catch {
      return void toast.error("Base URL 必须是有效的 HTTP 或 HTTPS 地址");
    }
    let inputs: Record<string, unknown>;
    try {
      inputs = parseInputs(draft.inputsText);
    } catch (error) {
      return void toast.error("固定 inputs 无效", { description: String(error) });
    }
    if (draft.apiKeyMode === "replace" && !draft.apiKey.trim()) {
      return void toast.error("请输入 Dify 应用 API Key");
    }
    setBusy(draft.id ?? "new");
    try {
      await bridge.replyRuntimeExecute(createCommand({
        kind: "dify.save",
        app: {
          ...(draft.id ? { id: draft.id } : {}),
          name: draft.name.trim(),
          enabled: draft.enabled,
          baseUrl: url.toString().replace(/\/$/, ""),
          inputs,
        },
        secretPatch: {
          apiKey: draft.apiKeyMode === "replace"
            ? { mode: "replace", value: draft.apiKey.trim() }
            : { mode: draft.apiKeyMode },
        },
      }, draft.revision));
      toast.success(draft.id ? "Dify 应用已更新" : "Dify 应用已创建");
      setEditorOpen(false);
      await load(true);
    } catch (error) {
      toast.error("保存失败", { description: toUserErrorMessage(error, "请检查连接配置。") });
    } finally {
      setBusy("");
    }
  };

  const test = async (app: DifyAppSummary) => {
    setBusy(app.id);
    try {
      await bridge.replyRuntimeExecute(createCommand({ kind: "dify.test", appId: app.id }, revision));
      toast.success("Dify 连接测试成功", { description: "已读取 Chatflow inputs 与附件能力；没有产生对话。" });
    } catch (error) {
      toast.error("Dify 连接测试失败", { description: toUserErrorMessage(error, "请检查应用 API Key 和发布状态。") });
    } finally {
      await load(true);
      setBusy("");
    }
  };

  const remove = async (app: DifyAppSummary) => {
    if (!confirm(`删除 Dify 应用“${app.name}”？被监听器引用时后台会拒绝删除。`)) return;
    setBusy(app.id);
    try {
      await bridge.replyRuntimeExecute(createCommand({ kind: "dify.delete", appId: app.id }, revision));
      toast.success("Dify 应用已删除");
      await load(true);
    } catch (error) {
      toast.error("删除失败", { description: toUserErrorMessage(error, "请先解除监听器引用。") });
    } finally {
      setBusy("");
    }
  };

  const enabledCount = apps.filter((app) => app.enabled).length;
  const verifiedCount = apps.filter((app) => app.connectionTestCurrent).length;
  const fileReadyCount = useMemo(() => apps.filter((app) => Object.values(app.capabilities?.fileUpload ?? {})
    .some((capability) => capability.enabled && capability.transferMethods.includes("local_file"))).length, [apps]);

  return (
    <div className="page-content runtime-page dify-page">
      <SectionHeader
        title="Dify 接入"
        description="登记已发布的 Chatflow 应用。API Key 只保存在本机后台；连接测试只读取参数，不启动工作流。"
        action={<div className="runtime-header-actions"><Button variant="secondary" onClick={() => void load()} disabled={loading}><RefreshCw size={13} className={loading ? "spin" : undefined} />刷新</Button><Button onClick={openCreate}><Plus size={14} />新增应用</Button></div>}
      />

      <div className="runtime-kpi-strip">
        <div><CloudCog size={16} /><span><strong>{apps.length}</strong><small>Chatflow 应用</small></span></div>
        <div><Bot size={16} /><span><strong>{enabledCount}</strong><small>已启用</small></span></div>
        <div><ShieldCheck size={16} /><span><strong>{verifiedCount}</strong><small>当前配置已验证</small></span></div>
        <div><FileUp size={16} /><span><strong>{fileReadyCount}</strong><small>参数声明本地附件</small></span></div>
      </div>

      {loading && !apps.length ? <div className="runtime-empty"><LoaderCircle className="spin" /><strong>正在读取 Dify 应用</strong><span>API Key 不会回显或写入日志。</span></div>
        : !apps.length ? <div className="runtime-empty runtime-empty-framed"><CloudCog size={30} /><strong>还没有 Dify Chatflow</strong><span>添加应用 Base URL 与 API Key，测试后即可在群监听中选择。</span><Button onClick={openCreate}><Plus size={14} />添加第一个应用</Button></div>
          : <div className="dify-app-list">{apps.map((app) => {
            const state = testState(app);
            const inputs = app.capabilities?.inputVariables ?? [];
            const uploads = Object.entries(app.capabilities?.fileUpload ?? {});
            return <article className="dify-app-card" key={app.id}>
              <div className="dify-app-rail" data-status={state.tone} />
              <div className="dify-app-main">
                <div className="dify-app-heading"><div className="runtime-icon"><Bot size={16} /></div><div><h3>{app.name}</h3><p>{app.baseUrl}</p></div><span className={`runtime-status runtime-status-${state.tone}`}>{state.label}</span><span className="runtime-transport">CHATFLOW</span></div>
                {app.baseUrl.startsWith("http://") && <div className="dify-http-warning"><AlertTriangle size={12} />HTTP 会以明文传输 API Key 与对话内容，仅适合可信内网。</div>}
                <div className={`dify-test-note is-${state.tone}`}>{state.tone === "success" ? <CheckCircle2 size={13} /> : state.tone === "danger" || state.tone === "warning" ? <AlertTriangle size={13} /> : <Activity size={13} />}<span><strong>{state.label}{app.lastTest?.testedAt ? ` · ${new Date(app.lastTest.testedAt).toLocaleString("zh-CN", { hour12: false })}` : ""}</strong><small>{state.detail}</small></span></div>
                <div className="dify-capability-grid"><div><span>固定 inputs</span><strong>{Object.keys(app.inputs ?? {}).length}</strong><small>{inputs.length ? `${inputs.filter((item) => item.required).length}/${inputs.length} 个声明变量必填` : "Chatflow 未声明输入变量"}</small></div><div><span>参数声明的文件能力</span><strong>{uploads.filter(([, value]) => value.enabled && value.transferMethods.includes("local_file")).length}</strong><small>{uploads.length ? `${uploads.map(([name, value]) => `${name} ${value.enabled ? `≤${value.numberLimit || "默认"}` : "关闭"}`).join(" · ")}；实际以上传接口为准` : "尚未测试"}</small></div><div><span>API Key</span><strong>{app.secrets?.apiKeyConfigured ? "已配置" : "未配置"}</strong><small>{app.secrets?.fingerprint ? `指纹 ${app.secrets.fingerprint.slice(0, 12)}` : "只在后台保存"}</small></div></div>
              </div>
              <div className="dify-app-actions"><Button variant="secondary" disabled={busy === app.id} onClick={() => void test(app)}>{busy === app.id ? <LoaderCircle className="spin" size={13} /> : <Activity size={13} />}测试参数</Button><button title="编辑" onClick={() => openEdit(app)}><Edit3 size={14} /></button><button className="danger-icon" title="删除" onClick={() => void remove(app)}><Trash2 size={14} /></button><ChevronRight size={14} /></div>
            </article>;
          })}</div>}

      {editorOpen && <div className="modal-backdrop schedule-modal-backdrop" onMouseDown={(event) => { if (!busy && event.currentTarget === event.target) setEditorOpen(false); }}>
        <section className="schedule-modal runtime-drawer" role="dialog" aria-modal="true" aria-label={draft.id ? "编辑 Dify 应用" : "新增 Dify 应用"}>
          <header className="schedule-modal-header"><div><span className="drawer-eyebrow">CHATFLOW CONNECTION</span><h2>{draft.id ? "编辑 Dify 应用" : "新增 Dify 应用"}</h2></div><button onClick={() => setEditorOpen(false)} aria-label="关闭"><X size={16} /></button></header>
          <div className="schedule-modal-body runtime-drawer-body">
            <section className="runtime-form-section"><div className="runtime-form-heading"><CloudCog size={14} /><span><strong>应用连接</strong><small>一个配置对应一个已发布 Chatflow；Cloud 默认地址已预填。</small></span></div><Field label="应用名称"><Input value={draft.name} placeholder="例如：售后 Chatflow" onChange={(event) => setDraft({ ...draft, name: event.target.value })} /></Field><Field label="Base URL" hint="Cloud: https://api.dify.ai/v1；自部署填写实例的 /v1 地址。"><Input value={draft.baseUrl} onChange={(event) => setDraft({ ...draft, baseUrl: event.target.value })} /></Field>{draft.baseUrl.startsWith("http://") && <div className="runtime-safety-note is-danger"><AlertTriangle size={14} /><span><strong>明文 HTTP</strong>API Key、消息与附件会在网络中明文传输，请仅用于隔离且可信的内网。</span></div>}<Switch checked={draft.enabled} onChange={(enabled) => setDraft({ ...draft, enabled })} label="启用该应用" description="停用后保留配置，但监听器不会调用 Chatflow。" /></section>
            <section className="runtime-form-section"><div className="runtime-form-heading"><KeyRound size={14} /><span><strong>API Key</strong><small>已保存的 Key 永不回显；编辑时必须明确保留、替换或清空。</small></span></div>{draft.id && draft.apiKeyConfigured && <div className="runtime-segmented"><button type="button" className={draft.apiKeyMode === "keep" ? "selected" : ""} onClick={() => setDraft({ ...draft, apiKeyMode: "keep", apiKey: "" })}>保留</button><button type="button" className={draft.apiKeyMode === "replace" ? "selected" : ""} onClick={() => setDraft({ ...draft, apiKeyMode: "replace" })}>替换</button><button type="button" className={draft.apiKeyMode === "clear" ? "selected danger" : ""} onClick={() => setDraft({ ...draft, apiKeyMode: "clear", apiKey: "" })}>清空</button></div>}{draft.apiKeyMode === "replace" && <Field label="应用 API Key" hint="通常以 app- 开头；不会回显。"><Input type="password" value={draft.apiKey} placeholder="app-…" onChange={(event) => setDraft({ ...draft, apiKey: event.target.value })} /></Field>}{draft.apiKeyMode === "keep" && <p className="dify-secret-copy">保留当前 Key · 指纹 {draft.secretFingerprint.slice(0, 12) || "已生成"}</p>}{draft.apiKeyMode === "clear" && <div className="runtime-safety-note is-danger"><CircleOff size={14} /><span><strong>清空 API Key</strong>保存后应用无法测试或运行，直到重新配置。</span></div>}</section>
            <section className="runtime-form-section"><div className="runtime-form-heading"><Bot size={14} /><span><strong>固定 inputs</strong><small>只接受 JSON 数据；不会执行模板、表达式或脚本。</small></span></div><Field label="Inputs JSON"><textarea className="input runtime-textarea mono" spellCheck={false} value={draft.inputsText} onChange={(event) => setDraft({ ...draft, inputsText: event.target.value })} /></Field></section>
          </div>
          <footer className="schedule-modal-footer"><Button variant="secondary" onClick={() => setEditorOpen(false)} disabled={Boolean(busy)}>取消</Button><Button onClick={() => void save()} disabled={Boolean(busy)}>{busy ? <LoaderCircle className="spin" size={13} /> : <Save size={13} />}保存应用</Button></footer>
        </section>
      </div>}
    </div>
  );
}
