"""updock -- real releases for your Docker containers, one tap to update.

Every night by default (``UPDOCK_CHECK_AT``, local time, or the frequency
chosen in the web interface) updock looks at each running
container that came from a registry and asks the registry what its tag --
usually ``latest`` -- points to *now*. A changed digest alone means nothing
(images are rebuilt all the time), so updock works out the **version** on
both sides, the running image and the remote one:

1. an ``<IMAGE>_VERSION`` environment variable (``PG_VERSION``,
   ``VALKEY_VERSION``...), which official images set to the exact version;
2. else the OCI label ``org.opencontainers.image.version``, but only if it
   matches a real release tag of the project on GitHub (some images put
   their base image's version there);
3. else the newest stable GitHub release published before the image was
   built.

linuxserver.io's ``-lsNNN`` packaging suffix is set aside: a bump of it
alone is a rebuild, not a release.

Only a real version change produces an ntfy notification, with the version
jump (major/minor/patch), the release notes from the project's GitHub
repository, a warning when they mention breaking changes, and buttons:
**Update**, **Notes**, **Ignore**. The buttons call back into updock
(``UPDOCK_PUBLIC_URL``, reachable from the phone over a VPN or the LAN) with
an HMAC-signed, single-use, expiring token.

An update runs ``docker compose pull`` + ``up -d`` on that one service, with
the very compose files and project the container was started with (read
from its ``com.docker.compose.*`` labels), waits for the container to be
running and healthy, and offers a **Roll back** to the previous image.

The web interface (``/``, same port) lists every container grouped by
compose project and offers the same actions, plus "check now" and
"recreate" for a container left on a deleted image. Its JSON API
(``/api/*``) only answers requests carrying the ``X-Updock`` header -- a
cross-site page cannot send one -- and, if ``UPDOCK_UI_PASSWORD`` is set,
HTTP Basic auth.

Per-container labels:
  updock.enable=false     never watch this container
  updock.github=org/repo  GitHub repository to read releases from

Run ``python updock.py --selftest`` for a dependency-free sanity check.
"""

import base64
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NTFY_URL = os.environ.get("UPDOCK_NTFY_URL", "https://ntfy.sh").rstrip("/")
NTFY_TOPIC = os.environ.get("UPDOCK_NTFY_TOPIC", "")
NTFY_TOKEN = os.environ.get("UPDOCK_NTFY_TOKEN", "")
SECRET = os.environ.get("UPDOCK_SECRET", "").encode()
PUBLIC_URL = os.environ.get("UPDOCK_PUBLIC_URL", "").rstrip("/")
PORT = int(os.environ.get("UPDOCK_PORT", "8099"))
CHECK_AT = os.environ.get("UPDOCK_CHECK_AT", "03:30")  # default schedule, see parse_schedule
CHECK_ON_START = os.environ.get("UPDOCK_CHECK_ON_START", "true").lower() == "true"
GITHUB_TOKEN = os.environ.get("UPDOCK_GITHUB_TOKEN", "")
NOTIFY_REBUILDS = os.environ.get("UPDOCK_NOTIFY_REBUILDS", "false").lower() == "true"
UI_PASSWORD = os.environ.get("UPDOCK_UI_PASSWORD", "")
LANG = os.environ.get("UPDOCK_LANG", "en").lower()[:2]
TOKEN_TTL_DAYS = 7
HEALTH_TIMEOUT = 180  # seconds to wait for an updated container
STATE_FILE = os.environ.get("UPDOCK_STATE_FILE", "/data/state.json")
DOCKER_SOCK = "/var/run/docker.sock"

# The OCI "source" label often points to a packaging repo rather than the
# project itself; these images get their release notes from upstream.
GITHUB_OVERRIDES = {
    "jellyfin/jellyfin": "jellyfin/jellyfin",
    "ghcr.io/immich-app/immich-server": "immich-app/immich",
    "ghcr.io/immich-app/immich-machine-learning": "immich-app/immich",
    "ghcr.io/blakeblackshear/frigate": "blakeblackshear/frigate",
    "ghcr.io/seerr-team/seerr": "seerr-team/seerr",
    "portainer/portainer-ce": "portainer/portainer",
    "henrygd/beszel": "henrygd/beszel",
    "henrygd/beszel-agent": "henrygd/beszel",
    "cloudflare/cloudflared": "cloudflare/cloudflared",
    "ollama/ollama": "ollama/ollama",
    "getwud/wud": "getwud/wud",
    "ghcr.io/homarr-labs/homarr": "homarr-labs/homarr",
    "ghcr.io/paperless-ngx/paperless-ngx": "paperless-ngx/paperless-ngx",
    "valkey/valkey": "valkey-io/valkey",
    "nginx": "nginx/nginx",
    "caddy": "caddyserver/caddy",
    "lscr.io/linuxserver/jellyfin": "jellyfin/jellyfin",
    "ghcr.io/seanmorley15/adventurelog": "seanmorley15/AdventureLog",
    "icereed/paperless-gpt": "icereed/paperless-gpt",
    "prodrigestivill/postgres-backup-local": "prodrigestivill/docker-postgres-backup-local",
    "ghcr.io/calibrain/shelfmark": "calibrain/shelfmark",
    "gotenberg/gotenberg": "gotenberg/gotenberg",
    "apache/tika": "apache/tika",
}
# Images whose version env var does not follow <NAME>_VERSION.
ENV_VERSION_KEYS = {"postgres": "PG_VERSION", "redis": "REDIS_VERSION"}
BREAKING = re.compile(
    r"breaking[ _-]?change|\bbreaking\b|migration required|manual (action|intervention)"
    r"|action required|backup before|not backward", re.I)

_state_lock = threading.Lock()
_update_lock = threading.Lock()


