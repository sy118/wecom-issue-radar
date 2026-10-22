import { useState } from "react";
import { Eye, EyeOff } from "lucide-react";
import { Field, Input, SectionHeader, Switch } from "../../components/ui";
import type { TencentDraft, UploadDraft } from "./types";

function SecretField({
  label,
  hint,
  value,
  placeholder,
  onChange,
}: {
  label: string;
  hint?: string;
  value: string;
  placeholder?: string;
  onChange: (value: string) => void;
}) {
  const [visible, setVisible] = useState(false);
  return (
    <Field label={label} hint={hint}>
      <div className="input-action">
        <Input
          type={visible ? "text" : "password"}
          value={value}
          placeholder={placeholder}
          onChange={(event) => onChange(event.target.value)}
        />
        <button
          type="button"
          className="button button-secondary pub-secret-toggle"
          title={visible ? "隐藏" : "显示"}
          onClick={() => setVisible((current) => !current)}
        >
          {visible ? <EyeOff size={15} /> : <Eye size={15} />}
        </button>
      </div>
    </Field>
  );
}

export function ConnectionTab({
  draft,
  upload,
  onChange,
  onUploadChange,
}: {
  draft: TencentDraft;
  upload: UploadDraft;
  onChange: (patch: Partial<TencentDraft>) => void;
  onUploadChange: (patch: Partial<UploadDraft>) => void;
}) {
  return (
    <div className="pub-tab-panel">
      <section className="pub-section">
        <SectionHeader
          title="腾讯文档连接"
          description="写入前会刷新预览并校验模板 revision；自动同步属于外部写入，必须单独开启。"
        />
        <div className="form-grid two-columns">
          <Field label="模板 ID" hint="旧版管线按模板 ID 匹配；默认由发布目标 ID 推导，一般无需修改">
            <Input
              value={draft.template_id}
              onChange={(event) => onChange({ template_id: event.target.value })}
              placeholder="default"
            />
          </Field>
          <Field label="每批写入条数" hint="范围 1–50，过大容易触发限流">
            <Input
              type="number"
              min={1}
              max={50}
              value={draft.batch_size}
              onChange={(event) => onChange({ batch_size: Number(event.target.value) || 50 })}
            />
          </Field>
          <Field label="文档 URL" className="span-two" hint="智能表格的分享链接，用于确认弹窗展示目标文档">
            <Input
              value={draft.url}
              onChange={(event) => onChange({ url: event.target.value })}
              placeholder="https://docs.qq.com/smartsheet/..."
            />
          </Field>
          <SecretField
            label="写入 Webhook URL"
            hint="接收 records 数组并写入该模板；留空则使用环境变量"
            value={draft.webhook_url}
            placeholder="https://..."
            onChange={(webhook_url) => onChange({ webhook_url })}
          />
          <Field label="Webhook 环境变量" hint="本机环境变量名，优先级低于直接填写的 URL">
            <Input
              value={draft.webhook_url_env}
              onChange={(event) => onChange({ webhook_url_env: event.target.value })}
              placeholder="WECOM_SMARTSHEET_WEBHOOK_URL"
            />
          </Field>
        </div>
        <Switch
          checked={draft.auto_sync}
          onChange={(auto_sync) => onChange({ auto_sync })}
          label="定时任务默认自动同步"
          description="仅作为新任务的默认值；真正写入前仍要预览确认，失败会记录在执行记录里。"
        />
      </section>

      <section className="pub-section">
        <SectionHeader
          title="企微图片上传凭据（全局）"
          description="所有腾讯文档目标共用；只有需要写入图片列时才需要配置。凭据保存在本机配置文件中。"
        />
        <div className="form-grid two-columns">
          <Field label="Corp ID">
            <Input
              value={upload.corpid}
              onChange={(event) => onUploadChange({ corpid: event.target.value })}
              placeholder="ww..."
            />
          </Field>
          <SecretField
            label="Corp Secret"
            value={upload.corpsecret}
            onChange={(corpsecret) => onUploadChange({ corpsecret })}
          />
          <Field label="素材上传接口" hint="企业微信 media/upload 地址">
            <Input
              value={upload.token_endpoint}
              onChange={(event) => onUploadChange({ token_endpoint: event.target.value })}
              placeholder="https://qyapi.weixin.qq.com/cgi-bin/media/upload"
            />
          </Field>
          <Field label="图片写入接口" hint="Smart Sheet 图片上传地址">
            <Input
              value={upload.image_upload_endpoint}
              onChange={(event) => onUploadChange({ image_upload_endpoint: event.target.value })}
              placeholder="https://..."
            />
          </Field>
          <Field label="表单字段名">
            <Input
              value={upload.image_form_field}
              onChange={(event) => onUploadChange({ image_form_field: event.target.value })}
              placeholder="media"
            />
          </Field>
          <Field label="上传传播等待（毫秒）" hint="图片上传后等待腾讯文档可读的缓冲时间">
            <Input
              type="number"
              min={0}
              value={upload.propagation_wait_ms_after_uploads}
              onChange={(event) => onUploadChange({
                propagation_wait_ms_after_uploads: Number(event.target.value) || 0,
              })}
            />
          </Field>
          <Field label="图片间隔（毫秒）">
            <Input
              type="number"
              min={0}
              value={upload.delay_ms_between_image_uploads}
              onChange={(event) => onUploadChange({
                delay_ms_between_image_uploads: Number(event.target.value) || 0,
              })}
            />
          </Field>
        </div>
      </section>
    </div>
  );
}
