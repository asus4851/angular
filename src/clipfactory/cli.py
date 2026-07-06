"""Typer CLI: init, run, demo, poll, channel/account/route management, status.

`clipfactory run` wires together the FastAPI app, the pipeline worker and the
scheduler in a single process (see docs/ARCHITECTURE.md). Heavy submodules
(the API app, uvicorn) are imported lazily inside their commands so that
`clipfactory --help` stays fast and never fails for unrelated reasons.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import typer
from sqlalchemy import func

from clipfactory import crypto
from clipfactory.config import get_settings
from clipfactory.ingest import youtube as ingest_yt
from clipfactory.db import init_db, session_scope
from clipfactory.models import (
    Account,
    CandidateStatus,
    Channel,
    Clip,
    ClipCandidate,
    Job,
    JobStatus,
    Platform,
    Post,
    Route,
    Video,
)
from clipfactory.pipeline import (
    approve_candidate,
    drain_queue,
    enqueue,
    enqueue_due_polls,
    run_worker,
    start_scheduler,
    start_worker_thread,
)

app = typer.Typer(name="clipfactory", help="Turn YouTube videos into viral shorts and auto-post them.", no_args_is_help=True)
channel_app = typer.Typer(help="Manage source channels.", no_args_is_help=True)
account_app = typer.Typer(help="Manage social-network accounts.", no_args_is_help=True)
route_app = typer.Typer(help="Manage channel -> account routes.", no_args_is_help=True)
app.add_typer(channel_app, name="channel")
app.add_typer(account_app, name="account")
app.add_typer(route_app, name="route")


# ---------------------------------------------------------------------------
# Core lifecycle
# ---------------------------------------------------------------------------


@app.command()
def init() -> None:
    """Create data directories and database tables."""
    init_db()
    settings = get_settings()
    typer.echo(f"Initialized database at {settings.database_url}")


@app.command("gen-key")
def gen_key() -> None:
    """Generate a Fernet key suitable for SECRET_KEY."""
    typer.echo(crypto.generate_key())


@app.command()
def run() -> None:
    """Run the API (with dashboard), the worker and the scheduler in one process."""
    import uvicorn

    init_db()
    settings = get_settings()

    stop_event = threading.Event()
    start_worker_thread(stop_event)
    start_scheduler(stop_event)

    try:
        uvicorn.run(
            "clipfactory.api.main:create_app",
            factory=True,
            host=settings.host,
            port=settings.port,
        )
    finally:
        stop_event.set()


@app.command()
def work() -> None:
    """Run the job worker in the foreground until interrupted (Ctrl-C)."""
    init_db()
    stop_event = threading.Event()
    try:
        run_worker(stop_event)
    except KeyboardInterrupt:
        stop_event.set()


@app.command()
def poll() -> None:
    """Enqueue POLL_CHANNEL jobs for every channel whose interval has elapsed."""
    init_db()
    count = enqueue_due_polls()
    typer.echo(f"Enqueued {count} poll job(s)")


@app.command()
def demo() -> None:
    """Run a fully offline demo: synthetic video + heuristic analysis + local export."""
    result = run_demo()
    typer.echo(f"Demo complete: {result['jobs_processed']} job(s) processed.")
    typer.echo(f"Exported clip(s) under: {result['export_dir']}")
    for path in result["exported_files"]:
        typer.echo(f"  {path}")


@app.command()
def status() -> None:
    """Show entity counts by status across the pipeline."""
    init_db()
    with session_scope() as session:
        for label, model in (
            ("videos", Video),
            ("candidates", ClipCandidate),
            ("clips", Clip),
            ("posts", Post),
            ("jobs", Job),
        ):
            typer.echo(f"{label}:")
            rows = session.query(model.status, func.count()).group_by(model.status).all()
            if not rows:
                typer.echo("  (none)")
                continue
            for value, count in rows:
                status_name = value.value if hasattr(value, "value") else value
                typer.echo(f"  {status_name}: {count}")


# ---------------------------------------------------------------------------
# approve / reject
# ---------------------------------------------------------------------------


@app.command()
def approve(candidate_id: int) -> None:
    """Approve a pending clip candidate and enqueue its render."""
    init_db()
    with session_scope() as session:
        candidate = session.get(ClipCandidate, candidate_id)
        if candidate is None:
            typer.echo(f"No such candidate: {candidate_id}", err=True)
            raise typer.Exit(code=1)
        if candidate.status == CandidateStatus.APPROVED:
            typer.echo(f"Candidate {candidate_id} is already approved")
            return
        approve_candidate(session, candidate)
    typer.echo(f"Approved candidate {candidate_id}; render enqueued")


@app.command()
def reject(candidate_id: int) -> None:
    """Reject a pending clip candidate."""
    init_db()
    with session_scope() as session:
        candidate = session.get(ClipCandidate, candidate_id)
        if candidate is None:
            typer.echo(f"No such candidate: {candidate_id}", err=True)
            raise typer.Exit(code=1)
        candidate.status = CandidateStatus.REJECTED
    typer.echo(f"Rejected candidate {candidate_id}")


# ---------------------------------------------------------------------------
# channel
# ---------------------------------------------------------------------------


@channel_app.command("add")
def channel_add(
    url: str,
    auto_approve: bool = typer.Option(False, "--auto-approve"),
    interval: int = typer.Option(30, "--interval", help="Poll interval in minutes"),
    max_clips: int = typer.Option(3, "--max-clips"),
    min_score: int = typer.Option(60, "--min-score"),
    language: str = typer.Option("", "--language"),
) -> None:
    """Resolve a channel URL/handle/id and add it as a source."""
    init_db()
    info = ingest_yt.resolve_channel(url)
    with session_scope() as session:
        existing = session.query(Channel).filter(Channel.yt_channel_id == info.yt_channel_id).one_or_none()
        if existing is not None:
            typer.echo(f"Channel already exists: {existing.id} ({existing.yt_channel_id})")
            return
        channel = Channel(
            yt_channel_id=info.yt_channel_id,
            title=info.title,
            url=info.url,
            auto_approve=auto_approve,
            check_interval_min=interval,
            max_clips_per_video=max_clips,
            min_score=min_score,
            language=language,
        )
        session.add(channel)
        session.flush()
        typer.echo(f"Added channel {channel.id}: {channel.title or channel.yt_channel_id}")


@channel_app.command("list")
def channel_list() -> None:
    init_db()
    with session_scope() as session:
        channels = session.query(Channel).order_by(Channel.id).all()
        if not channels:
            typer.echo("(no channels)")
            return
        for c in channels:
            typer.echo(
                f"{c.id}\t{c.yt_channel_id}\t{c.title!r}\tenabled={c.enabled}\t"
                f"auto_approve={c.auto_approve}\tinterval={c.check_interval_min}m\t"
                f"min_score={c.min_score}\tmax_clips={c.max_clips_per_video}"
            )


@channel_app.command("remove")
def channel_remove(channel_id: int) -> None:
    init_db()
    with session_scope() as session:
        channel = session.get(Channel, channel_id)
        if channel is None:
            typer.echo(f"No such channel: {channel_id}", err=True)
            raise typer.Exit(code=1)
        session.delete(channel)
    typer.echo(f"Removed channel {channel_id}")


# ---------------------------------------------------------------------------
# account
# ---------------------------------------------------------------------------


def _read_credentials(credentials_json: str | None) -> dict:
    if not credentials_json:
        return {}
    raw = sys.stdin.read() if credentials_json == "-" else Path(credentials_json).read_text(encoding="utf-8")
    return json.loads(raw) if raw.strip() else {}


@account_app.command("add")
def account_add(
    platform: Platform = typer.Option(..., "--platform"),
    name: str = typer.Option(..., "--name"),
    credentials_json: str | None = typer.Option(
        None, "--credentials-json", help="Path to a JSON file with credentials, or '-' for stdin"
    ),
) -> None:
    """Add a publishing account. Credentials are encrypted at rest."""
    init_db()
    creds = _read_credentials(credentials_json)
    encrypted = crypto.encrypt_credentials(creds) if creds else ""
    with session_scope() as session:
        existing = (
            session.query(Account).filter(Account.platform == platform, Account.name == name).one_or_none()
        )
        if existing is not None:
            typer.echo(f"Account already exists: {existing.id} ({platform.value}/{name})")
            return
        account = Account(platform=platform, name=name, credentials_encrypted=encrypted)
        session.add(account)
        session.flush()
        typer.echo(f"Added account {account.id}: {platform.value}/{name}")


@account_app.command("list")
def account_list() -> None:
    init_db()
    with session_scope() as session:
        accounts = session.query(Account).order_by(Account.id).all()
        if not accounts:
            typer.echo("(no accounts)")
            return
        for a in accounts:
            has_creds = bool(a.credentials_encrypted)
            typer.echo(f"{a.id}\t{a.platform.value}\t{a.name}\tenabled={a.enabled}\thas_credentials={has_creds}")


@account_app.command("remove")
def account_remove(account_id: int) -> None:
    init_db()
    with session_scope() as session:
        account = session.get(Account, account_id)
        if account is None:
            typer.echo(f"No such account: {account_id}", err=True)
            raise typer.Exit(code=1)
        session.delete(account)
    typer.echo(f"Removed account {account_id}")


# ---------------------------------------------------------------------------
# route
# ---------------------------------------------------------------------------


@route_app.command("add")
def route_add(
    channel: int = typer.Option(..., "--channel"),
    account: int = typer.Option(..., "--account"),
    hashtags: str = typer.Option("", "--hashtags", help="Comma-separated extra hashtags"),
) -> None:
    init_db()
    extra_hashtags = [tag.strip() for tag in hashtags.split(",") if tag.strip()]
    with session_scope() as session:
        if session.get(Channel, channel) is None:
            typer.echo(f"No such channel: {channel}", err=True)
            raise typer.Exit(code=1)
        if session.get(Account, account) is None:
            typer.echo(f"No such account: {account}", err=True)
            raise typer.Exit(code=1)
        existing = (
            session.query(Route)
            .filter(Route.channel_id == channel, Route.account_id == account)
            .one_or_none()
        )
        if existing is not None:
            typer.echo(f"Route already exists: {existing.id}")
            return
        route = Route(channel_id=channel, account_id=account, extra_hashtags=extra_hashtags)
        session.add(route)
        session.flush()
        typer.echo(f"Added route {route.id}: channel {channel} -> account {account}")


@route_app.command("list")
def route_list() -> None:
    init_db()
    with session_scope() as session:
        routes = session.query(Route).order_by(Route.id).all()
        if not routes:
            typer.echo("(no routes)")
            return
        for r in routes:
            typer.echo(
                f"{r.id}\tchannel={r.channel_id}\taccount={r.account_id}\t"
                f"enabled={r.enabled}\thashtags={r.extra_hashtags}"
            )


@route_app.command("remove")
def route_remove(route_id: int) -> None:
    init_db()
    with session_scope() as session:
        route = session.get(Route, route_id)
        if route is None:
            typer.echo(f"No such route: {route_id}", err=True)
            raise typer.Exit(code=1)
        session.delete(route)
    typer.echo(f"Removed route {route_id}")


# ---------------------------------------------------------------------------
# demo (offline end-to-end run, factored out so tests can call it directly)
# ---------------------------------------------------------------------------

_DEMO_CHANNEL_ID = "UC_demo000000000000000"
_DEMO_VIDEO_ID = "demo00000001"
_DEMO_DURATION_SEC = 30.0

_DEMO_TRANSCRIPT_LINES = [
    "Why does nobody talk about this secret trick?",
    "Here are the 3 mistakes everyone makes on day one.",
    "Never do this if you want to save time!",
    "How did I go from zero to a hundred so fast?",
    "This is the number 1 secret nobody shares with you.",
    "Why is this simple mistake costing you so much?",
    "Never ignore these 5 warning signs again!",
    "How can you fix this in under 10 minutes?",
    "This one secret changed everything for me.",
    "Why do most people never get this right?",
]


def _build_demo_transcript_segments():
    from clipfactory.schemas import TranscriptSegment

    n = len(_DEMO_TRANSCRIPT_LINES)
    step = _DEMO_DURATION_SEC / n
    segments = []
    t = 0.0
    for line in _DEMO_TRANSCRIPT_LINES:
        segments.append(TranscriptSegment(start=t, end=t + step, text=line))
        t += step
    return segments


def _get_or_create_demo_channel(session) -> Channel:
    channel = session.query(Channel).filter(Channel.yt_channel_id == _DEMO_CHANNEL_ID).one_or_none()
    if channel is not None:
        return channel
    channel = Channel(
        yt_channel_id=_DEMO_CHANNEL_ID,
        title="ClipFactory Demo Channel",
        url=f"https://www.youtube.com/channel/{_DEMO_CHANNEL_ID}",
        enabled=True,
        auto_approve=True,
        check_interval_min=30,
        max_clips_per_video=3,
        min_score=40,
        language="en",
        render_preset={"video_bitrate": "800k", "audio_bitrate": "96k"},
    )
    session.add(channel)
    session.flush()
    return channel


def _get_or_create_demo_account(session) -> Account:
    account = (
        session.query(Account).filter(Account.platform == Platform.LOCAL, Account.name == "demo").one_or_none()
    )
    if account is not None:
        return account
    account = Account(platform=Platform.LOCAL, name="demo", credentials_encrypted="", enabled=True)
    session.add(account)
    session.flush()
    return account


def _get_or_create_demo_route(session, channel: Channel, account: Account) -> Route:
    route = (
        session.query(Route)
        .filter(Route.channel_id == channel.id, Route.account_id == account.id)
        .one_or_none()
    )
    if route is not None:
        return route
    route = Route(channel_id=channel.id, account_id=account.id, extra_hashtags=["demo"])
    session.add(route)
    session.flush()
    return route


def run_demo() -> dict:
    """Run the whole pipeline offline: synthetic video, heuristic analysis, local export.

    No network access or API keys are required: a short synthetic video is
    generated with ffmpeg, paired with a synthetic hooky transcript so the
    offline `HeuristicAnalyzer` reliably produces candidates, then the queue
    is drained synchronously. Factored out of the `demo` command so tests can
    call it directly and assert on the result.
    """
    from clipfactory import media
    from clipfactory.models import JobType, Transcript, Video, VideoStatus
    from clipfactory.transcripts.youtube import segments_to_json

    init_db()
    settings = get_settings()

    with session_scope() as session:
        channel = _get_or_create_demo_channel(session)
        account = _get_or_create_demo_account(session)
        _get_or_create_demo_route(session, channel, account)

        source_path = settings.sources_dir / f"{_DEMO_VIDEO_ID}.mp4"
        if not source_path.exists():
            media.make_test_video(source_path, duration=_DEMO_DURATION_SEC)

        video = session.query(Video).filter(Video.yt_video_id == _DEMO_VIDEO_ID).one_or_none()
        if video is None:
            video = Video(
                channel_id=channel.id,
                yt_video_id=_DEMO_VIDEO_ID,
                title="ClipFactory Demo Video",
                duration_sec=_DEMO_DURATION_SEC,
                status=VideoStatus.TRANSCRIBED,
            )
            session.add(video)
            session.flush()
            session.add(
                Transcript(
                    video_id=video.id,
                    language="en",
                    source="auto",
                    segments=segments_to_json(_build_demo_transcript_segments()),
                )
            )
            session.flush()

        video_id = video.id
        if video.status == VideoStatus.TRANSCRIBED:
            enqueue(session, JobType.ANALYZE_VIDEO, {"video_id": video_id})

    processed = drain_queue(max_jobs=50)

    export_dir = settings.export_dir
    exported_files = sorted(export_dir.rglob("*.mp4")) if export_dir.exists() else []
    return {
        "jobs_processed": processed,
        "export_dir": export_dir,
        "exported_files": exported_files,
        "video_id": video_id,
    }


if __name__ == "__main__":
    app()