MESSAGES = {
    # key: (English, French)
    "skip.self": ("updock itself", "updock lui-même"),
    "skip.disabled": ("disabled (label updock.enable=false)",
                      "désactivé (label updock.enable=false)"),
    "skip.local": ("built locally", "image construite localement"),
    "skip.pinned": ("pinned by digest: update it in your compose file",
                    "épinglée par digest : se met à jour via le fichier compose"),
    "skip.stale": ("running on an image that no longer exists: recreate it",
                   "tourne sur une image qui n'existe plus : à recréer"),
    "skip.deleted": ("original image deleted: recreate it",
                     "image d'origine supprimée : à recréer"),
    "skip.error": ("unreadable", "illisible"),
    "kind.major": ("MAJOR version", "version MAJEURE"),
    "kind.minor": ("new version", "nouvelle version"),
    "kind.patch": ("patch", "correctif"),
    "kind.rebuild": ("rebuild", "reconstruction"),
    "kind.unknown": ("new build", "nouvelle build"),
    "notif.line": ("{kind} ({ref}).", "{kind} ({ref})."),
    "notif.from_github": ("Version inferred from the GitHub releases (image build date).",
                          "Version déduite des releases GitHub (date de build de l'image)."),
    "notif.breaking": ("⚠️ Possible breaking changes: read the notes FIRST.",
                       "⚠️ Changements cassants possibles : lire les notes AVANT."),
    "btn.update": ("Update", "Mettre à jour"),
    "btn.notes": ("Notes", "Notes"),
    "btn.ignore": ("Ignore", "Ignorer"),
    "btn.rollback": ("Roll back", "Revenir en arrière"),
    "job.update": ("update", "mise à jour"),
    "job.rollback": ("rollback", "retour arrière"),
    "job.recreate": ("recreation", "recréation"),
    "job.pulling": ("pulling the image…", "téléchargement de l'image…"),
    "job.restarting": ("restarting…", "redémarrage…"),
    "job.restoring": ("restoring the previous image…", "restauration de l'image précédente…"),
    "job.back_to": ("back to {version}", "revenu à {version}"),
    "job.recreating": ("recreating the container…", "recréation du conteneur…"),
    "job.recreated": ("container recreated", "conteneur recréé"),
    "err.impossible": ("{name}: {action} not possible", "{name} : {action} impossible"),
    "err.not_compose": ("container not found or not managed by docker compose",
                        "conteneur introuvable ou non géré par docker compose"),
    "err.files_hidden": ("compose files not visible from updock: {files}",
                         "fichiers compose invisibles depuis updock : {files}"),
    "err.rate_check": ("registry rate limit reached, will retry at the next check",
                       "limite de requêtes du registre atteinte, nouvel essai à la prochaine vérification"),
    "err.429": ("registry download limit reached (Docker Hub): try again in a few hours",
                "limite de téléchargement du registre atteinte (Docker Hub) : réessaie dans quelques heures"),
    "err.disk": ("disk full: free some space and try again",
                 "disque plein : libère de la place puis réessaie"),
    "err.not_found": ("image not found on the registry", "image introuvable sur le registre"),
    "err.port": ("port already used by another container",
                 "port déjà utilisé par un autre conteneur"),
    "err.compose": ("docker compose failed", "docker compose a échoué"),
    "err.unhealthy": ("the container does not start properly after the update",
                      "le conteneur ne démarre pas correctement après la mise à jour"),
    "err.no_previous": ("no previous image known", "aucune image précédente connue"),
    "err.no_restart": ("the container does not restart", "le conteneur ne redémarre pas"),
    "err.no_start": ("the container does not start", "le conteneur ne démarre pas"),
    "updated.title": ("{name} updated ✅", "{name} mis à jour ✅"),
    "updated.body": ("{old} → {new}, the container is running.",
                     "{old} → {new}, le conteneur tourne."),
    "failed.title": ("{name}: update failed ❌", "{name} : échec de la mise à jour ❌"),
    "rolledback.title": ("{name}: back to the previous version ✅",
                         "{name} : retour à l'ancienne version ✅"),
    "rolledback.body": ("Previous image restored ({version}). This release stays ignored until the next one.",
                        "Image précédente restaurée ({version}). La release reste ignorée jusqu'à la suivante."),
    "rollback_failed.title": ("{name}: rollback failed ❌", "{name} : retour arrière en échec ❌"),
    "reason.pending": ("not checked yet", "pas encore vérifié"),
    "reason.not_in_registry": ("not found on the registry (local image?)",
                               "introuvable sur le registre (image locale ?)"),
    "http.password": ("password required", "mot de passe requis"),
    "api.header": ("missing X-Updock header", "en-tête X-Updock manquant"),
    "api.checking": ("a check is already running", "vérification déjà en cours"),
    "api.unknown": ("unknown action", "action inconnue"),
    "api.nothing": ("nothing to ignore", "rien à ignorer"),
    "api.busy": ("an action is already running", "une action est déjà en cours"),
    "api.bad_schedule": ("Invalid frequency. Examples: 03:30 · sun 03:30 · every 6h (1, 2, 3, 4, 6, 8 or 12 h).",
                         "Fréquence invalide. Exemples : 03:30 · dim 03:30 · every 6h (1, 2, 3, 4, 6, 8 ou 12 h)."),
    "link.invalid": ("invalid, expired or already used link",
                     "lien invalide, expiré ou déjà utilisé"),
    "link.update": ("update of {name} started", "mise à jour de {name} lancée"),
    "link.rollback": ("rollback of {name} started", "retour arrière de {name} lancé"),
    "link.ignored": ("{name}: version ignored", "{name} : version ignorée"),
    "link.use_buttons": ("Use the notification buttons (POST).",
                         "Utiliser les boutons de la notification (POST)."),
}


def _(key, **kw):
    """Message in the configured language (UPDOCK_LANG: en or fr)."""
    text = MESSAGES[key][1 if LANG == "fr" else 0]
    return text.format(**kw) if kw else text


def log(msg):
    print(f"{datetime.now():%Y-%m-%d %H:%M:%S} {msg}", flush=True)


# --- state -------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE) as f:
            state = json.load(f)
    except (OSError, ValueError):
        state = {}
    for key in ("notified", "ignored", "used", "rollback"):
        state.setdefault(key, {})
    return state


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=1, sort_keys=True)
    os.replace(tmp, STATE_FILE)


# --- Docker Engine API (unix socket) ------------------------------------------

class _UnixConnection(http.client.HTTPConnection):
    def __init__(self):
        super().__init__("localhost", timeout=60)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(60)
        self.sock.connect(DOCKER_SOCK)


def docker_get(path):
    conn = _UnixConnection()
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        if resp.status >= 300:
            raise RuntimeError(f"docker {path}: HTTP {resp.status}")
        return json.loads(body)
    finally:
        conn.close()


def docker_cli(*args, cwd=None, timeout=900):
    """Run the docker CLI; return (ok, combined output tail).

    ``cwd`` is also exported as PWD: the root compose file mounts ``${PWD}``,
    which must resolve to the stack directory as it does on the host."""
    env = dict(os.environ, PWD=cwd) if cwd else None
    proc = subprocess.run(["docker", *args], capture_output=True, text=True,
                          timeout=timeout, cwd=cwd, env=env)
    out = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, out[-1500:]


# --- image references and registries -------------------------------------------

def parse_ref(ref):
    """Split an image reference into (registry, repository, tag, digest)."""
    digest = None
    if "@" in ref:
        ref, digest = ref.split("@", 1)
    name, tag = ref, "latest"
    last = ref.rsplit("/", 1)[-1]
    if ":" in last:
        name, tag = ref.rsplit(":", 1)
    first = name.split("/", 1)[0]
    if "/" in name and ("." in first or ":" in first or first == "localhost"):
        registry, repo = name.split("/", 1)
    else:
        registry, repo = "docker.io", name
    if registry == "docker.io" and "/" not in repo:
        repo = "library/" + repo
    return registry, repo, tag, digest


def short_name(registry, repo):
    """Human name used for overrides: 'jellyfin/jellyfin', 'ghcr.io/x/y'."""
    if registry == "docker.io":
        return repo[len("library/"):] if repo.startswith("library/") else repo
    return f"{registry}/{repo}"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def http_req(url, headers=None, data=None, method=None, timeout=30):
    """HTTP call returning (status, headers, body). Redirects are returned,
    not followed: registries redirect blobs to a CDN that must NOT receive
    the registry's Authorization header."""
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method=method)
    try:
        with _opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


