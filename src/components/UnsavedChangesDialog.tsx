import { AlertTriangle } from "lucide-react";

import { Overlay } from "./Overlay";
import { Button } from "./ui";

/**
 * In-app replacement for window.confirm() when a page holds unsaved edits.
 * Native confirm blocks the renderer and ignores the app's visual language;
 * this dialog keeps the same promise (stay / leave) with keyboard support,
 * focus trapping and focus restore handled by the shared Overlay primitive.
 */
export function UnsavedChangesDialog({
  open,
  onStay,
  onLeave,
}: {
  open: boolean;
  onStay: () => void;
  onLeave: () => void;
}) {
  return (
    <Overlay
      open={open}
      onClose={onStay}
      size="sm"
      eyebrow="Unsaved changes"
      title="有未保存的修改"
      footer={(
        <div className="overlay-footer-actions">
          <Button variant="secondary" data-autofocus onClick={onStay}>留在本页</Button>
          <Button variant="danger" onClick={onLeave}>仍然离开</Button>
        </div>
      )}
    >
      <div className="overlay-status">
        <span className="overlay-status-icon"><AlertTriangle size={18} aria-hidden="true" /></span>
        <div>
          <strong>离开后，这些修改不会保存</strong>
          <p>可以先留在本页保存，再切换到其他页面。</p>
        </div>
      </div>
    </Overlay>
  );
}
