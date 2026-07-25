#!/usr/bin/env python3
"""
IPTVScraper - Scraping di siti IPTV e generazione playlist M3U Italia
"""

import os, re, sys, time, sqlite3, logging, hashlib, subprocess, html, concurrent.futures
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse, quote
from configparser import ConfigParser

import requests
from bs4 import BeautifulSoup

# ───────────────────────── CONFIGURAZIONE ─────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Stati is_working per canali: 0=non funzionante, 1=funzionante, 2=non testato
CH_WORKING = 1
CH_DEAD = 0
CH_UNKNOWN = 2

ITALIAN_KEYWORDS = [
    'rai', 'rai 1', 'rai 2', 'rai 3', 'rai 4', 'rai movie', 'rai premium',
    'rai news', 'rai sport', 'rai storia', 'rai scuola', 'rai radio',
    'rai yo-yo', 'rai yoyo', 'rai gulp', 'rai 5', 'mediaset', 'canale 5',
    'italia 1', 'italia 2', 'rete 4', 'rete 4 hd', 'la7', 'la7d',
    'tv8', 'nove', 'boing', 'cartoonito', 'iris', 'twentyseven',
    'cielo', 'supertennis', 'sportitalia', 'sportitalia 2',
    'italia', 'italian', 'italy', 'tgcom24', 'radio italia',
    'deejay tv', 'm2o tv', 'frisbee', 'k2', 'giallo', 'focus',
    'top crime', 'mediaset extra', '20 mediaset', '20 mediase',
    'cine34', 'italia 7', '7gold'
]

SITE_CFG = {
    'stbemucodes9': {
        'url': 'https://stbemucodes9.blogspot.com',
        'type': 'blogger',
        'post_selector': '.post, .post-outer',
        'title_selector': 'h3 a, h2 a, .post-title a',
        'content_selector': '.post-body, .entry-content',
        'next_selector': '.blog-pager-older-link a, a[rel="next"]',
    },
    'stbstalker': {
        'url': 'https://stbstalker.alaaeldinee.com',
        'type': 'blogger',
        'post_selector': '.post, .post-outer',
        'title_selector': 'h3 a, h2 a, .post-title a',
        'content_selector': '.post-body, .entry-content',
        'next_selector': '.blog-pager-older-link a, a[rel="next"]',
    },
    'stbm3ufree': {
        'url': 'https://stbm3ufree.com',
        'type': 'wordpress',
        'post_selector': '.post-item, article, .posts-items li',
        'title_selector': '.post-title a, h2.post-title a',
        'content_selector': '.entry-content, .post-body',
        'next_selector': 'a.next, .next-page, .pagination a.next, a[rel="next"]',
        'category_pages': [
            '/m3u-list/italy-iptv/',
            '/m3u-list/',
            '/category/italy-iptv/',
        ],
    },
    'worldiptv': {
        'url': 'https://world-iptv.club',
        'type': 'wordpress',
        'post_selector': '.post-item, article, .posts-items li',
        'title_selector': '.post-title a, h2.post-title a',
        'content_selector': '.entry-content, .post-body',
        'next_selector': 'a.next, .next-page, .pagination a.next, a[rel="next"]',
        'tag_pages': ['/tag/italy/'],
    },
}

USER_AGENTS = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 Safari/605.1.15',
]

# ───────────────────────── UTILITY ─────────────────────────

def load_config():
    cp = ConfigParser()
    cp.read(os.path.join(BASE_DIR, 'config.ini'))
    return cp

def setup_logging(cfg):
    level = logging.DEBUG if cfg.getboolean('SCRAPER', 'debug', fallback=False) else logging.INFO
    log_file = os.path.join(BASE_DIR, cfg.get('SCRAPER', 'log_file', fallback='scraper.log'))
    logging.basicConfig(
        level=level,
        format='%(asctime)s [%(levelname)s] %(message)s',
        handlers=[logging.FileHandler(log_file, encoding='utf-8'), logging.StreamHandler(sys.stdout)]
    )
    return logging.getLogger(__name__)

def make_session():
    sess = requests.Session()
    sess.headers['User-Agent'] = USER_AGENTS[0]
    sess.headers['Accept'] = 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
    sess.headers['Accept-Language'] = 'it-IT,it;q=0.9,en;q=0.8'
    return sess

def rotate_ua(session):
    import random
    session.headers['User-Agent'] = random.choice(USER_AGENTS)

def safe_request(session, url, timeout=30, max_retries=2):
    for attempt in range(max_retries):
        try:
            resp = session.get(url, timeout=timeout, allow_redirects=True)
            resp.raise_for_status()
            return resp
        except requests.exceptions.RequestException as e:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)
                rotate_ua(session)
                continue
            logging.getLogger(__name__).warning('Richiesta fallita (%s): %s', url, e)
            return None

