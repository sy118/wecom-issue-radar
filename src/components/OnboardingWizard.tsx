import { useId, useState } from "react";
import { ArrowRight, CheckCircle2, ChevronLeft, Download, LoaderCircle, MessageSquareReply, Radar, X } from "lucide-react";
import { toast } from "sonner";
import { bridge } from "../lib/bridge";
import { toUserErrorMessage } from "../lib/errors";
import { useDialogA11y } from "../lib/useDialogA11y";
import {
  PATH_STEPS,
  buildTrialExportRequest,
  localDateString,
  pathLabel,
  pickFirstGroup,
  stepLabel,
  type OnboardingPath,
  type OnboardingProgress,
} from "../lib/onboarding";
import type { AppConfig, PageId } from "../types";
import { Button, Field, Input } from "./ui";

const PATH_TARGET: Record<OnboardingPath, PageId> = {
  export: "wecom-issue",
  reply: "wecom-reply",
  wechat: "wechat-workbench",
};

const PATH_DESCRIPTION: Record<OnboardingPath, string> = {
  export: "把本机企微群聊整理成 Excel/Markdown，不需要模型也能用。",
  reply: "在群里发现问题后自动拟稿，默认只拟稿、你确认后再发。",
  wechat: "读取个人微信会话、生成回复草稿；正在接入，将在 M2 融合版本上线。",
};

