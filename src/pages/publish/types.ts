import type {
  AppConfig,
  IssueFieldDefinition,
  PublishTarget,
  SmartSheetFieldMapping,
  SmartSheetFieldSchema,
  SmartSheetTemplate,
} from "../../types";

export const SYSTEM_MAPPING_SOURCES = [
  "$date",
  "$images",
  "$sender",
  "$message_time",
  "$issue_key",
] as const;

export type PublishTransformMode = "direct" | "constant" | "template" | "date" | "enum";

/** Per-target-field transform stored at `publish_targets[].config.transforms`. */
export interface PublishTransform {
  mode: PublishTransformMode;
  constant?: string;
  template?: string;
  date_format?: string;
  enum_map?: Record<string, string>;
}

export type PublishTransforms = Record<string, PublishTransform>;
export type PublishSchema = Record<string, SmartSheetFieldSchema>;

/** Editable view of a Tencent Smart Sheet publish target's `config` block. */
export interface TencentDraft {
  template_id: string;
  url: string;
  webhook_url: string;
  webhook_url_env: string;
  batch_size: number;
  auto_sync: boolean;
  prompt_id: string;
  schema: PublishSchema;
  field_mappings: SmartSheetFieldMapping[];
  transforms: PublishTransforms;
}

export interface PromptOption {
  id: string;
  name: string;
  fields: IssueFieldDefinition[];
  strategy: boolean;
}

export interface SourceOption {
  key: string;
  label: string;
  type: string;
  system: boolean;
  options: string[];
}

export interface MappingProblem {
  key: string;
  level: "error" | "warning";
  mappingIndex?: number;
  message: string;
}

export interface PreviewRow {
  fieldId: string;
  title: string;
  type: string;
  sourceKey: string;
  mode: PublishTransformMode;
  value: unknown;
  display: string;
  mapped: boolean;
  sourceMissing: boolean;
}

export interface PreviewResult {
  rows: PreviewRow[];
  problems: MappingProblem[];
  sample: Array<{ key: string; label: string; display: string; system: boolean }>;
}

export interface PublishSaveResult {
  config: AppConfig;
  targets: PublishTarget[];
}

export interface UploadDraft {
  token_endpoint: string;
  image_upload_endpoint: string;
  image_form_field: string;
  propagation_wait_ms_after_uploads: number;
  delay_ms_between_image_uploads: number;
  corpid: string;
  corpsecret: string;
}

export type LegacyTemplate = SmartSheetTemplate;

export const FALLBACK_ISSUE_FIELDS: IssueFieldDefinition[] = [
  {
    key: "problem_description",
    label: "问题描述",
    type: "long_text",
    required: true,
    instruction: "",
    options: [],
    default_value: "",
  },
];
