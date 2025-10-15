Heim-EMS (OCPP + Wettersteuerung) – Web-only Deploy (Render + Vercel)

Überblick

Backend: FastAPI + python-ocpp (OCPP 1.6J), Endpunkte:

WebSocket: wss://<render-app>/ocpp/{CP_ID}

REST: GET /api/points, POST /api/points/{cp_id}/mode/{mode}, POST /api/points/{cp_id}/limit?kw=…

Frontend: React (Vite) auf Vercel

Wetterdaten: Open-Meteo

Steuerung: Ladeleistung wird in Eco-Modus anhand Strahlungsprognose zwischen CLOUDY_KW und SUNNY_KW geregelt.

Was du brauchst

GitHub Account

Render Account (Backend)

Vercel Account (Frontend)

Keine lokale Installation nötig

Repository anlegen (nur im Browser)

Neues GitHub Repo erstellen (z. B. heim-ems).

Die folgende Struktur im Web-Editor anlegen:
/backend (Dateien siehe unten)
/frontend (Dateien siehe unten)
README-DEPLOY.md (diese Datei)

Änderungen committen.

Backend auf Render deployen

Render → New → Web Service → „Connect repository“ → wähle dein Repo.

Root Directory: backend

Environment → Add (Folgende Variablen setzen; Werte anpassen)
LAT = 48.87        # grobe Näherung Radldorf – bitte bei Bedarf anpassen
LON = 12.65        # grobe Näherung Radldorf – bitte bei Bedarf anpassen
SUNNY_KW = 11.0
CLOUDY_KW = 3.7
RAD_SUNNY = 650    # W/m² ab der es „sonnig“ ist
RAD_CLOUDY = 200   # W/m² bis zu der es „bewölkt“ ist
BASE_LIMIT_KW = 11.0
MIN_GRID_KW = 0.5

Build Command:
pip install --no-cache-dir -r requirements.txt

Start Command:
uvicorn main:app --host 0.0.0.0 --port $PORT

Create Web Service. Nach „Live“ die URL notieren, z. B. https://ems-backend.onrender.com

Hinweise:

Free-Tier kann „schlafen“ → OCPP-Verbindung wird getrennt. Für Dauerbetrieb Render „Starter“ (Always-on).

Frontend auf Vercel deployen

Vercel → Add New → Project → Import Git Repository.

Root Directory: frontend

Build Command: npm ci && npm run build

Output Directory: dist

Environment Variable:
VITE_API = https://ems-backend.onrender.com  # Render-URL vom Backend

Deploy. Frontend-URL notieren, z. B. https://ems-ui.vercel.app

Optional (Sicherheit/CORS)

Wenn die Vercel-URL feststeht, im Backend die CORS-Allowlist auf genau diese Domain einschränken (statt "*").

Wallbox per OCPP anbinden

In der Wallbox-Konfiguration:

OCPP Version: 1.6J

Central System URL: wss://<deine-Render-URL>/ocpp/{CP_ID}
Beispiel: wss://ems-backend.onrender.com/ocpp/homebox1

Heartbeat: 30–60 s

Nach BootNotification taucht die Box im Frontend auf.

Bedienung

UI zeigt Ladestatus und Ziel-kW.

Modi:

Eco: Ziel-kW wird alle 15 Min. anhand der Strahlungsprognose zwischen CLOUDY_KW und SUNNY_KW geregelt (linear skaliert zwischen RAD_CLOUDY und RAD_SUNNY).

Max: BASE_LIMIT_KW

Aus: 0 kW

Button „kW setzen“ überschreibt den momentanen Zielwert manuell.

Kosten

Backend: Render Free (schläft) = 0 €, oder Starter (always-on) ~ 5–7 €/Monat

Frontend: Vercel Free = 0 €

Wetter: Open-Meteo = 0 €

Troubleshooting

Box erscheint nicht: Prüfe OCPP-URL (wss://…), CP_ID, und ob Render-Dienst „Live“ ist.

CORS-Fehler im Browser: VITE_API korrekt gesetzt? Im Backend CORS-Allowlist prüfen.

Leistung setzt sich nicht: Unterstützt die Wallbox SetChargingProfile (OCPP 1.6J, Ampere-basiert)? Manche Geräte erwarten einen aktiven Ladevorgang (Stecker verbunden, Session gestartet).

Nächste Schritte

Auth (Token/JWT) für Admin-Endpoints.

Persistente DB (Render PostgreSQL) statt In-Memory.

Genauere PV-Logik (Dachparameter) oder Strompreisdaten integrieren.

Lizenz

Privatprojekt; passe nach Bedarf an.
