# Experiment Studio — Proxy Pools, Network Diagnostics, 10 Sessions

Scope: Experiment Studio browser only on VPS_1. No changes to unrelated Scene Builder jobs, SML, Voice, Upload Studio or Auto Script.

## Proxy management
- Saved owner-scoped HTTP(S) and SOCKS5 proxy endpoints, credentials redacted in API responses.
- Pool selection is optional per profile. A managed profile uses the selected endpoint first and tries same-pool backups if Chrome could not reach the public IPv4 check endpoint. It **never silently falls back to a direct connection**.
- Pool health check uses a short-lived Chromium browser to request api4.ipify.org, returning reachability and observed exit IPv4. Failures are not interpreted as verified anonymity. The proxy check contacts a third-party service.
- Existing profile directories and cookies remain in place. First additive schema migration creates a permission-limited SQLite backup. No automatic rotation for running sessions.

## Network diagnostics
- On demand, the selected browser profile checks observed public IPv4 and IPv6 using external providers and enumerates limited WebRTC ICE candidate types. These tests share network metadata with their providers.
- DNS leak test opens an independent diagnostic page for visual inspection. Browser-side JavaScript alone cannot verify resolver provenance. No check claims leak-free anonymity, even when IPv6 or ICE candidates appear unavailable.
- Exit-IP checks are not tests of DNS, TLS, WebRTC or IPv6 privacy as a whole.

## Runtime reliability and admission
- MAX_ACTIVE=10 globally; each profile is a separate Playwright persistent Chrome directory and can only be started once at a time.
- Each new process requires at least 384 MiB MemAvailable on Linux, so the limit is a ceiling, not a guarantee that every VPS can handle 10 running browsers.
- Idle processes are swept in the background; unexpectedly closed sessions get a bounded recovery attempt while their profile directories are preserved.
- Temporary health probes do not alter saved browser profiles; stop/delete remain owner-scoped. Browser runtime, proxy pool tests and live owner acceptance are separate gates.

## Release
Isolated PR CI and guarded Scene-only GitHub → Tailscale release with health verification and rollback. Do not claim current production deployment from passing source tests. OAuth, Auto Script and unrelated applications remain unchanged.