class Registry:
    MANIFEST_TYPES = ", ".join([
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    ])

    def __init__(self, registry, repo):
        self.host = "registry-1.docker.io" if registry == "docker.io" else registry
        self.repo = repo
        self.token = None

    def _get(self, path, accept=None):
        headers = {"Accept": accept} if accept else {}
        url = f"https://{self.host}/v2/{self.repo}/{path}"
        for _ in range(2):
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            status, hdrs, body = http_req(url, headers)
            if status == 401 and not self.token:
                self._authenticate(hdrs.get("WWW-Authenticate", ""))
                continue
            if status in (301, 302, 307, 308):
                status, hdrs, body = http_req(hdrs["Location"])
            if status != 200:
                raise RuntimeError(f"{self.host}/{self.repo}/{path}: HTTP {status}")
            return hdrs, body
        raise RuntimeError(f"{self.host}/{self.repo}: authentication failed")

    def _authenticate(self, challenge):
        params = dict(re.findall(r'(\w+)="([^"]*)"', challenge))
        if "realm" not in params:
            raise RuntimeError(f"{self.host}: unsupported auth challenge")
        query = {"scope": f"repository:{self.repo}:pull"}
        if "service" in params:
            query["service"] = params["service"]
        status, _, body = http_req(
            params["realm"] + "?" + urllib.parse.urlencode(query))
        if status != 200:
            raise RuntimeError(f"{self.host}: token HTTP {status}")
        data = json.loads(body)
        self.token = data.get("token") or data.get("access_token")

    def head_digest(self, tag):
        """Digest the tag points to. A HEAD request does not count against
        Docker Hub's anonymous pull quota, unlike a manifest GET."""
        url = f"https://{self.host}/v2/{self.repo}/manifests/{tag}"
        for _ in range(2):
            headers = {"Accept": self.MANIFEST_TYPES}
            if self.token:
                headers["Authorization"] = f"Bearer {self.token}"
            status, hdrs, _ = http_req(url, headers, method="HEAD")
            if status == 401 and not self.token:
                self._authenticate(hdrs.get("WWW-Authenticate", ""))
                continue
            return hdrs.get("Docker-Content-Digest") if status == 200 else None
        return None

    def remote_image(self, tag, arch, os_name="linux"):
        """(digest of the tag, image config) for our platform."""
        hdrs, body = self._get(f"manifests/{tag}", self.MANIFEST_TYPES)
        digest = hdrs.get("Docker-Content-Digest") or \
            "sha256:" + hashlib.sha256(body).hexdigest()
        manifest = json.loads(body)
        if "manifests" in manifest:  # multi-arch index
            match = [m for m in manifest["manifests"]
                     if m.get("platform", {}).get("architecture") == arch
                     and m.get("platform", {}).get("os") == os_name]
            if not match:
                raise RuntimeError(f"no {os_name}/{arch} image for {tag}")
            _, body = self._get(f"manifests/{match[0]['digest']}",
                                self.MANIFEST_TYPES)
            manifest = json.loads(body)
        _, blob = self._get(f"blobs/{manifest['config']['digest']}")
        return digest, json.loads(blob)


# --- versions -------------------------------------------------------------------

LS_SUFFIX = re.compile(r"-ls\d+$")


def _cfg(config):
    return config.get("config") or config.get("Config") or {}


def env_version(config, repo):
    """``<IMAGE>_VERSION`` from the environment: official images (postgres,
    valkey...) set it to the exact version they ship."""
    env = dict(e.split("=", 1) for e in _cfg(config).get("Env") or [] if "=" in e)
    base = repo.rsplit("/", 1)[-1]
    key = ENV_VERSION_KEYS.get(base, base.upper().replace("-", "_") + "_VERSION")
    return env.get(key, "").strip() or None


def label_version(config):
    """The OCI version label -- NOT trustworthy on its own: some images put
    their base image's version there (ollama says 24.04, i.e. Ubuntu)."""
    version = (_cfg(config).get("Labels") or {}).get(
        "org.opencontainers.image.version", "").strip()
    if version.lower() in ("", "latest", "main", "master", "dev"):
        return None
    return version


def version_of(config, repo):
    """Quick version, without GitHub (status page, update messages)."""
    return env_version(config, repo) or label_version(config)


def _stable(releases):
    return [r for r in releases if not r.get("prerelease")
            and not is_prerelease(r.get("tag_name"))]


def plausible_label(label, releases):
    """A label missing from the release list is still trusted when it is
    the NEXT version of the project: same major as its newest stable
    release, and higher. That is a release published after the list was
    fetched (or not published on GitHub yet). A base image's version
    (Ollama's "24.04" next to v0.x releases) fails the major check."""
    stable = _stable(releases)
    if not label or not stable or is_prerelease(label):
        return False
    newest = max(stable, key=lambda r: numbers(r.get("tag_name")))
    a, b = numbers(label), numbers(newest.get("tag_name"))
    return bool(a and b) and a[0] == b[0] and a > b


def resolve_version(config, repo, releases, created, refresh=None):
    """(version, how) for an image, cross-checked against GitHub releases.

    1. env var -- exact, set by the image itself;
    2. label, if it matches a real release tag of the project (re-read the
       release list once if it does not: it may predate the release), or
       if it is plausibly the project's next version;
    3. the newest stable release whose COMMIT predates the image build.
       GitHub's ``created_at`` is the date of the release's commit, while
       ``published_at`` is often later than the image: projects build their
       images first and publish the release afterwards;
    4. label unverified, only when the project's releases are unknown.
    """
    version = env_version(config, repo)
    if version:
        return version, "env"
    label = label_version(config)
    if not releases:
        return label, "label"
    if label and not find_release(releases, label) and refresh:
        releases = refresh() or releases
    if label and (find_release(releases, label) or plausible_label(label, releases)):
        return label, "label"
    built = (created or "")[:19]
    when = lambda r: (r.get("created_at") or r.get("published_at") or "")[:19]
    for rel in sorted(_stable(releases), key=when, reverse=True):
        if built and when(rel) <= built:
            return rel["tag_name"], "github"
    return None, "unknown"


def upstream(version):
    """Version without a linuxserver.io packaging suffix."""
    return LS_SUFFIX.sub("", version or "")


def numbers(version):
    return [int(n) for n in re.findall(r"\d+", norm_tag(version))]


def is_prerelease(version):
    return bool(re.search(r"(alpha|beta|rc|pre|dev|nightly)", version or "", re.I))


def bump_kind(old, new):
    """'major' | 'minor' | 'patch' | 'rebuild' | 'downgrade'."""
    if norm_tag(old) == norm_tag(new):
        return "rebuild"
    a, b = numbers(old), numbers(new)
    if not a or not b:
        return "patch" if old != new else "rebuild"
    if b < a:
        return "downgrade"
    for i, (x, y) in enumerate(zip(a, b)):
        if x != y:
            return ("major", "minor")[min(i, 1)] if i < 2 else "patch"
    return "patch"


# --- GitHub ----------------------------------------------------------------------

_gh_cache = {}


def github_repo(labels, image_labels, registry, repo):
    if labels.get("updock.github"):
        return labels["updock.github"]
    name = short_name(registry, repo)
    if name in GITHUB_OVERRIDES:
        return GITHUB_OVERRIDES[name]
    source = (image_labels or {}).get("org.opencontainers.image.source", "")
    m = re.match(r"https?://github\.com/([^/]+/[^/#?]+)", source)
    return m.group(1).removesuffix(".git") if m else None


def github_releases(gh_repo, max_age=None):
    """Recent releases, cached 6 h (1 h after a failure): unauthenticated
    GitHub allows 60 requests an hour. ``max_age`` (seconds) asks for a
    fresher list -- used when an image announces a version the cached list
    does not know yet."""
    cached = _gh_cache.get(gh_repo)
    if cached and time.time() < cached[0] and \
            (max_age is None or time.time() - cached[2] < max_age):
        return cached[1]
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "updock"}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    status, _, body = http_req(
        f"https://api.github.com/repos/{gh_repo}/releases?per_page=30", headers)
    releases = json.loads(body) if status == 200 else []
    if status != 200:
        log(f"github {gh_repo}: HTTP {status}")
    releases = [r for r in releases if not r.get("draft")]
    _gh_cache[gh_repo] = (time.time() + (6 if status == 200 else 1) * 3600,
                          releases, time.time())
    return releases


TAG_PREFIX = re.compile(r"^(release|version|rel)[-_]?|^[vV](?=\d)", re.I)


def norm_tag(tag):
    """'v1.2.3', 'release-1.2.3', '1.2.3-ls42' -> '1.2.3'."""
    return TAG_PREFIX.sub("", upstream((tag or "").strip()))


