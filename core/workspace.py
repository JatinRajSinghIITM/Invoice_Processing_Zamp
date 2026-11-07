"""
A Workspace is one visitor's private copy of everything that changes while
the app is used: the audit-trail database, the vendor master, the PO dataset
and any uploaded PDFs.

Why this exists: a deployed Streamlit app is ONE process serving every
visitor. With a single shared database, one visitor's run of a sample invoice
makes the next visitor's first run look like a duplicate, and one visitor's
"Reset" wipes everyone's history. Giving each browser session its own
workspace (seeded from the repo's data/ folder) removes that whole class of
cross-talk without needing real user accounts.

The CLI harness and the test suite use DEFAULT_WORKSPACE (the repo's data/
folder) so they behave exactly as before.
"""
import hashlib
import shutil
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

SEED_DIR = Path(__file__).parent.parent / "data"
SESSIONS_ROOT = Path(tempfile.gettempdir()) / "zamp_invoice_sessions"
REFERENCE_FILES = ("vendors.csv", "purchase_orders.csv")


@dataclass(frozen=True)
class Workspace:
    root: Path

    @property
    def db_path(self) -> Path:
        return self.root / "processing_history.db"

    @property
    def vendors_path(self) -> Path:
        return self.root / "vendors.csv"

    @property
    def pos_path(self) -> Path:
        return self.root / "purchase_orders.csv"

    @property
    def uploads_dir(self) -> Path:
        d = self.root / "uploads"
        d.mkdir(parents=True, exist_ok=True)
        return d


DEFAULT_WORKSPACE = Workspace(SEED_DIR)


def new_session_workspace() -> Workspace:
    """Fresh workspace seeded with the repo's default vendors and POs."""
    _purge_stale()
    root = SESSIONS_ROOT / uuid.uuid4().hex
    root.mkdir(parents=True, exist_ok=True)
    ws = Workspace(root)
    reset_reference_data(ws)
    return ws


def workspace_for_client(client_id: str) -> Workspace:
    """A stable private workspace for one API client (same id, same workspace),
    created and seeded on first use. The id is hashed, so it is never used as a path."""
    digest = hashlib.sha256(client_id.encode("utf-8")).hexdigest()[:20]
    ws = Workspace(SESSIONS_ROOT / f"api-{digest}")
    if not ws.vendors_path.exists():
        ws.root.mkdir(parents=True, exist_ok=True)
        reset_reference_data(ws)
    else:
        ws.root.touch()            # keeps an active client's folder out of the stale-session purge
    return ws


def reset_reference_data(ws: Workspace) -> None:
    """Restore this workspace's vendor master and PO dataset to the defaults."""
    ws.root.mkdir(parents=True, exist_ok=True)
    for name in REFERENCE_FILES:
        shutil.copyfile(SEED_DIR / name, ws.root / name)


def _purge_stale(max_age_hours: float = 12) -> None:
    """Best-effort cleanup of abandoned session folders so a long-running
    server does not accumulate them forever."""
    if not SESSIONS_ROOT.exists():
        return
    cutoff = time.time() - max_age_hours * 3600
    for d in SESSIONS_ROOT.iterdir():
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass
