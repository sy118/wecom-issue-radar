import { Minus, Moon, Square, Sun, X } from "lucide-react";
import { toast } from "sonner";
import { getDesktop } from "../lib/desktop";
import { toUserErrorMessage } from "../lib/errors";
import type { IpcCommand } from "../../electron/shared/contracts";

export function TitleBar({
  dark,
  onToggleTheme,
}: {
  dark: boolean;
  onToggleTheme: () => void;
}) {
  const performWindowAction = async (command: IpcCommand) => {
    try {
      await getDesktop().invoke(command);
    } catch (error) {
      toast.error("窗口操作未完成", {
        description: toUserErrorMessage(error, "请稍后重试。"),
      });
    }
  };

  return (
    <header className="titlebar">
      <div className="titlebar-product">
        <span className="titlebar-mark">W</span>
        <span>企微问题雷达</span>
      </div>
      <div className="window-actions">
        <button title={dark ? "切换浅色模式" : "切换深色模式"} onClick={onToggleTheme}>
          {dark ? <Sun size={15} /> : <Moon size={15} />}
        </button>
        <button title="最小化" onClick={() => void performWindowAction("window_minimize")}><Minus size={16} /></button>
        <button title="最大化" onClick={() => void performWindowAction("window_toggle_maximize")}><Square size={13} /></button>
        <button className="window-close" title="关闭" onClick={() => void performWindowAction("window_close")}><X size={16} /></button>
      </div>
    </header>
  );
}