def find_release(releases, version):
    """The release whose tag matches this version, if any (an exact tag
    first: linuxserver.io publishes one release per -lsNNN build)."""
    for rel in releases:
        if rel.get("tag_name") == version:
            return rel
    want = norm_tag(version)
    for rel in releases:
        if norm_tag(rel.get("tag_name")) == want:
            return rel
    return None


def latest_release(releases):
    stable = [r for r in releases if not r.get("prerelease")
              and not is_prerelease(r.get("tag_name"))]
    return stable[0] if stable else None


def notes_excerpt(body, limit=700):
    """Release notes as plain text, trimmed for a phone notification."""
    text = re.sub(r"<!--.*?-->", "", body or "", flags=re.S)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)            # images
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)         # links
    text = re.sub(r"^#+\s*", "", text, flags=re.M)               # headings
    text = re.sub(r"\*\*|__|`", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text if len(text) <= limit else text[:limit].rsplit("\n", 1)[0] + "\n…"


# --- signed action links ------------------------------------------------------------

def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def make_token(action, container, extra=""):
    payload = json.dumps({"a": action, "c": container, "x": extra,
                          "e": int(time.time()) + TOKEN_TTL_DAYS * 86400,
                          "n": secrets.token_hex(8)}, separators=(",", ":"))
    sig = hmac.new(SECRET, payload.encode(), hashlib.sha256).digest()
    return _b64(payload.encode()) + "." + _b64(sig)


def check_token(token, state):
    """Decoded payload of a valid, unexpired, unused token -- else None."""
    try:
        raw, sig = token.split(".", 1)
        payload = _unb64(raw)
        good = hmac.new(SECRET, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(good, _unb64(sig)):
            return None
        data = json.loads(payload)
    except (ValueError, TypeError):
        return None
    if data["e"] < time.time() or data["n"] in state["used"]:
        return None
    return data


def action_url(action, container, extra=""):
    return f"{PUBLIC_URL}/{action}/{make_token(action, container, extra)}"


# --- ntfy ---------------------------------------------------------------------------

def notify(title, message, tags=(), priority=3, actions=(), click=None):
    if not NTFY_TOPIC:
        log(f"(ntfy disabled) {title}: {message}")
        return
    payload = {"topic": NTFY_TOPIC, "title": title, "message": message,
               "tags": list(tags), "priority": priority,
               "actions": list(actions)}
    if click:
        payload["click"] = click
    headers = {"Content-Type": "application/json"}
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
    status, _, body = http_req(NTFY_URL, headers,
                               json.dumps(payload).encode(), "POST")
    if status >= 300:
        log(f"ntfy: HTTP {status} {body[:200]!r}")


def http_button(label, url):
    return {"action": "http", "label": label, "url": url, "method": "POST",
            "clear": True}


# --- containers ---------------------------------------------------------------------

SKIP_CODES = ("self", "disabled", "local", "pinned", "stale", "deleted", "error")
SELF_ID = socket.gethostname()  # Docker sets it to our short container id


def inspect_containers():
    """Every running container, each with ``skip`` = None (watched) or a
    reason code. One unreadable container never sinks the others."""
    entries = []
    for summary in docker_get("/containers/json"):
        try:
            entries.append(inspect_one(summary["Id"]))
        except (RuntimeError, KeyError, ValueError) as exc:
            labels = summary.get("Labels") or {}
            entries.append({
                "name": (summary.get("Names") or ["/?"])[0].lstrip("/"),
                "ref": summary.get("Image", ""), "skip": "error",
                "error": str(exc), "labels": labels, "compose": None,
                "project": labels.get("com.docker.compose.project", ""),
                "version": None})
    return entries


def inspect_one(container_id):
    info = docker_get(f"/containers/{container_id}/json")
    labels = info["Config"].get("Labels") or {}
    ref = info["Config"]["Image"]
    registry, repo, tag, pinned = parse_ref(ref)
    entry = {"name": info["Name"].lstrip("/"), "ref": ref, "labels": labels,
             "compose": compose_args(labels),
             "project": labels.get("com.docker.compose.project", ""),
             "registry": registry, "repo": repo, "tag": tag, "pinned": pinned,
             "image_id": info["Image"], "skip": None, "version": None}
    if info["Id"].startswith(SELF_ID):
        entry["skip"] = "self"
        return entry
    if labels.get("updock.enable", "").lower() == "false":
        entry["skip"] = "disabled"
        return entry
    try:
        image = docker_get(f"/images/{info['Image']}/json")
    except RuntimeError:
        # The container's image is gone (rebuilt, re-pulled, pruned). If
        # the name still resolves, a newer image is waiting: recreate.
        try:
            docker_get(f"/images/{ref}/json")
            entry["skip"] = "stale"
        except RuntimeError:
            entry["skip"] = "deleted"
        return entry
    digests = [d.split("@", 1)[1] for d in image.get("RepoDigests") or []]
    entry.update({
        "digests": digests, "created": image.get("Created", ""),
        "arch": image.get("Architecture", "amd64"),
        "version": version_of(image, repo),
        "config": {"config": image.get("Config") or {}},
        "image_labels": (image.get("Config") or {}).get("Labels") or {},
    })
    # Built by compose (`build:` without `image:`): the image is named
    # <project>-<service>. With Docker's containerd image store even such a
    # local image gets a "RepoDigest", so that alone does not tell.
    built = f"{entry['project']}-{labels.get('com.docker.compose.service', '')}"
    if pinned:
        entry["skip"] = "pinned"
    elif not digests or ref.split(":")[0] == built:
        entry["skip"] = "local"
    return entry


def find_entry(name):
    for entry in inspect_containers():
        if entry["name"] == name:
            return entry
    return None


def compose_args(labels):
    """`docker compose` arguments that address the container's own project."""
    project = labels.get("com.docker.compose.project")
    workdir = labels.get("com.docker.compose.project.working_dir")
    files = labels.get("com.docker.compose.project.config_files")
    service = labels.get("com.docker.compose.service")
    if not (project and workdir and files and service):
        return None
    args = ["compose", "-p", project, "--project-directory", workdir]
    for f in files.split(","):
        args += ["-f", f]
    env_file = labels.get("com.docker.compose.project.environment_file")
    if env_file:
        for f in env_file.split(","):
            args += ["--env-file", f]
    return {"args": args, "service": service, "files": files.split(","),
            "workdir": workdir}


# --- the check ---------------------------------------------------------------------

class NotInRegistry(Exception):
    pass


def release_url(gh, release):
    if release:
        return release.get("html_url")
    return f"https://github.com/{gh}/releases" if gh else None


def _refresher(gh):
    """Re-read a repo's releases if the cached list is over 15 min old."""
    return (lambda: github_releases(gh, max_age=900)) if gh else None


def current_result(entry, gh):
    """Result for a container already on the newest image of its tag."""
    releases = []
    if gh and not env_version(entry["config"], entry["repo"]):
        releases = github_releases(gh)  # to verify (or replace) the label
    version, _ = resolve_version(entry["config"], entry["repo"], releases,
                                 entry["created"], _refresher(gh))
    release = find_release(releases, version) if version and releases else None
    return {"status": "current", "current": version, "latest": version,
            "github": gh, "url": release_url(gh, release)}


def assess(entry, remote_digest, remote_config, gh):
    """Result for a container whose tag now points elsewhere."""
    releases = github_releases(gh) if gh else []
    old, how_old = resolve_version(entry["config"], entry["repo"], releases,
                                   entry["created"], _refresher(gh))
    new, how_new = resolve_version(remote_config, entry["repo"], releases,
                                   remote_config.get("created"), _refresher(gh))
    if gh and how_new in ("github", "unknown") and (not new or norm_tag(new) == norm_tag(old)):
        # A new image that looks like a rebuild is often a release the cached
        # list (6 h) does not know yet: projects push the image, then publish
        # the release. Re-read the list once before calling it a rebuild.
        fresh = github_releases(gh, max_age=900)
        if fresh is not releases:
            new, how_new = resolve_version(remote_config, entry["repo"], fresh,
                                           remote_config.get("created"))
    releases = github_releases(gh) if gh else []  # possibly refreshed above
    if not new:
        kind = "unknown"
    elif not old:
        kind = "patch"
    else:
        kind = bump_kind(old, new)
    if kind == "downgrade" or (new and is_prerelease(new) and not is_prerelease(old)):
        return {"status": "current", "current": old, "latest": old,
                "github": gh, "url": release_url(gh, None)}
    release = find_release(releases, new) if new else None
    notes = (release or {}).get("body", "")
    return {"status": "rebuild" if kind in ("rebuild", "unknown") else "update",
            "kind": kind, "current": old, "latest": new,
            "source": "github" if "github" in (how_old, how_new) else how_new,
            "github": gh, "url": release_url(gh, release),
            "notes": notes_excerpt(notes), "digest": remote_digest,
            "breaking": kind == "major" or bool(BREAKING.search(notes or ""))}


def check_entry(entry):
    gh = github_repo(entry["labels"], entry["image_labels"], entry["registry"],
                     entry["repo"])
    reg = Registry(entry["registry"], entry["repo"])
    try:
        head = reg.head_digest(entry["tag"])
        if head and head in entry["digests"]:
            return current_result(entry, gh)  # cheapest possible answer
        remote_digest, remote_config = reg.remote_image(entry["tag"], entry["arch"])
    except RuntimeError as exc:
        if re.search(r"HTTP (401|403|404)", str(exc)):
            raise NotInRegistry(str(exc)) from exc
        raise
    if remote_digest in entry["digests"]:
        return current_result(entry, gh)
    return assess(entry, remote_digest, remote_config, gh)


def announce(entry, res):
    name = entry["name"]
    old, new = pretty(res["current"]) or "?", pretty(res["latest"]) or _("kind.unknown")
    title = f"{name}: {old} → {new}"
    kind = _("kind." + res["kind"])
    lines = [_("notif.line", kind=kind[:1].upper() + kind[1:], ref=entry["ref"])]
    if res.get("source") == "github":
        lines.append(_("notif.from_github"))
    if res["breaking"]:
        lines.append(_("notif.breaking"))
    if res.get("notes"):
        lines += ["", res["notes"]]
    actions = []
    if entry["compose"]:
        actions.append(http_button(_("btn.update"), action_url("update", name)))
    if res.get("url"):
        actions.append({"action": "view", "label": _("btn.notes"), "url": res["url"]})
    actions.append(http_button(_("btn.ignore"), action_url("ignore", name, res["digest"])))
    priority = 4 if res["breaking"] else 3
    tags = ["warning"] if res["breaking"] else ["package"]
    notify(title, "\n".join(lines), tags, priority, actions, res.get("url"))


_check_lock = threading.Lock()


def check_once():
    """Check every watched container; store the results for the interface
    and notify new releases. A second call while one runs is a no-op."""
    if not _check_lock.acquire(blocking=False):
        return
    try:
        _check()
    finally:
        _check_lock.release()


def _check():
    results, to_announce = {}, []
    previous = load_state().get("results", {})
    for entry in inspect_containers():
        if entry["skip"]:
            continue
        name = entry["name"]
        try:
            res = check_entry(entry)
        except NotInRegistry:
            res = {"status": "local"}
        except Exception as exc:  # one bad registry must not stop the run
            log(f"{name}: {exc}")
            why = str(exc)[:300]
            if "HTTP 429" in why:
                why = _("err.rate_check")
            prev = previous.get(name)
            if prev and prev.get("status") not in ("error", None):
                # A transient failure must not erase what we knew.
                results[name] = dict(prev, warning=why)
                continue
            res = {"status": "error", "error": why}
        res["checked"] = time.time()
        results[name] = res
        if res["status"] == "update" or (res["status"] == "rebuild" and NOTIFY_REBUILDS):
            to_announce.append((entry, res))
    with _state_lock:
        state = load_state()
        state["results"] = results
        state["last_check"] = time.time()
        fresh = [(e, r) for e, r in to_announce
                 if r["digest"] not in (state["notified"].get(e["name"]),
                                        state["ignored"].get(e["name"]))]
        for entry, res in fresh:
            state["notified"][entry["name"]] = res["digest"]
        save_state(state)
    for entry, res in fresh:
        log(f"{entry['name']}: {res['current']} -> {res['latest']} ({res['kind']})")
        announce(entry, res)
    log(f"check done, {len(fresh)} new release(s)")


# --- update / rollback / recreate ------------------------------------------------------

jobs = {}  # name -> {"action", "state": running|done|failed, "msg", "t"}


def set_job(name, action, state, msg=""):
    jobs[name] = {"action": action, "state": state, "msg": msg, "t": time.time()}


def container_state(name):
    try:
        return docker_get(f"/containers/{name}/json")["State"]
    except RuntimeError:
        return {}  # mid-recreate: the container briefly does not exist


def wait_healthy(name):
    """True once the container runs (and is healthy, if it has a check)."""
    deadline = time.time() + HEALTH_TIMEOUT
    while time.time() < deadline:
        st = container_state(name)
        health = (st.get("Health") or {}).get("Status")
        if st.get("Running") and st.get("RestartCount", 0) == 0 and \
                health in (None, "healthy"):
            time.sleep(10)  # still up 10 s later = not a crash loop
            st = container_state(name)
            if st.get("Running") and st.get("RestartCount", 0) == 0:
                return True
        if st.get("Status") in ("exited", "dead"):
            return False
        time.sleep(5)
    return False


def start_job(target, name, *args):
    """Run an action in the background; refuse a second one on the same
    container. Returns False if one is already running."""
    if jobs.get(name, {}).get("state") in ("queued", "running"):
        return False
    set_job(name, target.__name__.removeprefix("run_"), "queued")
    threading.Thread(target=target, args=(name, *args), daemon=True).start()
    return True


def _compose_ready(name, action, entry, notify_failure):
    """compose args for ``entry``, or None after reporting why not."""
    why = None
    if not entry or not entry.get("compose"):
        why = _("err.not_compose")
    else:
        missing = [f for f in entry["compose"]["files"] if not os.path.exists(f)]
        if missing:
            why = _("err.files_hidden", files=", ".join(missing))
    if not why:
        return entry["compose"]
    set_job(name, action, "failed", why)
    if notify_failure:
        notify(_("err.impossible", name=name, action=_("job." + action)), why, ["x"], 4)
    return None


def run_update(name, from_ntfy=False):
    with _update_lock:
        set_job(name, "update", "running", _("job.pulling"))
        entry = find_entry(name)
        c = _compose_ready(name, "update", entry, from_ntfy)
        if not c:
            return
        old_version, old_image = entry["version"], entry["image_id"]
        log(f"{name}: updating ({entry['ref']})")
        ok, out = docker_cli(*c["args"], "pull", c["service"], cwd=c["workdir"])
        if ok:
            set_job(name, "update", "running", _("job.restarting"))
            ok, out = docker_cli(*c["args"], "up", "-d", "--no-deps",
                                 c["service"], cwd=c["workdir"])
        healthy = ok and wait_healthy(name)
        now = find_entry(name) or {}
        new_version = pretty(now.get("version")) or "?"
        with _state_lock:
            state = load_state()
            if ok:  # the container was recreated: the old image is a way back
                state["rollback"][name] = {"image": old_image, "ref": entry["ref"],
                                           "version": old_version}
            if healthy:
                res = state.get("results", {}).get(name, {})
                state.setdefault("results", {})[name] = {
                    "status": "current", "current": now.get("version"),
                    "latest": now.get("version"), "github": res.get("github"),
                    "url": res.get("url"), "checked": time.time()}
            save_state(state)
        rollback_btn = [http_button(_("btn.rollback"),
                                    action_url("rollback", name, old_image))] if ok else []
        if healthy:
            log(f"{name}: updated to {new_version}")
            set_job(name, "update", "done", f"{pretty(old_version) or '?'} → {new_version}")
            if from_ntfy:
                notify(_("updated.title", name=name),
                       _("updated.body", old=pretty(old_version) or "?", new=new_version),
                       ["white_check_mark"], 3, rollback_btn)
            return
        log(f"{name}: update failed\n{out}")
        why = _("err.unhealthy") if ok else friendly_error(out)
        set_job(name, "update", "failed", why)
        notify(_("failed.title", name=name), why, ["x"], 5, rollback_btn)


def friendly_error(out):
    """The useful part of a failed docker command, in plain words."""
    low = out.lower()
    if "429" in out or "too many requests" in low:
        return _("err.429")
    if "no space left" in low:
        return _("err.disk")
    if "manifest unknown" in low or "not found" in low:
        return _("err.not_found")
    if "port is already allocated" in low:
        return _("err.port")
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    return (lines[-1] if lines else _("err.compose"))[:200]


def run_rollback(name, old_image=None, from_ntfy=False):
    with _update_lock:
        set_job(name, "rollback", "running", _("job.restoring"))
        state = load_state()
        saved = state["rollback"].get(name, {})
        old_image = old_image or saved.get("image")
        entry = find_entry(name)
        c = _compose_ready(name, "rollback", entry, from_ntfy)
        if not c:
            return
        if not old_image:
            set_job(name, "rollback", "failed", _("err.no_previous"))
            return
        ok, out = docker_cli("tag", old_image, entry["ref"])
        if ok:
            ok, out = docker_cli(*c["args"], "up", "-d", "--no-deps", "--pull",
                                 "never", c["service"], cwd=c["workdir"])
        if ok and wait_healthy(name):
            version = pretty(saved.get("version")) or old_image[:19]
            set_job(name, "rollback", "done", _("job.back_to", version=version))
            with _state_lock:
                state = load_state()
                state["ignored"][name] = state["notified"].get(name, "")
                state["rollback"].pop(name, None)
                state.get("results", {}).pop(name, None)  # re-checked next run
                save_state(state)
            if from_ntfy:
                notify(_("rolledback.title", name=name),
                       _("rolledback.body", version=version), ["leftwards_arrow_with_hook"], 3)
        else:
            why = friendly_error(out) if not ok else _("err.no_restart")
            set_job(name, "rollback", "failed", why)
            notify(_("rollback_failed.title", name=name), why, ["x"], 5)


def run_recreate(name):
    """Recreate a container whose image is gone or was replaced."""
    with _update_lock:
        set_job(name, "recreate", "running", _("job.recreating"))
        c = _compose_ready(name, "recreate", find_entry(name), False)
        if not c:
            return
        ok, out = docker_cli(*c["args"], "up", "-d", "--no-deps",
                             "--force-recreate", c["service"], cwd=c["workdir"])
        if ok and wait_healthy(name):
            set_job(name, "recreate", "done", _("job.recreated"))
        else:
            set_job(name, "recreate", "failed",
                    friendly_error(out) if not ok else _("err.no_start"))


# --- web interface + API -----------------------------------------------------------------

UI_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui.html")


def pretty(version):
    """Display form: '4.0.20.3014-ls329' -> '4.0.20.3014',
    '16.15-1.pgdg120+1' -> '16.15', '3.6.4+dfsg-2' -> '3.6.4'."""
    if not version:
        return None
    m = re.match(r"v?\d+(?:\.\d+)*", upstream(version))
    return m.group(0) if m else version


def row(entry, res, state):
    """What the interface needs about one container."""
    name = entry["name"]
    job = jobs.get(name)
    if job and job["state"] in ("done", "failed") and time.time() - job["t"] > 900:
        job = None
    out = {"name": name, "project": entry.get("project") or "—",
           "ref": entry["ref"], "current": pretty(entry.get("version")),
           "latest": None, "kind": None, "breaking": False, "notes": "",
           "url": None, "reason": "", "job": job,
           "can_rollback": name in state["rollback"] and bool(entry.get("compose")),
           "rollback_to": pretty(state["rollback"].get(name, {}).get("version")),
           "checked": res.get("checked") if res else None,
           "warning": (res or {}).get("warning", "")}
    if entry["skip"] in ("stale", "deleted"):
        out.update(status="recreate", reason=_("skip." + entry["skip"]))
    elif entry["skip"]:
        out.update(status="other", reason=_("skip." + entry["skip"]))
        if entry["skip"] == "error":
            out["reason"] += f" : {entry.get('error', '')}"
    elif not res:
        out.update(status="pending", reason=_("reason.pending"))
    elif res["status"] == "local":
        out.update(status="other", reason=_("reason.not_in_registry"))
    elif res["status"] == "error":
        out.update(status="error", reason=res.get("error", ""))
    else:
        out.update(current=pretty(res.get("current")) or out["current"],
                   latest=pretty(res.get("latest")), url=res.get("url"),
                   status=res["status"])
        if out["latest"] and out["current"] and \
                norm_tag(out["latest"]) == norm_tag(out["current"]):
            out["latest"] = out["current"]  # "1.4.0" vs "v1.4.0"
        if res["status"] in ("update", "rebuild"):
            if res.get("digest") in entry.get("digests", []):
                out.update(status="current", latest=out["current"])  # done since
            else:
                out.update(kind=res.get("kind"), breaking=res.get("breaking", False),
                           notes=res.get("notes", ""))
                if state["ignored"].get(name) == res.get("digest"):
                    out["status"] = "ignored"
    out["can_update"] = bool(entry.get("compose")) and \
        out["status"] in ("update", "rebuild", "ignored")
    out["can_recreate"] = bool(entry.get("compose")) and out["status"] == "recreate"
    return out


def api_state():
    state = load_state()
    results = state.get("results", {})
    rows = [row(e, results.get(e["name"]), state) for e in inspect_containers()]
    schedule = current_schedule(state)
    try:
        nxt = round((time.time() + seconds_until_next(parse_schedule(schedule),
                                                       datetime.now())) / 60) * 60
    except ValueError:
        nxt = None
    return {"schedule": schedule, "schedule_default": CHECK_AT,
            "last_check": state.get("last_check"),
            "next_check": nxt, "checking": _check_lock.locked(),
            "containers": sorted(rows, key=lambda r: (r["project"], r["name"]))}


class Handler(BaseHTTPRequestHandler):
    server_version = "updock"

    def log_message(self, fmt, *args):
        pass

    def _reply(self, code, body, ctype="text/plain; charset=utf-8", headers=()):
        if not isinstance(body, bytes):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in headers:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, data):
        self._reply(code, json.dumps(data), "application/json")

    def _authorized(self):
        """Optional password (UPDOCK_UI_PASSWORD), HTTP Basic."""
        if not UI_PASSWORD:
            return True
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Basic "):
            try:
                pwd = base64.b64decode(auth[6:]).decode().split(":", 1)[1]
                if hmac.compare_digest(pwd.encode(), UI_PASSWORD.encode()):
                    return True
            except (ValueError, IndexError, UnicodeDecodeError):
                pass
        self._reply(401, _("http.password"),
                    headers=[("WWW-Authenticate", 'Basic realm="updock"')])
        return False

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            return self._reply(200, "ok")
        if path not in ("/", "/api/state"):
            return self._reply(404, "not found")
        if not self._authorized():
            return
        if path == "/":
            with open(UI_FILE, "rb") as f:
                page = f.read().replace(b"__UPDOCK_LANG__", LANG.encode())
            return self._reply(200, page, "text/html; charset=utf-8")
        try:
            self._json(200, api_state())
        except Exception as exc:  # the page must answer, whatever happens
            self._json(500, {"error": str(exc)})

    def do_POST(self):
        parts = self.path.strip("/").split("/")
        if parts[0] == "api":
            return self._api(parts[1:])
        if len(parts) != 2 or parts[0] not in ("update", "ignore", "rollback"):
            return self._reply(404, "not found")
        self._ntfy_button(*parts)

    def _api(self, parts):
        if not self._authorized():
            return
        # A custom header cannot be sent cross-site without a CORS preflight,
        # which this server never grants: a malicious page visited on the
        # LAN cannot trigger an update.
        if self.headers.get("X-Updock") != "1":
            return self._json(403, {"error": _("api.header")})
        action = parts[0] if parts else ""
        if action == "schedule" and len(parts) == 1:
            return self._set_schedule()
        if action == "check" and len(parts) == 1:
            if _check_lock.locked():
                return self._json(409, {"error": _("api.checking")})
            threading.Thread(target=check_once, daemon=True).start()
            return self._json(202, {"ok": True})
        if len(parts) != 2 or action not in ("update", "ignore", "rollback", "recreate"):
            return self._json(404, {"error": _("api.unknown")})
        name = urllib.parse.unquote(parts[1])
        if action == "ignore":
            with _state_lock:
                state = load_state()
                res = state.get("results", {}).get(name)
                if not res or not res.get("digest"):
                    return self._json(404, {"error": _("api.nothing")})
                state["ignored"][name] = res["digest"]
                save_state(state)
            return self._json(200, {"ok": True})
        target = {"update": run_update, "rollback": run_rollback,
                  "recreate": run_recreate}[action]
        if not start_job(target, name):
            return self._json(409, {"error": _("api.busy")})
        return self._json(202, {"ok": True})

    def _set_schedule(self):
        """{"schedule": "03:30"} -- empty string = back to UPDOCK_CHECK_AT."""
        try:
            size = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(min(size, 2048)) or b"{}")
            spec = body.get("schedule", "") if isinstance(body, dict) else ""
            spec = ", ".join(p.strip() for p in str(spec).split(",") if p.strip())
            if spec:
                parse_schedule(spec)
        except ValueError as exc:  # also covers bad JSON
            log(f"rejected schedule: {exc}")
            return self._json(400, {"error": _("api.bad_schedule")})
        with _state_lock:
            state = load_state()
            if spec and spec != CHECK_AT:
                state["schedule"] = spec
            else:
                state.pop("schedule", None)
            save_state(state)
        _schedule_changed.set()
        log(f"schedule set to {current_schedule()}")
        return self._json(200, {"ok": True, "schedule": current_schedule()})

    def _ntfy_button(self, action, token):
        with _state_lock:
            state = load_state()
            data = check_token(token, state)
            if not data or data["a"] != action:
                return self._reply(403, _("link.invalid"))
            state["used"][data["n"]] = data["e"]
            now = time.time()
            state["used"] = {n: e for n, e in state["used"].items() if e > now}
            if data["a"] == "ignore":
                state["ignored"][data["c"]] = data["x"]
            save_state(state)
        name = data["c"]
        if action == "update":
            start_job(run_update, name, True)
            return self._reply(202, _("link.update", name=name))
        if action == "rollback":
            start_job(run_rollback, name, data["x"], True)
            return self._reply(202, _("link.rollback", name=name))
        log(f"{name}: version ignored")
        return self._reply(200, _("link.ignored", name=name))


