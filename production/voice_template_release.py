"""VPS1 Voice Studio sidebar-only release: immutable HTML overlay, no worker restarts.

The protected-main production installer dispatches here only for the explicit
[vps1-voice-template-only] marker. A locked, authenticated GitHub/Tailscale
operation owns the deployment. All Voice API, engine code, OAuth, queue storage,
worker services and systemd worker states remain unchanged.
"""
from __future__ import annotations

import hashlib
import http.client
import json
import socket
import ssl
from pathlib import Path
import shutil
import subprocess
import time
import urllib.request

from production.runtime import BASE, REPO, atomic_json, atomic_link, event, http_check, run

APP = "voice-studio"
CURRENT = BASE / "current/voice-studio"
RELEASES = BASE / "releases/voice-studio"
UNIT = "vps1-production-voice-studio.service"
PAGE = Path("part01/templates/voice_studio.html")
JOURNALS = BASE / "transactions/voice-sidebar-only"
TITLE = b"<title>Voice Studio \xc2\xb7 VPS1</title>"
OLD_ASSETS = (
    b'<link rel="stylesheet" href="/&#115;tatic/vps1_native_site_shell.css?v=1">\n'
    b'<script src="/&#115;tatic/vps1_native_site_shell.js?v=1" data-workspace="voice" defer></script>'
)
NEW_ASSETS = (
    b'<link rel="stylesheet" href="/&#115;tatic/vps1_native_site_shell.css?v=20261020-upload-studio-v1">\n'
    b'<script src="/&#115;tatic/vps1_native_site_shell.js?v=20261020-upload-studio-v1" data-workspace="voice" defer></script>'
)
PREVIOUS_ASSETS = (
    b'<link rel="stylesheet" href="/&#115;tatic/vps1_native_site_shell.css?v=20261019-voice-nav-v2">\n'
    b'<script src="/&#115;tatic/vps1_native_site_shell.js?v=20261019-voice-nav-v2" data-workspace="voice" defer></script>'
)
ASSET_NOTE = b"<!-- VPS1 Voice native sidebar assets loaded before large inline CSS. -->"
OLD_LIMITS = b"const MAX_TEXT=,MAX_BATCH=;"
OLD_TEMPLATE_LIMITS = b"const MAX_TEXT={{ max_text }},MAX_BATCH={{ max_batch }};"
NEW_LIMITS = b"const MAX_TEXT=1000000,MAX_BATCH=500;"


def require(ok: bool, why: str) -> None:
    if not ok:
        raise RuntimeError("VOICE_UI_RELEASE_BLOCKED: " + why)


def patch_template(payload: bytes) -> bytes:
    """Only relocate two trusted asset links; preserve all other Voice markup."""
    require(payload.count(TITLE) == 1, "existing Voice template title changed")
    # The current production release may already carry the latest asset pair.
    # Treat that state as idempotent and preserve the template byte-for-byte.
    if (payload.count(NEW_ASSETS) == 1 and
            payload.count(OLD_ASSETS) == 0 and
            payload.count(PREVIOUS_ASSETS) == 0):
        require(payload.count(ASSET_NOTE) == 1 and
                b'id="vsEngineOmni"' in payload and
                b'id="vsEngineKokoro"' in payload,
                "current Voice sidebar assets or engine controls invalid")
        return payload
    # The last approved Voice sidebar-only release already moved assets above
    # the large inline stylesheet. Upgrade that exact version in place without
    # touching engine controls, worker services or the rest of the HTML.
    if payload.count(PREVIOUS_ASSETS) == 1 and payload.count(ASSET_NOTE) == 1:
        require(payload.count(OLD_ASSETS) == 0 and payload.count(NEW_ASSETS) == 0,
                "mixed Voice sidebar assets")
        updated = payload.replace(PREVIOUS_ASSETS, NEW_ASSETS, 1)
        require(updated.count(NEW_ASSETS) == 1 and
                b'id="vsEngineOmni"' in updated and
                b'id="vsEngineKokoro"' in updated,
                "Voice sidebar upgrade failed")
        return updated
    require(payload.count(OLD_ASSETS) == 1, "expected old native CSS/JS links not found")
    require(payload.count(b'vps1_native_site_shell.js?v=20261020-upload-studio-v1') == 0,
            "Voice template already patched")
    updated = payload.replace(OLD_ASSETS, ASSET_NOTE, 1)
    updated = updated.replace(TITLE, TITLE + b"\n" + NEW_ASSETS, 1)
    require(updated.count(NEW_ASSETS) == 1 and updated.count(OLD_ASSETS) == 0,
            "Voice template asset relocation failed")
    require(b'id="vsEngineOmni"' in updated and b'id="vsEngineKokoro"' in updated,
            "Voice engine UI controls unexpectedly missing")
    return updated


