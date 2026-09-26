import { useEffect } from 'react'

interface ConfirmDialogProps {
  title: string
  message: string
  detail?: string
  confirmLabel?: string
  cancelLabel?: string
  busy?: boolean
  busyLabel?: string
  error?: string
  onConfirm: () => void
  onCancel: () => void
}

/**
 * Confirmation dialog sharing the visual language of the Memory panel:
 * same overlay, card, header, button styling and uppercase title treatment.
 */
export function ConfirmDialog({
  title,
  message,
  detail,
  confirmLabel = '确认删除',
  cancelLabel = '取消',
  busy = false,
  busyLabel = '删除中...',
  error,
  onConfirm,
  onCancel
}: ConfirmDialogProps) {
  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !busy) onCancel()
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [busy, onCancel])

  return (
    <div
      className="fixed inset-0 z-[100] bg-black/30 flex items-center justify-center p-6"
      onMouseDown={event => {
        if (event.target === event.currentTarget && !busy) onCancel()
      }}
    >
      <section
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirm-dialog-title"
        aria-describedby="confirm-dialog-message"
        className="w-full max-w-md bg-background border border-border shadow-xl"
      >
        <header className="flex items-center justify-between px-6 py-5 border-b border-border">
          <h2
            id="confirm-dialog-title"
            className="text-sm font-semibold tracking-widest uppercase"
          >
            {title}
          </h2>
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="px-3 py-2 border border-border text-sm hover:bg-muted disabled:opacity-40"
            aria-label="关闭确认弹窗"
          >
            Close
          </button>
        </header>

        <div className="p-6">
          <p id="confirm-dialog-message" className="text-sm">
            {message}
          </p>
          {detail && (
            <p className="mt-3 text-xs text-muted-foreground break-words">{detail}</p>
          )}
          {error && <p className="mt-4 text-sm text-red-600">{error}</p>}
        </div>

        <footer className="flex items-center justify-end gap-2 px-6 py-5 border-t border-border">
          <button
            type="button"
            onClick={onCancel}
            disabled={busy}
            className="px-4 py-2 border border-border text-sm hover:bg-muted disabled:opacity-40 transition-colors"
          >
            {cancelLabel}
          </button>
          <button
            type="button"
            onClick={onConfirm}
            disabled={busy}
            className="px-4 py-2 border border-foreground bg-foreground text-background text-sm disabled:opacity-40 transition-colors"
          >
            {busy ? busyLabel : confirmLabel}
          </button>
        </footer>
      </section>
    </div>
  )
}
