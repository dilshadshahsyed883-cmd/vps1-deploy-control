# Self Media Library - Standalone Windows Desktop

Independent Windows desktop distribution built from the existing Self Media Library source. The **portable folder includes all SML source modules, UI files, processing logic and local server**. It does not connect to VPS_1, Scene Builder, Drive, or a hosted SML service. It makes no changes to their databases or files.

## Running

Download the **SML-Desktop-Windows** artifact from the successful workflow run and unzip the artifact. Keep the SML_Desktop folder intact, then double-click **SML_Desktop.exe** inside it. No separate Python installation is necessary. Windows WebView2 is recommended for the app window; without it, the app opens the same local UI in a browser.

The default storage location is **D:\SML_Desktop_Data** if a D: drive is available, otherwise **Documents\SML_Desktop_Data**. User media, SQLite catalogs, AI models, proxy profiles and review decisions are stored there, not alongside the program. All data stays after updating the application. For a different path, close the app and run **CHANGE_DATA_FOLDER.bat**. Switching folders does not copy or delete the previous library.

FFmpeg/FFprobe are shipped alongside the EXE. Video processing and thumbnails need no separately installed FFmpeg. AI vision, speech and face recognition dependencies are bundled, but their large verified models are **downloaded on first explicit setup/use**, so those features require an internet connection on first activation. YouTube import requires internet access and rights to the media; upstream YouTube access limitations may prevent some imports.

## Features

- Upload and organize videos and images; accept processed clips without splitting.
- FFmpeg split/thumbnail processing, configurable settings, process queue and pair review (keep one/discard other); verified local cleanup.
- Library and preview subtabs, saved collections, keywords/description/date/orientation/status filters, bulk approve/reject/trash.
- Duplicate review, AI workspace and manual AI metadata corrections, search/ranking.
- Manual people collections and image face matching with approval, with local-only embedding and audit state.
- Authorized YouTube video import and optional SML-owned proxy profiles.
- Portability: runs on localhost, with a persistent database and user-settable storage root.

The original VPS editor integration endpoints may be present in the source for compatibility, but no Scene Builder deployment or cloud connection is configured. No VPS media or catalog is auto-imported.

## Build and verification

CI runs a clean Windows Python 3.11 environment, copies the upstream SML source without changing it, adds Windows compatibility adapters, verifies startup and processed clip intake, builds a PyInstaller onedir desktop app with pywebview, and uploads a zipped portable folder.

Scope: build remains isolated in desktop/sml and the Windows CI workflow. No production release job is triggered.

**Important:** A successful CI artifact validates packaging and smoke tests, not interactive owner UAT. Run additional manual tests on Windows for YouTube, AI model downloads, face recognition, playback, and large queues.
