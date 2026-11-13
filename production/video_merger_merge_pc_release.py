"""Fail-closed overlay: extend only Video Merger merge URL rows with PC file inputs.

The live release may contain unrelated Queue, Library, Proxy, and shell work.
Never replace the full page.
"""


class MergePcUiDrift(RuntimeError):
    pass


def exactly_one(page, text, message):
    if page.count(text)!=1:
        raise MergePcUiDrift('VIDEO_MERGE_PC_UI_BLOCKED: '+message)


def section(page, start, end):
    exactly_one(page,start,'missing or duplicated boundary '+start)
    exactly_one(page,end,'missing or duplicated boundary '+end)
    i=page.index(start)
    j=page.index(end,i+len(start))
    if j<i:
        raise MergePcUiDrift('VIDEO_MERGE_PC_UI_BLOCKED: invalid section order')
    return page[i:j]


def frozen_merge_pc_overlay(live, source, baseline):
    """Patch the exact reviewed merge controls, preserving every other byte."""
    control='.vm-app .vm-pc-merge-choice{display:flex;align-items:center;gap:8px;min-width:0}'
    css_anchor='.vm-app .video-item{'
    exactly_one(source,control,'PC merge style missing')
    if control not in live:
        exactly_one(live,css_anchor,'live CSS anchor drift')
        css=section(source,control,css_anchor)
        if control in baseline:
            raise MergePcUiDrift('VIDEO_MERGE_PC_UI_BLOCKED: baseline already modified')
        live=live.replace(css_anchor,css+css_anchor,1)
    else:
        exactly_one(live,control,'duplicate PC merge CSS')

    boundaries=(
        ('function urlRow(','function relabelRows(){'),
        ('function toggleTrim(','function resText(){'),
        ('function specFromItem(','let vmQueueAdding=false'),
        ('async function queueMerge(){','function scheduleAutoName(){'),
        ('function scheduleAutoName(){','function activeStatus('),
    )
    for start,end in boundaries:
        old=section(baseline,start,end)
        new=section(source,start,end)
        if old==new:
            raise MergePcUiDrift('VIDEO_MERGE_PC_UI_BLOCKED: missing reviewed change')
        if live.count(new)==1:
            continue
        if live.count(old)!=1:
            raise MergePcUiDrift('VIDEO_MERGE_PC_UI_BLOCKED: live Merge UI drift: '+start)
        live=live.replace(old,new,1)

    for marker in ('class="vm-pc-merge-choice"', 'class="video-pc-file"',
                   'async function uploadMergePcRow(', '/api/merge/pc/start',
                   '/api/merge/pc/chunk','await uploadMergePcRow(item)'):
        exactly_one(live,marker,'PC merge control absent or duplicated: '+marker)
    return live
