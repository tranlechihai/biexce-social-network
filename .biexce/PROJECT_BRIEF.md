# Ting Ting MVP — Project Brief

## Authority and objective

`TING_TING_MVP_SCOPE.md` is the sole scope authority for this acceptance project.
Build a local/staging-only Ting Ting social backend and responsive browser demo so a user can complete authentication, profile, friendship, blocking, post/feed, like, and comment flows without calling the API manually.

## Confirmed implementation decisions

- Use Python 3, FastAPI, SQLAlchemy, SQLite, Jinja2, vanilla HTML/CSS/JavaScript, and Pytest, as preferred by the scope.
- Use signed bearer tokens for REST API authentication and store the token in a secure HTTP-only cookie for the web demo; API clients may also send `Authorization: Bearer <token>`.
- Registration requires both a username and email; each is unique. Login accepts either username or email plus password.
- An accepted friend request creates a bidirectional friendship. The feed contains only posts currently visible to the viewer; pagination applies after visibility filtering. Unfriend removes mutual visibility. Block removes any friendship/request relationship and prevents either party from reading or interacting with the other party's content; unblock does not restore it.

## Scope and exclusions

Implement only the mandatory sections 4–6 of `TING_TING_MVP_SCOPE.md`, including seed data, documentation, API, responsive web demo, unit/integration tests, and browser smoke checklist. Do not implement any item listed in section 7 (media, E2EE, chat/realtime, circles/advanced audiences, public/recommendation features, moderation/legal workflows, distributed architecture, native mobile, or production deployment).

## Delivery gates

Create a 5–7 task implementation plan, red-team it with BX Review, and obtain Gate 1 approval before source implementation. After implementation, run regression/integration tests and an integration review before requesting Gate 2 acceptance.
