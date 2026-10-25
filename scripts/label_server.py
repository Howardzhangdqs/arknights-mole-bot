#!/usr/bin/env python3
"""模板标注工具：浏览器里框选标注，完成后自动裁剪模板 + 生成预览。

用法: python3 scripts/label_server.py [端口]
打开: http://<本机IP>:8765  （本机 Tailscale IP 可从局域网/手机访问）
"""
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHOT = os.path.join(BASE, "assets", "screenshots")
TPL = os.path.join(BASE, "templates")
LABELS = os.path.join(TPL, "labels.json")

IMAGES = [
    "shot_01.jpg", "shot_02.jpg", "shot_05.jpg", "shot_06.jpg",
    "shot_07.jpg", "shot_08.jpg",
    "frames_live/f10.png", "frames_live/f17.png", "frames_live/f26.png",
    "live_01.png", "live_02_operator.png",
]

PRESETS = ["mole_alpha", "mole_small", "watermelon", "cabbage", "drone",
           "boss_core", "warning", "enemy_purple", "enemy_ice", "other"]

HTML = """<!doctype html><html><head><meta charset="utf-8">
<title>打地鼠模板标注</title><style>
body{font-family:system-ui;margin:0;display:flex;height:100vh;background:#1e1e1e;color:#eee}
#side{width:230px;padding:10px;overflow:auto;border-right:1px solid #444}
#side button{display:block;width:100%;margin:4px 0;padding:6px;background:#2d2d2d;color:#eee;border:1px solid #555;border-radius:4px;cursor:pointer;text-align:left}
#side button.active{background:#0a6;border-color:#0a6}
#side button:hover{border-color:#888}
#main{flex:1;display:flex;flex-direction:column}
#bar{padding:8px;background:#252526;display:flex;gap:6px;flex-wrap:wrap;align-items:center}
#bar input{background:#3c3c3c;color:#eee;border:1px solid #555;border-radius:4px;padding:5px;width:140px}
#bar .p{padding:4px 8px;background:#094771;border-radius:4px;cursor:pointer;border:1px solid #0a6db3}
#bar .act{padding:6px 14px;border-radius:4px;cursor:pointer;border:0;font-weight:bold}
#wrap{flex:1;overflow:auto;position:relative}
canvas{position:absolute;top:0;left:0;cursor:crosshair}
#boxes{padding:8px;background:#252526;max-height:180px;overflow:auto;font-size:13px}
#boxes div{margin:2px 0}.del{color:#f66;cursor:pointer;margin-left:8px}
#tip{color:#9fd}
</style></head><body>
<div id="side"><b>图片列表</b><div id="imgs"></div></div>
<div id="main">
  <div id="bar">
    <b>模板名:</b><input id="name" list="presets" placeholder="输入或点选">
    <datalist id="presets"></datalist>
    <span id="presetbtns"></span>
    <button class="act" style="background:#0a6" onclick="save()">保存</button>
    <button class="act" style="background:#c33" onclick="finish()">✅ 完成并裁剪</button>
    <span id="tip">拖拽画框 → 自动用当前模板名；换名再画下一框</span>
  </div>
  <div id="wrap"><canvas id="cv"></canvas></div>
  <div id="boxes"></div>
</div>
<script>
let cur=null, scale=1, boxes={}, dragging=null;
const cv=document.getElementById('cv'), ctx=cv.getContext('2d');
async function init(){
  const imgs=await (await fetch('/api/images')).json();
  const lab=await (await fetch('/api/labels')).json();
  boxes=lab;
  const side=document.getElementById('imgs');
  imgs.forEach(f=>{const b=document.createElement('button');b.textContent=f.split('/').pop();
    b.onclick=()=>load(f,b);b.dataset.f=f;side.appendChild(b);});
  const dl=document.getElementById('presets'), pb=document.getElementById('presetbtns');
  (await (await fetch('/api/presets')).json()).forEach(p=>{
    const o=document.createElement('option');o.value=p;dl.appendChild(o);
    const s=document.createElement('span');s.className='p';s.textContent=p;
    s.onclick=()=>document.getElementById('name').value=p;pb.appendChild(s);});
  side.firstChild.click();
}
function load(f,btn){
  cur=f;document.querySelectorAll('#side button').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  const img=new Image();img.onload=()=>{
    const wrap=document.getElementById('wrap');
    scale=Math.min(1,(wrap.clientWidth-10)/img.width);
    cv.width=img.width*scale;cv.height=img.height*scale;
    cv.style.backgroundImage=`url(/image/${encodeURIComponent(f)})`;
    cv.style.backgroundSize=`${cv.width}px ${cv.height}px`;
    draw();listBoxes();
  };img.src='/image/'+encodeURIComponent(f);
}
function nat(e){const r=cv.getBoundingClientRect();return[(e.clientX-r.left)/scale,(e.clientY-r.top)/scale];}
cv.onmousedown=e=>{dragging=[nat(e),nat(e)];};
cv.onmousemove=e=>{if(!dragging)return;dragging[1]=nat(e);draw();
  const[a,b]=dragging;ctx.strokeStyle='#0f0';ctx.lineWidth=2/scale;
  ctx.strokeRect(a[0],a[1],b[0]-a[0],b[1]-a[1]);};
cv.onmouseup=e=>{
  if(!dragging)return;const[a,b]=dragging;dragging=null;
  const x=Math.round(Math.min(a[0],b[0])),y=Math.round(Math.min(a[1],b[1]));
  const w=Math.round(Math.abs(b[0]-a[0])),h=Math.round(Math.abs(b[1]-a[1]));
  if(w<8||h<8){draw();return;}
  const name=document.getElementById('name').value.trim()||'unnamed';
  (boxes[cur]=boxes[cur]||[]).push({name,x,y,w,h});draw();listBoxes();
};
function draw(){
  ctx.clearRect(0,0,cv.width,cv.height);ctx.save();ctx.scale(scale,scale);
  (boxes[cur]||[]).forEach((b,i)=>{
    ctx.strokeStyle=['#0ff','#ff0','#f0f','#0f0'][i%4];ctx.lineWidth=3/scale;
    ctx.strokeRect(b.x,b.y,b.w,b.h);
    ctx.fillStyle=ctx.strokeStyle;ctx.font=`${22/scale}px sans-serif`;
    ctx.fillText(`${b.name} (${b.w}x${b.h})`,b.x,b.y-8/scale);});
  ctx.restore();
}
function listBoxes(){
  const el=document.getElementById('boxes');
  el.innerHTML=(boxes[cur]||[]).map((b,i)=>
    `<div>${i+1}. <b style="color:#0ff">${b.name}</b> ${b.x},${b.y},${b.w},${b.h}
     <span class="del" onclick="boxes[cur].splice(${i},1);draw();listBoxes()">[删]</span></div>`).join('')
    ||'<i>本图还没有框</i>';
}
async function save(){
  const r=await fetch('/api/labels',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(boxes)});
  document.getElementById('tip').textContent=r.ok?'已保存 ✓':'保存失败!';}
async function finish(){
  await save();
  const r=await fetch('/api/finish',{method:'POST'});
  const j=await r.json();
  document.getElementById('tip').textContent=`裁剪完成: ${j.count} 张模板 → templates/  预览: templates/preview/_all_templates.png`;
  alert('完成！共裁剪 '+j.count+' 张模板\\n预览图: templates/preview/_all_templates.png\\n（回去告诉 AI 标注完成了）');
}
init();
</script></body></html>"""


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        p = unquote(self.path)
        if p == "/":
            return self._send(200, HTML.encode(), "text/html; charset=utf-8")
        if p == "/api/images":
            return self._send(200, IMAGES)
        if p == "/api/presets":
            return self._send(200, PRESETS)
        if p == "/api/labels":
            if os.path.exists(LABELS):
                return self._send(200, open(LABELS, encoding="utf-8").read())
            return self._send(200, {})
        if p.startswith("/image/"):
            fp = os.path.join(SHOT, p[len("/image/"):])
            if os.path.isfile(fp):
                ext = "jpeg" if fp.endswith((".jpg", ".jpeg")) else "png"
                return self._send(200, open(fp, "rb").read(), f"image/{ext}")
        self._send(404, {"err": "not found"})

    def do_POST(self):
        p = unquote(self.path)
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        if p == "/api/labels":
            with open(LABELS, "w", encoding="utf-8") as f:
                json.dump(body, f, ensure_ascii=False, indent=1)
            return self._send(200, {"ok": True})
        if p == "/api/finish":
            count = crop_all()
            return self._send(200, {"ok": True, "count": count})
        self._send(404, {"err": "not found"})

    def log_message(self, *_):
        pass


