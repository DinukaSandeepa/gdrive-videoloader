from urllib.parse import unquote, parse_qs, urlparse
import requests
import argparse
import sys
from tqdm import tqdm
import os
from typing import Optional, Tuple
from http.cookiejar import MozillaCookieJar
import json

def get_video_url(page_content: str, verbose: bool) -> Tuple[Optional[str], Optional[str]]:
    """Extracts the video playback URL and title from get_video_info response.

    Tries in order:
    1) Parse player_response JSON and read streamingData URLs
    2) Fallback to scanning for 'videoplayback' in the raw content
    3) Title from player_response.videoDetails.title or 'title' param
    """
    if verbose:
        print("[INFO] Parsing video playback URL and title.")

    video: Optional[str] = None
    title: Optional[str] = None

    # Parse the response as a query string
    qs = parse_qs(page_content, keep_blank_values=True)

    # Try player_response first (most reliable)
    pr_raw = None
    if 'player_response' in qs and qs['player_response']:
        pr_raw = qs['player_response'][0]
        try:
            pr = json.loads(unquote(pr_raw))
            if isinstance(pr, dict):
                # Title
                title = pr.get('videoDetails', {}).get('title') or title
                # Streaming data
                sd = pr.get('streamingData', {}) or {}
                candidates = []
                if isinstance(sd, dict):
                    fmts = sd.get('formats') or []
                    adp = sd.get('adaptiveFormats') or []
                    if isinstance(fmts, list):
                        candidates.extend(fmts)
                    if isinstance(adp, list):
                        candidates.extend(adp)

                # Prefer mp4 URLs, else first available with direct 'url'
                def pick_url(items):
                    # Filter items that have a direct 'url'
                    direct = [it for it in items if isinstance(it, dict) and 'url' in it]
                    if not direct:
                        return None
                    mp4s = [it for it in direct if 'mimeType' in it and 'mp4' in str(it['mimeType']).lower()]
                    chosen = (mp4s[0] if mp4s else direct[0])
                    return chosen.get('url')

                if candidates:
                    cand_url = pick_url(candidates)
                    if cand_url:
                        video = cand_url
        except json.JSONDecodeError:
            if verbose:
                print("[WARN] Failed to decode player_response JSON. Falling back to scanning.")

    # Fallback: Scan raw content for 'videoplayback'
    if not video:
        content_list = page_content.split("&")
        for content in content_list:
            if content.startswith('title=') and not title:
                title = unquote(content.split('=')[-1])
            elif "videoplayback" in content and not video:
                video = unquote(content).split("|")[-1]
            if video and title:
                break

    if verbose:
        print(f"[INFO] Video URL: {video}")
        print(f"[INFO] Video Title: {title}")
    return video, title


def parse_cookie_header(cookie_header: str) -> requests.cookies.RequestsCookieJar:
    """Parses a Cookie header string like 'a=b; c=d' into a cookie jar."""
    jar = requests.cookies.RequestsCookieJar()
    if not cookie_header:
        return jar
    parts = [p.strip() for p in cookie_header.split(';') if p.strip()]
    for part in parts:
        if '=' in part:
            name, value = part.split('=', 1)
            jar.set(name.strip(), value.strip(), domain='.google.com')
    return jar


