# IPTVScraper

Scraper automatico per playlist IPTV italiane. Estrae canali italiani da 4 siti IPTV pubblici, genera un file M3U unico e lo pubblica su GitHub.

## Obiettivo

Aggregare in un unico file M3U tutti i canali italiani trovati su diverse fonti IPTV pubbliche, evitando di dover visitare manualmente decine di pagine ogni giorno. Lo script è pensato per funzionare in modo **incrementale** su Windows, schedulato via Task Scheduler.

## Fonti scrapate

| Sito | Tipo | URL |
|------|------|-----|
| stbemucodes9.blogspot.com | Blogger | https://stbemucodes9.blogspot.com |
| stbstalker.alaaeldinee.com | Blogger | https://stbstalker.alaaeldinee.com |
| stbm3ufree.com | WordPress | https://stbm3ufree.com |
| world-iptv.club | WordPress | https://world-iptv.club |

## Tecnologie

- **Python 3.12** — Linguaggio principale
- **requests** — Download pagine e playlist M3U
- **BeautifulSoup 4** — Parsing HTML dei siti
- **SQLite 3** — Database di stato (incrementale)
- **Git + GitHub** — Versionamento e pubblicazione playlist
- **Windows Task Scheduler** — Esecuzione automatica post-avvio

## Architettura

```
┌─────────────────────────────────────────────────────────┐
│                     scraper.py                          │
│                                                         │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐  │
│  │ BloggerScraper│  │WordPress-    │  │ Database     │  │
│  │ (stbemucodes9,│  │Scraper       │  │ (state.db)   │  │
│  │  stbstalker)  │  │(stbm3ufree,  │  │              │  │
│  │               │  │ world-iptv)  │  │ - scraped_   │  │
│  │ get_post_list │  │              │  │   posts      │  │
│  │ → paginazione │  │ get_post_list│  │ - processed_ │  │
│  │   Blogger     │  │ → paginazione│  │   links      │  │
│  │               │  │   WordPress  │  │ - channels   │  │
│  └───────┬───────┘  └──────┬───────┘  │ - stb_codes  │  │
│          │                 │          └──────────────┘  │
│          └─────────┬───────┘                            │
│                    ▼                                    │
│  ┌──────────────────────────────┐                      │
│  │ _extract_m3u_links()        │                      │
│  │ _extract_xtream_links()     │                      │
│  │ _extract_stb_codes()        │                      │
│  └──────────────┬───────────────┘                      │
│                 ▼                                      │
│  ┌──────────────────────────────┐                      │
│  │ download_and_parse_m3u()    │                      │
│  │ → is_m3u_content()          │                      │
│  │ → parse_m3u()               │                      │
│  │ → is_italian_channel()      │                      │
│  └──────────────┬───────────────┘                      │
│                 ▼                                      │
│  ┌──────────────────────────────┐                      │
│  │ generate_m3u() → .m3u       │                      │
│  │ git_push() → GitHub         │                      │
│  └──────────────────────────────┘                      │
└─────────────────────────────────────────────────────────┘
```

## Flusso di esecuzione

1. **Carica configurazione** da `config.ini` (token GitHub, timeout, siti abilitati)
2. **Inizializza database SQLite** (`state.db`) — crea tabelle se non esistono
3. **Per ogni sito abilitato:**
   a. Recupera lista dei post (pagina iniziale + paginazione)
   b. Per ogni post non ancora scrapato:
      - Estrae link M3U (tag `<a>` con `.m3u`/`.m3u8`/`/get.php`)
      - Estrae link Xtream (credenziali SERVER/USER/PASS → costruisce URL `/get.php`)
      - Estrae codici STB (non convertibili, salvati separatamente)
      - Scarica ogni link M3U e lo parsifica
      - Filtra solo canali italiani per match su group-title, tvg-name, tvg-id, tvg-language
      - Salva canali nel database (UNIQUE su stream_url + name)
      - Segna il post come scrapato
4. **Se ci sono nuovi canali:**
   - Genera file M3U unico (`italian_tv.m3u`)
   - Esegue commit e push su GitHub
5. Altrimenti skip (nessuna operazione superflua)

## Database (state.db)

Tabelle:

