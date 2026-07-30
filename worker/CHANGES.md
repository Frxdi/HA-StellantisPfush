# Änderungen gegenüber dem Original-Repo

Basierend auf GitHub-Issue #2 und PR #3
(https://github.com/andreadegiovine/homeassistant-stellantis-vehicles-worker-v2/pull/3)

## main.py – Patch angewendet

1. **Login-Selektoren einzeln statt kombiniert geprüft**, jeweils mit
   `state="visible"`. Verhindert, dass ein versteckter Gigya-Template-Input
   zuerst gematcht wird und die Eingabe blockiert (Timeout).

2. **Consent-/Autorisierungs-Seite**: erweiterte Selektor-Liste
   (`#consentbutton`, `#cvs_from`, `#cvs_form`, generische Formular-Buttons),
   jeweils einzeln auf Sichtbarkeit geprüft. Falls kein sichtbarer Button
   gefunden wird: JS-Fallback über `form.requestSubmit()` / `form.submit()`.

3. **Code-Erfassung korrigiert**: Es wird nur noch der Code aus der
   *finalen* Nicht-HTTP-Redirect-URL (Ziel `oauth2redirect`) akzeptiert.
   Zwischen-Redirects über `https://idpcvs.<brand>.com/...` mit eigenem
   `code=`-Parameter werden ignoriert (führten sonst zu `invalid_grant`).

4. **Polling statt Einmal-Check** auf den erfassten Code nach Login/Consent.

5. **Debug-Artefakte bei Fehlern**: Screenshot, HTML und eine JSON-Datei mit
   URL (Query-String entfernt) und Body-Snippet werden nach `/tmp/oauth_debug`
   geschrieben – ohne Zugangsdaten oder Tokens zu loggen.

## Wichtiger Hinweis zu Dockerfile / render.yaml / requirements.txt

Diese drei Dateien konnte ich in dieser Session nicht 1:1 aus dem Original-Repo
laden (Tool-Einschränkung beim Rohdatei-Abruf). Sie wurden daher anhand des
üblichen Aufbaus für ein FastAPI + Playwright-Projekt **neu rekonstruiert**
und sollten funktionieren, sind aber **keine exakte Kopie** deiner
bestehenden Dateien.

**Empfehlung:** Falls dein aktuelles Deployment bereits läuft und nur der
OAuth-Flow fehlschlägt, ersetze in deinem bestehenden Projekt nur die Datei
`main.py` durch die gepatchte Version aus diesem ZIP und behalte dein
bisheriges `Dockerfile`, `render.yaml` und `requirements.txt` unverändert bei.
Das ist der risikoärmere Weg.

## Syntax-Check

`python3 -m py_compile main.py` lief fehlerfrei durch.
