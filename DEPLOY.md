# Deploying Nexo POS for free (testing)

Recommended: **PythonAnywhere** — genuinely free tier, no credit card, gives you
`https://<your-username>.pythonanywhere.com`. Good fit for this app (small
Flask service + file-based SQLite).

## Steps (~10 minutes, done in your own browser)

1. **Sign up** at https://www.pythonanywhere.com/registration/register/beginner/
   (Beginner/free account — no card required).

2. Open a **Bash console** from the PythonAnywhere dashboard ("Consoles" → "Bash") and clone the repo:
   ```bash
   git clone https://github.com/crrf/elrojo.git
   cd elrojo
   ```

3. Create a virtualenv and install dependencies:
   ```bash
   mkvirtualenv --python=/usr/bin/python3.10 elrojo-venv
   pip install -r requirements.txt
   ```

4. Go to the **Web** tab → **Add a new web app** → choose **Flask** → pick the
   Python version matching the virtualenv above → when asked for the Flask
   project path, point it at `/home/<your-username>/elrojo/app.py`.
   PythonAnywhere generates a WSGI config file for you at
   `/var/www/<your-username>_pythonanywhere_com_wsgi.py`.

5. Edit that generated WSGI file (link is on the Web tab) so it matches this
   app — replace its contents with:
   ```python
   import sys, os

   path = '/home/<your-username>/elrojo'
   if path not in sys.path:
       sys.path.insert(0, path)

   # app.py requires SECRET_KEY to be set — fails fast otherwise (see app.py).
   # Generate your own: python3 -c "import secrets; print(secrets.token_hex(32))"
   os.environ['SECRET_KEY'] = 'REPLACE_WITH_A_GENERATED_SECRET'

   from app import app as application
   ```

6. On the **Web** tab, set the **Virtualenv** path to
   `/home/<your-username>/.virtualenvs/elrojo-venv`.

7. Click **Reload**. Your app is live at `https://<your-username>.pythonanywhere.com`.

Default login seeded by `init_db()`: username `admin`, password `admin123`.
**Change it immediately** via the Users page once you're in — this is a
testing deployment, not a hardened production one (see `AUDIT_REPORT.md` /
`COMPLETION_REPORT.md` for the full list of what's been hardened vs. still
open).

## Notes specific to this app
- The SQLite file (`pos.db`) lives inside the cloned repo directory and is
  writable there by default — fine for testing, but PythonAnywhere's free
  tier has no persistent-disk guarantee beyond your account storage quota,
  and there's no automated backup. Don't treat data on the free tier as durable.
- `.env` is gitignored on purpose (see `.env.example`) — the WSGI file above
  sets `SECRET_KEY` directly instead, since PythonAnywhere doesn't read a
  `.env` file for you automatically.
- Free-tier PythonAnywhere apps get put to sleep with no forced restart
  schedule but *do* have a daily CPU-second quota — fine for manual testing,
  not for load testing.

## Alternative: Render.com
Render's free web-service tier works the same way (connect the GitHub repo,
it builds and deploys automatically on push) but now requires card
verification even for the free tier, which is why PythonAnywhere is the
default recommendation here. If you already have a Render account verified,
the app needs:
- **Build command:** `pip install -r requirements.txt`
- **Start command:** `gunicorn app:app` (add `gunicorn` to `requirements.txt` first — it isn't there today since this app has only been run via Flask's dev server)
- **Environment variable:** `SECRET_KEY` set in Render's dashboard (same fail-fast requirement as above)
