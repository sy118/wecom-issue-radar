import { useEffect, useMemo, useState } from "react";
import {
  Copy,
  Eraser,
  Inbox,
  LoaderCircle,
  Pencil,
  Plus,
  Save,
  Search,
  Send,
  Trash2,
  X,
} from "lucide-react";
import { toast } from "sonner";

import { Button, Field, Input } from "../../components/ui";
import { toUserErrorMessage } from "../../lib/errors";
import { deletePhrase, fillWechat, listPhrases, savePhrase, supportsWechatFill } from "../../components/daily/api";
import {
  PHRASE_VARIABLE_NAMES,
  applyVariables,
  defaultVariableValues,
  draftFromPhrase,
  emptyPhraseDraft,
  extractVariables,
  filterPhrases,
  missingVariables,
  phraseCategories,
  todayDate,
  trimText,
  upsertPhrase,
  validatePhraseDraft,
} from "../../components/daily/logic";
import type { Phrase, PhraseDraft } from "../../components/daily/types";
import { DailyBusy, DailyError, DailyNotice, DailyPageHeader, copyText } from "../../components/daily/ui";
import { DailyPanel } from "./DailyPanel";
import "./daily.css";

const ALL_CATEGORIES = "全部";

/**
 * 话术库：分类/搜索/增删改 + {{姓名}}/{{日期}}/{{金额}} 变量替换，
 * 一键复制或填入微信输入框（只填不发）。
 */
