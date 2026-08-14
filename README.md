# 🎬 AutoTune Video Encoder & Web UI

Ein intelligentes, hochgradig automatisiertes Tool zur Videokompression mit NVEncC (NVIDIA GPU) und FFmpeg (CPU Fallback). 

Das Tool analysiert Eingabevideos vorab auf Bitraten-Spitzen und Rauschmuster, misst die Bildqualität mittels VMAF ein (Zielbereich ~96.5–97.5), ermittelt vollautomatisch den optimalen Kompressionswert (QVBR / CRF) und bietet optional NVIDIA Tensor-Core KI-Enhancements. Neben einer mächtigen **CLI** verfügt das Tool nun über eine moderne **FastAPI-Weboberfläche** mit Live-WebSocket-Logging und Drag & Drop.

---

## ✨ Features & Highlights

*   🌐 **Moderne FastAPI Web-UI:** Ein aufgeräumtes, responsives Tailwind-CSS-Interface mit Drag & Drop, Live-Fortschrittsanzeige und Echtzeit-Log-Streaming direkt in den Browser.
*   🔌 **Live-WebSocket-Logging:** Sämtliche Terminal- und Pipeline-Ausgaben werden in Echtzeit in die Weboberfläche gestreamt.
*   🛡️ **Sicheres Datei-Staging & Schutz vor langen Pfaden:** Automatische Bereinigung und Kürzung von Dateinamen auf einen sicheren Stem (max. 100 Zeichen) mit dem Suffix `_uploaded`. Dies verhindert Windows-Pfadlimit-Fehler und garantiert eine restlose Bereinigung im Arbeitsverzeichnis.
*   🎯 **Automatisierte QVBR / CRF Ermittlung (4-2-1 Einmessung):** Findet auf Basis von VMAF-Analysen automatisch den perfekten Qualitätswert für HEVC & AV1.
*   🔄 **Dynamischer Nachlauf & Safety-Cap:** Bricht bei hohen VMAF-Werten nicht vorzeitig ab, sondern stuft dynamisch in 2er-Schritten weiter ab (inklusive harter Obergrenzen: QVBR Max 30 bei HEVC, 34 bei AV1).
*   🔍 **Pre-Flight Rausch-Check (Noise Analysis):** Analysiert Rausch-Samples mit dem `--vpp-knn`-Filter. Erkennt starkes Rauschen (Delta $\ge 25\%$) und passt den VMAF-Zielwert automatisch an (95.5).
*   🤖 **NVIDIA Tensor-KI Video Optimization:** Integrierte Modi für SDR-zu-HDR10-Konvertierung (`--vpp-ngx-truehdr`) und AI Upscaling (`--vpp-ngx-vrs`).
*   ⚡ **Automatischer Engine-Fallback:** Erkennt kompatible NVIDIA-GPUs über `nvidia-smi` und schaltet bei Bedarf nahtlos auf den CPU-Fallback mit FFmpeg (`libsvtav1` / `libx265`) um.
*   📂 **Isoliertes Work-Directory & Cleanup:** Temporäre Clips, Rausch-Samples und Uploads werden im `_Work/`-Ordner verarbeitet und nach Abschluss (oder bei Fehlern) restlos bereinigt. Finale Videos und `.summary.txt`-Berichte landen im `Results/`-Ordner.

---

## 🛠️ Voraussetzungen & Ordnerstruktur

Das Projekt erwartet seine Abhängigkeiten in einer strukturierten Umgebung:

```text
📁 AV-Encode/
├── 📁 video_tool/
│   ├── 📄 server.py             <-- FastAPI Backend & WebSocket-Manager
│   ├── 📄 main.py               <-- Encoding-Pipeline & Wrapper
│   ├── 📄 config.py             <-- Workflow-Defaults & Konfiguration
│   └── 📄 index.html            <-- Frontend (Tailwind CSS & WebSockets)
├── 📁 FFMPeg/
│   ├── 📄 ffmpeg.exe            <-- Benötigt für Sample-Schnitt & CPU-Fallback
│   └── 📄 ffprobe.exe           <-- Benötigt für Bitraten- & Track-Analyse
├── 📁 NVEncC/
│   └── 📄 nvencc64.exe          <-- Benötigt für NVIDIA GPU-Encoding & KI-Filter
├── 📁 _Work/                    <-- Temporäres Arbeitsverzeichnis (wird bereinigt)
└── 📁 Results/                  <-- Zielordner für finale Videos & Summaries
```

---

## 🚀 Nutzung

**Option A:** Über die Weboberfläche (Empfohlen)

1. Starte den FastAPI-Server über das Terminal (im Projektverzeichnis):
```
Bash
uvicorn video_tool.server:app --reload --port 8265
```
2. Öffne deinen Browser und rufe die angezeigte Adresse auf (z. B. http://127.0.0.1:8265).

3. Ziehe dein Video per Drag & Drop in die Upload-Zone oder wähle es aus.

4. Wähle den gewünschten Codec (HEVC oder AV1) sowie den HDR-Modus.

5. Klicke auf Encoding starten. Der Live-Fortschritt und alle Log-Meldungen erscheinen direkt im Live-Reporting-Fenster.

**Option B:** Über die Kommandozeile (CLI)
Du kannst das Skript weiterhin direkt über Python oder die PowerShell ausführen:
```
PowerShell
python video_tool/main.py --input "C:\Videos\MeinFilm.mkv" --codec hevc
```

---

## 🛡️ Technische Details zu Audio & Untertiteln

Audiokopie & Kapitel: Audio-Spuren und Kapitel werden 1:1 übernommen (--audio-copy, --chapter-copy).

Forced Subtitles: Das System setzt voraus, dass mindestens eine Untertitelspur mit Forced-Flag vorhanden ist, und brennt erzwungene Untertitel automatisch in das Video ein (--vpp-subburn track=1,forced_subs_only=on).
