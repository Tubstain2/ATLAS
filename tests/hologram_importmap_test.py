"""Test relative importmap paths — loads from ui/ dir just like atlas_ui.html."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from PyQt6.QtWidgets import QApplication
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings, QWebEngineScript
from PyQt6.QtCore import QUrl, QTimer

TEST_HTML = (Path(__file__).parent.parent / "ui" / "test_relative_importmap.html").resolve()

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

# Inject error catchers before page loads
catch_js = """
window.addEventListener('error', function(e){
  console.error('PAGEERROR: ' + e.message + ' at ' + e.filename + ':' + e.lineno);
});
window.addEventListener('unhandledrejection', function(e){
  console.error('UNHANDLED_REJECTION: ' + String(e.reason));
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

v.resize(800, 600)
v.load(QUrl.fromLocalFile(str(TEST_HTML)))
v.show()

def check():
    page.runJavaScript("window._three_loaded ? 'yes' : 'no'", lambda r: print(f"[PY] THREE loaded via relative importmap: {r}"))
    print("\nAll JS messages:")
    for m in js_log:
        print(f"  {m}")

QTimer.singleShot(5000, check)
QTimer.singleShot(6000, app.quit)
sys.exit(app.exec())
