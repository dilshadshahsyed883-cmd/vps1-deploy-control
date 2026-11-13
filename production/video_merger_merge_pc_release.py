"""Fail-closed Video Merger Video 2 PC picker overlay.

Only reviewed Merge-specific UI fragments may be patched in the active release.
Everything else (Queue, Library, Proxy, scene shell) must be retained verbatim.
"""

class MergePcOverlayBlocked(RuntimeError):
    pass


def require(condition,reason):
    if not condition:
        raise MergePcOverlayBlocked("VIDEO2_PC_OVERLAY_BLOCKED: "+reason)


def one_line(page,prefix):
    found=[line for line in page.splitlines() if line.startswith(prefix)]
    require(len(found)==1,"missing or ambiguous callback "+prefix)
    return found[0]


def section(page,start,end):
    require(page.count(start)==1 and page.count(end)==1,"ambiguous section "+start)
    a=page.index(start); b=page.index(end,a+len(start))
    require(a<b,"wrong section order "+start)
    return page[a:b]


def apply_merge_pc_overlay(live,source,baseline):
    require('function selectMergePcFile(input)' in source and
            'class="vm-merge-pc-file"' in source and
            'every(mergePcReady)' in source,
            "reviewed PC controls missing")
    require('function selectMergePcFile(input)' not in baseline,
            "baseline already contains PC controls")
    for prefix in (
        'function urlRow(',
        'function toggleTrim(cb){',
        'function specFromItem(item,i){',
        'function scheduleAutoName(){',
        'async function refresh(){',
        'function mergerEligibility(){',
    ):
        old=one_line(baseline,prefix)
        new=one_line(source,prefix)
        require(old!=new,"expected Merge callback change absent "+prefix)
        if live.count(new)==1:
            continue
        require(live.count(old)==1,"live Merge callback drift "+prefix)
        live=live.replace(old,new,1)
    start='function vmQueueButtonState(){'
    end='async function queueMerge(){'
    old=section(baseline,start,end)
    new=section(source,start,end)
    require(old!=new,"expected queue button change absent")
    if live.count(new)!=1:
        require(live.count(old)==1,"live queue button drift")
        live=live.replace(old,new,1)
    helpers=section(source,'function mergePcReady(item){','function specFromItem(item,i){')
    require('function selectMergePcFile(input)' in helpers,"upload helper absent")
    if live.count(helpers)!=1:
        require('function mergePcReady(item){' not in live,
                "unexpected existing upload helper")
        anchor='function specFromItem(item,i){'
        require(live.count(anchor)==1,"upload insertion anchor missing")
        live=live.replace(anchor,helpers+anchor,1)
    require(live.count('function selectMergePcFile(input)')==1 and
            live.count('class="vm-merge-pc-file"')==1 and
            live.count('every(mergePcReady)')==1,
            "PC Video 2 overlay incomplete")
    return live
