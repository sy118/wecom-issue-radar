import { useEffect } from "react";

const dirtySources = new Map<string, boolean>();

export function setSourceDirty(source: string, dirty: boolean): void {
  if (dirty) dirtySources.set(source, true);
  else dirtySources.delete(source);
}

export function hasUnsavedChanges(): boolean {
  return dirtySources.size > 0;
}

/** Returns true when it is safe to leave; asks the user otherwise. */
export function confirmLeave(): boolean {
  if (!hasUnsavedChanges()) return true;
  return window.confirm("有未保存的修改，离开后这些修改会丢失。仍要离开吗？");
}

/** Marks one editor as dirty while it is mounted and has pending changes. */
export function useUnsavedChanges(source: string, dirty: boolean): void {
  useEffect(() => {
    setSourceDirty(source, dirty);
    return () => setSourceDirty(source, false);
  }, [source, dirty]);
}
