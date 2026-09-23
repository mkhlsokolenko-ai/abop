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
  // глобальный хоткей: выделенный текст из любого приложения (Word/Excel/браузер) → анализ в чате ABOP
  onAnalyze: (cb) => ipcRenderer.on("ape:analyze", (_e, text) => cb(text)),
});
