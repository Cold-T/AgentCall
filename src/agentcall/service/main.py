import argparse
import logging

import uvicorn

from agentcall.api.app import create_app
from agentcall.service.config import Config


def main():
    parser = argparse.ArgumentParser(description="AgentCall headless Bluetooth service")
    parser.add_argument("--config", help="TOML configuration file")
    parser.add_argument(
        "--log-level", default="info", choices=("debug", "info", "warning", "error")
    )
    args = parser.parse_args()
    config = Config.load(args.config)
    logging.basicConfig(
        level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    uvicorn.run(create_app(config), host=config.host, port=config.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
