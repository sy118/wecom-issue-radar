import { AlertTriangle, CircleAlert } from "lucide-react";
import type { MappingProblem } from "./types";

export function ProblemList({ problems }: { problems: MappingProblem[] }) {
  if (!problems.length) return null;
  return (
    <div className="pub-problem-list">
      {problems.map((problem) => (
        <div key={problem.key} className={`pub-problem ${problem.level}`}>
          {problem.level === "error" ? <CircleAlert size={14} /> : <AlertTriangle size={14} />}
          <span>{problem.message}</span>
        </div>
      ))}
    </div>
  );
}
