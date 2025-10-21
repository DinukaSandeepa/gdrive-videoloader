from urllib.parse import unquote, parse_qs, urlparse
import requests
import argparse
import sys
from tqdm import tqdm
import os
from typing import Optional, Tuple, Any
from http.cookiejar import MozillaCookieJar
import json
import shutil
import subprocess
import re
import html

def get_video_url(page_content: str, verbose: bool) -> Tuple[Optional[str], Optional[str]]:
    """Extracts the video playback URL and title from get_video_info response.

    Tries in order:
    1) Parse player_response JSON and read streamingData URLs
    2) Fallback to scanning for 'videoplayback' in the raw content
    3) Title from player_response.videoDetails.title or 'title' param
    """
    if verbose:
        print("[INFO] Parsing video playback URL and title.")

    video_url_val: Optional[str] = None
    title_val: Optional[str] = None

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
                title_val = pr.get('videoDetails', {}).get('title') or title_val
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
                        video_url_val = cand_url
        except json.JSONDecodeError:
            if verbose:
                print("[WARN] Failed to decode player_response JSON. Falling back to scanning.")

    # Fallback: Scan raw content for 'videoplayback'
    if not video_url_val:
        content_list = page_content.split("&")
        for content in content_list:
            if content.startswith('title=') and not title_val:
                title_val = unquote(content.split('=')[-1])
            elif "videoplayback" in content and not video_url_val:
                candidate = unquote(content).split("|")[-1]
                # Remove leading 'url=' if present
                if candidate.startswith('url='):
                    candidate = candidate[4:]
                video_url_val = candidate
            if video_url_val and title_val:
                break

    # Regex-based fallback on fully decoded content
    if not video_url_val:
        decoded = unquote(page_content)
        m = re.search(r'https?://[^"\s]*videoplayback[^"\s]*', decoded)
        if m:
            video_url_val = m.group(0)

    if verbose:
        print(f"[INFO] Video URL: {video_url_val}")
        print(f"[INFO] Video Title: {title_val}")
    return video_url_val, title_val


def parse_player_response(page_content: str) -> Optional[dict]:
    qs = parse_qs(page_content, keep_blank_values=True)
    pr_raw = None
    if 'player_response' in qs and qs['player_response']:
        pr_raw = qs['player_response'][0]
        try:
            pr = json.loads(unquote(pr_raw))
            if isinstance(pr, dict):
                return pr
        except json.JSONDecodeError:
            return None
    return None


def try_uc_direct_url(session: requests.Session, file_id: str, verbose: bool = False) -> Optional[str]:
    """Attempt to get a direct download URL via uc?export=download flow (handles confirm token).

    Returns the final URL to download or None if not available.
    """
    base = 'https://drive.google.com'
    first = f'{base}/uc?export=download&id={file_id}'
    if verbose:
        print(f"[INFO] Trying uc fallback: {first}")
    r = session.get(first)
    # If headers already provide a direct file
    cd = r.headers.get('Content-Disposition')
    if cd:
        if verbose:
            print("[INFO] uc returned direct download.")
        return r.url

    # Parse confirm token from HTML
    txt = r.text
    # Pattern 1: hidden input name="confirm" value="TOKEN"
    m = re.search(r'name=\"confirm\"\s+value=\"([^\"]+)\"', txt)
    token = m.group(1) if m else None
    if not token:
        # Pattern 2: href contains confirm=TOKEN
        m2 = re.search(r'href=\"([^"]*?confirm=[^&\"]+[^\"]*)\"', txt)
        if m2:
            link = html.unescape(m2.group(1))
            if link.startswith('/'):
                link = base + link
            if verbose:
                print("[INFO] uc found confirm link.")
            return link

    if token:
        # Build confirmed URL
        conf_url = f'{base}/uc?export=download&confirm={token}&id={file_id}'
        if verbose:
            print(f"[INFO] uc confirm token acquired.")
        return conf_url

    if verbose:
        print("[WARN] uc fallback did not find a confirm token.")
    return None


