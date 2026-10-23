# JUNO AI V32 Online Deployment Starter

This package separates the website from the Flask API:

- **GitHub Pages** serves the static files in `frontend/`.
- **Render** runs `app.py` as a Flask API with Gunicorn.
- **Hosted AI** is configured using an OpenAI-compatible chat-completions endpoint. This means your personal PC does not need to be online for ordinary text chats.
- **Persistent storage** uses a Render disk mounted at `/var/data` for the SQLite database and uploaded files. The included Render disk configuration requires a paid Render instance/disk; do not deploy a database-backed production service on ephemeral storage.

## 1. Push this project to GitHub

Upload/commit the project files to your repository. Do **not** upload `.env`, a real database, private keys, or API tokens. GitHub Pages is deployed by `.github/workflows/pages.yml` from the `frontend/` directory. In the repository, enable GitHub Actions/Pages if prompted.

## 2. Configure the static frontend

Edit `frontend/config.js` and replace `https://YOUR-RENDER-SERVICE.onrender.com` with your Render API origin, with no trailing slash. This value is public; never put an API key in this file. Commit and push the change. The deployed page will be the GitHub Pages URL shown in the repository's Pages settings.

## 3. Deploy the backend to Render

Use **New → Blueprint** and select this repository so Render reads `render.yaml`. The blueprint includes a persistent 1 GB disk and a Starter web service. In the Render environment settings, set:

- `JUNO_ALLOWED_ORIGINS` to the exact GitHub Pages origin, e.g. `https://YOURNAME.github.io` (no path or trailing slash). If you use a project site, its origin is still `https://YOURNAME.github.io`; do not include `/repository-name`.
- `JUNO_CLOUD_AI_BASE_URL` to the provider's OpenAI-compatible API base URL, often ending in `/v1`.
- `JUNO_CLOUD_AI_API_KEY` to the provider secret.
- `JUNO_CLOUD_AI_MODEL` to the exact model identifier the provider supports.
- `JUNO_ADMIN_EMAIL` and `JUNO_ADMIN_PASSWORD` to bootstrap an administrator on first startup. Use a strong, unique password.
- `JUNO_PRIVATE_MEMORY_KEY` to a valid, stable Fernet key if you use private memory. Generate with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. Keep it safe and stable; changing it makes existing encrypted private memories unreadable.

The hosted inference adapter supports text messages with OpenAI-compatible `/chat/completions` APIs. Image attachments currently use the Ollama vision path; for cloud-only image understanding, a provider-specific multimodal adapter still needs to be added. If hosted AI fails, JUNO will try configured Ollama workers; Render cannot reach private LAN addresses like `192.168.x.x` unless you deliberately provide secure network access.

## 4. Session and browser compatibility

The API sets secure cross-site session cookies (`SameSite=None; Secure`) for the GitHub Pages frontend. Some browsers may block third-party cookies between `github.io` and `onrender.com`, which can break login. The most reliable production setup is a custom domain with the frontend and API under the same site, or a same-site reverse proxy. Do not disable secure cookies on the public deployment.

## 5. Verify deployment

- Open `https://YOUR-RENDER-SERVICE.onrender.com/api/health`; check `cloud_ai_configured: true`.
- Open the GitHub Pages URL, create a user, log in, create a chat and test a simple prompt.
- Check Render logs for errors. Never paste API keys or secrets into issue reports.
- Test image upload, voice, maps, private memory and admin functions separately; external APIs and browser support vary.

## Important limitations

- Free model/API tiers vary by provider, require provider credentials, and may have quotas or rate limits. No hosted AI provider is guaranteed to be free or uninterrupted.
- SQLite persistence and uploads require a persistent disk. The included Render persistent disk is not part of Render's free web-service tier.
- GitHub Pages is static hosting only; all API, account, admin and model operations run on Render.
- This starter keeps the existing SQLite data model and admin features. It is not an automatic migration of an older production database. Back up any existing database before using it.
