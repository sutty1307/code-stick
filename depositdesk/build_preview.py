"""Build the offline demo from the same HTML, CSS and JavaScript as the application."""
from pathlib import Path
import base64
root=Path(__file__).resolve().parent
html=(root/'static/index.html').read_text()
html=html.replace('<title>DepositDesk · Payment register</title>','<title>DepositDesk · Interactive demo</title>')
html=html.replace('<link rel="stylesheet" href="/styles.css">','<style>'+(root/'static/styles.css').read_text()+'</style>')
icon=base64.b64encode((root/'static/favicon.svg').read_bytes()).decode()
html=html.replace('href="/favicon.svg"','href="data:image/svg+xml;base64,'+icon+'"')
html=html.replace('<script src="/app.js"></script>','<script>'+(root/'preview-adapter.js').read_text()+'</script><script>'+(root/'static/app.js').read_text()+'</script>')
(root/'DepositDesk-Upgraded-Preview.html').write_text(html)
print('Created DepositDesk-Upgraded-Preview.html')
