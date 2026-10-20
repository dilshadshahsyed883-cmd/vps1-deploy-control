"""Auto Stream V2: independently authorized YouTube channels, read-only Sheets, live API.

Never accesses Auto Uploader OAuth/DB/queue. Network operations happen only on
explicit authenticated API requests, not during app import or GET /health.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
from urllib import error, parse, request

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import HTMLResponse

from auto_stream_engine import StreamEngine, service, scheduled_utc

YOUTUBE_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"
SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets.readonly"
OAUTH_BASE = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
YT_BASE = "https://www.googleapis.com/youtube/v3"
SHEETS_BASE = "https://sheets.googleapis.com/v4/spreadsheets"
CHANNEL_RE = re.compile(r"^UC[A-Za-z0-9_-]{22}$")
SHEET_RE = re.compile(r"^[A-Za-z0-9_-]{15,128}$")
SAFE_RANGE = re.compile(r"^[A-Za-z0-9_ '\-!:$]{1,120}$")
router = APIRouter(prefix="/engine", tags=["Auto Stream Channels and Sheets"])


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _configured():
    return all(os.environ.get(key) for key in (
        "AUTO_STREAM_GOOGLE_CLIENT_ID", "AUTO_STREAM_GOOGLE_CLIENT_SECRET",
        "AUTO_STREAM_GOOGLE_REDIRECT_URI", "AUTO_STREAM_FERNET_KEY"))


def _configuration():
    # Browser-saved encrypted OAuth web client overrides legacy environment;
    # no restart or VPS CLI is required after an authorized admin Save.
    from auto_stream_engine import ENGINE_ROOT
    # Avoid creating storage on read-only/CI requests. Only load a browser
    # OAuth client when one was actually saved for this independent engine.
    if (ENGINE_ROOT / "oauth-client.enc").exists():
        from auto_stream_client_config import store as _client_store
        client = _client_store().read_client()
        if client is not None:
            return client
    if not _configured():
        raise ValueError("Auto Stream Google OAuth is not configured on the VPS")
    redirect = os.environ["AUTO_STREAM_GOOGLE_REDIRECT_URI"]
    if (not redirect.startswith("https://app.scenebuilder.online/short-stream/engine/oauth/callback")
            or redirect != "https://app.scenebuilder.online/short-stream/engine/oauth/callback"):
        raise ValueError("OAuth redirect URI must match the dedicated HTTPS callback")
    return (os.environ["AUTO_STREAM_GOOGLE_CLIENT_ID"],
            os.environ["AUTO_STREAM_GOOGLE_CLIENT_SECRET"], redirect)


def _cipher():
    try:
        from cryptography.fernet import Fernet
        return Fernet(os.environ["AUTO_STREAM_FERNET_KEY"].encode("ascii"))
    except (KeyError, ValueError, ImportError) as exc:
        raise ValueError("Auto Stream token encryption is not configured") from exc


def _http_json(url: str, *, access_token: str | None = None,
               form: dict | None = None, method: str = "GET",
               payload: dict | None = None, timeout: int = 15):
    """Allowlisted Google endpoints; do not reflect tokens or bodies in errors."""
    origin_ok = url.startswith((TOKEN_URL, YT_BASE + "/", SHEETS_BASE + "/"))
    if not origin_ok:
        raise ValueError("Google API endpoint is outside allowlist")
    headers = {"Accept": "application/json"}
    body = None
    if form is not None:
        body = parse.urlencode(form).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if payload is not None:
        body = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    if access_token:
        headers["Authorization"] = "Bearer " + access_token
    req = request.Request(url, data=body, headers=headers, method=method)
    try:
        with request.urlopen(req, timeout=timeout) as response:
            data = response.read(1024 * 1024)
            return json.loads(data)
    except error.HTTPError as exc:
        # Don't allow potentially credential-bearing Google response bodies
        # to surface in frontend messages, traces, GitHub CI or VPS logs.
        hints={'quotaExceeded':'Google API quota exhausted; retry after quota resets',
               'dailyLimitExceeded':'Google API daily quota exhausted',
               'liveStreamingNotEnabled':'Enable YouTube Live on the selected channel before retrying',
               'livePermissionBlocked':'YouTube Live permissions are blocked for this channel',
               'userRequestsExceedRateLimit':'Google API rate limit reached; wait before retrying',
               'invalid_grant':'Google authorization expired or was revoked; reconnect this channel'}
        try:
            detail=json.loads(exc.read(16384));error_doc=detail.get('error',{})
            reason=error_doc if isinstance(error_doc,str) else next((v.get('reason') for v in error_doc.get('errors',[]) if v.get('reason') in hints),'')
        except (ValueError,AttributeError,TypeError):reason=''
        raise ValueError(hints.get(reason,"Google API request refused (HTTP " + str(exc.code) + "); check API access, channel permissions and quota")) from None
    except (error.URLError, TimeoutError, json.JSONDecodeError):
        raise ValueError("Google API request unavailable or returned invalid data") from None


class AutoStreamAccounts:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else service().root
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.db = self.root / "integrations.sqlite3"
        with self.connect() as con:
            con.execute("""CREATE TABLE IF NOT EXISTS oauth_state (
                digest TEXT PRIMARY KEY, verifier TEXT NOT NULL,
                expires TEXT NOT NULL, requested_scopes TEXT NOT NULL DEFAULT '')""")
            try:
                con.execute("ALTER TABLE oauth_state ADD COLUMN requested_scopes TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            con.execute("""CREATE TABLE IF NOT EXISTS channels (
                id TEXT PRIMARY KEY, title TEXT NOT NULL,
                authorized INTEGER NOT NULL DEFAULT 0,
                token BLOB, scopes TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL)""")
            con.execute("""CREATE TABLE IF NOT EXISTS sheet_sources (
                id TEXT PRIMARY KEY, channel_id TEXT NOT NULL,
                spreadsheet_id TEXT NOT NULL, a1_range TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (channel_id) REFERENCES channels(id))""")
            con.execute("""CREATE TABLE IF NOT EXISTS broadcasts (
                job_id TEXT PRIMARY KEY, channel_id TEXT NOT NULL,
                broadcast_id TEXT, live_stream_id TEXT,
                status TEXT NOT NULL, updated_at TEXT NOT NULL)""")
            for table, column, definition in (
                ("oauth_state", "expected_channel", "TEXT NOT NULL DEFAULT ''"),
                ("sheet_sources", "name", "TEXT NOT NULL DEFAULT ''"),
                ("sheet_sources", "sheet_url", "TEXT NOT NULL DEFAULT ''"),
                ("sheet_sources", "header_map", "TEXT NOT NULL DEFAULT '{}'"),
                ("sheet_sources", "sync_status", "TEXT NOT NULL DEFAULT 'Never synced'"),
                ("sheet_sources", "last_sync", "TEXT NOT NULL DEFAULT ''"),
                ("channels", "active", "INTEGER NOT NULL DEFAULT 1"),
                ("channels", "group_name", "TEXT NOT NULL DEFAULT ''"),
                ("channels", "account_label", "TEXT NOT NULL DEFAULT ''"),
                ("channels", "brand_account", "TEXT NOT NULL DEFAULT 'unknown'"),
                ("channels", "sheet_id", "TEXT NOT NULL DEFAULT ''"),
                ("channels", "connection_state", "TEXT NOT NULL DEFAULT 'UNTESTED'"),
                ("channels", "last_test", "TEXT NOT NULL DEFAULT ''")):
                if column not in {r[1] for r in con.execute("PRAGMA table_info(" + table + ")")}:
                    con.execute("ALTER TABLE " + table + " ADD COLUMN " + column + " " + definition)
        os.chmod(self.db, 0o600)

    def connect(self):
        con = sqlite3.connect(str(self.db), timeout=10)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA busy_timeout=10000")
        con.execute("PRAGMA foreign_keys=ON")
        return con

    def channel_catalog(self):
        with self.connect() as con:
            rows = [dict(r) for r in con.execute("SELECT id,title,authorized,updated_at,active,group_name,account_label,brand_account,sheet_id,connection_state,last_test FROM channels ORDER BY title")]
            for row in rows:
                row['authorized']=bool(row['authorized']);row['active']=bool(row['active'])
                row['token_status']='STORED' if row['authorized'] else 'MISSING'
            return rows

    def import_metadata(self, channel_id, title):
        if not CHANNEL_RE.fullmatch(channel_id):
            raise ValueError("YouTube channel ID is invalid")
        title = str(title).strip()
        if not title or len(title) > 150:
            raise ValueError("Channel title required (maximum 150 characters)")
        with self.connect() as con:
            con.execute("""INSERT INTO channels (id,title,updated_at)
                VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET
                title=excluded.title,updated_at=excluded.updated_at""",
                (channel_id, title, now()))
        return {"id": channel_id, "title": title, "authorized": False if
                not any(c["id"] == channel_id and c["authorized"] for c in
                        self.channel_catalog()) else True}

    def begin_oauth(self, include_sheets: bool = False, expected_channel: str = ""):
        if expected_channel and not CHANNEL_RE.fullmatch(expected_channel):
            raise ValueError("Invalid expected YouTube channel ID")
        client, _, redirect = _configuration()
        state = secrets.token_urlsafe(36)
        verifier = secrets.token_urlsafe(56)
        digest = hashlib.sha256(state.encode()).hexdigest()
        requested_scopes = YOUTUBE_SCOPE + ((" " + SHEETS_SCOPE) if include_sheets else "")
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        expires = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        with self.connect() as con:
            con.execute("DELETE FROM oauth_state WHERE expires<?", (now(),))
            con.execute("INSERT INTO oauth_state(digest,verifier,expires,requested_scopes) VALUES (?,?,?,?)",
                        (digest, verifier, expires, requested_scopes))
            con.execute("UPDATE oauth_state SET expected_channel=? WHERE digest=?", (expected_channel, digest))
        params = {
            "response_type": "code", "client_id": client, "redirect_uri": redirect,
            "scope": requested_scopes,
            "access_type": "offline", "prompt": "consent select_account",
            "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
            "include_granted_scopes": "true",
        }
        return {"authorization_url": OAUTH_BASE + "?" + parse.urlencode(params),
                "expires_seconds": 600}

    def finish_oauth(self, state, code):
        if not state or not code or len(state) > 256 or len(code) > 4096:
            raise ValueError("OAuth callback is incomplete")
        digest = hashlib.sha256(state.encode()).hexdigest()
        with self.connect() as con:
            row = con.execute("SELECT verifier,expires,requested_scopes,expected_channel FROM oauth_state WHERE digest=?",
                              (digest,)).fetchone()
            con.execute("DELETE FROM oauth_state WHERE digest=?", (digest,))
        if not row or row["expires"] < now():
            raise ValueError("OAuth state expired or already consumed")
        client, secret, redirect = _configuration()
        token = _http_json(TOKEN_URL, form={
            "code": code, "client_id": client, "client_secret": secret,
            "redirect_uri": redirect, "grant_type": "authorization_code",
            "code_verifier": row["verifier"]}, method="POST")
        grant = set(str(token.get("scope", "")).split())
        requested = set(str(row["requested_scopes"] or YOUTUBE_SCOPE).split())
        if YOUTUBE_SCOPE not in grant:
            raise ValueError("YouTube Live access must be granted")
        if SHEETS_SCOPE in requested and SHEETS_SCOPE not in grant:
            raise ValueError("Sheets read-only access was requested but not granted")
        if not token.get("refresh_token"):
            raise ValueError("Google did not supply offline authorization")
        access = token.get("access_token")
        if not access:
            raise ValueError("Google did not supply access authorization")
        verified = _http_json(YT_BASE + "/channels?" +
            parse.urlencode({"part": "snippet", "mine": "true", "maxResults": "10"}),
            access_token=access).get("items", [])
        if len(verified) != 1:
            raise ValueError("Google authorization must resolve to one chosen YouTube channel")
        channel = verified[0]
        cid = channel.get("id", "")
        if not CHANNEL_RE.fullmatch(cid):
            raise ValueError("Unable to verify authorized YouTube channel")
        if row["expected_channel"] and cid != row["expected_channel"]:
            raise ValueError("Authorized YouTube channel does not match the selected channel; select the correct Brand Account")
        title = (channel.get("snippet") or {}).get("title") or cid
        protected = _cipher().encrypt(token["refresh_token"].encode())
        with self.connect() as con:
            con.execute("""INSERT INTO channels(id,title,authorized,token,scopes,updated_at)
                 VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                 title=excluded.title,authorized=1,token=excluded.token,
                 scopes=excluded.scopes,updated_at=excluded.updated_at""",
                (cid, str(title)[:150], 1, protected,
                 " ".join(sorted(grant)), now()))
        return {"id": cid, "title": str(title)[:150], "authorized": True}

    def disconnect(self, channel):
        if not CHANNEL_RE.fullmatch(channel):
            raise ValueError("Invalid channel")
        with self.connect() as con:
            res = con.execute("""UPDATE channels SET authorized=0,token=NULL,
                scopes='',updated_at=? WHERE id=?""", (now(), channel))
        if not res.rowcount:
            raise KeyError("Channel not found")
        return {"id": channel, "authorized": False}

    def _access(self, channel_id, required_scope):
        with self.connect() as con:
            row = con.execute("SELECT token,scopes FROM channels WHERE id=? AND authorized=1 AND active=1",
                              (channel_id,)).fetchone()
        if not row or not row["token"] or required_scope not in row["scopes"].split():
            raise ValueError("Channel needs a separate Auto Stream OAuth connection")
        refresh = _cipher().decrypt(row["token"]).decode()
        client, secret, _ = _configuration()
        token = _http_json(TOKEN_URL, form={
            "client_id": client, "client_secret": secret,
            "refresh_token": refresh, "grant_type": "refresh_token"}, method="POST")
        value = token.get("access_token")
        if not value:
            raise ValueError("Channel authorization has expired; reconnect required")
        return value

    def manage_channels(self, ids, action, values=None):
        if not isinstance(ids,list) or not 1<=len(ids)<=100 or any(not CHANNEL_RE.fullmatch(str(i)) for i in ids):
            raise ValueError('Select 1–100 valid channels')
        values=values or {}
        if action not in ('activate','deactivate','group','edit','remove'):
            raise ValueError('Unsupported channel operation')
        with self.connect() as con:
            con.execute('BEGIN IMMEDIATE')
            for cid in ids:
                row=con.execute('SELECT * FROM channels WHERE id=?',(cid,)).fetchone()
                if not row:raise KeyError('Channel not found')
                jobs=self.root/'stream-jobs.sqlite3'
                if action in ('deactivate','remove') and jobs.exists():
                    with sqlite3.connect(f'file:{jobs}?mode=ro',uri=True) as jobcon:
                        if jobcon.execute("SELECT 1 FROM jobs WHERE channel_id=? AND status IN ('STARTING','INGESTING','STOPPING','SCHEDULED','READY') LIMIT 1",(cid,)).fetchone():
                            raise ValueError('Channel has active/prepared jobs; cancel or finish them first')
                if action=='remove':
                    if row['authorized']:raise ValueError('Remove the local token before removing this inventory row')
                    if con.execute('SELECT 1 FROM sheet_sources WHERE channel_id=?',(cid,)).fetchone():raise ValueError('Disconnect associated sheet sources first')
                    con.execute('DELETE FROM channels WHERE id=?',(cid,));continue
                updates={}
                if action in ('activate','deactivate'):updates['active']=int(action=='activate')
                elif action=='group':updates['group_name']=str(values.get('group_name',''))[:100]
                else:
                    for key in ('title','group_name','account_label','brand_account'):
                        if key in values:updates[key]=str(values[key]).strip()[:150]
                    if not updates:raise ValueError('No editable channel fields provided')
                    if 'title' in updates and not updates['title']:raise ValueError('Channel name is required')
                    if updates.get('brand_account','unknown') not in ('yes','no','unknown'):raise ValueError('Brand Account must be yes, no or unknown')
                updates['updated_at']=now()
                con.execute('UPDATE channels SET '+','.join(k+'=?' for k in updates)+' WHERE id=?',(*updates.values(),cid))
        return {'updated':len(ids),'action':action}

    def test_channel(self,cid):
        try:
            access=self._access(cid,YOUTUBE_SCOPE)
            result=_http_json(YT_BASE+'/channels?'+parse.urlencode({'part':'id,snippet','mine':'true','maxResults':'10'}),access_token=access)
            items=result.get('items',[])
            if len(items)!=1 or items[0].get('id')!=cid:raise ValueError('Connected Google/Brand Account does not match this UC Channel ID; reconnect')
            with self.connect() as con:con.execute("UPDATE channels SET connection_state='HEALTHY',last_test=?,updated_at=? WHERE id=?",(now(),now(),cid))
            return {'channel_id':cid,'ok':True,'connection_state':'HEALTHY','checked_at':now()}
        except ValueError:
            with self.connect() as con:con.execute("UPDATE channels SET connection_state='NEEDS_RECONNECT',last_test=? WHERE id=?",(now(),cid))
            raise

    def list_sheet_sources(self):
        with self.connect() as con:
            return [dict(r) for r in con.execute("SELECT * FROM sheet_sources ORDER BY id")]

    def add_sheet(self, channel_id, spreadsheet_id, a1_range):
        if not CHANNEL_RE.fullmatch(channel_id) or not SHEET_RE.fullmatch(spreadsheet_id):
            raise ValueError("Invalid sheet or channel ID")
        if not SAFE_RANGE.fullmatch(a1_range) or ".." in a1_range:
            raise ValueError("Invalid sheet A1 range")
        with self.connect() as con:
            if not con.execute("SELECT 1 FROM channels WHERE id=? AND authorized=1",
                               (channel_id,)).fetchone():
                raise ValueError("Authorize the channel in Auto Stream before linking a Sheet")
            sid = "sheet_" + secrets.token_hex(8)
            con.execute("INSERT INTO sheet_sources(id,channel_id,spreadsheet_id,a1_range,updated_at) VALUES(?,?,?,?,?)",
                        (sid, channel_id, spreadsheet_id, a1_range, now()))
        return {"id": sid, "channel_id": channel_id,
                "spreadsheet_id": spreadsheet_id, "a1_range": a1_range}

    def link_named_sheet(self, payload):
        name = str(payload.get("name", "")).strip()
        url = str(payload.get("sheet_url", "")).strip()
        parsed = parse.urlparse(url)
        match = re.fullmatch(r"/spreadsheets/d/([A-Za-z0-9_-]{15,128})(?:/.*)?", parsed.path)
        if not name or len(name) > 150:
            raise ValueError("Sheet Name is required (maximum 150 characters)")
        if parsed.scheme != "https" or parsed.netloc != "docs.google.com" or not match:
            raise ValueError("Paste a complete https://docs.google.com/spreadsheets/d/... link")
        cid = str(payload.get("channel_id", ""))
        access = self._access(cid, SHEETS_SCOPE)
        spreadsheet = match.group(1)
        metadata = _http_json(SHEETS_BASE + "/" + spreadsheet + "?fields=sheets(properties)", access_token=access)
        tabs = [t["properties"] for t in metadata.get("sheets", [])]
        gid = parse.parse_qs(parsed.fragment).get("gid", parse.parse_qs(parsed.query).get("gid", [None]))[0]
        selected = next((t for t in tabs if str(t.get("sheetId")) == str(payload.get("worksheet_id", gid))), None)
        if selected is None:
            if len(tabs) != 1:
                return {"requires_worksheet": True, "worksheets": tabs}
            selected = tabs[0]
        title = str(selected["title"])
        a1 = "'" + title.replace("'", "''") + "'!A1:AZ201"
        with self.connect() as con:
            prior = con.execute("SELECT id FROM sheet_sources WHERE channel_id=? AND spreadsheet_id=? AND a1_range=?", (cid, spreadsheet, a1)).fetchone()
            supplied=payload.get('id')
            if supplied and not con.execute('SELECT 1 FROM sheet_sources WHERE id=?',(str(supplied),)).fetchone():raise KeyError('Sheet source not found')
            sid = str(supplied) if supplied else (prior["id"] if prior else "sheet_" + secrets.token_hex(8))
            con.execute("""INSERT INTO sheet_sources(id,channel_id,spreadsheet_id,a1_range,updated_at,name,sheet_url)
                VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,sheet_url=excluded.sheet_url,channel_id=excluded.channel_id,spreadsheet_id=excluded.spreadsheet_id,a1_range=excluded.a1_range,updated_at=excluded.updated_at""",
                (sid,cid,spreadsheet,a1,now(),name,url))
        return {"id":sid,"name":name,"sheet_url":url,"worksheet":title}

    def sync_sheet(self, sheet_id, dry_run=True, engine: StreamEngine | None = None):
        with self.connect() as con:row=con.execute('SELECT * FROM sheet_sources WHERE id=?',(sheet_id,)).fetchone()
        if not row:raise KeyError('Sheet link not found')
        access=self._access(row['channel_id'],SHEETS_SCOPE)
        url=SHEETS_BASE+'/'+parse.quote(row['spreadsheet_id'])+'/values/'+parse.quote(row['a1_range'],safe='')
        try:values=_http_json(url,access_token=access).get('values') or []
        except ValueError:
            with self.connect() as con:con.execute("UPDATE sheet_sources SET sync_status='FAILED: access or Google API error' WHERE id=?",(sheet_id,))
            raise
        if not values:return {'dry_run':dry_run,'total':0,'created':0,'errors':[],'preview':[],'headers':[]}
        aliases={
            'channelid':'channel_id','ucchannelid':'channel_id','youtubechannelid':'channel_id','ucid':'channel_id',
            'channelname':'channel_name','name':'channel_name','channel':'channel_name','title':'title','streamtitle':'title','videotitle':'title',
            'gmail':'account_label','googleaccount':'account_label','email':'account_label','group':'group_name','groupname':'group_name',
            'mediapath':'media_path','media':'media_path','videopath':'media_path','videoreference':'media_path',
            'scheduledat':'scheduled_at','scheduledstart':'scheduled_at','starttime':'scheduled_at','schedule':'scheduled_at',
            'enabled':'enabled','active':'enabled','status':'status','description':'description','privacy':'privacy',
            'duration':'duration','durationseconds':'duration','loops':'loop_count','loopcount':'loop_count','continuous':'continuous','unlimited':'continuous'}
        custom=json.loads(row['header_map'] or '{}')
        normalize=lambda text:re.sub(r'[^a-z0-9]','',str(text).lower())
        headers=[str(v).strip() for v in values[0]]
        columns=[custom.get(h,aliases.get(normalize(h),normalize(h))) for h in headers]
        if len(columns)!=len(set(columns)):raise ValueError('Duplicate mapped columns; edit the header mapping')
        if 'channel_id' not in columns:raise ValueError('Sheet requires Channel ID / YouTube Channel ID; set header mapping if needed')
        schedule_mode='media_path' in columns
        if schedule_mode and 'title' not in columns:raise ValueError('Stream scheduling sheet requires Title / Stream Title')
        engine=engine or service();created=0;skipped=0;preview=[];errors=[];inventory=0
        for index,fields in enumerate(values[1:1001],start=2):
            data={k:str(fields[i]).strip() if i<len(fields) else '' for i,k in enumerate(columns)}
            if not any(data.values()):continue
            if data.get('enabled','true').lower() in ('false','no','0','inactive','disabled'):continue
            try:
                cid=data.get('channel_id','')
                if not CHANNEL_RE.fullmatch(cid):raise ValueError('Invalid UC Channel ID')
                if not schedule_mode:
                    entry={'row':index,'channel_id':cid,'channel_name':data.get('channel_name') or cid,'group_name':data.get('group_name','')}
                    preview.append(entry)
                    if not dry_run:
                        self.import_metadata(cid,entry['channel_name'])
                        with self.connect() as con:con.execute('UPDATE channels SET group_name=?,account_label=?,sheet_id=?,updated_at=? WHERE id=?',(entry['group_name'],data.get('account_label','')[:150],sheet_id,now(),cid))
                        inventory+=1
                    continue
                if not any(c['id']==cid and c['authorized'] and c['active'] for c in self.channel_catalog()):
                    raise ValueError('Row channel requires an active independent Auto Stream OAuth connection')
                when=scheduled_utc(data.get('scheduled_at'))
                engine._source(data.get('media_path',''))
                title=data.get('title','')
                if not title or len(title)>100:raise ValueError('Invalid title')
                key='sheet-'+hashlib.sha256((sheet_id+':'+cid+':'+data['media_path']+':'+str(when)+':'+title).encode()).hexdigest()[:40]
                payload={'channel_id':cid,'media_path':data['media_path'],'title':title,'scheduled_at':when,
                         'idempotency_key':key,'description':data.get('description',''),'privacy':data.get('privacy') or 'private',
                         'loop_count':int(data.get('loop_count') or 1),'duration':float(data.get('duration') or 0),
                         'continuous':data.get('continuous','').lower() in ('true','yes','1')}
                if payload['privacy'] not in ('private','unlisted','public'):raise ValueError('Invalid privacy')
                if not 1<=payload['loop_count']<=10000 or not 0<=payload['duration']<=604800:raise ValueError('Invalid loop count or duration')
                preview.append({'row':index,**{k:v for k,v in payload.items() if k!='idempotency_key'}})
                if not dry_run:
                    with engine._connect() as con:exists=con.execute('SELECT 1 FROM jobs WHERE idem=?',(key,)).fetchone()
                    if exists:skipped+=1
                    else:engine.create(payload);created+=1
            except (ValueError,TypeError) as exc:errors.append({'row':index,'reason':str(exc)[:160]})
        if not dry_run:
            with self.connect() as con:con.execute('UPDATE sheet_sources SET sync_status=?,last_sync=?,updated_at=? WHERE id=?',('Synced: '+str(created)+' drafts, '+str(inventory)+' channels, '+str(len(errors))+' errors',now(),now(),sheet_id))
        return {'dry_run':dry_run,'mode':'schedule' if schedule_mode else 'channel_inventory','total':len(preview),
                'created':created,'inventory_updated':inventory,'skipped_duplicates':skipped,'errors':errors,
                'preview':preview[:50],'headers':headers,'mapping':dict(zip(headers,columns)),'limited_to_rows':1000}

    def channel_live_status(self, channel_id):
        access = self._access(channel_id, YOUTUBE_SCOPE)
        result = _http_json(YT_BASE + "/liveBroadcasts?" + parse.urlencode({
            "part": "id,snippet,status,contentDetails", "mine": "true",
            "maxResults": "50", "broadcastStatus": "all"}), access_token=access)
        # Never return stream key / OAuth credentials or ingestion info.
        return {"channel_id": channel_id, "broadcasts": [
            {"id": b.get("id"), "title": (b.get("snippet") or {}).get("title"),
             "life_cycle": (b.get("status") or {}).get("lifeCycleStatus"),
             "privacy": (b.get("status") or {}).get("privacyStatus"),
             "youtube_verified_live": (b.get("status") or {}).get("lifeCycleStatus") == "live"}
            for b in result.get("items", [])]}


_instance: AutoStreamAccounts | None = None


def accounts():
    global _instance
    if _instance is None:
        _instance = AutoStreamAccounts()
    return _instance


def _api_guard(action):
    try:
        return action()
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        # Values are validation-safe and contain no tokens, keys or Google bodies.
        raise HTTPException(422, str(exc)) from exc


@router.get("/integrations/health")
def integrations_health():
    try:
        _cipher()
        vault_ready = True
    except ValueError:
        vault_ready = False
    try:
        _configuration()
        client_ready = True
    except ValueError:
        client_ready = False
    return {"ok": True, "oauth_configured": client_ready and vault_ready,
            "vault_ready": vault_ready, "google_client_ready": client_ready,
            "channels_connected": sum(c["authorized"] for c in accounts().channel_catalog()),
            "sheets_readonly": True, "youtube_mutations_enabled": False,
            "publishing_enabled": os.environ.get("AUTO_STREAM_PUBLISH_ENABLED") == "1"}


@router.get("/channels")
def channels():
    return {"channels": accounts().channel_catalog()}


@router.post("/channels/import")
def import_channel(payload: dict = Body(...)):
    return _api_guard(lambda: accounts().import_metadata(
        str(payload.get("channel_id", "")), str(payload.get("title", ""))))


@router.post("/channels/{channel_id}/disconnect")
def disconnect_channel(channel_id: str):
    return _api_guard(lambda: accounts().disconnect(channel_id))


@router.get("/oauth/start")
def oauth_start(with_sheets: bool = Query(False), expected_channel: str = Query("")):
    return _api_guard(lambda: accounts().begin_oauth(include_sheets=with_sheets, expected_channel=expected_channel))


@router.get("/oauth/callback", response_class=HTMLResponse)
def oauth_callback(state: str = Query(""), code: str = Query(""),
                   error: str = Query("")):
    if error:
        raise HTTPException(400, "Google authorization denied")
    result = _api_guard(lambda: accounts().finish_oauth(state, code))
    # Escape channel title; never display refresh/access token or code.
    from html import escape
    return HTMLResponse("<!doctype html><html><body>"
                        "<h2>Auto Stream channel connected</h2><p>"
                        + escape(result["title"]) + "</p>"
                        '<p><a href="/auto-stream/">Return to Auto Stream</a></p></body></html>')


@router.get("/sheets")
def sheets():
    return {"sources": accounts().list_sheet_sources()}


@router.post("/sheets")
def add_sheet(payload: dict = Body(...)):
    if "sheet_url" in payload:
        return _api_guard(lambda: accounts().link_named_sheet(payload))
    return _api_guard(lambda: accounts().add_sheet(
        str(payload.get("channel_id", "")),
        str(payload.get("spreadsheet_id", "")),
        str(payload.get("a1_range", "Sheet1!A1:Z200"))))


@router.post("/sheets/{sheet_id}/sync")
def sync_sheet(sheet_id: str, dry_run: bool = Query(True)):
    return _api_guard(lambda: accounts().sync_sheet(sheet_id, dry_run=dry_run))


@router.get("/channels/{channel_id}/live")
def channel_live_status(channel_id: str):
    return _api_guard(lambda: accounts().channel_live_status(channel_id))


@router.post('/channels/manage')
def manage_channels(payload:dict=Body(...)):
    return _api_guard(lambda:accounts().manage_channels(payload.get('ids'),str(payload.get('action','')),payload.get('values')))


@router.post('/channels/{channel_id}/test')
def test_channel(channel_id:str):
    return _api_guard(lambda:accounts().test_channel(channel_id))


@router.patch('/sheets/{sheet_id}')
def update_sheet(sheet_id:str,payload:dict=Body(...)):
    def run():
        acct=accounts()
        with acct.connect() as con:
            source=con.execute('SELECT * FROM sheet_sources WHERE id=?',(sheet_id,)).fetchone()
            if not source:raise KeyError('Sheet source not found')
            if 'header_map' in payload:
                mapping=payload['header_map']
                allowed={'channel_id','channel_name','account_label','group_name','media_path','title','scheduled_at','enabled','status','description','privacy','duration','loop_count','continuous'}
                if not isinstance(mapping,dict) or len(mapping)>60 or any(not isinstance(k,str) or v not in allowed for k,v in mapping.items()):raise ValueError('Invalid header mapping')
                con.execute('UPDATE sheet_sources SET header_map=?,updated_at=? WHERE id=?',(json.dumps(mapping),now(),sheet_id))
                return {'updated':sheet_id}
        return acct.link_named_sheet({**dict(source),**payload,'id':sheet_id})
    return _api_guard(run)


@router.delete('/sheets/{sheet_id}')
def disconnect_sheet(sheet_id:str):
    def run():
        acct=accounts()
        with acct.connect() as con:
            con.execute('UPDATE channels SET sheet_id=? WHERE sheet_id=?',('',sheet_id))
            con.execute('DELETE FROM sheet_sources WHERE id=?',(sheet_id,))
        return {'disconnected':sheet_id,'jobs_and_tokens_preserved':True}
    return _api_guard(run)
