"""ReproKit visual identity: warm editorial theme, logo, hero, frames, quotes.

Presentation only — no product logic lives here.

Includes a light/dark palette (switched with the lamp control) and a CSS-only
bubble swarm that settles into the letters R and K when released.
"""

from __future__ import annotations

import html
from string import Template
from typing import Iterable

LIGHT = {
    "bg": "#FAF7F1",
    "surface": "#FFFFFF",
    "card": "#FFFFFF",
    "sand": "#F2ECE1",
    "sand_deep": "#EAE1D2",
    "ink": "#23211E",
    "body": "#4C4740",
    "muted": "#756F66",
    "line": "#E4DACB",
    "accent": "#C15F3C",
    "accent_deep": "#9E4829",
    "accent_soft": "#F6E4DA",
    "gold": "#B0832F",
    "code_bg": "#262421",
    "code_text": "#F3EADB",
    "code_line": "#37322C",
    "input_bg": "#FFFDF9",
    "chip_bg": "rgba(255,255,255,.72)",
    "hero_from": "#FFFDF8",
    "hero_to": "#FBF6ED",
    "hero_glow": "#F7E3D6",
    "hero_glow2": "#F3EADC",
    "shadow": "0 1px 2px rgba(35, 33, 30, .04), 0 12px 32px -18px rgba(35, 33, 30, .25)",
    "shadow_deep": "0 2px 4px rgba(35,33,30,.04), 0 30px 70px -46px rgba(35,33,30,.45)",
    "scroll": "#D9CDBD",
    "bubble_a": "#E9A184",
    "bubble_b": "#D9BE8B",
    "bubble_c": "#C9B7A2",
}

DARK = {
    "bg": "#141211",
    "surface": "#1E1B19",
    "card": "#1E1B19",
    "sand": "#241F1B",
    "sand_deep": "#2C2521",
    "ink": "#F4EEE6",
    "body": "#D9D0C5",
    "muted": "#A79C90",
    "line": "#352D27",
    "accent": "#D97757",
    "accent_deep": "#EDA184",
    "accent_soft": "#3A2822",
    "gold": "#D6A85A",
    "code_bg": "#0D0C0B",
    "code_text": "#F1E7D8",
    "code_line": "#2A2421",
    "input_bg": "#221E1B",
    "chip_bg": "rgba(255,255,255,.04)",
    "hero_from": "#221E1B",
    "hero_to": "#191615",
    "hero_glow": "#3A2822",
    "hero_glow2": "#2A2320",
    "shadow": "0 1px 2px rgba(0, 0, 0, .35), 0 14px 34px -20px rgba(0, 0, 0, .8)",
    "shadow_deep": "0 2px 4px rgba(0,0,0,.4), 0 30px 70px -46px rgba(0,0,0,.9)",
    "scroll": "#3A322C",
    "bubble_a": "#E08A64",
    "bubble_b": "#B99A66",
    "bubble_c": "#7C6E5F",
}

_SERIF = '"Iowan Old Style", "Palatino Linotype", "Book Antiqua", Georgia, serif'
_SANS = '"Inter", "Segoe UI", system-ui, -apple-system, sans-serif'
_MONO = '"JetBrains Mono", "Cascadia Code", Consolas, "SFMono-Regular", monospace'

