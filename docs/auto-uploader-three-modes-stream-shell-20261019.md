# Auto Uploader three modes + separate Auto Stream subtab (ISS-09)
Approved 19 Oct 2026. Existing Scene single login, LUM queues/Sheets/OAuth/history and standalone streaming app are preserved.

* Main LUM UI now has two primary subtabs. **Auto Uploader** keeps its existing eight workspace tabs and exactly three uploader mode choices: Engine 1, Engine 2, Short Uploader.
* **Auto Stream** is a separate navigation tab with a non-operative description and a link to the existing /short-stream/ application. It does not execute streaming APIs, update queues, or start FFmpeg; detailed stream logic remains undecided.
* Engine 2 continues as an isolated, parallel uploader, **not a backup**. A valid thumbnail is required at add-video admission, profile/Sheets matching, and upload preflight.
* Short Uploader shares the uploading workflow but does not require a thumbnail; both manual admission and channel/Sheets queueing remain thumbnail-free. No thumbnail stage/download/Drive thumbnail move occurs for new Shorts channel jobs.
* Mode isolation, no duplicate publication, and authorization remain deployment safeguards; nothing auto-enables either previously disarmed publisher. Existing source media or databases are not migrated, rewritten or deleted.
* Run `python -m unittest production.test_uploader_three_mode_split production.test_uploader_four_modes production.test_uploader_heavy_only`, broader uploader CI, then guarded single-application GitHub/Tailscale deployment with health and rollback. Owner functional UAT remains separate.
