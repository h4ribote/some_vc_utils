"""Command line entry point: one image, one console script, the subcommand selects the role."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from pydantic import ValidationError

from svu import __version__
from svu.config import ConfigError, Settings, format_validation_error, get_settings, reset_settings_cache
from svu.logging import configure_logging

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_CONFIG = 78  # EX_CONFIG from sysexits.h

log = logging.getLogger("svu.cli")


def _migrate(settings: Settings, args: argparse.Namespace) -> int:
    from svu.db.migrate import upgrade

    log.info("applying migrations", extra={"db_host": settings.db_host, "db_name": settings.db_name})
    upgrade(settings.database_url, "head")
    log.info("migrations applied")
    return EXIT_OK


def _bot(settings: Settings, args: argparse.Namespace) -> int:
    from svu.bot.app import run_bot

    return run_bot(settings)


def _api(settings: Settings, args: argparse.Namespace) -> int:
    import uvicorn

    from svu.api.app import build_state, create_app

    app = create_app(build_state(settings))
    # The API is reachable only through cloudflared on the compose network, so forwarded headers are trusted from any peer.
    config = uvicorn.Config(app, host=settings.api_host, port=settings.api_port, log_config=None, proxy_headers=True, forwarded_allow_ips="*", server_header=False, lifespan="on")
    server = uvicorn.Server(config)
    log.info("starting API", extra={"host": settings.api_host, "port": settings.api_port})
    server.run()
    if not server.started:
        log.error("API did not start")
        return EXIT_FAILURE
    log.info("API stopped")
    return EXIT_OK


def _worker(settings: Settings, args: argparse.Namespace) -> int:
    from svu.worker.app import run_worker

    return run_worker(settings)


def _backup(settings: Settings, args: argparse.Namespace) -> int:
    from svu.backup import prune_backups, take_backup

    target = take_backup(settings)
    pruned = prune_backups(settings.backup_dir, settings.backup_retention)
    log.info("database backup written", extra={"file": target.name, "pruned": len(pruned)})
    print(target)
    return EXIT_OK


def _restore(settings: Settings, args: argparse.Namespace) -> int:
    from svu.backup import restore_backup

    path = Path(args.file)
    if not path.is_absolute():
        path = settings.backup_dir / path
    restore_backup(settings, path)
    log.info("database restored", extra={"file": path.name})
    return EXIT_OK


def _import_sqlite(settings: Settings, args: argparse.Namespace) -> int:
    from svu.db.engine import create_engine
    from svu.importer import SQLiteImportError, import_sqlite
    from svu.vc import VCClient

    if args.match_wallet:
        settings.require("vc_client_id", "vc_client_secret")

    async def run() -> int:
        wallet = None
        if args.match_wallet:
            vc = VCClient(settings.vc_base_url, settings.secret("vc_client_id"), settings.secret("vc_client_secret"))
            try:
                wallet = await vc.balances()
            finally:
                await vc.close()
        engine = create_engine(settings.database_url)
        try:
            report = await import_sqlite(engine, Path(args.file), wallet=wallet, dry_run=args.dry_run)
        except SQLiteImportError as exc:
            print(f"Import refused: {exc}", file=sys.stderr)
            return EXIT_FAILURE
        finally:
            await engine.dispose()
        verb = "Would import" if args.dry_run else "Imported"
        print(
            f"{verb} {report.guilds} guilds ({report.balances} pool balances), {report.rules} reward rules"
            f" ({report.skipped_rules} skipped), {report.cooldowns} cooldowns and {report.users} DM opt-ins."
        )
        for adj in report.adjustments:
            print(f"Pool of guild {adj.guild_id} raised from {adj.recorded} to {adj.imported} {adj.unit} to match the wallet.")
        for unit, amount in report.unassigned.items():
            print(f"The wallet also holds {amount} {unit}, which no pool uses; it stays in the wallet.")
        return EXIT_OK

    return asyncio.run(run())


def _vc_sync(settings: Settings, args: argparse.Namespace) -> int:
    from svu.db.engine import create_engine
    from svu.vc import VCClient
    from svu.worker.jobs import sync_vc_app

    settings.require("vc_client_id", "vc_client_secret", "public_base_url")

    async def run() -> list[str]:
        engine = create_engine(settings.database_url)
        vc = VCClient(settings.vc_base_url, settings.secret("vc_client_id"), settings.secret("vc_client_secret"))
        try:
            return await sync_vc_app(engine, vc, settings.public_base_url)
        finally:
            await vc.close()
            await engine.dispose()

    changed = asyncio.run(run())
    print(f"VirtualCrypto app updated: {', '.join(changed)}" if changed else "VirtualCrypto app already up to date")
    return EXIT_OK


HELP: dict[str, str] = {
    "migrate": "Apply database migrations up to the latest revision.",
    "bot": "Run the Discord bot.",
    "api": "Run the webhook receiver and the dashboard.",
    "worker": "Run payouts, notifications and the other periodic jobs.",
    "backup": "Take a database backup now and prune old ones.",
    "restore": "Replace the database contents with a backup; stop bot, api and worker first.",
    "import-sqlite": "Import the previous SQLite database into an empty database.",
    "vc-sync": "Point the VirtualCrypto app's webhook at this deployment and record its public key.",
}

COMMANDS: dict[str, Callable[[Settings, argparse.Namespace], int]] = {
    "migrate": _migrate,
    "bot": _bot,
    "api": _api,
    "worker": _worker,
    "backup": _backup,
    "restore": _restore,
    "import-sqlite": _import_sqlite,
    "vc-sync": _vc_sync,
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="svu", description="Some VC Utils service entry point.")
    parser.add_argument("--version", action="version", version=f"svu {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="command")
    for name, help_text in HELP.items():
        sub = subparsers.add_parser(name, help=help_text, description=help_text)
        if name == "restore":
            sub.add_argument("file", help="Backup file name in the backup directory, or an absolute path.")
        if name == "import-sqlite":
            sub.add_argument("file", help="Path of the previous database.db inside the container.")
            sub.add_argument("--match-wallet", action="store_true", help="Raise each pool to what the bot's VirtualCrypto wallet holds in its unit.")
            sub.add_argument("--dry-run", action="store_true", help="Report what would be imported, then roll back.")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, load and validate settings, configure logging, then run the chosen command and return its exit code."""
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    reset_settings_cache()
    try:
        settings = get_settings()
    except ValidationError as exc:
        print(format_validation_error(exc), file=sys.stderr)
        return EXIT_CONFIG
    except ConfigError as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    configure_logging(settings.log_level, settings.secret_values())
    try:
        return COMMANDS[args.command](settings, args)
    except ConfigError as exc:
        print(f"Invalid configuration: {exc}", file=sys.stderr)
        return EXIT_CONFIG
    except Exception:
        log.exception("command failed", extra={"command": args.command})
        return EXIT_FAILURE
