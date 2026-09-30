"""Background jobs, run as a second process: `python -m app.worker`.

Every minute it purges old replay-protection ids. Every 6 hours it re-runs the NIP-05 check
on approved organisations. The worker expires abandoned disbursements each minute; payments already
marked `PAYING` remain pending for wallet/provider reconciliation."""

import logging
import time
from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db.models import Organization
from app.db.session import get_engine
from app.directory.nip05 import Fetcher, check_nip05, make_fetcher
from app.payments.service import expire_disbursements
from app.settings import get_settings

log = logging.getLogger("worker")
NIP05_EVERY_SECONDS = 6 * 3600


def purge_seen_auth_events() -> int:
    keep = 2 * get_settings().nip98_window_seconds
    with get_engine().begin() as conn:
        result = conn.execute(
            text("DELETE FROM seen_auth_events WHERE seen_at < now() - make_interval(secs => :s)"),
            {"s": keep},
        )
    return result.rowcount


def recheck_nip05(fetch: Fetcher | None = None) -> dict[str, int]:
    """Suspends an approved organisation when its website definitely stops vouching for its
    key. A timeout or a 5xx is logged and retried next round, not held against it."""
    fetch = fetch or make_fetcher(get_settings())
    counts = {"verified": 0, "suspended": 0, "unreachable": 0}
    with Session(get_engine()) as db:
        orgs = db.scalars(
            select(Organization).where(Organization.status == "approved").with_for_update()
        ).all()
        for org in orgs:
            result = check_nip05(org.domain, org.nostr_pubkey, fetch)
            if result.ok:
                org.nip05_verified_at = datetime.now(UTC)
                counts["verified"] += 1
            elif result.definitive:
                org.status = "suspended"
                counts["suspended"] += 1
                log.warning("suspended %s: %s", org.domain, result.reason)
            else:
                counts["unreachable"] += 1
                log.info("could not check %s: %s", org.domain, result.reason)
        db.commit()
    return counts


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    last_nip05 = float("-inf")
    while True:
        try:
            removed = purge_seen_auth_events()
            if removed:
                log.info("purged %d seen auth events", removed)
            expired = expire_disbursements()
            if any(expired.values()):
                log.info("expired disbursements: %s", expired)
        except Exception:
            log.exception("periodic maintenance failed")
        if time.monotonic() - last_nip05 >= NIP05_EVERY_SECONDS:
            try:
                log.info("nip05 recheck: %s", recheck_nip05())
            except Exception:
                log.exception("nip05 recheck failed")
            last_nip05 = time.monotonic()
        time.sleep(60)


if __name__ == "__main__":
    main()
