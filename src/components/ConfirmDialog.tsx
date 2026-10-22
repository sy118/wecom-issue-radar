/** Shared in-app confirmation dialog for destructive actions. */

import { AlertTriangle } from "lucide-react";

import { Overlay } from "./Overlay";
import { Button } from "./ui";

export interface ConfirmRequest {
  title: string;
  message: string;
  confirmLabel: string;
  tone?: "primary" | "danger";
  onConfirm: () => void;
}

export function ConfirmDialog({
  request,
  onClose,
}: {
  request: ConfirmRequest;
  onClose: () => void;
}) {
  return (
    <Overlay
      open
      onClose={onClose}
      size="sm"
      eyebrow="Confirm"
      title={request.title}
      footer={(
        <div className="overlay-footer-actions">
          <Button variant="secondary" data-autofocus onClick={onClose}>取消</Button>
          <Button
            variant={request.tone === "danger" ? "danger" : "primary"}
            onClick={() => {
              onClose();
              request.onConfirm();
            }}
          >
            {request.confirmLabel}
          </Button>
        </div>
      )}
    >
      <div className="overlay-status">
        <span className="overlay-status-icon"><AlertTriangle size={18} /></span>
        <div>
          <strong>请确认</strong>
          <p>{request.message}</p>
        </div>
      </div>
    </Overlay>
  );
}
