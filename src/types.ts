export type PageId = "run" | "schedules" | "prompts" | "mcp" | "reply" | "settings" | "about";

export type ReplyRuntimeCommandBody = { kind: string; [key: string]: unknown };

export interface ReplyRuntimeCommand<Body extends ReplyRuntimeCommandBody = ReplyRuntimeCommandBody> {
  protocolVersion: 1;
  commandId: string;
  expectedRevision?: number;
  body: Body;
}

export interface ReplyRuntimeQuery<Body extends ReplyRuntimeCommandBody = ReplyRuntimeCommandBody> {
  protocolVersion: 1;
  body: Body;
}

export interface ReplyRuntimeEvent {
  protocolVersion?: 1;
  eventId?: string;
  seq?: number;
  kind?: string;
  [key: string]: unknown;
}

export type McpTransportType = "sse" | "stdio" | "streamable-http";
export type SecretEdit =
  | { mode: "keep" }
  | { mode: "clear" }
  | { mode: "replace"; value: string | Record<string, string> };

export interface McpToolSummary {
  name: string;
  title?: string;
  description?: string;
  schemaSha256?: string;
  schemaStatus?: "current" | "changed" | "unknown";
  inputSchema?: Record<string, unknown>;
}

export interface McpLastTestSummary {
  status: "success" | "failed" | "never";
  testedAt?: string;
  error?: unknown;
}

export interface McpServerSummary {
  id: string;
  revision: number;
  name: string;
  enabled: boolean;
  transportType: McpTransportType;
  url?: string | null;
  command?: string | null;
  args?: string[];
  secrets?: {
    headersConfigured: boolean;
    envConfigured: boolean;
    fingerprint?: string;
  };
  connectionStatus?: "untested" | "connecting" | "connected" | "failed";
  connectionMessage?: string;
  lastTest?: McpLastTestSummary;
  toolCount?: number;
  tools?: McpToolSummary[];
  updatedAt?: string;
}

export interface ReplyTuning {
  pollIntervalSeconds: number;
  sameSenderMergeSeconds: number;
  humanReplyWaitSeconds: number;
  sessionTimeoutSeconds: number;
  maxConcurrency: number;
  mcpTimeoutSeconds: number;
}

export type ReplyDeliveryMode = "review" | "automatic";

export interface ListenerToolGrant {
  serverId: string;
  serverName?: string;
  toolName: string;
  toolTitle?: string;
  schemaSha256: string;
  schemaStatus?: "current" | "changed" | "missing";
}

export interface ReplyListenerSummary {
  id: string;
  revision: number;
  name: string;
  enabled: boolean;
  groupId: string;
  groupName: string;
  systemPrompt: string;
  webhookConfigured: boolean;
  webhookHint?: string;
  webhookVerified: boolean;
  webhookVerifiedAt?: string;
  deliveryMode: ReplyDeliveryMode;
  tools: ListenerToolGrant[];
  tuning: ReplyTuning;
  health?: "stopped" | "starting" | "monitoring" | "degraded" | "error";
  healthMessage?: string;
  lastPollAt?: string;
  pendingCount?: number;
}

export type ReplyWorkStatus = "waiting" | "working" | "pending" | "sent" | "closed" | "failed";

export type ReplyWorkImageStatus = "none" | "resolving" | "ready" | "processed" | "partial" | "unavailable" | "unsupported";

export interface ReplyWorkItem {
  id: string;
  version: number;
  listenerId: string;
  listenerName?: string;
  groupId: string;
  groupName: string;
  senderId?: string;
  senderName: string;
  question: string;
  answer?: string;
  status: ReplyWorkStatus;
  stage?: string;
  reason?: string;
  errorCode?: string;
  errorStage?: string;
  mentionMode?: "userid" | "mobile" | "unresolved";
  createdAt: string;
  updatedAt?: string;
  completedAt?: string;
  detectedAt?: string;
  sourceDelaySeconds?: number;
  mergeDueAt?: string;
  humanWaitDueAt?: string;
  imageCount?: number;
  imageAvailableCount?: number;
  imageUnavailableCount?: number;
  imageStatus?: ReplyWorkImageStatus;
  duplicateCount?: number;
  evidence?: Array<{ serverName?: string; toolName?: string; summary: string }>;
}

export interface ReplyRuntimeSnapshot {
  running?: boolean;
  startedAt?: string;
  activeRetrievals?: number;
  queuedRetrievals?: number;
  listeners?: ReplyListenerSummary[];
  pendingCount?: number;
  recentFailures?: number;
}

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
  issue_fields: IssueFieldDefinition[];
  default_smart_sheet_template_id?: string;
}

export interface PromptConfig {
  default_id: string;
  default_issue_fields?: IssueFieldDefinition[];
  items: PromptItem[];
}

export type IssueFieldType =
  | "text"
  | "long_text"
  | "single_select"
  | "multiple_select"
  | "boolean"
  | "number"
  | "date"
  | "datetime"
  | "url";

export interface IssueFieldDefinition {
  key: string;
  label: string;
  type: IssueFieldType;
  required: boolean;
  instruction: string;
  options: string[];
  default_value: unknown;
}

