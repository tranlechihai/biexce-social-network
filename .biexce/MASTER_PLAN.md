# Ting Ting MVP — Master Implementation Plan

> **SUPERSEDED (2026-08-20):** This was the original 6-task MVP plan — kept for
> history, no longer the planning authority. Current scope, status, and
> roadmap live in `PROJECT_STATUS_AND_ROADMAP.md`; delivery status is tracked
> in `.opencode/HANDOFF.md`.

## 1. Authority, outcome, and planning assumptions

- **Scope authority:** `TING_TING_MVP_SCOPE.md` (all 139 lines reviewed). `.biexce/PROJECT_BRIEF.md` supplies confirmed implementation decisions without expanding that scope.
- **Starting point:** greenfield workspace containing only `README.md`, the scope, and the project brief; there is no application, configuration, dependency manifest, or test command yet.
- **Target outcome:** a local/staging-only FastAPI social backend and responsive browser demo covering mandatory scope sections 4–6, with seed data, operator documentation, automated tests, and a browser smoke checklist.
- **Delivery model:** exactly six tasks, executed serially with **WIP=1**. Gate 1 and BX Review precede implementation. Integration review and a passing regression suite precede the Gate 2 request.
- **Command status:** every command in this plan is a **planned deliverable and unexecuted at planning time**. Task `t-001` must establish the dependency/test entry points before later tasks use them.

## 2. Grounded implementation choices

These choices implement the confirmed brief while keeping the acceptance project small.

1. **Single deployable modular monolith:** Python 3, FastAPI, server-rendered Jinja2, and vanilla HTML/CSS/JavaScript. Modules are separated by responsibility (configuration/database, auth/profile, social graph, posts/feed, interactions, web) rather than deployed separately.
2. **Persistence:** synchronous SQLAlchemy 2-style sessions over SQLite. Foreign keys are enabled. Schema initialization is create-only: it may create a new database/missing MVP tables but must never drop, truncate, or silently replace an existing database. Before writes, it validates that the configured target is SQLite and that any existing Ting Ting schema is compatible; otherwise it fails with an actionable message and leaves data unchanged. A production migration platform is not required.
3. **Dependencies and entry points:** one clear Python dependency manifest (likely `pyproject.toml`) includes runtime and test dependencies. The README will document virtual-environment setup and canonical setup, seed, server, and test commands.
4. **Authentication:** passwords use an established adaptive password-hashing library. A short-lived signed bearer token contains only stable identity and expiry claims. API clients may use `Authorization: Bearer`; the web uses the same token in an HttpOnly, SameSite cookie. The cookie `Secure` setting is configuration-controlled so staging can require HTTPS while documented local HTTP remains usable. Logout clears the cookie. Signing secrets come from environment/configuration and are neither committed nor logged.
5. **Identity/profile:** registration requires unique normalized username and email; login accepts either plus password. Profile is basic MVP data (username/email plus a small editable display/bio field set chosen by implementation), and only its owner can update it. Passwords/tokens never appear in response models or logs.
6. **Authorization as a shared policy boundary:** block and current friendship checks are reusable server-side rules. Block is symmetric for visibility/interaction even if represented as a directed action. It removes requests/friendship between the pair, and unblock restores no relationship.
7. **Social graph:** one pending request is allowed per unordered user pair. Accept creates one bidirectional relationship; reject closes the request. Self-request, duplicate pending request, and already-friends transitions are rejected consistently.
8. **Posts/feed:** text-only posts have `ONLY_ME` or `FRIENDS`. Read, edit/delete, feed inclusion, like, and comment paths re-evaluate current friendship/block state. Feed uses deterministic newest-first ordering (timestamp plus ID tie-breaker) and bounded `limit`/`offset` pagination applied after authorization filtering.
9. **Interactions:** one like per user/post enforced by a database uniqueness constraint; like and unlike return success when replayed. Comments are text-only. A comment may be deleted by its author or the post author. Counts are derived/updated transactionally and returned consistently with stored state.
10. **API conventions:** REST endpoints live under `/api`; successful create/delete responses use conventional status codes. Errors have one JSON shape, planned as `{"error":{"code":"...","message":"...","details":...}}`, with stable handling for validation, unauthenticated, forbidden, conflict, and not-found cases. Exact route names may be refined conflict-free, but the public behaviors in the stories may not be omitted.
11. **Web boundary:** `/web` (or an equivalently clear browser route group) supplies registration/login/logout, feed/post form, profile/user discovery and social actions, block/unblock, likes, and comments. It consumes the same service/authorization rules as the API and visibly reports validation and server errors.
12. **Seed failure safety:** a transactional seed command creates at least three users, friendship, posts of both audiences, likes, and comments only in a fresh, empty, compatible SQLite target. Before writing, seed preflight refuses an incompatible or pre-populated target with an actionable error and no mutation. If any insert/domain transition fails after preflight, the whole seed transaction rolls back so no partial seed remains. A demo password is supplied at invocation/configuration rather than embedded as a source secret.

## 3. Public capability inventory

