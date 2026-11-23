import asyncio
from contextlib import asynccontextmanager
import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.db import init_db
from app.routers import (
    accounts,
    auth,
    clips,
    health,
    metrics,
    queue,
    streamers,
    studio,
    system,
    targets,
)
from app.services.copywriter import CopywriterService
from app.services.google_drive import GoogleDriveService
from app.services.learning import LearningService
from app.services.metrics_collector import MetricsCollectorService
from app.services.neon_client import NeonClippingClient
from app.services.processing_queue import ProcessingQueueWorker
from app.services.publish_scheduler import PublishSchedulerWorker
from app.services.recorder import RecorderManager
from app.services.retention_gc import RetentionGC
from app.services.studio_service import StudioService
from app.services.telegram_bot import TelegramBotService
from app.services.watcher_manager import WatcherManager
from app.services.youtube_publisher import YouTubePublisherService
from app.storage import ensure_storage_dirs, ensure_studio_dir

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialize storage, SQLite schema, and Phase 2/3/4 background workers."""
    settings = get_settings()
    ensure_storage_dirs(settings)
    ensure_studio_dir(settings)
    await init_db(settings.db_path)

    copywriter = CopywriterService(settings)
    google_drive = GoogleDriveService(settings)
    neon_client = NeonClippingClient(
        base_url=settings.neonclipping_url,
        data_dir=settings.neonclipping_data_dir,
    )
    youtube_publisher = YouTubePublisherService(settings)
    telegram_bot = TelegramBotService(settings, publisher=youtube_publisher)
    processing_queue = ProcessingQueueWorker(
        settings,
        neon_client=neon_client,
        copywriter=copywriter,
        google_drive=google_drive,
        on_chunk_drafts_ready=telegram_bot.notify_chunk_ready,
    )
    studio_service = StudioService(
        settings,
        gpu_lock=processing_queue.gpu_lock,
        neon_client=neon_client,
        copywriter=copywriter,
        google_drive=google_drive,
    )
    recorder = RecorderManager(
        settings,
        on_chunk_queued=processing_queue.enqueue_chunk,
        on_session_problem=telegram_bot.notify_recording_problem,
    )
    watcher_manager = WatcherManager(
        settings,
        on_online=recorder.start_recording,
        on_offline=recorder.request_stop,
        is_recording=recorder.is_recording,
        offline_confirm_polls=2,
    )
    retention_gc = RetentionGC(settings)
    metrics_collector = MetricsCollectorService(settings)
    learning = LearningService(settings)
    publish_scheduler = PublishSchedulerWorker(settings, publisher=youtube_publisher)

    app.state.copywriter = copywriter
    app.state.google_drive = google_drive
    app.state.neon_client = neon_client
    app.state.youtube_publisher = youtube_publisher
    app.state.telegram_bot = telegram_bot
    app.state.processing_queue = processing_queue
    app.state.studio_service = studio_service
    app.state.recorder = recorder
    app.state.watcher_manager = watcher_manager
    app.state.retention_gc = retention_gc
    app.state.metrics_collector = metrics_collector
    app.state.learning = learning
    app.state.publish_scheduler = publish_scheduler

    if settings.enable_background_workers:
        await watcher_manager.start()
        await processing_queue.start()
        await retention_gc.start()
        await telegram_bot.start()
        await metrics_collector.start()
        await learning.start()
        await publish_scheduler.start()
        # Finish recordings a hard stop left behind; runs in the background because
        # recovering an hour of video takes a while.
        app.state.recovery_task = asyncio.create_task(recorder.recover_interrupted(), name="recover-recordings")

    try:
        yield
    finally:
        await publish_scheduler.stop()
        await studio_service.stop()
        await learning.stop()
        await metrics_collector.stop()
        await telegram_bot.stop()
        await watcher_manager.stop()
        await recorder.stop_all()
        await processing_queue.stop()
        await retention_gc.stop()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Clip Automation Pipeline API",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def errors_as_json(request: Request, call_next):
        # Registered before CORS so CORS wraps it: an unhandled error now reaches the
        # browser as a readable 500 instead of a bare "Failed to fetch".
        try:
            return await call_next(request)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Unhandled error on %s %s", request.method, request.url.path)
            return JSONResponse(status_code=500, content={"detail": f"{type(exc).__name__}: {exc}"})

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3000",
            "http://127.0.0.1:3000",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(accounts.router)
    app.include_router(streamers.router)
    app.include_router(targets.router)
    app.include_router(clips.router)
    app.include_router(metrics.router)
    app.include_router(system.router)
    app.include_router(queue.router)
    app.include_router(studio.router)

    return app


app = create_app()