export function DailyPhrasesPage() {
  const [phrases, setPhrases] = useState<Phrase[]>([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [query, setQuery] = useState("");
  const [category, setCategory] = useState(ALL_CATEGORIES);
  const [editor, setEditor] = useState<PhraseDraft | null>(null);
  const [editorError, setEditorError] = useState("");
  const [saving, setSaving] = useState(false);
  const [confirmDeleteId, setConfirmDeleteId] = useState("");
  const [deletingId, setDeletingId] = useState("");
  const [fillingId, setFillingId] = useState("");
  const [chatTitle, setChatTitle] = useState("");
  const [variables, setVariables] = useState(() => defaultVariableValues());

  const load = async () => {
    setLoading(true);
    setLoadError("");
    try {
      setPhrases(await listPhrases());
    } catch (reason) {
      setLoadError(toUserErrorMessage(reason, "读取话术失败，请稍后重试。"));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const categories = useMemo(() => phraseCategories(phrases), [phrases]);
  const visible = useMemo(() => filterPhrases(phrases, query, category), [phrases, query, category]);
  // The three built-ins are always shown; template-specific placeholders such as
  // {{事由}} come from the backend so built-in phrases stay fillable too.
  const variableNames = useMemo(() => {
    const names: string[] = [...PHRASE_VARIABLE_NAMES];
    for (const phrase of phrases) {
      for (const name of phrase.variables) {
        if (!names.includes(name)) names.push(name);
      }
    }
    return names;
  }, [phrases]);

  const openNew = () => {
    setEditor(emptyPhraseDraft());
    setEditorError("");
  };

  const openEdit = (phrase: Phrase) => {
    setEditor(draftFromPhrase(phrase));
    setEditorError("");
  };

  const save = async () => {
    if (!editor) return;
    const message = validatePhraseDraft(editor);
    if (message) {
      setEditorError(message);
      return;
    }
    setSaving(true);
    setEditorError("");
    try {
      const saved = await savePhrase(editor);
      setPhrases((current) => upsertPhrase(current, saved));
      setEditor(null);
      toast.success(editor.id ? "话术已更新" : "话术已保存");
    } catch (reason) {
      setEditorError(toUserErrorMessage(reason, "保存失败，请稍后重试。"));
    } finally {
      setSaving(false);
    }
  };

  const remove = async (phrase: Phrase) => {
    if (confirmDeleteId !== phrase.id) {
      setConfirmDeleteId(phrase.id);
      return;
    }
    setDeletingId(phrase.id);
    try {
      await deletePhrase(phrase.id);
      setPhrases((current) => current.filter((item) => item.id !== phrase.id));
      setConfirmDeleteId("");
      toast.success("话术已删除");
    } catch (reason) {
      toast.error("删除失败", { description: toUserErrorMessage(reason, "请稍后重试。") });
    } finally {
      setDeletingId("");
    }
  };

  const copy = async (content: string) => {
    const applied = applyVariables(content, variables);
    const copied = await copyText(applied);
    if (copied) toast.success("已复制到剪贴板", { description: "变量已按填写内容替换。" });
    else toast.error("复制失败，请手动选择文本复制");
  };

  const fill = async (phrase: Phrase) => {
    const applied = applyVariables(phrase.content, variables);
    const missing = missingVariables(phrase.content, variables);
    if (missing.length > 0) {
      toast.warning(`还有变量没填：${missing.join("、")}`, { description: "未填写的占位符会原样保留。" });
    }
    const title = chatTitle.trim();
    if (!title) {
      toast.warning("请先填写目标聊天标题", { description: "标题要和微信聊天窗口里显示的完全一致。" });
      return;
    }
    if (!supportsWechatFill()) {
      const copied = await copyText(applied);
      toast.warning("当前版本不支持填入微信", {
        description: copied ? "文案已复制，请手动粘贴到微信输入框。" : "请手动复制文案到微信输入框。",
      });
      return;
    }
    setFillingId(phrase.id);
    try {
      const response = await fillWechat(title, applied);
      toast.success(`已填入「${response.chatTitle}」`, {
        description: `${response.characters} 字；只填入不发送，请确认后自己点发送。`,
      });
    } catch (reason) {
      const copied = await copyText(applied);
      toast.warning("填入失败，已降级为复制", {
        description: copied
          ? `${toUserErrorMessage(reason, "个人微信可能未连接。")} 文案已复制，可手动粘贴。`
          : toUserErrorMessage(reason, "个人微信可能未连接，请手动复制文案。"),
      });
    } finally {
      setFillingId("");
    }
  };

  const insertVariable = (name: string) => {
    if (!editor) return;
    const token = `{{${name}}}`;
    setEditor({ ...editor, content: `${editor.content}${token}` });
  };

  return (
    <div className="page-content dl-page">
      <DailyPageHeader
        eyebrow="沟通助手"
        title="话术库"
        description="请假、催进度、拒绝、道歉、汇报这些常用场景存成模板，填好变量就能直接用。"
        actions={
          <Button onClick={openNew}>
            <Plus size={15} /> 新建话术
          </Button>
        }
      />

      <DailyPanel
        title="筛选与变量"
        description="搜索标题、内容或分类；变量会替换所有话术里的占位符，留空的变量原样保留。"
        actions={<span className="dl-hint">显示 {visible.length} / {phrases.length} 条</span>}
      >
        <div className="dl-toolbar">
          <Field label="搜索" className="dl-grow">
            <div className="dl-search">
              <Search size={15} aria-hidden="true" />
              <Input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="搜标题、内容或分类"
              />
            </div>
          </Field>
          <Field label="分类">
            <select
              className="input"
              value={category}
              onChange={(event) => setCategory(event.target.value)}
              aria-label="分类筛选"
            >
              <option value={ALL_CATEGORIES}>{ALL_CATEGORIES}</option>
              {categories.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          </Field>
          <Field label="填入微信的目标聊天标题" className="dl-grow">
            <Input
              value={chatTitle}
              onChange={(event) => setChatTitle(event.target.value)}
              placeholder="例如：客户-张先生"
            />
          </Field>
        </div>

        <div className="dl-variable-block">
          <div className="dl-variable-head">
            <span className="dl-variable-title">变量</span>
            <span className="dl-hint">留空的变量会原样保留占位符。</span>
          </div>
          <div className="dl-variable-grid">
            {variableNames.map((name) => (
              <Field key={name} label={`{{${name}}}`} className="dl-variable-field">
                <Input
                  value={variables[name] ?? ""}
                  onChange={(event) => setVariables((current) => ({ ...current, [name]: event.target.value }))}
                  placeholder={
                    name === "日期" ? todayDate() : name === "金额" ? "例如：1,280" : `例如：${name}内容`
                  }
                />
              </Field>
            ))}
          </div>
        </div>
      </DailyPanel>

      {loadError && <DailyError message={loadError} onRetry={() => void load()} />}

      {loading && (
        <div className="dl-loading-panel">
          <DailyBusy label="正在读取话术…" />
        </div>
      )}

      {!loading && !loadError && phrases.length === 0 && (
        <div className="dl-empty-state">
          <Inbox size={20} aria-hidden="true" />
          <div>
            <strong>还没有话术</strong>
            <p>先存一条常用场景，比如请假申请或催进度；之后填好变量就能一键复制或填入微信。</p>
            <div className="dl-empty-actions">
              <Button variant="secondary" onClick={openNew}>
                <Plus size={15} /> 新建第一条话术
              </Button>
            </div>
          </div>
        </div>
      )}

      {!loading && phrases.length > 0 && visible.length === 0 && (
        <div className="dl-empty-state">
          <Search size={20} aria-hidden="true" />
          <div>
            <strong>没有匹配的话术</strong>
            <p>换个关键词或分类试试；也可以清空搜索条件看全部话术。</p>
          </div>
        </div>
      )}

      {visible.length > 0 && (
        <div className="dl-phrase-grid">
          {visible.map((phrase) => {
            const applied = applyVariables(phrase.content, variables);
            const missing = missingVariables(phrase.content, variables);
            const confirming = confirmDeleteId === phrase.id;
            return (
              <article className="glass-card dl-phrase-card" key={phrase.id}>
                <div className="dl-phrase-head">
                  <div className="dl-phrase-title">
                    <h3>{phrase.title}</h3>
                    <span className="dl-badge">{phrase.category}</span>
                  </div>
                  <span className="dl-counter">{trimText(phrase.content).length} 字</span>
                </div>
                <p className="dl-phrase-content">{applied}</p>
                {missing.length > 0 && (
                  <p className="dl-hint">待填变量：{missing.join("、")}</p>
                )}
                <div className="dl-phrase-actions">
                  <Button variant="secondary" onClick={() => void copy(phrase.content)}>
                    <Copy size={14} /> 复制
                  </Button>
                  <Button
                    onClick={() => void fill(phrase)}
                    disabled={fillingId === phrase.id}
                  >
                    {fillingId === phrase.id ? <LoaderCircle className="spin" size={14} /> : <Send size={14} />}
                    {fillingId === phrase.id ? "填入中…" : "填入微信"}
                  </Button>
                  <Button
                    variant="ghost"
                    onClick={() => openEdit(phrase)}
                    aria-label={`编辑话术：${phrase.title}`}
                    title="编辑话术"
                  >
                    <Pencil size={14} />
                  </Button>
                  {confirming ? (
                    <Button
                      variant="danger"
                      onClick={() => void remove(phrase)}
                      disabled={deletingId === phrase.id}
                    >
                      {deletingId === phrase.id ? <LoaderCircle className="spin" size={14} /> : <Trash2 size={14} />}
                      确认删除
                    </Button>
                  ) : (
                    <Button
                      variant="ghost"
                      onClick={() => void remove(phrase)}
                      disabled={deletingId === phrase.id}
                      aria-label={`删除话术：${phrase.title}`}
                      title="删除话术"
                    >
                      <Trash2 size={14} />
                    </Button>
                  )}
                </div>
              </article>
            );
          })}
        </div>
      )}

      {editor && (
        <DailyPanel
          className="dl-editor"
          title={editor.id ? "编辑话术" : "新建话术"}
          description={`用 {{变量}} 标记可替换内容，复制/填入时会自动替换。`}
          actions={
            <Button variant="ghost" onClick={() => setEditor(null)} aria-label="关闭话术编辑器" title="关闭">
              <X size={15} />
            </Button>
          }
        >
          <div className="dl-field-grid">
            <Field label="标题">
              <Input
                value={editor.title}
                onChange={(event) => setEditor({ ...editor, title: event.target.value })}
                placeholder="例如：请假一天"
              />
            </Field>
            <Field label="分类">
              <Input
                value={editor.category}
                onChange={(event) => setEditor({ ...editor, category: event.target.value })}
                placeholder="例如：请假 / 催进度 / 拒绝 / 道歉 / 汇报"
                list="dl-phrase-categories"
              />
            </Field>
          </div>
          <datalist id="dl-phrase-categories">
            {categories.map((item) => (
              <option key={item} value={item} />
            ))}
          </datalist>

          <Field label="内容" hint="可用变量见下方按钮；{{姓名}} {{日期}} {{金额}} 会自动替换。">
            <textarea
              className="textarea"
              value={editor.content}
              onChange={(event) => setEditor({ ...editor, content: event.target.value })}
              placeholder="例如：{{姓名}}，我今天身体不太舒服，想请一天假，工作我会先交接给同事。"
              rows={6}
            />
          </Field>

          <div className="dl-chip-row">
            <span className="dl-variable-title">插入变量</span>
            {variableNames.map((name) => (
              <Button
                key={name}
                variant="secondary"
                className="dl-chip-button"
                onClick={() => insertVariable(name)}
              >
                <Plus size={13} /> {`{{${name}}}`}
              </Button>
            ))}
            <span className="dl-hint">
              已用变量：{extractVariables(editor.content).join("、") || "无"}
            </span>
          </div>

          {editorError && <DailyError message={editorError} />}

          <div className="dl-action-bar">
            <Button onClick={() => void save()} disabled={saving}>
              {saving ? <LoaderCircle className="spin" size={15} /> : <Save size={15} />}
              {saving ? "保存中…" : "保存话术"}
            </Button>
            <Button variant="secondary" onClick={() => setEditor(null)}>
              <Eraser size={15} /> 取消
            </Button>
            <span className="dl-hint">话术只保存在本机，不会自动发送。</span>
          </div>
        </DailyPanel>
      )}

      <DailyNotice>
        话术只保存在本机；复制或填入前请确认变量替换结果，不会自动发送。
      </DailyNotice>
    </div>
  );
}