export function OnboardingWizard({
  config,
  progress,
  onChange,
  onSaveConfig,
  onClose,
  onNavigate,
}: {
  config: AppConfig;
  progress: OnboardingProgress;
  onChange: (next: OnboardingProgress) => void;
  onSaveConfig: (config: AppConfig) => Promise<void>;
  onClose: () => void;
  onNavigate: (page: PageId) => void;
}) {
  const [detecting, setDetecting] = useState(false);
  const [directories, setDirectories] = useState<string[]>([]);
  const [wecomRunning, setWecomRunning] = useState<boolean | null>(null);
  const [keysRequested, setKeysRequested] = useState(false);
  const [keysConfirmed, setKeysConfirmed] = useState(false);
  const [draft, setDraft] = useState<AppConfig>(config);
  const [saving, setSaving] = useState(false);
  const [modelSaved, setModelSaved] = useState(false);
  const [trialBusy, setTrialBusy] = useState(false);
  const [trialDayDir, setTrialDayDir] = useState("");
  const titleId = useId();
  // Guided flow: Tab is trapped in the panel, but Escape never abandons the
  // wizard — the user must finish or explicitly choose 跳过引导.
  const panelRef = useDialogA11y<HTMLDivElement>({
    open: true,
    closeOnEscape: false,
    initialFocus: "panel",
  });

  const path = progress.path;
  const steps = path ? PATH_STEPS[path] : [];
  const stepKey = path ? steps[Math.min(progress.step, steps.length - 1)] : "";
  const isLast = Boolean(path && progress.step >= steps.length - 1);

  const detect = async () => {
    setDetecting(true);
    try {
      const result = await bridge.detectEnvironment();
      setDirectories(result.dataDirectories);
      setWecomRunning(result.running);
      if (result.dataDirectories.length > 0) {
        const next = { ...config, wxwork_db_dir: result.dataDirectories[0] };
        setDraft(next);
        await onSaveConfig(next);
        toast.success("已保存检测到的企业微信目录");
      }
    } catch (error) {
      toast.error("自动检测失败", {
        description: toUserErrorMessage(error, "可以先跳过，稍后在设置里手动选择目录。"),
      });
    } finally {
      setDetecting(false);
    }
  };

  const saveModel = async () => {
    setSaving(true);
    try {
      await onSaveConfig({ ...draft, llm: draft.llm });
      setModelSaved(true);
      toast.success("模型配置已保存");
    } catch (error) {
      toast.error("保存失败", { description: toUserErrorMessage(error, "请稍后重试。") });
    } finally {
      setSaving(false);
    }
  };

  /** Path A finish step: one group, today only, export files only — never analysis or a doc write. */
  const runTrialExport = async () => {
    setTrialBusy(true);
    try {
      const response = await bridge.listGroups();
      const group = pickFirstGroup(response.groups);
      if (!group) {
        toast.warning("还没有读到群聊", {
          description: "可以先去「问题雷达」点『读取群聊』确认数据目录和密钥，或直接跳过这一步。",
        });
        return;
      }
      const request = buildTrialExportRequest({
        date: localDateString(),
        group,
        promptId: config.prompts.default_id,
      });
      const result = await bridge.runTask(request);
      const failedRun = result.runs?.find((run) => run.status === "failed");
      if (result.status === "failed" || failedRun) {
        const reason = failedRun?.error || "企业微信数据可能还没读出来，请稍后重试。";
        toast.error("试导出没有成功", {
          description: toUserErrorMessage(reason, "可以先跳过，稍后在「问题雷达」里手动导出。"),
        });
        return;
      }
      const dayDir = result.dayDir || result.runs?.find((run) => run.dayDir)?.dayDir || "";
      setTrialDayDir(dayDir);
      onChange({ ...progress, completed: true, skipped: false });
      toast.success(dayDir ? `已导出到 ${dayDir}` : "已导出今天的数据", {
        description: "可以在「问题雷达」查看 Excel 和 Markdown，随时能重新导出。",
      });
      onNavigate("wecom-issue");
    } catch (error) {
      toast.error("试导出失败", {
        description: toUserErrorMessage(error, "可以先跳过，稍后在「问题雷达」里手动导出。"),
      });
    } finally {
      setTrialBusy(false);
    }
  };

  const launchKeys = async () => {
    try {
      await bridge.launchKeyExtraction();
      setKeysRequested(true);
      toast.success("已打开密钥提取窗口", {
        description: "按窗口里的提示操作，完成后回来勾选下方确认。",
      });
    } catch (error) {
      toast.error("无法打开密钥提取", {
        description: toUserErrorMessage(error, "请确认企业微信已登录并保持运行。"),
      });
    }
  };

  const finish = () => {
    onChange({ ...progress, completed: true, skipped: false });
    if (path) onNavigate(PATH_TARGET[path]);
    else onClose();
  };

  const skip = () => {
    onChange({ ...progress, completed: true, skipped: true });
    onClose();
  };

  const next = () => {
    if (!path) return;
    if (isLast) finish();
    else onChange({ ...progress, step: progress.step + 1 });
  };

  const back = () => {
    if (progress.step === 0 && path) {
      onChange({ ...progress, path: null, step: 0 });
      return;
    }
    onChange({ ...progress, step: Math.max(0, progress.step - 1) });
  };

  return (
    <div className="onboarding-backdrop">
      <div
        ref={panelRef}
        className="onboarding-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
      >
        <div className="onboarding-head">
          <div>
            <h1 id={titleId}>{path ? stepLabel(path, progress.step) : "先花 1 分钟跑通一件事"}</h1>
            <p>
              {path
                ? pathLabel(path) + " · 其他设置以后都能在设置里改。"
                : "选一条最接近你现在需求的路径，完成后就会落到对应页面。"}
            </p>
          </div>
          <button type="button" className="button button-ghost" onClick={skip} title="跳过引导">
            <X size={16} />
          </button>
        </div>

        <div className="onboarding-progress">
          {steps.map((step, index) => (
            <i key={step} className={index <= progress.step ? "active" : undefined} />
          ))}
          <span>{path ? `${Math.min(progress.step + 1, steps.length)}/${steps.length}` : "选择路径"}</span>
        </div>

        {!path && (
          <div className="onboarding-paths">
            {(["export", "reply", "wechat"] as OnboardingPath[]).map((item) => (
              <button
                key={item}
                type="button"
                className="onboarding-path"
                onClick={() => onChange({ ...progress, path: item, step: 0 })}
              >
                <strong>{pathLabel(item)}</strong>
                <span>{PATH_DESCRIPTION[item]}</span>
              </button>
            ))}
          </div>
        )}

        {path && stepKey === "detect" && (
          <div className="onboarding-body">
            <div className="onboarding-note">
              点击「自动检测」，应用会扫描企业微信安装位置和最近使用的数据目录。检测不到时，稍后可在
              设置 → 常用 → 企业微信数据里手动选择文件夹。
            </div>
            <div>
              <Button variant="secondary" onClick={() => void detect()} disabled={detecting}>
                {detecting ? <LoaderCircle className="spin" size={15} /> : <Radar size={15} />}
                {detecting ? "检测中…" : "自动检测"}
              </Button>
            </div>
            {(directories.length > 0 || wecomRunning !== null) && (
              <div className="onboarding-diag">
                <strong>{wecomRunning ? "企业微信正在运行" : "未检测到运行中的企业微信"}</strong>
                {directories.length > 0 ? (
                  directories.map((directory) => <code key={directory}>{directory}</code>)
                ) : (
                  <span>没有自动找到数据目录，请手动选择。</span>
                )}
              </div>
            )}
          </div>
        )}

        {path && stepKey === "keys" && (
          <div className="onboarding-body">
            <div className="onboarding-note">
              保持企业微信已登录并运行，点击「提取密钥」。密钥只保存在本机，用于读取本地群聊数据库。
            </div>
            <div>
              <Button variant="secondary" onClick={() => void launchKeys()}>
                {keysRequested ? "重新打开密钥提取" : "提取密钥"}
              </Button>
            </div>
            <label className="onboarding-note" style={{ display: "flex", gap: 10, alignItems: "center" }}>
              <input
                type="checkbox"
                checked={keysConfirmed}
                onChange={(event) => setKeysConfirmed(event.target.checked)}
              />
              密钥提取窗口已经提示成功
            </label>
          </div>
        )}

        {path && stepKey === "model" && (
          <div className="onboarding-body">
            <div className="onboarding-note">
              连接模型后，群监听才能自动拟稿。暂时不配也可以先导出 Excel，只是不会自动整理问题。
            </div>
            <Field label="Base URL" hint="例如 https://api.deepseek.com/v1，或你的 OpenAI 兼容服务地址">
              <Input
                value={draft.llm.base_url}
                onChange={(event) => setDraft({ ...draft, llm: { ...draft.llm, base_url: event.target.value } })}
                placeholder="https://api.example.com/v1"
              />
            </Field>
            <Field label="API Key">
              <Input
                type="password"
                value={draft.llm.api_key}
                onChange={(event) => setDraft({ ...draft, llm: { ...draft.llm, api_key: event.target.value } })}
                placeholder="sk-..."
              />
            </Field>
            <Field label="模型名称" hint="例如 deepseek-chat、gpt-4o-mini 或你们自己的模型名">
              <Input
                value={draft.llm.model}
                onChange={(event) => setDraft({ ...draft, llm: { ...draft.llm, model: event.target.value } })}
                placeholder="deepseek-chat"
              />
            </Field>
            <div>
              <Button variant="secondary" onClick={() => void saveModel()} disabled={saving}>
                {saving ? "保存中…" : "保存模型配置"}
              </Button>
            </div>
            {modelSaved && (
              <div className="onboarding-note">
                <CheckCircle2 size={18} /> 已保存。下一步只是去创建监听，不会自动创建，也不会自动发送。
              </div>
            )}
          </div>
        )}

        {path && stepKey === "listener" && (
          <div className="onboarding-body">
            <div className="onboarding-note">
              模型已就绪。下一步去「群监听回复」创建一个<strong>只拟稿</strong>监听：它在群里发现问题后生成草稿，默认不发送，
              需要你确认后才可能发出。
            </div>
            <div className="onboarding-diag">
              <strong>建议先这样建</strong>
              <span>· 只勾一个自己每天盯的群，先跑通一天。</span>
              <span>· 保持「自动发送」关闭，草稿质量稳定后再考虑打开。</span>
              <span>· 不会替你创建任何监听，下面只是跳到那个页面。</span>
            </div>
            <div>
              <Button
                onClick={() => {
                  onChange({ ...progress, completed: true, skipped: false });
                  onNavigate("wecom-reply");
                }}
              >
                <MessageSquareReply size={15} /> 去创建只拟稿监听
              </Button>
            </div>
          </div>
        )}

        {path && stepKey === "wechat-coming" && (
          <div className="onboarding-body">
            <div className="onboarding-note">
              个人微信的会话读取、草稿和「填入但不发送」会在 M2 融合版本上线。你现在可以先完成企微侧配置。
            </div>
          </div>
        )}

        {path && stepKey === "finish" && (
          <div className="onboarding-body">
            {path === "export" && (
              <>
                <div className="onboarding-note">
                  <CheckCircle2 size={18} /> 配置完成。建议先跑一次今天的导出，确认数据能读出来。
                </div>
                <div>
                  <Button variant="secondary" onClick={() => void runTrialExport()} disabled={trialBusy}>
                    {trialBusy ? <LoaderCircle className="spin" size={15} /> : <Download size={15} />}
                    {trialBusy ? "正在导出今天的数据…" : "试导今天的数据（只导出，不分析）"}
                  </Button>
                </div>
                <div className="onboarding-diag">
                  <strong>这一步只做</strong>
                  <span>· 取第一个群聊、今天 00:00–23:59，导出 Excel + Markdown。</span>
                  <span>· 不跑 OCR、不做 AI 分析、不写腾讯文档。</span>
                  {trialDayDir && <code>{trialDayDir}</code>}
                </div>
              </>
            )}
            {path === "reply" && (
              <>
                <div className="onboarding-note">
                  <CheckCircle2 size={18} /> 配置完成。群监听默认只拟稿：发现问题先生成草稿，你确认后才可能发出。
                </div>
                <div className="onboarding-diag">
                  <strong>接下来</strong>
                  <span>· 群监听回复：创建只拟稿监听，先盯一个群跑一天。</span>
                  <span>· 自动发送保持关闭，确认草稿质量后再决定是否打开。</span>
                </div>
              </>
            )}
            {path === "wechat" && (
              <>
                <div className="onboarding-note">
                  <CheckCircle2 size={18} /> 个人微信能力正在接入，将在 M2 融合版本上线；现在可以先看看占位页面。
                </div>
                <div>
                  <Button
                    variant="secondary"
                    onClick={() => {
                      onChange({ ...progress, completed: true, skipped: false });
                      onNavigate("wechat-workbench");
                    }}
                  >
                    <ArrowRight size={15} /> 看看个人微信页（即将上线）
                  </Button>
                </div>
              </>
            )}
          </div>
        )}

        <div className="onboarding-actions">
          <Button variant="ghost" onClick={skip}>跳过引导</Button>
          <div style={{ display: "flex", gap: 8 }}>
            {path && (
              <Button variant="secondary" onClick={back}>
                <ChevronLeft size={15} /> 上一步
              </Button>
            )}
            {path && stepKey !== "finish" && (
              <Button
                onClick={next}
                disabled={stepKey === "keys" && !keysConfirmed}
              >
                下一步
              </Button>
            )}
            {path && stepKey === "finish" && (
              <Button onClick={finish} disabled={trialBusy}>
                进入应用
              </Button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
