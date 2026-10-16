# Scene Builder source-only shadow candidate

- Proposed loopback port 18080 is **not assigned or authorized** until Tab 3 port check.
- Non-root vps1scene with isolated database, scripts, media, and scratch; original 8080 unchanged.
- No Auto Script, excluded Algrow import, script extraction/processing threads or Google outbox loop.
- GET /health passive; GET /internal/runtime-ready protected by X-Runtime-Probe-Token.
- POST/PUT/PATCH/DELETE blocked in candidate middleware. Do not import queues, publish or migrate DBs.
- Worker consumers, SSO/WS, read-route side effects, dependencies and port binding need further Tab 3 verification.
