"""
POCH — mobile-friendly app
===========================
Streamlit front-end over poch_core.py (your existing orchestrator + senate
+ sub-apps + knowledge base — unchanged logic, just wrapped in a UI you can
open on your phone).

Run locally:
    pip install -r requirements.txt
    streamlit run poch_app.py

Deploy for free so it has a phone-friendly URL (no install needed on your
phone, just open the link in Safari/Chrome and "Add to Home Screen"):
    1. Push this folder to a GitHub repo (poch_core.py, poch_app.py,
       requirements.txt).
    2. Go to https://share.streamlit.io , sign in with GitHub, "New app",
       point it at the repo, main file = poch_app.py.
    3. It builds and gives you a URL like poch-yourname.streamlit.app.
       Open that on your phone, tap Share -> Add to Home Screen, and it
       behaves like an app icon.
"""
import streamlit as st

from poch_core import (
    Poch,
    load_live_knowledge_base,
    set_squad_by_names,
    CommunitySignal,
    ingest_signals,
)

st.set_page_config(page_title="Poch — FPL Assistant", page_icon="⚽", layout="centered")

DEFAULT_TEAM_ID = 5372956  # your FPL team ID, pre-filled — change or clear as needed


# ------------------------------------------------------------------
# Session state — keeps the knowledge base + Poch alive between clicks
# ------------------------------------------------------------------
if "kb" not in st.session_state:
    st.session_state.kb = None
    st.session_state.poch = None


def get_poch():
    return st.session_state.poch


# ------------------------------------------------------------------
# Sidebar — load / set up your squad
# ------------------------------------------------------------------
with st.sidebar:
    st.header("Setup")
    team_id = st.number_input("FPL team ID", value=DEFAULT_TEAM_ID, step=1)
    bank = st.number_input("Bank (£m)", value=0.5, step=0.1, format="%.1f")

    if st.button("Load live data", use_container_width=True):
        with st.spinner("Pulling live FPL data..."):
            try:
                kb = load_live_knowledge_base(team_id=int(team_id))
                kb.bank = float(bank)
                st.session_state.kb = kb
                st.session_state.poch = Poch(kb)
                if kb.your_squad:
                    st.success(f"Loaded {len(kb.your_squad)} players from your team picks.")
                else:
                    st.warning(
                        "Loaded prices/fixtures, but couldn't pull your picks yet "
                        "(normal before this gameweek's deadline). Set your squad "
                        "manually below."
                    )
            except Exception as e:
                st.error(f"Couldn't load live data: {e}")

    kb = st.session_state.kb
    if kb is not None:
        st.divider()
        st.caption("Manually set squad (only needed if picks didn't load)")
        names_text = st.text_area(
            "Player names, one per line",
            placeholder="Kelleher\nLecomte\nKonsa\n...",
            height=120,
        )
        if st.button("Set squad by name", use_container_width=True):
            names = [n.strip() for n in names_text.splitlines() if n.strip()]
            missing_before = set(kb.players.keys())
            ids = set_squad_by_names(kb, names)
            if len(ids) != len(names):
                st.warning("Some names didn't match — check spelling and try again.")
            else:
                st.success(f"Squad set: {len(ids)} players.")

st.title("⚽ Poch")
st.caption("Your FPL decision agent — orchestrator, senate, and sub-apps in your pocket.")

poch = get_poch()

if poch is None:
    st.info("Load your team from the sidebar to get started.")
    st.stop()

kb = st.session_state.kb
if not kb.your_squad:
    st.warning("No squad set yet — use the sidebar to load live data or set your squad by name.")
    st.stop()

# ------------------------------------------------------------------
# Tabs — each maps to a Poch capability
# ------------------------------------------------------------------
tab_lineup, tab_transfers, tab_squad, tab_community = st.tabs(
    ["📋 Lineup", "🔄 Transfers", "🧢 Squad", "💬 Community"]
)

with tab_lineup:
    if st.button("Pick this week's lineup", type="primary"):
        result = poch.pick_lineup()
        if "error" in result:
            st.error(result["error"])
        else:
            lu = result["lineup"]
            st.subheader(f"{lu['formation']} — {lu['total_projected']} pts projected")

            st.markdown("**Starting XI**")
            for pid in lu["starting_ids"]:
                p = kb.players[pid]
                st.write(f"• {p.web_name} ({p.position}, £{p.cost_m}m)")

            st.markdown("**Bench (in order)**")
            st.write(", ".join(kb.players[pid].web_name for pid in lu["bench_ids"]))

            cap = result.get("captain")
            if cap:
                st.markdown(f"**Captain:** {cap['captain_name']}  |  **Vice:** {cap['vice_name']}")

            if result.get("chip_advice"):
                st.markdown("**Chip advice**")
                for c in result["chip_advice"]:
                    st.write(f"• {c['chip']}: {c['reason']}")

            if result.get("cautions"):
                st.warning("Senate cautions:\n" + "\n".join(f"- {c}" for c in result["cautions"]))
            else:
                st.success("Senate: no cautions raised.")