_CSS = Template("""
<style>
:root {
  --rk-bg: $bg;
  --rk-surface: $surface;
  --rk-card: $card;
  --rk-sand: $sand;
  --rk-sand-deep: $sand_deep;
  --rk-ink: $ink;
  --rk-body: $body;
  --rk-muted: $muted;
  --rk-line: $line;
  --rk-accent: $accent;
  --rk-accent-deep: $accent_deep;
  --rk-accent-soft: $accent_soft;
  --rk-gold: $gold;
  --rk-code-bg: $code_bg;
  --rk-code-text: $code_text;
  --rk-input-bg: $input_bg;
  --rk-chip-bg: $chip_bg;
  --rk-shadow: $shadow;
  --rk-shadow-deep: $shadow_deep;
  --rk-serif: $serif;
  --rk-sans: $sans;
  --rk-mono: $mono;
  --rk-radius: 18px;
}

html, body, [class*="css"] { font-family: var(--rk-sans); }

[data-testid="stAppViewContainer"] {
  background:
    radial-gradient(900px 500px at 8% -8%, $hero_glow 0%, rgba(0,0,0,0) 60%),
    radial-gradient(800px 460px at 100% 0%, $hero_glow2 0%, rgba(0,0,0,0) 55%),
    var(--rk-bg);
  color: var(--rk-ink);
}

.stApp::before {
  content: "";
  position: fixed;
  inset: 0 0 auto 0;
  height: 4px;
  background: linear-gradient(90deg, $accent 0%, $accent_deep 45%, $gold 100%);
  z-index: 1000000;
}

[data-testid="stHeader"] { background: transparent; }
[data-testid="stToolbar"] { right: 1.1rem; }

.block-container, [data-testid="stMainBlockContainer"] {
  max-width: 1160px;
  padding-top: 2.1rem;
  padding-bottom: 4.5rem;
}

h1, h2, h3, h4 { font-family: var(--rk-serif); letter-spacing: -0.015em; color: var(--rk-ink); }
h1 { font-size: clamp(2.1rem, 3.6vw, 3.15rem); line-height: 1.06; margin-bottom: .3rem; }
h2 { font-size: clamp(1.55rem, 2.2vw, 2rem); margin-top: .2rem; }
h3 { font-size: 1.22rem; }
p, li, label, .stMarkdown { font-size: 15.5px; line-height: 1.68; color: var(--rk-ink); }
[data-testid="stCaptionContainer"], .stCaption { color: var(--rk-muted); }
a { color: var(--rk-accent-deep); }
hr { border-color: var(--rk-line); }

/* ---------- Hero ---------- */
.rk-hero {
  position: relative; overflow: hidden;
  border: 1px solid var(--rk-line);
  border-radius: 26px;
  padding: 30px 32px 26px;
  margin: 6px 0 22px;
  background:
    radial-gradient(620px 260px at 92% -10%, $hero_glow 0%, rgba(0,0,0,0) 62%),
    linear-gradient(160deg, $hero_from 0%, $hero_to 100%);
  box-shadow: var(--rk-shadow-deep);
}
.rk-hero::after {
  content: ""; position: absolute; right: -70px; bottom: -110px;
  width: 260px; height: 260px; border-radius: 50%;
  border: 1px solid rgba(193, 95, 60, .18);
}
.rk-hero-top { display: flex; gap: 18px; align-items: flex-start; }
.rk-logo { flex: 0 0 auto; width: 58px; height: 58px; filter: drop-shadow(0 8px 16px rgba(193, 95, 60, .22)); }
.rk-brand { flex: 1 1 auto; min-width: 0; }
.rk-kicker {
  font-family: var(--rk-mono); font-size: 11.5px; letter-spacing: .16em;
  text-transform: uppercase; color: var(--rk-accent-deep); margin-bottom: 6px;
}
.rk-tagline { font-family: var(--rk-serif); font-size: 1.12rem; color: var(--rk-muted); margin: .1rem 0 0; }
.rk-lede { max-width: 74ch; margin: 18px 0 0; color: var(--rk-body); }
.rk-chips { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 18px; }
.rk-chip {
  display: inline-flex; align-items: center; gap: 7px;
  padding: 6px 12px; border-radius: 999px;
  border: 1px solid var(--rk-line); background: var(--rk-chip-bg);
  font-size: 12.5px; color: var(--rk-muted);
}
.rk-chip::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: var(--rk-accent); }
.rk-term {
  margin-top: 20px; display: inline-flex; align-items: center; gap: 10px;
  padding: 9px 14px; border-radius: 12px;
  background: #262421; border: 1px solid #37322C; color: #F3EADB;
  font-family: var(--rk-mono); font-size: 12.5px;
}
.rk-term .rk-prompt { color: #D9A17E; }
.rk-term .rk-ok { color: #9CBFA2; }
.rk-term .rk-bad { color: #E4A08C; }

/* ---------- Section frames ---------- */
.rk-section { margin: 26px 0 10px; }
.rk-section .rk-kicker { margin-bottom: 4px; }
.rk-section p.rk-hint { margin: 4px 0 0; color: var(--rk-muted); font-size: 13.5px; max-width: 82ch; }

/* ---------- Cards, metrics, expanders ---------- */
[data-testid="stVerticalBlockBorderWrapper"] {
  background: var(--rk-card);
  border: 1px solid var(--rk-line) !important;
  border-radius: var(--rk-radius);
  box-shadow: var(--rk-shadow);
}
[data-testid="stMetric"] {
  background: var(--rk-card); border: 1px solid var(--rk-line);
  border-radius: var(--rk-radius); padding: 14px 16px 12px;
  box-shadow: var(--rk-shadow);
}
[data-testid="stMetricValue"] { font-family: var(--rk-serif); color: var(--rk-ink); font-size: 1.5rem; }
[data-testid="stMetricLabel"] {
  font-family: var(--rk-mono); text-transform: uppercase;
  letter-spacing: .1em; font-size: 10.5px; color: var(--rk-muted);
}
[data-testid="stExpander"] {
  background: var(--rk-card); border: 1px solid var(--rk-line);
  border-radius: var(--rk-radius); box-shadow: var(--rk-shadow); overflow: hidden;
}
[data-testid="stExpander"] summary { font-weight: 600; }
[data-testid="stExpander"] summary:hover { color: var(--rk-accent-deep); }

/* ---------- Buttons ---------- */
.stButton > button, .stDownloadButton > button,
[data-testid="stBaseButton-secondary"], [data-testid="stBaseButton-primary"] {
  border-radius: 12px; font-weight: 600; padding: .56rem 1.05rem;
  border: 1px solid var(--rk-line); background: var(--rk-card); color: var(--rk-ink);
  transition: transform .14s ease, box-shadow .14s ease, background .14s ease;
}
.stButton > button:hover, .stDownloadButton > button:hover {
  transform: translateY(-1px); box-shadow: 0 10px 22px -14px rgba(0, 0, 0, .45);
  border-color: var(--rk-accent);
}
[data-testid="stBaseButton-primary"] {
  background: var(--rk-accent) !important; border-color: var(--rk-accent) !important;
  color: #FFF8F1 !important;
}
[data-testid="stBaseButton-primary"]:hover { background: var(--rk-accent-deep) !important; }

/* ---------- Mode switch ---------- */
[data-testid="stButtonGroup"] {
  background: var(--rk-sand); padding: 5px; border-radius: 999px;
  border: 1px solid var(--rk-line); width: fit-content !important;
  max-width: 100%; margin: 0 auto 2px;
}
[data-testid="stButtonGroup"] button {
  border-radius: 999px !important; border: 0 !important;
  padding: .4rem 1.15rem !important; font-weight: 600; color: var(--rk-ink);
}
[data-testid="stButtonGroup"] button[kind="segmented_controlActive"],
[data-testid="stButtonGroup"] button[aria-checked="true"] {
  background: var(--rk-accent) !important; color: #FFF8F1 !important;
  box-shadow: 0 6px 14px -8px rgba(193, 95, 60, .9);
}

/* ---------- Inputs ---------- */
[data-testid="stTextArea"] textarea, [data-testid="stTextInput"] input,
[data-baseweb="select"] > div {
  background: var(--rk-input-bg) !important; color: var(--rk-ink) !important;
  border-radius: 12px !important; border-color: var(--rk-line) !important;
}
[data-testid="stTextArea"] textarea:focus, [data-testid="stTextInput"] input:focus {
  border-color: var(--rk-accent) !important;
  box-shadow: 0 0 0 3px rgba(193, 95, 60, .16) !important;
}
[data-testid="stFileUploaderDropzone"] {
  background: linear-gradient(180deg, $hero_from 0%, $hero_to 100%);
  border: 1.5px dashed $accent;
  border-radius: var(--rk-radius); padding: 1.2rem; opacity: .96;
}
[data-testid="stForm"] {
  background: var(--rk-card); border: 1px solid var(--rk-line);
  border-radius: var(--rk-radius); padding: 1rem 1.1rem; box-shadow: var(--rk-shadow);
}

/* ---------- Alerts ---------- */
[data-testid="stAlert"] {
  border-radius: 14px; border: 1px solid var(--rk-line);
  box-shadow: none; background: var(--rk-card); color: var(--rk-ink);
}

/* ---------- Code ---------- */
[data-testid="stCode"] pre, .stCode pre {
  background: var(--rk-code-bg) !important;
  border: 1px solid var(--rk-code-line) !important;
  border-radius: 14px !important;
}
[data-testid="stCode"] code, .stCode code {
  color: var(--rk-code-text) !important;
  font-family: var(--rk-mono) !important; font-size: 12.8px !important;
}

/* ---------- Steps ---------- */
.rk-steps { display: grid; grid-template-columns: repeat(5, minmax(0, 1fr)); gap: 10px; margin: 8px 0 4px; }
.rk-step {
  border: 1px solid var(--rk-line); border-radius: 14px;
  background: var(--rk-chip-bg); padding: 10px 12px;
  font-size: 12.5px; color: var(--rk-muted);
}
.rk-step b { display: block; color: var(--rk-ink); font-size: 12.8px; line-height: 1.35; margin-top: 3px; }
.rk-step .rk-num { font-family: var(--rk-mono); font-size: 10.5px; letter-spacing: .12em; color: var(--rk-muted); }
.rk-step.is-done { border-color: var(--rk-line); background: var(--rk-card); }
.rk-step.is-done .rk-num { color: var(--rk-gold); }
.rk-step.is-current {
  border-color: var(--rk-accent); background: var(--rk-accent-soft);
  box-shadow: 0 10px 22px -16px rgba(193, 95, 60, .8);
}
.rk-step.is-current .rk-num { color: var(--rk-accent-deep); }

/* ---------- Quote + footer ---------- */
.rk-quote {
  margin: 30px 0 8px; padding: 18px 22px;
  border-left: 3px solid var(--rk-accent);
  background: linear-gradient(90deg, $accent_soft, rgba(0,0,0,0));
  border-radius: 0 14px 14px 0;
}
.rk-quote p { font-family: var(--rk-serif); font-size: 1.12rem; font-style: italic; margin: 0; color: var(--rk-body); }
.rk-quote span {
  display: block; margin-top: 8px; font-family: var(--rk-mono);
  font-size: 10.5px; letter-spacing: .12em; text-transform: uppercase; color: var(--rk-muted);
}
.rk-footer {
  margin-top: 34px; padding-top: 16px; border-top: 1px solid var(--rk-line);
  display: flex; justify-content: space-between; gap: 14px; flex-wrap: wrap;
  color: var(--rk-muted); font-family: var(--rk-mono);
  font-size: 10.5px; letter-spacing: .1em; text-transform: uppercase;
}

/* ---------- Landing / intro ---------- */
.rk-landing {
  position: relative; overflow: hidden;
  border: 1px solid var(--rk-line); border-radius: 30px;
  padding: 46px 46px 38px; margin: 8px 0 22px;
  background:
    radial-gradient(760px 340px at 88% -18%, $hero_glow 0%, rgba(0,0,0,0) 60%),
    radial-gradient(520px 320px at -6% 108%, $hero_glow2 0%, rgba(0,0,0,0) 62%),
    linear-gradient(158deg, $hero_from 0%, $hero_to 100%);
  box-shadow: var(--rk-shadow-deep);
}
.rk-landing::before {
  content: ""; position: absolute; top: 30px; right: 34px;
  width: 132px; height: 132px; border-radius: 32px;
  border: 1px solid rgba(193, 95, 60, .16); transform: rotate(12deg);
}
.rk-landing::after {
  content: ""; position: absolute; right: 92px; bottom: -80px;
  width: 240px; height: 240px; border-radius: 50%;
  border: 1px solid rgba(176, 131, 47, .14);
}
.rk-brandmark { display: flex; align-items: center; gap: 16px; margin-bottom: 26px; }
.rk-brandmark .rk-logo { width: 64px; height: 64px; }
.rk-name {
  font-family: var(--rk-serif); font-size: clamp(2.9rem, 6vw, 4.4rem);
  line-height: .95; letter-spacing: -.03em; margin: 0; color: var(--rk-ink);
}
.rk-name em { font-style: italic; color: var(--rk-accent-deep); }
.rk-subname {
  font-family: var(--rk-mono); font-size: 11.5px; letter-spacing: .2em;
  text-transform: uppercase; color: var(--rk-muted); margin-top: 10px;
}
.rk-purpose {
  font-family: var(--rk-serif); font-size: clamp(1.18rem, 1.8vw, 1.5rem);
  line-height: 1.55; color: var(--rk-body); max-width: 62ch; margin: 0;
}
.rk-purpose strong { color: var(--rk-accent-deep); font-weight: 600; }
.rk-pillars { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; margin: 18px 0 6px; }
.rk-pillar {
  border: 1px solid var(--rk-line); border-radius: 18px;
  background: var(--rk-card); padding: 18px 18px 16px; box-shadow: var(--rk-shadow);
}
.rk-pillar .rk-num { font-family: var(--rk-mono); font-size: 10.5px; letter-spacing: .16em; color: var(--rk-accent-deep); }
.rk-pillar b { display: block; font-family: var(--rk-serif); font-size: 1.18rem; margin: 6px 0 6px; color: var(--rk-ink); }
.rk-pillar p { margin: 0; font-size: 13.8px; line-height: 1.62; color: var(--rk-body); }
.rk-struggles { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 14px; margin: 16px 0 6px; }
.rk-struggle {
  position: relative; border: 1px solid var(--rk-line); border-radius: 18px;
  background: linear-gradient(170deg, $hero_from 0%, $hero_to 100%);
  padding: 20px 20px 18px; box-shadow: var(--rk-shadow);
}
.rk-struggle::before {
  content: "\\201C"; position: absolute; top: 6px; right: 16px;
  font-family: var(--rk-serif); font-size: 46px; line-height: 1;
  color: rgba(193, 95, 60, .22);
}
.rk-struggle p {
  font-family: var(--rk-serif); font-style: italic; font-size: 1.02rem;
  line-height: 1.58; margin: 0 0 12px; color: var(--rk-body);
}
.rk-struggle span { font-family: var(--rk-mono); font-size: 10px; letter-spacing: .12em; text-transform: uppercase; color: var(--rk-muted); }
.rk-voices { display: grid; grid-template-columns: 1.15fr .85fr; gap: 14px; margin-top: 16px; }
.rk-voice { border-radius: 18px; padding: 20px 22px; border: 1px solid var(--rk-line); background: var(--rk-card); }
.rk-voice.is-accent { background: linear-gradient(150deg, $accent_soft 0%, $hero_to 100%); border-color: var(--rk-line); }
.rk-voice p { font-family: var(--rk-serif); font-size: 1.06rem; line-height: 1.6; margin: 0 0 10px; color: var(--rk-body); }
.rk-voice.is-accent p { font-style: italic; }
.rk-voice span { font-family: var(--rk-mono); font-size: 10px; letter-spacing: .12em; text-transform: uppercase; color: var(--rk-muted); }
.rk-cta-note { margin-top: 18px; display: flex; flex-wrap: wrap; gap: 10px 22px; align-items: center; color: var(--rk-muted); font-size: 13px; }
.rk-cta-note b { color: var(--rk-ink); font-weight: 600; }

/* ---------- Bubble swarm ---------- */
.rk-swarm {
  position: relative; overflow: hidden;
  height: 330px; margin: 4px 0 6px;
  border: 1px solid var(--rk-line); border-radius: 26px;
  background:
    radial-gradient(560px 260px at 50% 120%, $hero_glow 0%, rgba(0,0,0,0) 68%),
    linear-gradient(180deg, $hero_from 0%, $hero_to 100%);
  box-shadow: var(--rk-shadow-deep);
  transition: background .6s ease;
}
.rk-swarm-label {
  position: absolute; left: 18px; top: 14px;
  font-family: var(--rk-mono); font-size: 10px; letter-spacing: .18em;
  text-transform: uppercase; color: var(--rk-muted); opacity: .8;
}
.rk-swarm-hint {
  position: absolute; left: 0; right: 0; bottom: 14px; text-align: center;
  font-family: var(--rk-serif); font-style: italic; font-size: 1rem;
  color: var(--rk-muted);
}
.rk-bub {
  position: absolute; left: var(--x); top: var(--y);
  width: var(--s); height: var(--s); border-radius: 50%;
  background: radial-gradient(circle at 32% 28%, rgba(255,255,255,.92), var(--c) 62%);
  box-shadow: 0 8px 18px -10px rgba(0,0,0,.55), inset 0 -2px 6px rgba(0,0,0,.10);
  opacity: .82;
  animation: rk-drift 9s ease-in-out infinite alternate;
  animation-delay: var(--d);
  will-change: transform;
}
.rk-swarm.is-settled .rk-bub {
  animation: rk-settle 2.4s cubic-bezier(.16,.84,.24,1) both;
  animation-delay: var(--d);
}
.rk-swarm.is-settled .rk-swarm-hint { animation: rk-fade 1.6s ease .9s both; }
@keyframes rk-drift {
  from { transform: translate(0, 0) scale(1); }
  to   { transform: translate(calc(var(--dx) * .22), calc(var(--dy) * .22)) scale(1.04); }
}
@keyframes rk-settle {
  0%   { transform: translate(var(--dx), var(--dy)) scale(.55); opacity: .25; }
  55%  { opacity: 1; }
  100% { transform: translate(0, 0) scale(1); opacity: 1; }
}
@keyframes rk-fade { from { opacity: 0; } to { opacity: .85; } }
@media (prefers-reduced-motion: reduce) {
  .rk-bub, .rk-swarm.is-settled .rk-bub { animation: none; }
}

/* ---------- Lamp ---------- */
.rk-lamp { display: flex; align-items: center; gap: 10px; }
.rk-lamp svg { width: 46px; height: 46px; flex: 0 0 auto; }
.rk-lamp .rk-glow { transition: opacity .35s ease, fill .35s ease; }

@media (max-width: 900px) {
  .rk-steps { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .rk-hero { padding: 24px 20px 22px; }
  .rk-landing { padding: 28px 22px 24px; }
  .rk-pillars, .rk-struggles, .rk-voices { grid-template-columns: 1fr; }
  .rk-swarm { height: 280px; }
}

/* ---------- Scrollbars ---------- */
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-thumb { background: $scroll; border-radius: 999px; }
::-webkit-scrollbar-track { background: transparent; }
</style>
""")


