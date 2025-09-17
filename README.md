# qBittorrent Remote (Accessible)

qBittorrent Remote (Accessible) is a wxPython desktop client that provides a screen-reader-friendly way to manage a qBittorrent instance over its Web API. The app focuses on accessible controls, clear announcements, and keyboard shortcuts so users relying on assistive technology can comfortably monitor and control torrents from Windows.

Key features:

- Connect to a remote qBittorrent server with saved credentials.
- View torrent status, speeds, peers, and other details in a sortable list optimized for screen readers.
- Start, pause, resume, recheck, and delete torrents (with optional data removal).
- Add new torrents by magnet link or URL.
- Inspect torrent properties, files, and tracker information.
- Minimize to the system tray for quick background use, with restore, options, and exit actions available from the tray icon.
- Optional file-association helpers to register `.torrent` files and magnet URIs with the client.

## Prerequisites

The application targets Windows and requires:

- Python 3.10 or newer.
- wxPython 4.2 or newer.
- The `requests` library.

These dependencies are declared implicitly; install them via `pip` as shown below.

## Setup

1. Clone the repository:

   ```bash
   git clone https://github.com/your-user/your-repo.git
   cd your-repo
   ```

2. Create and activate a virtual environment (recommended):

   ```bash
   python -m venv .venv
   .venv\Scripts\activate
   ```

3. Install dependencies:

   ```bash
   pip install wxPython requests
   ```

   > wxPython wheels are available for Windows through `pip`. If installation fails, consult the [wxPython downloads page](https://wxpython.org/pages/downloads/) for platform-specific instructions.

## Running the app

Launch the client from the repository root:

```bash
python main.py
```

On first launch you will be prompted to provide the qBittorrent Web UI address and credentials. The application remembers these settings for subsequent sessions in `qbittorrent-wx-client.json`.

### Command-line options

- `python main.py <magnet-or-url> ...` — queue one or more magnet links/URLs to add immediately after connecting.
- `python main.py --register-associations` — register the client as the handler for `.torrent` files and magnet links (Windows or Linux).
- `python main.py --unregister-associations` — remove previously registered associations.

## Building a standalone executable (optional)

If you need a distributable executable, tools such as [PyInstaller](https://pyinstaller.org/) can bundle the script. A minimal command looks like:

```bash
pyinstaller --windowed --name qbittorrent-remote main.py
```
