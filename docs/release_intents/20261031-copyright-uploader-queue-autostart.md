# Copyright Uploader queue auto-start — isolated production activation

Source fix merged in PR #536. Queue admission re-arms the existing serial
worker. Copyright Uploader keeps no Auto Engine.

The commit is a release marker only; no user jobs, media, database,
uploads, channel records or credentials may be edited by this document.

Guard: [vps1-copyright-studio-release] [vps1-copyright-uploader-ui].
Use the independently isolated Copyright Uploader installer, idle check,
Tailscale and rollback. Preserve Short, Song and Video Merger. Auto Script OFFLINE.