def _tokens(dark: bool) -> dict[str, str]:
    palette = dict(DARK if dark else LIGHT)
    palette.update(serif=_SERIF, sans=_SANS, mono=_MONO)
    return palette


def inject(dark: bool = False) -> None:
    """Inject the theme (light or dark) once per script run."""
    import streamlit as st

    st.markdown(_CSS.substitute(_tokens(dark)), unsafe_allow_html=True)


def _esc(value: str) -> str:
    return html.escape(value or "", quote=True)


def logo(size: int = 58) -> str:
    """Inline SVG mark: warm tile, serif R, small spark."""
    return f"""
<svg class="rk-logo" width="{size}" height="{size}" viewBox="0 0 64 64"
     xmlns="http://www.w3.org/2000/svg" role="img" aria-label="ReproKit logo">
  <defs>
    <linearGradient id="rkTile" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="#D97757"/>
      <stop offset="100%" stop-color="#A9472A"/>
    </linearGradient>
  </defs>
  <rect x="2" y="2" width="60" height="60" rx="18" fill="url(#rkTile)"/>
  <rect x="2.75" y="2.75" width="58.5" height="58.5" rx="17.25"
        fill="none" stroke="rgba(255,248,241,.35)" stroke-width="1.5"/>
  <text x="30" y="43" text-anchor="middle" font-family="Georgia, 'Times New Roman', serif"
        font-size="31" font-weight="700" fill="#FFF8F1">R</text>
  <circle cx="47.5" cy="18" r="3.2" fill="#F6D9A8"/>
</svg>
"""