def parse_content_disposition_filename(headers: dict) -> Optional[str]:
    """Extract a filename from Content-Disposition headers, RFC 5987 aware.

    Supports filename*=UTF-8''... and filename="...".
    """
    cd = headers.get('Content-Disposition') or headers.get('content-disposition')
    if not cd:
        return None
    # Try RFC 5987 filename*
    m_star = re.search(r"filename\*\s*=\s*([^']*)''([^;]+)", cd, flags=re.IGNORECASE)
    if m_star:
        enc = (m_star.group(1) or 'UTF-8').upper()
        val = unquote(m_star.group(2))
        try:
            return val.encode('latin1').decode(enc, errors='ignore') if enc != 'UTF-8' else val
        except Exception:
            return val
    # Try simple filename="..."
    m = re.search(r'filename\s*=\s*"([^"]+)"', cd, flags=re.IGNORECASE)
    if m:
        return m.group(1)
    # Or filename=without-quotes
    m2 = re.search(r'filename\s*=\s*([^;]+)', cd, flags=re.IGNORECASE)
    if m2:
        return m2.group(1).strip()
    return None


def head_filename(session: requests.Session, url: str, verbose: bool = False) -> Optional[str]:
    """Attempt to fetch the suggested filename from response headers using HEAD (or fallback GET)."""
    try:
        resp = session.head(url, allow_redirects=True)
        name = parse_content_disposition_filename(resp.headers)
        if name:
            if verbose:
                print(f"[INFO] Filename from HEAD: {name}")
            return name
        # Some endpoints don't support HEAD; try a lightweight GET
        resp = session.get(url, stream=True)
        try:
            name = parse_content_disposition_filename(resp.headers)
            if name:
                if verbose:
                    print(f"[INFO] Filename from GET headers: {name}")
                return name
        finally:
            resp.close()
    except requests.RequestException:
        return None
    return None


def sanitize_filename(name: str) -> str:
    invalid = '<>:"/\\|?*'
    for ch in invalid:
        name = name.replace(ch, '_')
    # Trim trailing dots/spaces which Windows disallows
    return name.strip().rstrip('. ')


def extract_streams(pr: dict) -> Tuple[Optional[str], list, list, list]:
    """Return (title, progressive_formats, adaptive_videos, adaptive_audios)."""
    pr_title = pr.get('videoDetails', {}).get('title') if isinstance(pr, dict) else None
    sd = pr.get('streamingData', {}) if isinstance(pr, dict) else {}
    progressive = []
    adaptive_v = []
    adaptive_a = []
    if isinstance(sd, dict):
        fmts = sd.get('formats') or []
        adp = sd.get('adaptiveFormats') or []
        if isinstance(fmts, list):
            progressive = [f for f in fmts if isinstance(f, dict)]
        if isinstance(adp, list):
            for f in adp:
                if not isinstance(f, dict):
                    continue
                mime_t = str(f.get('mimeType', '')).lower()
                if mime_t.startswith('video/'):
                    adaptive_v.append(f)
                elif mime_t.startswith('audio/'):
                    adaptive_a.append(f)
    return pr_title, progressive, adaptive_v, adaptive_a


def _height_of(stream: dict) -> int:
    # Try height directly or parse from qualityLabel like '1080p'
    h = stream.get('height')
    if isinstance(h, int):
        return h
    ql = stream.get('qualityLabel')
    if isinstance(ql, str) and ql.endswith('p'):
        num = ''.join([c for c in ql if c.isdigit()])
        return int(num) if num.isdigit() else 0
    return 0


def _bitrate_of(stream: dict) -> int:
    br = stream.get('bitrate')
    if isinstance(br, int):
        return br
    abr = stream.get('averageBitrate')
    return int(abr) if isinstance(abr, int) else 0


def choose_best_streams(
    progressive: list,
    adaptive_v: list,
    adaptive_a: list,
    preferred: str = 'best',
    itag: Optional[str] = None,
) -> Tuple[Optional[dict], Optional[dict], Optional[dict]]:
    """Return (selected_progressive, selected_video, selected_audio).

    If itag is provided, try to match it across any streams. If adaptive video selected,
    pick best matching audio as well.
    """
    # itag override
    if itag is not None:
        # Search progressive first, then adaptive video, then audio (rare)
        for f in progressive:
            if str(f.get('itag')) == str(itag):
                return f, None, None
        for v in adaptive_v:
            if str(v.get('itag')) == str(itag):
                # Get best audio companion
                best_a = max(adaptive_a, key=_bitrate_of) if adaptive_a else None
                return None, v, best_a
        for a in adaptive_a:
            if str(a.get('itag')) == str(itag):
                return None, None, a

    if preferred == 'progressive':
        # Choose highest resolution progressive (fallback to bitrate)
        prog = None
        if progressive:
            prog = max(progressive, key=lambda s: (_height_of(s), _bitrate_of(s)))
        return prog, None, None

    # preferred == 'best': try adaptive merge first
    if adaptive_v:
        best_v = max(adaptive_v, key=lambda s: (_height_of(s), _bitrate_of(s)))
        best_a = max(adaptive_a, key=_bitrate_of) if adaptive_a else None
        if best_a:
            return None, best_v, best_a
    # Fallback to progressive
    prog = None
    if progressive:
        prog = max(progressive, key=lambda s: (_height_of(s), _bitrate_of(s)))
    return prog, None, None


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

