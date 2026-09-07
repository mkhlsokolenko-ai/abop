---
name: conventional-commits
description: Use when creating git commits, writing commit messages, or following version control workflows
---

# Conventional Commits

Ты формируешь структурированные commit-сообщения по спецификации Conventional Commits — читаемая история, автоматический changelog, семантическое версионирование. Коммит/пуш — это ДЕЙСТВИЕ, меняющее историю: по умолчанию готовишь сообщение и дифф (dry-run), фактический commit/push — под подтверждением человека.

## Когда применять
- Создаёшь git-коммит или пишешь commit-сообщение.
- Настраиваешь рабочий процесс version control (changelog, semver, релизы).
- Готовишь дифф/сообщение к ревью перед фиксацией.

## Overview

The Conventional Commits specification provides:

- **Automated changelog generation** - Tools can parse commits to generate CHANGELOG.md
- **Semantic versioning** - Commit types map to version bumps (feat → minor, breaking → major)
- **Clear project history** - Standardized format makes git log readable
- **Automated releases** - CI/CD can trigger releases based on commit types

## Quick Reference

| Type | Use For | Version Bump |
| ---- | ------- | ------------ |
| `feat` | New feature | MINOR |
| `fix` | Bug fix | PATCH |
| `docs` | Documentation only | PATCH |
| `style` | Formatting, no logic change | PATCH |
| `refactor` | Code restructuring, no behavior change | PATCH |
| `perf` | Performance improvement | PATCH |
| `test` | Adding or updating tests | PATCH |
| `build` | Build system or dependencies | PATCH |
| `ci` | CI/CD configuration | PATCH |
| `chore` | Maintenance, no user-facing change | PATCH |
| `revert` | Revert a previous commit | PATCH |

## Format

```text
<type>[optional scope]: <description>

[optional body]

[optional footer(s)]
```

**Rules:**

- Type is required
- Scope is optional - use parenthesized section affected: `(api)`, `auth)`, `parser)`
- Description is required
- Keep description concise, imperative mood, lowercase, no trailing period
- Body and footer are optional
- Separate subject from body with blank line
- Wrap body at 72 characters

## Examples

```text
feat: add user authentication

Implement JWT-based authentication with login/logout endpoints.
Includes password hashing and session management.

Closes: #123
```

```text
fix(api): handle null response from server

Previous implementation crashed when server returned null.
Now returns empty result set.

Assisted-by: GLM 4.6 via Claude Code
```

```text
feat(storage)!: change bucket listing API

BREAKING CHANGE: bucket list now returns async iterator
instead of array. Update all callers to use for-await.
```

```text
refactor(core): simplify error handling

Consolidate duplicate error handlers into single utility.
No behavior changes - internal cleanup only.
```

```text
revert: feat(auth): add OAuth support

This reverts commit 8b5a1c2. OAuth provider changed
their API and we need to redesign integration.
```

## Breaking Changes

Indicate breaking changes in two ways:

**Option 1**: Add `!` after type/scope

```text
feat(api)!: remove deprecated endpoint
```

**Option 2**: Add `BREAKING CHANGE:` footer

```text
feat(api): remove deprecated endpoint

BREAKING CHANGE: endpoint no longer exists. Use newEndpoint instead.
```

## AI Attribution

AI agents must disclose their assistance in the commit footer:

```text
Assisted-by: [Model Name] via [Tool Name]
```

Examples:

- `Assisted-by: GLM 4.6 via Claude Code`
- `Assisted-by: Claude Opus 4.5 via claude.ai`

## Common Mistakes

| Mistake | Why Wrong | Correct |
| ------- | --------- | ------- |
| `Added login feature` | Past tense, capitalized | `feat: add login feature` |
| `fix bug.` | Trailing period | `fix: resolve login error` |
| `update` | Missing type | `chore: update dependencies` |
| `feature:add-auth` | Missing space after colon | `feat: add authentication` |
| `FEAT: big change` | Uppercase type | `feat: add authentication` |
| Multi-line description no blank line | No separation | Add blank line after subject |

