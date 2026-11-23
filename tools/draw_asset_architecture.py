"""Replace the robot schematic with the verified whole-machine rendering."""
from pathlib import Path
import base64
import json
from html import escape
from PIL import Image, ImageDraw, ImageFont

root=Path(__file__).resolve().parents[1]
model=root/"site/assets/robot"
out=root/"site/assets/figures"
width,height=1160,680
svg=[f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="System architecture and hardware roles">',
'<rect width="1160" height="680" fill="white"/>','<style>text{font-family:Segoe UI,Arial,sans-serif}</style>']
image=Image.new("RGB",(width*2,height*2),"white")
draw=ImageDraw.Draw(image)
fontdir = Path("C:/Windows/Fonts")
normal_font, bold_font = fontdir / "segoeui.ttf", fontdir / "seguisb.ttf"
if not normal_font.is_file():
    fonts = Path("/usr/share/fonts/truetype/liberation2")
    normal_font, bold_font = fonts / "LiberationSans-Regular.ttf", fonts / "LiberationSans-Bold.ttf"
    if not normal_font.is_file():
        fonts = Path("/usr/share/fonts/truetype/dejavu")
        normal_font, bold_font = fonts / "DejaVuSans.ttf", fonts / "DejaVuSans-Bold.ttf"
def box(x,y,w,h):
    svg.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="#fbfcfe" stroke="#dce2e9" rx="7"/>')
    draw.rounded_rectangle((x*2,y*2,(x+w)*2,(y+h)*2),14,fill="#fbfcfe",outline="#dce2e9",width=2)
def text(x,y,s,size=16,color="#172331",bold=False):
    svg.append(f'<text x="{x}" y="{y}" font-size="{size}" fill="{color}" font-weight="{600 if bold else 400}">{escape(s)}</text>')
    font=ImageFont.truetype(str(bold_font if bold else normal_font),size*2)
    draw.text((x*2,y*2),s,font=font,fill=color,anchor="ls")
def arrow(points,color="#2563eb",dashed=False):
    value=" ".join(f"{x},{y}" for x,y in points)
    svg.append(f'<polyline points="{value}" fill="none" stroke="{color}" stroke-width="2"'+(' stroke-dasharray="5 4"' if dashed else '')+'/>')
    draw.line([(x*2,y*2) for x,y in points],fill=color,width=4)
    import math
    (a,b),(x,y)=points[-2:];ang=math.atan2(y-b,x-a)
    tri=[(x,y),(x-9*math.cos(ang-.4),y-9*math.sin(ang-.4)),(x-9*math.cos(ang+.4),y-9*math.sin(ang+.4))]
    svg.append('<polygon points="'+" ".join(f"{a},{b}" for a,b in tri)+f'" fill="{color}"/>')
    draw.polygon([(a*2,b*2) for a,b in tri],fill=color)