RESPONSIVE_ID = b'<style id="voice-responsive-20261020">'


def responsive_overlay(original: bytes, source: bytes) -> bytes:
    """Copy only the approved style block; retain runtime-specific HTML and JS."""
    require(source.count(RESPONSIVE_ID) == 1, "responsive style missing or duplicated")
    start = source.index(RESPONSIVE_ID)
    end = source.index(b"</style>", start) + len(b"</style>")
    block = source[start:end]
    require(b"<script" not in block and b"url(" not in block,
            "responsive overlay must contain local CSS only")
    require(original.count(RESPONSIVE_ID) <= 1, "duplicate installed responsive style")
    if RESPONSIVE_ID in original:
        old_start = original.index(RESPONSIVE_ID)
        old_end = original.index(b"</style>", old_start) + len(b"</style>")
        return original[:old_start] + block + original[old_end:]
    require(original.count(b"</head>") == 1, "Voice document head invalid")
    return original.replace(b"</head>", block + b"\n</head>", 1)



# The public native document is under /voice-studio/app, while the isolated
# web service itself is under /app. Nginx text substitution must not decide
# where the queue API points. Update only four reviewed inline JS expressions.
QUEUE_ROUTE_PAIRS = (
    (b"const apiRoot='/tts/api/kokoro'",
     b"const apiRoot=(location.pathname.startsWith('/voice-studio/')?'/voice-studio':'')+'/'+'tts/api/kokoro'"),
    (b"const $=id=>document.getElementById(id),root='/tts/api/edge'",
     b"const $=id=>document.getElementById(id),root=(location.pathname.startsWith('/voice-studio/')?'/voice-studio':'')+'/'+'tts/api/edge'"),
    (b"link.href=((window.location.pathname.startsWith('/voice-studio') && typeof j.audio_url==='string' && j.audio_url.startsWith('/'+'tts/api/kokoro/audio/')) ? '/voice-studio/service'+j.audio_url : j.audio_url);",
     b"link.href=apiRoot+'/audio/'+encodeURIComponent(j.job_id);"),
    (b"a.href=j[ext+'_url'];",
     b"a.href=root+'/files/'+encodeURIComponent(j.job_id)+'/'+ext;"),
)


