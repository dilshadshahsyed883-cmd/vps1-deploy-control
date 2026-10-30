# Short Studio final navigation release

Owner request: complete the existing Short Maker move and independent Short Uploader using the existing engines, with projects and credentials preserved.

Verified production evidence:
- 41007058481: Short Uploader 8019 health/HTML/workers and authenticated HTTPS passed; Short Maker service and seven gateway routes passed. The legacy Scene partial overlay failed to start, and Scene rolled back.
- PR #435 repairs that overlay by staging song_uploader_alias.py and preflighting the pinned httpx dependency.
- 41007792889: scoped LUM release succeeded with all five engine idle checks, independent Short Uploader health/HTML and 25 SQLite backups. The installed host helper returns early for LUM-only updates, so this success does not prove shared navigation deployment.

After protected CI, merge this release intent with [vps1-short-studio-release] to execute the existing four-app serialized Tailscale workflow against current main. Do not use the preserved-first-install marker: Short Uploader is installed and normal live idle admission now applies.

Require Short Uploader, Short Maker, Video Merger and final Scene release success; authenticated website navigation/HTML and anonymous protections; preserve queues, projects, OAuth, original engines and Auto Script. No YouTube publishing. Keep issue #418 open until live evidence passes.
