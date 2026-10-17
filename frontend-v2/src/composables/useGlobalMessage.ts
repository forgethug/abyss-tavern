type MessageKind = 'success' | 'error' | 'warning' | 'info'

function show(kind: MessageKind, message: string) {
  const event = new CustomEvent('ai-rpg-message', {
    detail: { kind, message },
  })
  window.dispatchEvent(event)
}

const messageApi = {
  success: (message: string) => show('success', message),
  error: (message: string) => show('error', message),
  warning: (message: string) => show('warning', message),
  info: (message: string) => show('info', message),
}

export function setGlobalMessage() {
  // Compatibility hook retained for migrated components.
}

export function useGlobalMessage() {
  return messageApi
}