QUEUE_FETCH_PAIRS = (
    (b"async function request(url,opt){let resp=await fetch(url,{credentials:'same-origin',...(opt||{})});let ct=resp.headers.get('content-type')||'';let json=ct.includes('json')?await resp.json():{};if(!resp.ok)throw Error(json.detail||json.error||('HTTP '+resp.status));return json;}",
     b"async function request(url,opt){let resp=await fetch(url,{credentials:'same-origin',cache:'no-store',...(opt||{})});let ct=resp.headers.get('content-type')||'';let json=ct.includes('json')?await resp.json():{};if(!resp.ok)throw Error('HTTP '+resp.status+' '+new URL(url,location.href).pathname+' : '+(json.detail||json.error||'API error'));return json;}"),
    (b"async function req(url,opt){const r=await fetch(url,{credentials:'same-origin',...(opt||{})});let data={};try{data=await r.json();}catch(_){}if(!r.ok)throw Error(data.detail||data.error||('HTTP '+r.status));return data;}",
     b"async function req(url,opt){const r=await fetch(url,{credentials:'same-origin',cache:'no-store',...(opt||{})});let data={};try{data=await r.json();}catch(_){}if(!r.ok)throw Error('HTTP '+r.status+' '+new URL(url,location.href).pathname+' : '+(data.detail||data.error||'API error'));return data;}"),
    (b"const r=await request(apiRoot+'/jobs');",
     b"const r=await request(apiRoot+'/'+'jobs?limit=150&v='+Date.now());"),
    (b"const r=await req(root+'/jobs');",
     b"const r=await req(root+'/'+'jobs?limit=150&v='+Date.now());"),
    (b"await req(root+'/jobs/'+jid+'/'+act,{method:'POST'});",
     b"await req(root+'/'+'jobs/'+jid+'/'+act,{method:'POST'});"),
)

def queue_route_overlay(original: bytes, source: bytes) -> bytes:
    """Copy only reviewed engine URL expressions; reject unknown runtime drift."""
    for old, corrected in QUEUE_ROUTE_PAIRS + QUEUE_FETCH_PAIRS:
        require(source.count(corrected) == 1 and old not in source,
                "GitHub Voice source queue path contract changed")
        require(original.count(old) + original.count(corrected) == 1,
                "installed Voice queue JS differs from reviewed versions")
        if old in original:
            original = original.replace(old, corrected, 1)
    return original


# AUDIO_DOWNLOADER_BROWSER_ROUTE_CONTRACT_V2: only these six reviewed inline JS
# substitutions are permitted. Keep the existing Voice runtime, style and workers.
AUDIO_BROWSER_ROUTE_PAIRS = (
    (b"const root = '/tts/api/audio-downloader';",
     b"// AUDIO_DOWNLOADER_BROWSER_ROUTE_CONTRACT_V2: preserve the authenticated Voice Studio Nginx prefix.\nconst root = (location.pathname.startsWith('/voice-studio/')?'/voice-studio':'')+'/tts/api/audio-downloader';"),
    (b"const res=await fetch(url,{credentials:'same-origin',headers:",
     b"const res=await fetch(url,{credentials:'same-origin',cache:'no-store',headers:"),
    (b"  const data=await res.json().catch(()=>({detail:'Server did not return JSON'}));\n  if(!res.ok)throw new Error(typeof data.detail==='string'?data.detail:`HTTP ${res.status}`);\n  return data;",
     b"  const type=String(res.headers.get('content-type')||'').toLowerCase();\n  const data=type.includes('application/json')?await res.json().catch(()=>null):null;\n  const pathname=new URL(url,location.href).pathname;\n  if(!res.ok)throw new Error('HTTP '+res.status+' '+pathname+': '+(data&&typeof data.detail==='string'?data.detail:'API request failed'));\n  if(!data||typeof data!=='object')throw new Error('API returned a non-JSON response for '+pathname+'; check session and proxy routing');\n  return data;"),
    (b"const response=await fetch(endpoint,{method:'GET',credentials:'same-origin'",
     b"const response=await fetch(root+endpoint.slice('/tts/api/audio-downloader'.length),{method:'GET',credentials:'same-origin'"),
    (b"async function loadJobs(){try{const r=await api(root+'/jobs?limit=150');const jobs=r.jobs||[];for(const [id,num] of [['vsaRecent',5],['vsaQueueRows',200],['vsaHistoryRows',200]]){$(id).replaceChildren(jobTable(jobs,num));}}catch(e){$('vsaRecent').textContent='Queue unavailable: '+e.message;$('vsaQueueRows').textContent='Queue unavailable: '+e.message;$('vsaHistoryRows').textContent='History unavailable: '+e.message;}}",
     b"async function loadJobs(){try{const r=await api(root+'/jobs?limit=200');if(!Array.isArray(r.jobs))throw new Error('Invalid queue API response: jobs list missing');const jobs=r.jobs;const terminal=new Set(['completed','delivered','failed','cancelled']);const historyJobs=jobs.filter(j=>terminal.has(j.status));$('vsaRecent').replaceChildren(jobTable(jobs,5));$('vsaQueueRows').replaceChildren(jobTable(jobs,200));$('vsaHistoryRows').replaceChildren(jobTable(historyJobs,200));}catch(e){$('vsaRecent').textContent='Queue unavailable: '+e.message;$('vsaQueueRows').textContent='Queue unavailable: '+e.message;$('vsaHistoryRows').textContent='History unavailable: '+e.message;}}"),
    (b"const $=id=>document.getElementById(id),prefix='/tts/api/audio-downloader/proxy';",
     b"const $=id=>document.getElementById(id),prefix=(location.pathname.startsWith('/voice-studio/')?'/voice-studio':'')+'/tts/api/audio-downloader/proxy';"),
)


