import { FileSpreadsheet, Github, MessageSquareText, ShieldCheck, Table2 } from "lucide-react";

export function AboutPage({ version }: { version: string }) {
  return (
    <div className="page-content about-page">
      <div className="about-hero">
        <div className="about-logo">W</div>
        <h1>企微问题雷达</h1>
        <p>从本地企业微信群聊到可跟踪业务问题的一站式桌面工具。</p>
        <span>Version {version} · Tauri + Rust + React</span>
      </div>
      <div className="about-grid">
        <div className="glass-card"><MessageSquareText /><strong>本地提取</strong><p>读取本机企业微信数据库，按群和日期隔离处理。</p></div>
        <div className="glass-card"><FileSpreadsheet /><strong>双格式导出</strong><p>Excel 给业务筛选汇总，Markdown 用于归档和二次 AI 分析。</p></div>
        <div className="glass-card"><Table2 /><strong>可选云同步</strong><p>Smart Sheet 写入前二次确认，并使用本地台账防止重复。</p></div>
        <div className="glass-card"><ShieldCheck /><strong>隐私优先</strong><p>配置和聊天缓存默认只存本机，仓库不包含业务数据。</p></div>
      </div>
      <div className="about-footer"><Github size={16} />sy118 / wecom-issue-radar</div>
    </div>
  );
}
