# Easiest way to update JUNO AI (no file-by-file editing)

## Recommended: GitHub Desktop
1. Install GitHub Desktop and sign in to the GitHub account that owns your JUNO repository.
2. Use **File → Clone repository** once to download the repository to your PC.
3. Download and extract the new JUNO release ZIP. Copy the extracted project files over the cloned repository folder and choose **Replace files** when asked. Do not copy `.env`, a real database, API keys, or secrets into GitHub.
4. Keep your existing `.env` and any local `data/` database. Do not delete the database or overwrite it with an empty database.
5. In GitHub Desktop, review the changed files, enter a summary such as `Update JUNO`, click **Commit to main**, then **Push origin**.
6. GitHub Pages redeploys the `frontend/` folder automatically. If Render is connected to the same repository, it redeploys the backend automatically too. Check the Render deployment log before testing.

You only need to clone the repository once. After that, each release is a whole-folder replacement and one commit/push—not editing files one by one.

## Protecting the knowledge base
- Before updating, sign in as admin → **Knowledge base** → **Export JSON backup**. Store the downloaded JSON somewhere outside the repository.
- The JSON import appends entries; it does not delete existing entries. If entries disappear after a redeploy/restart, the usual cause is the host using temporary storage or pointing at a different database file—not the knowledge retrieval code.
- On Render, this project expects a persistent disk mounted at `/var/data` and `DATABASE=/var/data/juno.db`. A Render persistent disk requires a paid service/disk. A free/ephemeral service can lose SQLite data on restart/redeploy. The admin Knowledge Base panel now shows a warning if the Render disk mount is not detected.
- If the database already reset, restoring the JSON backup through **Bulk JSON import** will recover the exported knowledge entries. It will not restore accounts, conversations, cloud keys, or other database records.
- Keep `JUNO_SESSION_SECRET` and `JUNO_PRIVATE_MEMORY_KEY` unchanged between deployments; changing encryption secrets can make saved cloud keys or private memories unreadable.
