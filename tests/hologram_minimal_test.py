"""Minimal Three.js test — rotating cyan cube, local vendor, file:// URLs."""
import sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from PyQt6.QtWidgets import QApplication
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
from PyQt6.QtCore import QUrl, QTimer

VENDOR = (Path(__file__).parent.parent / "ui" / "vendor" / "three").resolve()
THREE_URL    = f"file://{VENDOR}/build/three.module.js"
ADDONS_URL   = f"file://{VENDOR}/examples/jsm/"

HTML = f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<script type="importmap">
{{"imports":{{"three":"{THREE_URL}","three/addons/":"{ADDONS_URL}"}}}}
</script>
</head>
<body style="margin:0;background:#001122">
<canvas id="c" style="display:block"></canvas>
<script type="module">
import * as THREE from 'three';
console.log('THREE loaded: r' + THREE.REVISION);

const canvas = document.getElementById('c');
canvas.width  = window.innerWidth;
canvas.height = window.innerHeight;

const renderer = new THREE.WebGLRenderer({{canvas, antialias:true}});
renderer.setSize(window.innerWidth, window.innerHeight);

const scene  = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(75, window.innerWidth/window.innerHeight, 0.1, 1000);
camera.position.z = 3;

const mesh = new THREE.Mesh(
  new THREE.BoxGeometry(1,1,1),
  new THREE.MeshBasicMaterial({{color: 0x00ccff, wireframe: true}})
);
scene.add(mesh);
console.log('THREE_MINIMAL_OK: cube added, starting render loop');

let frames = 0;
(function loop() {{
  requestAnimationFrame(loop);
  mesh.rotation.x += 0.01;
  mesh.rotation.y += 0.01;
  renderer.render(scene, camera);
  frames++;
  if (frames === 30) console.log('THREE_RENDER_OK: 30 frames rendered');
}})();
</script></body></html>"""

# Write to temp file so QWebEngine loads it as a real file:// URL
tmp = Path(tempfile.mktemp(suffix=".html"))
tmp.write_text(HTML)

js_log = []

class LogPage(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, msg, line, src):
        print(f"[JS] {msg}")
        js_log.append(msg)

app = QApplication(sys.argv)
v = QWebEngineView()
page = LogPage(v)
v.setPage(page)
s = v.settings()
s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled,              True)
s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls,  True)
s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls,True)
v.resize(800, 600)
v.setWindowTitle("ATLAS Three.js Minimal Test")
v.load(QUrl.fromLocalFile(str(tmp)))
v.show()

def capture():
    px = v.grab()
    out = str(Path(__file__).parent.parent / "debug_hologram_minimal.png")
    px.save(out)
    ok = any("THREE_MINIMAL_OK" in m for m in js_log)
    render_ok = any("THREE_RENDER_OK" in m for m in js_log)
    print(f"\nResult: THREE loaded={'yes' if any('THREE loaded' in m for m in js_log) else 'NO'}")
    print(f"        Scene ready  ={'yes' if ok else 'NO'}")
    print(f"        Frames OK    ={'yes' if render_ok else 'NO'}")
    print(f"Screenshot: {out}")
    tmp.unlink(missing_ok=True)

QTimer.singleShot(5000, capture)
QTimer.singleShot(6000, app.quit)
sys.exit(app.exec())
