// Ad-hoc подпись macOS-сборки. Запускается electron-builder'ом после упаковки .app и ДО создания
// dmg/zip, поэтому в образ попадает уже подписанный бандл.
//
// Зачем вообще: на Apple Silicon ядро отказывается исполнять Mach-O без подписи — совсем
// неподписанное приложение не «ругается Gatekeeper'ом», а не запускается с «повреждено».
// Developer ID у нас нет, но ad-hoc подписи (`--sign -`) для запуска достаточно: Gatekeeper
// всё равно спросит про неизвестного разработчика (см. desktop/README.md), а вот «повреждено»
// не будет.
const { execFileSync } = require("node:child_process");
const path = require("node:path");

exports.default = async function adhocSign(context) {
  if (context.electronPlatformName !== "darwin") return;
  const app = path.join(context.appOutDir, `${context.packager.appInfo.productFilename}.app`);
  const sidecar = path.join(app, "Contents", "Resources", "ape-sidecar");

  // Вложенный код подписываем ПЕРВЫМ: codesign запечатывает содержимое бандла, и подпись
  // внутренностей после внешней ломает печать. PyInstaller свои бинари на macOS подписывает сам,
  // так что это подстраховка — отсюда терпимость к отдельным файлам (текстовые файлы с битом
  // исполнения codesign отвергнет, и это нормально). Настоящая проверка — `--verify` ниже.
  execFileSync("/bin/sh", ["-c",
    `find ${JSON.stringify(sidecar)} -type f \( -name '*.dylib' -o -name '*.so' -o -perm -111 \) ` +
    `-print0 | xargs -0 -n1 codesign --force --sign - 2>/dev/null || true`], { stdio: "inherit" });

  execFileSync("codesign", ["--force", "--deep", "--sign", "-", app], { stdio: "inherit" });
  // Без этой проверки «подписали» означало бы «команда не упала»: печать бандла она не смотрит.
  execFileSync("codesign", ["--verify", "--strict", "--verbose=2", app], { stdio: "inherit" });
  console.log("[afterPack] ad-hoc подпись проверена:", app);
};
