# MultiAgent v2 Frontend Contract

Read this before changing anything in `webapp/`.

## Stack

- React 18 + TypeScript + Vite. No Tailwind, no CSS-in-JS.
- Data: `@tanstack/react-query` (hooks in `src/app/queries.ts`).
- UI state: `zustand` (`src/app/store.ts`).
- Icons: `lucide-react` (stroke icons, size 14–16).
- Build: `cd webapp && npx tsc -b --noEmit && npx vite build` (outputs to `multiagent/web/dist`).

## Design rules

- Base font 13px, body copy 13px, secondary 12px, metadata 11px. Never below 11px.
- Use tokens from `src/styles/tokens.css` only. No raw hex colors.
- Use existing primitives from `src/ui/primitives.tsx`, `src/ui/overlay.tsx`, `src/ui/toast.tsx`.
- Use `TaskRow`, `StatusBadge`, `RunStatusBadge` from `src/components/`.
- Pages start with `<div className="page">`, a `page-head` with title + subtitle + actions.
- Empty states are explicit and actionable; never leave a blank panel.
- All user/agent text is untrusted: render as text, never `dangerouslySetInnerHTML`.
- Every async action has loading + error feedback via `useToast()`.

## Ownership

Only edit files inside your assigned directory. Do not edit `src/app/*`, `src/ui/*`,
`src/components/*`, `src/styles/tokens.css`, `src/styles/layout.css`, or
`src/api/*` — the lead owns those. Add feature CSS in a local `*.css` file imported
by your component.

## Backend API (existing, base = same origin)

- `GET /api/projects` → `Project[]`
- `GET /api/projects/{pid}/dashboard` → project, tasks, agents, approvals, activity, running
- `GET /api/tasks/{tid}` → `{task, dependencies, history, runs, artifacts, handoff, changes}`
- `GET /api/tasks/{tid}/changes` → `ChangeRow[]`
- `GET /api/tasks/{tid}/diff?path=` → `{path, binary, diff, action}`
- `GET /api/tasks/{tid}/workspace` → working dir / copy info
- `POST /api/tasks/{tid}/run` → start one task
- `POST /api/tasks/{tid}/{op}` where op ∈ retry|takeover|resume|approve|skip|cancel|sync|revert
- `POST /api/projects/{pid}/run|stop|recover`
- `GET /api/projects/{pid}/runs`, `/run-events`, `/stats`, `/artifacts`
- `GET/POST /api/projects/{pid}/board`, `/handoffs`, `/approvals`, `/snapshots`
- `GET /api/agents/detail`, `POST /api/agents/{id}/check`
- `GET/PUT /api/config/files/{name}`

New backend routes are being added in parallel:

- `GET /api/projects/{pid}/git` → git status
- `GET /api/tasks/{tid}/worktree` → worktree/branch info
- `GET /api/projects/{pid}/files?path=` → directory listing
- `GET /api/projects/{pid}/file?path=` → file content
- `POST /api/tasks/{tid}/validate` → run validation checks

If a route is missing, degrade gracefully (catch 404 and show a hint), never crash the page.

## Definition of done

1. `npx tsc -b --noEmit` passes.
2. `npx vite build` passes.
3. Page renders with empty data and with real data (no crash on `undefined`).
4. Keyboard accessible: focus states, Esc closes overlays, buttons are real `<button>`.
5. Report: files changed + what you verified.