def lamp(dark: bool) -> str:
    """Desk-lamp SVG. Lit = dark mode (lamps are for the dark)."""
    bulb = "#F6D9A8" if dark else "#5A534B"
    glow = "opacity:1" if dark else "opacity:0"
    arm = "#8A7F73" if dark else "#B7AA9A"
    shade = "#D97757" if dark else "#CDBFAE"
    return f"""
<svg viewBox="0 0 64 64" xmlns="http://www.w3.org/2000/svg" role="img"
     aria-label="{'Lamp on — dark theme' if dark else 'Lamp off — light theme'}">
  <circle class="rk-glow" cx="32" cy="26" r="21" fill="#F6D9A8" style="{glow}" opacity="0"/>
  <path d="M18 46 h28" stroke="{arm}" stroke-width="3.4" stroke-linecap="round"/>
  <path d="M32 46 V28" stroke="{arm}" stroke-width="3.4" stroke-linecap="round"/>
  <path d="M22 28 h20 l-3.4 -10 h-13.2 Z" fill="{shade}"/>
  <circle cx="32" cy="31" r="3.6" fill="{bulb}"/>
  <path d="M32 16 V9" stroke="{arm}" stroke-width="2.2" stroke-linecap="round"/>
  <circle cx="32" cy="7" r="2.4" fill="{bulb}"/>
</svg>
"""