def audio_browser_route_overlay(original: bytes, source: bytes) -> bytes:
    for old, corrected in AUDIO_BROWSER_ROUTE_PAIRS:
        require(source.count(corrected) == 1 and old not in source,
                "GitHub Audio Downloader browser route contract changed")
        require(original.count(old) + original.count(corrected) == 1,
                "installed Audio Downloader JavaScript differs from reviewed versions")
        if old in original:
            original = original.replace(old, corrected, 1)
    return original


VOICE_WEB = Path("part01/vps1_omnivoice_app.py")
WEB_CACHE_OLD = b"def studio(request:Request): return templates.TemplateResponse(request, \"voice_studio.html\", {})"
WEB_CACHE_NEW = b"def studio(request:Request):\n    response = templates.TemplateResponse(request, \"voice_studio.html\", {})\n    response.headers[\"Cache-Control\"] = \"private, no-store, max-age=0, must-revalidate\"\n    response.headers[\"Pragma\"] = \"no-cache\"\n    response.headers[\"X-Voice-Queue-UI\"] = \"VSQ-20261020-01\"\n    return response"


def voice_web_cache_overlay(original: bytes, source: bytes) -> bytes:
    """Patch only template response headers in the existing Voice web entry."""
    require(source.count(WEB_CACHE_NEW) == 1 and WEB_CACHE_OLD not in source,
            "new web no-cache contract absent from reviewed source")
    require(original.count(WEB_CACHE_OLD) + original.count(WEB_CACHE_NEW) == 1,
            "installed Voice web entrypoint differs from approved versions")
    return original.replace(WEB_CACHE_OLD, WEB_CACHE_NEW, 1)


def read_full_authenticated_voice_html(cookie: str) -> bytes:
    """Nginx HTML may exceed the legacy 180 kB shared HTTPS probe cap.

    Keep the existing shared getter unchanged for other apps and use a
    bounded full read only for Voice's late inline Kokoro/Edge queue scripts.
    TLS SNI/CA validation and Scene cookie remain mandatory.
    """
    from production.route_uploader_scene_https import DOMAIN
    with socket.create_connection(("127.0.0.1", 443), timeout=8) as raw:
        with ssl.create_default_context().wrap_socket(raw, server_hostname=DOMAIN) as tls:
            conn = http.client.HTTPSConnection(DOMAIN, timeout=12)
            conn.sock = tls
            conn.request("GET", "/voice-studio/app", headers={
                "Host": DOMAIN,
                "Cookie": cookie,
                "Accept-Encoding": "identity",
                "Connection": "close",
            })
            response = conn.getresponse()
            data = response.read(750_000)
            require("no-store" in response.getheader("Cache-Control", "").lower() and
                    response.getheader("X-Voice-Queue-UI", "") == "VSQ-20261020-01",
                    "public Voice document can be cached or is an outdated revision")
            require(response.status == 200 and
                    "html" in response.getheader("Content-Type", "").lower() and
                    b'id="voiceStudioEdgeEngineV1Script"' in data and
                    b'id="voiceStudioDualEngineV1Script"' in data,
                    "full authenticated Voice document missing late queue scripts")
            return data