def build_session(
    cookies_file: Optional[str] = None,
    browser_cookies: Optional[str] = None,
    cookie_header: Optional[str] = None,
    verbose: bool = False,
) -> requests.Session:
    """Build a requests Session, optionally loading Google cookies from various sources.

    - cookies_file: Path to a Netscape cookies.txt file (exported from a browser).
    - browser_cookies: one of ['chrome','edge','firefox','brave','opera','vivaldi','any'] to load cookies
      directly from the installed browser using browser-cookie3 (optional dependency).
    - cookie_header: raw Cookie header string (e.g., "SID=...; HSID=...").
    """
    s = requests.Session()
    # Set a desktop-like UA to reduce chances of blocked requests
    s.headers.update({
        'User-Agent': (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
            'AppleWebKit/537.36 (KHTML, like Gecko) '
            'Chrome/124.0.0.0 Safari/537.36'
        ),
        'Referer': 'https://drive.google.com/'
    })

    total_loaded = 0

    # 1) Load from cookies file: support Netscape cookies.txt or JSON export formats
    if cookies_file:
        if verbose:
            print(f"[INFO] Loading cookies from file: {cookies_file}")
        try:
            with open(cookies_file, 'r', encoding='utf-8') as f:
                content = f.read().strip()
            if content.startswith('{') or content.startswith('['):
                # JSON cookie format (e.g., exported by Cookie-Editor)
                data = json.loads(content)
                cookies_list = data.get('cookies', data) if isinstance(data, dict) else data
                count = 0
                for c in cookies_list:
                    name = c.get('name')
                    value = c.get('value')
                    if not name:
                        continue
                    domain = c.get('domain') or '.google.com'
                    path = c.get('path') or '/'
                    secure = bool(c.get('secure', False))
                    # Create and set cookie
                    cookie = requests.cookies.create_cookie(
                        name=name,
                        value=value,
                        domain=domain,
                        path=path,
                        secure=secure,
                    )
                    s.cookies.set_cookie(cookie)
                    count += 1
                total_loaded += count
            else:
                # Assume Netscape/Mozilla cookies.txt
                mozjar = MozillaCookieJar()
                mozjar.load(cookies_file, ignore_discard=True, ignore_expires=True)
                s.cookies.update(mozjar)
                total_loaded += len(mozjar)
        except (FileNotFoundError, OSError, json.JSONDecodeError) as err:
            print(f"[WARN] Failed to load cookies from file: {err}")

    # 2) Load from installed browser (browser-cookie3)
    if browser_cookies:
        import importlib
        try:
            bc3 = importlib.import_module('browser_cookie3')
        except ImportError:
            print(
                "[ERROR] browser-cookie3 is not installed. Install it with 'pip install browser-cookie3' "
                "or remove --browser-cookies."
            )
            raise

        if verbose:
            print(f"[INFO] Loading cookies from browser: {browser_cookies}")

        domain = '.google.com'
        cj = None
        try:
            if browser_cookies == 'chrome':
                cj = bc3.chrome(domain_name=domain)
            elif browser_cookies == 'edge':
                cj = bc3.edge(domain_name=domain)
            elif browser_cookies == 'firefox':
                cj = bc3.firefox(domain_name=domain)
            elif browser_cookies == 'brave':
                cj = bc3.brave(domain_name=domain)
            elif browser_cookies == 'opera':
                cj = bc3.opera(domain_name=domain)
            elif browser_cookies == 'vivaldi':
                cj = bc3.vivaldi(domain_name=domain)
            elif browser_cookies == 'any':
                # Load from any available browser and filter by domain
                cj = bc3.load(domain_name=domain)
            else:
                print(f"[WARN] Unknown browser option: {browser_cookies}")
        except (RuntimeError, OSError, ValueError) as err:
            print(f"[WARN] Failed to load cookies from browser: {err}")
            cj = None

        if cj:
            s.cookies.update(cj)
            total_loaded += len(cj)

    # 3) Load from Cookie header string
    if cookie_header:
        jar = parse_cookie_header(cookie_header)
        s.cookies.update(jar)
        total_loaded += len(jar)

    if verbose:
        # Show a few cookie names for debugging
        names = [c.name for c in s.cookies][:5]
        print(f"[INFO] Loaded {total_loaded} cookies. Sample: {names}")

    return s

