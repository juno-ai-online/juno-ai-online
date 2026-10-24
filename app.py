import os, json, time, uuid, sqlite3, secrets, logging, threading, re, base64, mimetypes, ast, tempfile, subprocess, hashlib
from datetime import datetime, timezone, timedelta
from functools import wraps
from urllib.parse import urlparse
import requests
from flask import Flask, request, jsonify, render_template, session, redirect, url_for, Response
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from dotenv import load_dotenv
from cryptography.fernet import Fernet, InvalidToken

try:
    from ddgs import DDGS
except Exception:
    DDGS = None

BASE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE, '.env'))
DATA_DIR = os.getenv('JUNO_DATA_DIR', os.path.join(BASE, 'data'))
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH = os.getenv('DATABASE', os.path.join(DATA_DIR, 'juno-v30.db'))
PORT = int(os.getenv('PORT', '8080'))
OLLAMA_TIMEOUT = float(os.getenv('OLLAMA_TIMEOUT', '180'))
SESSION_DAYS = int(os.getenv('SESSION_DAYS', '30'))
COOKIE_SECURE = os.getenv('COOKIE_SECURE', '0') == '1'
GOOGLE_CLIENT_ID = os.getenv('GOOGLE_CLIENT_ID', '').strip()
DDGS_REGION = os.getenv('DDGS_REGION', 'ng-en').strip() or 'us-en'
# Optional OpenAI-compatible hosted inference endpoint. This lets JUNO answer without a personal PC/Ollama worker.
JUNO_CLOUD_AI_BASE_URL = os.getenv('JUNO_CLOUD_AI_BASE_URL', '').strip().rstrip('/')
JUNO_CLOUD_AI_API_KEY = os.getenv('JUNO_CLOUD_AI_API_KEY', '').strip()
JUNO_CLOUD_AI_MODEL = os.getenv('JUNO_CLOUD_AI_MODEL', '').strip()
JUNO_ALLOWED_ORIGINS = {x.strip().rstrip('/') for x in os.getenv('JUNO_ALLOWED_ORIGINS', '').split(',') if x.strip()}
SESSION_COOKIE_SAMESITE = os.getenv('SESSION_COOKIE_SAMESITE', 'Lax').strip().capitalize()
if SESSION_COOKIE_SAMESITE not in ('Lax', 'Strict', 'None'):
    SESSION_COOKIE_SAMESITE = 'Lax'

def persistent_session_secret():
    configured=os.getenv('JUNO_SESSION_SECRET','').strip()
    if configured and not configured.startswith('replace-with-'):
        return configured
    # Keep sessions valid across application restarts even when the operator has not set an env secret.
    secret_path=os.path.join(DATA_DIR,'.session_secret')
    try:
        if os.path.isfile(secret_path):
            with open(secret_path,'r',encoding='utf-8') as f:
                existing=f.read().strip()
            if len(existing)>=32: return existing
        generated=secrets.token_urlsafe(48)
        with open(secret_path,'w',encoding='utf-8') as f: f.write(generated)
        try: os.chmod(secret_path,0o600)
        except OSError: pass
        log.warning('JUNO_SESSION_SECRET is not configured; a persistent secret was created in the data directory. Set JUNO_SESSION_SECRET in .env for managed deployments.')
        return generated
    except OSError:
        log.exception('Could not persist the session secret; sessions may not survive a restart.')
        return secrets.token_urlsafe(48)

app = Flask(__name__, template_folder='templates', static_folder='static')
app.config.update(SECRET_KEY=persistent_session_secret(), SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE=SESSION_COOKIE_SAMESITE, SESSION_COOKIE_SECURE=COOKIE_SECURE,
                  PERMANENT_SESSION_LIFETIME=timedelta(days=SESSION_DAYS), MAX_CONTENT_LENGTH=100 * 1024 * 1024)  # No arbitrary Flask request-size ceiling; deploy behind normal infrastructure safeguards.
logging.basicConfig(level=logging.INFO, format='%(asctime)s | %(levelname)s | %(message)s')
log = logging.getLogger('juno-v30')

def request_origin():
    scheme = (request.headers.get('X-Forwarded-Proto') or request.scheme).split(',')[0].strip()
    return f"{scheme}://{request.host}".rstrip('/')

@app.before_request
def protect_cross_origin_writes():
    origin = request.headers.get('Origin', '').strip().rstrip('/')
    if not origin or request.method in ('GET', 'HEAD', 'OPTIONS'):
        return None
    allowed = JUNO_ALLOWED_ORIGINS | {request_origin()}
    if origin not in allowed:
        return jsonify({'error': 'This website origin is not allowed to access JUNO.'}), 403
    return None

@app.after_request
def add_cors_headers(response):
    origin = request.headers.get('Origin', '').strip().rstrip('/')
    allowed = JUNO_ALLOWED_ORIGINS | {request_origin()}
    if origin and origin in allowed:
        response.headers['Access-Control-Allow-Origin'] = origin
        response.headers['Access-Control-Allow-Credentials'] = 'true'
        response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, PATCH, DELETE, OPTIONS'
        response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
        response.headers['Vary'] = 'Origin'
    return response


def cloud_key_cipher():
    """Encrypt registered cloud API keys using a key derived from JUNO's persistent server secret."""
    raw = str(app.config.get('SECRET_KEY', ''))
    key = base64.urlsafe_b64encode(hashlib.sha256(('JUNO_CLOUD_KEYS_V1:' + raw).encode('utf-8')).digest())
    return Fernet(key)


def cloud_provider_rows():
    with db() as c:
        return [dict(r) for r in c.execute(
            "SELECT id,name,base_url,model,encrypted_api_key,enabled,priority,status,last_tested,created_at,updated_at FROM cloud_provider_keys ORDER BY priority,id"
        ).fetchall()]


def cloud_chat_completion(messages, requested_model='auto'):
    """Try enabled registered OpenAI-compatible cloud providers, then legacy environment configuration."""
    if any(isinstance(m, dict) and m.get('images') for m in messages):
        return None  # Current cloud adapter is text-only; image requests can use a vision-capable Ollama worker.
    clean_messages = []
    for item in messages:
        role = item.get('role')
        if role in ('system', 'user', 'assistant') and isinstance(item.get('content', ''), str):
            clean_messages.append({'role': role, 'content': item.get('content', '')})
    candidates = []
    try:
        for row in cloud_provider_rows():
            if not row['enabled'] or requested_model not in ('', 'auto', 'cloud', 'online', row['name'], row['model']):
                continue
            try:
                key = cloud_key_cipher().decrypt(bytes(row['encrypted_api_key'])).decode('utf-8')
                candidates.append((row['id'], row['name'], row['base_url'], row['model'], key))
            except (InvalidToken, ValueError, UnicodeDecodeError):
                log.error('Registered cloud provider %s key could not be decrypted.', row['id'])
    except sqlite3.Error:
        log.exception('Could not load registered cloud providers.')
    if JUNO_CLOUD_AI_BASE_URL and JUNO_CLOUD_AI_API_KEY and JUNO_CLOUD_AI_MODEL and requested_model in ('', 'auto', 'cloud', 'online', JUNO_CLOUD_AI_MODEL):
        candidates.append((None, 'Environment Cloud AI', JUNO_CLOUD_AI_BASE_URL, JUNO_CLOUD_AI_MODEL, JUNO_CLOUD_AI_API_KEY))
    for provider_id, name, base_url, model_name, api_key in candidates:
        url = base_url.rstrip('/')
        if not url.endswith('/chat/completions'):
            url += '/chat/completions'
        try:
            response = requests.post(url, headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
                json={'model': model_name, 'messages': clean_messages, 'temperature': 0.4}, timeout=min(OLLAMA_TIMEOUT, 90))
            response.raise_for_status()
            choices = response.json().get('choices') or []
            answer = str(((choices[0].get('message') or {}).get('content') or '') if choices else '').strip()
            if not answer:
                raise RuntimeError('Empty cloud response')
            if provider_id is not None:
                with db() as c:
                    c.execute("UPDATE cloud_provider_keys SET status='online',last_tested=?,updated_at=? WHERE id=?", (now(), now(), provider_id)); c.commit()
            return answer, model_name
        except Exception as exc:
            # Never log provider response bodies, URLs containing credentials, or keys.
            log.warning('Cloud provider %s request failed (%s); trying next provider.', name, type(exc).__name__)
            if provider_id is not None:
                with db() as c:
                    c.execute("UPDATE cloud_provider_keys SET status='error',last_tested=?,updated_at=? WHERE id=?", (now(), now(), provider_id)); c.commit()
    return None

DB_LOCK = threading.RLock()
WORKER_LOCK = threading.RLock()
WORKER_BUSY = set()
NOMINATIM_LOCK = threading.Lock()
NOMINATIM_LAST = 0.0
MAP_CACHE = {}


def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')

def private_memory_cipher():
    # Prefer a dedicated Fernet key. The stable session secret is a backward-compatible fallback.
    configured=os.getenv('JUNO_PRIVATE_MEMORY_KEY','').strip()
    if configured:
        try: return Fernet(configured.encode('ascii'))
        except (ValueError,UnicodeEncodeError): raise RuntimeError('JUNO_PRIVATE_MEMORY_KEY must be a valid Fernet key; generate one with Fernet.generate_key().')
    raw=str(app.config.get('SECRET_KEY',''))
    key=base64.urlsafe_b64encode(hashlib.sha256(raw.encode('utf-8')).digest())
    return Fernet(key)

PRIVATE_MEMORY_MAX_BYTES = 1024 * 1024
def db():
    con = sqlite3.connect(DB_PATH, timeout=20)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA foreign_keys=ON')
    con.execute('PRAGMA journal_mode=WAL')
    return con

def columns(con, table): return {r['name'] for r in con.execute(f'PRAGMA table_info({table})')}

