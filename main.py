from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import requests
import re
import json
import urllib.parse
import subprocess
import os
import time
import random
import urllib3
import tempfile

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/121.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
]

_search_cache = {}
_cache_ttl = 300

# Используем /tmp для кэша на Render
CACHE_DIR = os.path.join(tempfile.gettempdir(), "audio_cache")
os.makedirs(CACHE_DIR, exist_ok=True)
print(f"Cache directory: {CACHE_DIR}")

def get_session():
    session = requests.Session()
    session.headers.update({
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9,ru-RU,ru;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
        "sec-ch-ua": '"Not_A Brand";v="8", "Chromium";v="120", "Google Chrome";v="120"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    })
    session.verify = False
    return session

def get_cached_search(query, max_results):
    key = f"{query}_{max_results}"
    cached = _search_cache.get(key)
    if cached and (time.time() - cached["time"]) < _cache_ttl:
        return cached["data"]
    return None

def set_cached_search(query, max_results, data):
    key = f"{query}_{max_results}"
    _search_cache[key] = {"data": data, "time": time.time()}
    if len(_search_cache) > 100:
        oldest = min(_search_cache, key=lambda k: _search_cache[k]["time"])
        del _search_cache[oldest]

def search_youtube_ytdlp(query, max_results=20):
    try:
        encoded_query = query.replace('"', '\\"')
        if len(encoded_query) > 500:
            encoded_query = encoded_query[:500]
        cmd = [
            "yt-dlp",
            "--no-check-certificates",
            "--no-warnings",
            "--quiet",
            "--flat-playlist",
            "--dump-json",
            "--extractor-args", "youtubetab:skip=authcheck",
            f"ytsearch{max_results}:{encoded_query}"
        ]
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "PYTHONIOENCODING": "utf-8"}
        )
        if result.returncode != 0 or not result.stdout.strip():
            print(f"[YT-DLP ERROR] {result.stderr}")
            return None

        tracks = []
        for line in result.stdout.strip().split('\n'):
            if not line.strip():
                continue
            try:
                data = json.loads(line)
                if data.get('_type') == 'playlist':
                    continue
                video_id = data.get('id', '')
                if not video_id:
                    continue

                duration = int(data.get('duration', 0) or 0)

                if duration:
                    mins, secs = divmod(duration, 60)
                    hours, mins = divmod(mins, 60)
                    if hours:
                        duration_text = f"{hours}:{mins:02d}:{secs:02d}"
                    else:
                        duration_text = f"{mins}:{secs:02d}"
                else:
                    duration_text = ""

                thumbnails = data.get('thumbnails', [])
                thumb_url = thumbnails[-1].get('url', '') if thumbnails else ''
                thumb_small = thumbnails[0].get('url', thumb_url) if thumbnails else thumb_url

                track = {
                    "videoId": video_id,
                    "id": video_id,
                    "title": data.get('title', 'Unknown'),
                    "artist": data.get('channel', data.get('uploader', 'Unknown')),
                    "channelTitle": data.get('channel', data.get('uploader', 'Unknown')),
                    "channelId": data.get('channel_id', ''),
                    "thumbnail": {
                        "small": thumb_small,
                        "medium": thumb_url,
                        "large": thumb_url
                    },
                    "duration": duration,
                    "durationText": duration_text,
                    "description": data.get('description', ''),
                    "viewCount": str(data.get('view_count', '')),
                    "publishedTime": data.get('upload_date', ''),
                }
                tracks.append(track)
                if len(tracks) >= max_results:
                    break
            except json.JSONDecodeError:
                continue
        return tracks
    except subprocess.TimeoutExpired:
        print("[YT-DLP] Timeout during search")
        return None
    except Exception as e:
        print(f"[YT-DLP ERROR] {e}")
        import traceback
        traceback.print_exc()
        return None

def search_youtube(query, max_results=20):
    cached = get_cached_search(query, max_results)
    if cached:
        print(f"[CACHE] Returning cached results for: {query}")
        return cached

    print(f"[SEARCH] Searching: {query}")
    tracks = search_youtube_ytdlp(query, max_results)
    if tracks:
        result = {"tracks": tracks, "total": len(tracks), "cached": False}
        set_cached_search(query, max_results, result)
        return result
    print(f"[FAILED] Could not search for: {query}")
    return {"error": "Failed to search YouTube. Please try again later.", "tracks": [], "total": 0}

def get_direct_audio_url(video_id):
    """Получает прямую ссылку на аудиопоток (yt-dlp -g)"""
    formats_to_try = [
        "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio[acodec!=none]",
        "bestaudio[ext=m4a]",
        "bestaudio[ext=webm]",
        "bestaudio[acodec!=none]",
        "bestaudio",
        "worstaudio[acodec!=none]",
        "worstaudio",
    ]
    for fmt in formats_to_try:
        cmd = [
            "yt-dlp",
            "--no-check-certificates",
            "--no-warnings",
            "--quiet",
            "-f", fmt,
            "-g",
            "--extractor-args", "youtube:skip=authcheck",
            f"https://www.youtube.com/watch?v={video_id}"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)
        if result.returncode == 0 and result.stdout.strip():
            url = result.stdout.strip().split('\n')[0]
            if url.startswith('http'):
                return url
    return None