## Body Guidelines

- **What**: Motivation for the change (vs. code comments describe HOW)
- **Contrast**: Explain the WHY and WHAT, not code details
- **Wrap at 72 characters** for readability in git log

```text
feat(summarization): add support for nested bullet points

Previous implementation only flattened all content. Now preserves
hierarchy by respecting indentation levels. Users can now create
structured summaries with parent-child relationships.

Closes: #456
```

## Footer Guidelines

Use footers for:

- **Breaking changes**: `BREAKING CHANGE: detailed explanation`
- **Issue references**: `Closes: #123`, `Fixes: #456`, `Refs: #789`
- **AI attribution**: `Assisted-by: Model via Tool`

Multiple footers separated by blank lines:

```text
feat: add batch upload

Implements multipart upload for large files.

BREAKING CHANGE: upload() signature changed - now requires options object
Closes: #123
Assisted-by: GLM 4.6 via Claude Code
```

## Git Commit Flags

**Required flag:** Always use `--signoff` when committing:

```bash
git commit --signoff -m "feat: add user authentication"
```

The `--signoff` flag adds a `Signed-off-by` trailer to the commit message, indicating the committer has certified the commit follows developer certificate of origin (DCO).

## Testing Your Commit

Before committing, verify:

1. [ ] Type is from allowed list
2. [ ] Description is imperative mood (add, fix, update)
3. [ ] Description is lowercase
4. [ ] No trailing period on description
5. [ ] Breaking changes marked with `!` or footer
6. [ ] AI attribution included (if applicable)
7. [ ] Body explains WHY not HOW
8. [ ] Using `--signoff` flag

## Definition of Done
- [ ] Тип из разрешённого списка; описание в императиве, lowercase, без точки в конце.
- [ ] Breaking changes отмечены `!` или футером `BREAKING CHANGE:`.
- [ ] Тело объясняет ПОЧЕМУ и ЧТО, не КАК; перенос по 72 символам.
- [ ] Добавлена AI-атрибуция в футере (если применимо) и флаг `--signoff`.
- [ ] Дифф просмотрен: в него **не попали секреты**, `.env`, ключи, ПДн, веса моделей, корпус данных.
- [ ] Сообщение и дифф подготовлены как dry-run; фактический `commit`/`push` выполняется **только после явного подтверждения человека (HITL)**.

## Анти-паттерны
- Выполнить `commit`/`push` без подтверждения человека — необратимое изменение истории/удалённого репозитория.
- Закоммитить секреты/`.env`/ключи/ПДн/веса/корпус — утечка (см. `.gitignore`).
- Прошедшее время и заглавная буква в описании (`Added login feature`).
- Точка в конце описания; отсутствие типа; заглавный тип (`FEAT:`).
- Отсутствие пустой строки между subject и body.
- Пропуск отметки breaking change при ломающем изменении контракта.
- Force-push и переписывание чужой истории без явного запроса.

## Безопасность
- `mode=action`, `egress=external`, `cite=False`.
- Git-коммит и особенно **push — это действие с egress наружу** (удалённый репозиторий).
- **HITL-гейт:** по умолчанию работаешь в dry-run — готовишь сообщение и дифф; фактический `git commit`/`git push` — только под явным подтверждением человека.
- **Не пушить секреты:** перед фиксацией проверь дифф на `.env`, ключи/токены, ПДн, веса моделей, корпус документов — они не должны попасть в историю.
- Не использовать `--no-verify`/обход хуков и подписей без явного запроса владельца.

## Интеграция
- `data_query`: на входе возьми фактический дифф/статус рабочего дерева — сообщение строится по реальным изменениям, не по догадке.
- `remember(criticality)`: пометь критичность (пуш в общий/защищённый бранч, breaking change — высокая критичность) для гейта конверта.
- `handoff`: при передаче на ревью/подтверждение отдай подготовленное сообщение и дифф человеку (HITL); слияние/релиз — соответствующей роли.
