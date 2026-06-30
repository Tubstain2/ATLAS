"""Full hologram test — loads actual atlas_ui.html and triggers showHologram()."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from PyQt6.QtWidgets import QApplication
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings, QWebEngineScript
from PyQt6.QtCore import QUrl, QTimer

UI_HTML = (Path(__file__).parent.parent / "ui" / "atlas_ui.html").resolve()

js_log = []

class LogPage(QWebEnginePage):
    def javaScriptConsoleMessage(self, level, msg, line, src):
        lvl = ["DBG","INF","WRN","ERR"][min(level.value, 3)]
        print(f"[JS:{lvl}] {src.split('/')[-1]}:{line} — {msg}")
        js_log.append(msg)

app = QApplication(sys.argv)
v = QWebEngineView()
page = LogPage(v)
v.setPage(page)

catch_js = """
window.addEventListener('error', function(e){
  console.error('PAGEERROR: ' + e.message + ' | ' + e.filename + ':' + e.lineno);
});
window.addEventListener('unhandledrejection', function(e){
  console.error('REJECTION: ' + String(e.reason));
});
"""
script = QWebEngineScript()
script.setSourceCode(catch_js)
script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld)
page.scripts().insert(script)

s = v.settings()
s.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled,               True)
s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls,   True)
s.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessRemoteUrls, True)
v.resize(1280, 800)
v.setWindowTitle("ATLAS Hologram Full Test")
v.load(QUrl.fromLocalFile(str(UI_HTML)))
v.show()

poll_count = [0]

def try_dynamic_import():
    for mod, label in [
        ("three", "THREE"),
        ("three/addons/controls/OrbitControls.js", "OrbitControls"),
        ("three/addons/postprocessing/EffectComposer.js", "EffectComposer"),
        ("three/addons/postprocessing/RenderPass.js", "RenderPass"),
        ("three/addons/postprocessing/UnrealBloomPass.js", "UnrealBloomPass"),
        ("three/addons/postprocessing/OutputPass.js", "OutputPass"),
    ]:
        page.runJavaScript(
            f"import('{mod}').then(m=>console.log('DYN_OK: {label}')).catch(e=>console.error('DYN_FAIL: {label} — '+e))"
        )

def poll_ready():
    poll_count[0] += 1
    if poll_count[0] == 3:  # at 1.5s, try a dynamic import to test importmap
        try_dynamic_import()
    page.runJavaScript("typeof window.showHologram", lambda t: on_type(t))

def on_type(t):
    if t == "function":
        print(f"[PY] showHologram ready after {poll_count[0] * 0.5:.1f}s")
        page.runJavaScript("window.showHologram('orb', {})")
        QTimer.singleShot(3000, capture)
        QTimer.singleShot(4000, app.quit)
    elif poll_count[0] < 30:  # up to 15s
        QTimer.singleShot(500, poll_ready)
    else:
        print("[PY] TIMEOUT: showHologram never became available")
        capture()
        QTimer.singleShot(1000, app.quit)

def capture():
    px = v.grab()
    out = str(Path(__file__).parent.parent / "debug_hologram_final_1.png")
    px.save(out)
    print(f"\n--- Full Hologram Result ---")
    print(f"Screenshot: {out}")
    print("All JS log lines:")
    for m in js_log:
        print(f"  {m}")

QTimer.singleShot(1000, poll_ready)
sys.exit(app.exec())