def download_file(url: str, session: requests.Session, filename: str, chunk_size: int, verbose: bool) -> None:
    """Downloads the file from the given URL using provided session, supports resuming."""
    headers = {}
    file_mode = 'wb'

    downloaded_size = 0
    if os.path.exists(filename):
        downloaded_size = os.path.getsize(filename)
        headers['Range'] = f"bytes={downloaded_size}-"
        file_mode = 'ab'

    if verbose:
        print(f"[INFO] Starting download from {url}")
        if downloaded_size > 0:
            print(f"[INFO] Resuming download from byte {downloaded_size}")

    response = session.get(url, stream=True, headers=headers)
    if response.status_code in (200, 206):  # 200 for new downloads, 206 for partial content
        total_size = int(response.headers.get('content-length', 0)) + downloaded_size
        with open(filename, file_mode) as file:
            with tqdm(total=total_size, initial=downloaded_size, unit='B', unit_scale=True, desc=filename, file=sys.stdout) as pbar:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if chunk:
                        file.write(chunk)
                        pbar.update(len(chunk))
        print(f"\n{filename} downloaded successfully.")
    else:
        print(f"Error downloading {filename}, status code: {response.status_code}")

def main(
    video_id: str,
    output_file: Optional[str] = None,
    chunk_size: int = 1024,
    verbose: bool = False,
    cookies_file: Optional[str] = None,
    browser_cookies: Optional[str] = None,
    cookie_header: Optional[str] = None,
) -> None:
    """Main function to process video ID and download the video file."""
    drive_url = f'https://drive.google.com/u/0/get_video_info?docid={video_id}&drive_originator_app=303'
    
    if verbose:
        print(f"[INFO] Accessing {drive_url}")

    session = build_session(cookies_file, browser_cookies, cookie_header, verbose)

    response = session.get(drive_url)
    page_content = response.text
    # Continue using the same session for subsequent requests

    video, title = get_video_url(page_content, verbose)

    # Determine filename
    def sanitize_name(name: str) -> str:
        # Windows-safe simple sanitization
        invalid = '<>:"/\\|?*'
        for ch in invalid:
            name = name.replace(ch, '_')
        return name.strip() or 'video'

    filename = output_file if output_file else None
    if not filename:
        base = title if title else video_id
        base = sanitize_name(base)
        # Try to infer extension from URL mime or path
        ext = 'mp4'
        try:
            parsed = urlparse(video or '')
            # Check query param 'mime'
            q = parse_qs(parsed.query)
            mime = (q.get('mime') or [None])[0]
            if mime and 'webm' in mime:
                ext = 'webm'
            elif mime and 'mp4' in mime:
                ext = 'mp4'
            # If path hints an extension
            path = parsed.path or ''
            if path.endswith('.webm'):
                ext = 'webm'
            elif path.endswith('.mp4'):
                ext = 'mp4'
        except Exception:
            pass
        filename = f"{base}.{ext}"
    if video:
        download_file(video, session, filename, chunk_size, verbose)
    else:
        print("Unable to retrieve the video URL. Ensure the video ID is correct and accessible.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Script to download videos from Google Drive.")
    parser.add_argument("video_id", type=str, help="The video ID from Google Drive (e.g., 'abc-Qt12kjmS21kjDm2kjd').")
    parser.add_argument("-o", "--output", type=str, help="Optional output file name for the downloaded video (default: video name in gdrive).")
    parser.add_argument("-c", "--chunk_size", type=int, default=1024, help="Optional chunk size (in bytes) for downloading the video. Default is 1024 bytes.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose mode.")
    parser.add_argument("--cookies-file", type=str, help="Path to a Netscape cookies.txt file (exported from a browser).")
    parser.add_argument(
        "--browser-cookies",
        choices=['chrome', 'edge', 'firefox', 'brave', 'opera', 'vivaldi', 'any'],
        help="Load Google cookies from an installed browser using browser-cookie3."
    )
    parser.add_argument(
        "--cookie",
        type=str,
        help="Raw Cookie header string to add (e.g., \"SID=...; HSID=...\")."
    )
    parser.add_argument("--version", action="version", version="%(prog)s 1.0")

    args = parser.parse_args()
    main(
        args.video_id,
        args.output,
        args.chunk_size,
        args.verbose,
        args.cookies_file,
        args.browser_cookies,
        args.cookie,
    )
