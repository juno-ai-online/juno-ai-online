# JUNO AI V32 — Online deployment starter

See [ONLINE-DEPLOYMENT.md](ONLINE-DEPLOYMENT.md) for the GitHub Pages + Render setup, cloud AI configuration, persistent storage, and security notes.

# JUNO AI V31.5 — Voice-enabled Interactive Playspace

- **Remembers the chosen name:** on each chat request, JUNO reads the signed-in user's display name from Settings and passes it as private personalization context. It can answer when asked what name the user chose, without awkwardly repeating the name in every response. The name is not exposed to other accounts.
- **Natural-language image search:** phrases such as “show me pictures of…”, “find photos of…”, “photographs of…”, “search images for…”, and “what does … look like?” automatically trigger image search when image search is enabled by the administrator.
- **Cleaner image queries:** removes conversational filler so image search searches the subject rather than the full chat sentence. Returned images are displayed as clickable image cards, with a useful fallback if no usable results are returned.
- These changes build on V30.5's free no-key DDGS search. Image availability depends on public search providers and the image host; some sites may block thumbnails or change their results.

## Previous V30.4 behavior

- Automatically searches for time-sensitive questions such as current sports records, recent results, current events, prices and questions that mention a future/recent year.
- If the model's first answer signals that its information is stale (for example, it cites 2024, says its knowledge is outdated, or cannot verify the answer), JUNO performs one free web search and retries the answer using the results.
- Search results are passed privately to the model; source cards and URLs are not shown in chat unless the user explicitly asks for sources.
- Greetings and ordinary small talk remain fast and do not trigger search.
- Confident matches in JUNO's knowledge base are answered directly without web search, unless the user explicitly asks for current/live information.
- Uses the free `ddgs` package; public search providers can rate-limit or change.

A new standalone JUNO AI application with a responsive black-and-gold chat UI, SQLite persistence, Ollama computer routing, account login, optional Google sign-in, admin console, privacy-conscious usage metrics, scheduled notices, one-time invitation codes, premium access codes, knowledge import, voice input, and free no-key web/image/news search.

## Previous response-speed and source cleanup

- Greetings, creative writing, code/debugging, translation and simple arithmetic stay fast without automatic web search.
- Factual questions, current facts, research and verification requests trigger automatic search when web search is enabled by the administrator; the search icon can force a lookup.
- Web source cards and URLs are hidden by default, but search results are supplied internally to improve the answer. Image cards remain available for image-search requests.
- The recent chat context window is reduced from 18 to 12 messages and Ollama context size from 8192 to 6144 to reduce overhead while preserving useful continuity.
- Existing database files are retained; do not delete the `data` folder when upgrading.


## Search — no paid API key

V30.5 uses the `ddgs` Python package. It does not require a Brave API key or another paid search API key. Search providers may rate-limit or change their public endpoints; if a search fails, JUNO reports that and instructs the model not to invent sources. Image search results are shown as image cards where providers return image URLs. Use results responsibly and respect the source websites' terms and image rights.

## Windows setup

1. Extract the ZIP into a new folder.
2. Install Python 3.11+.
3. Copy `.env.example` to `.env`.
4. Edit `.env` before running:
   - Set a long random `JUNO_SESSION_SECRET` (recommended; a persistent local secret file is created automatically if left as the placeholder).
   - Set `JUNO_ADMIN_EMAIL` and `JUNO_ADMIN_PASSWORD` to bootstrap the first administrator account.
   - Set `GOOGLE_CLIENT_ID` only if Google sign-in is configured in Google Cloud. It can be left blank.
   - Verify the Ollama computer IPs and ports.
5. Run `start.bat`, or manually run:
   ```powershell
   py -m venv .venv
   .venv\Scripts\activate
   pip install -r requirements.txt
   python app.py
   ```
6. Open `http://127.0.0.1:8080`.

