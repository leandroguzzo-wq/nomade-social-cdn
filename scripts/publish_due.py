"""
GitHub Actions runner: publica las piezas IG cuya hora programada cae
en la ventana [now - WINDOW_MIN, now + WINDOW_MIN/2].

Se ejecuta cada hora via cron. Solo publica IG (FB cross-post via Meta
Business Suite no es necesario, se hace desde IG nativo).

Vars de entorno:
  META_PAGE_ACCESS_TOKEN  Token con scope instagram_content_publish + instagram_basic
  META_IG_BUSINESS_ID     Cuenta IG Business
  META_PAGE_ID            (no usado, queda para futuro)
  WINDOW_MIN              Minutos a mirar atras (default 60)
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

GRAPH = "https://graph.facebook.com/v25.0"
TZ_AR = timezone(timedelta(hours=-3))

TOKEN = os.environ["META_PAGE_ACCESS_TOKEN"]
IG_ID = os.environ["META_IG_BUSINESS_ID"]
WINDOW_MIN = int(os.environ.get("WINDOW_MIN", "60"))

REPO_ROOT = Path(__file__).parent.parent
CAL_PATH = REPO_ROOT / "scripts" / "calendar.json"
LOG_PATH = REPO_ROOT / "scripts" / "publish_log.jsonl"


def http(method, url, data=None):
    if data is not None:
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(url, data=body, method=method,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
    else:
        req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code}: {e.read().decode()}")


def log_event(ev):
    ev["logged_at_utc"] = datetime.now(timezone.utc).isoformat()
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(ev, ensure_ascii=False) + "\n")


def already_published(piece_id):
    if not LOG_PATH.exists():
        return False
    for line in LOG_PATH.read_text(encoding="utf-8").splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("piece_id") == piece_id and ev.get("result", {}).get("status") == "published":
            return True
    return False


def wait_container(cid, max_wait=300, poll=10):
    start = time.time()
    while time.time() - start < max_wait:
        r = http("GET", f"{GRAPH}/{cid}?fields=status_code,status&access_token={TOKEN}")
        sc = r.get("status_code", "")
        if sc == "FINISHED":
            return True
        if sc in ("ERROR", "EXPIRED"):
            raise RuntimeError(f"Container {cid}: {r}")
        time.sleep(poll)
    raise TimeoutError(f"Container {cid} no llego a FINISHED en {max_wait}s")


def post_photo(image_url, caption):
    r = http("POST", f"{GRAPH}/{IG_ID}/media", {
        "image_url": image_url,
        "caption": caption,
        "access_token": TOKEN,
    })
    wait_container(r["id"])
    pub = http("POST", f"{GRAPH}/{IG_ID}/media_publish", {
        "creation_id": r["id"],
        "access_token": TOKEN,
    })
    return pub["id"]


def post_reel(video_url, caption):
    r = http("POST", f"{GRAPH}/{IG_ID}/media", {
        "video_url": video_url,
        "caption": caption,
        "media_type": "REELS",
        "access_token": TOKEN,
    })
    wait_container(r["id"], max_wait=600)
    pub = http("POST", f"{GRAPH}/{IG_ID}/media_publish", {
        "creation_id": r["id"],
        "access_token": TOKEN,
    })
    return pub["id"]


def post_carousel(media_list, caption):
    children = []
    for m in media_list:
        if m["kind"] == "IMAGE":
            r = http("POST", f"{GRAPH}/{IG_ID}/media", {
                "image_url": m["url"],
                "is_carousel_item": "true",
                "access_token": TOKEN,
            })
        else:  # VIDEO
            r = http("POST", f"{GRAPH}/{IG_ID}/media", {
                "video_url": m["url"],
                "media_type": "VIDEO",
                "is_carousel_item": "true",
                "access_token": TOKEN,
            })
            wait_container(r["id"], max_wait=300)
        children.append(r["id"])

    carousel = http("POST", f"{GRAPH}/{IG_ID}/media", {
        "media_type": "CAROUSEL",
        "children": ",".join(children),
        "caption": caption,
        "access_token": TOKEN,
    })
    wait_container(carousel["id"])
    pub = http("POST", f"{GRAPH}/{IG_ID}/media_publish", {
        "creation_id": carousel["id"],
        "access_token": TOKEN,
    })
    return pub["id"]


def publish_piece(p):
    ptype = p["type"]
    caption = p["copy"]
    if ptype == "carousel":
        return post_carousel(p["media"], caption)
    if ptype in ("reel", "video"):
        return post_reel(p["media"][0]["url"], caption)
    if ptype == "photo":
        return post_photo(p["media"][0]["url"], caption)
    raise ValueError(f"tipo desconocido: {ptype}")


def main():
    if not CAL_PATH.exists():
        print(f"calendar.json no encontrado en {CAL_PATH}")
        sys.exit(0)

    cal = json.loads(CAL_PATH.read_text(encoding="utf-8"))
    now = datetime.now(timezone.utc)
    window_back = now - timedelta(minutes=WINDOW_MIN)
    window_fwd = now + timedelta(minutes=WINDOW_MIN // 2)

    print(f"[{now.isoformat()}] Ventana: {window_back.isoformat()} → {window_fwd.isoformat()}")

    candidatos = []
    for p in cal["pieces"]:
        if p["channel"] != "IG":
            continue
        when = datetime.fromisoformat(p["scheduled_at"])
        # Pasar a UTC para comparar
        when_utc = when.astimezone(timezone.utc)
        if window_back <= when_utc <= window_fwd:
            if already_published(p["id"]):
                print(f"  [SKIP] ya publicado: {p['id']}")
                continue
            candidatos.append(p)

    if not candidatos:
        print("Nada para publicar en esta ventana.")
        return

    print(f"Publicando {len(candidatos)} pieza(s):")
    for p in candidatos:
        print(f"  → {p['id']}  ({p['type']})")

    for p in candidatos:
        try:
            mid = publish_piece(p)
            log_event({
                "piece_id": p["id"],
                "result": {"status": "published", "media_id": mid},
                "channel": "IG",
                "type": p["type"],
                "scheduled_at": p["scheduled_at"],
            })
            print(f"  ✓ {p['id']} → IG media {mid}")
        except Exception as e:
            log_event({
                "piece_id": p["id"],
                "result": {"status": "error", "error": str(e)},
                "channel": "IG",
                "type": p["type"],
                "scheduled_at": p["scheduled_at"],
            })
            print(f"  ✗ {p['id']}: {e}")


if __name__ == "__main__":
    main()
