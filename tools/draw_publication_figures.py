"""Render the publication's original editable diagrams and PNG previews."""
from pathlib import Path
from html import escape
import json
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "site" / "assets" / "figures"
OUT.mkdir(parents=True, exist_ok=True)
INK, MUTED, LINE = "#172331", "#627183", "#dce2e9"
BLUE, ORANGE, PURPLE, TEAL = "#2563eb", "#d97706", "#7c3aed", "#087e75"
FONT = Path("C:/Windows/Fonts/segoeui.ttf")
BOLD = Path("C:/Windows/Fonts/seguisb.ttf")
if not FONT.exists():
    FONT = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
    BOLD = Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")

class Diagram:
    def __init__(self, width, height, title):
        self.w, self.h = width, height
        self.image = Image.new("RGB", (width * 2, height * 2), "white")
        self.draw = ImageDraw.Draw(self.image)
        self.svg = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(title)}">',
                    '<style>text{font-family:Segoe UI,Arial,sans-serif}</style>',
                    '<rect width="100%" height="100%" fill="white"/>']
    def rect(self, x, y, w, h, fill="white", stroke=LINE, radius=8, width=1.4):
        self.svg.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}" stroke="{stroke}" stroke-width="{width}"/>')
        self.draw.rounded_rectangle((x*2,y*2,(x+w)*2,(y+h)*2),radius=radius*2,fill=fill,outline=stroke,width=max(1,round(width*2)))
    def text(self, x, y, text, size=17, color=INK, bold=False, anchor="start"):
        self.svg.append(f'<text x="{x}" y="{y}" fill="{color}" font-size="{size}" font-weight="{600 if bold else 400}" text-anchor="{anchor}">{escape(text)}</text>')
        font=ImageFont.truetype(str(BOLD if bold else FONT),size*2)
        self.draw.text((x*2,y*2),text,font=font,fill=color,anchor={"start":"ls","middle":"ms","end":"rs"}[anchor])
    def line(self, pts, color=LINE, width=2, dash=False, arrow=False):
        coords=" ".join(f"{x},{y}" for x,y in pts)
        self.svg.append(f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="{width}" stroke-linejoin="round" stroke-linecap="round"'+(' stroke-dasharray="6 5"' if dash else '')+'/>')
        for (x1,y1),(x2,y2) in zip(pts,pts[1:]):
            if dash:
                import math
                length=math.hypot(x2-x1,y2-y1)
                for a in range(0,int(length),11):
                    b=min(a+6,length)
                    self.draw.line((2*(x1+(x2-x1)*a/length),2*(y1+(y2-y1)*a/length),2*(x1+(x2-x1)*b/length),2*(y1+(y2-y1)*b/length)),fill=color,width=width*2)
            else:self.draw.line((x1*2,y1*2,x2*2,y2*2),fill=color,width=width*2)
        if arrow:
            import math
            (x0,y0),(x,y)=pts[-2:]
            a=math.atan2(y-y0,x-x0)
            tip=[(x,y),(x-9*math.cos(a-.4),y-9*math.sin(a-.4)),(x-9*math.cos(a+.4),y-9*math.sin(a+.4))]
            self.svg.append('<polygon points="'+" ".join(f"{a:.2f},{b:.2f}" for a,b in tip)+f'" fill="{color}"/>')
            self.draw.polygon([(a*2,b*2) for a,b in tip],fill=color)
    def label(self, x, y, title, subtitle, color=BLUE, w=305):
        self.rect(x,y,w,70,fill="#f7f9fc")
        self.rect(x,y,4,70,fill=color,stroke=color,radius=0)
        self.text(x+18,y+28,title,19,bold=True)
        self.text(x+18,y+51,subtitle,14,MUTED)
    def save(self,name):
        self.svg.append("</svg>")
        (OUT/(name+".svg")).write_text("\n".join(self.svg),encoding="utf8")
        self.image.save(OUT/(name+".png"))

d=Diagram(1160,624,"Data paths for observation, inference, execution and review")
d.text(24,34,"DATA LINK",14,MUTED,True)
for x,w,t in [(24,314,"OBSERVE"),(422,314,"DECIDE & VALIDATE"),(820,316,"EXECUTE & REVIEW")]:
    d.text(x,76,t,16,INK,True)
    d.line([(x,88),(x+w,88)],LINE,1)
d.label(24,115,"Front + wrist cameras","Frames, timestamps, camera identity",TEAL,w=314)
d.label(24,211,"Joint + gripper feedback","Measured state and freshness",TEAL,w=314)
d.label(422,115,"Operator or learned policy","Task, checkpoint, prompt, action chunk",PURPLE,w=314)
d.label(422,241,"Planning + admission","Limits, source, owner, model identity",PURPLE,w=314)
d.label(820,241,"Local robot executor","Targets via CAN / MOVEJ",BLUE,w=316)
d.line([(338,150),(390,150),(390,150),(422,150)],TEAL,2,arrow=True)
d.line([(338,246),(370,246),(370,190),(402,190),(402,150),(422,150)],TEAL,2,arrow=True)
d.line([(580,185),(580,241)],PURPLE,2,arrow=True)
d.text(598,219,"Proposed actions",14,MUTED)
d.line([(736,276),(820,276)],BLUE,3,arrow=True)
d.text(778,257,"Accepted",12,BLUE,True,"middle")
d.line([(978,311),(978,354),(181,354),(181,281)],TEAL,2,arrow=True)
d.text(580,346,"Measured feedback closes the loop",14,TEAL,False,"middle")
d.label(820,115,"Operator STOP","Independent of model inference",ORANGE,w=316)
d.line([(978,185),(978,241)],ORANGE,2,arrow=True)
d.rect(24,395,712,124,fill="#f8fafc")
d.text(46,429,"Verified recording",20,bold=True)
d.text(46,460,"Raw video · measured joints · model predictions · sent commands",17,MUTED)
d.text(46,489,"Record identity + timestamps bind every stream to the trial.",16,MUTED)
d.line([(180,354),(180,395)],ORANGE,2,arrow=True)
d.line([(973,311),(1138,311),(1138,554),(756,554),(756,455),(736,455)],ORANGE,2,arrow=True)
d.label(820,409,"Replay + human review","Check outcome and failure evidence",BLUE,w=316)
d.line([(736,455),(820,455)],ORANGE,2,arrow=True)
d.line([(978,479),(978,579),(579,579)],PURPLE,2,dash=True,arrow=True)
d.text(24,585,"PLANNED",13,PURPLE,True)
d.text(117,585,"Correction labels → dataset aggregation → retraining (DAgger)",17,PURPLE)
d.save("data-link")

d=Diagram(1160,480,"Operator workflow from connection to recorded trial review")
d.text(24,34,"OPERATOR WORKFLOW",14,MUTED,True)
steps=[
 (24,90,"Connect","SSH + host identity","PC → GPU host → robot"),
 (422,90,"Inspect","CAN, cameras, feedback","Verified USB + active owner"),
 (820,90,"Choose","Teleoperation or policy","Task, route, checkpoint"),
 (820,285,"Validate & start","Fresh state + operator action","One trial, one recording"),
 (422,285,"Record & stop","Observe progress","STOP remains available"),
 (24,285,"Review & iterate","Replay + quality checks","Save a versioned result"),
]
for x,y,t,a,b in steps:
 d.rect(x,y,316,126,fill="#fbfcfe")
 d.text(x+22,y+35,t,23,bold=True)
 d.text(x+22,y+69,a,17,MUTED)
 d.text(x+22,y+98,b,15,MUTED)
d.line([(340,153),(422,153)],BLUE,2,arrow=True)
d.line([(738,153),(820,153)],BLUE,2,arrow=True)
d.line([(978,216),(978,285)],BLUE,2,arrow=True)
d.line([(820,348),(738,348)],BLUE,2,arrow=True)
d.line([(422,348),(340,348)],BLUE,2,arrow=True)
d.line([(181,285),(181,254),(580,254),(580,216)],PURPLE,2,dash=True,arrow=True)
d.text(380,244,"Inspect again after a change",14,PURPLE,False,"middle")
d.text(24,454,"A failed condition returns to inspection; a new action requires a fresh request.",16,MUTED)
d.save("workflow")
import runpy
runpy.run_path(str(ROOT/"tools/draw_asset_architecture.py"),run_name="__main__")
print(json.dumps({"diagrams":["system-architecture","data-link","workflow"],"formats":["svg","png"],"labels":"English"}))
