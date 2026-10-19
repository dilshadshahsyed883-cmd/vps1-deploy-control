# Upload Studio — Unified Navigation V1 (2026-10-20)

Approved: Rename the main website sidebar entry to **Upload Studio** and show exactly two subtabs inside it: **Auto Uploader** and **Auto Stream**.

- Scene and native site sidebars expose one Upload Studio item, highlighted for either independent app.
- A shared native tab bar links to existing Auto Uploader at `/local-upload-manager/app?embedded=1&tab=dashboard` and Auto Stream at `/auto-stream/`; no iframe or backend merging.
- Uploader keeps its original 8 internal pages, Engine 1, parallel Engine 2 and Shorts. Stream keeps its separate `/short-stream/engine` OAuth, Sheets, live API and stream jobs.
- Native app shells use cache-busted shared assets. Existing redundant uploader main subtab bar is suppressed only for the native site shell, without removing uploader functionality.
- Both tabs link to independent app documents. A tab switch can load a new document; shared browser view transitions reduce visual flicker. This is intentionally not a new SPA.
- Existing URLs, bookmarks, queues, channel history, OAuth credentials, scheduler states and data are unchanged. Opening Auto Stream does not start broadcasts. Auto Script stays OFFLINE.

Offline checks: `node --test tests/test_scene_native_sidebar_role.js` and `python3 -m unittest production.test_auto_stream_canonical production.test_scene_video_studio_shell production.test_uploader_native_top_tabs tests.test_voice_sidebar_ui_only_release -v`. Use the existing guarded, rollback-capable GitHub → Tailscale native-studio workflow for coordinated UI deployment. Owner functional testing remains separate.