def download_file(url: str, http: requests.Session, out_path: str, chunk_size: int, verbose: bool) -> None:
    """Downloads the file from the given URL using provided session, supports resuming."""
    headers = {}
    file_mode = 'wb'

    downloaded_size = 0
    if os.path.exists(out_path):
        downloaded_size = os.path.getsize(out_path)
        headers['Range'] = f"bytes={downloaded_size}-"
        file_mode = 'ab'

    if verbose:
        print(f"[INFO] Starting download from {url}")
        if downloaded_size > 0:
            print(f"[INFO] Resuming download from byte {downloaded_size}")

    response = http.get(url, stream=True, headers=headers)
    if response.status_code in (200, 206):  # 200 for new downloads, 206 for partial content
        total_size = int(response.headers.get('content-length', 0)) + downloaded_size
        with open(out_path, file_mode) as file:
            with tqdm(total=total_size, initial=downloaded_size, unit='B', unit_scale=True, desc=out_path, file=sys.stdout) as pbar:
                for chunk in response.iter_content(chunk_size=chunk_size):
                    if chunk:
                        file.write(chunk)
                        pbar.update(len(chunk))
        print(f"\n{out_path} downloaded successfully.")
    else:
        print(f"Error downloading {out_path}, status code: {response.status_code}")


def merge_streams_ffmpeg(video_path: str, audio_path: str, output_path: str, verbose: bool) -> bool:
    """Merge video+audio into output_path using ffmpeg with stream copy. Returns True on success."""
    ffmpeg = shutil.which('ffmpeg')
    if not ffmpeg:
        print("[WARN] ffmpeg not found on PATH. Install ffmpeg or use --quality progressive to avoid merging.")
        return False
    cmd = [ffmpeg, '-y', '-i', video_path, '-i', audio_path, '-c', 'copy', output_path]
    if verbose:
        print(f"[INFO] Merging with ffmpeg: {' '.join(cmd)}")
    try:
        res = subprocess.run(cmd, capture_output=not verbose, text=True, check=True)
        return res.returncode == 0
    except subprocess.CalledProcessError as cpe:
        if not verbose and cpe.stderr:
            print(cpe.stderr)
        print("[ERROR] ffmpeg merge failed.")
        return False
    except (OSError, FileNotFoundError) as err:
        print(f"[ERROR] Failed to run ffmpeg: {err}")
        return False