def icon_line(points):
    color="#627183"
    coords=" ".join(f"{x},{y}" for x,y in points)
    svg.append(f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/>')
    draw.line([(round(x*2),round(y*2)) for x,y in points],fill=color,width=3,joint="curve")
    for x,y in (points[0],points[-1]):
        draw.ellipse((x*2-1.5,y*2-1.5,x*2+1.5,y*2+1.5),fill=color)

def icon_rect(x,y,w,h,radius=2):
    svg.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="none" stroke="#627183" stroke-width="1.7"/>')
    draw.rounded_rectangle((x*2,y*2,(x+w)*2,(y+h)*2),radius*2,outline="#627183",width=3)

def icon_circle(x,y,radius):
    svg.append(f'<circle cx="{x}" cy="{y}" r="{radius}" fill="none" stroke="#627183" stroke-width="1.7"/>')
    draw.ellipse(((x-radius)*2,(y-radius)*2,(x+radius)*2,(y+radius)*2),outline="#627183",width=3)

def device_icon(kind,x,y):
    svg.append(f'<g aria-label="{kind} icon">')
    if kind=="Operator PC":
        icon_rect(x+4,y+3,34,23,2)
        icon_line([(x+1,y+31),(x+41,y+31),(x+37,y+26),(x+5,y+26),(x+1,y+31)])
        icon_line([(x+17,y+28.5),(x+25,y+28.5)])
    elif kind=="GPU host":
        icon_rect(x+9,y,26,38,3)
        icon_circle(x+22,y+11,6)
        icon_line([(x+16,y+26),(x+28,y+26)])
        icon_line([(x+16,y+31),(x+28,y+31)])
        icon_circle(x+22,y+11,1.4)
    elif kind=="Robot computer":
        icon_rect(x+2,y+10,40,23,4)
        icon_line([(x+7,y+10),(x+12,y+3),(x+34,y+3),(x+39,y+10)])
        icon_rect(x+9,y+19,8,5,1)
        icon_rect(x+22,y+19,8,5,1)
        icon_circle(x+36,y+22,1)
    elif kind=="Verified USB":
        icon_rect(x+13,y+12,18,25,4)
        icon_line([(x+16,y+12),(x+16,y+1),(x+28,y+1),(x+28,y+12)])
        icon_line([(x+20,y+5),(x+20,y+8)])
        icon_line([(x+24,y+5),(x+24,y+8)])
        icon_circle(x+22,y+28,2)
    else:
        raise ValueError(kind)
    svg.append("</g>")

text(24,34,"SYSTEM ARCHITECTURE",14,"#627183",True)
box(24,87,282,170)
text(45,122,"Operator PC",25,bold=True)
device_icon("Operator PC",248,102)
text(45,151,"Windows + OpenSSH",16,"#627183")
text(45,187,"Browser / thin client",18,bold=True)
text(45,217,"Inspect · choose · start · stop",15,"#627183")
box(412,87,282,170)
text(433,122,"GPU host",25,bold=True)
device_icon("GPU host",637,100)
text(433,151,"RTX 4090 + Ubuntu",16,"#627183")
text(433,187,"Web, API and inference",18,bold=True)
text(433,217,"Releases, weights and jobs",15,"#627183")
arrow([(306,171),(412,171)])
text(339,155,"SSH",15,"#2563eb",True)
text(320,196,"LAN / VPN",13,"#627183")
box(412,356,282,174)
text(433,391,"Robot computer",23,bold=True)
device_icon("Robot computer",637,371)
text(433,419,"AgileX NUC",16,"#627183")
text(433,454,"Shared device agent",18,bold=True)
text(433,482,"Local control + camera / CAN",14,"#627183")
arrow([(552,257),(552,356)],"#087e75")
text(570,299,"Ethernet",15,"#087e75",True)
text(570,321,"Private link",13,"#627183")
box(24,356,282,174)
text(45,391,"Verified USB",23,bold=True)
device_icon("Verified USB",248,368)
text(45,419,"Trial-bound recording",16,"#627183")
text(45,454,"Video + measured joints",17,bold=True)
text(45,482,"Predictions + sent commands",14,"#627183")
arrow([(412,444),(306,444)],"#d97706")
text(333,428,"Save",14,"#d97706",True)
box(769,87,367,508)
text(790,122,"COBOT MAGIC",25,bold=True)
text(790,151,"Four-arm mobile platform",15,"#627183")
# Pages places the pinned source image here without bundling it.
box(790,183,325,342)
svg.append('<a href="https://raw.githubusercontent.com/agilexrobotics/mobile_aloha_sim/f799ae0192c0c2bbd502ec4d9bbec8c2633acd1a/img/img.png">')
text(820,346,"View the upstream assembly",16,"#1c5aa6",True)
svg.append("</a>")
text(820,370,"AgileX Robotics · source preview",13,"#627183")
text(790,555,"Whole-machine digital asset",16,bold=True)
text(790,579,"AgileX mobile_aloha_sim · f799ae0",12,"#627183")
arrow([(694,444),(769,444)],"#087e75")
text(706,423,"CAN",13,"#087e75",True)
text(701,471,"Cameras",12,"#627183")
text(24,610,"Software roles and physical data links",15,"#627183")
text(24,640,"Reference diagram; not a calibrated geometric model.",14,"#627183")
svg.append("</svg>")
(out/"system-architecture.svg").write_text("\n".join(svg),encoding="utf8")
image.save(out/"system-architecture.png")
print("Public architecture figure built")