def is_m3u_content(resp):
    ct = resp.headers.get('Content-Type', '').lower()
    if any(x in ct for x in ['mpegurl', 'mpegurl', 'x-mpegurl', 'text/plain', 'application/octet-stream']):
        return True
    body = resp.text[:500].strip()
    if body.startswith('#EXTM3U') or body.startswith('#EXTINF'):
        return True
    return False

def looks_like_stb(text):
    patterns = [
        r'(?:portal|server|url)\s*[:=]\s*(https?://[^\s,;]+)',
        r'(?:mac|mac address)\s*[:=]\s*([0-9a-fA-F:]{17})',
        r'(?:stb(?:emu)?\s*code|serial)\s*[:=]\s*(\S+)',
    ]
    scores = sum(1 for p in patterns if re.search(p, text, re.I))
    return scores >= 2

# ───────────────────────── DATABASE ─────────────────────────

class Database:
    def __init__(self, db_path):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_tables()

    def _init_tables(self):
        self.conn.executescript('''
            CREATE TABLE IF NOT EXISTS scraped_posts (
                url TEXT PRIMARY KEY,
                site TEXT NOT NULL,
                title TEXT,
                scraped_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS processed_links (
                url TEXT PRIMARY KEY,
                filename TEXT,
                channel_count INTEGER DEFAULT 0,
                content_hash TEXT,
                is_working INTEGER DEFAULT 1,
                fail_reason TEXT,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                stream_url TEXT NOT NULL,
                group_title TEXT DEFAULT '',
                tvg_id TEXT DEFAULT '',
                tvg_name TEXT DEFAULT '',
                tvg_logo TEXT DEFAULT '',
                tvg_language TEXT DEFAULT '',
                source_link TEXT DEFAULT '',
                source_site TEXT DEFAULT '',
                is_working INTEGER DEFAULT 2,
                last_tested TIMESTAMP,
                fail_reason TEXT,
                first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(stream_url, name)
            );
            CREATE TABLE IF NOT EXISTS stb_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                portal TEXT,
                mac TEXT,
                serial TEXT,
                raw_text TEXT,
                source_url TEXT,
                source_site TEXT,
                found_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
        ''')
        # Migrazione: aggiungi colonne mancanti su DB esistenti
        for col, dtype in [('is_working', 'INTEGER DEFAULT 2'), ('last_tested', 'TIMESTAMP'), ('fail_reason', 'TEXT')]:
            try:
                self.conn.execute(f'ALTER TABLE channels ADD COLUMN {col} {dtype}')
            except sqlite3.OperationalError:
                pass
        self.conn.commit()

    def is_post_scraped(self, url):
        return self.conn.execute('SELECT 1 FROM scraped_posts WHERE url = ?', (url,)).fetchone() is not None

    def mark_post_scraped(self, url, site, title):
        self.conn.execute('INSERT OR IGNORE INTO scraped_posts(url, site, title) VALUES (?, ?, ?)',
                         (url, site, title))
        self.conn.commit()

    def is_link_processed(self, url):
        return self.conn.execute('SELECT 1 FROM processed_links WHERE url = ?', (url,)).fetchone() is not None

    def mark_link_processed(self, url, filename, count, content_hash, ok=True, reason=None):
        self.conn.execute('''INSERT OR REPLACE INTO processed_links
            (url, filename, channel_count, content_hash, is_working, fail_reason)
            VALUES (?, ?, ?, ?, ?, ?)''', (url, filename, count, content_hash, 1 if ok else 0, reason))
        self.conn.commit()

    def save_channel(self, name, stream_url, group_title, tvg_id, tvg_name, tvg_logo, tvg_language, source_link, source_site):
        try:
            self.conn.execute('''INSERT OR IGNORE INTO channels
                (name, stream_url, group_title, tvg_id, tvg_name, tvg_logo, tvg_language, source_link, source_site, is_working)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (name.strip(), stream_url.strip(), group_title.strip(), tvg_id.strip(),
                 tvg_name.strip(), tvg_logo.strip(), tvg_language.strip(),
                 source_link.strip(), source_site.strip(), CH_UNKNOWN))
            self.conn.commit()
            return True
        except Exception as e:
            logging.getLogger(__name__).warning('Errore salvataggio canale %s: %s', name, e)
            return False

    def mark_channel_tested(self, ch_id, is_working, fail_reason=None):
        self.conn.execute('''UPDATE channels SET is_working=?, last_tested=CURRENT_TIMESTAMP, fail_reason=? WHERE id=?''',
                         (is_working, fail_reason, ch_id))
        self.conn.commit()

    def get_untested_channels(self, limit=500):
        return self.conn.execute('''SELECT id, stream_url FROM channels
            WHERE is_working IN (2) OR (is_working=1 AND last_tested IS NOT NULL
            AND datetime(last_tested) < datetime('now', '-1 day'))
            ORDER BY last_tested NULLS FIRST LIMIT ?''', (limit,)).fetchall()

    def save_stb_code(self, portal, mac, serial, raw_text, source_url, source_site):
        self.conn.execute('''INSERT INTO stb_codes(portal, mac, serial, raw_text, source_url, source_site)
            VALUES (?, ?, ?, ?, ?, ?)''', (portal, mac, serial, raw_text, source_url, source_site))
        self.conn.commit()

    def get_all_channels(self):
        return self.conn.execute('''
            SELECT DISTINCT name, stream_url, group_title, tvg_id, tvg_name, tvg_logo, tvg_language
            FROM channels WHERE is_working=1 ORDER BY group_title, name
        ''').fetchall()

    def get_stats(self):
        return {
            'posts': self.conn.execute('SELECT COUNT(*) FROM scraped_posts').fetchone()[0],
            'links': self.conn.execute('SELECT COUNT(*) FROM processed_links').fetchone()[0],
            'channels': self.conn.execute('SELECT COUNT(*) FROM channels').fetchone()[0],
            'working_links': self.conn.execute('SELECT COUNT(*) FROM processed_links WHERE is_working=1').fetchone()[0],
        }

# ─────────────────── FILTRO ITALIANO ───────────────────

def is_italian_channel(name, group_title, tvg_id, tvg_name, tvg_language):
    fields_to_check = [
        (name or ''),
        (group_title or ''),
        (tvg_name or ''),
        (tvg_id or ''),
        (tvg_language or ''),
    ]
    combined = ' '.join(f.lower().strip() for f in fields_to_check)
    combined_words = set(re.findall(r'\b\w+\b', combined))

    for kw in ITALIAN_KEYWORDS:
        if ' ' in kw:
            if kw in combined:
                return True
        else:
            if kw in combined_words:
                return True

    if tvg_id and '.it' in tvg_id.lower():
        return True
    if tvg_language and tvg_language.lower() in ('italian', 'it', 'ita'):
        return True

    if group_title:
        gt = group_title.lower()
        gt_words = set(re.findall(r'\b\w+\b', gt))
        if any(x in gt_words for x in ['italy', 'italia', 'ita', 'italian']):
            return True

    return False

# ─────────────────── PARSER M3U ───────────────────

def parse_m3u(content, source_link='', source_site=''):
    channels = []
    current = {}
    for line in content.splitlines():
        line = line.strip()
        if line.startswith('#EXTINF:'):
            current = {}
            params_str = line[len('#EXTINF:'):]
            comma_pos = params_str.rfind(',')
            if comma_pos >= 0:
                ch_name = params_str[comma_pos + 1:].strip()
                attrs_str = params_str[:comma_pos].strip()
                current['name'] = ch_name
                current['group_title'] = ''
                current['tvg_id'] = ''
                current['tvg_name'] = ''
                current['tvg_logo'] = ''
                current['tvg_language'] = ''

                for match in re.finditer(r'(\w[\w-]*)\s*=\s*"([^"]*)"', attrs_str):
                    key = match.group(1).lower()
                    val = match.group(2)
                    if key in ('group-title', 'group_title'):
                        current['group_title'] = val
                    elif key in ('tvg-id', 'tvg_id'):
                        current['tvg_id'] = val
                    elif key in ('tvg-name', 'tvg_name'):
                        current['tvg_name'] = val
                    elif key in ('tvg-logo', 'tvg_logo'):
                        current['tvg_logo'] = val
                    elif key in ('tvg-language', 'tvg_language'):
                        current['tvg_language'] = val

                if not current['tvg_name']:
                    current['tvg_name'] = ch_name
                if not current['name']:
                    current['name'] = ch_name
        elif line and not line.startswith('#'):
            url = line.split('|')[0].split('?')[0].strip()
            if url and current and 'name' in current:
                ch = current.copy()
                ch['stream_url'] = url
                ch['source_link'] = source_link
                ch['source_site'] = source_site
                channels.append(ch)
            current = {}
    return channels

SHORTENER_DOMAINS = ['shortoearn.com', 'thermometeranalogyincomprehensible.com', 'oxy.cloud', 'fastuplod.org']

def download_and_parse_m3u(session, url, db, source_site, log, timeout=30):
    if db.is_link_processed(url):
        return 0

    # Skip JS-based shorteners that can't be resolved without a headless browser
    if any(d in url.lower() for d in SHORTENER_DOMAINS):
        db.mark_link_processed(url, '', 0, '', False, 'shortener JS-based')
        return 0

    log.info('  Download link M3U: %s', url[:100])
    resp = safe_request(session, url, timeout=timeout)

    if resp is None:
        db.mark_link_processed(url, '', 0, '', False, 'richiesta fallita')
        return 0

    if is_m3u_content(resp):
        content = resp.text
    else:
        soup = BeautifulSoup(resp.text, 'html.parser')
        found = False
        for meta in soup.select('meta[http-equiv="refresh"]'):
            m = re.search(r'url\s*=\s*(\S+)', meta.get('content', ''), re.I)
            if m:
                resp2 = safe_request(session, urljoin(url, m.group(1)))
                if resp2 and is_m3u_content(resp2):
                    content = resp2.text
                    found = True
                    break
        if not found:
            for a in soup.select('a[href]'):
                href = a['href']
                if '.m3u' in href.lower():
                    resp2 = safe_request(session, urljoin(url, href))
                    if resp2 and is_m3u_content(resp2):
                        content = resp2.text
                        found = True
                        break
        if not found:
            db.mark_link_processed(url, '', 0, '', False, 'contenuto non M3U')
            return 0

    channels = parse_m3u(content, url, source_site)
    italian = [ch for ch in channels if is_italian_channel(
        ch.get('name', ''), ch.get('group_title', ''),
        ch.get('tvg_id', ''), ch.get('tvg_name', ''),
        ch.get('tvg_language', '')
    )]

    for ch in italian:
        db.save_channel(
            ch['name'], ch['stream_url'], ch['group_title'],
            ch['tvg_id'], ch['tvg_name'], ch['tvg_logo'],
            ch['tvg_language'], url, source_site
        )

    content_hash = hashlib.md5(content.encode()).hexdigest()
    db.mark_link_processed(url, os.path.basename(urlparse(url).path) or 'playlist.m3u',
                          len(channels), content_hash, True)
    log.info('    Canali totali: %d, Italiani: %d', len(channels), len(italian))
    return len(italian)

# ─────────────────── VERIFICA STREAM ───────────────────

STREAM_ERRORS = {
    'ok': (True, None),
    'auth_required': (True, 'auth_required'),
    'timeout': (False, 'tempo_scaduto'),
    'connection_error': (False, 'errore_connessione'),
    'dns_error': (False, 'dns_irrisolvibile'),
    'not_found': (False, '404_non_trovato'),
    'server_error': (False, 'errore_server'),
    'ssl_error': (False, 'errore_ssl'),
    'too_many_redirects': (False, 'troppi_redirect'),
    'unknown': (False, 'errore_sconosciuto'),
}

TEMPORARY_ERRORS = {'tempo_scaduto', 'errore_server', 'troppi_redirect'}
PERMANENT_ERRORS = {'404_non_trovato', 'errore_connessione', 'dns_irrisolvibile', 'errore_ssl'}

def test_single_stream(url, timeout=10):
    try:
        resp = requests.head(url, timeout=timeout, allow_redirects=True,
                             headers={'User-Agent': USER_AGENTS[0]})
        if resp.status_code < 400:
            return 'ok', resp.status_code
        elif resp.status_code in (401, 403):
            try:
                r2 = requests.get(url, timeout=timeout, stream=True,
                                  headers={'User-Agent': USER_AGENTS[0], 'Range': 'bytes=0-1'})
                if r2.status_code in (200, 206, 403):
                    return 'auth_required', r2.status_code
                return 'auth_required', resp.status_code
            except Exception:
                return 'auth_required', resp.status_code
        elif resp.status_code in (404, 410):
            return 'not_found', resp.status_code
        elif resp.status_code >= 500:
            return 'server_error', resp.status_code
        else:
            return 'unknown', resp.status_code
    except requests.exceptions.SSLError:
        return 'ssl_error', 0
    except requests.exceptions.ConnectionError:
        return 'connection_error', 0
    except requests.exceptions.Timeout:
        return 'timeout', 0
    except requests.exceptions.TooManyRedirects:
        return 'too_many_redirects', 0
    except Exception:
        return 'unknown', 0

def verify_channels(db, log, max_workers=10, test_timeout=10):
    channels = db.get_untested_channels(limit=2000)
    if not channels:
        log.info('Nessun canale da verificare.')
        return 0, 0

    log.info('Verifica %d canali (%d thread)...', len(channels), max_workers)
    working = 0
    dead = 0
    tested = 0

    def test_one(ch):
        ch_id, url = ch
        try:
            result, status = test_single_stream(url, timeout=test_timeout)
            is_working = CH_WORKING if STREAM_ERRORS[result][0] else CH_DEAD
            fail_reason = STREAM_ERRORS[result][1]
            db.mark_channel_tested(ch_id, is_working, fail_reason)
            return result, ch_id, url[:80], True
        except Exception as e:
            db.mark_channel_tested(ch_id, CH_DEAD, 'errore_test')
            return 'unknown', ch_id, url[:80], True

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(test_one, ch): ch for ch in channels}
        for future in concurrent.futures.as_completed(futures):
            try:
                result, ch_id, url_preview, _ = future.result()
                tested += 1
                if STREAM_ERRORS[result][0]:
                    working += 1
                else:
                    dead += 1
                if tested % 100 == 0:
                    log.info('  Verificati %d/%d: %d ok, %d ko', tested, len(channels), working, dead)
            except Exception:
                tested += 1
                dead += 1

    log.info('Verifica completata: %d ok, %d ko su %d testati', working, dead, tested)

    # Riassunto errori permanenti vs temporanei
    cur = db.conn.execute(
        "SELECT fail_reason, COUNT(*) FROM channels WHERE is_working=0 AND fail_reason IS NOT NULL GROUP BY fail_reason"
    )
    for reason, count in cur.fetchall():
        tipo = 'PERMANENTE' if reason in PERMANENT_ERRORS else 'TEMPORANEO'
        log.info('  %s: %d canali (%s)', tipo, count, reason)

    return working, dead

# ─────────────────── SITE SCRAPERS ───────────────────

class SiteScraper:
    def __init__(self, name, cfg, session, db, log, request_timeout=30):
        self.name = name
        self.cfg = cfg
        self.session = session
        self.db = db
        self.log = log
        self.base_url = cfg['url']
        self.request_timeout = request_timeout

    def _full_url(self, path):
        return urljoin(self.base_url, path)

    def _soup(self, url):
        resp = safe_request(self.session, url)
        if resp is None:
            return None
        if 'text/html' not in resp.headers.get('Content-Type', ''):
            return None
        return BeautifulSoup(resp.text, 'html.parser')

    def _extract_m3u_links(self, text_html, base_url, text_clean=None):
        links = []
        exclude_patterns = ['share=', '/tag/', '/category/', '/go/report', 'facebook.com', 'twitter.com', 'x.com/sharer', 'play.google.com', 'stbemu-android-app', 'vuiptvplayer', 'sfvip-player', 'stalker-player']
        m3u_patterns = ['.m3u', '.m3u8', '/get.php']
        shortener_domains = ['shortoearn.com', 'thermometeranalogyincomprehensible.com']
        for a in BeautifulSoup(text_html, 'html.parser').select('a[href]'):
            href = a['href'].lower()
            if any(x in href for x in exclude_patterns):
                continue
            if any(x in href for x in m3u_patterns):
                links.append(urljoin(base_url, a['href']))
            elif any(d in href for d in shortener_domains):
                links.append(urljoin(base_url, a['href']))
        search_text = text_clean if text_clean else text_html
        for m in re.finditer(r'(https?://[^\s"\'<>]+\.(?:m3u8?|txt))', search_text, re.I):
            url = m.group(1)
            if url not in links:
                links.append(url)
        for m in re.finditer(r'(https?://[^\s"\'<>]+/get\.php[^\s"\'<>]*)', html.unescape(search_text), re.I):
            url = m.group(1)
            if url not in links:
                links.append(url)
        return links

    def _extract_xtream_links(self, text, base_url):
        links = []
        current_server = None
        current_user = None
        current_pass = None
        for line in text.splitlines():
            line = line.strip()
            if not line:
                if current_server and current_user and current_pass and current_user.strip() and current_pass.strip():
                    srv = current_server.rstrip('/')
                    usr = current_user.strip()
                    pw = current_pass.strip()
                    m3u_url = f"{srv}/get.php?username={quote(usr)}&password={quote(pw)}&type=m3u_plus&output=ts"
                    links.append(m3u_url)
                current_server = None; current_user = None; current_pass = None
                continue
            server = re.search(r'(?:server|url|host)\s*[:=]\s*(https?://[^\s,;]+)', line, re.I)
            if server:
                current_server = server.group(1); continue
            user = re.search(r'(?:user(?:name)?)\s*[:=]\s*(\S+)', line, re.I)
            if user and user.group(1).strip():
                current_user = user.group(1).strip(); continue
            pwd = re.search(r'(?:pass(?:word)?|key)\s*[:=]\s*(\S+)', line, re.I)
            if pwd and pwd.group(1).strip():
                current_pass = pwd.group(1).strip(); continue
        if current_server and current_user and current_pass and current_user.strip() and current_pass.strip():
            srv = current_server.rstrip('/')
            usr = current_user.strip()
            pw = current_pass.strip()
            m3u_url = f"{srv}/get.php?username={quote(usr)}&password={quote(pw)}&type=m3u_plus&output=ts"
            links.append(m3u_url)
        return links

    def _extract_stb_codes(self, text, base_url, post_url):
        blocks = re.split(r'\n\s*\n', text)
        for block in blocks:
            if looks_like_stb(block):
                portal = re.search(r'(?:portal|server|url)\s*[:=]\s*(https?://[^\s,;]+)', block, re.I)
                mac = re.search(r'(?:mac|mac address)\s*[:=]\s*([0-9a-fA-F:]{17})', block, re.I)
                serial = re.search(r'(?:stb(?:emu)?\s*code|serial)\s*[:=]\s*(\S+)', block, re.I)
                self.db.save_stb_code(
                    portal.group(1) if portal else '',
                    mac.group(1) if mac else '',
                    serial.group(1) if serial else '',
                    block.strip()[:500], post_url, self.name
                )

    def scrape(self):
        raise NotImplementedError

class BloggerScraper(SiteScraper):
    def get_post_list(self, start_url):
        urls = []
        seen = set()
        url = start_url
        while url:
            self.log.info('  Pagina post: %s', url)
            soup = self._soup(url)
            if soup is None:
                break
            for sel in ('.post-outer > a', '.post-title a', 'h3 a', 'h2 a', '.post-header h2 a'):
                for a in soup.select(sel):
                    href = a.get('href')
                    if href and href not in seen and not href.startswith('#') and '/search' not in href and '/go/report' not in href and 'blogger.com' not in href:
                        urls.append(href)
                        seen.add(href)
            if not urls:
                for a in soup.select('a'):
                    href = a.get('href', '')
                    if href and '/202' in href and href not in seen:
                        urls.append(href)
                        seen.add(href)
            next_link = soup.select_one('.blog-pager-older-link a, a[rel="next"]')
            url = next_link.get('href') if next_link else None
            if url:
                time.sleep(1)
        return list(dict.fromkeys(urls))

    def scrape(self):
        self.log.info('[%s] Inizio scraping...', self.name)
        post_urls = self.get_post_list(self.base_url)
        self.log.info('[%s] Trovati %d post totali', self.name, len(post_urls))

        italian_count = 0
        for i, post_url in enumerate(post_urls, 1):
            if self.db.is_post_scraped(post_url):
                continue
            self.log.info('  [%d/%d] Post: %s', i, len(post_urls), post_url[:90])
            soup = self._soup(post_url)
            if soup is None:
                continue
            content_el = soup.select_one('.post-body, .entry-content')
            if content_el is None:
                self.db.mark_post_scraped(post_url, self.name, '')
                continue

            text_html = str(content_el)
            text_clean = content_el.get_text('\n')
            post_title = soup.select_one('title')
            title = post_title.text.strip() if post_title else ''

            m3u_links = self._extract_m3u_links(text_html, post_url, text_clean)
            xtream_links = self._extract_xtream_links(text_clean, post_url)
            self._extract_stb_codes(text_clean, self.base_url, post_url)

            all_links = list(dict.fromkeys(m3u_links + xtream_links))
            if all_links:
                self.log.info('    Link M3U/Xtream trovati: %d', len(all_links))
                for link in all_links:
                    count = download_and_parse_m3u(self.session, link, self.db, self.name, self.log, timeout=self.request_timeout)
                    italian_count += count
                    time.sleep(0.5)

            self.db.mark_post_scraped(post_url, self.name, title)
            time.sleep(self.cfg.get('delay', 1))

        return italian_count


class WordPressScraper(SiteScraper):
    def get_post_list(self, start_urls):
        urls = []
        seen = set()
        max_empty_pages = 3
        for start in start_urls:
            url = start
            page_num = 1
            empty_count = 0
            while url and len(urls) < 500 and empty_count < max_empty_pages:
                self.log.info('  Pagina %d: %s', page_num, url)
                soup = self._soup(url)
                if soup is None:
                    break
                found = False
                for a in soup.select('.post-title a, h2.post-title a, .entry-title a, article a[href*="/202"]'):
                    href = a.get('href', '')
                    if href and href not in seen and '/202' in href:
                        urls.append(href)
                        seen.add(href)
                        found = True
                if not found:
                    empty_count += 1
                else:
                    empty_count = 0

                next_link = soup.select_one('a.next, .next-page, a[rel="next"], .pagination a:not(.prev)')
                if next_link:
                    url = urljoin(start, next_link.get('href', ''))
                    if url == start:
                        page_num += 1
                        url = start.rstrip('/') + f'/page/{page_num}/'
                    else:
                        page_num += 1
                else:
                    page_num += 1
                    url = start.rstrip('/') + f'/page/{page_num}/'
                time.sleep(0.5)
        return list(dict.fromkeys(urls))

    def scrape(self):
        self.log.info('[%s] Inizio scraping...', self.name)

        start_urls = [self.base_url]
        if 'category_pages' in self.cfg:
            start_urls = [self._full_url(p) for p in self.cfg['category_pages']]
        elif 'tag_pages' in self.cfg:
            start_urls = [self._full_url(p) for p in self.cfg['tag_pages']]

        post_urls = self.get_post_list(start_urls)
        self.log.info('[%s] Trovati %d post totali', self.name, len(post_urls))

        italian_count = 0
        for i, post_url in enumerate(post_urls, 1):
            if self.db.is_post_scraped(post_url):
                continue
            self.log.info('  [%d/%d] Post: %s', i, len(post_urls), post_url[:90])
            soup = self._soup(post_url)
            if soup is None:
                continue
            content_el = soup.select_one('.entry-content, .post-body, .entry')
            if content_el is None:
                self.db.mark_post_scraped(post_url, self.name, '')
                continue

            text_html = str(content_el)
            text_clean = content_el.get_text('\n')
            post_title = soup.select_one('title')
            title = post_title.text.strip() if post_title else ''

            m3u_links = self._extract_m3u_links(text_html, post_url, text_clean)
            xtream_links = self._extract_xtream_links(text_clean, post_url)
            self._extract_stb_codes(text_clean, self.base_url, post_url)

            all_links = list(dict.fromkeys(m3u_links + xtream_links))

            for btn in content_el.select('a.wp-block-button__link, a[class*="button"], a[class*="btn"]'):
                href = btn.get('href', '')
                if href and not any(x in href for x in ['facebook', 'twitter', 'whatsapp', 'telegram', 'mailto', 'share=', 'x.com/sharer', 'play.google.com', '?go=', '/app/', 'stbemu-android', 'vuiptvplayer', 'sfvip-player', 'stalker-player']):
                    if href not in [url for url in all_links] and not href.startswith('#') and not href.startswith('mailto'):
                        m3u_links.append(href)
                        all_links.append(href)
            if all_links:
                self.log.info('    Link trovati: %d M3U + %d Xtream', len(m3u_links), len(xtream_links))
                for link in all_links:
                    count = download_and_parse_m3u(self.session, link, self.db, self.name, self.log, timeout=self.request_timeout)
                    italian_count += count
                    time.sleep(0.5)

            self.db.mark_post_scraped(post_url, self.name, title)
            delay = float(self.cfg.get('delay', 2))
            time.sleep(delay)

        return italian_count

# ─────────────────── GENERAZIONE M3U ───────────────────

def generate_m3u(db, output_path, site_name='IPTVScraper'):
    channels = db.get_all_channels()
    now = datetime.now().strftime('%d/%m/%Y %H:%M')

    lines = ['#EXTM3U']
    lines.append(f'#PLAYLIST: Italian TV - Generata da {site_name}')
    lines.append(f'#GENERATED: {now}')
    lines.append(f'#CHANNELS: {len(channels)}')
    lines.append('')

    for ch in channels:
        name, url, group, tvg_id, tvg_name, tvg_logo, tvg_lang = ch
        attrs = []
        if tvg_id:
            attrs.append(f'tvg-id="{tvg_id}"')
        if tvg_name:
            attrs.append(f'tvg-name="{tvg_name}"')
        if tvg_logo:
            attrs.append(f'tvg-logo="{tvg_logo}"')
        if group:
            attrs.append(f'group-title="{group}"')
        if tvg_lang:
            attrs.append(f'tvg-language="{tvg_lang}"')
        attrs_str = ' '.join(attrs)
        extinf = f'#EXTINF:-1 {attrs_str},{name}'
        lines.append(extinf)
        lines.append(url)
        lines.append('')

    content = '\n'.join(lines)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(content)

    return content, len(channels)

# ─────────────────── GIT OPERATIONS ───────────────────

def git_push(cfg, output_path, log):
    token = cfg.get('GITHUB', 'token', fallback='')
    repo_url = cfg.get('GITHUB', 'repo_url', fallback='')
    branch = cfg.get('GITHUB', 'branch', fallback='main')
    m3u_file = cfg.get('GITHUB', 'm3u_filename', fallback='italian_tv.m3u')

    if not token:
        log.warning('Token GitHub non configurato. Salto push.')
        return False
    if not repo_url:
        log.warning('Repo URL non configurata. Salto push.')
        return False

    m3u_path = os.path.join(BASE_DIR, m3u_file)
    if not os.path.exists(m3u_path):
        log.warning('File M3U non trovato: %s', m3u_path)
        return False

    try:
        log.info('Inizio operazioni Git...')

        if not os.path.exists(os.path.join(BASE_DIR, '.git')):
            log.info('Inizializzazione repo Git...')
            subprocess.run(['git', 'init'], cwd=BASE_DIR, check=True, capture_output=True)
            authed_url = repo_url.replace('https://', f'https://{token}@')
            subprocess.run(['git', 'remote', 'add', 'origin', authed_url], cwd=BASE_DIR, check=True, capture_output=True)

        subprocess.run(['git', 'remote', 'set-url', 'origin',
                        repo_url.replace('https://', f'https://{token}@')],
                       cwd=BASE_DIR, check=True, capture_output=True)
        subprocess.run(['git', 'fetch', 'origin'], cwd=BASE_DIR, capture_output=True, timeout=30)

        remote_exists = subprocess.run(['git', 'rev-parse', f'origin/{branch}'],
                                       cwd=BASE_DIR, capture_output=True).returncode == 0

        if remote_exists:
            subprocess.run(['git', 'checkout', branch], cwd=BASE_DIR, capture_output=True)
            subprocess.run(['git', 'reset', '--soft', f'origin/{branch}'], cwd=BASE_DIR, check=True, capture_output=True)
        else:
            subprocess.run(['git', 'checkout', '-b', branch], cwd=BASE_DIR, check=True, capture_output=True)

        subprocess.run(['git', 'add', '-A'], cwd=BASE_DIR, check=True, capture_output=True)

        diff = subprocess.run(['git', 'diff', '--cached', '--name-only'], cwd=BASE_DIR,
                             capture_output=True, text=True)
        if not diff.stdout.strip():
            log.info('Nessuna modifica da committare.')
            return True

        now = datetime.now().strftime('%d/%m/%Y %H:%M')
        msg = f'Aggiornamento playlist italiana {now}'
        subprocess.run(['git', 'commit', '-m', msg], cwd=BASE_DIR, check=True, capture_output=True)

        log.info('Push su GitHub (%s)...', branch)
        push_args = ['git', 'push', 'origin', branch]
        if not remote_exists:
            push_args.append('--force-with-lease')
        result = subprocess.run(push_args, cwd=BASE_DIR, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            log.warning('Push fallito: %s', result.stderr[:300] if result.stderr else 'errore sconosciuto')
            return False

        log.info('Push completato con successo!')
        return True

    except subprocess.CalledProcessError as e:
        log.warning('Errore Git (exit %d): %s', e.returncode, e.stderr[:200] if e.stderr else str(e))
        return False
    except Exception as e:
        log.warning('Errore Git: %s', e)
        return False

# ─────────────────── MAIN ───────────────────

def main():
    cfg = load_config()
    log = setup_logging(cfg)
    log.info('=' * 60)
    log.info('IPTVScraper avviato')
    log.info('=' * 60)

    data_dir = os.path.join(BASE_DIR, cfg.get('SCRAPER', 'temp_dir', fallback='temp'))
    os.makedirs(data_dir, exist_ok=True)

    db = Database(os.path.join(BASE_DIR, 'state.db'))
    session = make_session()

    scrapers = []
    req_timeout = cfg.getint('SCRAPER', 'request_timeout', fallback=30)
    for name, site_cfg in SITE_CFG.items():
        val = cfg.get('SITES', name, fallback='')
        if val and val.lower() not in ('false', '0', 'no', 'off'):
            if site_cfg['type'] == 'blogger':
                scrapers.append(BloggerScraper(name, site_cfg, session, db, log, request_timeout=req_timeout))
            elif site_cfg['type'] == 'wordpress':
                scrapers.append(WordPressScraper(name, site_cfg, session, db, log, request_timeout=req_timeout))

    total_italian = 0
    for scraper in scrapers:
        try:
            count = scraper.scrape()
            total_italian += count
        except Exception as e:
            log.error('Errore durante scraping di %s: %s', scraper.name, e, exc_info=True)

    log.info('=' * 60)
    stats = db.get_stats()
    log.info('Statistiche scraping:')
    log.info('  Post scrapati: %d', stats['posts'])
    log.info('  Link processati: %d (di cui %d funzionanti)', stats['links'], stats['working_links'])
    log.info('  Canali italiani (totale): %d', stats['channels'])
    log.info('  Nuovi canali in questa esecuzione: %d', total_italian)

    # Verifica i canali non testati
    test_timeout = cfg.getint('SCRAPER', 'test_stream_timeout', fallback=10)
    test_threads = cfg.getint('SCRAPER', 'test_threads', fallback=10)
    working, dead = verify_channels(db, log, max_workers=test_threads, test_timeout=test_timeout)

    if stats['channels'] > 0:
        m3u_filename = cfg.get('GITHUB', 'm3u_filename', fallback='italian_tv.m3u')
        output_path = os.path.join(BASE_DIR, m3u_filename)
        content, count = generate_m3u(db, output_path)
        log.info('File M3U generato: %s (%d canali verificati, %.1f KB)',
                 output_path, count, len(content) / 1024)
        if count > 0:
            git_push(cfg, output_path, log)
        else:
            log.info('Nessun canale verificato funzionante, skip push.')
    else:
        log.warning('Nessun canale italiano trovato. File M3U non generato.')

    log.info('=' * 60)
    log.info('IPTVScraper completato')
    log.info('=' * 60)

if __name__ == '__main__':
    main()