def lamp_control(dark: bool, *, key: str) -> bool:
    """Render the lamp + pull-cord button. Returns the new dark-mode flag."""
    import streamlit as st

    col_lamp, col_btn = st.columns([1, 1.25])
    with col_lamp:
        st.markdown(f'<div class="rk-lamp">{lamp(dark)}</div>', unsafe_allow_html=True)
        st.caption("Lamp on = dark")
    with col_btn:
        label = "Pull the cord" if not dark else "Snuff the lamp"
        if st.button(label, key=key, use_container_width=True):
            return not dark
    return dark


def hero(
    kicker: str,
    title: str,
    tagline: str,
    lede: str,
    *,
    chips: Iterable[str] = (),
    terminal: str = "",
) -> None:
    """Editorial hero with logo, serif headline, chips, and a terminal line."""
    import streamlit as st

    chip_html = "".join(f'<span class="rk-chip">{_esc(chip)}</span>' for chip in chips)
    term_html = (
        f'<div class="rk-term"><span class="rk-prompt">$</span>{terminal}</div>'
        if terminal
        else ""
    )
    st.markdown(
        f"""
<div class="rk-hero">
  <div class="rk-hero-top">
    {logo()}
    <div class="rk-brand">
      <div class="rk-kicker">{_esc(kicker)}</div>
      <h1>{_esc(title)}</h1>
      <p class="rk-tagline">{_esc(tagline)}</p>
    </div>
  </div>
  <p class="rk-lede">{_esc(lede)}</p>
  <div class="rk-chips">{chip_html}</div>
  {term_html}
</div>
        """,
        unsafe_allow_html=True,
    )