def main(
    video_id: str,
    output_file: Optional[str] = None,
    _chunk_size: int = 1024,
    verbose: bool = False,
    cookies_file: Optional[str] = None,
    browser_cookies: Optional[str] = None,
    cookie_header: Optional[str] = None,
) -> dict[str, Any]:
    """Main function to process video ID and download the video file."""
    drive_url = f'https://drive.google.com/u/0/get_video_info?docid={video_id}&drive_originator_app=303'
    
    if verbose:
        print(f"[INFO] Accessing {drive_url}")

    session_obj = build_session(cookies_file, browser_cookies, cookie_header, verbose)

    response = session_obj.get(drive_url)
    page_content = response.text
    # Continue using the same session for subsequent requests

    # Prefer parsing detailed streams for quality selection
    pr = parse_player_response(page_content)
    if pr:
        title, prog_streams, vid_streams, aud_streams = extract_streams(pr)
    else:
        prog_streams, vid_streams, aud_streams = [], [], []
        title = None

    # CLI selection will be parsed from args in __main__
    # Defer filename decision to after stream selection
    filename = output_file if output_file else None
    # The actual download selection is done after argument parsing in __main__ where we know quality/itag.
    return {
    'session': session_obj,
        'page_content': page_content,
        'title': title,
        'prog_streams': prog_streams,
        'vid_streams': vid_streams,
        'aud_streams': aud_streams,
        'default_filename': filename,
    }

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

    parser.add_argument(
        "-q", "--quality",
        choices=['best', 'progressive'],
        default='best',
        help="Quality selection: 'best' tries adaptive (video+audio) merge via ffmpeg, falling back to best progressive; 'progressive' only downloads the best single-file stream."
    )
    parser.add_argument(
        "--itag",
        type=str,
        help="Force a specific itag to download. If itag is a video-only stream, best audio will be merged when possible."
    )

    args = parser.parse_args()
    # Run pre-download flow to get streams
    result = main(
        args.video_id,
        args.output,
        args.chunk_size,
        args.verbose,
        args.cookies_file,
        args.browser_cookies,
        args.cookie,
    )

    http_session = result['session']
    video_title = result['title']
    prog_list = result['prog_streams']
    v_list = result['vid_streams']
    a_list = result['aud_streams']
    out_name = result['default_filename']

    # If we have streams, choose best according to args
    video_url = None
    audio_url = None
    container_hint = None

    if prog_list or v_list or a_list:
        sel_prog, sel_vid, sel_aud = choose_best_streams(
            prog_list, v_list, a_list, preferred=args.quality, itag=args.itag
        )
        def get_url(s: Optional[dict]) -> Optional[str]:
            return s.get('url') if isinstance(s, dict) else None
        if sel_prog:
            video_url = get_url(sel_prog)
            # infer container
            prog_mime = str(sel_prog.get('mimeType', '')).lower()
            if 'mp4' in prog_mime:
                container_hint = 'mp4'
            elif 'webm' in prog_mime:
                container_hint = 'webm'
        elif sel_vid and get_url(sel_vid):
            video_url = get_url(sel_vid)
            audio_url = get_url(sel_aud) if sel_aud else None
            vmt = str(sel_vid.get('mimeType', '')).lower()
            amt = str(sel_aud.get('mimeType', '')).lower() if sel_aud else ''
            # choose final container: mp4 if both are mp4; else mkv as safe container
            if 'mp4' in vmt and 'mp4' in amt:
                container_hint = 'mp4'
            elif 'webm' in vmt and 'webm' in amt:
                container_hint = 'webm'
            else:
                container_hint = 'mkv'
    else:
        # Fallback to simple URL extraction
        simple_url, _simple_title = get_video_url(result['page_content'], args.verbose)
        video_url = simple_url
        if _simple_title and not video_title:
            video_title = _simple_title
        # try to infer container from query mime
        if video_url:
            q = parse_qs(urlparse(video_url).query)
            mime = (q.get('mime') or [None])[0]
            if mime:
                if 'mp4' in mime:
                    container_hint = 'mp4'
                elif 'webm' in mime:
                    container_hint = 'webm'

        # Last resort: try uc direct download flow
        if not video_url:
            uc_url = try_uc_direct_url(http_session, args.video_id, args.verbose)
            if uc_url:
                video_url = uc_url

    # Determine final filename if not provided earlier
    if not out_name:
        # Try to get filename from server headers first (most accurate)
        header_name = head_filename(http_session, video_url, args.verbose) if video_url else None
        if header_name:
            out_name = sanitize_filename(header_name)
        else:
            # Fall back to title from player_response or the file ID
            base = video_title if video_title else args.video_id
            base = sanitize_filename(base or 'video') or 'video'
            ext = container_hint or 'mp4'
            out_name = f"{base}.{ext}"

    # If user provided a name without extension and we know the container, append it
    if out_name and container_hint:
        root, ext = os.path.splitext(out_name)
        if not ext:
            out_name = f"{root}.{container_hint}"

    if not video_url:
        print("Unable to retrieve the video URL. Ensure the video ID is correct and accessible.")
        sys.exit(1)

    # If we have both video and audio URLs, perform two downloads and merge
    if audio_url:
        base_noext, _ = os.path.splitext(out_name)
        vtemp = f"{base_noext}.video.tmp"
        atemp = f"{base_noext}.audio.tmp"
        try:
            download_file(video_url, http_session, vtemp, args.chunk_size, args.verbose)
            download_file(audio_url, http_session, atemp, args.chunk_size, args.verbose)
            merged_ok = merge_streams_ffmpeg(vtemp, atemp, out_name, args.verbose)
            if not merged_ok:
                print("[ERROR] Could not merge video and audio. You can manually merge the .tmp files with ffmpeg.")
            else:
                print(f"[INFO] Saved merged file: {out_name}")
        finally:
            # Cleanup temp files if they exist
            for p in (vtemp, atemp):
                try:
                    if os.path.exists(p):
                        os.remove(p)
                except OSError:
                    pass
    else:
        # Single progressive download
        download_file(video_url, http_session, out_name, args.chunk_size, args.verbose)
