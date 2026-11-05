"""Команды сервиса: python -m app <команда>"""

import argparse
import asyncio
import contextlib

from app.core.db import engine
from app.core.logs import configure_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app", description="Сервис игр Metacritic")
    commands = parser.add_subparsers(dest="command", required=True, metavar="команда")
    commands.add_parser("worker", help="обход по расписанию и ручные запуски")
    commands.add_parser("crawl", help="один проход обхода прямо сейчас")
    commands.add_parser("db-roles", help="создать или обновить роли базы app_web и app_worker")

    users = commands.add_parser("users", help="пользователи служебной части")
    actions = users.add_subparsers(dest="action", required=True, metavar="действие")
    actions.add_parser("create-admin", help="создать администратора").add_argument("login")
    actions.add_parser("set-password", help="сменить пароль и завершить сессии").add_argument(
        "login"
    )
    return parser


async def run(args: argparse.Namespace) -> None:
    # Импорты внутри команд: users и db-roles не тянут fastembed и LLM-клиенты
    try:
        if args.command == "worker":
            from app.worker.main import run_worker

            await run_worker()
        elif args.command == "crawl":
            from app.worker.main import crawl_once

            run_id = await crawl_once()
            print("Проход уже идёт" if run_id is None else f"Проход {run_id} завершён")
        elif args.command == "db-roles":
            from app.core.db_roles import setup_roles

            await setup_roles()
        elif args.command == "users":
            from app.auth.cli import create_admin, set_password

            action = create_admin if args.action == "create-admin" else set_password
            await action(args.login)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    if args.command in ("worker", "crawl"):
        configure_logging()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(args))