with tab_transfers:
    st.caption("Weekly suggestions, reviewed by the senate")
    if st.button("Suggest transfers"):
        proposals = poch.suggest_transfers()
        if not proposals:
            st.info("No transfers currently clear the senate with a positive net gain.")
        else:
            for t in proposals:
                hit = " (after a -4 hit)" if t["hit_applies"] else ""
                st.markdown(f"**{t['player_out']} → {t['player_in']}**")
                st.write(f"Net gain: {t['net_gain_after_hit']} pts{hit} · Cost change: £{t['cost_delta_m']}m")
                for c in t.get("cautions", []):
                    st.caption(f"⚠️ {c}")
                st.divider()

    st.caption("Evaluate a specific swap")
    col1, col2 = st.columns(2)
    with col1:
        out_name = st.text_input("Player out")
    with col2:
        in_name = st.text_input("Player in")
    if st.button("Evaluate transfer"):
        by_name = {p.web_name.lower(): p.id for p in kb.players.values()}
        out_id, in_id = by_name.get(out_name.strip().lower()), by_name.get(in_name.strip().lower())
        if not out_id or not in_id:
            st.error("Couldn't match one or both names — check spelling.")
        else:
            result = poch.evaluate_transfer(out_id, in_id)
            if "error" in result:
                st.error(result["error"])
            else:
                verdict = "✅ Recommended" if result["recommend"] else "❌ Not recommended"
                st.markdown(f"**{verdict}**")
                st.write(
                    f"Projected gain: {result['projected_gain_over_horizon']} pts "
                    f"(net after hit: {result['net_gain_after_hit']}) · "
                    f"Cost change: £{result['cost_delta_m']}m"
                )
                if result.get("veto_reason"):
                    st.error(f"Senate veto: {result['veto_reason']}")
                for c in result.get("cautions", []):
                    st.caption(f"⚠️ {c}")

with tab_squad:
    st.markdown("**Current 15**")
    for pid in kb.your_squad:
        p = kb.players.get(pid)
        if p:
            st.write(f"• {p.web_name} ({p.position}, £{p.cost_m}m)")
    st.caption(f"Bank: £{kb.bank}m · Free transfers: {kb.free_transfers}")

    st.divider()
    if st.button("Rebuild squad from scratch"):
        result = poch.build_squad()
        if "error" in result:
            st.error(result["error"])
        else:
            sq = result["squad"]
            st.success(f"New squad — £{sq['bank_remaining']}m left, {sq['total_projected']} pts projected")
            for pid in sq["player_ids"]:
                p = kb.players[pid]
                st.write(f"• {p.web_name} ({p.position}, £{p.cost_m}m)")

with tab_community:
    st.caption(
        "Feed in community intel yourself (e.g. things Claude finds when you "
        "ask it to search FPL forums/sites), then Poch's Community senator "
        "will factor it into its verdicts."
    )
    with st.form("add_signal"):
        sig_type = st.selectbox(
            "Signal type", ["must_have", "hidden_gem", "enabler", "gw_strategy", "avoid"]
        )
        subject = st.text_input("Player name (or 'Captaincy' for strategy notes)")
        summary = st.text_area("Summary")
        source_count = st.number_input("Number of sources", value=1, min_value=1, step=1)
        submitted = st.form_submit_button("Add signal")
        if submitted and subject and summary:
            ingest_signals(kb, [CommunitySignal(sig_type, subject, summary, source_count=int(source_count))])
            st.success("Signal added.")

    if kb.community_signals:
        rec = poch.community_recommendations()
        for title, key in [
            ("Must-haves", "must_haves"),
            ("Hidden gems", "hidden_gems"),
            ("Enablers", "enablers"),
            ("Gameweek strategy", "gw_strategy"),
            ("Avoid", "avoids"),
        ]:
            items = rec[key]
            if items:
                st.markdown(f"**{title}**")
                for s in items:
                    st.write(f"• {s['subject']}: {s['summary']} ({s['source_count']} source(s))")
    else:
        st.info("No community intel loaded yet.")
