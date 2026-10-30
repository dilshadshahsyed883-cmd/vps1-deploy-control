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
# The approved Song Studio shell was upgraded on 29 October. Keep the older
# approved asset signature for backwards compatibility with live releases.
SONG_STUDIO_ASSETS = NEW_ASSETS.replace(b"20261020-upload-studio-v1", b"20261029-song-studio-v1")
COPYRIGHT_STUDIO_ASSETS = SONG_STUDIO_ASSETS.replace(
    b'vps1_native_site_shell.js?v=20261029-song-studio-v1"',
    b'vps1_native_site_shell.js?v=20261029-song-studio-v1&copyright-nav=20261030-v3"',
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
    # Admit exactly one approved pair; reject mixed versions or duplicate links.
    asset_pairs = (OLD_ASSETS, NEW_ASSETS, PREVIOUS_ASSETS,
                   SONG_STUDIO_ASSETS, COPYRIGHT_STUDIO_ASSETS)
    require(sum(payload.count(pair) for pair in asset_pairs) == 1,
            "unexpected duplicated or mixed Voice shell assets")
    if payload.count(COPYRIGHT_STUDIO_ASSETS) == 1:
        require(payload.count(b'id="vsEngineOmni"') == 1 and
                payload.count(b'id="vsEngineKokoro"') == 1,
                "Copyright Studio shell controls invalid")
        return payload
    # The current Song Studio Voice HTML can use the approved newer shared shell.
    if (payload.count(SONG_STUDIO_ASSETS) == 1 and
            payload.count(NEW_ASSETS) == 0 and payload.count(OLD_ASSETS) == 0 and
            payload.count(PREVIOUS_ASSETS) == 0):
        require(payload.count(b'id="vsEngineOmni"') == 1 and
                payload.count(b'id="vsEngineKokoro"') == 1,
                "Song Studio shell controls invalid")
        return payload
    # The current production release may already carry the latest asset pair.
    # Treat that state as idempotent and preserve the template byte-for-byte.
    if (payload.count(NEW_ASSETS) == 1 and
            payload.count(OLD_ASSETS) == 0 and
            payload.count(PREVIOUS_ASSETS) == 0):
        # Runtime may have received the canonical assets through a different
        # approved Voice feature overlay, which does not preserve ASSET_NOTE.
        # The marker is a comment, not a security boundary; require exact
        # canonical assets plus unique engine controls and reject duplicates.
        require(payload.count(ASSET_NOTE) <= 1 and
                payload.count(b'id="vsEngineOmni"') == 1 and
                payload.count(b'id="vsEngineKokoro"') == 1,
                "current Voice sidebar assets or engine controls invalid")
        return payload
    require(payload.count(SONG_STUDIO_ASSETS) == 0,
            "unexpected duplicated or mixed Song Studio shell assets")
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



# Allowlisted OmniVoice UI-only parity overlay. Existing workers, other
# engine templates, static assets and on-disk voice data are preserved.
OMNI_PARITY_ID = b'<style id="omni-kokoro-parity-ui">'
OMNI_PARITY_MARKUP = (
    (b'<label class="label">Target language</label><select id="omniLanguage">',
     b'<label class="label" for="omniLanguage">Target language</label><select id="omniLanguage">'),
    (b'<label class="label">Voice profile</label><select id="omniProfileSelect">',
     b'<label class="label" for="omniProfileSelect">Voice profile</label><select id="omniProfileSelect">'),
    (b'<label class="label">Speed</label><div class="range"><input id="omniSpeed"',
     b'<label class="label" for="omniSpeed">Speed</label><div class="range"><input id="omniSpeed"'),
    (b'<label class="label">Sampling steps</label><input id="omniNumStep"',
     b'<label class="label" for="omniNumStep">Sampling steps</label><input id="omniNumStep"'),
    (b'<output id="omniSpeedOut">', b'<output id="omniSpeedOut" for="omniSpeed">'),
    (b'<div class="notice" id="omniQueueStatus" style=',
     b'<div class="notice" id="omniQueueStatus" role="status" aria-live="polite" style='),
)


def omnivoice_parity_overlay(original: bytes, source: bytes) -> bytes:
    """Transfer only the owner-reviewed Omni styles/labels into live HTML."""
    require(source.count(OMNI_PARITY_ID) == 1, "Omni parity source CSS missing")
    start = source.index(OMNI_PARITY_ID)
    end = source.index(b"</style>", start) + len(b"</style>")
    block = source[start:end]
    require(b"<script" not in block and b"url(" not in block and
            b"@import" not in block, "Omni parity CSS has external/script content")
    require(original.count(OMNI_PARITY_ID) <= 1,
            "Omni parity style duplicated in installed template")
    if OMNI_PARITY_ID in original:
        old_start = original.index(OMNI_PARITY_ID)
        old_end = original.index(b"</style>", old_start) + len(b"</style>")
        original = original[:old_start] + block + original[old_end:]
    else:
        require(original.count(b"</head>") == 1,
                "Omni parity target document head missing")
        original = original.replace(b"</head>", block + b"\n</head>", 1)
    for old, new in OMNI_PARITY_MARKUP:
        require(source.count(new) == 1 and old not in source,
                "Omni parity source control contract differs")
        require(original.count(old) + original.count(new) == 1,
                "Omni parity installed control contract differs")
        original = original.replace(old, new, 1)
    return original




# OMNI_KOKORO_HEADER_LAYOUT_20261021: move only the already-working Omni
# title/status and existing five action tabs into a Kokoro-style header card.
# Keep scripts, engine state, queue data and all other app routes unchanged.
OMNI_HEADER_STYLE_ID = b'<style id="omni-kokoro-header-layout-20261021">'
OMNI_HEADER_START = b'<div id="vsOmniHeader" class="vs-omni-header">'
OMNI_HEADER_END = b'<!-- VS_OMNI_HEADER_END -->'
OMNI_PAGE_SECTION = b'<section class="page" id="page-omnivoice">\n'
OMNI_HEADING_PLACEHOLDER = b'  <!-- OmniVoice heading and tabs rendered in shared header -->'
OMNI_LEGACY_HEADING = ('  <div class="pagehead"><div><div class="eyebrow">'
                       'SERVERLESS · MULTILINGUAL TTS</div><h1>OmniVoice GPU</h1>'
                       '<div class="subtitle">Dedicated OmniVoice voice-clone '
                       'production for Hindi, Marathi, Gujarati, Bengali and Arabic.'
                       '</div></div><div class="statusbar"><span class="badge gray">'
                       '5 languages</span><span class="badge amber" '
                       'id="omniWorkerBadge">Checking worker…</span></div></div>').encode()


def _omni_header(payload: bytes) -> bytes:
    require(payload.count(OMNI_HEADER_START) == 1 and
            payload.count(OMNI_HEADER_END) == 1,
            "Omni header start/end missing or repeated")
    start = payload.index(OMNI_HEADER_START)
    end = payload.index(OMNI_HEADER_END) + len(OMNI_HEADER_END)
    return payload[start:end]


def verify_omni_kokoro_header(payload: bytes) -> None:
    require(payload.count(OMNI_HEADER_STYLE_ID) == 1 and
            payload.count(OMNI_HEADING_PLACEHOLDER) == 1 and
            payload.count(b'id="omniWorkerBadge"') == 1,
            "LIVE_OMNI_HEADER_MISSING: CSS, worker status or page placeholder")
    header = _omni_header(payload)
    require(payload.index(OMNI_HEADER_START) < payload.index(OMNI_PAGE_SECTION)
            and header.count(b'<h1>OmniVoice</h1>') == 1
            and header.count(b'<nav class="enginebar"') == 1
            and header.count(b'data-vps1-tab=') == 5
            and b'<h1>Kokoro TTS</h1>' not in header
            and payload.count(b'<nav class="enginebar"') == 1
            and (OMNI_PAGE_SECTION + OMNI_HEADING_PLACEHOLDER) in payload,
            "LIVE_OMNI_HEADER_MISSING: heading is not above five existing tabs")


def omni_kokoro_header_overlay(original: bytes, source: bytes) -> bytes:
    """Allowlist exactly one scoped style + structural relocation; idempotent."""
    verify_omni_kokoro_header(source)
    start = source.index(OMNI_HEADER_STYLE_ID)
    finish = source.index(b'</style>', start) + len(b'</style>')
    css = source[start:finish]
    require(len(css) < 5500 and b'<script' not in css and
            b'url(' not in css and b'@import' not in css and
            b'#vsOmniHeader .enginebar' in css and
            b'body.vs-kokoro-active #vsOmniHeader' in css and
            b'body.vs-edge-active #vsOmniHeader' in css and
            b'body.vs-audio-active #vsOmniHeader' in css,
            "Omni header CSS outside approved engine scope")
    new_header = _omni_header(source)
    start_nav = new_header.index(b'<nav class="enginebar"')
    end_nav = new_header.index(b'</nav>', start_nav) + len(b'</nav>')
    nav = new_header[start_nav:end_nav]
    require(source.count(nav) == 1 and
            source.count(OMNI_LEGACY_HEADING) == 0 and
            source.count(OMNI_PAGE_SECTION + OMNI_HEADING_PLACEHOLDER) == 1,
            "GitHub Omni header has unexpected source markup")
    if OMNI_HEADER_START in original or OMNI_HEADER_END in original:
        active = _omni_header(original)
        require(active == new_header,
                "installed Omni header differs from reviewed markup")
    else:
        require(original.count(nav) == 1 and
                original.count(OMNI_PAGE_SECTION + OMNI_LEGACY_HEADING) == 1 and
                original.count(OMNI_HEADING_PLACEHOLDER) == 0,
                "installed Omni header not an approved previous layout")
        original = original.replace(nav, new_header, 1)
        original = original.replace(OMNI_PAGE_SECTION + OMNI_LEGACY_HEADING,
                                    OMNI_PAGE_SECTION + OMNI_HEADING_PLACEHOLDER, 1)
    require(original.count(OMNI_HEADER_STYLE_ID) <= 1,
            "duplicate installed Omni header styles")
    if OMNI_HEADER_STYLE_ID in original:
        start = original.index(OMNI_HEADER_STYLE_ID)
        require(original.find(b'</style>', start) >= 0,
                "incomplete installed Omni header CSS")
        finish = original.index(b'</style>', start) + len(b'</style>')
        original = original[:start] + css + original[finish:]
    else:
        require(original.count(b'</head>') == 1, "installed Voice head changed")
        original = original.replace(b'</head>', css + b'\n</head>', 1)
    verify_omni_kokoro_header(original)
    return original


# The public native document is under /voice-studio/app, while the isolated
# web service itself is under /app. Nginx text substitution must not decide
# where the queue API points. Update only three currently used inline JS expressions.
QUEUE_ROUTE_PAIRS = (
    (b"const apiRoot='/tts/api/kokoro'",
     b"const apiRoot=(location.pathname.startsWith('/voice-studio/')?'/voice-studio':'')+'/'+'tts/api/kokoro'"),
    (b"const $=id=>document.getElementById(id),root='/tts/api/edge'",
     b"const $=id=>document.getElementById(id),root=(location.pathname.startsWith('/voice-studio/')?'/voice-studio':'')+'/'+'tts/api/edge'"),
    (b"link.href=((window.location.pathname.startsWith('/voice-studio') && typeof j.audio_url==='string' && j.audio_url.startsWith('/'+'tts/api/kokoro/audio/')) ? '/voice-studio/service'+j.audio_url : j.audio_url);",
     b"link.href=apiRoot+'/audio/'+encodeURIComponent(j.job_id);"),
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



# VS-UI-20261021-LIVE: the previous Voice template installer copied an old
# immutable HTML template without these source UI changes. Apply only this
# allowlisted browser presentation delta; preserve queues and worker code.
VOICE_UI_PARITY_ID = b'<style id="voiceStudioUiParity20261021">'
VOICE_UI_MARKUP_PAIRS = (
    (b" <div id=\"vseQueue\" class=\"vse-card\" hidden><h3>Edge TTS independent queue</h3><button type=\"button\" id=\"vseRefresh\" class=\"vse-btn alt\">Refresh</button><p class=\"vse-note\">Queued, processing, paused, retry and delivered status. Pause/resume/cancel applies to each Edge job only.</p><table><thead><tr><th>File</th><th>Job status</th><th>Files</th><th>Controls</th></tr></thead><tbody id=\"vseQueueRows\"><tr><td colspan=\"4\">No Edge jobs yet.</td></tr></tbody></table></div>",
     b" <div id=\"vseQueue\" class=\"vse-card\" hidden><div class=\"vsq-heading\"><div><h3>Edge TTS independent queue</h3><p class=\"vse-note\">Queued, processing, paused, retry and delivered status. Pause/resume/cancel applies to each Edge job only.</p></div><button type=\"button\" id=\"vseRefresh\" class=\"vse-btn alt\">Refresh</button></div><div class=\"vsq-tablewrap\"><table><thead><tr><th scope=\"col\">File</th><th scope=\"col\">Job status</th><th scope=\"col\">Audio</th><th scope=\"col\">Controls</th></tr></thead><tbody id=\"vseQueueRows\"><tr><td colspan=\"4\">No Edge jobs yet.</td></tr></tbody></table></div></div>"),  # Edge
    (b" <div id=\"vskQueue\" class=\"vsk-card\" hidden><h3>Kokoro queue</h3><button id=\"vskRefreshQueue\" type=\"button\" class=\"vsk-action\">Refresh</button><p class=\"vsk-note\">Jobs shown here are independent of OmniVoice.</p><table><thead><tr><th>File / job</th><th>Status</th><th>Audio</th></tr></thead><tbody id=\"vskQueueRows\"><tr><td colspan=\"3\">No Kokoro jobs yet</td></tr></tbody></table></div>",
     b" <div id=\"vskQueue\" class=\"vsk-card\" hidden><div class=\"vsq-heading\"><div><h3>Kokoro queue</h3><p class=\"vsk-note\">Jobs shown here are independent of OmniVoice.</p></div><button id=\"vskRefreshQueue\" type=\"button\" class=\"vsk-action\">Refresh</button></div><div class=\"vsq-tablewrap\"><table><thead><tr><th scope=\"col\">File / job</th><th scope=\"col\">Status</th><th scope=\"col\">Audio</th><th scope=\"col\">Controls</th></tr></thead><tbody id=\"vskQueueRows\"><tr><td colspan=\"4\">No Kokoro jobs yet</td></tr></tbody></table></div></div>"),  # Kokoro
)

# An earlier successful Voice release installed this reviewed four-column Edge
# table with a Files header; accept it as an exact migration predecessor only.
EDGE_QUEUE_PREVIOUS = VOICE_UI_MARKUP_PAIRS[0][1].replace(
    b'<th scope="col">Audio</th>', b'<th scope="col">Files</th>', 1
)

VOICE_UI_JS_PAIRS = (
    (b"btn.type='button';btn.textContent=label;",
     b"btn.type='button';btn.className='vsq-action'+(['cancel','delete'].includes(action)?' vsq-danger':'');btn.textContent=label;"),  # Kokoro button
    (b"c=document.createElement('td');a.textContent=j.filename||j.job_id||j.id||'';",
     b"c=document.createElement('td'),d=document.createElement('td');d.className='vsq-controls';a.textContent=j.filename||j.job_id||j.id||'';"),  # Kokoro controls cell
    (b"c.append(' ',button);",
     b"d.append(button);"),  # Kokoro button destination
    (b"tr.append(a,b,c);rows.append(tr);",
     b"tr.append(a,b,c,d);rows.append(tr);"),  # Kokoro append cell
    (b"if(!jobs.length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=3;td.textContent='No jobs';",
     b"if(!jobs.length){const tr=document.createElement('tr'),td=document.createElement('td');td.colSpan=4;td.textContent='No jobs';"),  # Kokoro empty row
    (b"el.className='vse-btn alt';el.textContent=label;",
     b"el.className='vse-btn alt vsq-action'+(['cancel','delete'].includes(act)?' vsq-danger':'');el.textContent=label;"),  # Edge button
    (b"ctrl=document.createElement('td');name.textContent=j.source_filename||j.job_id;",
     b"ctrl=document.createElement('td');ctrl.className='vsq-controls';name.textContent=j.source_filename||j.job_id;"),  # Edge cell
)


def _voice_ui_queue_line(payload: bytes, name: bytes) -> bytes:
    prefix = b'<div id="' + name + b'" '
    lines = [item for item in payload.splitlines() if prefix in item]
    require(len(lines) == 1 and lines[0].count(prefix) == 1 and len(lines[0]) < 4096,
            "Voice UI queue shell changed: " + name.decode("ascii"))
    return lines[0]


def voice_ui_parity_overlay(original: bytes, source: bytes) -> bytes:
    """Copy reviewed HTML, scoped CSS and renderer-only JS; fail closed on drift."""
    require(source.count(VOICE_UI_PARITY_ID) == 1, "Voice parity CSS absent")
    start = source.index(VOICE_UI_PARITY_ID)
    end = source.index(b"</style>", start) + len(b"</style>")
    css = source[start:end]
    require(len(css) < 14000 and b"<script" not in css and b"url(" not in css
            and b"@import" not in css and b"{#" not in css
            and b"#vskQueue .vsq-controls" in css
            and b"#vseQueue .vsq-controls" in css
            and b"#page-clone .card.pad" in css
            and b"#page-queue .tablewrap" in css,
            "Voice UI styles extend outside approved scope")
    require(original.count(VOICE_UI_PARITY_ID) <= 1, "duplicate Voice parity CSS")
    if VOICE_UI_PARITY_ID in original:
        old_start = original.index(VOICE_UI_PARITY_ID)
        require(original.find(b"</style>", old_start) >= 0,
                "incomplete installed Voice parity CSS")
        old_end = original.index(b"</style>", old_start) + len(b"</style>")
        original = original[:old_start] + css + original[old_end:]
    else:
        # A new approved Omni header style may already be present when
        # backfilling legacy Voice parity. Retain GitHub source CSS order.
        if OMNI_HEADER_STYLE_ID in original:
            require(original.count(OMNI_HEADER_STYLE_ID) == 1,
                    "duplicate Omni header CSS in existing Voice HTML")
            original = original.replace(OMNI_HEADER_STYLE_ID,
                                        css + b"\n" + OMNI_HEADER_STYLE_ID, 1)
        else:
            require(original.count(b"</head>") == 1, "missing Voice HTML head")
            original = original.replace(b"</head>", css + b"\n</head>", 1)

    for name, (legacy, reviewed) in zip((b"vseQueue", b"vskQueue"),
                                         VOICE_UI_MARKUP_PAIRS):
        require(_voice_ui_queue_line(source, name) == reviewed,
                "reviewed Voice queue source markup drifted")
        active = _voice_ui_queue_line(original, name)
        allowed = (legacy, reviewed, EDGE_QUEUE_PREVIOUS) if name == b"vseQueue" else (legacy, reviewed)
        require(active in allowed, "unrecognized live Voice queue markup")
        if active != reviewed:
            original = original.replace(active, reviewed, 1)

    for legacy, reviewed in VOICE_UI_JS_PAIRS:
        require(source.count(reviewed) == 1 and legacy not in source,
                "reviewed source Voice UI renderer drifted")
        require(original.count(legacy) + original.count(reviewed) == 1,
                "live Voice renderer is not an approved version")
        original = original.replace(legacy, reviewed, 1)
    require(all(legacy not in original and reviewed in original
                for legacy, reviewed in VOICE_UI_MARKUP_PAIRS + VOICE_UI_JS_PAIRS),
            "Voice UI overlay incomplete")
    return original



# Edge queue presents only one audio download and the verified Google Drive
# link; original TXT/SRT files, worker delivery and queue DB are untouched.
EDGE_AUDIO_RENDER_OLD = b"for(const ext of ['mp3','wav','txt','srt']){\n const group=document.createElement('div'),receipt=receipts[ext];\n const remoteOK=verified&&receipt&&receipt.id&&receipt.url==='https://drive.google.com/file/d/'+receipt.id+'/view';\n if(j[ext+'_url']){const a=document.createElement('a');a.href=root+'/files/'+encodeURIComponent(j.job_id)+'/'+ext;a.textContent='Download '+ext.toUpperCase();a.rel='noopener';group.append(a);}\n else if(remoteOK){const a=document.createElement('a');a.href='https://drive.google.com/uc?export=download&id='+encodeURIComponent(receipt.id);a.target='_blank';a.rel='noopener noreferrer';a.textContent='Download '+ext.toUpperCase()+' (Drive)';group.append(a);}\n if(remoteOK){const line=document.createElement('div'),link=document.createElement('a');link.href=receipt.url;link.target='_blank';link.rel='noopener noreferrer';link.textContent='Google Drive: Open '+ext.toUpperCase()+' file';line.append(link);group.append(line);}\n if(group.hasChildNodes())files.append(group);\n}\n"
EDGE_AUDIO_RENDER_NEW = b"// Edge Queue: one audio download + one verified Drive link, Kokoro-style.\n// TXT/SRT receipts stay intact in the backend and are not shown in this queue.\nconst audioFormats=['mp3','wav'];\nconst localAudio=audioFormats.find(ext=>!!j[ext+'_url']);\nconst remoteAudio=audioFormats.find(ext=>{const item=receipts[ext];return verified&&item&&item.id&&item.url==='https://drive.google.com/file/d/'+item.id+'/view';});\nif(localAudio||remoteAudio){\n const line=document.createElement('div'),link=document.createElement('a');\n if(localAudio){link.href=root+'/files/'+encodeURIComponent(j.job_id)+'/'+localAudio;link.textContent='Download '+localAudio.toUpperCase();link.rel='noopener';}\n else{const item=receipts[remoteAudio];link.href='https://drive.google.com/uc?export=download&id='+encodeURIComponent(item.id);link.target='_blank';link.rel='noopener noreferrer';link.textContent='Download '+remoteAudio.toUpperCase()+' (Drive)';}\n line.append(link);files.append(line);\n}\nif(remoteAudio){const line=document.createElement('div'),link=document.createElement('a');link.href=receipts[remoteAudio].url;link.target='_blank';link.rel='noopener noreferrer';link.textContent='Google Drive: Open uploaded file';line.append(link);files.append(line);}\n"
EDGE_AUDIO_SCRIPT = b'<script id="voiceStudioEdgeEngineV1Script">'


def verify_edge_audio_markup(page: bytes) -> None:
    """Fail the guarded release unless actual served Edge UI is audio-only."""
    require(page.count(EDGE_AUDIO_SCRIPT) == 1 and
            _voice_ui_queue_line(page, b"vseQueue") == VOICE_UI_MARKUP_PAIRS[0][1],
            "LIVE_EDGE_AUDIO_UI_MISSING: Edge queue table unchanged")
    start = page.index(EDGE_AUDIO_SCRIPT)
    finish = page.find(b"</script>", start)
    require(finish > start, "LIVE_EDGE_AUDIO_UI_MISSING: Edge script absent")
    edge_script = page[start:finish]
    require(EDGE_AUDIO_RENDER_NEW in edge_script and
            EDGE_AUDIO_RENDER_OLD not in edge_script and
            b"const audioFormats=['mp3','wav'];" in edge_script and
            b"link.textContent='Google Drive: Open uploaded file'" in edge_script and
            b"['mp3','wav','txt','srt']" not in edge_script and
            b"j.allowed_actions||[]" in edge_script and
            b"/delivery/retry" in edge_script,
            "LIVE_EDGE_AUDIO_UI_MISSING: audio/Drive renderer still old")


def voice_edge_audio_overlay(original: bytes, source: bytes) -> bytes:
    """Transfer only reviewed Edge output-link UI, keep all APIs intact."""
    require(source.count(EDGE_AUDIO_RENDER_NEW) == 1 and
            EDGE_AUDIO_RENDER_OLD not in source,
            "reviewed Edge Audio-only GitHub source differs")
    require(original.count(EDGE_AUDIO_RENDER_OLD) +
            original.count(EDGE_AUDIO_RENDER_NEW) == 1,
            "unrecognized or duplicated live Edge audio renderer")
    original = original.replace(EDGE_AUDIO_RENDER_OLD, EDGE_AUDIO_RENDER_NEW, 1)
    verify_edge_audio_markup(original)
    return original


# AUDIO_DOWNLOADER_BROWSER_ROUTE_CONTRACT_V2: only these six reviewed inline JS
# substitutions are permitted. Keep the existing Voice runtime, style and workers.
AUDIO_BROWSER_ROUTE_PAIRS = (
    (b"const root = '/tts/api/audio-downloader';",
     b"// AUDIO_DOWNLOADER_BROWSER_ROUTE_CONTRACT_V2: preserve the authenticated Voice Studio Nginx prefix.\nconst root = (location.pathname.startsWith('/voice-studio/')?'/voice-studio':'')+'/'+'tts/api/audio-downloader';"),
    (b"const res=await fetch(url,{credentials:'same-origin',headers:",
     b"const res=await fetch(url,{credentials:'same-origin',cache:'no-store',headers:"),
    (b"  const data=await res.json().catch(()=>({detail:'Server did not return JSON'}));\n  if(!res.ok)throw new Error(typeof data.detail==='string'?data.detail:`HTTP ${res.status}`);\n  return data;",
     b"  const type=String(res.headers.get('content-type')||'').toLowerCase();\n  const data=type.includes('application/json')?await res.json().catch(()=>null):null;\n  const pathname=new URL(url,location.href).pathname;\n  if(!res.ok)throw new Error('HTTP '+res.status+' '+pathname+': '+(data&&typeof data.detail==='string'?data.detail:'API request failed'));\n  if(!data||typeof data!=='object')throw new Error('API returned a non-JSON response for '+pathname+'; check session and proxy routing');\n  return data;"),
    (b"const response=await fetch(endpoint,{method:'GET',credentials:'same-origin'",
     b"const response=await fetch(root+endpoint.slice(('/'+'tts/api/audio-downloader').length),{method:'GET',credentials:'same-origin'"),
    (b"async function loadJobs(){try{const r=await api(root+'/jobs?limit=200');if(!Array.isArray(r.jobs))throw new Error('Invalid queue API response: jobs list missing');$('vsaQueueRows').replaceChildren(jobTable(r.jobs,200));}catch(e){$('vsaQueueRows').textContent='Queue unavailable: '+e.message;}}",
     b"async function loadJobs(){try{const r=await api(root+'/'+'jobs?limit=200');if(!Array.isArray(r.jobs))throw new Error('Invalid queue API response: jobs list missing');$('vsaQueueRows').replaceChildren(jobTable(r.jobs,200));}catch(e){$('vsaQueueRows').textContent='Queue unavailable: '+e.message;}}"),

    (b"const $=id=>document.getElementById(id),prefix='/tts/api/audio-downloader/proxy';",
     b"const $=id=>document.getElementById(id),prefix=(location.pathname.startsWith('/voice-studio/')?'/voice-studio':'')+'/'+'tts/api/audio-downloader/proxy';"),
)


# AUDIO_DOWNLOADER_NGINX_SAFE_PATHS_V3: reject Nginx-sub_filter-vulnerable route fragments.
AUDIO_BROWSER_V3_PATH_PAIRS = (
    (b"api(root+'/jobs?limit=200')",
     b"api(root+'/'+'jobs?limit=200')"),
    (b"api(root+'/jobs',{method:'POST'",
     b"api(root+'/'+'jobs',{method:'POST'"),
    (b"api(root+'/jobs/'+encodeURIComponent(job.job_id)+'/cancel'",
     b"api(root+'/'+'jobs/'+encodeURIComponent(job.job_id)+'/'+'cancel'"),
    (b"api(root+'/jobs/'+encodeURIComponent(job.job_id)+'/retry'",
     b"api(root+'/'+'jobs/'+encodeURIComponent(job.job_id)+'/'+'retry'"),
    (b"api(root+'/status')",
     b"api(root+'/'+'status')"),
    (b"request('/profiles/'+encodeURIComponent(p.id)+'/test'",
     b"request('/'+'profiles/'+encodeURIComponent(p.id)+'/'+'test'"),
    (b"request('/profiles/'+encodeURIComponent(p.id)+'/activate'",
     b"request('/'+'profiles/'+encodeURIComponent(p.id)+'/'+'activate'"),
    (b"request('/profiles/'+encodeURIComponent(p.id),{method:'DELETE'",
     b"request('/'+'profiles/'+encodeURIComponent(p.id),{method:'DELETE'"),
    (b"request('/profiles',{method:'POST'",
     b"request('/'+'profiles',{method:'POST'"),
    (b"request('/enable',{method:'POST'",
     b"request('/'+'enable',{method:'POST'"),
)


def audio_browser_route_overlay(original: bytes, source: bytes) -> bytes:
    for old, corrected in AUDIO_BROWSER_ROUTE_PAIRS + AUDIO_BROWSER_V3_PATH_PAIRS:
        require(source.count(corrected) == 1 and old not in source,
                "GitHub Audio Downloader browser route contract changed")
        require(original.count(old) + original.count(corrected) == 1,
                "installed Audio Downloader JavaScript differs from reviewed versions")
        if old in original:
            original = original.replace(old, corrected, 1)
    return original


# SONG_AUDIO_DESKTOP_DOWNLOAD_V1: isolated, allowlisted authenticated page link.
SONG_AUDIO_DESKTOP_URL = b"https://github.com/dilshadshahsyed883-cmd/Rajudesktop/releases/download/song-audio-downloader-run-3/SongAudioDownloader.exe"
SONG_AUDIO_DESKTOP_STYLE = b'<style id="song-audio-desktop-download-20261030">'
SONG_AUDIO_DESKTOP_OPEN = b'    <!-- SONG_AUDIO_DESKTOP_DOWNLOAD_V1 -->'
SONG_AUDIO_DESKTOP_CLOSE = b'    <!-- /SONG_AUDIO_DESKTOP_DOWNLOAD_V1 -->'
SONG_AUDIO_DESKTOP_BADGE = '    <span class="vsa-badge" id="vsaReady">Checking downloader…</span>'.encode("utf-8")


def song_audio_desktop_download_overlay(original: bytes, source: bytes) -> bytes:
    """Copy reviewed download markup and scoped style; leave the live Voice engines untouched."""
    require(source.count(SONG_AUDIO_DESKTOP_OPEN) == 1 and
            source.count(SONG_AUDIO_DESKTOP_CLOSE) == 1 and
            source.count(SONG_AUDIO_DESKTOP_URL) == 1,
            "Song Audio desktop link source is not approved")
    a = source.index(SONG_AUDIO_DESKTOP_OPEN)
    b = source.index(SONG_AUDIO_DESKTOP_CLOSE, a) + len(SONG_AUDIO_DESKTOP_CLOSE)
    block = source[a:b]
    require(block.count(b'id="vsaDesktopAppDownload"') == 1 and
            block.count(SONG_AUDIO_DESKTOP_BADGE.strip()) == 1 and
            b'rel="noopener noreferrer"' in block and
            b'target="_blank"' in block and
            b'<script' not in block and b'onclick=' not in block,
            "Song Audio desktop link markup changed")
    require(source.count(SONG_AUDIO_DESKTOP_STYLE) == 1,
            "desktop download style missing or duplicated")
    a = source.index(SONG_AUDIO_DESKTOP_STYLE)
    b = source.index(b'</style>', a) + len(b'</style>')
    css = source[a:b]
    require(b'<script' not in css and b'url(' not in css and b'@import' not in css,
            "desktop download CSS cannot import remote resources")
    if SONG_AUDIO_DESKTOP_OPEN in original:
        require(original.count(SONG_AUDIO_DESKTOP_OPEN) == 1 and
                original.count(SONG_AUDIO_DESKTOP_CLOSE) == 1,
                "multiple live desktop download blocks")
        a = original.index(SONG_AUDIO_DESKTOP_OPEN)
        b = original.index(SONG_AUDIO_DESKTOP_CLOSE, a) + len(SONG_AUDIO_DESKTOP_CLOSE)
        original = original[:a] + block + original[b:]
    else:
        require(original.count(SONG_AUDIO_DESKTOP_BADGE) == 1,
                "original downloader readiness indicator missing")
        original = original.replace(SONG_AUDIO_DESKTOP_BADGE, block, 1)
    if SONG_AUDIO_DESKTOP_STYLE in original:
        require(original.count(SONG_AUDIO_DESKTOP_STYLE) == 1,
                "multiple live desktop download style blocks")
        a = original.index(SONG_AUDIO_DESKTOP_STYLE)
        b = original.index(b'</style>', a) + len(b'</style>')
        original = original[:a] + css + original[b:]
    else:
        anchor = b'<style id="audio-downloader-v1-css">'
        require(original.count(anchor) == 1, "existing Audio Downloader CSS anchor missing")
        original = original.replace(anchor, css + b'\n\n' + anchor, 1)
    require(original.count(SONG_AUDIO_DESKTOP_URL) == 1 and
            original.count(b'id="vsaDesktopAppDownload"') == 1,
            "desktop download URL or button duplicated")
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


def require_idle_audio_downloader() -> None:
    """Never interrupt an actively processing or waiting audio job for a UI-only release."""
    request = urllib.request.Request("http://127.0.0.1:8143/tts/api/audio-downloader/status")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            require(response.status == 200, "Audio Downloader status API unavailable")
            status = json.loads(response.read(32768))
    except (OSError, ValueError) as exc:
        raise RuntimeError("Audio Downloader preflight unavailable; abort Voice reload") from exc
    require(isinstance(status, dict) and isinstance(status.get("queue"), dict),
            "Audio Downloader status missing actual queue counts")
    busy_states = ("queued", "downloading", "downloaded", "processing", "converting", "uploading")
    require(all(not int(status["queue"].get(name) or 0) for name in busy_states),
            "Audio Downloader has active/pending jobs; postpone Voice UI reload")


def verify_audio_downloader_https(cookie: str) -> None:
    """Proof of genuine authenticated Queue, History datasource and preserved status/proxy APIs."""
    from production.route_uploader_scene_https import https_get
    paths = (
        ("/voice-studio/tts/api/audio-downloader/jobs?limit=1", "jobs"),
        ("/voice-studio/tts/api/audio-downloader/status", "queue"),
        ("/voice-studio/tts/api/audio-downloader/proxy", "profiles"),
    )
    for path, key in paths:
        status, kind, body = https_get(path, cookie)
        require(status == 200 and "json" in kind.lower(),
                "Audio Downloader authenticated HTTPS "+key+" endpoint failed")
        try:
            response = json.loads(body)
        except (TypeError, ValueError) as exc:
            raise RuntimeError("Audio Downloader HTTPS "+key+" returned malformed JSON") from exc
        require(isinstance(response, dict) and isinstance(response.get(key),
                list if key != "queue" else dict),
                "Audio Downloader HTTPS "+key+" did not return real API contract")
        anonymous, _, _ = https_get(path)
        require(anonymous in (401, 403, 303),
                "Audio Downloader HTTPS "+key+" was accessible without session")


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
        require(RESPONSIVE_ID in body and (NEW_ASSETS in body or SONG_STUDIO_ASSETS in body) and
                NEW_LIMITS in body and
                OLD_LIMITS not in body and b'id="vsEngineOmni"' in body,
                "Voice web UI template guard is not live")
    require(body.count(SONG_AUDIO_DESKTOP_URL) == 1 and
            body.count(b'id="vsaDesktopAppDownload"') == 1,
            "Song Audio desktop link missing in local page")
    verify_edge_audio_markup(body)
    verify_omni_kokoro_header(body)
    require(VOICE_UI_PARITY_ID in body and
            body.count(b'<th scope="col">Controls</th>') == 2 and
            b"tr.append(a,b,c,d);rows.append(tr);" in body and
            b"d.append(button);" in body and
            b"ctrl.className='vsq-controls'" in body and
            b"c.append(' ',button);" not in body,
            "LIVE_VOICE_UI_PARITY_MISSING: upstream Voice UI stayed old")
    require(all(new in body and old not in body for old, new in QUEUE_ROUTE_PAIRS + QUEUE_FETCH_PAIRS),
            "local Voice queue JavaScript is not updated")
    require(all(new in body and old not in body for old, new in AUDIO_BROWSER_ROUTE_PAIRS),
            "local Audio Downloader browser URLs not updated")
    from production.route_uploader_scene_https import https_get, scene_cookie
    cookie = scene_cookie()
    code, kind, html = https_get("/voice-studio/app", cookie)
    require(code == 200 and "html" in kind.lower()
            and (b"vps1_native_site_shell.js?v=20261020-upload-studio-v1" in html or
                 b"vps1_native_site_shell.js?v=20261029-song-studio-v1" in html)
            and RESPONSIVE_ID in html and NEW_LIMITS in html and OLD_LIMITS not in html,
            "authenticated public Voice page template guard not updated")
    full_html = read_full_authenticated_voice_html(cookie)
    require(full_html.count(SONG_AUDIO_DESKTOP_URL) == 1 and
            full_html.count(b'id="vsaDesktopAppDownload"') == 1,
            "Song Audio desktop link missing on authenticated public page")
    verify_edge_audio_markup(full_html)
    verify_omni_kokoro_header(full_html)
    require(VOICE_UI_PARITY_ID in full_html and
            full_html.count(b'<th scope="col">Controls</th>') == 2 and
            b"tr.append(a,b,c,d);rows.append(tr);" in full_html and
            b"d.append(button);" in full_html and
            b"ctrl.className='vsq-controls'" in full_html,
            "LIVE_VOICE_UI_PARITY_MISSING: authenticated public page stayed old")
    require(all(new in full_html and old not in full_html for old, new in QUEUE_ROUTE_PAIRS + QUEUE_FETCH_PAIRS),
            "public Voice queue JavaScript is not updated")
    # Nginx may rewrite HTML string literals via sub_filter. Exact reviewed
    # browser-source bytes are verified against the local upstream above; the
    # public verification must check the served script revision plus real API
    # contracts rather than treating a transformed response as identical bytes.
    require(b"AUDIO_DOWNLOADER_BROWSER_ROUTE_CONTRACT_V2" in full_html
            and b'id="audio-downloader-v1-js"' in full_html
            and b'id="audio-downloader-proxy-v1"' in full_html,
            "public Audio Downloader script marker absent")
    # Browser calls, not just the API endpoint, must survive Nginx HTML
    # sub_filter. Existing gateway rewrites literal '/jobs to '/voice-studio/jobs.
    # Check the actual authenticated publicly served HTML after filtering.
    start = full_html.find(b'<script id="audio-downloader-v1-js">')
    end = full_html.find(b'</script>', start)
    proxy_start = full_html.find(b'<script id="audio-downloader-proxy-v1">')
    proxy_end = full_html.find(b'</script>', proxy_start)
    require(start >= 0 and end > start and proxy_start >= 0 and proxy_end > proxy_start,
            "public Audio Downloader browser scripts missing")
    downloader_js = full_html[start:end]
    proxy_js = full_html[proxy_start:proxy_end]
    require(all(corrected in downloader_js + proxy_js and old not in downloader_js + proxy_js
                for old, corrected in AUDIO_BROWSER_V3_PATH_PAIRS),
            "public Audio Downloader browser fetch paths rewritten or stale")
    require(b"'/voice-studio/jobs" not in downloader_js and
            b"'/voice-studio/status" not in downloader_js and
            b"'/voice-studio/profiles" not in proxy_js,
            "public Audio Downloader browser fetch paths duplicated Voice prefix")
    verify_audio_downloader_https(cookie)
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
    require((NEW_ASSETS in source_html or SONG_STUDIO_ASSETS in source_html) and
            source_html.count(NEW_LIMITS) == 1,
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
    patched_html = omnivoice_parity_overlay(patched_html, source_html)
    patched_html = voice_ui_parity_overlay(patched_html, source_html)
    patched_html = omni_kokoro_header_overlay(patched_html, source_html)
    patched_html = voice_edge_audio_overlay(patched_html, source_html)
    patched_html = queue_route_overlay(patched_html, source_html)
    patched_html = audio_browser_route_overlay(patched_html, source_html)
    patched_html = song_audio_desktop_download_overlay(patched_html, source_html)
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
        require_idle_audio_downloader()
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