def section(kicker: str, title: str, hint: str = "") -> None:
    """Section header with a numbered kicker and optional hint."""
    import streamlit as st

    hint_html = f'<p class="rk-hint">{_esc(hint)}</p>' if hint else ""
    st.markdown(
        f"""
<div class="rk-section">
  <div class="rk-kicker">{_esc(kicker)}</div>
  <h2>{_esc(title)}</h2>
  {hint_html}
</div>
        """,
        unsafe_allow_html=True,
    )


def quote(text: str, attribution: str) -> None:
    """Editorial pull-quote used between major blocks."""
    import streamlit as st

    st.markdown(
        f"""
<div class="rk-quote">
  <p>&ldquo;{_esc(text)}&rdquo;</p>
  <span>{_esc(attribution)}</span>
</div>
        """,
        unsafe_allow_html=True,
    )


def footer_quote() -> None:
    """Closing quote + brand rule."""
    quote("Show the run, not the opinion. Evidence first, elegance second.", "ReproKit")
    import streamlit as st

    st.markdown(
        """
<div class="rk-footer">
  <span>ReproKit &middot; evidence over intuition</span>
  <span>Logic Mode / Magic Mode</span>
</div>
        """,
        unsafe_allow_html=True,
    )


def workflow_steps(steps, current_index: int) -> None:
    """Compact step rail for Magic Mode (presentation only)."""
    import streamlit as st

    cells = []
    for index, (_key, label) in enumerate(steps):
        state = (
            "is-done"
            if index < current_index
            else "is-current" if index == current_index else ""
        )
        cells.append(
            f'<div class="rk-step {state}">'
            f'<span class="rk-num">{index + 1:02d}</span>'
            f"<b>{_esc(label)}</b></div>"
        )
    st.markdown(f'<div class="rk-steps">{"".join(cells)}</div>', unsafe_allow_html=True)


