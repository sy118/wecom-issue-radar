import { ListTodo } from "lucide-react";

import { DailyPageHeader } from "../../components/daily/ui";
import { TodosPanel } from "../../components/daily/TodosPanel";

export function DailyTodosPage() {
  return (
    <div className="page-content daily-page">
      <DailyPageHeader
        eyebrow="Daily Todos"
        icon={<ListTodo size={13} />}
        title="待办"
        description="从聊天里的承诺和请求自动整理待办，完成后勾掉，不需要的跳过。"
      />
      <section className="glass-card">
        <TodosPanel />
      </section>
    </div>
  );
}