def init_db():
    with DB_LOCK, db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,email TEXT NOT NULL UNIQUE,password_hash TEXT,is_admin INTEGER NOT NULL DEFAULT 0,active INTEGER NOT NULL DEFAULT 1,session_version INTEGER NOT NULL DEFAULT 0,google_sub TEXT,created_at TEXT NOT NULL,last_seen TEXT);
        CREATE TABLE IF NOT EXISTS computers(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,ip TEXT NOT NULL,port INTEGER NOT NULL DEFAULT 11434,enabled INTEGER NOT NULL DEFAULT 1,weight INTEGER NOT NULL DEFAULT 50,priority INTEGER NOT NULL DEFAULT 10,model_pin TEXT DEFAULT '',status TEXT DEFAULT 'unknown',last_seen TEXT,response_time REAL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(ip,port));
        CREATE TABLE IF NOT EXISTS conversations(id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,title TEXT NOT NULL DEFAULT 'New chat',summary TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL,FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY AUTOINCREMENT,conversation_id TEXT NOT NULL,role TEXT NOT NULL,content TEXT NOT NULL,model TEXT,computer_id INTEGER,response_time REAL,created_at TEXT NOT NULL,FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,FOREIGN KEY(computer_id) REFERENCES computers(id) ON DELETE SET NULL);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS cloud_provider_keys(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL UNIQUE,base_url TEXT NOT NULL,model TEXT NOT NULL,encrypted_api_key BLOB NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,priority INTEGER NOT NULL DEFAULT 10,status TEXT NOT NULL DEFAULT 'untested',last_tested TEXT,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY AUTOINCREMENT,title TEXT NOT NULL,body TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,trigger_type TEXT NOT NULL DEFAULT 'next_login',trigger_value INTEGER NOT NULL DEFAULT 0,start_at TEXT,end_at TEXT,created_at TEXT NOT NULL,created_by INTEGER);
        CREATE TABLE IF NOT EXISTS notification_seen(user_id INTEGER NOT NULL,notification_id INTEGER NOT NULL,seen_at TEXT NOT NULL,PRIMARY KEY(user_id,notification_id),FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,FOREIGN KEY(notification_id) REFERENCES notifications(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS invitation_codes(id INTEGER PRIMARY KEY AUTOINCREMENT,code_hash TEXT NOT NULL UNIQUE,assigned_name TEXT NOT NULL,used_by INTEGER,used_at TEXT,expires_at TEXT,created_at TEXT NOT NULL,created_by INTEGER);
        CREATE TABLE IF NOT EXISTS premium_codes(id INTEGER PRIMARY KEY AUTOINCREMENT,code_hash TEXT NOT NULL UNIQUE,label TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,expires_at TEXT,created_at TEXT NOT NULL,created_by INTEGER);
        CREATE TABLE IF NOT EXISTS premium_grants(user_id INTEGER PRIMARY KEY,code_id INTEGER,active INTEGER NOT NULL DEFAULT 1,granted_at TEXT NOT NULL,FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,FOREIGN KEY(code_id) REFERENCES premium_codes(id) ON DELETE SET NULL);
        CREATE TABLE IF NOT EXISTS knowledge(id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,info TEXT NOT NULL,tags TEXT DEFAULT '',enabled INTEGER NOT NULL DEFAULT 1,image_url TEXT DEFAULT '',image_alt TEXT DEFAULT '',link_url TEXT DEFAULT '',link_text TEXT DEFAULT '',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS user_preferences(user_id INTEGER PRIMARY KEY,display_name TEXT,theme TEXT NOT NULL DEFAULT 'system',notifications_enabled INTEGER NOT NULL DEFAULT 1,updated_at TEXT NOT NULL,FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS user_private_memory(user_id INTEGER PRIMARY KEY,encrypted_content BLOB NOT NULL,updated_at TEXT NOT NULL,FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS uploads(id TEXT PRIMARY KEY,user_id INTEGER NOT NULL,conversation_id TEXT NOT NULL,message_id INTEGER,original_name TEXT NOT NULL,stored_name TEXT NOT NULL,mime_type TEXT NOT NULL,size_bytes INTEGER NOT NULL,created_at TEXT NOT NULL,FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE,FOREIGN KEY(message_id) REFERENCES messages(id) ON DELETE SET NULL);
        CREATE TABLE IF NOT EXISTS user_suggestions(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER NOT NULL,title TEXT NOT NULL,details TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'new',created_at TEXT NOT NULL,FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE);
        CREATE TABLE IF NOT EXISTS request_stats(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id INTEGER,conversation_id TEXT,computer_id INTEGER,model TEXT,success INTEGER NOT NULL DEFAULT 0,response_time REAL,created_at TEXT NOT NULL,FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE SET NULL,FOREIGN KEY(computer_id) REFERENCES computers(id) ON DELETE SET NULL);
        ''')
        c.execute('CREATE INDEX IF NOT EXISTS idx_knowledge_enabled ON knowledge(enabled, id DESC)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_messages_conversation_id ON messages(conversation_id, id)')
        # Safe additive migrations for databases created by earlier JUNO builds.
        migrations = {
          'users': {'active':'INTEGER NOT NULL DEFAULT 1','session_version':'INTEGER NOT NULL DEFAULT 0','google_sub':'TEXT','last_seen':'TEXT'},
          'computers': {'weight':'INTEGER NOT NULL DEFAULT 50','model_pin':"TEXT DEFAULT ''",'status':"TEXT DEFAULT 'unknown'",'last_seen':'TEXT','response_time':'REAL'},
          'conversations': {'summary':"TEXT NOT NULL DEFAULT ''"},
          'knowledge': {'image_url':"TEXT DEFAULT ''",'image_alt':"TEXT DEFAULT ''",'link_url':"TEXT DEFAULT ''",'link_text':"TEXT DEFAULT ''"}
        }
        for table, fields in migrations.items():
            existing = columns(c, table)
            for name, decl in fields.items():
                if name not in existing: c.execute(f'ALTER TABLE {table} ADD COLUMN {name} {decl}')
        prior_code_kb = c.execute('SELECT value FROM settings WHERE key=?',('code_file_max_kb',)).fetchone()
        had_code_mb = c.execute('SELECT 1 FROM settings WHERE key=?',('code_file_max_mb',)).fetchone() is not None
        defaults = {'allow_web_search':'1','allow_image_search':'1','allow_voice_input':'1','allow_knowledge':'1','allow_theme_choice':'1','allow_uploads':'1','allow_maps':'1','allow_code_tools':'1','code_file_max_mb':'1','upload_limit_count':'3','upload_window_minutes':'60','upload_max_mb':'10','chat_limit':'30','cooldown_seconds':'60','premium_enabled':'1','default_theme':'system','routing_mode':'weighted','system_prompt':'You are JUNO, a capable, clear and honest general AI assistant. Answer directly, explain reasoning at a useful level, and distinguish verified facts from uncertainty. For research, use supplied web sources when available and cite them. For code, provide complete, runnable solutions where practical.'}
        for k,v in defaults.items(): c.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)',(k,v))
        # Migrate the old KB setting once, preserving the existing effective limit.
        if not had_code_mb and prior_code_kb:
            try: migrated_mb=max(1,min(1024,(int(prior_code_kb['value'])+1023)//1024))
            except (TypeError,ValueError): migrated_mb=1
            c.execute('UPDATE settings SET value=? WHERE key=?',(str(migrated_mb),'code_file_max_mb'))
        # Seed default workers only on a truly empty new database.
        if c.execute('SELECT COUNT(*) FROM computers').fetchone()[0] == 0:
            seed = [('Juno Fast',os.getenv('OLLAMA_PC1_IP','192.168.100.3'),int(os.getenv('OLLAMA_PC1_PORT','11434')),60,10),('Juno Pro',os.getenv('OLLAMA_PC2_IP','192.168.100.19'),int(os.getenv('OLLAMA_PC2_PORT','11434')),40,20)]
            for name,ip,port,weight,priority in seed:
                c.execute('INSERT OR IGNORE INTO computers(name,ip,port,weight,priority,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',(name,ip,port,weight,priority,now(),now()))
        # Optional initial admin; only creates if the email does not already exist.
        admin_email=os.getenv('JUNO_ADMIN_EMAIL','').strip().lower(); admin_password=os.getenv('JUNO_ADMIN_PASSWORD','')
        if admin_email and admin_password and not c.execute('SELECT 1 FROM users WHERE email=?',(admin_email,)).fetchone():
            c.execute('INSERT INTO users(name,email,password_hash,is_admin,created_at) VALUES(?,?,?,?,?)',('JUNO Admin',admin_email,generate_password_hash(admin_password),1,now()))
        c.commit()

def setting(key, default=''):
    with db() as c:
        row=c.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
        return row['value'] if row else default

def put_setting(key,value):
    with db() as c: c.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,str(value))); c.commit()

def current_user():
    uid=session.get('uid')
    if not uid: return None
    with db() as c:
        row=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
        if not row or not row['active'] or int(row['session_version']) != int(session.get('sv',-1)):
            session.clear(); return None
        # Refresh online presence at most once per minute to avoid a write on every API call.
        last=row['last_seen'] or ''
        if not last or last < (datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat(timespec='seconds'):
            c.execute('UPDATE users SET last_seen=? WHERE id=?',(now(),uid)); c.commit()
            row=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
    return row

def auth_required(fn):
    @wraps(fn)
    def wrapped(*a,**kw):
        if not current_user(): return jsonify({'error':'Please sign in again.'}),401
        return fn(*a,**kw)
    return wrapped

def admin_required(fn):
    @wraps(fn)
    def wrapped(*a,**kw):
        u=current_user()
        if not u: return jsonify({'error':'Please sign in again.'}),401
        if not u['is_admin']: return jsonify({'error':'Admin access required.'}),403
        return fn(*a,**kw)
    return wrapped

def safe_user(u): return {'id':u['id'],'name':u['name'],'email':u['email'],'is_admin':bool(u['is_admin']),'active':bool(u['active']),'premium':has_premium(u['id'])}
def has_premium(uid):
    with db() as c:
        row=c.execute('SELECT p.active,p.code_id,c.enabled,c.expires_at FROM premium_grants p LEFT JOIN premium_codes c ON c.id=p.code_id WHERE p.user_id=?',(uid,)).fetchone()
    if not row or not row['active'] or (row['code_id'] is not None and not row['enabled']): return False
    if row['expires_at'] and row['expires_at'] < now(): return False
    return True

def start_session(user):
    session.clear(); session.permanent=True; session['uid']=user['id']; session['sv']=user['session_version']; session['login_epoch']=time.time()
    with db() as c: c.execute('UPDATE users SET last_seen=? WHERE id=?',(now(),user['id'])); c.commit()

def login_user(email,password):
    with db() as c: u=c.execute('SELECT * FROM users WHERE email=? COLLATE NOCASE',(email.strip().lower(),)).fetchone()
    if not u or not u['active'] or not u['password_hash'] or not check_password_hash(u['password_hash'],password): return None
    start_session(u); return u

def hash_code(code):
    import hashlib
    return hashlib.sha256(code.strip().encode('utf-8')).hexdigest()

def ddg_search(query, kind='text', limit=6):
    """Free no-key metasearch with result cleanup and a quick alternate-query fallback."""
    if DDGS is None:
        return {'results': [], 'error': 'Free search is unavailable because the ddgs package is not installed.'}
    query = re.sub(r'\s+', ' ', str(query or '')).strip()[:300]
    if not query:
        return {'results': [], 'error': 'Enter a search query.'}

    def run(q, timeout=8, region=DDGS_REGION):
        with DDGS(timeout=timeout) as ddgs:
            if kind == 'images':
                return list(ddgs.images(q, max_results=limit, safesearch='moderate', region=region, backend='auto'))
            if kind == 'news':
                return list(ddgs.news(q, max_results=limit, safesearch='moderate', region=DDGS_REGION, backend='auto'))
            return list(ddgs.text(q, max_results=limit, safesearch='moderate', region=DDGS_REGION, backend='auto'))

    rows = []
    try:
        rows = run(query)
        # Some engines return no results for a long conversational prompt. Retry a shorter,
        # search-engine-friendly query once rather than silently giving up.
        if not rows:
            if kind == 'images':
                compact = re.sub(r'\b(?:please|show me|find me|give me|pictures?|photos?|photographs?|images?|visuals?|of|for)\b', ' ', query, flags=re.I)
                compact = re.sub(r'\s+', ' ', compact).strip()
                if compact and compact.lower() != query.lower(): rows = run(compact[:180], timeout=6)
                if not rows and DDGS_REGION != 'us-en': rows = run((compact or query)[:140], timeout=6, region='us-en')
            else:
                compact = re.sub(r'^(please\s+)?(help me|can you|could you|would you|please|tell me|explain to me|i want to know)\s+', '', query, flags=re.I)
                compact = re.sub(r'\b(find evidence about|research the topic of|give me information about)\b', '', compact, flags=re.I).strip(' ?.,:')
                if compact and compact.lower() != query.lower(): rows = run(compact[:180], timeout=6)
    except Exception as e:
        log.warning('Free metasearch %s attempt failed: %s', kind, type(e).__name__)
        # Retry failed image lookups with a compact subject and a second region; public free providers can rate-limit.
        try:
            compact=re.sub(r'\b(?:please|show me|find me|give me|pictures?|photos?|photographs?|images?|visuals?|of|for)\b',' ',query,flags=re.I)
            compact=re.sub(r'\s+',' ',compact).strip() or query[:120]
            rows=run(compact[:180],timeout=6,region=DDGS_REGION)
            if not rows and kind=='images' and DDGS_REGION!='us-en': rows=run(compact[:140],timeout=6,region='us-en')
        except Exception as retry_error:
            log.warning('Free metasearch retry failed: %s', type(retry_error).__name__)
        if not rows:
            return {'results': [], 'error': 'Free web search is temporarily unavailable or rate-limited. JUNO can still answer from its model knowledge, but current facts may need verification.'}

    results, seen = [], set()
    for x in rows:
        if kind == 'images':
            url = x.get('url') or x.get('image') or ''
            image_url = x.get('image') or x.get('thumbnail') or url
            if not urlparse(url).scheme.startswith('http') or not image_url:
                continue
            item = {'title': x.get('title') or 'Image result', 'url': url, 'image': image_url,
                    'thumbnail': x.get('thumbnail') or image_url, 'source': x.get('source') or urlparse(url).netloc}
        else:
            url = x.get('href') or x.get('url') or ''
            if not url.startswith(('https://', 'http://')) or url in seen:
                continue
            seen.add(url)
            item = {'title': x.get('title') or 'Search result', 'url': url,
                    'snippet': x.get('body') or x.get('excerpt') or x.get('description') or '',
                    'source': urlparse(url).netloc, 'date': x.get('date') or x.get('published') or ''}
        results.append(item)
        if len(results) >= limit:
            break
    return {'results': results, 'error': None if results else 'The search provider returned no usable results.'}

def get_workers():
    with db() as c: return [dict(r) for r in c.execute('SELECT * FROM computers WHERE enabled=1 ORDER BY priority,id')]

def probe_worker(worker, timeout=2.5):
    started=time.time()
    try:
        r=requests.get(f"http://{worker['ip']}:{worker['port']}/api/tags",timeout=timeout)
        r.raise_for_status(); models=[m.get('name') for m in r.json().get('models',[]) if m.get('name')]
        elapsed=time.time()-started
        with db() as c: c.execute("UPDATE computers SET status='online',last_seen=?,response_time=?,updated_at=? WHERE id=?",(now(),elapsed,now(),worker['id'])); c.commit()
        return models,elapsed
    except Exception:
        with db() as c: c.execute("UPDATE computers SET status='offline',updated_at=? WHERE id=?",(now(),worker['id'])); c.commit()
        return [],None

def is_vision_model(model_name):
    name=(model_name or '').lower()
    return any(token in name for token in ('gemma3','gemma4','qwen2.5vl','qwen3-vl','qwen2-vl','llava','minicpm-v','moondream','mistral-small3.1','pixtral','phi3.5-vision','granite3.2-vision','llama4','internvl','smolvlm','vision'))

def select_worker(requested_model='auto', excluded=None, vision=False):
    excluded=excluded or set(); workers=get_workers(); candidates=[]
    for w in workers:
        if w['id'] in excluded: continue
        with WORKER_LOCK:
            if w['id'] in WORKER_BUSY: continue
        models,elapsed=probe_worker(w)
        if not models: continue
        pin=(w.get('model_pin') or '').strip()
        model=requested_model
        if vision:
            # Image questions must use a vision-capable model, not merely the worker's default text model.
            if pin in models and is_vision_model(pin): model=pin
            elif requested_model!='auto' and requested_model in models and is_vision_model(requested_model): model=requested_model
            else: model=next((m for m in models if is_vision_model(m)),None)
            if not model: continue
        else:
            if pin: model=pin
            elif model=='auto': model=next((m for m in models if any(s in m.lower() for s in ('gemma','qwen','llama'))),models[0])
        if model not in models: continue
        candidates.append((w,model,elapsed or 999))
    if not candidates: return None
    # Weighted preference among available computers; the lowest priority is still a fallback.
    candidates.sort(key=lambda x:(int(x[0].get('priority') or 50),x[2]))
    total=sum(max(0,int(x[0].get('weight') or 0)) for x in candidates)
    if total <= 0: return candidates[0]
    pick=secrets.randbelow(total)
    for candidate in candidates:
        pick -= max(0,int(candidate[0].get('weight') or 0))
        if pick < 0: return candidate
    return candidates[0]

@app.get('/')
def home(): return render_template('index.html', google_client_id=GOOGLE_CLIENT_ID, settings={'allow_maps':setting('allow_maps','1')=='1','allow_code_tools':setting('allow_code_tools','1')=='1','code_file_max_mb':setting('code_file_max_mb','1')})
@app.get('/admin')
def admin_page():
    u=current_user()
    if not u: return redirect(url_for('home'))
    if not u['is_admin']: return redirect(url_for('home'))
    return render_template('admin.html')

@app.get('/api/me')
def api_me():
    u=current_user()
    return jsonify({'user':safe_user(u) if u else None,'google_client_id':GOOGLE_CLIENT_ID,'settings':{'allow_web_search':setting('allow_web_search','1')=='1','allow_image_search':setting('allow_image_search','1')=='1','allow_voice_input':setting('allow_voice_input','1')=='1','allow_theme_choice':setting('allow_theme_choice','1')=='1','premium_enabled':setting('premium_enabled','1')=='1','allow_uploads':setting('allow_uploads','1')=='1','allow_maps':setting('allow_maps','1')=='1','allow_code_tools':setting('allow_code_tools','1')=='1'}})

@app.post('/api/signup')
def signup():
    data=request.get_json(silent=True) or {}; email=str(data.get('email','')).strip().lower(); password=str(data.get('password','')); name=str(data.get('name','')).strip() or email.split('@')[0]
    if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$',email): return jsonify({'error':'Enter a valid email address.'}),400
    if len(password)<8: return jsonify({'error':'Use a password with at least 8 characters.'}),400
    try:
        with db() as c:
            c.execute('INSERT INTO users(name,email,password_hash,created_at) VALUES(?,?,?,?)',(name[:80],email,generate_password_hash(password),now())); c.commit()
            u=c.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone()
    except sqlite3.IntegrityError: return jsonify({'error':'An account with that email already exists.'}),409
    start_session(u); return jsonify({'user':safe_user(u)})

@app.post('/api/login')
def login():
    d=request.get_json(silent=True) or {}; u=login_user(str(d.get('email','')),str(d.get('password','')))
    if not u: return jsonify({'error':'Email or password is incorrect, or the account is disabled.'}),401
    return jsonify({'user':safe_user(u)})

@app.post('/api/logout')
def logout(): session.clear(); return jsonify({'ok':True})

@app.post('/api/google-login')
def google_login():
    d=request.get_json(silent=True) or {}; token=d.get('credential')
    if not GOOGLE_CLIENT_ID: return jsonify({'error':'Google sign-in is not configured by the administrator.'}),503
    if not token: return jsonify({'error':'Missing Google credential.'}),400
    try:
        from google.oauth2 import id_token
        from google.auth.transport import requests as google_requests
        info=id_token.verify_oauth2_token(token,google_requests.Request(),GOOGLE_CLIENT_ID)
        email=info.get('email','').lower(); sub=info.get('sub'); name=info.get('name') or email.split('@')[0]
        if not info.get('email_verified') or not email or not sub: raise ValueError('Unverified account')
        with db() as c:
            u=c.execute('SELECT * FROM users WHERE email=? COLLATE NOCASE',(email,)).fetchone()
            if u:
                if not u['active']: return jsonify({'error':'This account is disabled.'}),403
                c.execute('UPDATE users SET google_sub=?,name=CASE WHEN name IS NULL OR name=\'\' THEN ? ELSE name END WHERE id=?',(sub,name,u['id']))
                u=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone()
            else:
                c.execute('INSERT INTO users(name,email,google_sub,created_at) VALUES(?,?,?,?)',(name,email,sub,now())); c.commit()
                u=c.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone()
            c.commit()
        start_session(u); return jsonify({'user':safe_user(u)})
    except Exception as e:
        log.info('Google sign-in rejected: %s',e); return jsonify({'error':'Google sign-in could not be verified.'}),401

@app.post('/api/invitation/redeem')
def redeem_invitation():
    d=request.get_json(silent=True) or {}; email=str(d.get('email','')).strip().lower(); password=str(d.get('password','')); code=str(d.get('code','')).strip()
    if not code or not email or len(password)<8: return jsonify({'error':'Enter email, a password of at least 8 characters and the invitation code.'}),400
    with db() as c:
        invite=c.execute('SELECT * FROM invitation_codes WHERE code_hash=? AND used_by IS NULL',(hash_code(code),)).fetchone()
        if not invite: return jsonify({'error':'Invitation code is invalid or already used.'}),400
        if invite['expires_at'] and invite['expires_at']<now(): return jsonify({'error':'Invitation code has expired.'}),400
        try:
            c.execute('INSERT INTO users(name,email,password_hash,created_at) VALUES(?,?,?,?)',(invite['assigned_name'],email,generate_password_hash(password),now()))
            uid=c.execute('SELECT id FROM users WHERE email=?',(email,)).fetchone()['id']
            c.execute('UPDATE invitation_codes SET used_by=?,used_at=? WHERE id=? AND used_by IS NULL',(uid,now(),invite['id'])); c.commit()
            u=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
        except sqlite3.IntegrityError: return jsonify({'error':'An account with that email already exists.'}),409
    start_session(u); return jsonify({'user':safe_user(u)})

@app.post('/api/premium/redeem')
@auth_required
def redeem_premium():
    if setting('premium_enabled','1')!='1': return jsonify({'error':'Premium access is currently disabled.'}),403
    code=str((request.get_json(silent=True) or {}).get('code','')).strip()
    with db() as c:
        row=c.execute('SELECT * FROM premium_codes WHERE code_hash=? AND enabled=1',(hash_code(code),)).fetchone()
        if not row or (row['expires_at'] and row['expires_at']<now()): return jsonify({'error':'Premium code is invalid or expired.'}),400
        c.execute('INSERT INTO premium_grants(user_id,code_id,active,granted_at) VALUES(?,?,1,?) ON CONFLICT(user_id) DO UPDATE SET code_id=excluded.code_id,active=1,granted_at=excluded.granted_at',(session['uid'],row['id'],now())); c.commit()
    return jsonify({'ok':True,'premium':True})

@app.get('/api/conversations')
@auth_required
def conversations():
    with db() as c: rows=c.execute('SELECT id,title,created_at,updated_at FROM conversations WHERE user_id=? ORDER BY updated_at DESC LIMIT 100',(session['uid'],)).fetchall()
    return jsonify({'conversations':[dict(r) for r in rows]})

@app.post('/api/conversations')
@auth_required
def new_conversation():
    cid=uuid.uuid4().hex
    with db() as c: c.execute('INSERT INTO conversations(id,user_id,title,created_at,updated_at) VALUES(?,?,?,?,?)',(cid,session['uid'],'New chat',now(),now())); c.commit()
    return jsonify({'id':cid,'title':'New chat'})

@app.get('/api/conversations/<cid>')
@auth_required
def get_conversation(cid):
    with db() as c:
        conv=c.execute('SELECT * FROM conversations WHERE id=? AND user_id=?',(cid,session['uid'])).fetchone()
        if not conv: return jsonify({'error':'Conversation not found.'}),404
        msgs=c.execute('SELECT role,content,model,created_at FROM messages WHERE conversation_id=? ORDER BY id',(cid,)).fetchall()
    return jsonify({'conversation':dict(conv),'messages':[{'role':m['role'],'content':m['content'],'created_at':m['created_at']} for m in msgs]})

@app.delete('/api/conversations/<cid>')
@auth_required
def delete_conversation(cid):
    with db() as c: c.execute('DELETE FROM conversations WHERE id=? AND user_id=?',(cid,session['uid'])); c.commit()
    return jsonify({'ok':True})

@app.post('/api/search')
@auth_required
def search_api():
    d=request.get_json(silent=True) or {}; kind=d.get('kind','text')
    if kind=='images' and setting('allow_image_search','1')!='1': return jsonify({'error':'Image search is disabled by the administrator.'}),403
    if kind!='images' and setting('allow_web_search','1')!='1': return jsonify({'error':'Web search is disabled by the administrator.'}),403
    return jsonify(ddg_search(str(d.get('query','')),kind, min(max(int(d.get('limit',6)),1),12)))

@app.get('/api/notifications')
@auth_required
def notifications_api():
    uid=session['uid']; login_epoch=float(session.get('login_epoch',time.time()))
    with db() as c:
        # Count actual user messages rather than conversation containers for usage-triggered notices.
        count=c.execute("SELECT COUNT(*) FROM messages m JOIN conversations v ON v.id=m.conversation_id WHERE v.user_id=? AND m.role='user'",(uid,)).fetchone()[0]
        rows=c.execute('''SELECT n.* FROM notifications n LEFT JOIN notification_seen s ON s.notification_id=n.id AND s.user_id=? WHERE n.enabled=1 AND s.notification_id IS NULL AND (n.start_at IS NULL OR n.start_at<=?) AND (n.end_at IS NULL OR n.end_at>=?) ORDER BY n.id DESC''',(uid,now(),now())).fetchall()
    ready=[]
    for n in rows:
        try: created_epoch=datetime.fromisoformat(n['created_at']).timestamp()
        except Exception: created_epoch=0
        if ((n['trigger_type']=='next_login' and login_epoch>=created_epoch)
            or (n['trigger_type']=='after_chats' and count>=int(n['trigger_value']))
            or (n['trigger_type']=='after_minutes' and login_epoch+int(n['trigger_value'])*60<=time.time())):
            ready.append(dict(n))
    return jsonify({'notifications':ready})

@app.post('/api/notifications/<int:nid>/seen')
@auth_required
def notification_seen(nid):
    with db() as c: c.execute('INSERT OR IGNORE INTO notification_seen(user_id,notification_id,seen_at) VALUES(?,?,?)',(session['uid'],nid,now())); c.commit()
    return jsonify({'ok':True})

def explicit_web_search(message):
    """True when the user asks JUNO to search, browse, or research online."""
    patterns=(r'\b(search( the)? (web|internet|online)|browse( the)? (web|internet)|look (it )?up online|google this|find (me )?(online|on the web)|search for (information|articles|evidence))\b',
              r'\b(with|include|give me|show me|provide|list)\s+(the )?(sources|citations|references|links|evidence)\b',
              r'\b(source|cite|citation|references)\s+(this|your answer|the answer|the claims)\b')
    return any(re.search(p, message, re.I) for p in patterns)

def is_non_research_task(message):
    """Keep creative, code, translation, and simple arithmetic requests fast."""
    m = message.strip().lower()
    if re.match(r'^(write|draft|compose|make up|create|invent|roleplay|brainstorm)\b', m) and not re.search(r'\b(research|current|latest|sources|verify|real examples)\b', m):
        return True
    if re.search(r'\b(debug|debugging|fix this code|review this code|write code|generate code|programming|python error|javascript error|translate|translation|rewrite|proofread|correct my grammar|solve this equation|calculate|\d\s*[+*/-]\s*\d)\b', m):
        return True
    return False

def needs_live_search(message):
    """Search broad factual questions and research requests, not just words like 'latest'."""
    m = message.strip()
    if not m or re.fullmatch(r'\s*(hi|hello|hey|good morning|good afternoon|good evening|thanks|thank you|who are you|who created you|what are you)[!.? ]*\s*', m, re.I):
        return explicit_web_search(m)
    if is_non_research_task(m) and not explicit_web_search(m):
        return False
    patterns=(
        r'\b(latest|current|currently|today|tonight|yesterday|this week|this month|recent|breaking|right now|as of \d{4}|in \d{4}|202[5-9]|2030)\b',
        r'\b(news|weather|exchange rate|price of|stock price|score|results today|who won|when is the next|live results|world cup|premier league|champions league|election results|election|president of|prime minister of)\b',
        r'\b(how many|how much|what is the total|total number of|record for|career goals|goals has|goals have|current record|latest version|newest version|population of|capital of|founder of|invented by|who discovered|when was|where is|who is|who was|what happened)\b',
        r'\b(research|sources|citations|references|find evidence|verify online|fact.?check|compare current|look up|investigate|literature review|case study|pros and cons of)\b',
        r'^\s*(who|what|when|where|which|why|how)\b')
    # Explicit research intent should not be blocked by a coincidental internal knowledge match.
    return explicit_web_search(m) or any(re.search(p, m, re.I) for p in patterns)

def search_query_for(message):
    m = re.sub(r'\s+', ' ', message).strip()
    if re.search(r'\b(ronaldo|messi|haaland|mbapp[eé]|lewandowski|salah)\b',m,re.I) and re.search(r'\b(goals?|scored|scoring|career)\b',m,re.I):
        return m + ' official career goals latest 2026'
    if re.search(r'\b(world cup|worldcup)\b',m,re.I) and re.search(r'\b2026\b',m):
        return m + ' final winner official result'
    # Remove conversational padding that can dilute search-engine relevance.
    q = re.sub(r'^(please\s+)?(can you|could you|would you|tell me|explain to me|i want to know|help me understand)\s+', '', m, flags=re.I)
    if re.search(r'\b(research|investigate|literature review|case study)\b', q, re.I):
        q += ' reliable sources evidence overview'
    return q[:300]

def answer_signals_stale_or_uncertain(answer):
    patterns=(r'\bas of (late )?202[0-4]\b', r'\bup to (late )?202[0-4]\b',
        r'\bmy knowledge (cutoff|ends|only goes)\b', r'\bI (do not|don.t) have access to (real.?time|live|current)',
        r'\bI (cannot|can.t|could not|couldn.t) (verify|confirm|determine|provide an exact|access current)',
        r'\bI don.t know\b', r'\bthere is no (winner|official result) yet\b',
        r'\bhas not happened yet\b', r'\bthe number changes frequently\b',
        r'\bmay be outdated\b', r'\bI cannot give a definitive answer\b')
    return any(re.search(p,answer,re.I) for p in patterns)

def match_knowledge(message, rows):
    """Rank internal knowledge conservatively to avoid unrelated keyword matches."""
    tokens=[w.lower() for w in re.findall(r"[\w'-]+", message) if len(w)>2 and w.lower() not in {
        'the','and','for','with','what','when','where','which','who','why','how','does','did','are','was','were','can','could','would','should','please','tell','give','about','from','into','that','this','have','has','had','you','your','juno'
    }]
    if not tokens:
        return [], False
    ranked=[]
    for row in rows:
        name=(row['name'] or '').lower(); info=(row['info'] or '').lower(); tags=(row['tags'] or '').lower()
        name_hits=sum(1 for t in tokens if t in name)
        tag_hits=sum(1 for t in tokens if t in tags)
        info_hits=sum(1 for t in tokens if t in info)
        score=name_hits*3 + tag_hits*2 + info_hits
        # Prefer entries with a strong title/tag match or several matching terms.
        if score:
            ranked.append((score, row, name_hits, tag_hits, info_hits))
    ranked.sort(key=lambda item:item[0], reverse=True)
    selected=[]
    for score,row,_,__,___ in ranked[:8]:
        info=str(row['info'] or '')
        # Long entries stay complete in SQLite. Send the most relevant passage to the model,
        # rather than always taking the beginning of a long document.
        if len(info)>10000:
            lower=info.lower(); positions=[]
            for token in tokens:
                start=0
                while True:
                    pos=lower.find(token,start)
                    if pos<0: break
                    positions.append(pos); start=pos+len(token)
                    if len(positions)>=80: break
                if len(positions)>=80: break
            center=max(positions,key=lambda pos:sum(1 for t in tokens if t in lower[max(0,pos-500):pos+500])) if positions else 0
            begin=max(0,center-4500); end=min(len(info),begin+10000)
            info=('…\n' if begin else '')+info[begin:end]+('\n… [Excerpt from a longer entry; complete information remains stored.]' if end<len(info) else '')
        selected.append(f"{row['name']}: {info}")
    confident=bool(ranked and (ranked[0][2] >= 1 or ranked[0][3] >= 1 or (len(tokens)>=3 and ranked[0][0]>=3)))
    return selected, confident

def smart_conversation_title(message, answer=''):
    """Create a short topic title rather than copying the entire first prompt."""
    raw=re.sub(r'\s+', ' ', str(message or '')).strip()
    # Prefer a useful topic phrase from common question shapes.
    patterns=[
        r"(?:please\s+)?(?:can you\s+)?(?:show|open|display|give me|find)\s+(?:me\s+)?(?:a\s+)?map\s+(?:of|for|to|showing)\s+(.+)",
        r"(?:please\s+)?(?:give me|show me|find)\s+(?:directions|navigation|a route)\s+(?:to|for)\s+(.+)",
        r"directions?\s+to\s+(.+)",
        r"(?:please\s+)?(?:show|put|locate|find)\s+(.+?)\s+(?:on|in)\s+(?:the\s+)?map",
        r"(?:please\s+)?(?:show|find)\s+(?:me\s+)?(?:pictures?|photos?|images?)\s+(?:of|for)\s+(.+)",
        r"(?:please\s+)?(?:can you\s+)?(?:explain|teach me about|tell me about|describe)\s+(?:to me\s+)?(.+)",
        r"(?:please\s+)?(?:can you\s+)?(?:help me (?:understand|build|make|write|fix|learn about))\s+(.+)",
        r"(?:what is|what are|how does|how do|why does|why do|when did|where is|who is|compare)\s+(.+)",
        r"(?:research|investigate|look up)\s+(.+)",
    ]
    topic=''
    for pat in patterns:
        m=re.match(pat,raw,re.I)
        if m:
            topic=m.group(1); break
    if not topic:
        topic=raw
    if re.fullmatch(r'(?:help me|help me with this|tell me something|can you help me|explain this)',topic.strip(),re.I):
        topic=''
    topic=re.sub(r'\b(?:please|for me|in simple terms|with examples|step by step|and explain it|and give examples)\b',' ',topic,flags=re.I)
    topic=re.sub(r'\s+',' ',topic).strip(' .,!?:;')
    topic=re.sub(r'^(?:the|a|an)\s+','',topic,flags=re.I)
    topic=topic.split('?')[0].split('\n')[0].strip(' .,!?:;')
    words=topic.split()
    if len(words)>7: topic=' '.join(words[:7])
    if not topic or len(topic)<3:
        heading=re.search(r'^\s*#{1,3}\s+(.+)$',str(answer or ''),re.M)
        topic=heading.group(1).strip() if heading else 'New conversation'
    # Title case without shouting acronyms or changing known technical names too aggressively.
    title=topic[:58].strip().rstrip(' .,!?:;')
    if title and not title.isupper(): title=title[0].upper()+title[1:]
    return title or 'New conversation'


def detect_knowledge_suggestion(message):
    """Return a title/details pair only for an explicit request to save or suggest knowledge."""
    text=re.sub(r'\s+',' ',str(message or '')).strip()
    patterns=(
        r'\b(?:add|save|put|store|remember|include)\b.{0,50}\b(?:to|in|into|for)\b.{0,35}\b(?:your|juno.?s|the ai.?s?)?\s*(?:knowledge(?:\s*base)?|knowledge bank|database|training data)\b',
        r'\b(?:suggest|submit|recommend)\b.{0,50}\b(?:knowledge(?:\s*base)?|juno.?s knowledge|ai knowledge)\b',
        r'\b(?:make this|this should be)\b.{0,40}\b(?:knowledge base|knowledge suggestion)\b',
    )
    if re.search(r'\bwhat (?:information|things?|should|could|can)\b.{0,70}\b(?:add|put|include)\b',text,re.I):
        return None
    if not any(re.search(p,text,re.I) for p in patterns):
        return None
    cleaned=re.sub(r'^(?:please\s+)?(?:juno[, ]*)?','',text,flags=re.I).strip()
    payload=''
    if ':' in text:
        payload=text.split(':',1)[1].strip()
    else:
        after=re.search(r'\b(?:that|is)\s+(.+)$',text,re.I)
        if after and len(after.group(1).split())>=3: payload=after.group(1).strip(' .?!')
    title_topic=re.sub(r'\s+',' ',payload).strip(' .,!?:;')
    if not title_topic and re.search(r'\b(?:this information|this to your knowledge|add this|remember this)\b',text,re.I):
        title_topic=''
    title=('Knowledge suggestion: '+title_topic[:75]).strip(' :') if title_topic else 'Knowledge base suggestion'
    return (title[:120], text[:5000])


def needs_image_search(message):
    """Detect explicit image requests and topics where visuals materially improve the answer."""
    if map_request_for(message):
        return False
    m = re.sub(r'\s+', ' ', str(message or '')).strip()
    patterns = (
        r'\b(show|find|search|fetch|get|bring|display|provide|send|give|attach)\b.{0,90}\b(images?|pictures?|photos?|photographs?|visuals?)\b',
        r'\b(images?|pictures?|photos?|photographs?|visuals?)\b.{0,55}\b(of|for|showing|featuring|from)\b',
        r'\b(image search|picture search|photo search|search images|search pictures|search photos)\b',
        r'\b(can i see|let me see|i want to see|show what)\b.{0,65}\b(looks? like|images?|pictures?|photos?|photographs?)\b',
        r'\bwhat does\b.{1,120}\blook like\b',
        r'\b(can you|could you|i want to|i need to)\b.{0,70}\b(see|view)\b.{0,70}\b(what|how|picture|image)\b',
        r'\b(photos?|photographs?|pictures?|images?)\s+(of|for)\b',
        r'\b(show|illustrate|visuali[sz]e|draw)\b.{0,70}\b(picture|image|photo|photograph|illustration)\b',
        r'\b(examples? of|types of|identify|recognise|recognize)\b.{0,80}\b(animal|bird|flower|plant|tree|mushroom|fish|insect|car|vehicle|landmark|building|architecture|flag|outfit|clothing|food|fruit|vegetable|planet|galaxy|dinosaur|painting|sculpture|artwork|logo|furniture|instrument|rock|mineral|constellation)\b',
    )
    if any(re.search(p, m, re.I) for p in patterns):
        return True
    # Image-aware learning: search visuals for concrete subjects when seeing them is useful,
    # but avoid turning abstract, coding, arithmetic, or purely conversational requests into image searches.
    concrete = r'\b(animals?|birds?|flowers?|plants?|trees?|mushrooms?|fish|insects?|cars?|vehicles?|landmarks?|buildings?|architecture|flags?|traditional clothing|outfits?|food|fruits?|vegetables?|planets?|galaxies|solar system|dinosaurs?|paintings?|sculptures?|artworks?|logos?|furniture|musical instruments?|minerals?|rocks?|constellations|maps?|cities|countries|capital cities|tourist attractions|monuments|famous people|public figures|scientists|artists|athletes|human body|anatomy|water cycle|rock cycle|photosynthesis|volcanoes|earth layers|cells)\b'
    visual_learning = r'\b(what is|explain|teach me about|learn about|compare|different kinds of|types of|examples of|identify|recognize|recognise|show me|tell me about)\b'
    nonvisual = r'\b(code|programming|debug|calculate|convert|solve|equation|proof|grammar|translate|summari[sz]e this text)\b'
    if re.search(concrete,m,re.I) and re.search(visual_learning,m,re.I) and not re.search(nonvisual,m,re.I):
        return True
    # Proper-noun subjects often name a landmark, city, artwork, product or public figure.
    named_subject = re.search(r"\b(?:tell me about|what is|explain|learn about|show me|compare)\s+(?:the\s+)?([A-Z][A-Za-z0-9'-]+(?:\s+[A-Z][A-Za-z0-9'-]+){0,4})", m)
    return bool(named_subject and not re.search(nonvisual,m,re.I))

def image_search_query_for(message):
    """Turn a chat-style image request into a short, useful image-engine query."""
    q = re.sub(r'\s+', ' ', str(message or '')).strip()
    look_like = re.match(r'^(?:please\s+)?what does\s+(.+?)\s+look like[?.!]*$', q, re.I)
    if look_like:
        q = look_like.group(1)
    # Strip a conversational opener and one search action, including combinations such as
    # "Can you find photographs of..." or "Please show me a picture of...".
    q = re.sub(r'^[\s,.:;!?]*(?:hey\s+juno[, ]*)?(?:please\s+)?(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?(?:tell\s+me\s+about|teach\s+me\s+about|learn\s+about|explain(?:\s+to\s+me)?|what\s+is|show(?:\s+me)?|find(?:\s+me)?|search(?:\s+for)?|look\s+for|get(?:\s+me)?|bring(?:\s+me)?|provide(?:\s+me(?:\s+with)?)?|give(?:\s+me)?|fetch(?:\s+me)?|display)\s+', '', q, flags=re.I)
    q = re.sub(r'\b(?:some )?(?:images?|pictures?|photos?|photographs?|visuals?)\s+(?:of|for)\b', ' ', q, flags=re.I)
    q = re.sub(r'\b(?:images?|pictures?|photos?|photographs?|visuals?)\b', ' ', q, flags=re.I)
    q = re.sub(r'\b(?:showing|featuring|that show|that look like|please|thanks|thank you)\b', ' ', q, flags=re.I)
    q = re.sub(r'\s+', ' ', q).strip(' \t,.:;!?-')
    while re.match(r'^(?:a|an|the)\s+', q, re.I):
        q = re.sub(r'^(?:a|an|the)\s+', '', q, count=1, flags=re.I)
    if not q:
        q = str(message or '').strip()
    return q[:180]

def map_request_for(message):
    """Recognize explicit map/location requests and return a clean destination query."""
    text=re.sub(r'\s+',' ',str(message or '')).strip()
    lead=r'^(?:(?:please|can you|could you|would you)\s+)?'
    patterns=[
        lead+r"(?:show|open|display|view|give me|find|put)\s+(?:me\s+)?(?:a\s+)?map\s+(?:of|for|to|showing)\s+(.+)$",
        lead+r"(?:show|put|locate|find)\s+(.+?)\s+(?:on|in)\s+(?:the\s+)?map$",
        lead+r"map\s+(?:of|for|to)\s+(.+)$",
        lead+r"(?:give me|show me|find)\s+(?:directions|navigation|a route)\s+(?:to|for)\s+(.+)$",
        lead+r"directions?\s+to\s+(.+)$",
        lead+r"(?:navigate|give directions|route)\s+(?:me\s+)?(?:to|for)\s+(.+)$",
        lead+r"how do i get to\s+(.+)$",
        lead+r"(?:take me|navigate me|route me)\s+to\s+(.+)$",
        lead+r"(?:can you\s+)?(?:show|find|open)\s+(?:the\s+)?location\s+(?:of|for)\s+(.+)$",
        lead+r"(?:find|show me|locate)\s+(?:some\s+)?(?:nearby|near me)\s+(.+)$",
        lead+r"(?:find|show me)\s+(.+?)\s+(?:near me|nearby)\s*$",
        lead+r"locate\s+(.+?)\s+(?:on|using)\s+(?:the\s+)?map$",
        lead+r"where is\s+(.+)$",
        lead+r"where am i[?!.]*$",
    ]
    for pat in patterns:
        m=re.match(pat,text,re.I)
        if m:
            q=((m.group(1) if m.lastindex else None) or 'my current location').strip(' .?!')
            q=re.sub(r'\s+(?:from|starting at)\s+(?:my current location|my location|here|there)$','',q,flags=re.I).strip()
            if not re.search(r'\b(directions?|navigation|route|navigate|get to)\b',text,re.I) and re.search(r'\b(?:near me|nearby|around me)\b',text,re.I) and not re.search(r'\b(?:near me|nearby|around me)\b',q,re.I):
                q=(q+' near me').strip()
            if q and len(q)<=180 and not re.search(r'\b(?:my code|this equation|this sentence)\b',q,re.I):
                return {'query':q,'directions':bool(re.search(r'\b(directions?|navigation|route|navigate|get to)\b',text,re.I)), 'nearby':bool(re.search(r'\b(near me|nearby|around me|my current location|my location)\b',q,re.I))}
    return None

def geocode_place(query):
    """Use OSM Nominatim for an on-demand geocode, cache results and respect its public rate limit."""
    global NOMINATIM_LAST
    cache_key=re.sub(r'\s+',' ',str(query or '')).strip().lower()[:180]
    if not cache_key: return None
    if cache_key in MAP_CACHE: return MAP_CACHE[cache_key]
    try:
        with NOMINATIM_LOCK:
            wait=max(0,1.1-(time.monotonic()-NOMINATIM_LAST))
            if wait: time.sleep(wait)
            r=requests.get('https://nominatim.openstreetmap.org/search',params={'q':query,'format':'jsonv2','limit':1},headers={'User-Agent':'JUNO-AI/31.1 (map lookup; on-demand user request)'},timeout=7)
            NOMINATIM_LAST=time.monotonic()
        r.raise_for_status(); rows=r.json()
        if not rows: MAP_CACHE[cache_key]=None; return None
        row=rows[0]; lat=float(row['lat']); lon=float(row['lon'])
        if not (-90<=lat<=90 and -180<=lon<=180): return None
        result={'name':str(row.get('display_name') or query)[:220],'lat':lat,'lon':lon}
        MAP_CACHE[cache_key]=result
        if len(MAP_CACHE)>250: MAP_CACHE.pop(next(iter(MAP_CACHE)))
        return result
    except Exception as e:
        log.info('Map geocoding unavailable (%s).',type(e).__name__)
        return None

def map_card_for(message, origin=None):
    if setting('allow_maps','1')!='1': return None
    intent=map_request_for(message)
    if not intent: return None
    query=intent['query']
    nearby=(intent['nearby'] or bool(re.search(r'\b(near me|nearby|around me|where am i)\b',message,re.I))) and not intent['directions']
    # If the user explicitly requested nearby/current-location help and allowed browser geolocation,
    # center on their browser-supplied coordinates. No location is inferred from IP.
    if nearby and isinstance(origin,dict):
        try:
            lat=float(origin.get('lat')); lon=float(origin.get('lon'))
            if not (-90<=lat<=90 and -180<=lon<=180): raise ValueError()
            place={'name':'Your shared current location','lat':lat,'lon':lon}
            if re.search(r'\b(where am i|my current location|my location)\b',query,re.I):
                maps_url='https://www.google.com/maps/search/?api=1&query='+requests.utils.quote(f'{lat},{lon}')
                label='Open your location in Google Maps'
            else:
                maps_url='https://www.google.com/maps/search/?api=1&query='+requests.utils.quote(query)
                label='Search '+query+' on Google Maps'
        except Exception:
            place=geocode_place(query); maps_url='https://www.google.com/maps/search/?api=1&query='+requests.utils.quote(query); label='Open in Google Maps'
    else:
        place=geocode_place(query)
        maps_url='https://www.google.com/maps/dir/?api=1&destination='+requests.utils.quote(query)+'&travelmode=driving' if intent['directions'] else 'https://www.google.com/maps/search/?api=1&query='+requests.utils.quote(query)
        label='Open directions in Google Maps' if intent['directions'] else 'Open in Google Maps'
    if not place: return {'query':query,'error':'The map provider could not find that place right now. Try a more specific place or address.','google_url':maps_url,'label':label}
    lat,lon=place['lat'],place['lon']; pad_lat=0.012; pad_lon=0.018
    bbox=f'{lon-pad_lon}%2C{lat-pad_lat}%2C{lon+pad_lon}%2C{lat+pad_lat}'
    embed=f'https://www.openstreetmap.org/export/embed.html?bbox={bbox}&layer=mapnik&marker={lat}%2C{lon}'
    if intent['directions'] and isinstance(origin,dict):
        try:
            olat=float(origin.get('lat')); olon=float(origin.get('lon'))
            if -90<=olat<=90 and -180<=olon<=180:
                maps_url='https://www.google.com/maps/dir/?api=1&origin='+requests.utils.quote(f'{olat},{olon}')+'&destination='+requests.utils.quote(f'{lat},{lon}')+'&travelmode=driving'
        except Exception: pass
    return {'query':query,'name':place['name'],'lat':lat,'lon':lon,'embed_url':embed,'google_url':maps_url,'label':label,'attribution':'Map data © OpenStreetMap contributors'}

@app.post('/api/tools/check-code')
@auth_required
def check_code():
    if setting('allow_code_tools','1')!='1': return jsonify({'error':'Code checking and file tools are disabled by the administrator.'}),403
    d=request.get_json(silent=True) or {}; language=str(d.get('language','text')).lower().strip(); code=str(d.get('code',''))
    max_mb=max(1,min(1024,int(setting('code_file_max_mb','1') or 1)))
    if len(code.encode('utf-8'))>max_mb*1024*1024: return jsonify({'error':f'Code exceeds the administrator limit of {max_mb} MB.'}),413
    try:
        if language in ('python','py','python3'):
            ast.parse(code); message='Python syntax looks valid. Code was parsed, not executed.'
        elif language in ('javascript','js','node','typescript','ts'):
            if language in ('typescript','ts'): return jsonify({'ok':True,'message':'TypeScript file can be downloaded, but no TypeScript compiler is configured for syntax checking.'})
            node=__import__('shutil').which('node')
            if not node: return jsonify({'ok':True,'message':'Node.js is not installed on the JUNO server, so JavaScript syntax checking is unavailable. The code was not executed.'})
            with tempfile.NamedTemporaryFile('w',suffix='.js',encoding='utf-8',delete=False) as f: f.write(code); path=f.name
            try:
                result=subprocess.run([node,'--check',path],capture_output=True,text=True,timeout=4)
                if result.returncode: return jsonify({'ok':False,'message':(result.stderr or result.stdout or 'JavaScript syntax check failed')[-1800:]})
            finally:
                try: os.unlink(path)
                except OSError: pass
            message='JavaScript syntax looks valid. Code was checked, not executed.'
        elif language in ('html','htm','xml'):
            from html.parser import HTMLParser
            class SafeHTMLParser(HTMLParser): pass
            SafeHTMLParser(convert_charrefs=True).feed(code)
            message='HTML parsed without a basic parser error. This is not a full standards validator.'
        elif language in ('css','scss','less'):
            # A lightweight structural check; no stylesheet code is executed.
            opens=0; quote=None; escaped=False
            for ch in code:
                if escaped: escaped=False; continue
                if ch=='\\': escaped=True; continue
                if quote:
                    if ch==quote: quote=None
                    continue
                if ch in ('"',"'"): quote=ch
                elif ch=='{': opens+=1
                elif ch=='}':
                    opens-=1
                    if opens<0: return jsonify({'ok':False,'message':'CSS has a closing brace without a matching opening brace.'})
            if opens!=0 or quote: return jsonify({'ok':False,'message':'CSS may have an unclosed brace or quoted string.'})
            message='CSS basic structure looks valid. This is not a full CSS compiler.'
        else:
            return jsonify({'ok':True,'message':f'No syntax checker is configured for {language or "this language"}. You can still download the code as a file.'})
        return jsonify({'ok':True,'message':message})
    except SyntaxError as e:
        return jsonify({'ok':False,'message':f'Syntax error on line {e.lineno}: {e.msg}'})
    except Exception as e:
        log.warning('Code check failed (%s).',type(e).__name__)
        return jsonify({'ok':False,'message':'The syntax check could not be completed.'})

@app.post('/api/uploads')
@auth_required
def upload_image():
    if setting('allow_uploads','1') != '1':
        return jsonify({'error':'Image uploads are disabled by the administrator.'}),403
    uid=session['uid']; cid=str(request.form.get('conversation_id','')).strip()
    files=request.files.getlist('files') or ([request.files['file']] if 'file' in request.files else [])
    if not cid or not files: return jsonify({'error':'Choose one or more images and an active conversation.'}),400
    if len(files)>8: return jsonify({'error':'Attach up to 8 images at a time.'}),400
    max_mb=max(1,min(50,int(setting('upload_max_mb','10') or 10)))
    allowed={'image/jpeg':'.jpg','image/png':'.png','image/webp':'.webp','image/gif':'.gif','image/bmp':'.bmp'}
    signatures={'image/jpeg':lambda b:b.startswith(b'\xff\xd8\xff'),'image/png':lambda b:b.startswith(b'\x89PNG\r\n\x1a\n'),'image/gif':lambda b:b.startswith((b'GIF87a',b'GIF89a')),'image/webp':lambda b:b.startswith(b'RIFF') and b[8:12]==b'WEBP','image/bmp':lambda b:b.startswith(b'BM')}
    validated=[]
    for file in files:
        if not file or not file.filename: continue
        original=secure_filename(file.filename)[:180] or 'image'
        mime=(file.mimetype or mimetypes.guess_type(original)[0] or '').lower()
        if mime not in allowed: return jsonify({'error':f'{original}: upload an image (PNG, JPG, WEBP, GIF or BMP).'}),415
        data=file.read(max_mb*1024*1024+1)
        if len(data)>max_mb*1024*1024: return jsonify({'error':f'{original} exceeds the {max_mb} MB per-image limit.'}),413
        if not data: return jsonify({'error':f'{original} is empty.'}),400
        if not signatures[mime](data): return jsonify({'error':f'{original} does not appear to be a valid image file.'}),415
        validated.append((original,mime,data))
    if not validated: return jsonify({'error':'No usable image files were selected.'}),400
    # Serialize quota checks so simultaneous requests cannot exceed the configured per-user window.
    with DB_LOCK, db() as c:
        conv=c.execute('SELECT id FROM conversations WHERE id=? AND user_id=?',(cid,uid)).fetchone()
        if not conv: return jsonify({'error':'Conversation not found.'}),404
        limit=max(0,int(setting('upload_limit_count','3') or 0)); window=max(1,int(setting('upload_window_minutes','60') or 60))
        cutoff=(datetime.now(timezone.utc)-timedelta(minutes=window)).isoformat(timespec='seconds')
        used=c.execute('SELECT COUNT(*) FROM uploads WHERE user_id=? AND created_at>=?',(uid,cutoff)).fetchone()[0]
        if limit and used+len(validated)>limit:
            return jsonify({'error':f'Upload limit reached: {limit} file(s) per {window} minutes. You have used {used}. Try again after the time window resets.','upload_limit':limit,'upload_window_minutes':window,'uploads_used':used}),429
        created=[]; upload_dir=os.path.join(DATA_DIR,'uploads',str(uid)); os.makedirs(upload_dir,exist_ok=True)
        for original,mime,data in validated:
            upload_id=uuid.uuid4().hex; stored=f'{upload_id}{allowed[mime]}'
            with open(os.path.join(upload_dir,stored),'wb') as out: out.write(data)
            c.execute('INSERT INTO uploads(id,user_id,conversation_id,original_name,stored_name,mime_type,size_bytes,created_at) VALUES(?,?,?,?,?,?,?,?)',(upload_id,uid,cid,original,stored,mime,len(data),now()))
            created.append({'id':upload_id,'name':original,'mime_type':mime,'size_bytes':len(data)})
        c.commit()
    return jsonify({'ok':True,'uploads':created,'uploads_used':used+len(created),'upload_limit':limit,'upload_window_minutes':window,'upload_max_mb':max_mb})

@app.post('/api/suggestions')
@auth_required
def submit_suggestion():
    d=request.get_json(silent=True) or {}; title=str(d.get('title','')).strip()[:120]; details=str(d.get('details','')).strip()[:5000]
    if not title or not details: return jsonify({'error':'Add a short title and explain your suggestion.'}),400
    with db() as c:
        c.execute('INSERT INTO user_suggestions(user_id,title,details,created_at) VALUES(?,?,?,?)',(session['uid'],title,details,now())); c.commit()
    return jsonify({'ok':True,'message':'Thanks — your suggestion was sent to the JUNO admin.'})

@app.post('/api/chat')
@auth_required
def chat():
    d=request.get_json(silent=True) or {}; message=str(d.get('message','')).strip(); cid=str(d.get('conversation_id','')).strip(); model=str(d.get('model','auto')).strip() or 'auto'
    web_mode=bool(d.get('web_search',True))
    force_web=bool(d.get('force_web_search',False))
    image_mode=bool(d.get('image_search',False)) or needs_image_search(message)
    if not message: return jsonify({'error':'Write a message first.'}),400
    if not cid: return jsonify({'error':'Start a new chat first.'}),400
    uid=session['uid']; premium=has_premium(uid)
    with db() as c:
        conv=c.execute('SELECT * FROM conversations WHERE id=? AND user_id=?',(cid,uid)).fetchone()
        if not conv: return jsonify({'error':'Conversation not found.'}),404
        user_count=c.execute("SELECT COUNT(*) FROM messages WHERE conversation_id=? AND role='user'",(cid,)).fetchone()[0]
        limit=int(setting('chat_limit','30') or 30)
        if not premium and limit>0 and user_count>=limit:
            cooldown=int(setting('cooldown_seconds','60') or 60)
            return jsonify({'error':f'You have reached {limit} messages in this chat. Please wait {cooldown}s or start a new chat.','limit_reached':True,'cooldown_seconds':cooldown}),429
        # Recognize explicit in-chat requests to change the preferred name and persist them to Settings.
        name_match=re.search(r"\b(?:please\s+)?(?:call me|you can call me|refer to me as|address me as|my preferred name is|my name is)\s+([A-Za-z][A-Za-z0-9 _'’-]{0,59})\s*[.!?,]*$",message,re.I)
        if name_match:
            preferred=re.sub(r'\s+',' ',name_match.group(1)).strip(' .,!?:;')[:80]
            if preferred and len(preferred.split())<=5:
                c.execute("INSERT INTO user_preferences(user_id,display_name,theme,notifications_enabled,updated_at) VALUES(?,?,?, ?,?) ON CONFLICT(user_id) DO UPDATE SET display_name=excluded.display_name,updated_at=excluded.updated_at",(uid,preferred,'system',1,now()))
                c.execute('UPDATE users SET name=? WHERE id=?',(preferred,uid))
        cur=c.execute('INSERT INTO messages(conversation_id,role,content,created_at) VALUES(?,?,?,?)',(cid,'user',message,now()))
        current_message_id=cur.lastrowid
        attachment_ids=d.get('upload_ids') or []
        if not isinstance(attachment_ids,list): attachment_ids=[]
        attachment_ids=[str(x)[:80] for x in attachment_ids[:8]]
        attached=[]
        if attachment_ids:
            if setting('allow_uploads','1')!='1': return jsonify({'error':'Image uploads are disabled by the administrator.'}),403
            placeholders=','.join('?' for _ in attachment_ids)
            rows=c.execute(f'SELECT * FROM uploads WHERE id IN ({placeholders}) AND user_id=? AND conversation_id=? AND message_id IS NULL',(*attachment_ids,uid,cid)).fetchall()
            for upload in rows:
                path=os.path.join(DATA_DIR,'uploads',str(uid),upload['stored_name'])
                try:
                    with open(path,'rb') as f: encoded=base64.b64encode(f.read()).decode('ascii')
                    attached.append((upload['id'],encoded,upload['original_name']))
                except OSError: log.warning('Uploaded image file is missing: %s',upload['id'])
            if len(attached)!=len(set(attachment_ids)):
                missing=len(set(attachment_ids))-len(attached)
                if missing: log.info('Some upload IDs were unavailable or already used (%s).',missing)
        knowledge_suggestion=detect_knowledge_suggestion(message)
        if knowledge_suggestion:
            c.execute('INSERT INTO user_suggestions(user_id,title,details,status,created_at) VALUES(?,?,?,?,?)',(uid,knowledge_suggestion[0],knowledge_suggestion[1],'new',now()))
        history=c.execute('SELECT role,content FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT 40',(cid,)).fetchall(); knowledge=c.execute('SELECT name,info,tags FROM knowledge WHERE enabled=1 ORDER BY id DESC',( )).fetchall()
        pref=c.execute('SELECT display_name FROM user_preferences WHERE user_id=?',(uid,)).fetchone()
        user_row=c.execute('SELECT name FROM users WHERE id=?',(uid,)).fetchone()
        display_name=((pref['display_name'] if pref else '') or (user_row['name'] if user_row else '') or '').strip()[:80]
        c.commit()
    history=[{'role':r['role'],'content':r['content']} for r in reversed(history)]
    if attached:
        for item in reversed(history):
            if item.get('role')=='user':
                item['images']=[encoded for _,encoded,_ in attached]
                names=', '.join(name for _,_,name in attached)
                item['content']=(item.get('content') or '')+"\n[The user attached image file(s): "+names+". Inspect the image content and answer the user’s question about it.]"
                break
    context=[]; knowledge_confident=False
    if conv['summary']:
        history.insert(0,{'role':'system','content':'Conversation continuity summary from earlier turns. Use it to preserve context, prior decisions and work across AI computers; if it conflicts with the latest user message, follow the latest message.\n'+conv['summary'][-12000:]})
    # Carry concise summaries from the user's other conversations so a new chat can resume earlier work.
    with db() as c:
        other_summaries=c.execute("SELECT title,summary FROM conversations WHERE user_id=? AND id<>? AND summary<>'' ORDER BY updated_at DESC LIMIT 8",(uid,cid)).fetchall()
        private_row=c.execute('SELECT encrypted_content FROM user_private_memory WHERE user_id=?',(uid,)).fetchone()
    if other_summaries:
        continuity='\n\n'.join(f"{r['title']}: {str(r['summary'])[-1400:]}" for r in other_summaries)[:8500]
        history.insert(0,{'role':'system','content':'PRIVATE CROSS-CHAT CONTINUITY (summaries from this same user’s recent chats; use only when relevant, never assume it is current if contradicted):\n'+continuity})
    if private_row:
        try:
            private_text=private_memory_cipher().decrypt(bytes(private_row['encrypted_content'])).decode('utf-8')
            if private_text.strip(): history.insert(0,{'role':'system','content':'USER-OWNED PRIVATE MEMORY (provided by the user in their own settings; confidential, do not repeat unless relevant, do not treat as verified public facts):\n'+private_text})
        except (InvalidToken,ValueError,UnicodeDecodeError):
            log.warning('Private memory could not be decrypted for user %s; not exposing stored ciphertext.',uid)
    if setting('allow_knowledge','1')=='1':
        context, knowledge_confident=match_knowledge(message, knowledge)
    sources=[]; image_results=[]; search_error=None; search_used=False
    explicit_search=force_web or explicit_web_search(message)
    # Search current facts automatically; keep greetings and confident internal knowledge fast.
    automatic_freshness=needs_live_search(message)
    should_web_search=(web_mode and setting('allow_web_search','1')=='1' and (explicit_search or automatic_freshness))
    if should_web_search:
        search_kind='news' if re.search(r'\b(news|breaking|headlines|what happened today|who won|election results)\b',message,re.I) else 'text'
        res=ddg_search(search_query_for(message),search_kind,8); sources=res['results']; search_error=res.get('error'); search_used=True
        if sources:
            history.append({'role':'system','content':'WEB RESEARCH CONTEXT (retrieved just now): Answer the user’s exact question, not merely the general topic. Prioritize official, primary, recent and mutually corroborated sources. Use the result dates when available. Synthesize the evidence in plain language; if a key detail is missing from snippets, say what remains uncertain instead of guessing. Do not display URLs, citations, source lists or a Sources section unless the user explicitly asks for them. Do not treat snippets as full-page content. Results:\n'+'\n'.join(f"- {x['title']} — {x.get('snippet','')} {('Date: '+x['date']) if x.get('date') else ''} (URL: {x['url']})" for x in sources)})
        elif search_error:
            history.append({'role':'system','content':'A live search was attempted but returned no results. Do not invent current facts; briefly state that current information could not be verified.'})
    if image_mode and setting('allow_image_search','1')=='1':
        image_query=image_search_query_for(message)
        img_res=ddg_search(image_query,'images',8)
        # If a conversational query produces poor results, retry once with the subject only.
        image_results=img_res['results']
        search_error=img_res.get('error')
        if not image_results and image_query.lower()!=message.lower():
            retry_img=ddg_search(image_query[:100],'images',8)
            image_results=retry_img.get('results') or []
            search_error=retry_img.get('error')
        search_used=True
        if image_results:
            history.append({'role':'system','content':f'IMAGE SEARCH: The interface will show {len(image_results)} real image result cards for the user query "{image_query}". Do not claim to have generated these images. Briefly introduce the results and, if useful, ask whether the user wants a different style or more specific images. Do not print raw image URLs.'})
        else:
            history.append({'role':'system','content':f'IMAGE SEARCH: No usable image cards were found for "{image_query}". Do not pretend pictures are attached. Tell the user image search did not return usable pictures and suggest a shorter or more specific subject phrase.'})
    map_data=map_card_for(message,d.get('map_origin') if isinstance(d.get('map_origin'),dict) else None)
    if map_data:
        if map_data.get('embed_url'):
            history.append({'role':'system','content':'A map card is attached to this answer using OpenStreetMap. Briefly explain the place/map and refer to the visible map card. A Google Maps link is provided for navigation; do not claim you started navigation.'})
        else:
            history.append({'role':'system','content':'The user requested a map, but the map provider did not find a location. Be transparent and ask for a more specific place/address.'})
    if context: history.insert(0,{'role':'system','content':'Relevant JUNO knowledge base entries:\n'+'\n'.join(context)})
    system=setting('system_prompt','You are JUNO, a helpful and accurate AI assistant.')
    personalization = (f'USER PERSONALIZATION: The name this user chose in Settings is "{display_name}". Remember it during this conversation and answer accurately if asked what name they chose. Use it naturally when directly addressing them, but do not force their name into every reply. Do not confuse their chosen display name with your own name.' if display_name else 'USER PERSONALIZATION: No preferred display name has been set. Do not guess the user name.')
    quality_rules=('JUNO RESPONSE STANDARD: Be fast, sensible, accurate, and directly useful. Answer the exact question in the first paragraph. Use concise wording for simple questions and structured detail for complex or research tasks. Explain unfamiliar concepts with clear examples. For multi-part requests, address every part. For research, synthesize the supplied search evidence rather than dumping it; distinguish confirmed facts, reasonable inference, and unknowns. Never invent statistics, quotations, sources, dates, or capabilities. If web results are supplied, use them over stale memory; if no useful results were returned, answer from reliable model knowledge and briefly flag only material uncertainty. Ask a follow-up only when an essential detail is missing. Avoid generic filler and unnecessary disclaimers. VISUAL REPRESENTATIONS: When a visual would genuinely improve an explanation (for example motion, acceleration, forces, a process, a comparison or simple data), prefer one safe visual specification in a fenced block labelled `juno-visual` when the user explicitly asks for a representation or a physical-process explanation would benefit from it. The block must contain JSON only and must never contain HTML, JavaScript, SVG markup or executable code. Supported schemas: {\"type\":\"motion\",\"title\":\"Acceleration\",\"description\":\"A vehicle speeds up to the right\",\"velocity\":\"increasing\",\"acceleration\":\"right\"}; {\"type\":\"bar-chart\",\"title\":\"Example\",\"items\":[{\"label\":\"A\",\"value\":4},{\"label\":\"B\",\"value\":7}]}; {\"type\":\"flow\",\"title\":\"Process\",\"steps\":[\"Step 1\",\"Step 2\",\"Step 3\"]}; or {\"type\":\"concept\",\"title\":\"Concept\",\"description\":\"Short explanation\",\"items\":[\"Idea one\",\"Idea two\",\"Idea three\"]}. Keep the normal explanation too; do not add a visual block for every simple question. If a visual block is unnecessary or uncertain, explain in text instead. KNOWLEDGE SUGGESTIONS: If the user asks to add information to JUNO’s knowledge base, tell them their request has been submitted as a suggestion for administrator review; do not claim it was added to the permanent knowledge base automatically. MATH AND SCIENTIFIC FORMATTING: Render mathematical expressions with standard LaTeX delimiters: use \\( ... \\) for inline math and separate \\[ ... \\] lines for display equations. Use correct LaTeX commands for fractions, roots, powers, subscripts, Greek letters, sums, integrals, limits, vectors, matrices, piecewise cases, inequalities, set theory, logic, calculus, probability, statistics, units and scientific notation. Examples include \\cup, \\cap, \\in, \\notin, \\subseteq, \\forall, \\exists, \\emptyset, \\mathbb{R}, \\sum, \\int, \\partial, \\nabla, \\approx, \\neq, \\leq and \\geq. Use aligned equations for multi-step working and \\boxed{...} for a clearly marked final result when useful. Keep explanations in ordinary text around equations. Preserve literal Unicode math symbols and superscripts where appropriate. Put ASCII/text diagrams in fenced code blocks so spacing remains aligned. Do not put math delimiters inside code blocks unless showing literal code.')
    if knowledge_suggestion:
        history.append({'role':'system','content':'KNOWLEDGE SUGGESTION NOTICE: The user explicitly asked to add or suggest information for JUNO knowledge. A suggestion has been saved for admin review. Confirm that it is pending review; do not claim it has already been added to the permanent knowledge base.'})
    history.insert(0,{'role':'system','content':system+'\n\nCURRENT DATE: '+datetime.now(timezone.utc).strftime('%Y-%m-%d')+'. Treat this as the current date; do not claim this date or earlier dates are in the future.\n\n'+personalization+'\n\n'+quality_rules + ('\nIMAGE UNDERSTANDING: The user attached image(s). Carefully inspect all attached images, describe visible details accurately, read legible text when possible, and answer the user’s specific question. If the selected model cannot interpret images, state that limitation clearly instead of guessing.' if attached else '')})
    excluded=set(); selected=None; started=time.time(); answer=''; err=None
    cloud_result = cloud_chat_completion(history, model) if not attached else None
    if cloud_result:
        answer, chosen_model = cloud_result
        worker = {'id': None, 'name': 'Cloud AI'}
    for attempt in range(2) if not answer else ():
        selected=select_worker(model,excluded,vision=bool(attached))
        if not selected: break
        worker,chosen_model,probe_time=selected
        with WORKER_LOCK: WORKER_BUSY.add(worker['id'])
        try:
            url=f"http://{worker['ip']}:{worker['port']}/api/chat"
            resp=requests.post(url,json={'model':chosen_model,'messages':history,'stream':False,'options':{'num_ctx':8192}},timeout=OLLAMA_TIMEOUT)
            resp.raise_for_status(); payload=resp.json(); answer=(payload.get('message') or {}).get('content','').strip()
            if not answer: raise RuntimeError('The model returned an empty response.')
            break
        except Exception as e:
            log.warning('Worker %s request failed: %s',worker['name'],e); excluded.add(worker['id']); err=e; selected=None
        finally:
            with WORKER_LOCK: WORKER_BUSY.discard(worker['id'])
    # If the model reveals that its answer is stale/uncertain, search and retry once.
    if answer and (not search_used or not sources) and web_mode and setting('allow_web_search','1')=='1' and answer_signals_stale_or_uncertain(answer):
        retry_search=ddg_search(search_query_for(message), 'news' if re.search(r'\b(news|breaking|who won|world cup|score|goals?)\b',message,re.I) else 'text', 7)
        retry_sources=retry_search.get('results') or []
        if retry_sources:
            search_used=True
            retry_history=list(history)
            retry_history.append({'role':'system','content':'Your initial draft was stale or uncertain. Use these freshly retrieved results to answer the original question directly. Prefer current dated evidence, do not mention a model knowledge cutoff, and do not show URLs or a Sources section unless requested. Results:\n'+'\n'.join(f"- {x['title']} — {x.get('snippet','')} (URL: {x['url']})" for x in retry_sources)})
            retry_worker=select_worker(model,set(),vision=bool(attached))
            if retry_worker:
                rw,rm,_=retry_worker
                with WORKER_LOCK: WORKER_BUSY.add(rw['id'])
                try:
                    rr=requests.post(f"http://{rw['ip']}:{rw['port']}/api/chat",json={'model':rm,'messages':retry_history,'stream':False,'options':{'num_ctx':8192}},timeout=OLLAMA_TIMEOUT)
                    rr.raise_for_status(); fresh=((rr.json().get('message') or {}).get('content') or '').strip()
                    if fresh: answer=fresh; worker,chosen_model=rw,rm
                except Exception as e:
                    log.warning('Freshness retry failed: %s',e)
                finally:
                    with WORKER_LOCK: WORKER_BUSY.discard(rw['id'])
    elapsed=time.time()-started
    if not answer:
        answer=('JUNO cannot inspect this image yet because no available AI computer has a recognized vision-capable Ollama model installed. Ask the administrator to install a vision model such as Gemma 3/4, Qwen VL, or LLaVA.' if attached else 'JUNO is temporarily unavailable. Please try again shortly.')
        with db() as c:
            c.execute('INSERT INTO messages(conversation_id,role,content,created_at) VALUES(?,?,?,?)',(cid,'assistant',answer,now()))
            c.execute('INSERT INTO request_stats(user_id,conversation_id,success,response_time,created_at) VALUES(?,?,?,?,?)',(uid,cid,0,elapsed,now())); c.commit()
        return jsonify({'answer':answer,'error':'JUNO is temporarily unavailable. Please try again shortly.','sources':[],'images':image_results,'map':map_data,'search_error':None,'search_used':search_used,'knowledge_suggestion_submitted':bool(knowledge_suggestion)}),503
    with db() as c:
        c.execute('INSERT INTO messages(conversation_id,role,content,model,computer_id,response_time,created_at) VALUES(?,?,?,?,?,?,?)',(cid,'assistant',answer,chosen_model,worker['id'],elapsed,now()))
        for upload_id,_,_ in attached:
            c.execute('UPDATE uploads SET message_id=? WHERE id=? AND user_id=?',(current_message_id,upload_id,uid))
        c.execute("UPDATE conversations SET title=CASE WHEN title='New chat' THEN ? ELSE title END,updated_at=?,summary=? WHERE id=?",(smart_conversation_title(message,answer),now(),(conv['summary']+'\nUser: '+message[:700]+'\nJUNO: '+answer[:1200])[-12000:],cid))
        c.execute('INSERT INTO request_stats(user_id,conversation_id,computer_id,model,success,response_time,created_at) VALUES(?,?,?,?,1,?,?)',(uid,cid,worker['id'],chosen_model,elapsed,now())); c.commit()
    return jsonify({'answer':answer,'sources':[],'images':image_results,'map':map_data,'search_error':None,'search_used':search_used,'conversation_id':cid,'knowledge_suggestion_submitted':bool(knowledge_suggestion)})

@app.get('/api/preferences')
@auth_required
def get_preferences():
    with db() as c:
        row=c.execute('SELECT * FROM user_preferences WHERE user_id=?',(session['uid'],)).fetchone()
        u=c.execute('SELECT name FROM users WHERE id=?',(session['uid'],)).fetchone()
    return jsonify(dict(row) if row else {'user_id':session['uid'],'display_name':u['name'] if u else '', 'theme':'system','notifications_enabled':1})

@app.post('/api/preferences')
@auth_required
def save_preferences():
    d=request.get_json(silent=True) or {}; display=str(d.get('display_name','')).strip()[:80]; theme=d.get('theme','system'); notify=int(bool(d.get('notifications_enabled',True)))
    if theme not in ('system','light','dark'): theme='system'
    if setting('allow_theme_choice','1')!='1': theme=setting('default_theme','system')
    with db() as c:
        if display:
            c.execute('UPDATE users SET name=? WHERE id=?',(display,session['uid']))
        c.execute('INSERT INTO user_preferences(user_id,display_name,theme,notifications_enabled,updated_at) VALUES(?,?,?,?,?) ON CONFLICT(user_id) DO UPDATE SET display_name=excluded.display_name,theme=excluded.theme,notifications_enabled=excluded.notifications_enabled,updated_at=excluded.updated_at',(session['uid'],display,theme,notify,now()))
        c.commit()
    return jsonify({'ok':True,'display_name':display,'theme':theme,'notifications_enabled':bool(notify)})

@app.get('/api/private-memory')
@auth_required
def get_private_memory():
    # This endpoint only returns the signed-in user's own memory. Admin endpoints intentionally never query this table.
    with db() as c:
        row=c.execute('SELECT encrypted_content,updated_at FROM user_private_memory WHERE user_id=?',(session['uid'],)).fetchone()
    if not row: return jsonify({'content':'','updated_at':None,'max_bytes':PRIVATE_MEMORY_MAX_BYTES})
    try:
        content=private_memory_cipher().decrypt(bytes(row['encrypted_content'])).decode('utf-8')
    except (InvalidToken,ValueError,UnicodeDecodeError):
        return jsonify({'error':'Your private memory could not be decrypted. Check JUNO_PRIVATE_MEMORY_KEY configuration.'}),500
    return jsonify({'content':content,'updated_at':row['updated_at'],'max_bytes':PRIVATE_MEMORY_MAX_BYTES})

@app.put('/api/private-memory')
@auth_required
def save_private_memory():
    d=request.get_json(silent=True) or {}
    content=d.get('content','')
    if not isinstance(content,str): return jsonify({'error':'Private memory must be text.'}),400
    size=len(content.encode('utf-8'))
    if size>PRIVATE_MEMORY_MAX_BYTES:
        return jsonify({'error':'Private memory is limited to 1 MiB (1,048,576 bytes).','max_bytes':PRIVATE_MEMORY_MAX_BYTES,'actual_bytes':size}),413
    encrypted=private_memory_cipher().encrypt(content.encode('utf-8'))
    with db() as c:
        if content.strip():
            c.execute('INSERT INTO user_private_memory(user_id,encrypted_content,updated_at) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET encrypted_content=excluded.encrypted_content,updated_at=excluded.updated_at',(session['uid'],encrypted,now()))
        else:
            c.execute('DELETE FROM user_private_memory WHERE user_id=?',(session['uid'],))
        c.commit()
    return jsonify({'ok':True,'bytes':size,'max_bytes':PRIVATE_MEMORY_MAX_BYTES,'updated_at':now(),'message':'Private memory saved. It is encrypted in the database and is not shown in the admin console.'})

@app.get('/api/settings')
@auth_required
def get_settings():
    keys=['allow_web_search','allow_image_search','allow_voice_input','allow_knowledge','allow_theme_choice','allow_uploads','allow_maps','allow_code_tools','code_file_max_mb','upload_limit_count','upload_window_minutes','upload_max_mb','default_theme','chat_limit','cooldown_seconds','premium_enabled']
    return jsonify({k:setting(k) for k in keys})

@app.get('/api/admin/overview')
@admin_required
def admin_overview():
    with db() as c:
        total=c.execute('SELECT COUNT(*) FROM users').fetchone()[0]; online=c.execute("SELECT COUNT(*) FROM users WHERE last_seen>=? AND active=1",((datetime.now(timezone.utc)-timedelta(minutes=3)).isoformat(timespec='seconds'),)).fetchone()[0]
        requests_total=c.execute('SELECT COUNT(*) FROM request_stats').fetchone()[0]; chats=c.execute('SELECT COUNT(*) FROM conversations').fetchone()[0]
        avg=c.execute('SELECT AVG(response_time) FROM request_stats WHERE success=1').fetchone()[0] or 0
        users=[dict(r) for r in c.execute('SELECT id,name,email,is_admin,active,created_at,last_seen,(SELECT COUNT(*) FROM conversations x WHERE x.user_id=users.id) chats,(SELECT COUNT(*) FROM request_stats s WHERE s.user_id=users.id) requests FROM users ORDER BY id DESC LIMIT 300')]
        workers=[dict(r) for r in c.execute('SELECT * FROM computers ORDER BY priority,id')]
        settings={r['key']:r['value'] for r in c.execute('SELECT key,value FROM settings')}
        notes=[dict(r) for r in c.execute('SELECT * FROM notifications ORDER BY id DESC LIMIT 100')]
        knowledge=[dict(r) for r in c.execute('SELECT * FROM knowledge ORDER BY id DESC')]
        invites=[dict(r) for r in c.execute('SELECT id,assigned_name,used_by,used_at,expires_at,created_at FROM invitation_codes ORDER BY id DESC LIMIT 100')]
        premium=[dict(r) for r in c.execute('SELECT id,label,enabled,expires_at,created_at FROM premium_codes ORDER BY id DESC LIMIT 100')]
        suggestions=[dict(r) for r in c.execute('SELECT s.*,u.name AS user_name,u.email AS user_email FROM user_suggestions s JOIN users u ON u.id=s.user_id ORDER BY s.id DESC LIMIT 300')]
        cloud_keys=[{'id':r['id'],'name':r['name'],'base_url':r['base_url'],'model':r['model'],'enabled':bool(r['enabled']),'priority':r['priority'],'status':r['status'],'last_tested':r['last_tested'],'created_at':r['created_at']} for r in c.execute('SELECT id,name,base_url,model,enabled,priority,status,last_tested,created_at FROM cloud_provider_keys ORDER BY priority,id')]
    storage = {
        'knowledge_count': len(knowledge),
        'database_exists': os.path.isfile(DB_PATH),
        'database_bytes': os.path.getsize(DB_PATH) if os.path.isfile(DB_PATH) else 0,
        'database_path': DB_PATH,
        'data_dir': DATA_DIR,
        'persistent_disk_detected': bool(os.getenv('RENDER')) and os.path.isdir('/var/data') and os.path.ismount('/var/data'),
        'render_environment': bool(os.getenv('RENDER')),
    }
    return jsonify({'stats':{'users':total,'online':online,'requests':requests_total,'chats':chats,'avg_response_time':round(avg,2)},'users':users,'computers':workers,'settings':settings,'notifications':notes,'knowledge':knowledge,'invitations':invites,'premium_codes':premium,'suggestions':suggestions,'cloud_keys':cloud_keys,'storage':storage})

@app.post('/api/admin/suggestions/<int:sid>')
@admin_required
def admin_suggestion_update(sid):
    d=request.get_json(silent=True) or {}; status=str(d.get('status','reviewed'))
    if status not in ('new','reviewed','implemented','dismissed'): return jsonify({'error':'Invalid suggestion status.'}),400
    with db() as c:
        cur=c.execute('UPDATE user_suggestions SET status=? WHERE id=?',(status,sid)); c.commit()
        if not cur.rowcount: return jsonify({'error':'Suggestion not found.'}),404
    return jsonify({'ok':True})

@app.post('/api/admin/users/<int:uid>')
@admin_required
def admin_user_update(uid):
    d=request.get_json(silent=True) or {}; action=d.get('action')
    if uid==session.get('uid') and action in ('block','remove_admin'): return jsonify({'error':'You cannot disable your own current admin account.'}),400
    with db() as c:
        u=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
        if not u: return jsonify({'error':'User not found.'}),404
        if action=='block': c.execute('UPDATE users SET active=0,session_version=session_version+1 WHERE id=?',(uid,))
        elif action=='restore': c.execute('UPDATE users SET active=1,session_version=session_version+1 WHERE id=?',(uid,))
        elif action=='make_admin': c.execute('UPDATE users SET is_admin=1,session_version=session_version+1 WHERE id=?',(uid,))
        elif action=='remove_admin': c.execute('UPDATE users SET is_admin=0,session_version=session_version+1 WHERE id=?',(uid,))
        elif action=='delete': c.execute('DELETE FROM users WHERE id=?',(uid,))
        else: return jsonify({'error':'Unknown action.'}),400
        c.commit()
    return jsonify({'ok':True})

def validate_cloud_provider_input(d):
    name = str(d.get('name', '')).strip()[:80]
    base_url = str(d.get('base_url', '')).strip().rstrip('/')
    model_name = str(d.get('model', '')).strip()[:180]
    api_key = str(d.get('api_key', '')).strip()
    try: priority = max(1, min(10000, int(d.get('priority', 50) or 50)))
    except (ValueError, TypeError): raise ValueError('Priority must be a whole number.')
    parsed = urlparse(base_url)
    if not name or not model_name or not base_url: raise ValueError('Provider name, API base URL, and model name are required.')
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Use a valid HTTPS API base URL without embedded credentials or query parameters.')
    if not api_key: raise ValueError('Enter the API key.')
    if len(api_key) > 4096: raise ValueError('The API key is too long.')
    return name, base_url, model_name, api_key, priority


@app.post('/api/admin/cloud-keys')
@admin_required
def admin_cloud_key_save():
    d = request.get_json(silent=True) or {}
    try: name, base_url, model_name, api_key, priority = validate_cloud_provider_input(d)
    except (ValueError, TypeError) as exc: return jsonify({'error': str(exc) or 'Check the provider details.'}), 400
    encrypted = cloud_key_cipher().encrypt(api_key.encode('utf-8')); stamp = now()
    try:
        with db() as c:
            if d.get('id'):
                try: provider_id = int(d['id'])
                except (ValueError, TypeError): return jsonify({'error':'Invalid provider ID.'}),400
                if not c.execute('SELECT id FROM cloud_provider_keys WHERE id=?',(provider_id,)).fetchone(): return jsonify({'error':'Cloud provider not found.'}),404
                c.execute('UPDATE cloud_provider_keys SET name=?,base_url=?,model=?,encrypted_api_key=?,enabled=?,priority=?,status=?,updated_at=? WHERE id=?',
                    (name,base_url,model_name,encrypted,int(bool(d.get('enabled',True))),priority,'untested',stamp,provider_id))
            else:
                c.execute('INSERT INTO cloud_provider_keys(name,base_url,model,encrypted_api_key,enabled,priority,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                    (name,base_url,model_name,encrypted,int(bool(d.get('enabled',True))),priority,'untested',stamp,stamp))
            c.commit()
    except sqlite3.IntegrityError: return jsonify({'error':'A cloud provider with that name already exists.'}),409
    log.info('Cloud provider configuration saved; API key not logged.')
    return jsonify({'ok':True})


@app.post('/api/admin/cloud-keys/<int:key_id>/test')
@admin_required
def admin_cloud_key_test(key_id):
    with db() as c: row=c.execute('SELECT * FROM cloud_provider_keys WHERE id=?',(key_id,)).fetchone()
    if not row: return jsonify({'error':'Cloud provider not found.'}),404
    try:
        api_key=cloud_key_cipher().decrypt(bytes(row['encrypted_api_key'])).decode('utf-8')
        base=row['base_url'].rstrip('/'); models_url=base if base.endswith('/models') else base+'/models'
        started=time.time()
        try:
            resp=requests.get(models_url,headers={'Authorization':f'Bearer {api_key}'},timeout=12); resp.raise_for_status()
        except Exception:
            # Some compatible providers expose chat completions but not a models-list endpoint.
            chat_url=base if base.endswith('/chat/completions') else base+'/chat/completions'
            resp=requests.post(chat_url,headers={'Authorization':f'Bearer {api_key}','Content-Type':'application/json'},
                json={'model':row['model'],'messages':[{'role':'user','content':'Reply with OK.'}],'max_tokens':5},timeout=15)
            resp.raise_for_status()
        status='online'; elapsed=round(time.time()-started,3); detail='API key accepted; provider endpoint responded.'
    except Exception as exc:
        status='error'; elapsed=None; detail='Connection test failed. Check the endpoint, key, and model in the provider dashboard.'
        log.warning('Cloud provider connection test failed (%s).',type(exc).__name__)
    with db() as c: c.execute('UPDATE cloud_provider_keys SET status=?,last_tested=?,updated_at=? WHERE id=?',(status,now(),now(),key_id)); c.commit()
    return jsonify({'ok':status=='online','status':status,'response_time':elapsed,'message':detail})


@app.post('/api/admin/cloud-keys/<int:key_id>/toggle')
@admin_required
def admin_cloud_key_toggle(key_id):
    d=request.get_json(silent=True) or {}
    with db() as c:
        if not c.execute('SELECT id FROM cloud_provider_keys WHERE id=?',(key_id,)).fetchone(): return jsonify({'error':'Cloud provider not found.'}),404
        c.execute('UPDATE cloud_provider_keys SET enabled=?,updated_at=? WHERE id=?',(int(bool(d.get('enabled'))),now(),key_id)); c.commit()
    return jsonify({'ok':True})


@app.delete('/api/admin/cloud-keys/<int:key_id>')
@admin_required
def admin_cloud_key_delete(key_id):
    with db() as c:
        cur=c.execute('DELETE FROM cloud_provider_keys WHERE id=?',(key_id,)); c.commit()
        if not cur.rowcount: return jsonify({'error':'Cloud provider not found.'}),404
    return jsonify({'ok':True})


@app.post('/api/admin/computers')
@admin_required
def admin_computer_save():
    d=request.get_json(silent=True) or {}; name=str(d.get('name','')).strip(); ip=str(d.get('ip','')).strip(); port=int(d.get('port',11434)); weight=max(0,min(100,int(d.get('weight',50)))); priority=int(d.get('priority',50)); pin=str(d.get('model_pin','')).strip()
    if not name or not ip or not (1<=port<=65535): return jsonify({'error':'Name, IP/host and valid port are required.'}),400
    try:
        with db() as c:
            if d.get('id'):
                c.execute('UPDATE computers SET name=?,ip=?,port=?,enabled=?,weight=?,priority=?,model_pin=?,updated_at=? WHERE id=?',(name,ip,port,int(bool(d.get('enabled',True))),weight,priority,pin,now(),int(d['id'])))
            else: c.execute('INSERT INTO computers(name,ip,port,enabled,weight,priority,model_pin,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',(name,ip,port,int(bool(d.get('enabled',True))),weight,priority,pin,now(),now()))
            c.commit()
    except sqlite3.IntegrityError: return jsonify({'error':'That IP and port are already configured.'}),409
    return jsonify({'ok':True})

@app.delete('/api/admin/computers/<int:wid>')
@admin_required
def admin_computer_delete(wid):
    with db() as c: c.execute('DELETE FROM computers WHERE id=?',(wid,)); c.commit()
    return jsonify({'ok':True})

@app.post('/api/admin/settings')
@admin_required
def admin_settings_save():
    d=request.get_json(silent=True) or {}
    allowed={'allow_web_search','allow_image_search','allow_voice_input','allow_knowledge','allow_theme_choice','allow_uploads','allow_maps','allow_code_tools','code_file_max_mb','upload_limit_count','upload_window_minutes','upload_max_mb','default_theme','chat_limit','cooldown_seconds','premium_enabled','routing_mode','system_prompt'}
    for k,v in d.items():
        if k not in allowed: continue
        if k=='upload_limit_count': v=max(0,min(1000,int(v or 0)))
        elif k=='code_file_max_mb': v=max(1,min(1024,int(v or 1)))
        elif k=='upload_window_minutes': v=max(1,min(10080,int(v or 60)))
        elif k=='upload_max_mb': v=max(1,min(50,int(v or 10)))
        put_setting(k,str(v)[:12000])
    return jsonify({'ok':True})

@app.post('/api/admin/notifications')
@admin_required
def admin_notification_create():
    d=request.get_json(silent=True) or {}; title=str(d.get('title','')).strip(); body=str(d.get('body','')).strip(); trigger=d.get('trigger_type','next_login'); value=max(0,int(d.get('trigger_value',0)))
    if not title or not body: return jsonify({'error':'Title and message are required.'}),400
    if trigger not in ('next_login','after_chats','after_minutes'): return jsonify({'error':'Invalid notification trigger.'}),400
    with db() as c: c.execute('INSERT INTO notifications(title,body,enabled,trigger_type,trigger_value,start_at,end_at,created_at,created_by) VALUES(?,?,1,?,?,?,?,?,?)',(title,body,trigger,value,d.get('start_at') or None,d.get('end_at') or None,now(),session['uid'])); c.commit()
    return jsonify({'ok':True})

@app.delete('/api/admin/notifications/<int:nid>')
@admin_required
def admin_notification_delete(nid):
    with db() as c: c.execute('DELETE FROM notifications WHERE id=?',(nid,)); c.commit()
    return jsonify({'ok':True})

@app.post('/api/admin/invitations')
@admin_required
def admin_invitation_create():
    d=request.get_json(silent=True) or {}; name=str(d.get('assigned_name','')).strip(); expiry=d.get('expires_at') or None
    if not name: return jsonify({'error':'Assign a recipient name.'}),400
    code='JUNO-'+secrets.token_urlsafe(9).replace('-','').replace('_','')[:12].upper()
    with db() as c: c.execute('INSERT INTO invitation_codes(code_hash,assigned_name,expires_at,created_at,created_by) VALUES(?,?,?,?,?)',(hash_code(code),name,expiry,now(),session['uid'])); c.commit()
    return jsonify({'ok':True,'code':code,'assigned_name':name})

@app.post('/api/admin/premium-codes')
@admin_required
def admin_premium_create():
    d=request.get_json(silent=True) or {}; label=str(d.get('label','Premium access')).strip() or 'Premium access'; expiry=d.get('expires_at') or None
    code='JUNO-P-'+secrets.token_urlsafe(10).replace('-','').replace('_','')[:14].upper()
    with db() as c: c.execute('INSERT INTO premium_codes(code_hash,label,expires_at,created_at,created_by) VALUES(?,?,?,?,?)',(hash_code(code),label,expiry,now(),session['uid'])); c.commit()
    return jsonify({'ok':True,'code':code,'label':label})

@app.post('/api/admin/premium-codes/<int:code_id>')
@admin_required
def admin_premium_toggle(code_id):
    d=request.get_json(silent=True) or {}
    with db() as c: c.execute('UPDATE premium_codes SET enabled=? WHERE id=?',(int(bool(d.get('enabled'))),code_id));
    with db() as c: c.execute('UPDATE premium_grants SET active=0 WHERE code_id=? AND ?=0',(code_id,int(bool(d.get('enabled'))))); c.commit()
    return jsonify({'ok':True})

@app.get('/api/admin/knowledge/export')
@admin_required
def admin_knowledge_export():
    """Download every knowledge entry as a portable JSON backup."""
    with db() as c:
        rows = [dict(r) for r in c.execute(
            'SELECT name,info,tags,enabled,image_url,image_alt,link_url,link_text,created_at,updated_at FROM knowledge ORDER BY id'
        ).fetchall()]
    payload = {'format': 'juno-knowledge-v1', 'exported_at': now(), 'count': len(rows), 'items': rows}
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    response = app.response_class(body, mimetype='application/json; charset=utf-8')
    response.headers['Content-Disposition'] = 'attachment; filename=juno-knowledge-backup.json'
    response.headers['Cache-Control'] = 'no-store'
    return response


@app.post('/api/admin/knowledge')
@admin_required
def admin_knowledge_save():
    d=request.get_json(silent=True) or {}; name=str(d.get('name','')).strip(); info=str(d.get('info','')).strip()
    if not name or not info: return jsonify({'error':'Name and information are required.'}),400
    with db() as c:
        c.execute('INSERT INTO knowledge(name,info,tags,enabled,image_url,image_alt,link_url,link_text,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(name,info,str(d.get('tags','')),int(bool(d.get('enabled',True))),str(d.get('image_url','')),str(d.get('image_alt','')),str(d.get('link_url','')),str(d.get('link_text','')),now(),now())); c.commit()
    return jsonify({'ok':True})

@app.post('/api/admin/knowledge/import')
@admin_required
def admin_knowledge_import():
    d=request.get_json(silent=True)
    if isinstance(d,dict): d=d.get('items')
    if not isinstance(d,list): return jsonify({'error':'Provide a JSON array of knowledge entries, or an object with an items array.'}),400
    added=0; skipped=0
    with db() as c:
        for item in d:
            if not isinstance(item,dict): skipped+=1; continue
            name=str(item.get('name','')).strip(); info=str(item.get('info',item.get('content',''))).strip()
            if not name or not info: skipped+=1; continue
            c.execute('INSERT INTO knowledge(name,info,tags,enabled,image_url,image_alt,link_url,link_text,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',(name,info,str(item.get('tags','')),int(bool(item.get('enabled',True))),str(item.get('image_url','')),str(item.get('image_alt','')),str(item.get('link_url','')),str(item.get('link_text','')),now(),now())); added+=1
        c.commit()
    return jsonify({'ok':True,'added':added,'skipped':skipped})

@app.delete('/api/admin/knowledge/<int:kid>')
@admin_required
def admin_knowledge_delete(kid):
    with db() as c: c.execute('DELETE FROM knowledge WHERE id=?',(kid,)); c.commit()
    return jsonify({'ok':True})

@app.post('/api/admin/workers/<int:wid>/test')
@admin_required
def admin_worker_test(wid):
    with db() as c: w=c.execute('SELECT * FROM computers WHERE id=?',(wid,)).fetchone()
    if not w: return jsonify({'error':'Computer not found.'}),404
    models,elapsed=probe_worker(dict(w)); return jsonify({'ok':bool(models),'models':models,'response_time':elapsed,'status':'online' if models else 'offline'})

@app.get('/api/health')
def health():
    try:
        with db() as c: registered = c.execute('SELECT 1 FROM cloud_provider_keys WHERE enabled=1 LIMIT 1').fetchone() is not None
    except sqlite3.Error: registered = False
    return jsonify({'ok':True,'service':'JUNO AI V32 Cloud Keys','search':'DDGS metasearch (auto backend, no API key)' if DDGS else 'disabled - install ddgs','search_region':DDGS_REGION,'google_sign_in_configured':bool(GOOGLE_CLIENT_ID),'cloud_ai_configured':registered or bool(JUNO_CLOUD_AI_BASE_URL and JUNO_CLOUD_AI_API_KEY and JUNO_CLOUD_AI_MODEL),'time':now()})

@app.errorhandler(413)
def too_large(_): return jsonify({'error':'Request is too large.'}),413
@app.errorhandler(Exception)
def unhandled(e):
    log.exception('Unhandled application error')
    if request.path.startswith('/api/'): return jsonify({'error':'JUNO encountered an internal error. Check the server log for the technical detail.'}),500
    return 'JUNO encountered an internal error. Check the server log.',500

init_db()
if __name__=='__main__':
    app.run(host='0.0.0.0',port=PORT,debug=False,threaded=True)
