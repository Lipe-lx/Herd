import argparse
import importlib.util
import logging
import os
import sys
from pathlib import Path

from telegram.ext import Application, MessageHandler, filters

from . import bridge, cron_manager, gatekeeper
from .env_loader import load_env

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


def build_app(config: dict, herd_path: Path) -> Application:
    app = Application.builder().token(config["telegram_bot_token"]).build()
    app.bot_data["config"] = config
    app.bot_data["herd_path"] = herd_path

    app.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, gatekeeper.handle_new_member),
        group=0,
    )
    app.add_handler(
        MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, gatekeeper.handle_dm),
        group=0,
    )
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & filters.TEXT, gatekeeper.dispatch_command),
        group=0,
    )
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & filters.TEXT, cron_manager.handle_cron),
        group=1,
    )
    app.add_handler(
        MessageHandler(filters.ChatType.GROUPS & filters.TEXT & ~filters.COMMAND, bridge.handle_message),
        group=2,
    )

    if importlib.util.find_spec("apscheduler") is not None and app.job_queue is not None:
        app.job_queue.run_repeating(cron_manager.cron_tick, interval=60)
        logger.info("CRON scheduler started with PTB JobQueue.")
    else:
        cron_manager.start_scheduler(config["telegram_bot_token"])

    return app


def main() -> None:
    load_env()
    parser = argparse.ArgumentParser(description="Herd Unified Runner")
    parser.add_argument("--config", type=Path, default=Path(os.getenv("CONFIG_PATH", "config.json")))
    parser.add_argument("--ui", action="store_true", help="Starts the local dashboard server.")
    parser.add_argument(
        "--open-browser",
        action="store_true",
        help="Legacy flag. The dashboard already opens automatically when used with --ui.",
    )
    args = parser.parse_args()

    config = bridge.load_config(args.config)
    if not config:
        logger.error("Failed to load configuration. Run ./start.sh setup first.")
        sys.exit(1)

    if not bridge.is_config_complete(config):
        logger.error("Missing required configuration. Run ./start.sh setup or complete the .env/config.json values.")
        sys.exit(1)

    herd_path = Path(config["scope"]) / ".herd"
    bridge.bootstrap_herd_dir(config["scope"], config)
    if config.get("auto_register", True):
        bridge.upsert_member_registration(config)

    bridge_state = {
        "status": {
            "online": True,
            "setup_complete": True,
            "alias": config["alias"],
            "scope": config["scope"],
            "allow_project_changes": bridge.allow_project_changes(config),
            "access_mode": bridge._status_access_mode(config),
        },
        "herd_path": str(herd_path),
        "config_path": args.config,
        "config_ref": config,
    }

    if args.ui:
        bridge.start_ui_server(bridge_state, open_browser=True)
        logger.info("Dashboard opened through the local authenticated UI session.")

    logger.info("Starting Herd unified polling...")
    app = build_app(config, herd_path)
    bridge_state["bot_data_ref"] = app.bot_data
    app.bot_data["bridge_state"] = bridge_state
    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