def landing_hero(
    *,
    kicker: str,
    name: str,
    emphasis: str,
    subname: str,
    purpose_html: str,
    chips: Iterable[str] = (),
) -> None:
    """Large intro lockup: logo, product name, one-paragraph purpose."""
    import streamlit as st

    chip_html = "".join(f'<span class="rk-chip">{_esc(chip)}</span>' for chip in chips)
    st.markdown(
        f"""
<div class="rk-landing">
  <div class="rk-brandmark">
    {logo(64)}
    <div>
      <div class="rk-kicker">{_esc(kicker)}</div>
      <h1 class="rk-name">{_esc(name)}<em>{_esc(emphasis)}</em></h1>
      <div class="rk-subname">{_esc(subname)}</div>
    </div>
  </div>
  <p class="rk-purpose">{purpose_html}</p>
  <div class="rk-chips">{chip_html}</div>
</div>
        """,
        unsafe_allow_html=True,
    )


def pillars(items: Iterable[tuple[str, str, str]]) -> None:
    """Three-up explainer cards: (number label, title, body)."""
    import streamlit as st

    cells = "".join(
        f'<div class="rk-pillar"><div class="rk-num">{_esc(num)}</div>'
        f"<b>{_esc(title)}</b><p>{_esc(body)}</p></div>"
        for num, title, body in items
    )
    st.markdown(f'<div class="rk-pillars">{cells}</div>', unsafe_allow_html=True)