def verify_runtime() -> None:
    """Read the actual web document locally, then verify authenticated HTTPS."""
    http_check(APP)
    address = "http://127.0.0.1:8143/app"
    with urllib.request.urlopen(address, timeout=15) as response:
        body = response.read(500000)
        require("no-store" in response.headers.get("Cache-Control", "").lower() and
                response.headers.get("X-Voice-Queue-UI", "") == "VSQ-20261020-01",
                "local Voice document cache/revision headers absent")
        require(response.status == 200 and "html" in response.headers.get("Content-Type", "").lower(),
                "Voice web UI HTTP response unavailable")
        require(RESPONSIVE_ID in body and NEW_ASSETS in body and NEW_LIMITS in body and
                OLD_LIMITS not in body and b'id="vsEngineOmni"' in body,
                "Voice web UI template guard is not live")
    require(all(new in body and old not in body for old, new in QUEUE_ROUTE_PAIRS + QUEUE_FETCH_PAIRS),
            "local Voice queue JavaScript is not updated")
    require(all(new in body and old not in body for old, new in AUDIO_BROWSER_ROUTE_PAIRS),
            "local Audio Downloader browser URLs not updated")
    from production.route_uploader_scene_https import https_get, scene_cookie
    cookie = scene_cookie()
    code, kind, html = https_get("/voice-studio/app", cookie)
    require(code == 200 and "html" in kind.lower()
            and b"vps1_native_site_shell.js?v=20261020-upload-studio-v1" in html
            and RESPONSIVE_ID in html and NEW_LIMITS in html and OLD_LIMITS not in html,
            "authenticated public Voice page template guard not updated")
    full_html = read_full_authenticated_voice_html(cookie)
    require(all(new in full_html and old not in full_html for old, new in QUEUE_ROUTE_PAIRS + QUEUE_FETCH_PAIRS),
            "public Voice queue JavaScript is not updated")
    require(all(new in full_html and old not in full_html for old, new in AUDIO_BROWSER_ROUTE_PAIRS),
            "public Audio Downloader URL contract not updated")
    anonymous, _, _ = https_get("/voice-studio/app")
    require(anonymous in {401, 403, 303}, "anonymous Voice page became accessible")


