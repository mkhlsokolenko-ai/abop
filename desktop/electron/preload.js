// Безопасный мост main ↔ renderer (contextIsolation). Отдаём в UI только апдейтер:
// статус событий electron-updater + действия (проверить / установить-и-перезапустить).
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("ape", {
  updater: {
    onStatus: (cb) => ipcRenderer.on("updater:status", (_e, data) => cb(data)),
    check: () => ipcRenderer.invoke("updater:check"),
    install: () => ipcRenderer.invoke("updater:install"),
  },
  exportPdf: (html, filename) => ipcRenderer.invoke("export:pdf", { html, filename }),
  openFile: (path) => ipcRenderer.invoke("file:open", path),
  revealFile: (path) => ipcRenderer.invoke("file:reveal", path),
  // глобальный хоткей: выделенный текст из любого приложения (Word/Excel/браузер) → анализ в чате ABOP
  onAnalyze: (cb) => ipcRenderer.on("ape:analyze", (_e, text) => cb(text)),
  // Какое сочетание в итоге занято: прежде занятый другим приложением хоткей молчал, и «не
  // работает» нельзя было отличить от «не нажал».
  onHotkey: (cb) => ipcRenderer.on("ape:hotkey", (_e, combo) => cb(combo)),
  hotkey: () => ipcRenderer.invoke("hotkey:active"),
  // Буфер обмена через main: `navigator.clipboard` зависит от разрешений и фокуса и отказывает молча.
  clipboard: {
    writeText: (text) => ipcRenderer.invoke("clip:write", text),
    readText: () => ipcRenderer.invoke("clip:read"),
  },
});
