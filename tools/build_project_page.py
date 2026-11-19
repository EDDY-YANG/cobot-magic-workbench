"""Build the public project page. All visible prose has an EN/ZH counterpart."""
from pathlib import Path
from html import escape

ROOT=Path(__file__).resolve().parents[1]
SITE=ROOT/"site"
SITE.mkdir(exist_ok=True)
def t(en,zh):
    return '<span data-language="en">'+en+'</span><span data-language="zh" hidden>'+zh+'</span>'
def p(en,zh,cls=""):
    return f'<p class="{cls}">'+t(en,zh)+'</p>'
def h(en,zh):
    return '<h2>'+t(en,zh)+'</h2>'
def top(manual=False):
    links = ('<a href="index.html">'+t("Project","项目")+'</a><a href="#setup">'+t("Get started","开始使用")+'</a>') if manual else ('<a href="index.html#capabilities">'+t("Capabilities","功能")+'</a><a href="index.html#workflow">'+t("Workflow","流程")+'</a><a href="manual.html">'+t("Manual","手册")+'</a>')
    return '<div class="topbar"><div class="topbar-inner"><a class="brand" href="index.html">EMERGE ROBOTICS</a><nav aria-label="Main">'+links+'</nav><button class="lang-toggle" type="button" data-language-toggle>中文</button></div></div>'
def frame(body,en,zh):
    return '<!doctype html><html lang="en" data-title-en="'+escape(en,quote=True)+'" data-title-zh="'+escape(zh,quote=True)+'"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><meta name="description" content="A workbench in development for COBOT MAGIC, an RTX 4090 host and a personal PC. Emerge Robotics."><meta name="color-scheme" content="light"><title>'+en+'</title><link rel="icon" href="assets/icon.svg" type="image/svg+xml"><link rel="stylesheet" href="assets/theme.css"></head><body>'+body+'<script src="assets/site.js" defer></script></body></html>'
def figure(name,alt,en,zh):
    return '<figure class="diagram"><a href="assets/figures/'+name+'.svg" target="_blank" rel="noopener"><img src="assets/figures/'+name+'.svg" alt="'+escape(alt,quote=True)+'" loading="lazy"></a><figcaption>'+t(en,zh)+'</figcaption><div class="figure-actions"><a href="assets/figures/'+name+'.svg" download>SVG</a><a href="assets/figures/'+name+'.png" download>PNG</a></div></figure>'
def footer():
    return '<footer><div>Yang Taige · Ye Hanzhi<br>Emerge Robotics · 2026</div><div class="footer-links"><a href="manual.html">'+t("User manual","使用手册")+'</a><a href="https://eddy-yang.github.io/quest3-dual-e6-teleoperation/">Quest 3 / Dual E6</a><a href="LICENSE.txt">MIT License</a></div></footer>'

def build():
    from build_public_release import build as release_build
    release_build()

if __name__=='__main__':build()
