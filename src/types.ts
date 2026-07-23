export type PageId = "run" | "prompts" | "settings" | "about";

export interface ModelConfig {
  provider?: string;
  base_url: string;
  api_key: string;
  model: string;
  temperature?: number;
  timeout_seconds?: number;
  max_input_chars?: number;
  max_output_tokens?: number;
  concurrency?: number;
}

export interface PromptItem {
  id: string;
  name: string;
  description: string;
  content: string;
}

export interface PromptConfig {
  default_id: string;
  items: PromptItem[];
}

export interface SmartSheetConfig {
  url: string;
  webhook_url_env: string;
  webhook_url: string;
  batch_size: number;
  schema: Record<string, unknown>;
  defaults: Record<string, unknown>;
  upload: {
    token_endpoint: string;
    image_upload_endpoint: string;
    image_form_field: string;
    propagation_wait_ms_after_uploads: number;
    delay_ms_between_image_uploads: number;
    corpid: string;
    corpsecret: string;
  };
}

export interface AppConfig {
  wxwork_db_dir: string;
  wxwork_keys_file: string;
  target_group_id: string;
  target_group_name: string;
  default_workspace: string;
  timezone: string;
  ocr: ModelConfig;
  llm: ModelConfig;
  prompts: PromptConfig;
  smart_sheet: SmartSheetConfig;
  [key: string]: unknown;
}
export interface BootstrapResult {
  config: AppConfig;
  configPath: string;
  appVersion: string;
}

export interface EnvironmentDetection {
  running: boolean;
  executablePaths: string[];
  dataDirectories: string[];
}

export interface GroupInfo {
  conversation_id?: string;
  id?: string;
  name?: string;
  display_name?: string;
  last_message_time?: string;
  [key: string]: unknown;
}

export interface TaskRequest {
  date: string;
  groupId: string;
  groupName: string;
  promptId: string;
  runOcr: boolean;
  runAnalysis: boolean;
  exportXlsx: boolean;
  exportMarkdown: boolean;
  prepareSmartSheet: boolean;
}

export interface SmartSheetPreview {
  total?: number;
  pending: number;
  already_synced: number;
  configured?: boolean;
}

export interface TaskResult {
  dayDir: string;
  outputs: Record<string, string>;
  definitionPath?: string | null;
  smartSheetPreview?: SmartSheetPreview | null;
}

export interface SyncResult {
  synced?: number;
  skipped?: number;
  [key: string]: unknown;
}
