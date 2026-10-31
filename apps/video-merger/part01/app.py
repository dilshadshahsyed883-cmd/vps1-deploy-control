#!/usr/bin/env python3
import sys, importlib.util, json, os, re, shutil, socket, sqlite3, subprocess, threading, time, uuid, urllib.parse, ipaddress, html, tempfile, shlex, stat
from contextlib import contextmanager
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

APP_NAME = 'Video Merger'
RELEASE_ID = 'VM_IMPORT_REPAIR_20261021'
BASE = Path(os.environ.get('VIDEO_MERGER_ROOT', '/opt/video-merger')).expanduser()
DATA = BASE / 'data'
WORK = BASE / 'work'
DB_PATH = DATA / 'video_merger.db'
LOCAL_OUTPUT_ROOT = BASE / 'verified_outputs'
COPYRIGHT_MEDIA_ROOT = Path(os.environ.get('VIDEO_MERGER_COPYRIGHT_ROOT', str(BASE / 'copyright_handoff')))
def owner_media_key(owner):
    import hashlib
    return hashlib.sha256(owner.strip().lower().encode('utf-8')).hexdigest()[:32]

RCLONE = '/usr/bin/rclone'
FFMPEG = '/usr/bin/ffmpeg'
FFPROBE = '/usr/bin/ffprobe'
RCLONE_CONFIG = '/home/ubuntu/.config/masterctl/rclone.conf'
REMOTE = 'gdrive:'
OUTPUT_ROOT = 'Video Merger Outputs'
LIBRARY_ROOT = 'Video Merger Library'
LIBRARY_CACHE = BASE / 'library_cache'
PROXY_CONFIG_DIR = Path(os.environ.get('VIDEO_MERGER_PROXY_CONFIG_DIR', '/home/ubuntu/.config/video-merger'))
PROXY_CONFIG_PATH = PROXY_CONFIG_DIR / 'proxy_profiles.json'
# Always use the interpreter running this release, never a data-directory venv.
def downloader_command(module):
    if importlib.util.find_spec(module) is None:
        raise RuntimeError(f'Video Merger dependency {module} is missing from its service Python; redeploy Video Merger dependencies')
    return [sys.executable, '-m', module]
DENO = Path(sys.executable).parent / 'deno'
if not DENO.is_file(): DENO = BASE / 'bin/deno'
os.environ.setdefault('DENO_DIR', str(BASE / 'deno_cache'))
PREFIX = '/video-merger'
MAX_VIDEOS = 50
MAX_URL_LEN = 4096
MAX_NAME_LEN = 180
MAX_SOURCE_BYTES = 12 * 1024 * 1024 * 1024
MIN_FREE_BYTES = 5 * 1024 * 1024 * 1024

for p in (DATA, WORK, LIBRARY_CACHE, PROXY_CONFIG_DIR): p.mkdir(parents=True, exist_ok=True)
try: os.chmod(PROXY_CONFIG_DIR, 0o700)
except Exception: pass

DB_LOCK = threading.RLock()
WORKER_WAKE = threading.Event()
STOP_EVENT = threading.Event()
FOLDER_CACHE = {'ts': 0.0, 'items': []}
FOLDER_CACHE_LOCK = threading.Lock()
PROXY_LOCK = threading.RLock()
LIBRARY_LOCK = threading.RLock()

RES_MAP = {
    ('16:9','720p'):(1280,720), ('16:9','1080p'):(1920,1080),
    ('9:16','720p'):(720,1280), ('9:16','1080p'):(1080,1920),
}

def utcnow(): return datetime.now(timezone.utc).isoformat(timespec='seconds')
def db():
    c=sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    c.row_factory=sqlite3.Row
    return c

