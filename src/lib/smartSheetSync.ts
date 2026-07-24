import type { TaskRunResult } from "../types";

export function smartSheetConfigurationBlockers(runs: TaskRunResult[]): string[] {
  const messages = new Set<string>();
  runs.forEach((run) => {
    const preview = run.smartSheetPreview;
    if (preview?.mapping_valid === false) {
      messages.add(preview.validation_error || `${run.groupName} 的腾讯文档字段映射无效`);
    }
    if (preview?.webhook_configured === false || preview?.configured === false) {
      messages.add(`${preview.template_name || run.smartSheetTemplateName || run.groupName} 尚未配置写入 Webhook`);
    }
  });
  return [...messages];
}
