# GDrive VideoLoader

**GDrive VideoLoader** is a Python-based tool to download videos from Google Drive effortlessly, **including those marked as _view-only_** (no download option). It supports resumable downloads, progress bars, and various customization options for video fetching and downloading.

## Features

- Download videos even if marked as *view-only* (without a download button)
- Supports resumable downloads (continue from where it stopped)
- Displays a progress bar for ongoing downloads
- Allows custom chunk sizes for downloading
- Optionally specify a custom output file name
- Verbose mode for detailed logs during execution

## Installation

### Prerequisites

- Python 3.7+
- Pip (Python package manager)

### Dependencies

Install the required Python packages using the following command:

```bash
pip install -r requirements.txt
```

## Usage

### Basic Command

To download a video by its Google Drive ID:

```bash
python gdrive_videoloader.py <video_id>
```

### Accessing private files with cookies (login)

If the video is not public or “anyone with link”, supply your Google cookies so the script can act as your logged-in browser session. You have three options:

1) Use a cookies JSON file (e.g., exported by a Cookie Editor extension)

```bash
python gdrive_videoloader.py <video_id> --cookies-file cookies.json -v
```

2) Use a Netscape cookies.txt file (e.g., exported by a "cookies.txt" extension)

```bash
python gdrive_videoloader.py <video_id> --cookies-file cookies.txt -v
```

3) Pull cookies directly from your installed browser (requires browser-cookie3)

```bash
pip install browser-cookie3
python gdrive_videoloader.py <video_id> --browser-cookies chrome -v
```

Alternatively, you can paste a raw Cookie header string:

```bash
python gdrive_videoloader.py <video_id> --cookie "SID=...; HSID=...; SSID=..." -v
```

Notes:
- Ensure the account whose cookies you use has access to the file.
- On Windows, browser-cookie3 reads your profile’s encrypted cookie store; run as the same user.
- If Google still returns an error, refresh cookies (re-export) and make sure they include the main Google auth cookies (e.g., SID/HSID/SSID) and drive.google.com cookies.

### Options

| Parameter                | Description                                                       | Default Value         |
|--------------------------|-------------------------------------------------------------------|-----------------------|
| `<video_id>`             | The video ID from Google Drive (required).                       | N/A                   |
| `-o`, `--output`         | Custom output file name for the downloaded video.                | Video name in GDrive  |
| `-c`, `--chunk_size`     | Chunk size (in bytes) for downloading the video.                 | 1024 bytes            |
| `-v`, `--verbose`        | Enable verbose mode for detailed logs.                           | Disabled              |
| `--cookies-file`         | Path to cookies file (JSON or Netscape cookies.txt).             | None                 |
| `--browser-cookies`      | Load cookies from an installed browser (chrome, edge, firefox, brave, opera, vivaldi, any). | None |
| `--cookie`               | Raw Cookie header string (e.g., "SID=...; HSID=...").           | None                 |
| `--version`              | Display the script version.                                      | N/A                   |
| `-h`, `--help`           | Display the help message.                                        | N/A                   |

## TODO

### Features
- Add support for downloading subtitles.
- Add support for multiple downloads (list or file of video IDs).
- Allow selection of video quality.
- Implement temporary file naming during download.

### UX
- Safely handle interruptions (KeyboardInterrupt).
- Display custom error messages based on request responses.

### Performance
- Implement parallel downloads to speed up the process.

### Organization
- Modularize the project into separate files (`downloader.py`, `cli.py`, `utils.py`).
- Add logging support using the `logging` module.
- Validate output file names for compatibility with the operating system.

### Code Quality
- Create automated tests for core functions.
- Add detailed documentation using `pdoc` or `Sphinx`.

## Contributing
Contributions are always welcome! If you have suggestions for improving the script or adding new features, feel free to fork the repository and submit a pull request.

## License
This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.
