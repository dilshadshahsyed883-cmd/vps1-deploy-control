"""One host-locked Song Studio rollout; producer enabled last, with guarded rollback."""
import ast
import fcntl
import json
from pathlib import Path

from production import runtime, song_maker_release as maker, audio_downloader_release as audio
from production.voice_template_release import require, require_idle_audio_downloader

MARKER='[vps1-song-folder-flow]'
ALLOWED={'init_storage','AudioPreferences','BatchRequest','_song_folder','get_preferences',
         'save_preferences','enqueue','_post_local_pair_to_song_maker','list_jobs','row_for',
         'get_job','job_action','download_audio','download_thumbnail'}


def audio_overlay(installed:bytes,reviewed:bytes) -> bytes:
    """Replace only folder admission/handoff code, preserving the installed yt-dlp engine."""
    old=installed.decode();new=reviewed.decode();old_nodes=ast.parse(old);new_nodes=ast.parse(new)
    def selected(tree):
        return {n.name:n for n in tree.body if isinstance(n,(ast.ClassDef,ast.FunctionDef,ast.AsyncFunctionDef))}
    before=selected(old_nodes);after=selected(new_nodes);edits=[];additions=[]
    old_lines=old.splitlines(keepends=True);new_lines=new.splitlines(keepends=True)
    def span(n):return min([n.lineno]+[d.lineno for d in n.decorator_list])-1,n.end_lineno
    for name in ALLOWED:
        require(name in after,'reviewed folder node missing: '+name)
        start,end=span(after[name]);text=''.join(new_lines[start:end])
        if name in before:
            a,b=span(before[name]);edits.append((a,b,text+'\n'))
        else:
            require(name=='_song_folder','installed admission contract missing: '+name)
            additions.append(text)
    for start,end,text in sorted(edits,reverse=True):old_lines[start:end]=[text]
    result=''.join(old_lines)+'\n\n'+'\n\n'.join(additions)+'\n'
    final=selected(ast.parse(result))
    for name,node in before.items():
        if name not in ALLOWED:
            require(name in final and ast.dump(node,include_attributes=False)==ast.dump(final[name],include_attributes=False),
                    'unapproved downloader engine change: '+name)
    require('song_folder_id' in result,'folder overlay missing')
    return result.encode()


def verify():
    from production.route_uploader_scene_https import scene_cookie, https_get
    cookie=scene_cookie();proof={}
    for path in ('/song-maker/api/folders','/song-maker/api/state','/voice-studio/tts/api/audio-downloader/preferences'):
        code,kind,body=https_get(path,cookie)
        require(code==200 and 'json' in kind.lower(),'Song folder HTTPS readback failed: '+path)
        data=json.loads(body)
        if path.endswith('/preferences'):require('song_folder_id' in data.get('preferences',{}),'Audio folder preference missing')
        if path.endswith('/folders'):require(isinstance(data.get('folders'),list),'Folder registry missing')
        proof[path]='PASS'
    code,_,page=https_get('/song-uploader?tab=auto_engine&new_profile=1',cookie)
    require(code==200 and b'Song Maker VPS Folder' in page and b'song-profile-folder' in page,'Song Uploader folder source unavailable')
    code,_,page=https_get('/audio-downloader/',cookie)
    require(code==200 and b'vsaFolderConfirmStart' in page and b'vsaSongFolder' in page,'Audio folder confirmation unavailable')
    # The destructive receipt endpoint is outside the public Nginx prefix.
    require(https_get('/song-maker/internal/song-folders',cookie)[0]!=200,'internal transfer endpoint exposed')
    proof.update(uploader='PASS',audio_confirmation='PASS',real_youtube_publish='NOT_RUN')
    return proof


def install(revision):
    require(len(revision)==40,'exact release SHA required')
    journal=runtime.BASE/'transactions/song-folder-workflow'/revision
    require(not journal.exists(),'immutable Song folder journal already exists')
    # All three persistent queues must be idle before the first service changes.
    maker.require_idle_jobs(maker.STATE/'state/jobs.sqlite3')
    runtime.require_lum_release_idle(runtime.DATA/'local-upload-manager')
    require_idle_audio_downloader()
    previous={'maker':str(maker.CURRENT.resolve(strict=True)),'voice':str(audio.CURRENT.resolve(strict=True))}
    journal.mkdir(parents=True,mode=0o700);runtime.atomic_json(journal/'previous.json',previous)
    completed=[]
    try:
        maker.install(revision);completed.append('maker')
        runtime.install_app('local-upload-manager',revision);completed.append('uploader')
        # Audio admission is enabled last; no new folder jobs can precede consumers.
        audio.install(revision,restore_proxy=False,include_thumbnail=True,song_folders=True);completed.append('voice')
        proof=verify()
        runtime.atomic_json(journal/'result.json',{'state':'LIVE_HEALTH_VERIFIED','revision':revision,'proof':proof})
        runtime.event(app='song-studio',result='PASS_SONG_FOLDER_WORKFLOW',revision=revision,proof=proof,auto_script='OFFLINE')
    except BaseException:
        rollbacks={}
        for name in reversed(completed):
            try:
                if name=='voice':
                    require_idle_audio_downloader();runtime.atomic_link(audio.CURRENT,previous['voice'])
                    runtime.run(['systemctl','restart',audio.UNIT],quiet=True);runtime.http_check('voice-studio')
                elif name=='uploader':
                    runtime.require_lum_release_idle(runtime.DATA/'local-upload-manager')
                    record=json.loads((runtime.BASE/'control/state/local-upload-manager.json').read_text())
                    require(record.get('revision')==revision,'uploader revision drift blocks rollback')
                    runtime.restore('local-upload-manager',record['previous'])
                else:
                    with (maker.STATE/'state/submission.lock').open('a') as fence:
                        fcntl.flock(fence,fcntl.LOCK_EX|fcntl.LOCK_NB)
                        maker.require_idle_jobs(maker.STATE/'state/jobs.sqlite3')
                        record=json.loads((maker.TRANSACTIONS/revision/'previous.json').read_text())
                        maker.restore_update(record)
                rollbacks[name]='PASS_DATA_PRESERVED'
            except Exception as exc:
                rollbacks[name]='DEFERRED_DATA_PRESERVED: '+str(exc)[:300]
        runtime.atomic_json(journal/'result.json',{'state':'FAILED','revision':revision,'rollback':rollbacks,'databases':'PRESERVED'})
        raise