If `JUNO_ADMIN_EMAIL` and `JUNO_ADMIN_PASSWORD` are set on first startup, V30 creates that admin account if the email does not already exist. Otherwise, a regular user is not automatically promoted to administrator.

## Linux/macOS setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env, then:
python app.py
```

## Ollama workers

Default workers are `192.168.100.3:11434` and `192.168.100.19:11434`. Change them in `.env` or in Admin Console → AI computers. Each worker can be assigned a model pin; a pinned worker only receives requests for that exact model. Weights control its relative share of eligible requests, while priority affects ordering and fallback. Workers must be reachable from the JUNO server and have the chosen model installed.

## Persistence and upgrades

- The database is stored at `data/juno-v30.db` by default.
- Existing data in this standalone V30 database persists across restarts.
- Do not delete `data/` if you want to keep accounts, conversations, settings, knowledge, invitation codes, and statistics.
- Keep `.env` private. Do not upload it publicly or include it in a shared ZIP.
- If you already have an older JUNO database, back it up before pointing `DATABASE` at it. V30 has additive migrations for selected older schemas, but it is a fresh schema and is not guaranteed to migrate every historic variant.

## Main features

- Email/password sign-up and sign-in; optional Google sign-in.
- One-time admin-assigned invitation codes and separate revocable premium codes.
- Persistent conversation history and compact continuity summaries supplied to the model.
- AUTO model selection and multiple Ollama worker routing with weights, priorities, busy-worker skipping, health tests, and model pinning.
- Automatic freshness search and one retry when an answer signals stale/uncertain knowledge; explicit web search; image search; news search for news queries.
- Private web research context for factual answers; image result cards for image-search requests.
- Code-fence cards with copy controls, voice-to-text, light/dark/system theme, personal preferences, recent chats, and animated notices.
- Admin dashboard for users, computer registry, feature settings, notifications, access codes, and knowledge management.
- JSON bulk knowledge import (up to 2,000 records per request).
- Usage statistics show counts and timings, not private chat contents.

## Important limitations

- Search quality, image availability, and rate limits depend on free upstream services and the installed `ddgs` package. No free public search service can promise uninterrupted operation.
- Ollama responses in this build are returned as a completed response rather than token-by-token streaming.
- Browser speech recognition availability depends on the browser and operating system.
- Google sign-in requires a valid Web OAuth Client ID in `.env` and correct authorized JavaScript origins in Google Cloud. The browser library is the current Google Identity Services library; email/password remains available if Google sign-in is not configured.
- Before exposing this application to the public internet, use HTTPS, set `COOKIE_SECURE=1`, configure a strong session secret, and add appropriate deployment security controls.

## V30.1 behavior update
- Web search is enabled by default for each chat message when the administrator has not disabled it.
- The user can still toggle web search off using the search icon.
- Image requests can trigger image search and display image cards.
- The chat interface no longer displays the selected model, worker computer, or response time. Those details remain in internal database statistics for administration and troubleshooting.
- Conversation history responses sent to the browser omit model names.


## V30.8 mathematical rendering

- Chat messages render LaTeX mathematics with MathJax, including inline and display equations.
- Loads standard math extensions for AMS environments, matrices, cases, cancellation, physics notation, chemical equations, extra symbols and accents.
- Re-typesets each new answer and messages loaded from saved conversations. Code blocks and text diagrams are excluded from math processing to preserve literal formatting.
- The model prompt now explicitly requests well-formed LaTeX for mathematical and scientific answers and preserves Unicode symbols such as ∪, ∩, ∅, ℝ, ≤, ≥, m² and m³.
- MathJax is loaded from a public CDN; if internet access is unavailable, plain Unicode symbols still display, but LaTeX expressions may remain as raw text.


## V30.8 — smart visuals and persistent knowledge

- Automatically requests image results for explicit picture/photo/image requests and many concrete visual-learning topics (for example animals, plants, landmarks, cars, flags, food, planets and artwork) when visuals are likely to help. Pure coding, arithmetic, translation and abstract questions are excluded from the visual-topic heuristic.
- Knowledge entries are retained in SQLite until an administrator explicitly deletes them. Removed the fixed 2,000-entry import ceiling, 80-entry retrieval ceiling, 300-entry admin display ceiling and Flask's fixed 8 MB request ceiling.
- Bulk JSON imports are sent in batches from the admin UI, and the UI also supports selecting a `.json` file. Imports append entries; they do not replace or erase previous knowledge.
- Adding a knowledge entry no longer clears the name, information, tags or metadata form, so previously entered text is not wiped after saving.
- Knowledge retrieval ranks across all enabled entries and selects relevant excerpts from long entries. Full source text remains stored, while model prompt size remains bounded by the model's context window; no application can send literally unlimited text in a single AI prompt.
- Chat context now uses the latest 40 messages and keeps a longer continuity summary. All messages remain stored in the database.

JSON knowledge imports still depend on available RAM, disk space, reverse-proxy limits and database capacity. V30.9 sets a 100 MB request ceiling and configurable per-image upload limits. Back up the `data` directory before deploying updates.


## V30.9 — Image uploads, saved preferred name, suggestions and mobile navigation

- Added authenticated image attachments to chat. PNG, JPG/JPEG, WEBP, GIF and BMP images are signature-checked, size-limited, associated with the signed-in user and active conversation, then passed as base64 image data to an available vision-capable Ollama model. The assistant is instructed to inspect images rather than guess.
- Image upload settings are controlled in one admin panel: enable/disable uploads, uploads per user per rolling time window (default 3 per 60 minutes; 0 means unlimited), window length in minutes and per-image size (1–50 MB). At most 8 images can be selected per request. The whole HTTP request is limited to 100 MB.
- In-chat phrases such as “call me Juno” or “my preferred name is Juno” update the user's saved preferred name, which is also available in Settings.
- Added a user-facing suggestion form in Settings and an admin “User suggestions” panel with review statuses.
- On mobile, the sidebar closes when the backdrop is tapped, Escape is pressed, or a conversation is selected. Upload previews and dialogs are adapted for narrow screens.
- Vision analysis requires at least one installed model whose name indicates image support (for example Gemma 3/4, Qwen VL, LLaVA or another recognized vision model). If none is available, JUNO explains that the administrator needs to install a vision-capable model.
- No database reset or deletion of existing conversations, preferences, knowledge entries or users is performed.


## V31.0 map, code-file and voice improvements

- Place/map/directions prompts can show an OpenStreetMap preview with a Google Maps link. Geocoding uses OSM Nominatim on explicit map requests only. Browser location is requested only for prompts that explicitly ask about nearby/current-location results; it is never inferred from IP. Admins can disable maps.
- Code blocks can be downloaded as language-appropriate files. An admin setting enables/disables these tools and controls maximum generated code file size in MB (1 MB–1 GB). The checker parses Python without executing it, optionally uses `node --check` for JavaScript, and performs basic HTML/CSS structural checks. It never executes user-provided code.
- Enter inserts a newline; Ctrl+Enter / Cmd+Enter sends the message, so mobile keyboard return no longer unexpectedly submits.
- Voice input is continuous while active, appends recognized words to existing typed text, and preserves the final transcript when stopped. Tapping the mic again stops dictation without sending or clearing the message.
- New admin settings: OpenStreetMap/navigation and code file tools/syntax checks; maximum generated code file size in MB (1 MB–1 GB).

Maps require network access to OpenStreetMap/Nominatim and Google Maps. The free public Nominatim service can rate-limit requests. The code checking feature is a syntax/structure checker, not a general-purpose execution sandbox or a guarantee that programs will run correctly.


## V31.2 smart conversation and learning improvements

- New conversations receive a short topic-based title after the first answer, instead of retaining the full first prompt. Common question forms, map requests and image requests are summarized into a topic label.
- The assistant can emit a safe `juno-visual` JSON block for supported educational representations. The frontend renders data-driven motion/acceleration illustrations, bar charts, process flows and concept cards; it never executes model-provided HTML or JavaScript for these visuals.
- Explicit requests to add information to JUNO's knowledge base are saved as user suggestions for administrator review, not silently written into the permanent knowledge base. A notice is shown in chat. The admin can review the suggestion using the existing suggestions area.
- Map intent is separated from image search so requests like “show me a map of Lagos” prioritize the OpenStreetMap map card rather than unrelated picture results. Image search remains available for relevant visual subjects and explicit picture/photo requests.
- Map prompts and direction requests are also summarized into more useful conversation titles.

This build is a source-code upgrade; live integration with the configured Ollama workers and external map/image providers must still be verified in the deployment environment.


## V31.3 updates

- Replaced the old maximum code file size setting in KB with MB, adjustable from 1 MB to 1024 MB (1 GB).
- Migrates the existing KB value to MB on first startup without resetting the database.
- Download actions read the current server setting when clicked, so an already-open chat uses newly saved limits without requiring a page refresh.
- The server-side syntax-check endpoint enforces the same MB limit.

## V31.2 updates

- Removed the chat-header manual search toggle; web/image search detection remains automatic and respects administrator settings.
- Mobile chat navigation no longer automatically focuses the message box when opening/creating chats or after sending, preventing the phone keyboard from unexpectedly opening.
- Added a per-user private memory field in Settings, capped at 1 MiB of UTF-8 data. The API only returns the signed-in user's own memory; the admin console does not query or display it. Stored memory is encrypted at rest using `JUNO_PRIVATE_MEMORY_KEY` (recommended) or a key derived from the persistent session secret. Keep the key stable across restarts/backups.
- Adds concise summaries from recent chats and private memory to the model context when relevant. The model context window is still finite.
- Improved image result cards with fallback from thumbnail to full image, loading resilience, source labels and responsive layout.
- `juno-visual` fenced blocks are parsed as data and rendered as in-app visuals, including an interactive motion animation with play/pause and speed control. They are never shown with code download/copy controls; malformed visual blocks show a friendly fallback card.
- Injects the current UTC date into the system instructions to reduce stale claims that the current month is in the future.


## V31.4 updates — JUNO Playspace

- Added an optional animated face with eyes, smile, blinks, waves, jumps and juggling.
- Added instant browser-side commands for moving left/right, returning to center, showing a visual, and stopping an action.
- Added an interactive “catch the star” mini-game with score tracking.
- Added fullscreen support with a CSS expanded-view fallback where the browser blocks the Fullscreen API.
- Added a command field in Playspace and natural-language quick commands from chat when the message explicitly refers to JUNO (for example, “JUNO move to the right”). These simple actions are local and do not require a model request.
- The face does not use a camera or microphone. All actions are user-triggered.


## V31.5 updates — voice interaction

- Added a **Listen** control to JUNO Playspace using the browser's built-in SpeechRecognition API when supported.
- Spoken face commands such as “move right”, “juggle”, “jump”, “show a visual”, and “play catch” are handled locally in the browser. Other spoken requests are transcribed into the chat and submitted to JUNO.
- Added browser text-to-speech for assistant replies, with a **Speak JUNO replies** toggle and a **Stop voice** control. Code blocks and URLs are omitted from speech for readability.
- Added listening glow and speaking mouth animations. Typing remains available at all times.
- Microphone use begins only after the user presses **Listen**. The browser may ask for microphone permission. Speech recognition availability depends on the browser and secure-context requirements; use HTTPS (for example, your Cloudflare HTTPS URL) or localhost where supported.
- Voice transcription and speech synthesis are browser features and may vary by browser, language, network policy, and installed voices. The feature does not add a camera or continuous background listening.