- **scraped_posts** — URL dei post già processati, con timestamps
- **processed_links** — URL M3U già scaricati, conteggio canali, hash del contenuto, stato funzionante/non
- **channels** — Canali italiani trovati (nome, stream URL, group-title, tvg-id/name/logo/language, fonte)
- **stb_codes** — Codici STB non convertibili (portal, MAC, serial, testo originale, fonte)

## Riconoscimento canali italiani

Lo script usa una keyword list per filtrare i canali:
- Nomi RAI (Rai 1, Rai 2, ...), Mediaset (Canale 5, Italia 1, Rete 4, ...)
- La7, TV8, Nove, Boing, Cartoonito, Iris, Cielo, Sportitalia, ...
- Match su campi: `name`, `group-title`, `tvg-name`, `tvg-id`, `tvg-language`
- Se `tvg-language` = "italian"/"it"/"ita" o `tvg-id` contiene ".it"

## Gestione Xtream codes

I siti pubblicano spesso credenziali Xtream in formato:

```
SERVER: http://example.com:80
USERNAME: myuser
PASSWORD: mypass
```

Lo script:
1. Cerca nel testo pulito (senza HTML) le tre righe consecutive
2. Costruisce l'URL: `http://example.com:80/get.php?username=myuser&password=mypass&type=m3u_plus&output=ts`
3. Se lo stesso URL è già presente come link diretto nella pagina, lo deduplica

## Limitazioni note

### stbm3ufree.com — Link dietro shortener JS
Tutti i link M3U passano da `shortoearn.com`, URL shortener che richiede JavaScript (redirect dopo timer, pulsante "Skip Ad"). Questi link vengono **skippati**. Per risolverli servirebbe un headless browser (Playwright/Selenium).

### stbstalker.alaaeldinee.com — Solo codici STB
Il sito pubblica esclusivamente codici STB (portal/MAC/serial), non convertibili in M3U. I codici vengono salvati in `stb_codes` ma non contribuiscono alla playlist.

### world-iptv.club — Nessun post trovato
Il tag `/tag/italy/` esiste ma non contiene post con link M3U. La struttura del sito potrebbe essere cambiata.

### Link APK e pagine app
Alcuni post WordPress contengono bottoni per app Android/Windows (Blue4K, STBEmu, VUIPTV). Questi link vengono esclusi dalle exclusion patterns per evitare richieste inutili.

## Configurazione

`config.ini` (escluso da .gitignore):

```ini
[GITHUB]
token = ghp_...                    # Personal Access Token
repo_url = https://github.com/...  # Destinazione playlist M3U
branch = main
m3u_filename = italian_tv.m3u

[SCRAPER]
delay = 2                          # Secondi tra post
request_timeout = 15               # Timeout richieste HTTP
temp_dir = temp
log_file = scraper.log
debug = false

[SITES]
stbemucodes9 = https://stbemucodes9.blogspot.com
stbstalker = https://stbstalker.alaaeldinee.com
stbm3ufree = https://stbm3ufree.com
worldiptv = https://world-iptv.club
```

Per disabilitare un sito: impostare il valore a `false` o `0`.

## Task Scheduler (Windows)

Il task va creato con privilegi di amministratore:

```
schtasks /create /tn "IPTVScraper" /tr "'C:\Python312\python.exe' 'C:\path\to\scraper.py'" /sc onstart /delay 0005:00 /ru "$env:USERDOMAIN\$env:USERNAME" /it /f
```

Oppure manualmente da **Task Scheduler**:
- Trigger: all'avvio, ritardo 5 min
- Azione: avvia `python.exe`, argomento `C:\...\scraper.py`
- Cartella di lavoro: `C:\...\IPTVScraper`

## Output

- `italian_tv.m3u` — Playlist M3U completa dei soli canali italiani
- `state.db` — Database di stato (non committato)
- `scraper.log` — Log di esecuzione (non committato)

## Manutenzione

- I link M3U nei siti scadono: lo script li marca come `is_working=0` se falliscono
- Per resettare lo scraping: cancellare `state.db`
- Per aggiornare la keyword list italiana: modificare `ITALIAN_KEYWORDS` in `scraper.py`

## Estendibilità

Per aggiungere un nuovo sito:
1. Aggiungere entry in `SITE_CFG` in `scraper.py` con `type: 'blogger'` o `'wordpress'`
2. Aggiungere URL in `config.ini` sezione `[SITES]`
3. Se necessario, personalizzare i selettori CSS per post/titolo/contenuto/paginazione