# --- main -------------------------------------------------------------------------------

def selftest():
    assert parse_ref("jellyfin/jellyfin") == ("docker.io", "jellyfin/jellyfin", "latest", None)
    assert parse_ref("postgres:16-alpine") == ("docker.io", "library/postgres", "16-alpine", None)
    assert parse_ref("lscr.io/linuxserver/sonarr:latest")[:3] == ("lscr.io", "linuxserver/sonarr", "latest")
    assert parse_ref("docker.io/valkey/valkey:9@sha256:ab")[3] == "sha256:ab"
    assert parse_ref("localhost:5000/x:1")[:3] == ("localhost:5000", "x", "1")
    assert bump_kind("4.0.15.2941-ls290", "4.0.20.3014-ls329") == "patch"
    assert bump_kind("4.0.15.2941-ls290", "4.0.15.2941-ls291") == "rebuild"
    assert bump_kind("1.2.3", "1.3.0") == "minor"
    assert bump_kind("v1.9.0", "2.0.0") == "major"
    assert bump_kind("2.0.0", "1.9.0") == "downgrade"
    assert bump_kind("16.4", "16.6") == "minor"
    cfg = {"config": {"Labels": {"org.opencontainers.image.version": "12.2"}}}
    assert version_of(cfg, "jellyfin/jellyfin") == "12.2"
    cfg = {"config": {"Env": ["PG_VERSION=16.6", "PATH=/x"]}}
    assert version_of(cfg, "library/postgres") == "16.6"
    assert version_of({"config": {"Labels": {"org.opencontainers.image.version": "latest"}}}, "a/b") is None
    ollama = {"config": {"Labels": {"org.opencontainers.image.version": "24.04"}}}
    rels = [{"tag_name": "v0.13.0", "published_at": "2026-10-16T10:00:00Z"},
            {"tag_name": "v0.13.1-rc0", "published_at": "2026-10-16T20:00:00Z",
             "prerelease": True},
            {"tag_name": "v0.12.9", "published_at": "2026-10-01T10:00:00Z"}]
    assert resolve_version(ollama, "ollama/ollama", rels, "2026-10-17T00:46:34Z") == ("v0.13.0", "github")
    assert resolve_version(ollama, "ollama/ollama", rels, "2026-10-05T00:00:00Z") == ("v0.12.9", "github")
    assert resolve_version(ollama, "ollama/ollama", [], "x") == ("24.04", "label")
    # A new image while the cached release list predates its release: re-read
    # once, and it is an update, not a rebuild.
    old_list = [{"tag_name": "v0.13.0", "created_at": "2026-08-12T10:00:00Z"}]
    new_list = [{"tag_name": "v0.13.1", "created_at": "2026-10-27T09:00:00Z"}] + old_list
    now = time.time()
    _gh_cache["seanmorley15/AdventureLog"] = (now + 3600, old_list, now - 3600)
    _saved = http_req
    globals()["http_req"] = lambda *a, **k: (200, {}, json.dumps(new_list).encode())
    try:
        entry = {"config": {}, "repo": "seanmorley15/adventurelog", "created": "2026-08-12T12:00:00Z"}
        res = assess(entry, "sha256:new", {"created": "2026-10-27T09:30:00Z"}, "seanmorley15/AdventureLog")
    finally:
        globals()["http_req"] = _saved
        _gh_cache.pop("seanmorley15/AdventureLog", None)
    assert (res["status"], res["current"], res["latest"], res["kind"]) == ("update", "v0.13.0", "v0.13.1", "patch"), res
    good = {"config": {"Labels": {"org.opencontainers.image.version": "0.12.9"}}}
    assert resolve_version(good, "ollama/ollama", rels, "2030")[1] == "label"
    # Image built before the release was published: the commit date counts.
    immich = [{"tag_name": "v3.3.5", "created_at": "2026-10-20T08:00:00Z",
               "published_at": "2026-10-20T14:00:00Z"},
              {"tag_name": "v3.3.4", "created_at": "2026-10-10T08:00:00Z",
               "published_at": "2026-10-10T12:00:00Z"}]
    bare = {"config": {}}
    assert resolve_version(bare, "immich-app/immich-server", immich,
                           "2026-10-20T09:30:00Z")[0] == "v3.3.5"
    assert resolve_version(bare, "immich-app/immich-server", immich,
                           "2026-10-15T00:00:00Z")[0] == "v3.3.4"
    # A label newer than the cached list: refreshed list, else plausibility.
    shelf = {"config": {"Labels": {"org.opencontainers.image.version": "1.5.0"}}}
    old_list = [{"tag_name": "v1.4.0", "created_at": "2026-09-01T00:00:00Z"}]
    assert resolve_version(shelf, "calibrain/shelfmark", old_list, "2026-10-20")[0] == "1.5.0"
    assert resolve_version(shelf, "calibrain/shelfmark", old_list, "2026-10-20",
                           lambda: [{"tag_name": "v1.5.0"}] + old_list) == ("1.5.0", "label")
    assert not plausible_label("24.04", [{"tag_name": "v0.13.0"}])
    assert not plausible_label("2.0.0", [{"tag_name": "v1.9.0"}])
    rels = [{"tag_name": "v2.1.0", "body": "x"}, {"tag_name": "v2.0.0", "body": "y"}]
    assert find_release(rels, "2.0.0")["body"] == "y"
    assert find_release([{"tag_name": "4.0.20.3014-ls329"}], "4.0.20.3014-ls329")
    assert find_release([{"tag_name": "release-1.31.7"}], "1.31.7")
    assert find_release([{"tag_name": "v0.21.0"}], "0.21.0")
    assert norm_tag("version-2.0") == "2.0" and norm_tag("vision") == "vision"
    assert BREAKING.search("## Breaking Changes\n- foo")
    assert "link" in notes_excerpt("## Title\nsee [link](http://x)")
    global SECRET
    SECRET = b"test"
    st = {"used": {}}
    tok = make_token("update", "sonarr")
    assert check_token(tok, st)["c"] == "sonarr"
    assert check_token(tok[:-2] + "AA", st) is None
    assert pretty("4.0.20.3014-ls329") == "4.0.20.3014"
    assert pretty("16.15-1.pgdg120+1") == "16.15"
    assert pretty("3.6.4+dfsg-2") == "3.6.4" and pretty("v1.6.2") == "v1.6.2"
    assert pretty(None) is None and pretty("pg16") == "pg16"
    assert "Docker Hub" in friendly_error("x 429 Too Many Requests")
    assert friendly_error("a\nError: boom\n") == "Error: boom"
    assert parse_schedule("3:30, 15:00") == [(None, 3, 30), (None, 15, 0)]
    night = datetime(2026, 10, 19, 23, 0)  # a Monday
    assert seconds_until_next(parse_schedule("03:30"), night) == 4.5 * 3600
    assert seconds_until_next(parse_schedule("03:30"), datetime(2026, 10, 19, 3, 0)) == 1800
    assert seconds_until_next(parse_schedule("03:30,15:00"), datetime(2026, 10, 19, 4, 0)) == 11 * 3600
    assert [h for _, h, _ in parse_schedule("every 6h")] == [3, 9, 15, 21]
    assert len(parse_schedule("1h")) == 24 and parse_schedule("toutes les 12h")[0] == (None, 3, 30)
    # Sunday 03:30 seen from Monday 23:00: 5 days + 4.5 h
    assert seconds_until_next(parse_schedule("sun 03:30"), night) == 5 * 86400 + 4.5 * 3600
    assert parse_schedule("dim 03:30") == parse_schedule("sun 03:30")
    for bad in ("", "25:00", "every 5h", "xyz 03:30", "noon"):
        try:
            parse_schedule(bad)
            raise AssertionError(bad)
        except ValueError:
            pass
    print("selftest ok")


DAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
        "lun": 0, "mar": 1, "mer": 2, "jeu": 3, "ven": 4, "sam": 5, "dim": 6}
EVERY = re.compile(r"(?:every\s+|toutes\s+les\s+)?(\d{1,2})\s*h", re.I)


def parse_schedule(spec):
    """Check schedule -> sorted [(weekday or None, hour, minute)].

    Comma-separated items, local time:
      03:30          every day at 03:30
      sun 03:30      every Sunday (mon..sun, or lun..dim)
      every 6h       every 6 hours, at 03:30, 09:30, 15:30, 21:30
                     (1, 2, 3, 4, 6, 8 or 12 hours, from 03:30)
    """
    slots = []
    for part in (spec or "").split(","):
        part = part.strip().lower()
        m = EVERY.fullmatch(part)
        if m:
            n = int(m[1])
            if n not in (1, 2, 3, 4, 6, 8, 12):
                raise ValueError(f"schedule: every {n}h does not divide a day "
                                 "(1, 2, 3, 4, 6, 8 or 12)")
            slots += [(None, (3 + k * n) % 24, 30) for k in range(24 // n)]
            continue
        m = re.fullmatch(r"(?:([a-z]{3})\s+)?(\d{1,2}):(\d{2})", part)
        if not m or (m[1] and m[1] not in DAYS) or int(m[2]) > 23 or int(m[3]) > 59:
            raise ValueError(f"schedule: invalid item {part!r} "
                             "(expected HH:MM, 'sun HH:MM' or 'every 6h')")
        slots.append((DAYS[m[1]] if m[1] else None, int(m[2]), int(m[3])))
    if not slots:
        raise ValueError("schedule: empty")
    return sorted(set(slots), key=lambda t: (t[1], t[2], -1 if t[0] is None else t[0]))


def seconds_until_next(slots, now):
    """Seconds from ``now`` (a local datetime) to the next scheduled slot."""
    base = now.replace(hour=0, minute=0, second=0, microsecond=0)
    best = None
    for day in range(8):
        for dow, h, m in slots:
            at = (base + timedelta(days=day)).replace(hour=h, minute=m)
            if (dow is None or at.weekday() == dow) and at > now:
                gap = (at - now).total_seconds()
                best = gap if best is None else min(best, gap)
    return best if best is not None else 86400.0


def current_schedule(state=None):
    """The schedule chosen in the web interface, else UPDOCK_CHECK_AT."""
    state = state if state is not None else load_state()
    return state.get("schedule") or CHECK_AT


_schedule_changed = threading.Event()


def main():
    if "--selftest" in sys.argv:
        return selftest()
    if "--check" in sys.argv:
        return check_once()
    if not SECRET or not PUBLIC_URL:
        sys.exit("UPDOCK_SECRET and UPDOCK_PUBLIC_URL are required")
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    parse_schedule(CHECK_AT)  # fail fast on a bad UPDOCK_CHECK_AT
    log(f"listening on :{PORT}, checks: {current_schedule()} ({time.tzname[0]})")
    # A release already notified is never notified again (state.json), so a
    # restart only rings for what is genuinely new since the last check.
    if CHECK_ON_START:
        try:
            check_once()
        except Exception as exc:
            log(f"check failed: {exc}")
    while True:
        try:
            slots = parse_schedule(current_schedule())
        except ValueError as exc:
            log(f"{exc} -- falling back to UPDOCK_CHECK_AT")
            slots = parse_schedule(CHECK_AT)
        wait = seconds_until_next(slots, datetime.now())
        log(f"next check in {wait / 3600:.1f} h")
        if _schedule_changed.wait(wait):  # changed from the web interface
            _schedule_changed.clear()
            continue
        try:
            check_once()
        except Exception as exc:
            log(f"check failed: {exc}")


if __name__ == "__main__":
    main()