The implementation may choose equivalent REST naming, but must expose and document these capabilities:

- **Auth/profile:** register, login, logout, get current profile, update current profile, and retrieve enough non-sensitive user/profile data to drive social actions.
- **Social graph:** create request; list relevant requests; accept/reject pending request; list friends; unfriend; block; unblock; and expose current relationship state needed by the web demo.
- **Posts/feed:** create text post; read visible post; edit/delete owned post; retrieve paginated visible feed in deterministic newest-first order.
- **Interactions:** idempotent like/unlike; create comment; list comments only with post visibility; delete comment under author/post-owner policy; return consistent like/comment state and counts.
- **Web:** every user outcome in scope section 2 is operable in a browser without manually calling the API.

## 4. Task graph, ownership, and serial schedule

```text
t-001 Foundation + auth/profile (bx-code)
  -> t-002 Social graph + block policy (bx-code)
    -> t-003 Posts, audience, and feed (bx-code)
      -> t-004 Likes and comments (bx-code)
        -> t-005 Responsive web + seed + README (bx-code)
          -> t-006 Independent test hardening + smoke evidence (bx-test)
```

| Order | Task | Subsystem owner | Principal deliverable | Effort |
|---:|---|---|---|---|
| 1 | `t-001` | bx-code / foundation-auth | runnable skeleton, API conventions, auth/profile, initial tests | L |
| 2 | `t-002` | bx-code / social graph | requests, friendship, block/unblock and policy tests | L |
| 3 | `t-003` | bx-code / content | post CRUD, visibility-safe paginated feed and tests | L |
| 4 | `t-004` | bx-code / interactions | idempotent likes, comments, counters and tests | M |
| 5 | `t-005` | bx-code / web-acceptance | responsive browser flows, seed command, README, smoke checklist draft | L |
| 6 | `t-006` | bx-test / quality | coverage audit, missing black-box tests, final smoke checklist and regression evidence | M |

**WIP rule:** start a task only after its dependency has met its acceptance criteria and its changes are available in the same workspace. Do not overlap writers. Ordinary task review is a checkpoint, not an extra task.

## 5. Requirement-to-task coverage

| Scope criterion | Primary task(s) | Verification boundary |
|---|---|---|
| `TT-AUTH` — registration/login/logout, unique username/email, password hashing, signed auth | `t-001`, `t-005` | auth integration tests; browser smoke |
| `TT-PROFILE` — read/basic update, owner-only mutation | `t-001`, `t-005` | ownership/validation API tests; browser smoke |
| `TT-SOCIAL` — pending/accepted/rejected, no self/duplicates, accept/reject/unfriend | `t-002`, `t-005` | social rule unit tests and API integration tests |
| `TT-BLOCK` — bilateral read/interaction denial, relationship cleanup, no restore | `t-002`–`t-005` | block transition plus cross-feature integration tests |
| `TT-POST` — text CRUD, author-only mutation, two audiences | `t-003`, `t-005` | ownership/audience tests and browser smoke |
| `TT-FEED` — current visibility, stable newest-first, pagination after filtering | `t-003` | feed integration tests with friendship/block transitions and tie ordering |
| `TT-LIKE` — authorized, replay-safe like/unlike and consistent count | `t-004`, `t-005` | API replay/authorization/persistence tests; browser smoke |
| `TT-COMMENT` — authorized create/list; owner or post owner delete; counts | `t-004`, `t-005` | role matrix, missing-resource, persistence tests; browser smoke |
| `TT-WEB` — all mandatory flows, clear errors, mobile/desktop usability | `t-005`, `t-006` | automated web-route tests plus manual browser checklist |
| `TT-SEED-DOC` — 3+ users and representative graph/content/interactions; create-only initialization; guarded transactional seed; setup/run/test/demo docs | `t-001`, `t-005`, `t-006` | fresh-database success, incompatible/pre-populated refusal, rollback assertions, and documented non-mutating error guidance |
| `TT-API-QUALITY` — consistent errors/status, validation/authz, retry safety, secret/log safety, modularity | `t-001`–`t-006` | shared error contract tests, negative tests, source/config review |
| `TT-TEST` — unit, integration, anonymous/forbidden/not-found, regression, no weakening, smoke checklist | every task; audit in `t-006` | Pytest suites and completed pre-Gate-2 checks |

## 6. Criterion-to-verification approach

### Planned canonical commands — all currently unexecuted

Task `t-001` establishes these or documents a clearly equivalent command; later stories use that established form:

```bash
python -m venv .venv
.venv/bin/python -m pip install -e '.[test]'
.venv/bin/python -m pytest
.venv/bin/python -m pytest tests/unit
.venv/bin/python -m pytest tests/integration
.venv/bin/python -m ting_ting.seed
.venv/bin/python -m uvicorn ting_ting.main:app --reload
```

