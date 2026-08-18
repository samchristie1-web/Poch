# Poch — FPL Assistant (app version)

This turns your Colab notebook into a small mobile-friendly web app, using
[Streamlit](https://streamlit.io). Same logic as your notebook (`poch_core.py`
is your code, essentially unchanged) — `poch_app.py` just adds a UI on top so
you can tap buttons on your phone instead of running cells.

## Files
- `poch_core.py` — your Poch engine: FPL client, knowledge base, senate,
  orchestrator. Unchanged from your notebook except the offline demo block
  at the bottom was removed (not needed for the app).
- `poch_app.py` — the Streamlit app: sidebar to load your team, tabs for
  Lineup / Transfers / Squad / Community intel.
- `requirements.txt` — the two packages it needs.

## Try it on your computer first (optional)
```
pip install -r requirements.txt
streamlit run poch_app.py
```
This opens in your browser at `localhost:8501`.

## Get it onto your phone (free, ~5 minutes)
1. **Put this folder in a GitHub repo.** If you don't have one yet:
   create a new repo on github.com, upload these three files
   (`poch_core.py`, `poch_app.py`, `requirements.txt`).
2. **Go to** [share.streamlit.io](https://share.streamlit.io) and sign in
   with your GitHub account.
3. Click **"New app"**, pick the repo, and set the main file path to
   `poch_app.py`. Click **Deploy**.
4. In a minute or two you'll get a URL like
   `https://poch-yourname.streamlit.app`.
5. Open that URL on your phone (Safari or Chrome), then use the browser's
   **Share → Add to Home Screen**. It'll sit on your home screen with an
   icon like a normal app and open full-screen.

That's it — no App Store, no native code. Every time you open it, it pulls
fresh FPL data when you tap "Load live data."

## Notes
- The community intel tab is manual by design (matching your original
  notebook) — paste in whatever you or Claude find when researching FPL
  forums, and the Community senator factors it into its verdicts.
- Streamlit Community Cloud is free for personal projects like this one.
- If you ever want push notifications (e.g. "reminder: deadline in 2 hours"),
  that would need a different hosting setup — worth a separate conversation
  if you want to go there.