export interface SmartSheetFieldSchema {
  title?: string;
  type?: string;
  enum?: string[];
  [key: string]: unknown;
}

export interface SmartSheetFieldMapping {
  source_key: string;
  target_field_id: string;
  target_type: string;
  required: boolean;
  default_value: unknown;
}

export interface SmartSheetTemplate {
  id: string;
  name: string;
  url: string;
  webhook_url_env: string;
  webhook_url: string;
  batch_size: number;
  schema: Record<string, SmartSheetFieldSchema>;
  field_mappings: SmartSheetFieldMapping[];
}

export interface SmartSheetConfig {
  default_template_id: string;
  templates: SmartSheetTemplate[];
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
  schedules?: ScheduleDefinition[];
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

export interface TaskGroup {
  id: string;
  name: string;
}

export interface ProcessingOptions {
  promptId: string;
  smartSheetTemplateId: string;
  runOcr: boolean;
  runAnalysis: boolean;
  exportXlsx: boolean;
  exportMarkdown: boolean;
  prepareSmartSheet: boolean;
}

export interface TaskRequest extends ProcessingOptions {
  startDate: string;
  endDate: string;
  /** @deprecated Compatibility field for older sidecars. */
  date?: string;
  startTime: string;
  endTime: string;
  groups: TaskGroup[];
}

export type ScheduleDateMode = "today" | "yesterday" | "fixed";

export interface ScheduleDefinition extends ProcessingOptions {
  id: string;
  name: string;
  enabled: boolean;
  /** Missing on legacy schedules and treated as false. */
  autoSyncSmartSheet?: boolean;
  runAt: string;
  weekdays: number[];
  dateMode: ScheduleDateMode;
  fixedDate: string;
  startTime: string;
  endTime: string;
  groups: TaskGroup[];
}

export interface SmartSheetPreview {
  total?: number;
  pending: number;
  already_synced: number;
  configured?: boolean;
  webhook_configured?: boolean;
  template_id?: string;
  template_name?: string;
  template_url?: string;
  template_revision?: string;
  document_revision?: string;
  mapping_valid?: boolean;
  validation_error?: string;
  image_integrity?: {
    mapped: boolean;
    complete: boolean;
    expected: number;
    available: number;
    missing: number;
    affected_issues: number;
  };
}

export interface SmartSheetRunSyncResult {
  mode: "automatic";
  status: "success" | "failed";
  synced?: number;
  /** Successful sync can still omit images that were not available locally. */
  missingImages?: number;
  warning?: string;
  error?: string;
}

export interface TaskRunResult {
  groupId: string;
  groupName: string;
  /** Per-group outcome for multi-group runs. Missing on legacy results. */
  status?: "success" | "empty" | "failed";
  /** Present when a group was skipped or failed. */
  error?: string;
  dayDir: string;
  outputs: Record<string, string>;
  startDate?: string;
  endDate?: string;
  startTime?: string;
  endTime?: string;
  smartSheetDate?: string;
  smartSheetTemplateId?: string;
  smartSheetTemplateName?: string;
  smartSheetTemplateUrl?: string;
  /** Frozen issue-definition snapshot used by preview and sync. Legacy results may omit it. */
  definitionPath?: string | null;
  /** Present when model analysis ran, including zero for a valid empty result. */
  issueCount?: number;
  smartSheetPreview?: SmartSheetPreview | null;
  smartSheetSync?: SmartSheetRunSyncResult | null;
}

export interface TaskResult {
  runs: TaskRunResult[];
  /** Aggregate outcome for multi-group runs. Missing on legacy results. */
  status?: "success" | "partial" | "empty" | "failed";
  totalCount?: number;
  successCount?: number;
  emptyCount?: number;
  failedCount?: number;
  dayDir?: string;
  outputs?: Record<string, string>;
  definitionPath?: string | null;
  issueCount?: number;
  smartSheetPreview?: SmartSheetPreview | null;
  /** Single-group compatibility mirror of the run-level automatic sync result. */
  smartSheetSync?: SmartSheetRunSyncResult | null;
}

export interface ScheduleEvent {
  scheduleId: string;
  scheduleName: string;
  message: string;
  success?: boolean;
  result?: TaskResult;
  historyPersisted?: boolean;
}

export interface PendingScheduleSync {
  pendingId: string;
  scheduleId: string;
  scheduleName: string;
  createdAt: string;
  result: TaskResult;
}

export type ScheduleExecutionStatus = "success" | "partial" | "empty" | "failed";

export interface ScheduleExecutionHistoryItem {
  executionId: string;
  scheduleId: string;
  scheduleName: string;
  trigger: "manual" | "automatic";
  startedAt: string;
  finishedAt: string;
  success: boolean;
  status: ScheduleExecutionStatus;
  message: string;
  result?: TaskResult | null;
}

export interface ScheduleExecutionHistoryPage {
  items: ScheduleExecutionHistoryItem[];
  page: number;
  pageSize: number;
  total: number;
  totalPages: number;
}

export interface SyncResult {
  total?: number;
  synced?: number;
  skipped?: number;
  image_count?: number;
  [key: string]: unknown;
}
