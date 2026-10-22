import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { AppConfig, IssueFieldDefinition, PublishTarget, SmartSheetConfig } from "../../types";
import { PublishTargetsPage } from "../PublishTargetsPage";
import { MappingTab } from "./MappingTab";
import { PreviewTab } from "./PreviewTab";
import {
  issueAnalysisPromptOptions,
  mappingSourceOptions,
  previewDraft,
  readTencentDraft,
  validateDraft,
} from "./logic";

const issueField = (key: string, label: string, type: IssueFieldDefinition["type"] = "text"): IssueFieldDefinition => ({
  key,
  label,
  type,
  required: false,
  instruction: "",
  options: [],
  default_value: "",
});

function config(publishTargets?: PublishTarget[]): AppConfig {
  const smartSheet: SmartSheetConfig = {
    default_template_id: "default",
    templates: [{
      id: "default",
      name: "旧版模板",
      url: "",
      webhook_url_env: "",
      webhook_url: "",
      batch_size: 50,
      schema: { f_desc: { title: "*问题描述", type: "text" } },
      field_mappings: [],
    }],
    upload: {
      token_endpoint: "",
      image_upload_endpoint: "",
      image_form_field: "media",
      propagation_wait_ms_after_uploads: 1500,
      delay_ms_between_image_uploads: 120,
      corpid: "",
      corpsecret: "",
    },
  };
  return {
    wxwork_db_dir: "",
    wxwork_keys_file: "",
    target_group_id: "",
    target_group_name: "",
    default_workspace: "D:/exports",
    timezone: "Asia/Shanghai",
    ocr: { base_url: "", api_key: "", model: "" },
    llm: { base_url: "", api_key: "", model: "" },
    prompts: {
      default_id: "prompt-a",
      items: [{
        id: "prompt-a",
        name: "问题分析提示词",
        description: "",
        content: "",
        issue_fields: [issueField("problem_description", "问题描述", "long_text")],
      }],
    },
    smart_sheet: smartSheet,
    publish_targets: publishTargets,
  };
}

const tencentTarget = (): PublishTarget => ({
  id: "tencent_default",
  name: "腾讯文档",
  kind: "tencent_smartsheet",
  config: {
    template_id: "default",
    schema: {
      f_desc: { title: "*问题描述", type: "text" },
      f_date: { title: "日期", type: "text" },
    },
    field_mappings: [
      { source_key: "problem_description", target_field_id: "f_desc", target_type: "text", required: true, default_value: "" },
      { source_key: "$date", target_field_id: "f_date", target_type: "text", required: false, default_value: "" },
    ],
    transforms: { f_date: { mode: "date", date_format: "YYYY-MM-DD" } },
  },
});

describe("publish UI smoke render", () => {
  it("renders the three-tab page and its target list", () => {
    const appConfig = config([tencentTarget()]);
    const markup = renderToStaticMarkup(
      <PublishTargetsPage config={appConfig} onSave={async () => {}} />,
    );
    expect(markup).toContain("发布目标");
    expect(markup).toContain("腾讯文档 Smart Sheet");
    expect(markup).toContain("连接");
    expect(markup).toContain("字段映射");
    expect(markup).toContain("预览");
  });

  it("renders legacy smart_sheet templates as fallback targets", () => {
    const appConfig = config(undefined);
    const markup = renderToStaticMarkup(
      <PublishTargetsPage config={appConfig} onSave={async () => {}} />,
    );
    expect(markup).toContain("旧版模板");
    expect(markup).toContain("Excel 导出");
  });

  it("renders the mapping tab with source, target and transform controls", () => {
    const appConfig = config([tencentTarget()]);
    const target = tencentTarget();
    const draft = readTencentDraft(appConfig, target);
    const sources = mappingSourceOptions(appConfig, draft.prompt_id);
    const prompts = issueAnalysisPromptOptions(appConfig);
    const markup = renderToStaticMarkup(
      <MappingTab
        draft={draft}
        sources={sources}
        prompts={prompts}
        problems={validateDraft(draft, sources)}
        templates={appConfig.smart_sheet.templates}
        onPromptChange={() => {}}
        onChange={() => {}}
      />,
    );
    expect(markup).toContain("提示词与来源字段");
    expect(markup).toContain("添加映射");
    expect(markup).toContain("目标字段 Schema");
    expect(markup).toContain("日期格式化");
    expect(markup).toContain("从旧版模板导入");
    expect(markup).toContain("从腾讯文档粘贴 Schema");
    expect(markup).toContain("腾讯字段数组");
  });

  it("renders the preview tab with every target column", () => {
    const appConfig = config([tencentTarget()]);
    const draft = readTencentDraft(appConfig, tencentTarget());
    const sources = mappingSourceOptions(appConfig, draft.prompt_id);
    const preview = previewDraft(draft, sources);
    const markup = renderToStaticMarkup(
      <PreviewTab preview={preview} promptName="问题分析提示词" />,
    );
    expect(markup).toContain("最终写入值");
    expect(markup).toContain("问题描述");
    expect(markup).toContain("日期");
    expect(markup).toContain("正常");
  });
});