def download_audio(video_id):
    """Скачивает оригинальный аудиофайл (M4A/WebM) в кэш"""
    for ext in ['m4a', 'webm']:
        output_path = os.path.join(CACHE_DIR, f"{video_id}.{ext}")

        if os.path.exists(output_path):
            if time.time() - os.path.getmtime(output_path) < 1200:
                print(f"[CACHE] Using cached audio for {video_id}")
                return output_path
            else:
                os.remove(output_path)

        print(f"[AUDIO] Downloading {video_id} as {ext}...")
        cmd = [
            "yt-dlp",
            "--no-check-certificates",
            "--no-warnings",
            "--quiet",
            "-f", f"bestaudio[ext={ext}]/bestaudio",
            "--output", output_path,
            "--extractor-args", "youtube:skip=authcheck",
            "--no-playlist",
            f"https://www.youtube.com/watch?v={video_id}"
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode == 0 and os.path.exists(output_path):
            if os.path.getsize(output_path) > 1000:
                return output_path
            else:
                os.remove(output_path)
    return None

def search_lyrics_lrclib(query):
    try:
        encoded = urllib.parse.quote(query)
        session = get_session()
        response = session.get(
            f"https://lrclib.net/api/search?q={encoded}",
            timeout=30
        )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"[LYRICS ERROR] {e}")
        return []

# ===== API Routes =====

@app.route('/')
def index():
    return jsonify({
        "name": "YouTube Music Proxy",
        "version": "6.0",
        "description": "YouTube proxy with direct audio + cache fallback",
        "endpoints": {
            "search": "GET /youtube_search?q=QUERY&count=20",
            "audio": "GET /youtube_audio?id=VIDEO_ID",
            "stream": "GET /stream?id=VIDEO_ID",
            "direct_stream": "GET /direct_stream?url=ENCODED_URL",
            "lyrics": "GET /youtube_lyrics?q=QUERY",
            "health": "GET /health"
        }
    })

@app.route('/youtube_search')
def youtube_search():
    query = request.args.get('q', '').strip()
    count = min(int(request.args.get('count', 20)), 50)
    if not query:
        return jsonify({"error": "Query parameter 'q' is required"}), 400
    result = search_youtube(query, count)
    if "error" in result and not result.get("tracks"):
        return jsonify(result), 503
    return jsonify(result)

@app.route('/youtube_audio')
def youtube_audio():
    video_id = request.args.get('id', '').strip()
    if not video_id or not re.match(r'^[a-zA-Z0-9_-]{11}$', video_id):
        return jsonify({"error": "Valid video ID required"}), 400

    # Способ 1: прямая ссылка через прокси
    direct_url = get_direct_audio_url(video_id)
    if direct_url:
        proxied_direct = f"{request.host_url.rstrip('/')}/direct_stream?url={urllib.parse.quote(direct_url)}"
        return jsonify({
            "videoId": video_id,
            "audioUrl": proxied_direct,
            "type": "proxied_direct",
            "success": True
        })

    # Способ 2: скачивание и кэширование
    return jsonify({
        "videoId": video_id,
        "audioUrl": f"{request.host_url.rstrip('/')}/stream?id={video_id}",
        "type": "download",
        "success": True
    })