- **Per-story evidence:** focused unit and integration test selectors named in each story. A feature task is not accepted on happy-path evidence alone; applicable validation, anonymous, forbidden, conflict/replay, and missing-resource cases are included.
- **Data-safety evidence:** integration tests use isolated temporary SQLite databases. Tests prove create-only schema initialization creates required structures on a new target, leaves a compatible initialized database unchanged, and rejects an incompatible target without mutation. Seed verification proves a fresh empty database receives the complete representative dataset, incompatible and pre-populated targets are refused before writes, and an injected mid-seed failure rolls back every seed write.
- **Authorization evidence:** tests assert public status/error behavior and persisted state. Cross-feature tests change friendship/block status after content creation to prove read-path authorization is current rather than inferred from an ID or stale feed result.
- **Web evidence:** automated route/form tests verify auth and clear error rendering. The manual smoke checklist records desktop and narrow-mobile viewport results for complete browser flows; manual testing complements rather than replaces API tests.
- **Final evidence:** `t-006` runs the full suite without skips added to evade failures, records command/result summaries, audits all `TT-*` rows, and hands the results to integration review before Gate 2. Because this is a plan, no PASS is claimed here.

## 7. Integration considerations

- SQLite constraints and transaction boundaries must preserve uniqueness under replay (identities, pending pair, friendship pair, likes). API conflict/idempotency behavior must agree with those constraints.
- Social graph/block policy is shared by posts, feed, likes, comments, API, and web. Later tasks extend the policy rather than duplicating weaker checks.
- Authorization responses must not leak blocked or invisible content. The implementation should consistently choose not-found versus forbidden for hidden resources and lock that choice in tests/error documentation.
- Web handlers must call shared application services or public APIs, not implement alternate authorization rules. Cookie and bearer extraction converge on the same current-user dependency.
- Feed pagination is performed over authorized results; tests need enough mixed visible/invisible records to catch under-filled or leaking pages.
- Deleting users/posts is not an MVP workflow. Foreign-key behavior is still explicit for required post/comment deletion so orphaned interactions and incorrect counters do not remain.
- Database startup/initialization is create-only and fail-closed for an incompatible existing schema; it must not use drop-and-recreate as failure handling.
- Seed records must use normal hashing and valid domain transitions where practical. Seed preflight allows only a fresh empty compatible target; incompatible or pre-populated targets fail before writes. One transaction prevents partial seed state on later failure.
- README commands are finalized only after their entry points exist. It documents normal initialization/seed behavior and explains that a refusal or failed seed leaves the target unchanged; guidance is limited to correcting configuration and selecting the intended fresh empty target, with no database lifecycle operations.

## 8. Material risks and controls

| Risk | Material consequence | Planned control / owning task |
|---|---|---|
| Visibility rules diverge across endpoints | blocked/non-friend content leaks | centralized policy in `t-002`; transition tests in `t-003`/`t-004`/`t-006` |
| Pagination before filtering | leaks or unexpectedly sparse/incorrect feed pages | bounded authorized query and mixed-record tests in `t-003` |
| Pair/race/retry duplicates | duplicate requests, friendships, or likes | canonical pair keys/unique constraints plus replay tests in `t-002`/`t-004` |
| Cookie security breaks local acceptance or staging security | browser cannot authenticate or cookie is unsafe | environment-controlled Secure flag, HttpOnly/SameSite, documented local/staging settings in `t-001`/`t-005` |
| Stateless logout misunderstood | previously copied API token remains valid until expiry | short expiry and documented logout semantics; no unrequested revocation service |
| Seed credentials or signing key committed/logged | secret exposure | invocation/env-supplied values and log assertions/review in `t-001`, `t-005`, `t-006` |
| Initialization/seed targets an existing, populated, or incompatible SQLite database | silent data loss, overwrite, or partially seeded state | create-only initialization guard in `t-001`; seed target preflight, refusal/no-mutation tests, transaction rollback, and non-mutating error guidance in `t-005`/`t-006` |
| Web duplicates backend policy | API/web authorization inconsistency | shared services and web integration tests in `t-005` |
| Greenfield commands differ from plan | unverifiable handoff | `t-001` establishes command entry points; `t-005` documents actual commands; `t-006` executes them |

## 9. Explicit exclusions (scope section 7)

No task may implement: **images, video, object storage, or CDN; E2EE, key envelopes, MLS, or device-key lifecycle; chat, calls, realtime, or push notifications; circles, selected audiences, mentions, reply threads, or expiry; public feeds, recommendations, hashtags, trending, or advertising; moderation cases, legal holds, privacy export/deletion orchestration; microservices, event brokers, distributed caches, or search clusters; a native mobile application; production deployment, Kubernetes, high availability, backup/PITR, or compliance certification.**

Extension points or future-work notes are allowed only when they add no implementation. Also excluded are Docker and frontend frameworks, unless Gate 1 explicitly changes the confirmed stack; no such change is planned.

## 10. Gate handoff

- **Gate 1:** BX Review red-teams this plan and the six stories; the user approves before `t-001` begins.
- **Gate 2 readiness:** all six tasks accepted; create-only initialization, fresh seed success, incompatible/pre-populated target refusal, and injected-failure rollback exercised; full unit/integration regression passes; browser smoke checklist completed; integration review reports no material issue. The user remains final acceptance authority.
