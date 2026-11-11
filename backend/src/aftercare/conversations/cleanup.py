"""Preview or discard private message uploads left uncommitted for at least 24 hours."""

import argparse
import json

from sqlalchemy.exc import SQLAlchemyError

from aftercare.business.errors import BusinessError
from aftercare.business.service import BusinessService
from aftercare.config import load_settings
from aftercare.db import guarded_engine

from .service import ConversationService


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("app", "test"), required=True)
    parser.add_argument("--limit", type=int, choices=range(1, 101), default=100)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    try:
        settings = load_settings(args.profile)
        engine = guarded_engine(settings)
        try:
            result = ConversationService(BusinessService(engine, settings)).cleanup_staged(
                limit=args.limit, execute=args.execute
            )
        finally:
            engine.dispose()
    except BusinessError as error:
        parser.exit(1, f"{error.code}\n")
    except (OSError, ValueError, SQLAlchemyError):
        parser.exit(1, "CHAT_CLEANUP_FAILED\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