def init_db():
    with DB_LOCK, db() as c:
        c.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS jobs(
          id TEXT PRIMARY KEY, name TEXT NOT NULL, videos_json TEXT NOT NULL,
          aspect TEXT NOT NULL, quality TEXT NOT NULL, fit_mode TEXT NOT NULL,
          folder TEXT NOT NULL, filename TEXT NOT NULL, status TEXT NOT NULL,
          progress INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '',
          error TEXT NOT NULL DEFAULT '', drive_url TEXT NOT NULL DEFAULT '',
          drive_id TEXT NOT NULL DEFAULT '', order_idx INTEGER NOT NULL,
          drive_enabled INTEGER NOT NULL DEFAULT 1,
          copyright_enabled INTEGER NOT NULL DEFAULT 0,
          owner_username TEXT NOT NULL DEFAULT '',
          local_path TEXT NOT NULL DEFAULT '',
          retry_of TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings(k TEXT PRIMARY KEY, v TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS library_items(
          id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE, filename TEXT NOT NULL,
          remote_path TEXT NOT NULL DEFAULT '', drive_id TEXT NOT NULL DEFAULT '', drive_url TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'ready', progress INTEGER NOT NULL DEFAULT 0, message TEXT NOT NULL DEFAULT '',
          error TEXT NOT NULL DEFAULT '', duration REAL NOT NULL DEFAULT 0, size_bytes INTEGER NOT NULL DEFAULT 0,
          source_url TEXT NOT NULL DEFAULT '', trim_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          owner_username TEXT NOT NULL DEFAULT ''
        );
        INSERT OR IGNORE INTO settings(k,v) VALUES('paused','0');
        CREATE TABLE IF NOT EXISTS owner_queue_state (
          owner_username TEXT PRIMARY KEY COLLATE NOCASE,
          paused INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS drive_folders (
          path TEXT PRIMARY KEY,
          owner_username TEXT NOT NULL,
          created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS drive_folders_owner_idx
          ON drive_folders(owner_username);
        ''')
        existing = {row[1] for row in c.execute("PRAGMA table_info(jobs)")}
        for name, kind in (
            ("drive_enabled", "INTEGER NOT NULL DEFAULT 1"),
            ("copyright_enabled", "INTEGER NOT NULL DEFAULT 0"),
            ("owner_username", "TEXT NOT NULL DEFAULT ''"),
            ("local_path", "TEXT NOT NULL DEFAULT ''"),
            ("retry_of", "TEXT NOT NULL DEFAULT ''"),
        ):
            if name not in existing:
                c.execute(f"ALTER TABLE jobs ADD COLUMN {name} {kind}")
        library_columns={row[1] for row in c.execute("PRAGMA table_info(library_items)")}
        if "owner_username" not in library_columns:
            c.execute("ALTER TABLE library_items ADD COLUMN owner_username TEXT NOT NULL DEFAULT ''")
        # Legacy, unassigned library media is preserved for admin recovery;
        # never speculate which current user owned it.
        c.execute("UPDATE jobs SET status='waiting', message='Recovered after service restart', updated_at=? WHERE status IN ('downloading','trimming','preparing','merging','verifying','uploading','processing')", (utcnow(),))
        c.execute("UPDATE library_items SET status='failed', progress=100, message='Import interrupted by service restart', error='Import interrupted by service restart', updated_at=? WHERE status='importing'", (utcnow(),))

def setting(k, default=''):
    with DB_LOCK, db() as c:
        r=c.execute('SELECT v FROM settings WHERE k=?',(k,)).fetchone()
        return r['v'] if r else default

def set_setting(k,v):
    with DB_LOCK, db() as c:
        c.execute('INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v',(k,str(v)))


def _default_proxy_config():
    return {'enabled':False,'active_id':'','profiles':[]}

def load_proxy_config():
    with PROXY_LOCK:
        if not PROXY_CONFIG_PATH.exists(): return _default_proxy_config()
        try:
            obj=json.loads(PROXY_CONFIG_PATH.read_text(encoding='utf-8'))
            if not isinstance(obj,dict): raise ValueError
            obj.setdefault('enabled',False); obj.setdefault('active_id',''); obj.setdefault('profiles',[])
            if not isinstance(obj['profiles'],list): obj['profiles']=[]
            return obj
        except Exception:
            return _default_proxy_config()

def save_proxy_config(cfg):
    with PROXY_LOCK:
        PROXY_CONFIG_DIR.mkdir(parents=True,exist_ok=True)
        tmp=PROXY_CONFIG_PATH.with_suffix('.tmp')
        tmp.write_text(json.dumps(cfg,indent=2),encoding='utf-8')
        os.chmod(tmp,0o600); os.replace(tmp,PROXY_CONFIG_PATH); os.chmod(PROXY_CONFIG_PATH,0o600)

def proxy_profile_by_id(pid):
    cfg=load_proxy_config()
    return next((x for x in cfg.get('profiles',[]) if x.get('id')==pid),None)

def proxy_url(profile):
    if not profile: return ''
    host=(profile.get('host') or '').strip(); port=int(profile.get('port') or 0)
    user=urllib.parse.quote(str(profile.get('username') or ''),safe='')
    pw=urllib.parse.quote(str(profile.get('password') or ''),safe='')
    auth=(user+':'+pw+'@') if (user or pw) else ''
    return f"http://{auth}{host}:{port}"

def active_proxy_profile():
    cfg=load_proxy_config()
    if not cfg.get('enabled'): return None
    aid=cfg.get('active_id') or ''
    profile=next((x for x in cfg.get('profiles',[]) if x.get('id')==aid),None)
    if not profile: raise RuntimeError('Enabled proxy profile is missing; select a valid profile or disable proxy')
    return profile

def public_proxy_state():
    cfg=load_proxy_config(); aid=cfg.get('active_id') or ''
    items=[]
    for x in cfg.get('profiles',[]):
        items.append({'id':x.get('id',''),'name':x.get('name',''),'host':x.get('host',''),'port':int(x.get('port') or 0),
                      'username':x.get('username',''),'has_password':bool(x.get('password')),'active':x.get('id')==aid})
    return {'enabled':bool(cfg.get('enabled')),'active_id':aid,'profiles':items}

def validate_proxy_fields(payload, existing=None):
    name=(payload.get('name') or '').strip()[:80]
    host=(payload.get('host') or '').strip()[:255]
    username=(payload.get('username') or '').strip()[:255]
    password=str(payload.get('password') or '')
    if existing is not None and not password: password=str(existing.get('password') or '')
    try: port=int(payload.get('port') or 0)
    except Exception: raise ValueError('Proxy port must be a number')
    if not name: raise ValueError('Proxy profile name is required')
    if not host or any(c.isspace() for c in host) or '/' in host: raise ValueError('Valid proxy host is required')
    if port<1 or port>65535: raise ValueError('Proxy port must be 1–65535')
    if not username: raise ValueError('Proxy username is required')
    if not password: raise ValueError('Proxy password is required')
    return {'name':name,'host':host,'port':port,'username':username,'password':password}

def save_proxy_profile(payload):
    cfg=load_proxy_config(); pid=(payload.get('id') or '').strip(); existing=None
    if pid: existing=next((x for x in cfg['profiles'] if x.get('id')==pid),None)
    if pid and not existing: raise KeyError('Proxy profile not found')
    fields=validate_proxy_fields(payload,existing)
    if any((x.get('name') or '').lower()==fields['name'].lower() and x.get('id')!=pid for x in cfg['profiles']):
        raise ValueError('A proxy profile with this name already exists')
    if existing:
        existing.update(fields)
    else:
        pid=uuid.uuid4().hex[:12]; existing={'id':pid,**fields}; cfg['profiles'].append(existing)
        if not cfg.get('active_id'): cfg['active_id']=pid
    save_proxy_config(cfg); return pid

def delete_proxy_profile(pid):
    cfg=load_proxy_config(); before=len(cfg['profiles']); cfg['profiles']=[x for x in cfg['profiles'] if x.get('id')!=pid]
    if len(cfg['profiles'])==before: raise KeyError('Proxy profile not found')
    if cfg.get('active_id')==pid: cfg['active_id']=cfg['profiles'][0]['id'] if cfg['profiles'] else ''
    if not cfg['profiles']: cfg['enabled']=False
    save_proxy_config(cfg)

def set_active_proxy(pid):
    cfg=load_proxy_config()
    if not any(x.get('id')==pid for x in cfg['profiles']): raise KeyError('Proxy profile not found')
    cfg['active_id']=pid; save_proxy_config(cfg)

def set_proxy_enabled(enabled):
    cfg=load_proxy_config()
    if enabled and (not cfg.get('active_id') or not any(x.get('id')==cfg.get('active_id') for x in cfg['profiles'])):
        raise ValueError('Add and select a proxy profile first')
    cfg['enabled']=bool(enabled); save_proxy_config(cfg)

# The already-running Audio Downloader owns the verified YouTube egress.
# Video only borrows its selected route read-only, never copies or edits settings.
AUDIO_PROXY_PATH=Path('/var/lib/fresh-website/voice-studio/audio-downloader/data/proxy_settings.json')

def working_audio_proxy_profile():
    path=AUDIO_PROXY_PATH
    try:
        if path.is_symlink() or not path.is_file(): return None
        st=path.stat(follow_symlinks=False)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid!=os.geteuid()
                or st.st_mode & 0o077 or st.st_size>65536): return None
        cfg=json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(cfg,dict) or cfg.get('enabled') is not True: return None
        profiles=cfg.get('profiles')
        if not isinstance(profiles,list): return None
        p=next((x for x in profiles if isinstance(x,dict)
                and x.get('id')==cfg.get('active_id')),None)
        if p is None: return None
        host=str(p.get('host') or '')
        port=int(p.get('port') or 0)
        if (not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]*',host)
                or not 1<=port<=65535 or not p.get('username')
                or not p.get('password')): return None
        return dict(p)
    except (OSError,UnicodeError,ValueError,TypeError):
        return None


def youtube_cookie_args():
    # Optional owner-provided, app-specific Netscape cookie file; never log contents.
    value=os.environ.get('VIDEO_MERGER_YOUTUBE_COOKIES_FILE','').strip()
    if not value: return []
    path=Path(value).expanduser()
    if not path.is_absolute() or not path.is_file() or path.is_symlink():
        raise RuntimeError('Configured Video Merger YouTube cookies file is unavailable')
    return ['--cookies',str(path)]

def yt_common_args(profile=None, force_profile=False):
    # Audio Downloader has verified real media on this VPS without forcing IPv4.
    args=['--no-playlist','--no-progress','--no-cache-dir','--socket-timeout','30','--retries','1','--fragment-retries','1','--extractor-retries','0']
    if DENO.exists(): args += ['--js-runtimes',f'deno:{DENO}']
    elif shutil.which('node'): args += ['--js-runtimes','node:'+shutil.which('node')]
    args += youtube_cookie_args()
    if profile is None and not force_profile: profile=active_proxy_profile()
    args += ['--proxy',proxy_url(profile) if profile else '']
    return args

def _private_youtube_cookie_bytes(path):
    """Audio Downloader's read-only private Netscape session contract."""
    fd=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=os.geteuid()
                or info.st_mode & 0o077 or info.st_size>1024*1024):
            raise ValueError('private owner-readable cookie export required')
        raw=stream.read(1024*1024+1)
    value=raw.decode('utf-8')
    if not value.startswith(('# Netscape HTTP Cookie File','# HTTP Cookie File')):
        raise ValueError('Netscape cookie header required')
    valid=False
    for line in value.splitlines():
        if not line or (line.startswith('#') and not line.startswith('#HttpOnly_')):
            continue
        fields=line.removeprefix('#HttpOnly_').split('\t')
        if len(fields)!=7: raise ValueError('invalid cookie entry')
        domain=fields[0].lstrip('.').lower()
        if domain in ('youtube.com','google.com') or domain.endswith(('.youtube.com','.google.com')):
            expiry=int(fields[4])
            valid |= expiry==0 or expiry>time.time()
    if not valid: raise ValueError('No unexpired YouTube session cookie entries')
    return raw


@contextmanager
def youtube_execution(profile=None, force_profile=False):
    """Snapshot proxy and private session for one yt-dlp attempt; no secrets in argv."""
    opts=yt_common_args(profile,force_profile)
    proxy=opts[opts.index('--proxy')+1]
    env=os.environ.copy()
    for key in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy','NO_PROXY','no_proxy'):
        env.pop(key,None)
    if proxy:
        for key in ('HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'):
            env[key]=proxy
    with tempfile.TemporaryDirectory(prefix='vm-youtube-',dir=WORK) as work:
        tmp=Path(work)
        config=tmp/'yt-dlp.conf'
        settings=[]
        params={'--socket-timeout','--retries','--fragment-retries','--extractor-retries',
                '--js-runtimes','--proxy','--cookies'}
        i=0
        while i<len(opts):
            flag=opts[i]
            if flag not in params:
                settings.append(flag)
                i+=1
                continue
            value=opts[i+1]
            if flag=='--cookies':
                try:
                    raw=_private_youtube_cookie_bytes(value)
                except (OSError,UnicodeError,ValueError) as exc:
                    raise RuntimeError('YOUTUBE_SESSION_CONFIGURATION: Valid private Netscape YouTube cookies are required') from None
                session=tmp/'session.txt'
                session.write_bytes(raw)
                session.chmod(0o600)
                value=str(session)
            settings.append(flag+' '+shlex.quote(value))
            i+=2
        config.write_text('\n'.join(settings)+'\n',encoding='utf-8')
        config.chmod(0o600)
        yield ['--ignore-config','--config-locations',str(config)],env


def youtube_failure(error):
    """Report source-specific provider blocks without exposing raw command output."""
    message=scrub_proxy_error(error)
    value=message.lower().replace('’',"'")
    if any(token in value for token in ('not a bot','sign in','confirm your age','cookies are no longer valid')):
        return RuntimeError('YOUTUBE_AUTH_REQUIRED: This URL was challenged by YouTube on both supported playback paths. The selected proxy is not proof of access; try a later manual Retry or an authorized session if this source requires one.')
    if 'po token' in value or 'proof of origin' in value:
        return RuntimeError('YOUTUBE_PROVIDER_CHALLENGE: This video requires additional YouTube verification; a proxy alone cannot guarantee access.')
    if '407' in value or 'proxy authentication' in value:
        return RuntimeError('PROXY_AUTH_FAILED: Selected YouTube proxy rejected authentication.')
    if '429' in value or 'too many requests' in value:
        return RuntimeError('YOUTUBE_RATE_LIMIT: YouTube rate-limited this session or IP. Retry later.')
    if any(term in value for term in ('tunnel connection failed: 503',
                                     'unable to connect to proxy','proxyerror')):
        return RuntimeError('PROXY_CONNECTION_FAILED: Selected proxy tunnel was unavailable. Video will use the separately enabled Audio route when available.')
    if '403' in value:
        return RuntimeError('YOUTUBE_ACCESS_DENIED: YouTube denied access to this video; check source permissions and authorized session.')
    return RuntimeError(message)


def run_youtube_download(options, timeout, profile=None, force_profile=False):
    """Use Video's selected route, then Audio's verified route on provider challenge.

    Both routes are resolved inside the same VPS. The Audio proxy settings are
    read only; no credential or job data crosses any remote API or gets logged.
    """
    routes=[(profile,force_profile)]
    if profile is None and not force_profile:
        audio=working_audio_proxy_profile()
        video=active_proxy_profile()
        if audio and (not video or any(
                str(audio.get(k))!=str(video.get(k)) for k in
                ('host','port','username','password'))):
            routes.append((audio,True))
    last_error=None
    for route_index,(route,forced) in enumerate(routes):
        for client in ('default','tv,web_safari'):
            with youtube_execution(route,forced) as (config,environment):
                extra=[] if client=='default' else [
                    '--extractor-args','youtube:player_client='+client]
                try:
                    return run(downloader_command('yt_dlp')+config+extra+options,
                               timeout=timeout,env=environment)
                except Exception as exc:
                    last_error=exc
                    low=scrub_proxy_error(exc).lower().replace('’',"'")
                    retryable=any(v in low for v in (
                        'not a bot','sign in to confirm','this video is unavailable',
                        'video unavailable'))
                    fatal=any(v in low for v in (
                        'http error 429','too many requests','http error 403',
                        'http error 407','proxy authentication'))
                    proxy_transport_down=any(v in low for v in (
                        'tunnel connection failed: 503','unable to connect to proxy',
                        'proxyerror'))
                    # Proven VPS case: old Video proxy returns HTTP 503 tunnel
                    # errors although its independently selected Audio proxy
                    # downloads real media. Skip a broken tunnel immediately and
                    # try the read-only Audio route once, without repeating a
                    # request against the broken proxy.
                    if proxy_transport_down and route_index+1<len(routes):
                        break
                    if fatal or not retryable:
                        raise youtube_failure(exc) from None
                    # A maximum of two standard playback clients per selected route.
                    # Only the second route is permitted after both fail.
    raise youtube_failure(last_error) from None


def scrub_proxy_error(text):
    text=str(text or '')
    for p in load_proxy_config().get('profiles',[]):
        # Scrub complete credential-bearing URLs first. Replacing one-character
        # usernames/passwords globally would corrupt error diagnostics.
        text=text.replace(proxy_url(p),'[private proxy URL]')
        for secret in (str(p.get('password') or ''),str(p.get('username') or '')):
            if len(secret)>=4: text=text.replace(secret,'***')
    text=re.sub(r'https?://[^\s@]+@','http://***:***@',text)
    # The Audio-selected route is read only. Redact its auth fields too.
    fallback=working_audio_proxy_profile()
    if fallback:
        text=text.replace(proxy_url(fallback),'[private audio proxy URL]')
        for field in ('password','username'):
            value=str(fallback.get(field) or '')
            if len(value)>=3:
                text=text.replace(value,'***')
                text=text.replace(urllib.parse.quote(value,safe=''),'***')
    cookie_path=os.environ.get('VIDEO_MERGER_YOUTUBE_COOKIES_FILE','').strip()
    if cookie_path: text=text.replace(cookie_path,'[private cookie file]')
    return text[-1800:]

def test_proxy_profile(pid):
    p=proxy_profile_by_id(pid)
    if not p: raise KeyError('Proxy profile not found')
    url=proxy_url(p)
    try:
        ip=run(['/usr/bin/curl','-fsS','--max-time','30','--proxy',url,'https://api.ipify.org'],timeout=40).strip()
        if not ip: raise RuntimeError('No egress IP returned')
        # Metadata extraction is NOT evidence that actual video bytes can download.
        # Use a short public sample, verify its media stream, and discard all bytes.
        with tempfile.TemporaryDirectory(prefix='vm-proxy-test-',dir=WORK) as workdir:
            out_pattern=str(Path(workdir)/'sample.%(ext)s')
            # The verified Audio Downloader real MP3+Drive canary used
            # jNQXAC9IVRw without cookies. Test its actual video stream too.
            run_youtube_download(['--format','bv*+ba/best/bv*',
                '--max-filesize','100M','-o',out_pattern,
                'https://youtu.be/jNQXAC9IVRw'],timeout=240,profile=p,force_profile=True)
            candidates=[f for f in Path(workdir).glob('sample.*')
                        if f.is_file() and f.suffix.lower() not in ('.part','.ytdl','.json')]
            if not candidates: raise RuntimeError('YouTube real download returned no media')
            validate_downloaded_file(candidates[0],1)
            verify_library_media(candidates[0])
        return {'egress_ip':ip,'youtube_ok':True,'download_verified':True}
    except Exception as e:
        raise RuntimeError(scrub_proxy_error(e))

def safe_component(s, maxlen=MAX_NAME_LEN):
    s=(s or '').strip().replace('\\','/').split('/')[-1]
    s=re.sub(r'[\x00-\x1f<>:"|?*]+','_',s)
    s=re.sub(r'\s+',' ',s).strip(' .')
    return s[:maxlen] or 'merged'

def safe_folder(s):
    raw=(s or '').strip().replace('\\','/')
    parts=[]
    for p in raw.split('/'):
        p=p.strip()
        if not p: continue
        if p in ('.','..'): raise ValueError('Invalid folder path')
        p=re.sub(r'[\x00-\x1f<>:"|?*]+','_',p).strip(' .')
        if not p: raise ValueError('Invalid folder name')
        parts.append(p[:100])
    if len(parts)>5: raise ValueError('Maximum folder depth is 5')
    return '/'.join(parts)

def validate_url(u):
    if not isinstance(u,str) or not u.strip() or len(u)>MAX_URL_LEN: raise ValueError('Invalid URL')
    p=urllib.parse.urlparse(u.strip())
    if p.scheme not in ('http','https') or not p.hostname: raise ValueError('Only http/https URLs are accepted')
    return u.strip()

def looks_youtube(u):
    h=(urllib.parse.urlparse(u).hostname or '').lower()
    return h in ('youtube.com','www.youtube.com','m.youtube.com','youtu.be','music.youtube.com') or h.endswith('.youtube.com')

def looks_drive(u):
    h=(urllib.parse.urlparse(u).hostname or '').lower()
    return h in ('drive.google.com','docs.google.com')

def drive_id_from_url(u):
    p=urllib.parse.urlparse(u)
    q=urllib.parse.parse_qs(p.query)
    if q.get('id'): return q['id'][0]
    m=re.search(r'/d/([A-Za-z0-9_-]{10,})', p.path)
    if m: return m.group(1)
    m=re.search(r'/file/d/([A-Za-z0-9_-]{10,})', p.path)
    return m.group(1) if m else ''

def reject_private_host(u):
    host=urllib.parse.urlparse(u).hostname
    if not host: raise ValueError('URL host missing')
    try:
        infos=socket.getaddrinfo(host,None)
    except socket.gaierror as e: raise ValueError(f'Cannot resolve host: {e}')
    for inf in infos:
        ip=ipaddress.ip_address(inf[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            raise ValueError('Private/local network URLs are not allowed')

def run(args, timeout=None, cwd=None, check=True, env=None):
    p=subprocess.run(args, cwd=cwd, timeout=timeout, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
    if check and p.returncode!=0: raise RuntimeError(f'Command failed ({p.returncode}): {" ".join(args[:3])}\n{p.stdout[-3000:]}')
    return p.stdout

def rclone(args, timeout=120, check=True):
    return run([RCLONE,'--config',RCLONE_CONFIG]+args, timeout=timeout, check=check)

def list_folders(force=False):
    with FOLDER_CACHE_LOCK:
        if not force and time.time()-FOLDER_CACHE['ts'] < 30:
            return list(FOLDER_CACHE['items'])
    try:
        out=rclone(['lsf', f'{REMOTE}{OUTPUT_ROOT}', '--dirs-only','--recursive'], timeout=120)
    except Exception:
        return []
    vals=[]
    for line in out.splitlines():
        x=line.strip().strip('/')
        if x and x not in vals: vals.append(x)
    vals=sorted(vals, key=str.lower)
    with FOLDER_CACHE_LOCK:
        FOLDER_CACHE['ts']=time.time(); FOLDER_CACHE['items']=list(vals)
    return vals

def ensure_output_root():
    rclone(['mkdir', f'{REMOTE}{OUTPUT_ROOT}'], timeout=120)

def create_folder(name):
    rel=safe_folder(name)
    if not rel: raise ValueError('Folder name required')
    ensure_output_root(); rclone(['mkdir', f'{REMOTE}{OUTPUT_ROOT}/{rel}'], timeout=120)
    with FOLDER_CACHE_LOCK: FOLDER_CACHE['ts']=0
    return rel

def delete_folder(name):
    rel=safe_folder(name)
    if not rel: raise ValueError('Root folder cannot be deleted')
    rclone(['rmdir', f'{REMOTE}{OUTPUT_ROOT}/{rel}'], timeout=120)
    with FOLDER_CACHE_LOCK: FOLDER_CACHE['ts']=0
    return rel


# Metadata-only folder listing keeps /api/state independent of Google Drive latency.
# Legacy shared output paths and files remain intact but are not owned by new users.
def owner_drive_root(owner):
    if not str(owner or '').strip():
        raise ValueError('Authenticated account required')
    return 'users/' + owner_media_key(owner)

def list_owner_folders(owner):
    root=owner_drive_root(owner)
    with DB_LOCK, db() as c:
        rows=c.execute('SELECT path FROM drive_folders WHERE lower(owner_username)=lower(?) ORDER BY path',(owner,)).fetchall()
    return [root]+[row['path'] for row in rows if row['path']!=root]

def resolve_owner_folder(owner, value):
    root=owner_drive_root(owner)
    path=safe_folder(value)
    if not path or path==root:
        return root
    with DB_LOCK, db() as c:
        row=c.execute('SELECT 1 FROM drive_folders WHERE path=? AND lower(owner_username)=lower(?)',(path,owner)).fetchone()
    if row is None:
        raise ValueError('Select a folder owned by your account')
    return path

def create_owner_folder(owner, name):
    root=owner_drive_root(owner)
    rel=safe_folder(name)
    if not rel or rel.startswith('users/'):
        raise ValueError('Enter a folder name relative to your own Drive root')
    path=root+'/'+rel
    if len(path.split('/'))>5:
        raise ValueError('Maximum folder depth exceeded')
    with DB_LOCK, db() as c:
        existing=c.execute('SELECT owner_username FROM drive_folders WHERE path=?',(path,)).fetchone()
        if existing:
            if existing['owner_username'].casefold()!=owner.casefold():
                raise ValueError('Folder belongs to another account')
            return path
    ensure_output_root()
    rclone(['mkdir',f'{REMOTE}{OUTPUT_ROOT}/{path}'],timeout=120)
    with DB_LOCK, db() as c:
        c.execute('INSERT OR IGNORE INTO drive_folders(path,owner_username,created_at) VALUES(?,?,?)',
                  (path,owner,utcnow()))
        row=c.execute('SELECT owner_username FROM drive_folders WHERE path=?',(path,)).fetchone()
        if row['owner_username'].casefold()!=owner.casefold():
            raise ValueError('Folder belongs to another account')
    return path

def delete_owner_folder(owner, path):
    path=resolve_owner_folder(owner,path)
    if path==owner_drive_root(owner):
        raise ValueError('Personal root folder is protected')
    with DB_LOCK, db() as c:
        active=c.execute("""SELECT 1 FROM jobs WHERE status NOT IN ('complete','failed','cancelled')
                            AND (folder=? OR folder LIKE ?) LIMIT 1""",(path,path+'/%')).fetchone()
        if active:
            raise ValueError('A queued or active merge is using this folder')
    # rclone rmdir refuses any nonempty folder; never delete its contents.
    rclone(['rmdir',f'{REMOTE}{OUTPUT_ROOT}/{path}'],timeout=120)
    with DB_LOCK, db() as c:
        c.execute('DELETE FROM drive_folders WHERE path=? AND lower(owner_username)=lower(?)',(path,owner))
    return path

def owner_queue_paused(owner):
    with DB_LOCK, db() as c:
        row=c.execute('SELECT paused FROM owner_queue_state WHERE owner_username=?',(owner,)).fetchone()
    return bool(row and row['paused'])

def set_owner_queue_paused(owner, paused):
    owner_drive_root(owner)
    with DB_LOCK, db() as c:
        c.execute('''INSERT INTO owner_queue_state(owner_username,paused) VALUES(?,?)
                     ON CONFLICT(owner_username) DO UPDATE SET paused=excluded.paused''',
                  (owner,int(bool(paused))))
    if not paused:
        WORKER_WAKE.set()

def validate_downloaded_file(path, idx):
    if not path.exists() or path.stat().st_size <= 0:
        raise RuntimeError(f'Video {idx}: downloaded file is empty or missing')
    if path.stat().st_size > MAX_SOURCE_BYTES:
        raise RuntimeError(f'Video {idx}: source exceeds 12 GB safety limit')
    if shutil.disk_usage(WORK).free < MIN_FREE_BYTES:
        raise RuntimeError('VPS free space fell below 5 GB safety reserve')

def download_direct_http(url, dst, idx):
    import requests
    sess=requests.Session(); cur=url
    for _ in range(6):
        reject_private_host(cur)
        r=sess.get(cur,stream=True,timeout=(20,120),allow_redirects=False,headers={'User-Agent':'VideoMerger/1.0'})
        if r.status_code in (301,302,303,307,308):
            loc=r.headers.get('Location')
            r.close()
            if not loc: raise RuntimeError(f'Video {idx}: redirect missing Location header')
            cur=urllib.parse.urljoin(cur,loc)
            continue
        r.raise_for_status()
        total=0
        with open(dst,'wb') as f:
            for chunk in r.iter_content(1024*1024):
                if not chunk: continue
                total += len(chunk)
                if total > MAX_SOURCE_BYTES:
                    raise RuntimeError(f'Video {idx}: source exceeds 12 GB safety limit')
                f.write(chunk)
        r.close(); validate_downloaded_file(dst,idx); return
    raise RuntimeError(f'Video {idx}: too many redirects')

def find_drive_item(fid):
    out=rclone(['lsjson', REMOTE, '--recursive','--files-only','--fast-list'], timeout=1800)
    items=json.loads(out or '[]')
    return next((x for x in items if str(x.get('ID') or x.get('Id') or '')==fid),None)

def source_to_file(url, dst, job_id, idx, status_cb=None):
    url=validate_url(url)
    def note(progress,message):
        if status_cb: status_cb(progress,message)
        elif job_id: update_job(job_id,'downloading',progress,message)
    if looks_youtube(url):
        note(max(3,min(38,4+idx*5)),f'Downloading video {idx}')
        tmpl=str(dst.with_suffix(''))+'.%(ext)s'
        run_youtube_download(['--merge-output-format','mp4','-f',
            'bv*+ba/best/bv*','-o',tmpl,url],timeout=7200)
        candidates=sorted(dst.parent.glob(dst.stem+'.*'),key=lambda p:p.stat().st_mtime,reverse=True)
        candidates=[p for p in candidates if p.is_file() and p.suffix.lower() not in ('.part','.ytdl','.json','.jpg','.webp')]
        if not candidates: raise RuntimeError(f'Video {idx}: YouTube download produced no file')
        shutil.move(str(candidates[0]),dst); validate_downloaded_file(dst,idx); verify_library_media(dst); return
    if looks_drive(url):
        note(max(3,min(38,4+idx*5)),f'Downloading Drive video {idx}')
        # Private files use the connected Drive account when public download fails.
        public_error=''
        try:
            run(downloader_command('gdown')+['--fuzzy',url,'-O',str(dst)],timeout=7200)
            validate_downloaded_file(dst,idx)
            verify_library_media(dst)
            return
        except Exception as exc:
            public_error=scrub_proxy_error(exc)
        if dst.exists(): dst.unlink(missing_ok=True)
        fid=drive_id_from_url(url)
        if not fid: raise RuntimeError(f'Video {idx}: Drive download failed and file ID could not be parsed')
        match=find_drive_item(fid)
        if not match: raise RuntimeError(f'Video {idx}: Drive file is not accessible through the connected Drive account. Public download: {public_error}')
        path=match.get('Path')
        if not path: raise RuntimeError(f'Video {idx}: Drive file path could not be resolved')
        rclone(['copyto',f'{REMOTE}{path}',str(dst)],timeout=7200)
        if not dst.exists() or dst.stat().st_size<=0: raise RuntimeError(f'Video {idx}: Drive copy failed')
        validate_downloaded_file(dst,idx); verify_library_media(dst); return
    reject_private_host(url); note(max(3,min(38,4+idx*5)),f'Downloading video {idx}'); download_direct_http(url,dst,idx)

def parse_nonneg_int(v, label, allow_blank=False):
    if v is None or (isinstance(v,str) and not v.strip()):
        if allow_blank: return None
        return 0
    if isinstance(v,bool): raise ValueError(f'{label} must be a whole number')
    try:
        n=int(v)
    except Exception:
        raise ValueError(f'{label} must be a whole number')
    if str(v).strip() not in (str(n), f'+{n}') and not isinstance(v,int):
        # Reject floats such as 1.5 rather than silently truncating them.
        try:
            if float(v) != n: raise ValueError
        except Exception:
            raise ValueError(f'{label} must be a whole number')
    if n < 0: raise ValueError(f'{label} cannot be negative')
    return n

def normalize_url_spec(v, idx=0):
    if isinstance(v,str): v={'url':v}
    if not isinstance(v,dict): raise ValueError(f'Video {idx or ""}: invalid source entry'.strip())
    url=validate_url(v.get('url',''))
    enabled=bool(v.get('trim_enabled',False))
    sm=parse_nonneg_int(v.get('start_min'),f'Video {idx} start minutes')
    ss=parse_nonneg_int(v.get('start_sec'),f'Video {idx} start seconds')
    em=parse_nonneg_int(v.get('end_min'),f'Video {idx} end minutes',allow_blank=True)
    es=parse_nonneg_int(v.get('end_sec'),f'Video {idx} end seconds',allow_blank=True)
    if ss>59: raise ValueError(f'Video {idx} start seconds must be 0–59')
    if es is not None and es>59: raise ValueError(f'Video {idx} end seconds must be 0–59')
    if em is None and es is not None: em=0
    if em is not None and es is None: es=0
    if not enabled: sm=0; ss=0; em=None; es=None
    return {'kind':'url','url':url,'trim_enabled':enabled,'start_min':sm,'start_sec':ss,'end_min':em,'end_sec':es}

def get_library_item(item_id):
    with DB_LOCK, db() as c:
        r=c.execute('SELECT * FROM library_items WHERE id=?',(item_id,)).fetchone()
        return dict(r) if r else None

def normalize_video_spec(v, idx=0, owner=None):
    if isinstance(v,dict) and v.get('kind')=='library':
        lid=(v.get('library_id') or '').strip()
        if not lid: raise ValueError(f'Video {idx}: select a Library video')
        item=get_library_item(lid)
        if not item: raise ValueError(f'Video {idx}: Library video no longer exists')
        if owner is not None and (not str(item.get('owner_username') or '').strip() or
               str(item['owner_username']).casefold() != str(owner).casefold()):
            raise ValueError(f'Video {idx}: Library video belongs to another user')
        if item.get('status')!='ready': raise ValueError(f'Video {idx}: Library video is not ready')
        return {'kind':'library','library_id':lid,'name':item.get('name',''),'trim_enabled':False,'start_min':0,'start_sec':0,'end_min':None,'end_sec':None}
    return normalize_url_spec(v,idx)

def probe(path):
    out=run([FFPROBE,'-v','error','-show_streams','-show_format','-of','json',str(path)], timeout=60)
    return json.loads(out)

def media_duration(info):
    try: return float((info.get('format') or {}).get('duration') or 0)
    except Exception: return 0.0

def trim_window(spec, duration, idx):
    spec=normalize_video_spec(spec,idx) if not (isinstance(spec,dict) and 'trim_enabled' in spec and ('url' in spec or spec.get('kind')=='library')) else spec
    if not spec.get('trim_enabled'):
        return 0.0, None, duration
    if duration<=0: raise RuntimeError(f'Video {idx}: source duration could not be determined for trimming')
    start=float(int(spec.get('start_min') or 0)*60 + int(spec.get('start_sec') or 0))
    em=spec.get('end_min'); es=spec.get('end_sec')
    end=None if em is None and es is None else float(int(em or 0)*60 + int(es or 0))
    if start>=duration:
        raise RuntimeError(f'Video {idx}: trim start {start:.0f}s is at or beyond video duration {duration:.1f}s')
    if end is not None:
        if end<=start: raise RuntimeError(f'Video {idx}: trim End must be greater than Start')
        if end>duration+0.35: raise RuntimeError(f'Video {idx}: trim end {end:.0f}s exceeds video duration {duration:.1f}s')
        end=min(end,duration)
    selected=(end-start) if end is not None else (duration-start)
    if selected<=0: raise RuntimeError(f'Video {idx}: selected trim segment is empty')
    return start,end,selected

def normalize(src,dst,w,h,fit,start=0.0,end=None,info=None):
    info=info or probe(src); streams=info.get('streams',[])
    has_video=any(x.get('codec_type')=='video' for x in streams)
    has_audio=any(x.get('codec_type')=='audio' for x in streams)
    if not has_video: raise RuntimeError(f'No video stream in {src.name}')
    if fit=='fit': vf=f'scale={w}:{h}:force_original_aspect_ratio=decrease,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,fps=30,setsar=1'
    else: vf=f'scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},fps=30,setsar=1'
    trim=[]
    if start and start>0: trim += ['-ss',f'{start:.3f}']
    if end is not None: trim += ['-t',f'{max(0.001,end-start):.3f}']
    common=['-c:v','libx264','-preset','veryfast','-crf','20','-pix_fmt','yuv420p','-c:a','aac','-b:a','160k','-ac','2','-ar','48000','-movflags','+faststart']
    if has_audio:
        args=[FFMPEG,'-y','-i',str(src)]+trim+['-vf',vf,'-map','0:v:0','-map','0:a:0']+common+[str(dst)]
    else:
        args=[FFMPEG,'-y','-i',str(src),'-f','lavfi','-i','anullsrc=channel_layout=stereo:sample_rate=48000']+trim+['-vf',vf,'-map','0:v:0','-map','1:a:0','-shortest']+common+[str(dst)]
    run(args, timeout=14400)

def library_cache_path(item_id): return LIBRARY_CACHE / f'{item_id}.mp4'
def library_remote_path(item_id): return f'{LIBRARY_ROOT}/assets/{item_id}.mp4'

def ensure_library_root():
    rclone(['mkdir',f'{REMOTE}{LIBRARY_ROOT}/assets'],timeout=120)

def safe_library_name(v):
    v=re.sub(r'[\x00-\x1f]+',' ',str(v or '')).strip()
    v=re.sub(r'\s+',' ',v)[:120]
    if not v: raise ValueError('Library name is required')
    return v

def library_name_exists(name, exclude_id=''):
    with DB_LOCK, db() as c:
        rows=c.execute('SELECT id,name FROM library_items').fetchall()
    return any(r['id']!=exclude_id and r['name'].lower()==name.lower() for r in rows)

def library_in_use(item_id):
    with DB_LOCK, db() as c:
        rows=c.execute("SELECT videos_json FROM jobs WHERE status NOT IN ('complete','failed','cancelled')").fetchall()
    for r in rows:
        try:
            if any(isinstance(v,dict) and v.get('kind')=='library' and v.get('library_id')==item_id for v in json.loads(r['videos_json'])): return True
        except Exception: pass
    return False

def list_library_items(owner=None):
    with DB_LOCK, db() as c:
        if owner is None:
            rows=c.execute('SELECT * FROM library_items ORDER BY lower(name), created_at').fetchall()
        else:
            rows=c.execute('SELECT * FROM library_items WHERE lower(owner_username)=lower(?) AND owner_username<>\'\' ORDER BY lower(name), created_at',(owner,)).fetchall()
    with DB_LOCK, db() as c:
        active_rows=c.execute("SELECT videos_json FROM jobs WHERE status NOT IN ('complete','failed','cancelled')").fetchall()
    active_ids=set()
    for row in active_rows:
        try:
            active_ids.update(v.get('library_id') for v in json.loads(row['videos_json'])
                              if isinstance(v,dict) and v.get('kind')=='library')
        except (ValueError,TypeError): pass
    out=[]
    for r in rows:
        d=dict(r)
        try: d['trim']=json.loads(d.pop('trim_json') or '{}')
        except Exception: d['trim']={}
        d['in_use']=d['id'] in active_ids; out.append(d)
    return out

def update_library_item(item_id, **kw):
    if not kw: return
    fields=[]; vals=[]
    for k,v in kw.items(): fields.append(f'{k}=?'); vals.append(v)
    fields.append('updated_at=?'); vals.append(utcnow()); vals.append(item_id)
    with DB_LOCK, db() as c: c.execute(f'UPDATE library_items SET {", ".join(fields)} WHERE id=?',vals)

def transcode_library(src,dst,start=0.0,end=None):
    args=[FFMPEG,'-y']
    if start and start>0: args += ['-ss',f'{start:.3f}']
    args += ['-i',str(src)]
    if end is not None: args += ['-t',f'{max(0.001,end-start):.3f}']
    args += ['-map','0:v:0','-map','0:a:0?','-c:v','libx264','-preset','veryfast','-crf','20','-pix_fmt','yuv420p','-c:a','aac','-b:a','160k','-movflags','+faststart',str(dst)]
    run(args,timeout=14400)

def verify_library_media(path):
    if not path.exists() or path.stat().st_size<=0: raise RuntimeError('Library output is empty or missing')
    info=probe(path); streams=info.get('streams',[])
    if not any(x.get('codec_type')=='video' for x in streams): raise RuntimeError('Library output has no video stream')
    dur=media_duration(info)
    if dur<=0: raise RuntimeError('Library output duration could not be verified')
    return dur

def process_library_import(item_id, source_url, trim_spec, replacing=False):
    ws=WORK/f'library_{item_id}_{uuid.uuid4().hex[:6]}'
    ws.mkdir(parents=True,exist_ok=True)
    try:
        def cb(progress,msg): update_library_item(item_id,status='importing',progress=min(70,max(2,progress)),message=msg,error='')
        raw=ws/'source.bin'; source_to_file(source_url,raw,None,1,status_cb=cb)
        info=probe(raw); duration=media_duration(info); start,end,selected=trim_window(trim_spec,duration,1)
        update_library_item(item_id,status='importing',progress=72,message='Saving final Library video')
        final=ws/'library.mp4'; transcode_library(raw,final,start,end)
        final_duration=verify_library_media(final)
        update_library_item(item_id,status='importing',progress=86,message='Uploading Library video to Google Drive')
        ensure_library_root(); remote=library_remote_path(item_id)
        rclone(['copyto',str(final),f'{REMOTE}{remote}'],timeout=14400)
        rawstat=rclone(['lsjson',f'{REMOTE}{remote}','--stat'],timeout=120); obj=json.loads(rawstat or '{}')
        size=int(obj.get('Size') or 0); fid=str(obj.get('ID') or obj.get('Id') or obj.get('id') or '')
        if size!=final.stat().st_size or size<=0 or not fid: raise RuntimeError('Library Drive upload verification failed')
        cache=library_cache_path(item_id); tmp=cache.with_suffix('.tmp.mp4'); shutil.copy2(final,tmp); os.replace(tmp,cache)
        update_library_item(item_id,status='ready',progress=100,message='Ready',error='',duration=float(final_duration),size_bytes=size,remote_path=remote,drive_id=fid,drive_url=f'https://drive.google.com/file/d/{fid}/view',source_url=source_url,trim_json=json.dumps(trim_spec))
    except Exception as e:
        msg=scrub_proxy_error(e)
        if replacing:
            update_library_item(item_id,status='ready',progress=100,message='Replace failed · previous video kept',error=msg)
        else:
            update_library_item(item_id,status='failed',progress=100,message='Import failed',error=msg)
    finally:
        shutil.rmtree(ws,ignore_errors=True)

def create_library_item(payload, replace_id='', owner=''):
    if not str(owner).strip():
        raise ValueError('Authenticated account required')
    replace_id=(replace_id or '').strip(); source=validate_url(payload.get('url',''))
    trim=normalize_url_spec({'url':source,'trim_enabled':bool(payload.get('trim_enabled',False)),'start_min':payload.get('start_min'),'start_sec':payload.get('start_sec'),'end_min':payload.get('end_min'),'end_sec':payload.get('end_sec')},1)
    if replace_id:
        item=get_library_item(replace_id)
        if not item or str(item.get('owner_username') or '').casefold()!=owner.casefold():
            raise KeyError('Library video not found')
        if library_in_use(replace_id): raise ValueError('This Library video is currently used by a waiting or active merge')
        update_library_item(replace_id,status='importing',progress=1,message='Replacing video',error='')
        threading.Thread(target=process_library_import,args=(replace_id,source,trim,True),daemon=True).start(); return replace_id
    name=safe_library_name(payload.get('name',''))
    if library_name_exists(name): raise ValueError('A Library video with this name already exists')
    item_id=uuid.uuid4().hex[:12]; now=utcnow(); remote=library_remote_path(item_id)
    with DB_LOCK, db() as c:
        c.execute('''INSERT INTO library_items(id,name,filename,remote_path,status,progress,message,source_url,trim_json,created_at,updated_at,owner_username) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                  (item_id,name,f'{item_id}.mp4',remote,'importing',1,'Queued for import',source,json.dumps(trim),now,now,owner))
    threading.Thread(target=process_library_import,args=(item_id,source,trim,False),daemon=True).start(); return item_id

def retry_library_item(item_id):
    """User-triggered retry of exactly the saved failed Library source, never a new entry."""
    with LIBRARY_LOCK:
        item=get_library_item(item_id)
        if not item: raise KeyError('Library video not found')
        if item['status']!='failed': raise ValueError('Only failed Library videos can be retried')
        if library_in_use(item_id): raise ValueError('Library video is used by an unfinished merge')
        source=validate_url(item['source_url'])
        try: trim=json.loads(item.get('trim_json') or '{}')
        except (ValueError,TypeError): raise ValueError('Saved trim settings are invalid') from None
        trim=normalize_url_spec({'url':source,**trim},1)
        with DB_LOCK, db() as c:
            changed=c.execute(
                "UPDATE library_items SET status='importing',progress=1,message='Retrying saved source',error='',updated_at=? WHERE id=? AND status='failed'",
                (utcnow(),item_id)).rowcount
        if changed!=1: raise ValueError('Library item was already updated')
        threading.Thread(target=process_library_import,args=(item_id,source,trim,False),daemon=True).start()
        return item_id


def rename_library_item(item_id, name):
    item=get_library_item(item_id)
    if not item: raise KeyError('Library video not found')
    name=safe_library_name(name)
    if library_name_exists(name,item_id): raise ValueError('A Library video with this name already exists')
    update_library_item(item_id,name=name); return name

def delete_library_item(item_id):
    item=get_library_item(item_id)
    if not item: raise KeyError('Library video not found')
    if item.get('status')=='importing': raise ValueError('Wait for the Library import to finish before deleting it')
    if library_in_use(item_id): raise ValueError('This Library video is currently used by a waiting or active merge')
    remote=item.get('remote_path') or library_remote_path(item_id)
    if item.get('drive_id'):
        rclone(['deletefile',f'{REMOTE}{remote}'],timeout=120,check=False)
    library_cache_path(item_id).unlink(missing_ok=True)
    with DB_LOCK, db() as c: c.execute('DELETE FROM library_items WHERE id=?',(item_id,))

def library_to_file(item_id,dst,idx):
    item=get_library_item(item_id)
    if not item or item.get('status')!='ready': raise RuntimeError(f'Video {idx}: Library video is not ready')
    cache=library_cache_path(item_id)
    if not cache.exists() or cache.stat().st_size<=0:
        remote=item.get('remote_path') or library_remote_path(item_id)
        rclone(['copyto',f'{REMOTE}{remote}',str(cache)],timeout=7200)
        verify_library_media(cache)
    shutil.copy2(cache,dst); validate_downloaded_file(dst,idx)

def youtube_title(url):
    try:
        out=run_youtube_download(['--simulate','--quiet','--no-warnings',
            '--print','%(title)s',url],timeout=90)
    except Exception as e:
        raise youtube_failure(e) from None
    lines=[x.strip() for x in out.splitlines() if x.strip()]
    if not lines: raise RuntimeError('Could not determine the YouTube video title')
    return lines[-1]

def source_display_filename(spec):
    if spec.get('kind')!='url': raise ValueError('Auto filename requires Video 2 to be a URL source')
    url=spec.get('url','')
    if looks_youtube(url): label=youtube_title(url)
    elif looks_drive(url):
        fid=drive_id_from_url(url); item=find_drive_item(fid) if fid else None
        label=(item or {}).get('Name') or (item or {}).get('Path') or ''
        if not label: raise RuntimeError('Could not determine the Google Drive filename')
    else:
        label=urllib.parse.unquote(Path(urllib.parse.urlparse(url).path).name)
        if not label: raise RuntimeError('Could not determine the source filename; use Manual filename')
    base=safe_component(label)
    stem=Path(base).stem if Path(base).suffix else base
    stem=safe_component(stem)
    return stem+'.mp4'

def resolve_output_filename(payload, videos):
    mode=(payload.get('filename_mode') or 'auto').strip().lower()
    if mode=='manual':
        raw=(payload.get('filename') or '').strip()
        if not raw: raise ValueError('Output file name is required in Manual mode')
        name=safe_component(raw)
        if not name.lower().endswith('.mp4'): name += '.mp4'
        return name
    if mode!='auto': raise ValueError('Filename mode must be Auto or Manual')
    return source_display_filename(videos[1])

def unique_output_filename(folder,filename):
    rel=safe_folder(folder); stem=Path(filename).stem; ext=Path(filename).suffix or '.mp4'
    for n in range(1,1000):
        cand=filename if n==1 else f'{stem} ({n}){ext}'
        rp=f'{OUTPUT_ROOT}/'+(rel+'/' if rel else '')+cand
        out=run([RCLONE,'--config',RCLONE_CONFIG,'lsjson',f'{REMOTE}{rp}','--stat'],timeout=30,check=False)
        try:
            obj=json.loads(out or '{}')
            if not obj or int(obj.get('Size') or 0)<=0: return cand
        except Exception: return cand
    raise RuntimeError('Could not allocate a unique output filename')

def verify_output(path,w,h,expected_duration=None):
    if not path.exists() or path.stat().st_size<=0: raise RuntimeError('Output file is empty or missing')
    info=probe(path); ss=info.get('streams',[])
    v=next((s for s in ss if s.get('codec_type')=='video'),None)
    a=next((s for s in ss if s.get('codec_type')=='audio'),None)
    if not v: raise RuntimeError('Verified output has no video stream')
    if not a: raise RuntimeError('Verified output has no audio stream')
    if int(v.get('width') or 0)!=w or int(v.get('height') or 0)!=h: raise RuntimeError('Output resolution verification failed')
    dur=media_duration(info)
    if dur<=0: raise RuntimeError('Output duration verification failed')
    if expected_duration and expected_duration>0:
        tolerance=max(2.0, expected_duration*0.015)
        if abs(dur-expected_duration)>tolerance:
            raise RuntimeError(f'Output duration verification failed: expected about {expected_duration:.1f}s, got {dur:.1f}s')
    return dur

def upload_drive(local, folder, filename):
    ensure_output_root()
    rel=safe_folder(folder)
    if rel: create_folder(rel)
    remote_path=f'{OUTPUT_ROOT}/'+(rel+'/' if rel else '')+filename
    rclone(['copyto',str(local),f'{REMOTE}{remote_path}'],timeout=14400)
    raw=rclone(['lsjson',f'{REMOTE}{remote_path}','--stat'],timeout=120)
    obj=json.loads(raw or '{}')
    size=int(obj.get('Size') or 0)
    local_size=local.stat().st_size
    if local_size<=0 or size!=local_size:
        raise RuntimeError(f'Drive upload verification failed: remote bytes {size} != local bytes {local_size}')
    fid=str(obj.get('ID') or obj.get('Id') or obj.get('id') or '')
    if not fid: raise RuntimeError('Drive upload verified but Drive file ID was not returned')
    return fid, f'https://drive.google.com/file/d/{fid}/view'

def get_job(job_id):
    with DB_LOCK, db() as c:
        r=c.execute('SELECT * FROM jobs WHERE id=?',(job_id,)).fetchone()
        return dict(r) if r else None

def update_job(jid,status=None,progress=None,message=None,error=None,drive_url=None,drive_id=None):
    fields=[]; vals=[]
    for k,v in [('status',status),('progress',progress),('message',message),('error',error),('drive_url',drive_url),('drive_id',drive_id)]:
        if v is not None: fields.append(f'{k}=?'); vals.append(v)
    fields.append('updated_at=?'); vals.append(utcnow()); vals.append(jid)
    with DB_LOCK, db() as c: c.execute(f'UPDATE jobs SET {", ".join(fields)} WHERE id=?',vals)


def update_local_path(jid,filename):
    with DB_LOCK, db() as c:
        c.execute('UPDATE jobs SET local_path=?,updated_at=? WHERE id=?',
                  (str(LOCAL_OUTPUT_ROOT / jid / filename), utcnow(), jid))


def retain_verified_output(src, job_id, filename):
    """Verified local output is durable even when both optional destinations are OFF."""
    directory = LOCAL_OUTPUT_ROOT / job_id
    if directory.is_symlink():
        raise RuntimeError('Unsafe local output directory')
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = directory / safe_component(filename)
    if destination.is_symlink():
        raise RuntimeError('Local output collision')
    if destination.exists():
        import hashlib
        def digest(path):
            h = hashlib.sha256()
            with path.open('rb') as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                    h.update(chunk)
            return h.hexdigest()
        if destination.stat().st_size != src.stat().st_size or digest(destination) != digest(src):
            raise RuntimeError('Different verified output already retained for this job')
        return destination
    if shutil.disk_usage(directory).free < MIN_FREE_BYTES:
        raise RuntimeError('Operational free-space reserve is too low')
    temp = directory / ('.' + uuid.uuid4().hex + '.partial')
    try:
        shutil.copyfile(src, temp)
        if temp.stat().st_size != src.stat().st_size or temp.stat().st_size <= 0:
            raise RuntimeError('Local MP4 retention size check failed')
        os.replace(temp, destination)
    finally:
        temp.unlink(missing_ok=True)
    return destination


def deliver_copyright_folder(local, owner, job_id):
    """Keep the verified output; add a separate account-scoped copy for manual uploader selection."""
    if not owner.strip():
        raise RuntimeError('Authenticated owner required for Copyright Uploader handoff')
    if COPYRIGHT_MEDIA_ROOT.is_symlink():
        raise RuntimeError('Unsafe handoff root')
    COPYRIGHT_MEDIA_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder = COPYRIGHT_MEDIA_ROOT / owner_media_key(owner)
    if folder.is_symlink():
        raise RuntimeError('Unsafe account handoff folder')
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    destination = folder / (job_id + '__' + local.name)
    if destination.is_symlink():
        raise RuntimeError('Handoff collision')
    if destination.exists():
        import hashlib
        def digest(path):
            h = hashlib.sha256()
            with path.open('rb') as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                    h.update(chunk)
            return h.hexdigest()
        if destination.stat().st_size != local.stat().st_size or digest(destination) != digest(local):
            raise RuntimeError('Handoff destination contains different media')
        return destination
    if shutil.disk_usage(folder).free < MIN_FREE_BYTES:
        raise RuntimeError('Operational free-space reserve is too low')
    temp = folder / ('.' + uuid.uuid4().hex + '.partial')
    try:
        shutil.copyfile(local, temp)
        if temp.stat().st_size != local.stat().st_size:
            raise RuntimeError('Handoff copy verification failed')
        os.replace(temp, destination)
    finally:
        temp.unlink(missing_ok=True)
    return destination


def update_job_filename(jid,filename):
    with DB_LOCK, db() as c: c.execute('UPDATE jobs SET filename=?, name=?, updated_at=? WHERE id=?',(filename,filename,utcnow(),jid))

def process_job(j):
    jid=j['id']; ws=WORK/jid
    if ws.exists(): shutil.rmtree(ws,ignore_errors=True)
    ws.mkdir(parents=True,exist_ok=True)
    try:
        raw_videos=json.loads(j['videos_json']); videos=[normalize_video_spec(v,i) for i,v in enumerate(raw_videos,1)]
        w,h=RES_MAP[(j['aspect'],j['quality'])]
        update_job(jid,'downloading',2,'Starting sources',error='')
        srcs=[]
        for i,spec in enumerate(videos,1):
            p=ws/f'source_{i:03d}.bin'
            if spec.get('kind')=='library':
                update_job(jid,'downloading',max(3,min(38,4+i*5)),f'Loading Library video {i}')
                library_to_file(spec['library_id'],p,i)
            else:
                source_to_file(spec['url'],p,jid,i)
            srcs.append(p)
        norms=[]; expected_total=0.0; total=max(len(srcs),1)
        for i,(p,spec) in enumerate(zip(srcs,videos),1):
            info=probe(p); duration=media_duration(info); start,end,selected=trim_window(spec,duration,i)
            prog=40+int(15*i/total)
            update_job(jid,'trimming' if spec.get('trim_enabled') else 'preparing',prog,(f'Trimming video {i}/{len(srcs)}' if spec.get('trim_enabled') else f'Inspecting video {i}/{len(srcs)}'))
            n=ws/f'norm_{i:03d}.mp4'; normalize(p,n,w,h,j['fit_mode'],start,end,info); norms.append(n); expected_total+=selected
            update_job(jid,'preparing',55+int(15*i/total),f'Preparing video {i}/{len(srcs)}')
        actual=unique_output_filename(j['folder'],j['filename']) if int(j.get('drive_enabled',1)) else j['filename']
        if actual!=j['filename']:
            update_job_filename(jid,actual); j=dict(j); j['filename']=actual
        update_job(jid,'merging',74,'Merging videos')
        concat=ws/'concat.txt'; concat.write_text(''.join("file '%s'\n"%str(p).replace("'","'\\''") for p in norms))
        out=ws/j['filename']; run([FFMPEG,'-y','-f','concat','-safe','0','-i',str(concat),'-c','copy','-movflags','+faststart',str(out)],timeout=14400)
        update_job(jid,'verifying',86,'Verifying merged output'); dur=verify_output(out,w,h,expected_total)
        retained = retain_verified_output(out, jid, j['filename'])
        update_local_path(jid,j['filename'])
        fid,url = '',''
        # Handoff is independent from Drive; a Drive outage must never prevent
        # a verified MP4 from being made available for manual uploader selection.
        if int(j.get('copyright_enabled',0)):
            update_job(jid,'uploading',91,'Adding to Copyright Uploader folder')
            deliver_copyright_folder(retained,j.get('owner_username',''),jid)
        if int(j.get('drive_enabled',1)):
            update_job(jid,'uploading',96,'Uploading to Google Drive')
            fid,url=upload_drive(retained,j['folder'],j['filename'])
        update_job(jid,'complete',100,f'Complete · {dur:.1f}s',drive_id=fid,drive_url=url,error='')
        shutil.rmtree(ws,ignore_errors=True)
    except Exception as e:
        update_job(jid,'failed',100,'Failed',error=scrub_proxy_error(e))

def next_waiting():
    # Atomically claim a waiting job before the worker leaves the DB lock.
    # Otherwise a user can cancel/delete a row after SELECT but before work starts.
    with DB_LOCK, db() as c:
        r=c.execute("""SELECT * FROM jobs j WHERE j.status='waiting'
                         AND NOT EXISTS (SELECT 1 FROM owner_queue_state p
                                         WHERE p.owner_username=j.owner_username AND p.paused=1)
                         ORDER BY j.order_idx,j.created_at LIMIT 1""").fetchone()
        if not r: return None
        c.execute("UPDATE jobs SET status='processing', message='Starting job', updated_at=? WHERE id=? AND status='waiting'",
                  (utcnow(), r['id']))
        claimed=dict(r)
        claimed['status']='processing'
        return claimed

def worker():
    while not STOP_EVENT.is_set():
        j=next_waiting()
        if not j: WORKER_WAKE.wait(2); WORKER_WAKE.clear(); continue
        process_job(j)

def list_jobs(owner=None, admin=False):
    with DB_LOCK, db() as c:
        rows=c.execute('SELECT * FROM jobs ORDER BY order_idx, created_at').fetchall()
    out=[]
    for r in rows:
        d=dict(r)
        if not admin and (not owner or not d.get('owner_username') or
                          str(d['owner_username']).casefold()!=str(owner).casefold()):
            continue
        d['videos']=json.loads(d.pop('videos_json')); out.append(d)
    return out

def add_job(payload, owner=''):
    if not str(owner).strip(): raise ValueError('Authenticated account required')
    videos=payload.get('videos') or []
    if not isinstance(videos,list) or len(videos)<2 or len(videos)>MAX_VIDEOS: raise ValueError(f'Use 2 to {MAX_VIDEOS} videos')
    videos=[normalize_video_spec(x,i,owner=owner) for i,x in enumerate(videos,1)]
    if videos[0].get('kind')!='library': raise ValueError('Video 1 must be selected from Library')
    if any(v.get('kind')!='url' for v in videos[1:]): raise ValueError('Video 2 and later videos must use URLs')
    aspect=payload.get('aspect','9:16'); quality=payload.get('quality','1080p')
    if (aspect,quality) not in RES_MAP: raise ValueError('Invalid resolution selection')
    fit='fit' if payload.get('fit_mode')=='fit' else 'crop'; folder=resolve_owner_folder(owner,payload.get('folder',''))
    filename=resolve_output_filename(payload,videos); name=filename; jid=uuid.uuid4().hex[:12]
    drive_enabled = payload.get('upload_to_drive', True) is True
    copyright_enabled = payload.get('add_to_copyright_folder', False) is True
    if copyright_enabled and not owner.strip(): raise ValueError('Login required for Copyright Uploader folder delivery')
    with DB_LOCK, db() as c:
        maxord=c.execute('SELECT COALESCE(MAX(order_idx),0) m FROM jobs').fetchone()['m']; now=utcnow()
        c.execute('''INSERT INTO jobs(id,name,videos_json,aspect,quality,fit_mode,folder,filename,status,order_idx,created_at,updated_at,
                  drive_enabled,copyright_enabled,owner_username) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
                  (jid,name,json.dumps(videos),aspect,quality,fit,folder,filename,'waiting',int(maxord)+10,now,now,
                   int(drive_enabled),int(copyright_enabled),owner))
    WORKER_WAKE.set(); return jid,filename

def delete_job(jid, owner='', admin=False):
    # Status verification and deletion share one lock with the worker claim.
    # Deletion must never remove a job after it was claimed for processing.
    with DB_LOCK, db() as c:
        j=c.execute('SELECT id,status,owner_username FROM jobs WHERE id=?',(jid,)).fetchone()
        if not j: raise KeyError('Job not found')
        if not admin and (not owner or not j['owner_username'] or str(j['owner_username']).casefold()!=str(owner).casefold()):
            raise ValueError('This job does not belong to the current account')
        if j['status'] not in ('waiting','complete','failed','cancelled'):
            raise ValueError('Active job cannot be deleted')
        c.execute('DELETE FROM jobs WHERE id=? AND status=?',(jid,j['status']))
    if j['status']!='complete': shutil.rmtree(WORK/jid, ignore_errors=True)
    # Preserve completed media even when a history row is removed; cleanup is a separate explicit action.


def cancel_job(jid, owner='', admin=False):
    """Cancel only a still-waiting job; preserve its history and all source/output files."""
    with DB_LOCK, db() as c:
        j=c.execute('SELECT id,status,owner_username FROM jobs WHERE id=?',(jid,)).fetchone()
        if not j: raise KeyError('Job not found')
        if not admin and (not owner or not j['owner_username'] or str(j['owner_username']).casefold()!=str(owner).casefold()):
            raise ValueError('This job does not belong to the current account')
        if j['status']!='waiting': raise ValueError('Only waiting jobs can be cancelled')
        c.execute("UPDATE jobs SET status='cancelled', progress=0, message='Cancelled before processing', updated_at=? WHERE id=? AND status='waiting'",
                  (utcnow(),jid))


def retry_job(jid, owner='', admin=False):
    """Idempotently retry a failed job as a new queued ID; never overwrite the failed record.

    A distinct work directory preserves the original verified output if the previous
    attempt failed after local retention or an optional destination delivery.
    """
    with DB_LOCK, db() as c:
        j=c.execute('SELECT * FROM jobs WHERE id=?',(jid,)).fetchone()
        if not j: raise KeyError('Job not found')
        if not admin and (not owner or not j['owner_username'] or str(j['owner_username']).casefold()!=str(owner).casefold()):
            raise ValueError('This job does not belong to the current account')
        if j['status']!='failed': raise ValueError('Only failed jobs can be retried')
        previous=c.execute('SELECT id,filename FROM jobs WHERE retry_of=? ORDER BY created_at LIMIT 1',(jid,)).fetchone()
        if previous:
            return previous['id'],previous['filename']
        new_id=uuid.uuid4().hex[:12]
        now=utcnow()
        maxord=c.execute('SELECT COALESCE(MAX(order_idx),0) AS maxord FROM jobs').fetchone()['maxord']
        c.execute("""INSERT INTO jobs(id,name,videos_json,aspect,quality,fit_mode,folder,filename,
                  status,progress,message,error,drive_url,drive_id,order_idx,
                  drive_enabled,copyright_enabled,owner_username,local_path,retry_of,created_at,updated_at)
                  VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (new_id,j['name'],j['videos_json'],j['aspect'],j['quality'],j['fit_mode'],
                   j['folder'],j['filename'],'waiting',0,'Retry queued','', '', '',int(maxord)+10,
                   j['drive_enabled'],j['copyright_enabled'],j['owner_username'],'',jid,now,now))
    WORKER_WAKE.set()
    return new_id,j['filename']


def move_job(jid,direction,owner='',admin=False):
    with DB_LOCK, db() as c:
        rows=(c.execute("SELECT id,order_idx FROM jobs WHERE status='waiting' ORDER BY order_idx,created_at").fetchall() if admin else c.execute("SELECT id,order_idx FROM jobs WHERE status='waiting' AND owner_username<>'' AND lower(owner_username)=lower(?) ORDER BY order_idx,created_at",(owner,)).fetchall())
        ids=[r['id'] for r in rows]
        if jid not in ids: raise ValueError('Only waiting jobs can be reordered')
        i=ids.index(jid); j=i+(-1 if direction=='up' else 1)
        if j<0 or j>=len(ids): return
        a,b=rows[i],rows[j]
        c.execute('UPDATE jobs SET order_idx=? WHERE id=?',(b['order_idx'],a['id']))
        c.execute('UPDATE jobs SET order_idx=? WHERE id=?',(a['order_idx'],b['id']))

def json_bytes(x): return json.dumps(x,separators=(',',':')).encode()

class Handler(BaseHTTPRequestHandler):
    server_version='VideoMerger/1.0'
    def log_message(self,fmt,*args): print('%s - %s'%(self.address_string(),fmt%args),flush=True)
    def send_bytes(self,code,body,ctype='application/json; charset=utf-8'):
        self.send_response(code); self.send_header('Content-Type',ctype); self.send_header('Content-Length',str(len(body))); self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(body)
    def send_json(self,code,obj): self.send_bytes(code,json_bytes(obj))
    def read_json(self):
        n=int(self.headers.get('Content-Length','0') or 0)
        if n>2_000_000: raise ValueError('Request too large')
        return json.loads(self.rfile.read(n) or b'{}')
    def route(self): return urllib.parse.urlparse(self.path).path
    def principal(self):
        owner=(self.headers.get('X-Auth-User') or '').strip()
        if not owner:
            raise ValueError('Authenticated Scene account required')
        return owner
    def is_admin(self):
        return self.headers.get('X-Is-Admin')=='1'
    def require_library_owner(self, item_id):
        item=get_library_item(item_id)
        if not item or not item.get('owner_username') or item['owner_username'].casefold()!=self.principal().casefold():
            raise ValueError('Library item is not owned by current account')
        return item
    def do_GET(self):
        p=self.route()
        try:
            if p in (PREFIX, PREFIX+'/'):
                # Scene SSO Nginx replaces trusted identity headers after login.
                user=(self.headers.get('X-Auth-User') or '').strip()[:120]
                is_admin=bool(user) and self.headers.get('X-Is-Admin')=='1'
                page=(Path(__file__).resolve().parent/'templates/index.html').read_text(encoding='utf-8')
                if user:
                    account=('<div class="sidebar-account" data-vps1-server-identity="1">'
                             '<div class="sidebar-account-name">'+html.escape(user,quote=True)+'</div>'
                             '<form action="/logout" method="post">'
                             '<button type="submit">Log out</button></form></div>')
                else:
                    account=('<div class="sidebar-account" data-vps1-server-identity="0">'
                             '<div class="sidebar-account-name">Account verification needed</div>'
                             '<a class="vps1-session-login" href="/login">Sign in</a></div>')
                anchor='<!-- V1_SERVER_ACCOUNT_FALLBACK -->'
                if page.count(anchor)!=1: raise RuntimeError('Video Merger sidebar anchor changed')
                page=page.replace(anchor,account,1)
                if not is_admin:
                    page=page.replace('</head>',
                        '<style>#vm-tab-proxy,#tab-proxy{display:none!important}</style></head>',1)
                if is_admin:
                    marker='<!-- Admin appears only after verified Scene session role -->'
                    if page.count(marker)!=1: raise RuntimeError('Video Merger Admin anchor changed')
                    page=page.replace(marker,
                        '<a class="vps1-server-admin" href="/admin">Admin</a>\\n'+marker,1)
                return self.send_bytes(200,page.encode('utf-8'),'text/html; charset=utf-8')
            if p==PREFIX+'/health': return self.send_json(200,{'ok':True,'app':APP_NAME,'release':RELEASE_ID,'paused':setting('paused','0')=='1'})
            m=re.fullmatch(re.escape(PREFIX)+r'/api/jobs/([A-Za-z0-9]+)/(download|view)',p)
            if m:
                job=get_job(m.group(1))
                owner=(self.headers.get('X-Auth-User') or '').strip()
                if not job or not owner or str(job.get('owner_username') or '').casefold()!=owner.casefold():
                    return self.send_json(404,{'ok':False,'error':'Output not found'})
                path = LOCAL_OUTPUT_ROOT / job['id'] / job['filename']
                if (not path.is_file() or path.is_symlink() or
                        path.resolve().parent != (LOCAL_OUTPUT_ROOT / job['id']).resolve() or
                        str(path) != str(job.get('local_path') or '')):
                    return self.send_json(404,{'ok':False,'error':'Verified output unavailable'})
                self.send_response(200)
                self.send_header('Content-Type','video/mp4')
                self.send_header('Content-Disposition',("inline" if m.group(2)=="view" else "attachment")+"; filename*=UTF-8''"+urllib.parse.quote(job['filename']))
                self.send_header('Content-Length',str(path.stat().st_size))
                self.send_header('X-Content-Type-Options','nosniff')
                self.end_headers()
                with path.open('rb') as handle:
                    while True:
                        chunk=handle.read(1024 * 1024)
                        if not chunk: break
                        self.wfile.write(chunk)
                return
            if p==PREFIX+'/api/state':
                owner=self.principal()
                return self.send_json(200,{'ok':True,'jobs':list_jobs(owner),
                    'folders':list_owner_folders(owner),'paused':owner_queue_paused(owner),
                    'output_root':OUTPUT_ROOT,'library':list_library_items(owner),
                    'proxy':public_proxy_state() if self.is_admin() else {'enabled':False,'profiles':[]}})
            return self.send_json(404,{'ok':False,'error':'Not found'})
        except Exception as e: self.send_json(500,{'ok':False,'error':str(e)})
    def do_POST(self):
        p=self.route()
        try:
            # Every mutation and source lookup must have verified Scene identity.
            # /health remains the only intentionally public operational probe.
            self.principal()
            payload=self.read_json()
            if p==PREFIX+'/api/jobs':
                jid,filename=add_job(payload,self.principal()); return self.send_json(201,{'ok':True,'id':jid,'filename':filename})
            if p==PREFIX+'/api/source-name':
                spec=normalize_url_spec({'url':payload.get('url','')},2); return self.send_json(200,{'ok':True,'filename':source_display_filename(spec)})
            if p==PREFIX+'/api/library': return self.send_json(202,{'ok':True,'id':create_library_item(payload,owner=self.principal())})
            m=re.fullmatch(re.escape(PREFIX)+r'/api/library/([A-Za-z0-9]+)/retry',p)
            if m:
                self.require_library_owner(m.group(1))
                return self.send_json(202,{'ok':True,'id':retry_library_item(m.group(1))})
            m=re.fullmatch(re.escape(PREFIX)+r'/api/library/([A-Za-z0-9]+)/rename',p)
            if m:
                self.require_library_owner(m.group(1))
                return self.send_json(200,{'ok':True,'name':rename_library_item(m.group(1),payload.get('name',''))})
            m=re.fullmatch(re.escape(PREFIX)+r'/api/library/([A-Za-z0-9]+)/replace',p)
            if m:
                self.require_library_owner(m.group(1))
                return self.send_json(202,{'ok':True,'id':create_library_item(payload,m.group(1),owner=self.principal())})
            m=re.fullmatch(re.escape(PREFIX)+r'/api/library/([A-Za-z0-9]+)/delete',p)
            if m:
                self.require_library_owner(m.group(1))
                delete_library_item(m.group(1)); return self.send_json(200,{'ok':True})
            if p.startswith(PREFIX+'/api/proxy/') and not self.is_admin():
                raise ValueError('Shared global controls are administrator-only until partitioned')
            if p==PREFIX+'/api/proxy/save': return self.send_json(200,{'ok':True,'id':save_proxy_profile(payload)})
            if p==PREFIX+'/api/proxy/delete': delete_proxy_profile((payload.get('id') or '').strip()); return self.send_json(200,{'ok':True})
            if p==PREFIX+'/api/proxy/active': set_active_proxy((payload.get('id') or '').strip()); return self.send_json(200,{'ok':True})
            if p==PREFIX+'/api/proxy/toggle': set_proxy_enabled(bool(payload.get('enabled'))); return self.send_json(200,{'ok':True})
            if p==PREFIX+'/api/proxy/test': return self.send_json(200,{'ok':True,**test_proxy_profile((payload.get('id') or '').strip())})
            m=re.fullmatch(re.escape(PREFIX)+r'/api/jobs/([A-Za-z0-9]+)/cancel',p)
            if m: cancel_job(m.group(1),self.principal(),admin=False); return self.send_json(200,{'ok':True})
            m=re.fullmatch(re.escape(PREFIX)+r'/api/jobs/([A-Za-z0-9]+)/retry',p)
            if m:
                retry_id,filename=retry_job(m.group(1),self.principal(),admin=False)
                return self.send_json(202,{'ok':True,'id':retry_id,'filename':filename})
            m=re.fullmatch(re.escape(PREFIX)+r'/api/jobs/([A-Za-z0-9]+)/delete',p)
            if m: delete_job(m.group(1),self.principal(),admin=False); return self.send_json(200,{'ok':True})
            m=re.fullmatch(re.escape(PREFIX)+r'/api/jobs/([A-Za-z0-9]+)/move',p)
            if m: move_job(m.group(1),payload.get('direction'),self.principal(),admin=False); return self.send_json(200,{'ok':True})
            if p==PREFIX+'/api/queue/pause':
                set_owner_queue_paused(self.principal(),True); return self.send_json(200,{'ok':True})
            if p==PREFIX+'/api/queue/resume':
                set_owner_queue_paused(self.principal(),False); return self.send_json(200,{'ok':True})
            if p==PREFIX+'/api/folders':
                return self.send_json(201,{'ok':True,'folder':create_owner_folder(self.principal(),payload.get('name',''))})
            if p==PREFIX+'/api/folders/delete':
                return self.send_json(200,{'ok':True,'folder':delete_owner_folder(self.principal(),payload.get('name',''))})
            return self.send_json(404,{'ok':False,'error':'Not found'})
        except (ValueError,KeyError) as e: self.send_json(400,{'ok':False,'error':str(e)})
        except Exception as e: self.send_json(500,{'ok':False,'error':str(e)})

if __name__=='__main__':
    init_db()
    # Never block the web listener on a network/Drive check. The worker and
    # folder actions already retry Drive operations when actually needed.
    def initial_drive_probe():
        try:
            ensure_output_root(); ensure_library_root()
            print('Initial Drive roots check: OK', flush=True)
        except Exception as e:
            print(f'Initial Drive root check failed (will retry on use): {e}', flush=True)
    if os.environ.get('FRESH_WEBSITE_SMOKE_TEST') != '1':
        threading.Thread(target=initial_drive_probe,daemon=True).start()
        t=threading.Thread(target=worker,daemon=True); t.start()
    port=int(os.environ.get('VIDEO_MERGER_PORT','8129'))
    httpd=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    print(f'Video Merger listening on 127.0.0.1:{port}',flush=True)
    try: httpd.serve_forever()
    finally: STOP_EVENT.set(); WORKER_WAKE.set()
