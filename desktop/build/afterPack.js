// Ad-hoc подпись macOS-сборки. Запускается electron-builder'ом после упаковки .app и ДО создания
// dmg/zip, поэтому в образ попадает уже подписанный бандл.
//
// Зачем вообще: на Apple Silicon ядро отказывается исполнять Mach-O без подписи — совсем
// неподписанное приложение не «ругается Gatekeeper'ом», а не запускается с «повреждено».
// Developer ID у нас нет, но ad-hoc подписи (`--sign -`) для запуска достаточно: Gatekeeper
// всё равно спросит про неизвестного разработчика (см. desktop/README.md), а вот «повреждено»
// не будет.
//
// Обход каталога — на Node, без вызова `sh`: первая версия звала `find` строкой, и экранирование
// скобок, пройдя через шаблонную строку JS, до шелла не доехало («syntax error near unexpected
// token `('»). Там, где нет шелла, нечего и квотировать.
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

function* файлы(каталог) {
  for (const e of fs.readdirSync(каталог, { withFileTypes: true })) {
    const p = path.join(каталог, e.name);
    if (e.isSymbolicLink()) continue;          // симлинки ведут на уже обойдённые файлы
    if (e.isDirectory()) yield* файлы(p);
    else if (e.isFile()) yield p;
  }
}

exports.файлы = файлы;   // экспортирован ради проверки обхода без macOS под рукой

exports.default = async function adhocSign(context) {
  if (context.electronPlatformName !== "darwin") return;
  const app = path.join(context.appOutDir, `${context.packager.appInfo.productFilename}.app`);
  const sidecar = path.join(app, "Contents", "Resources", "ape-sidecar");

  // Вложенный код подписываем ПЕРВЫМ: codesign запечатывает содержимое бандла, и подпись
  // внутренностей после внешней ломает печать. PyInstaller свои бинари на macOS подписывает сам,
  // так что это подстраховка — отсюда терпимость к отдельным файлам: текстовый скрипт с битом
  // исполнения codesign отвергнет, и это нормально. Настоящая проверка — `--verify` ниже.
  let подписано = 0;
  let отвергнуто = 0;
  if (fs.existsSync(sidecar)) {
    for (const f of файлы(sidecar)) {
      const бинарь = /\.(dylib|so)$/.test(f) || (fs.statSync(f).mode & 0o111) !== 0;
      if (!бинарь) continue;
      try {
        execFileSync("codesign", ["--force", "--sign", "-", f], { stdio: "ignore" });
        подписано++;
      } catch (e) {
        отвергнуто++;
      }
    }
  }
  console.log(`[afterPack] сайдкар: подписано ${подписано}, отвергнуто (не Mach-O) ${отвергнуто}`);

  execFileSync("codesign", ["--force", "--deep", "--sign", "-", app], { stdio: "inherit" });
  // Без этой проверки «подписали» означало бы «команда не упала»: печать бандла она не смотрит.
  execFileSync("codesign", ["--verify", "--strict", "--verbose=2", app], { stdio: "inherit" });
  console.log("[afterPack] ad-hoc подпись проверена:", app);
};
