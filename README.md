🎬 AutoTune Video Encoder (PowerShell)

Ein intelligentes, hochgradig automatisiertes CLI-Tool zur Videokompression mit NVEncC (NVIDIA GPU) und FFmpeg (CPU Fallback).

Das Skript analysiert das Eingabevideo vorab auf Bitraten-Spitzen sowie Rauschmuster, misst die Bildqualität mittels VMAF ein, um vollautomatisch den optimalen Kompressionswert (QVBR / CRF) zu ermitteln, und bietet optionale NVIDIA Tensor-Core KI-Enhancements.



✨ Features & Highlights

    🎯 Automatisierte QVBR / CRF Ermittlung (4-2-1 Einmessung): Findet auf Basis von VMAF-Analysen (Zielbereich ~96.5–97.5) automatisch den perfekten Qualitätswert für HEVC & AV1.
    🔄 Dynamischer Nachlauf & Safety-Cap: Bricht bei hohen VMAF-Werten nicht vorzeitig ab, sondern stuft dynamisch in 2er-Schritten weiter ab, inklusive harter Obergrenzen (QVBR Max: 30 bei HEVC, 34 bei AV1), um Artefakte im Gesamtlauf zu verhindern.
    🔍 Pre-Flight Rausch-Check (Noise Analysis): Analysiert ein Rausch-Sample des Videos mit dem --vpp-knn Filter. Erkennt starkes Rauschen (Delta $\ge 25\%$) und senkt den VMAF-Zielwert automatisch auf 95.5 ab.
    🤖 NVIDIA Tensor-KI Video Optimization (NVEncC): Bietet Modi für SDR-zu-HDR10-Konvertierung (--vpp-ngx-truehdr) und DVD2HD AI Upscaling (--vpp-ngx-vrs).
    ⚡ Automatischer Engine-Fallback: Erkennt kompatible NVIDIA-GPUs über nvidia-smi und schaltet bei Bedarf nahtlos auf den CPU-Fallback mit FFmpeg (libsvtav1 / libx265) um.
    📂 Isoliertes Work-Directory & Cleanup: Temporäre Clips, Rausch-Samples und Logs werden in einem separaten _Work-Verzeichnis verarbeitet und nach erfolgreichem Encode automatisch bereinigt. Die finale Datei sowie eine detaillierte .summary.txt landen im Results/-Ordner.



🛠️ Voraussetzungen & OrdnerstrukturDas Skript erwartet seine Abhängigkeiten in festen Unterordnern relativ zum Skript-Verzeichnis (ScriptRoot):

    📁 MeinEncodingOrdner/
    ├── 📄 AutoTune_Encode.ps1       <-- Hauptskript
    ├── 📁 FFMPeg/
    │   ├── 📄 ffmpeg.exe            <-- Benötigt für Sample-Schnitt & CPU-Fallback
    │   └── 📄 ffprobe.exe           <-- Benötigt für Bitraten- & Track-Analyse
    ├── 📁 NVEncC/
    │   └── 📄 nvencc64.exe          <-- Benötigt für NVIDIA GPU-Encoding & KI-Filter
    └── 📁 Results/                  <-- Wird automatisch erstellt (Finale Videos & Summaries)



🚀 Nutzung

    1. Skript ausführen

    Öffne die PowerShell und starte das Skript. Du kannst den Videopfad direkt als Parameter übergeben oder das Skript ohne Parameter starten, um interaktiv nach dem Pfad gefragt zu werden:
    PowerShell.\AutoTune_Encode.ps1 -InputPath "C:\Videos\MeinFilm.mkv"

    2. Interaktive MenüCodec-Wahl:

    Wähle zwischen HEVC (Option 1) und AV1 (Option 2).
    KI-Optimierung (nur bei NVIDIA GPU):
        - Keine KI-Filter (Standard)
        - SDR -> HDR10 (TrueHDR)
        - DVD2HD AI Upscaling (NGX VRS 1080p)
        - DVD2HD + TrueHDR

    3. Ergebnisse

    Nach erfolgreichem Durchlauf findest du im Results/-Ordner:
        - Das komprimierte finale Video (namensbasiert mit Codec, QVBR-Wert und gewähltem Modus).
        - Die zugehörige .summary.txt mit Kennzahlen wie Dateigrößen-Ersparnis, gemessenem VMAF-Wert, Encoding-Speed und Gesamtlaufzeit.

🛡️ Technische Details zu Audio & Untertiteln

    Audiokopie & Kapitel:
        - Audio-Spuren und Kapitel werden 1:1 übernommen (--audio-copy, --chapter-copy).
        - Forced Subtitles: Das Skript setzt voraus, dass mindestens eine Untertitelspur vorhanden ist, und brennt erzwungene Untertitel automatisch in das Video ein (--vpp-subburn track=1,forced_subs_only=on).
