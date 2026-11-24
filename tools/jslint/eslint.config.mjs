// Линтер JS десктопа. Запуск из корня репо:
//   tools/jslint/node_modules/.bin/eslint -c tools/jslint/eslint.config.mjs desktop/ui desktop/electron
// Сторож — tests/test_js_undef_guard.py: падает на no-undef (имя без определения). no-unused-vars —
// предупреждение: это «объявлено и никем не зовётся», смотрят глазами, в отчёты аудита.
//
// 20–23.11 четыре падения «… is not defined» дошли до прода мимо всех сторожей: сторожа интерфейса
// строковые и проверяют, что в файле есть нужный текст, а не что имена в нём определены.
import globals from "globals";

const rules = { "no-undef": "error", "no-unused-vars": ["warn", { args: "none", caughtErrors: "none" }] };

export default [
  {
    files: ["desktop/ui/**/*.js"],
    languageOptions: { ecmaVersion: 2024, sourceType: "module", globals: { ...globals.browser } },
    rules,
  },
  {
    files: ["desktop/electron/**/*.js"],
    languageOptions: { ecmaVersion: 2024, sourceType: "commonjs", globals: { ...globals.node, ...globals.browser } },
    rules,
  },
  {
    ignores: ["**/vendor/**", "**/fonts/**"],
  },
];