def crop_all():
    import cv2
    labels = json.load(open(LABELS, encoding="utf-8"))
    os.makedirs(os.path.join(TPL, "preview"), exist_ok=True)
    count, files = 0, []
    for img_rel, lst in labels.items():
        img = cv2.imread(os.path.join(SHOT, img_rel))
        if img is None:
            continue
        for b in lst:
            name = re.sub(r"\W+", "_", b["name"]).strip("_") or "unnamed"
            out = os.path.join(TPL, f"{name}.png")
            k = 2
            while os.path.exists(out) and k < 99:
                out = os.path.join(TPL, f"{name}_{k}.png"); k += 1
            x, y = max(0, b["x"]), max(0, b["y"])
            x2 = min(img.shape[1], x + b["w"]); y2 = min(img.shape[0], y + b["h"])
            if x2 - x < 8 or y2 - y < 8:
                continue
            cv2.imwrite(out, img[y:y2, x:x2])
            files.append(os.path.basename(out)); count += 1
    # 拼贴预览
    import numpy as np
    cell, cols = 200, 5
    rows = (len(files) + cols - 1) // cols or 1
    sheet = np.full((rows * 230, cols * 220, 3), 40, np.uint8)
    for i, fn in enumerate(sorted(files)):
        t = cv2.imread(os.path.join(TPL, fn))
        h, w = t.shape[:2]
        s = min(cell / w, cell / h)
        t = cv2.resize(t, (int(w * s), int(h * s)))
        r, c = divmod(i, cols)
        y0, x0 = r * 230 + (cell - t.shape[0]) // 2, c * 220 + (cell - t.shape[1]) // 2
        sheet[y0:y0 + t.shape[0], x0:x0 + t.shape[1]] = t
        cv2.putText(sheet, fn[:-4], (c * 220 + 10, r * 230 + 222), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)
    cv2.imwrite(os.path.join(TPL, "preview", "_all_templates.png"), sheet)
    return count


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"标注工具: http://0.0.0.0:{port}  (labels → {LABELS})")
    ThreadingHTTPServer(("0.0.0.0", port), H).serve_forever()