def struggles(items: Iterable[tuple[str, str]]) -> None:
    """Developer pain-point quotes: (quote, attribution)."""
    import streamlit as st

    cells = "".join(
        f'<div class="rk-struggle"><p>{_esc(text)}</p>'
        f"<span>{_esc(who)}</span></div>"
        for text, who in items
    )
    st.markdown(f'<div class="rk-struggles">{cells}</div>', unsafe_allow_html=True)


def voices(primary: tuple[str, str], secondary: tuple[str, str]) -> None:
    """Two-up quote panel: a real attributed quote plus a team voice."""
    import streamlit as st

    (p_text, p_who), (s_text, s_who) = primary, secondary
    st.markdown(
        f"""
<div class="rk-voices">
  <div class="rk-voice is-accent">
    <p>&ldquo;{_esc(p_text)}&rdquo;</p>
    <span>{_esc(p_who)}</span>
  </div>
  <div class="rk-voice">
    <p>{_esc(s_text)}</p>
    <span>{_esc(s_who)}</span>
  </div>
</div>
        """,
        unsafe_allow_html=True,
    )


def cta_note(items: Iterable[tuple[str, str]]) -> None:
    """Small reassurance row under a call to action."""
    import streamlit as st

    line = "".join(f"<span><b>{_esc(a)}</b> {_esc(b)}</span>" for a, b in items)
    st.markdown(f'<div class="rk-cta-note">{line}</div>', unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Bubble swarm: drifts, then settles into R and K
# ---------------------------------------------------------------------------
# 5x7 dot-matrix glyphs; "1" marks a bubble.
_GLYPH_R = (
    "11110",
    "10001",
    "10001",
    "11110",
    "10100",
    "10010",
    "10001",
)
_GLYPH_K = (
    "10001",
    "10010",
    "10100",
    "11000",
    "10100",
    "10010",
    "10001",
)


def _glyph_points(glyph: tuple[str, ...]) -> list[tuple[float, float]]:
    """Normalized (x, y) centres for filled cells of a glyph."""
    rows = len(glyph)
    cols = len(glyph[0])
    points = []
    for r, row in enumerate(glyph):
        for c, cell in enumerate(row):
            if cell == "1":
                points.append(((c + 0.5) / cols, (r + 0.5) / rows))
    return points


def _jitter(index: int) -> tuple[float, float, int]:
    """Deterministic pseudo-random scatter offsets (px) and delay (ms)."""
    dx = ((index * 47) % 190) - 95
    dy = ((index * 71) % 150) - 75
    delay = (index % 11) * 85
    return float(dx), float(dy), delay


def swarm(assembled: bool) -> None:
    """Bubble swarm: drifts when idle, settles into R/K when assembled."""
    import streamlit as st

    points: list[tuple[float, float]] = []
    # R sits slightly left of centre, K slightly right.
    for gx, glyph, width, x0 in (
        (1, _GLYPH_R, 0.34, 0.18),
        (2, _GLYPH_K, 0.34, 0.52),
    ):
        for px, py in _glyph_points(glyph):
            points.append((x0 + px * width, 0.14 + py * 0.70))

    # A few strays that never quite join the letters.
    strays = [(0.08, 0.72), (0.92, 0.30), (0.47, 0.06), (0.72, 0.86), (0.20, 0.10)]
    points.extend(strays)

    # CSS variables so the swarm re-colours with the theme.
    palette = (
        "var(--rk-accent)",
        "var(--rk-gold)",
        "color-mix(in srgb, var(--rk-sand-deep) 85%, var(--rk-accent) 15%)",
    )

    bubbles = []
    for index, (x, y) in enumerate(points):
        size = 11 + (index * 7) % 13
        dx, dy, delay = _jitter(index)
        color = palette[index % len(palette)]
        bubbles.append(
            f'<span class="rk-bub" style="--x:{x * 100:.2f}%;--y:{y * 100:.2f}%;'
            f'--s:{size}px;--dx:{dx}px;--dy:{dy}px;--d:{delay}ms;--c:{color}"></span>'
        )

    state = "is-settled" if assembled else "is-drifting"
    hint = (
        "R &middot; K &mdash; the swarm found its name."
        if assembled
        else "Click below to release the bug swarm."
    )
    st.markdown(
        f"""
<div class="rk-swarm {state}">
  <div class="rk-swarm-label">the swarm</div>
  {''.join(bubbles)}
  <div class="rk-swarm-hint">{hint}</div>
</div>
        """,
        unsafe_allow_html=True,
    )