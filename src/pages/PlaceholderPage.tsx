import { ArrowLeft, Sparkles } from "lucide-react";
import type { PageId } from "../types";
import { Button } from "../components/ui";

export function PlaceholderPage({
  title,
  description,
  milestone,
  bullets,
  onBack,
  onNavigate,
}: {
  title: string;
  description: string;
  milestone: string;
  bullets: string[];
  onBack: () => void;
  onNavigate?: (page: PageId) => void;
}) {
  return (
    <div className="coming-page">
      <span className="coming-badge">
        <Sparkles size={14} /> {milestone} 上线
      </span>
      <h1>{title}</h1>
      <p>{description}</p>
      <ul className="coming-list">
        {bullets.map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
      <div className="workbench-card-actions">
        <Button variant="secondary" onClick={onBack}>
          <ArrowLeft size={15} /> 回工作台
        </Button>
        {onNavigate && (
          <Button variant="ghost" onClick={() => onNavigate("settings")}>
            先去配置数据源
          </Button>
        )}
      </div>
    </div>
  );
}