def install(revision: str) -> None:
    require(len(revision) == 40 and all(c in "0123456789abcdef" for c in revision),
            "invalid approved immutable SHA")
    require(CURRENT.is_symlink(), "current Voice release not an approved symlink")
    previous = CURRENT.resolve(strict=True)
    require(previous.is_relative_to(RELEASES) and previous.is_dir(),
            "current Voice pointer not owned by production releases")
    require((previous / PAGE).is_file() and not (previous / PAGE).is_symlink(),
            "previous Voice HTML missing or linked")
    active = subprocess.run(["systemctl", "is-active", "--quiet", UNIT],
                            check=False, capture_output=True).returncode == 0
    require(active, "current Voice web service unavailable")
    http_check(APP)

    source = (REPO / "apps/voice-studio" / PAGE)
    require(source.is_file() and not source.is_symlink(),
            "trusted Voice template source missing")
    source_html = source.read_bytes()
    require(NEW_ASSETS in source_html and source_html.count(NEW_LIMITS) == 1,
            "approved GitHub Voice template markers missing")
    original_html = (previous / PAGE).read_bytes()
    patched_html = patch_template(original_html)
    # Apply the single approved browser boot guard from the source template.
    # This keeps the release worker-safe: no synthesis service or data store is touched.
    if original_html.count(OLD_LIMITS) == 1:
        patched_html = patched_html.replace(OLD_LIMITS, NEW_LIMITS, 1)
    elif original_html.count(OLD_TEMPLATE_LIMITS) == 1:
        patched_html = patched_html.replace(OLD_TEMPLATE_LIMITS, NEW_LIMITS, 1)
    elif original_html.count(NEW_LIMITS) == 1:
        pass
    else:
        require(False, "unexpected Voice template limit guard state")
    require(patched_html.count(NEW_LIMITS) == 1 and
            OLD_LIMITS not in patched_html and
            OLD_TEMPLATE_LIMITS not in patched_html,
            "Voice template limit guard overlay failed")

    patched_html = responsive_overlay(patched_html, source_html)
    patched_html = queue_route_overlay(patched_html, source_html)
    patched_html = audio_browser_route_overlay(patched_html, source_html)
    source_entry = REPO / "apps/voice-studio" / VOICE_WEB
    active_entry = previous / VOICE_WEB
    require(source_entry.is_file() and not source_entry.is_symlink()
            and active_entry.is_file() and not active_entry.is_symlink(),
            "Voice app web entrypoint must be a trusted regular file")
    patched_entry = voice_web_cache_overlay(active_entry.read_bytes(),
                                            source_entry.read_bytes())


    target = RELEASES / (revision + "-sidebar-only")
    if previous == target:
        verify_runtime()
        event(app=APP, result="PASS_VOICE_UI_ALREADY_LIVE", workers="PRESERVED")
        return
    require(not target.exists() and not target.is_symlink(),
            "immutable Voice UI target already exists")
    journal = JOURNALS / revision
    require(not journal.exists(), "Voice UI release journal already exists")
    journal.mkdir(parents=True, mode=0o700)
    atomic_json(journal / "previous.json", {
        "old_release": str(previous),
        "old_template_sha256": hashlib.sha256(original_html).hexdigest(),
        "new_template_sha256": hashlib.sha256(patched_html).hexdigest(),
        "new_web_cache_sha256": hashlib.sha256(patched_entry).hexdigest(),
        "app": APP, "revision": revision, "workers": "unchanged",
    })
    shutil.copytree(previous, target, symlinks=True)
    (target / PAGE).write_bytes(patched_html)
    (target / VOICE_WEB).write_bytes(patched_entry)
    manifest_file = target / "_production_manifest.json"
    require(manifest_file.is_file() and not manifest_file.is_symlink(),
            "copied immutable release manifest missing")
    manifest = json.loads(manifest_file.read_text())
    require(manifest.get("app") == APP and str(PAGE) in manifest.get("files", {}),
            "copied Voice release manifest invalid")
    manifest["revision"] = revision
    manifest["files"][str(PAGE)] = hashlib.sha256(patched_html).hexdigest()
    require(str(VOICE_WEB) in manifest["files"],
            "Voice web entrypoint absent from approved release manifest")
    manifest["files"][str(VOICE_WEB)] = hashlib.sha256(patched_entry).hexdigest()
    manifest["ui_only_overlay"] = str(previous)
    atomic_json(manifest_file, manifest, 0o644)
    switched = False
    try:
        atomic_link(CURRENT, target)
        switched = True
        # Only the browser-facing Voice web process reloads Jinja templates.
        # Never stop/restart voice workers or touch job databases here.
        run(["systemctl", "restart", UNIT], timeout=60, quiet=True)
        verify_runtime()
        atomic_json(journal / "result.json", {
            "revision": revision, "state": "ACTIVE", "workers": "PRESERVED",
            "ui": "PASS", "public_https": "PASS",
        })
        event(app=APP, result="PASS_VOICE_TEMPLATE_ONLY",
              workers="PRESERVED", data="UNCHANGED", rollback=str(journal))
    except BaseException:
        if switched:
            atomic_link(CURRENT, previous)
            run(["systemctl", "restart", UNIT], timeout=60, quiet=True)
            http_check(APP)
            event(app=APP, rollback="PASS", workers="UNCHANGED", data="PRESERVED")
        raise
