import { getCurrentWindow } from "@tauri-apps/api/window";
import { Minus, Moon, Square, Sun, X } from "lucide-react";

export function TitleBar({
  dark,
  onToggleTheme,
}: {
  dark: boolean;
  onToggleTheme: () => void;
}) {
  const appWindow = getCurrentWindow();
  return (
    <header className="titlebar" data-tauri-drag-region>
      <div className="titlebar-product" data-tauri-drag-region>
        <span className="titlebar-mark">W</span>
        <span data-tauri-drag-region>企微问题雷达</span>
      </div>
      <div className="window-actions">
        <button title={dark ? "切换浅色模式" : "切换深色模式"} onClick={onToggleTheme}>
          {dark ? <Sun size={15} /> : <Moon size={15} />}
        </button>
        <button title="最小化" onClick={() => void appWindow.minimize()}><Minus size={16} /></button>
        <button title="最大化" onClick={() => void appWindow.toggleMaximize()}><Square size={13} /></button>
        <button className="window-close" title="关闭" onClick={() => void appWindow.close()}><X size={16} /></button>
      </div>
    </header>
  );
}