@app.route('/direct_stream')
def direct_stream():
    """Проксирует прямую ссылку YouTube, добавляя Referer и Origin"""
    url = request.args.get('url', '')
    if not url:
        return jsonify({"error": "url required"}), 400

    try:
        session = get_session()
        
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Referer": "https://www.youtube.com/",
            "Origin": "https://www.youtube.com",
            "Accept": "*/*",
            "Accept-Encoding": "identity;q=1, *;q=0",
            "Accept-Language": "en-US,en;q=0.9",
            "Range": request.headers.get("Range", "bytes=0-"),
            "Sec-Fetch-Dest": "audio",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "cross-site",
            "Cache-Control": "no-cache",
        }
        
        resp = session.get(url, headers=headers, stream=True, timeout=30, verify=False)

        def generate():
            try:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        yield chunk
            except Exception as e:
                print(f"[STREAM GENERATOR ERROR] {e}")
                raise

        response = Response(
            generate(),
            status=resp.status_code,
            content_type=resp.headers.get('content-type', 'audio/mp4'),
            direct_passthrough=True
        )
        
        # Копируем заголовки
        for header in ['Content-Length', 'Content-Range', 'Accept-Ranges']:
            if header in resp.headers:
                response.headers[header] = resp.headers[header]
        
        # Добавляем CORS
        response.headers['Access-Control-Allow-Origin'] = '*'
        response.headers['Access-Control-Allow-Headers'] = 'Range, Content-Range, Accept-Ranges'
        response.headers['Access-Control-Expose-Headers'] = 'Content-Range, Content-Length, Accept-Ranges'
        response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate'
        
        return response
    except requests.exceptions.RequestException as e:
        print(f"[DIRECT STREAM ERROR] {e}")
        return jsonify({"error": f"Network error: {str(e)}"}), 500
    except Exception as e:
        print(f"[DIRECT STREAM ERROR] {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500

@app.route('/stream')
def stream_audio():
    """Отдаёт закэшированный аудиофайл"""
    video_id = request.args.get('id', '').strip()
    if not video_id:
        return jsonify({"error": "Video ID required"}), 400

    try:
        audio_path = download_audio(video_id)
        if not audio_path:
            return jsonify({"error": "Failed to download audio"}), 500

        ext = audio_path.split('.')[-1]
        content_type = "audio/mp4" if ext == "m4a" else "audio/webm"
        file_size = os.path.getsize(audio_path)
        range_header = request.headers.get("Range", "")

        if range_header:
            range_match = re.match(r'bytes=(\d+)-(\d*)', range_header)
            if range_match:
                start = int(range_match.group(1))
                end = int(range_match.group(2)) if range_match.group(2) else file_size - 1
                length = end - start + 1

                def range_generator():
                    with open(audio_path, 'rb') as f:
                        f.seek(start)
                        remaining = length
                        while remaining > 0:
                            chunk_size = min(65536, remaining)
                            chunk = f.read(chunk_size)
                            if not chunk:
                                break
                            remaining -= len(chunk)
                            yield chunk

                resp = Response(
                    range_generator(),
                    status=206,
                    content_type=content_type,
                    direct_passthrough=True
                )
                resp.headers['Content-Range'] = f'bytes {start}-{end}/{file_size}'
                resp.headers['Content-Length'] = str(length)
                resp.headers['Accept-Ranges'] = 'bytes'
                resp.headers['Access-Control-Allow-Origin'] = '*'
                resp.headers['Access-Control-Allow-Headers'] = 'Range'
                resp.headers['Access-Control-Expose-Headers'] = 'Content-Range, Content-Length, Accept-Ranges'
                return resp

        def full_generator():
            with open(audio_path, 'rb') as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    yield chunk

        resp = Response(
            full_generator(),
            status=200,
            content_type=content_type,
            direct_passthrough=True
        )
        resp.headers['Content-Length'] = str(file_size)
        resp.headers['Accept-Ranges'] = 'bytes'
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Headers'] = 'Range'
        resp.headers['Access-Control-Expose-Headers'] = 'Content-Range, Content-Length, Accept-Ranges'
        resp.headers['Cache-Control'] = 'public, max-age=600'
        return resp
    except Exception as e:
        print(f"[STREAM ERROR] {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/youtube_lyrics')
def youtube_lyrics():
    query = request.args.get('q', '').strip()
    if not query:
        return jsonify({"error": "Query parameter 'q' is required"}), 400
    results = search_lyrics_lrclib(query)
    if results and len(results) > 0:
        track = results[0]
        return jsonify({
            "found": True,
            "trackName": track.get("trackName"),
            "artistName": track.get("artistName"),
            "albumName": track.get("albumName"),
            "duration": track.get("duration"),
            "plainLyrics": track.get("plainLyrics"),
            "syncedLyrics": track.get("syncedLyrics"),
            "source": "lrclib.net"
        })
    return jsonify({"found": False, "lyrics": None})

@app.route('/health')
def health():
    yt_dlp_ok = False
    yt_dlp_version = "not found"
    try:
        result = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            yt_dlp_ok = True
            yt_dlp_version = result.stdout.strip()
    except:
        pass

    search_test = False
    if yt_dlp_ok:
        try:
            test_result = subprocess.run(
                ["yt-dlp", "--flat-playlist", "--dump-json", "ytsearch1:test"],
                capture_output=True, text=True, timeout=15
            )
            search_test = test_result.returncode == 0 and len(test_result.stdout.strip()) > 0
        except:
            pass

    cache_files = []
    if os.path.exists(CACHE_DIR):
        cache_files = [f for f in os.listdir(CACHE_DIR) if f.endswith(('.m4a', '.webm', '.mp3'))]

    return jsonify({
        "status": "ok",
        "yt-dlp": yt_dlp_version,
        "yt-dlp_available": yt_dlp_ok,
        "search_test_passed": search_test,
        "search_cache_size": len(_search_cache),
        "audio_cache_files": len(cache_files),
        "cache_dir": CACHE_DIR,
        "audio_formats": "M4A / WebM (original, no ffmpeg needed)"
    })

if __name__ == '__main__':
    print("=" * 60)
    print("  YouTube Music Proxy v6.0 (Render)")
    print("  Direct audio proxying + file cache")
    print(f"  Cache dir: {CACHE_DIR}")
    print("=" * 60)
    try:
        result = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True)
        print(f"yt-dlp: {result.stdout.strip()}")
    except:
        print("WARNING: yt-dlp not found!")
    
    # Render использует порт из переменной окружения PORT
    port = int(os.environ.get('PORT', 5000))
    print(f"Starting on port {port}")
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
